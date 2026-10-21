from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .models import Couple, CoupleMembership, Invitation
from .services import (
    CoupleRuleError,
    accept_invitation,
    create_invitation,
)


User = get_user_model()
FAST_PASSWORD_HASHER = "django.contrib.auth.hashers.MD5PasswordHasher"


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class InvitationTests(TestCase):
    def setUp(self):
        self.inviter = User.objects.create_user(
            username="invite_giver",
            password="a-long-test-password",
        )
        self.partner = User.objects.create_user(
            username="partner",
            password="a-long-test-password",
        )

    def test_invitation_is_hashed_expires_and_creates_owner_plant(self):
        created = create_invitation(self.inviter)

        self.assertNotEqual(created.invitation.token_hash, created.code)
        self.assertEqual(len(created.invitation.token_hash), 64)
        self.assertGreater(created.invitation.expires_at, timezone.now())
        self.assertLessEqual(
            created.invitation.expires_at - created.invitation.created_at,
            timedelta(days=7),
        )
        self.assertTrue(hasattr(self.inviter, "flower_plant"))
        self.assertEqual(created.invitation.couple.memberships.count(), 1)

    def test_partner_acceptance_is_single_use_and_creates_both_plants(self):
        created = create_invitation(self.inviter)

        couple = accept_invitation(self.partner, created.code)

        self.assertEqual(couple.memberships.count(), 2)
        self.assertEqual(
            set(couple.memberships.values_list("user_id", flat=True)),
            {self.inviter.pk, self.partner.pk},
        )
        self.assertEqual(
            set(couple.memberships.values_list("slot", flat=True)),
            {1, 2},
        )
        self.assertTrue(hasattr(self.partner, "flower_plant"))
        created.invitation.refresh_from_db()
        self.assertIsNotNone(created.invitation.used_at)
        with self.assertRaises(CoupleRuleError):
            accept_invitation(self.partner, created.code)

    def test_user_cannot_accept_their_own_invitation(self):
        created = create_invitation(self.inviter)

        with self.assertRaisesRegex(CoupleRuleError, "own invitation"):
            accept_invitation(self.inviter, created.code)

        self.assertEqual(created.invitation.couple.memberships.count(), 1)
        self.assertIsNone(created.invitation.used_at)

    def test_expired_invitation_cannot_be_accepted(self):
        created = create_invitation(self.inviter)

        with patch(
            "couples.services.timezone.now",
            return_value=created.invitation.expires_at + timedelta(seconds=1),
        ):
            with self.assertRaisesRegex(CoupleRuleError, "expired"):
                accept_invitation(self.partner, created.code)

        self.assertEqual(created.invitation.couple.memberships.count(), 1)
        self.assertFalse(hasattr(self.partner, "flower_plant"))

    def test_user_cannot_join_a_second_couple(self):
        first_invitation = create_invitation(self.inviter)
        other_inviter = User.objects.create_user(
            username="other_inviter",
            password="a-long-test-password",
        )
        second_invitation = create_invitation(other_inviter)
        accept_invitation(self.partner, first_invitation.code)

        with self.assertRaisesRegex(CoupleRuleError, "another partner"):
            accept_invitation(self.partner, second_invitation.code)

        self.assertIsNone(second_invitation.invitation.used_at)
        self.assertEqual(CoupleMembership.objects.filter(user=self.partner).count(), 1)

    def test_database_rejects_membership_in_a_second_couple(self):
        create_invitation(self.inviter)
        other_couple = Couple.objects.create()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CoupleMembership.objects.create(
                    couple=other_couple,
                    user=self.inviter,
                    slot=1,
                )

        self.assertEqual(CoupleMembership.objects.filter(user=self.inviter).count(), 1)

    def test_connected_user_cannot_create_another_invitation(self):
        created = create_invitation(self.inviter)
        accept_invitation(self.partner, created.code)

        with self.assertRaises(CoupleRuleError):
            create_invitation(self.inviter)

        self.assertEqual(Couple.objects.count(), 1)

    def test_expired_invitation_can_be_rotated_without_exposing_old_code(self):
        original = create_invitation(self.inviter)
        with patch(
            "couples.services.timezone.now",
            return_value=original.invitation.expires_at + timedelta(seconds=1),
        ):
            renewed = create_invitation(self.inviter)

        self.assertEqual(original.invitation.pk, renewed.invitation.pk)
        self.assertNotEqual(original.code, renewed.code)
        with self.assertRaisesRegex(CoupleRuleError, "not valid"):
            accept_invitation(self.partner, original.code)
        accept_invitation(self.partner, renewed.code)


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class CoupleViewTests(TestCase):
    def setUp(self):
        self.inviter = User.objects.create_user(
            username="inviter_view",
            password="a-long-test-password",
        )
        self.client.force_login(self.inviter)

    def test_invitation_post_redirects_and_displays_code_once(self):
        response = self.client.post("/couples/invite/")

        self.assertRedirects(response, "/garden/", fetch_redirect_response=False)
        dashboard = self.client.get("/garden/")
        code = dashboard.context["new_invitation_code"]
        self.assertTrue(code)
        self.assertContains(dashboard, code)
        self.assertIsNone(self.client.get("/garden/").context["new_invitation_code"])

    def test_invalid_invitation_code_shows_a_friendly_error(self):
        response = self.client.post(
            "/couples/accept/",
            {"code": "not-a-real-code"},
            follow=True,
        )

        self.assertContains(response, "That invitation is not valid.")
        self.assertEqual(Couple.objects.count(), 0)


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class ConcurrentInvitationAcceptanceTests(TransactionTestCase):
    def test_only_one_partner_can_accept_the_same_invitation_concurrently(self):
        inviter = User.objects.create_user(
            username="shared_inviter",
            password="a-long-test-password",
        )
        first_partner = User.objects.create_user(
            username="first_partner",
            password="a-long-test-password",
        )
        second_partner = User.objects.create_user(
            username="second_partner",
            password="a-long-test-password",
        )
        invitation = create_invitation(inviter)
        barrier = Barrier(2)

        def accept(user_id):
            close_old_connections()
            try:
                user = User.objects.get(pk=user_id)
                barrier.wait(timeout=10)
                try:
                    accept_invitation(user, invitation.code)
                    return True
                except CoupleRuleError:
                    return False
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    accept,
                    (first_partner.pk, second_partner.pk),
                )
            )

        self.assertEqual(sum(results), 1)
        self.assertEqual(invitation.invitation.couple.memberships.count(), 2)
        self.assertIsNotNone(
            Invitation.objects.get(pk=invitation.invitation.pk).used_at
        )

    def test_one_user_accepting_two_invitations_concurrently_joins_only_one(self):
        first_inviter = User.objects.create_user(
            username="first_inviter",
            password="a-long-test-password",
        )
        second_inviter = User.objects.create_user(
            username="second_inviter",
            password="a-long-test-password",
        )
        joining_user = User.objects.create_user(
            username="joining_user",
            password="a-long-test-password",
        )
        first = create_invitation(first_inviter)
        second = create_invitation(second_inviter)
        barrier = Barrier(2)

        def accept(code):
            close_old_connections()
            try:
                user = User.objects.get(pk=joining_user.pk)
                barrier.wait(timeout=10)
                try:
                    accept_invitation(user, code)
                    return True
                except CoupleRuleError:
                    return False
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(accept, (first.code, second.code)))

        self.assertEqual(sum(results), 1)
        self.assertEqual(CoupleMembership.objects.filter(user=joining_user).count(), 1)
        self.assertEqual(
            CoupleMembership.objects.filter(user=joining_user)
            .get()
            .couple.memberships.count(),
            2,
        )

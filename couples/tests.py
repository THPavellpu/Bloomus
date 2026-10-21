import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
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
class InvitationOnboardingTests(TestCase):
    def setUp(self):
        self.inviter = User.objects.create_user(username="link_inviter")
        self.created = create_invitation(self.inviter)
        self.url = reverse("couples:invitation_link", args=(self.created.code,))

    def signup_data(self, username="new_partner"):
        return {
            "username": username,
            "email": "partner@example.com",
            "password1": "MossyMeadow!9274",
            "password2": "MossyMeadow!9274",
        }

    def test_readable_codes_accept_lowercase_spaces_and_missing_hyphens(self):
        self.assertRegex(self.created.code, r"^BU(?:-[A-HJ-NP-Z2-9]{4}){4}$")
        partner = User.objects.create_user(username="code_partner")
        self.client.force_login(partner)
        pasted = "  " + self.created.code.lower().replace("-", " ") + "  "
        response = self.client.post(reverse("couples:accept"), {"code": pasted})
        self.assertRedirects(response, reverse("garden:dashboard"))
        self.assertEqual(
            partner.couple_membership.couple_id, self.created.invitation.couple_id
        )

    def test_old_urlsafe_codes_remain_usable(self):
        legacy_code = "aB_legacy-case-Sensitive-Token-123456789"
        Invitation.objects.filter(pk=self.created.invitation.pk).update(
            token_hash=hashlib.sha256(legacy_code.encode()).hexdigest()
        )
        partner = User.objects.create_user(username="legacy_partner")
        accept_invitation(partner, legacy_code)
        self.assertEqual(partner.couple_membership.slot, 2)

    def test_dashboard_displays_copy_code_link_and_paste_field(self):
        self.client.force_login(self.inviter)
        response = self.client.post(reverse("couples:invite"), follow=True)
        code = response.context["new_invitation_code"]
        link = "http://testserver" + reverse("couples:invitation_link", args=(code,))
        self.assertContains(response, link)
        self.assertContains(response, "Copy code")
        self.assertContains(response, "Copy link")
        self.assertContains(response, "Share invitation")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.client.logout()
        partner = User.objects.create_user(username="paste_partner")
        self.client.force_login(partner)
        self.assertContains(self.client.get(reverse("garden:dashboard")), 'name="code"')

    def test_link_preview_does_not_create_membership_or_pending_session(self):
        response = self.client.get(self.url)
        self.assertContains(response, self.inviter.username)
        self.assertContains(response, "Continue with this invitation")
        self.assertEqual(CoupleMembership.objects.count(), 1)
        self.assertNotIn("pending_invitation_code", self.client.session)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response["Cache-Control"])

    def test_new_user_signs_up_and_connects_automatically(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("signup"))
        self.assertContains(self.client.get(reverse("signup")), "connect you automatically")
        response = self.client.post(reverse("signup"), self.signup_data(), follow=True)
        partner = User.objects.get(username="new_partner")
        self.assertRedirects(response, reverse("garden:dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), partner.pk)
        self.assertEqual(
            partner.couple_membership.couple_id, self.created.invitation.couple_id
        )
        self.assertTrue(hasattr(partner, "flower_plant"))
        self.assertNotIn("pending_invitation_code", self.client.session)
        self.assertContains(response, "You and your partner are connected!")

    def test_existing_user_signs_in_and_connects_automatically(self):
        partner = User.objects.create_user(
            username="existing_partner", password="MossyMeadow!9274"
        )
        self.client.post(self.url)
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Sign in to connect automatically")
        response = self.client.post(
            reverse("login"),
            {"username": partner.username, "password": "MossyMeadow!9274"},
        )
        self.assertRedirects(response, reverse("garden:dashboard"))
        self.assertEqual(partner.couple_membership.slot, 2)
        self.assertNotIn("pending_invitation_code", self.client.session)

    def test_invalid_signup_or_login_retains_pending_invitation(self):
        self.client.post(self.url)
        response = self.client.post(reverse("signup"), {"username": ""})
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("login"), {"username": "nobody", "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["pending_invitation_code"], self.created.code)
        self.assertEqual(CoupleMembership.objects.count(), 1)

    def test_signed_in_user_connects_only_after_post_and_refresh_is_safe(self):
        partner = User.objects.create_user(username="signed_in_partner")
        self.client.force_login(partner)
        self.client.get(self.url)
        self.assertFalse(CoupleMembership.objects.filter(user=partner).exists())
        self.assertRedirects(self.client.post(self.url), reverse("garden:dashboard"))
        self.assertEqual(self.client.get(self.url).status_code, 400)
        self.assertEqual(self.client.post(self.url).status_code, 400)
        self.assertEqual(CoupleMembership.objects.filter(user=partner).count(), 1)

    def test_expired_used_and_invalid_links_do_not_store_invitation(self):
        with patch(
            "couples.services.timezone.now",
            return_value=self.created.invitation.expires_at + timedelta(seconds=1),
        ):
            response = self.client.post(self.url)
            self.assertContains(response, "expired", status_code=400)
        partner = User.objects.create_user(username="used_partner")
        accept_invitation(partner, self.created.code)
        self.assertContains(self.client.get(self.url), "already been used", status_code=400)
        invalid_url = reverse("couples:invitation_link", args=("unknown",))
        self.assertContains(self.client.post(invalid_url), "not valid", status_code=400)
        self.assertNotIn("pending_invitation_code", self.client.session)

    def test_expiry_during_signup_keeps_account_but_reports_connection_failure(self):
        self.client.post(self.url)
        with patch(
            "couples.services.timezone.now",
            return_value=self.created.invitation.expires_at + timedelta(seconds=1),
        ):
            response = self.client.post(reverse("signup"), self.signup_data(), follow=True)
        self.assertContains(response, "We couldn&#x27;t connect you")
        self.assertContains(response, "expired")
        partner = User.objects.get(username="new_partner")
        self.assertFalse(CoupleMembership.objects.filter(user=partner).exists())
        self.assertNotIn("pending_invitation_code", self.client.session)

    def test_rotation_before_login_is_revalidated(self):
        partner = User.objects.create_user(username="late_partner", password="MossyMeadow!9274")
        self.client.post(self.url)
        create_invitation(self.inviter)
        response = self.client.post(
            reverse("login"),
            {"username": partner.username, "password": "MossyMeadow!9274"},
            follow=True,
        )
        self.assertContains(response, "That invitation is not valid.")
        self.assertFalse(CoupleMembership.objects.filter(user=partner).exists())

    def test_self_invitation_and_existing_membership_are_rejected(self):
        self.client.force_login(self.inviter)
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "own invitation")
        other = User.objects.create_user(username="already_inviting")
        create_invitation(other)
        self.client.force_login(other)
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "already connected to another partner")
        self.created.invitation.refresh_from_db()
        self.assertIsNone(self.created.invitation.used_at)

    def test_continue_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.get(self.url)
        self.assertEqual(csrf_client.post(self.url).status_code, 403)
        self.assertNotIn("pending_invitation_code", csrf_client.session)


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

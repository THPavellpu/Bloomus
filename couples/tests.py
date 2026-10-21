import hashlib
import os
import runpy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from garden.models import FlowerPlant

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

    def test_pending_inviter_can_join_partner_and_keep_their_plant(self):
        own = create_invitation(self.partner)
        plant = FlowerPlant.objects.get(owner=self.partner)
        target = create_invitation(self.inviter)

        couple = accept_invitation(self.partner, target.code)

        self.assertEqual(couple.pk, target.invitation.couple_id)
        self.assertEqual(couple.memberships.count(), 2)
        self.assertFalse(Couple.objects.filter(pk=own.invitation.couple_id).exists())
        self.assertFalse(Invitation.objects.filter(pk=own.invitation.pk).exists())
        self.assertEqual(FlowerPlant.objects.get(owner=self.partner).pk, plant.pk)
        self.assertEqual(FlowerPlant.objects.count(), 2)
        with self.assertRaisesRegex(CoupleRuleError, "not valid"):
            accept_invitation(User.objects.create_user(username="late_joiner"), own.code)

    def test_invalid_target_preserves_pending_invitation_and_plant(self):
        own = create_invitation(self.partner)
        plant = FlowerPlant.objects.get(owner=self.partner)

        with self.assertRaises(CoupleRuleError):
            accept_invitation(self.partner, "invalid")

        self.assertEqual(
            CoupleMembership.objects.get(user=self.partner).couple_id,
            own.invitation.couple_id,
        )
        self.assertTrue(Invitation.objects.filter(pk=own.invitation.pk).exists())
        self.assertEqual(FlowerPlant.objects.get(owner=self.partner).pk, plant.pk)

    def test_expired_target_preserves_pending_invitation(self):
        own = create_invitation(self.partner)
        target = create_invitation(self.inviter)
        with patch(
            "couples.services.timezone.now",
            return_value=target.invitation.expires_at + timedelta(seconds=1),
        ):
            with self.assertRaisesRegex(CoupleRuleError, "expired"):
                accept_invitation(self.partner, target.code)
        self.assertTrue(Invitation.objects.filter(pk=own.invitation.pk).exists())
        self.assertEqual(
            CoupleMembership.objects.get(user=self.partner).couple_id,
            own.invitation.couple_id,
        )

    def test_failed_join_rolls_back_pending_invitation_replacement(self):
        own = create_invitation(self.partner)
        target = create_invitation(self.inviter)
        with patch.object(
            Invitation, "save", side_effect=RuntimeError("invitation write failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "invitation write failed"):
                accept_invitation(self.partner, target.code)
        self.assertEqual(
            CoupleMembership.objects.get(user=self.partner).couple_id,
            own.invitation.couple_id,
        )
        self.assertTrue(Invitation.objects.filter(pk=own.invitation.pk).exists())
        target.invitation.refresh_from_db()
        self.assertIsNone(target.invitation.used_at)


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

    def test_pending_inviter_has_code_entry_before_and_after_refresh(self):
        for response in (
            self.client.post(reverse("couples:invite"), follow=True),
            self.client.get(reverse("garden:dashboard")),
        ):
            self.assertContains(response, 'id="invitation-code"')
            self.assertContains(response, 'name="code"')
            self.assertContains(response, "Join your partner")

    def test_pending_inviter_can_join_using_pasted_code(self):
        own = create_invitation(self.inviter)
        partner = User.objects.create_user(username="paste_target")
        target = create_invitation(partner)
        response = self.client.post(
            reverse("couples:accept"), {"code": target.code}, follow=True
        )
        self.assertContains(response, "You and your partner are connected!")
        self.assertNotContains(response, 'id="invitation-code"')
        self.assertFalse(Couple.objects.filter(pk=own.invitation.couple_id).exists())
        self.assertEqual(
            CoupleMembership.objects.get(user=self.inviter).couple_id,
            target.invitation.couple_id,
        )

    def test_connected_inviter_cannot_join_another_couple_using_code(self):
        own = create_invitation(self.inviter)
        partner = User.objects.create_user(username="connected_partner")
        accept_invitation(partner, own.code)
        other = User.objects.create_user(username="other_target")
        target = create_invitation(other)
        response = self.client.post(
            reverse("couples:accept"), {"code": target.code}, follow=True
        )
        self.assertContains(response, "already connected to another partner")
        self.assertNotContains(response, 'id="invitation-code"')
        self.assertEqual(
            CoupleMembership.objects.get(user=self.inviter).couple_id,
            own.invitation.couple_id,
        )

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
        self.assertEqual(response["Referrer-Policy"], "same-origin")
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
        self.assertRedirects(
            self.client.post(self.url, {"auth_action": "login"}),
            reverse("login"),
        )
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

    def test_self_invitation_and_connected_membership_are_rejected(self):
        self.client.force_login(self.inviter)
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "own invitation")
        other = User.objects.create_user(username="already_inviting")
        other_invitation = create_invitation(other)
        other_partner = User.objects.create_user(username="already_connected_partner")
        accept_invitation(other_partner, other_invitation.code)
        self.client.force_login(other)
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "already connected to another partner")
        self.created.invitation.refresh_from_db()
        self.assertIsNone(self.created.invitation.used_at)

    def test_pending_inviter_can_join_through_shared_link(self):
        partner = User.objects.create_user(username="pending_link_partner")
        own = create_invitation(partner)
        plant = FlowerPlant.objects.get(owner=partner)
        self.client.force_login(partner)
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "You and your partner are connected!")
        self.assertEqual(
            CoupleMembership.objects.get(user=partner).couple_id,
            self.created.invitation.couple_id,
        )
        self.assertFalse(Invitation.objects.filter(pk=own.invitation.pk).exists())
        self.assertEqual(FlowerPlant.objects.get(owner=partner).pk, plant.pk)

    def test_continue_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.get(self.url)
        self.assertEqual(csrf_client.post(self.url).status_code, 403)
        self.assertNotIn("pending_invitation_code", csrf_client.session)


@override_settings(
    PASSWORD_HASHERS=[FAST_PASSWORD_HASHER],
    DEBUG=False,
    ALLOWED_HOSTS=["bloomus.onrender.com"],
    CSRF_TRUSTED_ORIGINS=["https://bloomus.onrender.com"],
    SECURE_SSL_REDIRECT=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
    SECURE_REFERRER_POLICY="same-origin",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ProductionInvitationCsrfTests(TestCase):
    host = "bloomus.onrender.com"
    origin = "https://bloomus.onrender.com"

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST=self.host)
        self.inviter = User.objects.create_user(username="csrf_inviter")
        self.created = create_invitation(self.inviter)
        self.url = reverse("couples:invitation_link", args=(self.created.code,))

    def get_page(self, url):
        return self.client.get(url, secure=True)

    def post_form(self, url, data=None, *, origin=None):
        return self.client.post(
            url,
            {
                **(data or {}),
                "csrfmiddlewaretoken": self.client.cookies["csrftoken"].value,
            },
            secure=True,
            HTTP_ORIGIN=self.origin if origin is None else origin,
        )

    def test_https_invitation_signup_and_automatic_acceptance_with_csrf(self):
        preview = self.get_page(self.url)
        self.assertEqual(preview["Referrer-Policy"], "same-origin")
        self.assertContains(preview, 'name="csrfmiddlewaretoken"')
        self.assertEqual(preview["X-Frame-Options"], "DENY")
        self.assertRedirects(
            self.post_form(self.url), reverse("signup"), fetch_redirect_response=False
        )
        signup = self.get_page(reverse("signup"))
        self.assertContains(signup, 'name="csrfmiddlewaretoken"')
        self.assertEqual(signup["Referrer-Policy"], "same-origin")
        response = self.post_form(
            reverse("signup"),
            {
                "username": "csrf_new_partner",
                "email": "csrf-partner@example.com",
                "password1": "MossyMeadow!9274",
                "password2": "MossyMeadow!9274",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("garden:dashboard"))
        partner = User.objects.get(username="csrf_new_partner")
        self.assertEqual(
            partner.couple_membership.couple_id, self.created.invitation.couple_id
        )
        self.assertEqual(int(self.client.session["_auth_user_id"]), partner.pk)
        self.assertNotIn("pending_invitation_code", self.client.session)
        self.created.invitation.refresh_from_db()
        self.assertIsNotNone(self.created.invitation.used_at)
        self.assertTrue(self.client.cookies["csrftoken"]["secure"])
        self.assertTrue(self.client.cookies["sessionid"]["secure"])

    def test_https_invitation_login_and_automatic_acceptance_with_csrf(self):
        partner = User.objects.create_user(
            username="csrf_existing_partner", password="MossyMeadow!9274"
        )
        own = create_invitation(partner)
        plant = FlowerPlant.objects.get(owner=partner)
        self.get_page(self.url)
        response = self.post_form(self.url, {"auth_action": "login"})
        self.assertEqual(response.url, reverse("login"))
        login_page = self.get_page(reverse("login"))
        self.assertContains(login_page, 'name="csrfmiddlewaretoken"')
        self.assertEqual(login_page["Referrer-Policy"], "same-origin")
        response = self.post_form(
            reverse("login"),
            {"username": partner.username, "password": "MossyMeadow!9274"},
        )
        self.assertEqual(response.status_code, 302)
        partner.refresh_from_db()
        self.assertEqual(partner.couple_membership.slot, 2)
        self.assertNotIn("pending_invitation_code", self.client.session)
        self.assertFalse(Invitation.objects.filter(pk=own.invitation.pk).exists())
        self.assertEqual(FlowerPlant.objects.get(owner=partner).pk, plant.pk)

    def test_signed_in_link_and_pasted_code_accept_with_csrf(self):
        for flow in ("link", "code"):
            with self.subTest(flow=flow):
                if flow == "code":
                    self.inviter = User.objects.create_user(username="csrf_code_inviter")
                    self.created = create_invitation(self.inviter)
                partner = User.objects.create_user(username=f"csrf_{flow}_partner")
                own = create_invitation(partner)
                self.client.force_login(partner)
                self.get_page(reverse("garden:dashboard"))
                if flow == "link":
                    self.get_page(self.url)
                    response = self.post_form(self.url)
                else:
                    response = self.post_form(
                        reverse("couples:accept"), {"code": self.created.code}
                    )
                self.assertEqual(response.status_code, 302)
                partner.refresh_from_db()
                self.assertEqual(
                    partner.couple_membership.couple_id,
                    self.created.invitation.couple_id,
                )
                self.assertFalse(Invitation.objects.filter(pk=own.invitation.pk).exists())
                self.client.logout()

    def test_null_and_foreign_origins_rejected_even_with_valid_tokens(self):
        self.get_page(self.url)
        for origin in ("null", "https://untrusted.example"):
            with self.subTest(origin=origin):
                response = self.post_form(self.url, origin=origin)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("pending_invitation_code", self.client.session)
        self.assertEqual(CoupleMembership.objects.count(), 1)
        self.created.invitation.refresh_from_db()
        self.assertIsNone(self.created.invitation.used_at)

    def test_signup_and_login_reject_null_origin_before_linking(self):
        self.get_page(self.url)
        self.post_form(self.url)
        for route in ("signup", "login"):
            self.get_page(reverse(route))
            self.assertEqual(
                self.post_form(reverse(route), origin="null").status_code, 403
            )
        self.assertEqual(CoupleMembership.objects.count(), 1)

    def test_valid_origin_still_requires_csrf_token(self):
        self.get_page(self.url)
        response = self.client.post(
            self.url, secure=True, HTTP_ORIGIN=self.origin
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("pending_invitation_code", self.client.session)

    def test_https_referer_fallback_when_browser_omits_origin(self):
        self.get_page(self.url)
        response = self.client.post(
            self.url,
            {"csrfmiddlewaretoken": self.client.cookies["csrftoken"].value},
            secure=True,
            HTTP_REFERER=self.origin + self.url,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("pending_invitation_code", self.client.session)

    def test_render_proxy_generates_https_invitation_link(self):
        self.client.force_login(self.inviter)
        self.get_page(reverse("garden:dashboard"))
        self.post_form(reverse("couples:invite"))
        response = self.client.get(
            reverse("garden:dashboard"), HTTP_X_FORWARDED_PROTO="https"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.context["new_invitation_url"].startswith(
                self.origin + "/couples/join/"
            )
        )
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_http_redirects_to_https_before_rendering_forms(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.url, self.origin + self.url)


class RenderEnvironmentTests(SimpleTestCase):
    def load_settings(self, environment):
        with (
            patch.dict(
                os.environ,
                {
                    # A public test fixture, never a deployment credential.
                    "SECRET_KEY": "test-only-not-a-deployment-secret-0123456789-abcdefghijklmnopqrstuvwxyz",
                    **environment,
                },
                clear=True,
            ),
            patch("dotenv.load_dotenv"),
        ):
            return runpy.run_path(str(settings.BASE_DIR / "config" / "settings.py"))

    def test_render_forces_debug_off_even_with_stale_true_variable(self):
        for marker in (
            {"RENDER": "true"},
            {"RENDER_EXTERNAL_HOSTNAME": "bloomus.onrender.com"},
        ):
            with self.subTest(marker=marker):
                configured = self.load_settings({"DEBUG": "True", **marker})
                self.assertFalse(configured["DEBUG"])
                self.assertTrue(configured["SECURE_SSL_REDIRECT"])
                self.assertTrue(configured["CSRF_COOKIE_SECURE"])
                self.assertEqual(configured["SECURE_REFERRER_POLICY"], "same-origin")

    def test_render_hostname_and_custom_origins_are_preserved(self):
        configured = self.load_settings({
            "RENDER_EXTERNAL_HOSTNAME": "bloomus.onrender.com",
            "ALLOWED_HOSTS": "couples.example",
            "CSRF_TRUSTED_ORIGINS": "https://couples.example",
        })
        self.assertIn("bloomus.onrender.com", configured["ALLOWED_HOSTS"])
        self.assertEqual(
            configured["CSRF_TRUSTED_ORIGINS"],
            ["https://couples.example", "https://bloomus.onrender.com"],
        )
        self.assertNotIn("null", configured["CSRF_TRUSTED_ORIGINS"])

    def test_local_development_debug_default_is_unchanged(self):
        self.assertTrue(self.load_settings({})["DEBUG"])


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class ConcurrentInvitationAcceptanceTests(TransactionTestCase):
    def test_reciprocal_pending_invitations_connect_once(self):
        first = User.objects.create_user(username="reciprocal_first")
        second = User.objects.create_user(username="reciprocal_second")
        first_invitation = create_invitation(first)
        second_invitation = create_invitation(second)
        barrier = Barrier(2)

        def accept(pair):
            user_id, code = pair
            close_old_connections()
            try:
                user = User.objects.get(pk=user_id)
                barrier.wait(timeout=10)
                try:
                    accept_invitation(user, code)
                    return True
                except CoupleRuleError:
                    return False
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                accept,
                ((first.pk, second_invitation.code), (second.pk, first_invitation.code)),
            ))
        self.assertEqual(sum(results), 1)
        self.assertEqual(Couple.objects.count(), 1)
        self.assertEqual(CoupleMembership.objects.count(), 2)
        self.assertEqual(FlowerPlant.objects.count(), 2)
        self.assertEqual(Invitation.objects.count(), 1)
        self.assertEqual(Invitation.objects.filter(used_at__isnull=False).count(), 1)

    def test_joining_and_someone_accepting_own_pending_code_cannot_double_connect(self):
        owner = User.objects.create_user(username="race_pending_owner")
        target_owner = User.objects.create_user(username="race_target_owner")
        contender = User.objects.create_user(username="race_contender")
        own = create_invitation(owner)
        target = create_invitation(target_owner)
        barrier = Barrier(2)

        def accept(pair):
            user_id, code = pair
            close_old_connections()
            try:
                user = User.objects.get(pk=user_id)
                barrier.wait(timeout=10)
                try:
                    accept_invitation(user, code)
                    return True
                except CoupleRuleError:
                    return False
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                accept, ((owner.pk, target.code), (contender.pk, own.code)),
            ))
        self.assertEqual(sum(results), 1)
        membership = CoupleMembership.objects.get(user=owner)
        self.assertEqual(membership.couple.memberships.count(), 2)
        self.assertEqual(FlowerPlant.objects.filter(owner=owner).count(), 1)
        self.assertEqual(
            CoupleMembership.objects.filter(user=contender).exists(),
            membership.couple_id == own.invitation.couple_id,
        )

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
        own = create_invitation(joining_user)
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
        self.assertFalse(Couple.objects.filter(pk=own.invitation.couple_id).exists())
        self.assertEqual(CoupleMembership.objects.filter(user=joining_user).count(), 1)
        self.assertEqual(
            CoupleMembership.objects.filter(user=joining_user)
            .get()
            .couple.memberships.count(),
            2,
        )

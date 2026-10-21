from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


User = get_user_model()


class LandingPageTests(TestCase):
    def test_homepage_shows_landing_copy_and_entry_points(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Love grows in")
        self.assertContains(response, reverse("login"))
        self.assertContains(response, reverse("signup"))
        self.assertTemplateUsed(response, "home.html")


class CustomUserTests(TestCase):
    def test_custom_user_manager_hashes_password(self):
        user = User.objects.create_user(
            username="garden_guest",
            email="garden@example.com",
            password="a-long-test-password",
        )

        self.assertEqual(user.__class__, User)
        self.assertTrue(user.check_password("a-long-test-password"))
        self.assertNotEqual(user.password, "a-long-test-password")


class AuthenticationRouteTests(TestCase):
    def test_signup_uses_safe_user_creation_form(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "new_bloomer",
                "email": "new@example.com",
                "password1": "MossyMeadow!9274",
                "password2": "MossyMeadow!9274",
            },
        )

        self.assertRedirects(response, reverse("login"))
        user = User.objects.get(username="new_bloomer")
        self.assertTrue(user.check_password("MossyMeadow!9274"))

    def test_login_and_post_logout_use_django_auth_views(self):
        user = User.objects.create_user(
            username="garden_guest",
            password="a-long-test-password",
        )

        login_response = self.client.post(
            reverse("login"),
            {"username": user.username, "password": "a-long-test-password"},
        )

        self.assertRedirects(login_response, reverse("garden:dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

        logout_response = self.client.post(reverse("logout"))

        self.assertRedirects(logout_response, reverse("home"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_authentication_pages_are_routed(self):
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)
        self.assertEqual(self.client.get(reverse("signup")).status_code, 200)

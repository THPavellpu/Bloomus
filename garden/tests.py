from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from couples.services import accept_invitation, create_invitation

from .models import FlowerPlant
from .services import get_plant_care


User = get_user_model()
FAST_PASSWORD_HASHER = "django.contrib.auth.hashers.MD5PasswordHasher"


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class FlowerPlantTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="flower_owner",
            password="a-long-test-password",
        )
        self.partner = User.objects.create_user(
            username="flower_partner",
            password="a-long-test-password",
        )

    def test_invitation_onboarding_creates_exactly_one_plant_for_each_member(self):
        invitation = create_invitation(self.owner)
        self.assertEqual(FlowerPlant.objects.filter(owner=self.owner).count(), 1)
        accept_invitation(self.partner, invitation.code)

        self.assertEqual(FlowerPlant.objects.filter(owner=self.owner).count(), 1)
        self.assertEqual(FlowerPlant.objects.filter(owner=self.partner).count(), 1)
        self.assertIsNotNone(self.owner.flower_plant.created_at)
        self.assertIsNone(self.owner.flower_plant.last_watered_at)

    def test_database_enforces_one_plant_per_owner(self):
        FlowerPlant.objects.create(owner=self.owner)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FlowerPlant.objects.create(owner=self.owner)

    def test_dashboard_only_displays_current_couple_plants(self):
        invitation = create_invitation(self.owner)
        accept_invitation(self.partner, invitation.code)
        outsider = User.objects.create_user(
            username="private_outsider",
            password="a-long-test-password",
        )
        outsider_invitation = create_invitation(outsider)

        self.client.force_login(self.owner)
        response = self.client.get(reverse("garden:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your flower")
        self.assertContains(response, "flower_partner")
        self.assertNotContains(response, "private_outsider")
        self.assertEqual(
            {plant["owner"].pk for plant in response.context["plants"]},
            {self.owner.pk, self.partner.pk},
        )
        self.assertIsNotNone(outsider_invitation)

    def test_unconnected_user_gets_empty_state_and_cannot_see_private_plants(self):
        invitation = create_invitation(self.owner)
        outsider = User.objects.create_user(
            username="unconnected_viewer",
            password="a-long-test-password",
        )
        self.client.force_login(outsider)

        response = self.client.get(reverse("garden:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your garden is waiting")
        self.assertNotContains(response, self.owner.username)
        self.assertEqual(response.context["plants"], [])
        self.assertIsNotNone(invitation)

    def test_dashboard_is_login_protected(self):
        response = self.client.get(reverse("garden:dashboard"))

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('garden:dashboard')}",
        )

    def test_browser_timestamp_parameter_cannot_change_plant_care_state(self):
        create_invitation(self.owner)
        plant = self.owner.flower_plant
        original_timestamp = plant.last_watered_at
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("garden:dashboard"),
            {"now": "2099-01-01T00:00:00Z", "last_watered_at": "2099-01-01T00:00:00Z"},
        )
        plant.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(plant.last_watered_at, original_timestamp)


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class PlantCareTests(TestCase):
    def test_growth_and_care_derive_from_aware_server_timestamps(self):
        owner = User.objects.create_user(
            username="care_owner",
            password="a-long-test-password",
        )
        now = timezone.now()
        plant = FlowerPlant.objects.create(owner=owner)
        FlowerPlant.objects.filter(pk=plant.pk).update(
            created_at=now - timedelta(days=10),
            last_watered_at=now - timedelta(hours=12),
        )
        plant.refresh_from_db()

        care = get_plant_care(plant, has_partner=True, now=now)

        self.assertEqual(care.stage, "Bud")
        self.assertEqual(care.status, "Feeling loved")

    def test_unconnected_owner_does_not_care_for_their_own_plant(self):
        owner = User.objects.create_user(
            username="waiting_owner",
            password="a-long-test-password",
        )
        plant = FlowerPlant.objects.create(owner=owner)

        care = get_plant_care(plant, has_partner=False)

        self.assertEqual(care.status, "Waiting for your person")

    def test_plant_health_never_uses_a_permanent_death_state(self):
        owner = User.objects.create_user(
            username="old_plant_owner",
            password="a-long-test-password",
        )
        now = timezone.now()
        plant = FlowerPlant.objects.create(owner=owner)
        FlowerPlant.objects.filter(pk=plant.pk).update(
            created_at=now - timedelta(days=3650),
            last_watered_at=now - timedelta(days=3650),
        )
        plant.refresh_from_db()

        care = get_plant_care(plant, has_partner=True, now=now)

        self.assertEqual(care.stage, "Bloom")
        self.assertEqual(care.status, "Ready for a little care")

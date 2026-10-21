from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from couples.services import accept_invitation, create_invitation
from messages.services import send_message_and_water

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
    def test_age_alone_does_not_grow_an_unwatered_plant(self):
        owner = User.objects.create_user(
            username="care_owner",
            password="a-long-test-password",
        )
        now = timezone.now()
        plant = FlowerPlant.objects.create(owner=owner)
        FlowerPlant.objects.filter(pk=plant.pk).update(
            created_at=now - timedelta(days=10),
        )
        plant.refresh_from_db()

        care = get_plant_care(plant, has_partner=True, now=now)

        self.assertEqual(care.stage, "Baby plant")
        self.assertEqual(care.status, "Ready for a little care")
        self.assertEqual(care.animation, "wilting")

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

        self.assertEqual(care.stage, "Baby plant")
        self.assertEqual(care.status, "Ready for a little care")


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class NoteDrivenGardenTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="baby_owner")
        self.partner = User.objects.create_user(username="caring_partner")
        invitation = create_invitation(self.owner)
        accept_invitation(self.partner, invitation.code)
        self.plant = FlowerPlant.objects.get(owner=self.owner)
        self.client.force_login(self.owner)

    def send_note(self):
        note = send_message_and_water(
            self.partner,
            body="A little love for your flower.",
            idempotency_key=uuid4(),
        )
        self.plant.refresh_from_db()
        return note

    def care_at(self, now, *, unread=True, has_partner=True):
        return get_plant_care(
            self.plant,
            has_partner=has_partner,
            has_unread_note=unread,
            now=now,
        )

    def test_growth_thresholds_follow_persisted_notes_not_age(self):
        self.assertEqual(self.care_at(timezone.now()).stage_key, "baby")
        expected_stages = ("sprout", "sprout", "bud", "bud", "bud", "bud", "bloom")
        for stage in expected_stages:
            with self.subTest(stage=stage):
                note = self.send_note()
                self.assertEqual(self.care_at(note.sent_at).stage_key, stage)

        neglected_care = self.care_at(note.sent_at + timedelta(days=365))
        self.assertEqual(neglected_care.stage_key, "bloom")
        self.assertEqual(neglected_care.animation, "wilting")

    def test_three_hour_boundary_and_unread_animation_rules(self):
        note = self.send_note()
        cases = (
            (timedelta(0), True, "growing"),
            (timedelta(hours=2, minutes=59), True, "growing"),
            (timedelta(hours=3), True, "growing"),
            (timedelta(hours=3, microseconds=1), True, "wilting"),
            (timedelta(hours=1), False, "idle"),
            (timedelta(hours=4), False, "wilting"),
        )
        for offset, unread, expected in cases:
            with self.subTest(offset=offset, unread=unread):
                care = self.care_at(note.sent_at + offset, unread=unread)
                self.assertEqual(care.animation, expected)
                self.assertEqual(care.needs_water, expected == "wilting")

    def test_initial_grace_period_and_waiting_partner_are_calm(self):
        self.assertEqual(self.care_at(self.plant.created_at).animation, "idle")
        self.assertEqual(
            self.care_at(self.plant.created_at + timedelta(hours=4)).animation,
            "wilting",
        )
        self.assertEqual(
            self.care_at(
                self.plant.created_at + timedelta(days=30), has_partner=False
            ).animation,
            "idle",
        )

    def test_unread_dot_and_growth_repeat_until_recipient_reads_note(self):
        note = self.send_note()
        for _ in range(2):
            response = self.client.get(reverse("garden:dashboard"))
            self.assertContains(response, "notes-notification-dot")
            self.assertContains(response, "1 unread partner note")
            self.assertEqual(response.context["unread_note_count"], 1)
            mine = next(item for item in response.context["plants"] if item["is_mine"])
            self.assertEqual(mine["care"].animation, "growing")
            self.assertEqual(mine["care"].stage_key, "sprout")

        history = self.client.get(reverse("romantic_messages:history"))
        self.assertContains(history, "notes-notification-dot")
        self.client.post(reverse("romantic_messages:mark_read", args=(note.pk,)))
        response = self.client.get(reverse("garden:dashboard"))
        self.assertNotContains(response, "notes-notification-dot")
        mine = next(item for item in response.context["plants"] if item["is_mine"])
        self.assertEqual(mine["care"].animation, "idle")
        self.assertEqual(mine["care"].stage_key, "sprout")

    def test_old_unread_note_keeps_dot_but_shows_negative_animation(self):
        note = self.send_note()
        with patch(
            "garden.views.timezone.now",
            return_value=note.sent_at + timedelta(hours=3, seconds=1),
        ):
            response = self.client.get(reverse("garden:dashboard"))

        self.assertContains(response, "notes-notification-dot")
        mine = next(item for item in response.context["plants"] if item["is_mine"])
        self.assertEqual(mine["care"].animation, "wilting")
        self.assertContains(response, "plant-animation-wilting")
        self.assertNotContains(response, "plant-animation-growing")

    def test_new_note_revives_drooping_plant_without_losing_growth(self):
        first_note = self.send_note()
        self.assertEqual(
            self.care_at(first_note.sent_at + timedelta(hours=4)).animation,
            "wilting",
        )
        with patch(
            "messages.services.timezone.now",
            return_value=first_note.sent_at + timedelta(hours=4),
        ):
            second_note = self.send_note()
        care = self.care_at(second_note.sent_at)
        self.assertEqual(care.animation, "growing")
        self.assertEqual(care.stage_key, "sprout")

    def test_refreshes_do_not_change_water_or_read_timestamps_or_growth(self):
        note = self.send_note()
        for _ in range(2):
            self.client.get(
                reverse("garden:dashboard"),
                {"now": "2099-01-01T00:00:00Z", "watering_count": 100},
            )
        self.plant.refresh_from_db()
        note.refresh_from_db()
        self.assertEqual(self.plant.last_watered_at, note.sent_at)
        self.assertIsNone(note.read_at)
        self.assertEqual(self.plant.watering_messages.count(), 1)
        self.assertEqual(self.care_at(note.sent_at).stage_key, "sprout")

    def test_sender_and_other_couples_get_no_recipient_notification(self):
        self.send_note()
        self.client.force_login(self.partner)
        response = self.client.get(reverse("garden:dashboard"))
        self.assertNotContains(response, "notes-notification-dot")
        self.assertNotContains(response, "plant-animation-growing")

        outsider = User.objects.create_user(username="outsider")
        create_invitation(outsider)
        self.client.force_login(outsider)
        response = self.client.get(reverse("garden:dashboard"))
        self.assertEqual(response.context["unread_note_count"], 0)
        self.assertNotContains(response, "notes-notification-dot")

    def test_notification_is_present_on_other_pages_and_for_multiple_notes(self):
        self.send_note()
        second = self.send_note()
        response = self.client.get(reverse("home"))
        self.assertContains(response, "notes-notification-dot")
        self.assertContains(response, "2 unread partner notes")
        self.client.post(reverse("romantic_messages:mark_read", args=(second.pk,)))
        response = self.client.get(reverse("home"))
        self.assertEqual(response.context["unread_note_count"], 1)
        self.assertContains(response, "notes-notification-dot")

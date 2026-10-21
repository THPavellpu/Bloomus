from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import (
    IntegrityError,
    close_old_connections,
    connections,
    transaction,
)
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from couples.models import CoupleMembership
from couples.services import accept_invitation, create_invitation
from garden.models import FlowerPlant
from garden.services import get_plant_care

from .models import RomanticMessage
from .services import (
    DuplicateSubmission,
    MessageRateLimitExceeded,
    MessageRuleError,
    send_message_and_water,
)


User = get_user_model()
FAST_PASSWORD_HASHER = "django.contrib.auth.hashers.MD5PasswordHasher"


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class RomanticMessageTests(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user(
            username="note_sender",
            password="a-long-test-password",
        )
        self.recipient = User.objects.create_user(
            username="note_recipient",
            password="a-long-test-password",
        )
        invitation = create_invitation(self.sender)
        accept_invitation(self.recipient, invitation.code)
        self.plant = FlowerPlant.objects.get(owner=self.recipient)
        self.write_url = reverse("romantic_messages:write")
        self.history_url = reverse("romantic_messages:history")

    def post_note(
        self,
        *,
        body="You make every day feel like spring.",
        key=None,
        follow=False,
        **extra,
    ):
        return self.client.post(
            self.write_url,
            {
                "body": body,
                "idempotency_key": str(key or uuid4()),
                **extra,
            },
            follow=follow,
        )

    def test_success_creates_private_message_and_waters_recipient_atomically(self):
        self.client.force_login(self.sender)

        response = self.post_note(follow=True)

        self.assertEqual(
            response.redirect_chain,
            [(reverse("garden:dashboard"), 302)],
        )
        self.assertContains(response, "plant-art-watered")
        watered_plants = [
            item for item in response.context["plants"] if item["just_watered"]
        ]
        self.assertEqual(len(watered_plants), 1)
        self.assertEqual(watered_plants[0]["owner"], self.recipient)
        note = RomanticMessage.objects.get()
        self.plant.refresh_from_db()
        self.assertEqual(note.sender, self.sender)
        self.assertEqual(note.recipient, self.recipient)
        self.assertEqual(note.watered_plant, self.plant)
        self.assertEqual(note.body, "You make every day feel like spring.")
        self.assertIsNotNone(note.sent_at)
        self.assertTrue(timezone.is_aware(note.sent_at))
        self.assertEqual(note.sent_at, self.plant.last_watered_at)
        self.assertIsNone(note.read_at)

    def test_message_updates_care_and_grows_plant_from_first_note(self):
        old_created_at = timezone.now() - timedelta(days=10)
        FlowerPlant.objects.filter(pk=self.plant.pk).update(
            created_at=old_created_at,
        )
        self.client.force_login(self.sender)

        self.post_note()

        note = RomanticMessage.objects.get()
        self.plant.refresh_from_db()
        care = get_plant_care(
            self.plant,
            has_partner=True,
            now=note.sent_at,
        )
        self.assertEqual(care.stage, "Sprout")
        self.assertEqual(care.status, "Feeling loved")
        self.assertEqual(self.plant.last_watered_at, note.sent_at)

    def test_empty_oversized_and_invalid_key_messages_are_rejected(self):
        self.client.force_login(self.sender)

        empty_response = self.post_note(body="   ")
        oversized_response = self.post_note(body="x" * 501)
        invalid_key_response = self.client.post(
            self.write_url,
            {"body": "A note", "idempotency_key": "not-a-uuid"},
        )

        self.assertEqual(empty_response.status_code, 200)
        self.assertContains(empty_response, "Write a little note")
        self.assertEqual(oversized_response.status_code, 200)
        self.assertEqual(invalid_key_response.status_code, 200)
        self.assertEqual(RomanticMessage.objects.count(), 0)
        self.plant.refresh_from_db()
        self.assertIsNone(self.plant.last_watered_at)

    def test_recipient_cannot_be_manipulated_to_water_sender_or_outsider(self):
        outsider = User.objects.create_user(
            username="message_outsider",
            password="a-long-test-password",
        )
        outsider_plant = FlowerPlant.objects.create(owner=outsider)
        self.client.force_login(self.sender)

        response = self.post_note(
            recipient_id=self.sender.pk,
            plant_id=outsider_plant.pk,
        )

        self.assertRedirects(response, reverse("garden:dashboard"))
        note = RomanticMessage.objects.get()
        self.assertEqual(note.recipient, self.recipient)
        self.assertEqual(note.watered_plant, self.plant)
        self.plant.refresh_from_db()
        outsider_plant.refresh_from_db()
        self.assertIsNotNone(self.plant.last_watered_at)
        self.assertIsNone(outsider_plant.last_watered_at)

    def test_sender_cannot_create_a_message_watering_their_own_plant(self):
        sender_plant = FlowerPlant.objects.get(owner=self.sender)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RomanticMessage.objects.create(
                    couple=self.sender.couple_membership.couple,
                    sender=self.sender,
                    recipient=self.sender,
                    watered_plant=sender_plant,
                    body="A message to myself",
                    idempotency_key=uuid4(),
                    sent_at=timezone.now(),
                )

    def test_duplicate_submission_does_not_create_or_water_twice(self):
        self.client.force_login(self.sender)
        key = uuid4()

        first_response = self.post_note(key=key)
        self.plant.refresh_from_db()
        original_watered_at = self.plant.last_watered_at
        second_response = self.post_note(key=key, body="Changed duplicate text")

        self.assertRedirects(first_response, reverse("garden:dashboard"))
        self.assertRedirects(second_response, reverse("garden:dashboard"))
        self.assertEqual(RomanticMessage.objects.count(), 1)
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.last_watered_at, original_watered_at)
        self.assertEqual(
            RomanticMessage.objects.get().body,
            "You make every day feel like spring.",
        )

    def test_failed_water_update_rolls_back_message_and_plant_timestamp(self):
        self.client.force_login(self.sender)

        with patch.object(FlowerPlant, "save", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.post_note()

        self.assertEqual(RomanticMessage.objects.count(), 0)
        self.plant.refresh_from_db()
        self.assertIsNone(self.plant.last_watered_at)

    def test_service_rejects_nonmember_and_blank_message(self):
        outsider = User.objects.create_user(
            username="unconnected_sender",
            password="a-long-test-password",
        )
        with self.assertRaises(MessageRuleError):
            send_message_and_water(
                outsider,
                body="Hello",
                idempotency_key=uuid4(),
            )
        with self.assertRaises(MessageRuleError):
            send_message_and_water(
                self.sender,
                body="   ",
                idempotency_key=uuid4(),
            )

    def test_sender_rate_limit_is_enforced(self):
        for _ in range(10):
            send_message_and_water(
                self.sender,
                body="A little love note.",
                idempotency_key=uuid4(),
            )

        with self.assertRaises(MessageRateLimitExceeded):
            send_message_and_water(
                self.sender,
                body="One more note.",
                idempotency_key=uuid4(),
            )
        self.assertEqual(RomanticMessage.objects.count(), 10)

    def test_duplicate_service_submission_is_rejected(self):
        key = uuid4()
        send_message_and_water(
            self.sender,
            body="One note.",
            idempotency_key=key,
        )

        with self.assertRaises(DuplicateSubmission):
            send_message_and_water(
                self.sender,
                body="One note again.",
                idempotency_key=key,
            )


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class MessagePrivacyAndReadTests(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user(
            username="private_sender",
            password="a-long-test-password",
        )
        self.recipient = User.objects.create_user(
            username="private_recipient",
            password="a-long-test-password",
        )
        invitation = create_invitation(self.sender)
        accept_invitation(self.recipient, invitation.code)

        self.outsider = User.objects.create_user(
            username="other_sender",
            password="a-long-test-password",
        )
        self.outsider_partner = User.objects.create_user(
            username="other_recipient",
            password="a-long-test-password",
        )
        outsider_invitation = create_invitation(self.outsider)
        accept_invitation(self.outsider_partner, outsider_invitation.code)

        self.note = send_message_and_water(
            self.sender,
            body="I am glad we found each other.",
            idempotency_key=uuid4(),
        )
        send_message_and_water(
            self.outsider,
            body="This belongs to another couple.",
            idempotency_key=uuid4(),
        )

    def test_history_shows_only_received_notes_without_marking_them_read(self):
        self.client.force_login(self.recipient)

        response = self.client.get(reverse("romantic_messages:history"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "private_sender")
        self.assertContains(response, "I am glad we found each other.")
        self.assertNotContains(response, "This belongs to another couple.")
        self.assertContains(response, "Unread")
        self.note.refresh_from_db()
        self.assertIsNone(self.note.read_at)

    def test_recipient_can_mark_note_read_but_sender_cannot(self):
        self.client.force_login(self.sender)
        denied_response = self.client.post(
            reverse("romantic_messages:mark_read", args=(self.note.pk,))
        )
        self.assertEqual(denied_response.status_code, 404)
        self.note.refresh_from_db()
        self.assertIsNone(self.note.read_at)

        self.client.force_login(self.recipient)
        response = self.client.post(
            reverse("romantic_messages:mark_read", args=(self.note.pk,))
        )

        self.assertRedirects(response, reverse("romantic_messages:history"))
        self.note.refresh_from_db()
        self.assertIsNotNone(self.note.read_at)
        self.assertTrue(timezone.is_aware(self.note.read_at))

    def test_read_mutation_requires_post(self):
        self.client.force_login(self.recipient)

        response = self.client.get(
            reverse("romantic_messages:mark_read", args=(self.note.pk,))
        )

        self.assertEqual(response.status_code, 405)
        self.note.refresh_from_db()
        self.assertIsNone(self.note.read_at)

    def test_other_couple_cannot_open_or_mark_recipient_message(self):
        self.client.force_login(self.outsider)

        response = self.client.post(
            reverse("romantic_messages:mark_read", args=(self.note.pk,))
        )

        self.assertEqual(response.status_code, 404)
        self.note.refresh_from_db()
        self.assertIsNone(self.note.read_at)

    def test_removed_member_cannot_retrieve_or_mark_their_old_couple_note(self):
        CoupleMembership.objects.filter(user=self.recipient).delete()
        self.client.force_login(self.recipient)

        history_response = self.client.get(reverse("romantic_messages:history"))
        read_response = self.client.post(
            reverse("romantic_messages:mark_read", args=(self.note.pk,))
        )

        self.assertNotContains(
            history_response,
            "I am glad we found each other.",
        )
        self.assertEqual(read_response.status_code, 404)
        self.note.refresh_from_db()
        self.assertIsNone(self.note.read_at)

    def test_unauthenticated_users_cannot_open_message_pages_or_send(self):
        self.assertRedirects(
            self.client.get(reverse("romantic_messages:history")),
            f"{reverse('login')}?next={reverse('romantic_messages:history')}",
        )
        self.assertRedirects(
            self.client.get(reverse("romantic_messages:write")),
            f"{reverse('login')}?next={reverse('romantic_messages:write')}",
        )
        self.assertRedirects(
            self.client.post(
                reverse("romantic_messages:write"),
                {"body": "Hello", "idempotency_key": str(uuid4())},
            ),
            f"{reverse('login')}?next={reverse('romantic_messages:write')}",
        )

    def test_nonmember_cannot_open_write_form(self):
        unconnected = User.objects.create_user(
            username="no_couple_user",
            password="a-long-test-password",
        )
        self.client.force_login(unconnected)

        response = self.client.get(reverse("romantic_messages:write"))

        self.assertEqual(response.status_code, 404)


@override_settings(PASSWORD_HASHERS=[FAST_PASSWORD_HASHER])
class ConcurrentMessageSubmissionTests(TransactionTestCase):
    def test_concurrent_duplicate_submission_only_waters_once(self):
        sender = User.objects.create_user(
            username="concurrent_sender",
            password="a-long-test-password",
        )
        recipient = User.objects.create_user(
            username="concurrent_recipient",
            password="a-long-test-password",
        )
        invitation = create_invitation(sender)
        accept_invitation(recipient, invitation.code)
        plant_id = FlowerPlant.objects.get(owner=recipient).pk
        submission_key = uuid4()
        barrier = Barrier(2)

        def submit_note():
            close_old_connections()
            try:
                sender_in_thread = User.objects.get(pk=sender.pk)
                barrier.wait(timeout=10)
                try:
                    send_message_and_water(
                        sender_in_thread,
                        body="A note from a racing request.",
                        idempotency_key=submission_key,
                    )
                    return True
                except DuplicateSubmission:
                    return False
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit_note(), range(2)))

        self.assertEqual(sum(results), 1)
        self.assertEqual(
            RomanticMessage.objects.filter(
                sender=sender,
                idempotency_key=submission_key,
            ).count(),
            1,
        )
        note = RomanticMessage.objects.get(idempotency_key=submission_key)
        plant = FlowerPlant.objects.get(pk=plant_id)
        self.assertEqual(plant.last_watered_at, note.sent_at)

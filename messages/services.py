import time
from datetime import timedelta

from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from couples.models import Couple, CoupleMembership
from garden.models import FlowerPlant

from .models import RomanticMessage


MAX_MESSAGES_PER_HOUR = 10
MESSAGE_RATE_WINDOW = timedelta(hours=1)


class MessageRuleError(Exception):
    """A message or watering request violated a domain rule."""


class DuplicateSubmission(MessageRuleError):
    pass


class MessageRateLimitExceeded(MessageRuleError):
    pass


def _send_message_once(sender, body, idempotency_key):
    now = timezone.now()
    try:
        with transaction.atomic():
            sender_membership = (
                CoupleMembership.objects.select_related("couple")
                .filter(user=sender)
                .first()
            )
            if sender_membership is None:
                raise MessageRuleError("Connect with your partner before sending a note.")

            # A shared parent-row lock serializes message submissions per couple on
            # databases that support row locks; SQLite retries its write-lock case.
            locked_couple = Couple.objects.select_for_update().get(
                pk=sender_membership.couple_id
            )

            if RomanticMessage.objects.filter(
                sender=sender,
                idempotency_key=idempotency_key,
            ).exists():
                raise DuplicateSubmission("This note has already been sent.")

            recent_count = RomanticMessage.objects.filter(
                sender=sender,
                sent_at__gte=now - MESSAGE_RATE_WINDOW,
            ).count()
            if recent_count >= MAX_MESSAGES_PER_HOUR:
                raise MessageRateLimitExceeded(
                    "You've sent plenty of love for now. Try again a little later."
                )

            # Recheck both memberships while holding the couple lock. Submitted
            # data never selects the recipient or the plant.
            memberships = list(
                CoupleMembership.objects.filter(couple=locked_couple)
                .select_related("user")
                .order_by("slot")
            )
            member_by_user_id = {member.user_id: member for member in memberships}
            sender_membership = member_by_user_id.get(sender.pk)
            if sender_membership is None:
                raise MessageRuleError("You are no longer a member of this couple.")

            recipient_membership = next(
                (
                    member
                    for member in memberships
                    if member.user_id != sender.pk
                ),
                None,
            )
            if recipient_membership is None:
                raise MessageRuleError("Your partner is not connected yet.")

            plant = (
                FlowerPlant.objects.select_for_update()
                .filter(owner_id=recipient_membership.user_id)
                .first()
            )
            if plant is None:
                raise MessageRuleError("Your partner's flower is not ready yet.")

            message = RomanticMessage.objects.create(
                couple=locked_couple,
                sender=sender,
                recipient=recipient_membership.user,
                watered_plant=plant,
                body=body,
                idempotency_key=idempotency_key,
                sent_at=now,
            )
            plant.last_watered_at = now
            plant.save(update_fields=("last_watered_at",))
    except IntegrityError as exc:
        if RomanticMessage.objects.filter(
            sender=sender,
            idempotency_key=idempotency_key,
        ).exists():
            raise DuplicateSubmission("This note has already been sent.") from exc
        raise

    return message


def send_message_and_water(sender, *, body, idempotency_key):
    normalized_body = body.strip()
    if not normalized_body or len(normalized_body) > 500:
        raise MessageRuleError("Messages must contain between 1 and 500 characters.")

    for attempt in range(8):
        try:
            return _send_message_once(sender, normalized_body, idempotency_key)
        except OperationalError as exc:
            is_sqlite_lock = connection.vendor == "sqlite" and "locked" in str(exc).lower()
            if not is_sqlite_lock or attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))

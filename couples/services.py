import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from garden.models import FlowerPlant

from .models import Couple, CoupleMembership, Invitation


INVITATION_LIFETIME = timedelta(days=7)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class CoupleRuleError(Exception):
    """A user-facing invitation or membership rule was not satisfied."""


@dataclass(frozen=True)
class CreatedInvitation:
    invitation: Invitation
    code: str


def _hash_code(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def normalize_invitation_code(code):
    compact = re.sub(r"[\s-]", "", code).upper()
    if re.fullmatch(r"BU[A-HJ-NP-Z2-9]{16}", compact):
        return "BU-" + "-".join(compact[index:index + 4] for index in range(2, 18, 4))
    return code.strip()


def available_invitation(code):
    normalized_code = normalize_invitation_code(code)
    if not normalized_code or len(normalized_code) > 128:
        raise CoupleRuleError("That invitation is not valid.")
    invitation = Invitation.objects.select_related("couple").filter(
        token_hash=_hash_code(normalized_code),
    ).first()
    if invitation is None:
        raise CoupleRuleError("That invitation is not valid.")
    if invitation.used_at is not None:
        raise CoupleRuleError("That invitation has already been used.")
    if invitation.expires_at <= timezone.now():
        raise CoupleRuleError("That invitation has expired.")
    if list(invitation.couple.memberships.values_list("slot", flat=True)) != [1]:
        raise CoupleRuleError("That invitation is no longer available.")
    return invitation


def create_invitation(user):
    code = "BU-" + "-".join(
        "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        for _ in range(4)
    )

    for attempt in range(8):
        try:
            return _create_invitation_once(user, code)
        except OperationalError as exc:
            is_sqlite_lock = (
                connection.vendor == "sqlite"
                and "locked" in str(exc).lower()
            )
            if not is_sqlite_lock or attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def _create_invitation_once(user, code):
    now = timezone.now()
    try:
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=user.pk)
            membership = (
                CoupleMembership.objects.select_for_update()
                .filter(user=user)
                .select_related("couple")
                .first()
            )

            if membership is None:
                couple = Couple.objects.create()
                CoupleMembership.objects.create(couple=couple, user=user, slot=1)
                FlowerPlant.objects.create(owner=user)
            else:
                couple = membership.couple
                if membership.slot != 1 or couple.memberships.count() != 1:
                    raise CoupleRuleError(
                        "You are already connected to a partner."
                    )

            invitation = (
                Invitation.objects.select_for_update()
                .filter(couple=couple)
                .first()
            )
            if invitation is None:
                invitation = Invitation.objects.create(
                    couple=couple,
                    token_hash=_hash_code(code),
                    expires_at=now + INVITATION_LIFETIME,
                )
            else:
                if invitation.used_at is not None:
                    raise CoupleRuleError(
                        "This invitation has already been used."
                    )
                invitation.token_hash = _hash_code(code)
                invitation.expires_at = now + INVITATION_LIFETIME
                invitation.save(update_fields=("token_hash", "expires_at"))
    except IntegrityError as exc:
        raise CoupleRuleError(
            "You already belong to a couple. Refresh and try again."
        ) from exc

    return CreatedInvitation(invitation=invitation, code=code)


def accept_invitation(user, code):
    normalized_code = normalize_invitation_code(code)
    if not normalized_code or len(normalized_code) > 128:
        raise CoupleRuleError("That invitation is not valid.")

    for attempt in range(8):
        try:
            return _accept_invitation_once(user, normalized_code)
        except OperationalError as exc:
            is_sqlite_lock = (
                connection.vendor == "sqlite"
                and "locked" in str(exc).lower()
            )
            if not is_sqlite_lock or attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def _accept_invitation_once(user, normalized_code):
    try:
        with transaction.atomic():
            target = Invitation.objects.filter(
                token_hash=_hash_code(normalized_code)
            ).first()
            if target is None:
                raise CoupleRuleError("That invitation is not valid.")
            inviter_id = target.couple.memberships.filter(slot=1).values_list(
                "user_id", flat=True
            ).first()
            if inviter_id is None:
                raise CoupleRuleError("That invitation is no longer available.")

            # Lock both users in a stable order before any membership changes,
            # including replacing a waiting garden or reciprocal acceptances.
            list(
                get_user_model().objects.select_for_update()
                .filter(pk__in=(user.pk, inviter_id))
                .order_by("pk")
            )
            invitation = (
                Invitation.objects.select_for_update()
                .filter(token_hash=_hash_code(normalized_code))
                .first()
            )
            if invitation is None:
                raise CoupleRuleError("That invitation is not valid.")
            if invitation.used_at is not None:
                raise CoupleRuleError("That invitation has already been used.")
            if invitation.expires_at <= timezone.now():
                raise CoupleRuleError("That invitation has expired.")

            couple = Couple.objects.select_for_update().get(pk=invitation.couple_id)
            existing_membership = (
                CoupleMembership.objects.select_for_update()
                .filter(user=user)
                .first()
            )
            if existing_membership is not None:
                if existing_membership.couple_id == couple.pk:
                    raise CoupleRuleError("You cannot accept your own invitation.")
                waiting_couple = Couple.objects.select_for_update().get(
                    pk=existing_membership.couple_id
                )
                if (
                    existing_membership.slot != 1
                    or waiting_couple.memberships.count() != 1
                    or not Invitation.objects.filter(
                        couple=waiting_couple, used_at__isnull=True
                    ).exists()
                ):
                    raise CoupleRuleError(
                        "You are already connected to another partner."
                    )

            occupied_slots = set(
                couple.memberships.values_list("slot", flat=True)
            )
            if occupied_slots != {1}:
                raise CoupleRuleError("That invitation is no longer available.")

            if existing_membership is not None:
                waiting_couple.delete()
            CoupleMembership.objects.create(couple=couple, user=user, slot=2)
            FlowerPlant.objects.get_or_create(owner=user)
            invitation.used_at = timezone.now()
            invitation.save(update_fields=("used_at",))
    except IntegrityError as exc:
        raise CoupleRuleError(
            "You already belong to a couple, or that invitation was just used."
        ) from exc

    return couple

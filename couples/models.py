from django.conf import settings
from django.db import models
from django.db.models import Q


class Couple(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Couple {self.pk}"


class CoupleMembership(models.Model):
    couple = models.ForeignKey(
        Couple,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="couple_membership",
    )
    slot = models.PositiveSmallIntegerField(choices=((1, "First"), (2, "Second")))
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("couple", "slot"),
                name="unique_couple_member_slot",
            ),
            models.CheckConstraint(
                condition=Q(slot__in=(1, 2)),
                name="couple_member_slot_is_one_or_two",
            ),
        ]

    def __str__(self):
        return f"{self.user} in {self.couple}"


class Invitation(models.Model):
    couple = models.OneToOneField(
        Couple,
        on_delete=models.CASCADE,
        related_name="invitation",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(expires_at__gt=models.F("created_at")),
                name="invitation_expires_after_creation",
            ),
        ]

    def __str__(self):
        return f"Invitation for {self.couple}"

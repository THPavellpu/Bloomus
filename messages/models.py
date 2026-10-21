from django.conf import settings
from django.db import models
from django.db.models import F, Q

from couples.models import Couple
from garden.models import FlowerPlant


class RomanticMessage(models.Model):
    couple = models.ForeignKey(
        Couple,
        on_delete=models.CASCADE,
        related_name="romantic_messages",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_romantic_messages",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_romantic_messages",
    )
    watered_plant = models.ForeignKey(
        FlowerPlant,
        on_delete=models.CASCADE,
        related_name="watering_messages",
    )
    body = models.CharField(max_length=500)
    idempotency_key = models.UUIDField()
    sent_at = models.DateTimeField(editable=False)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-sent_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("sender", "idempotency_key"),
                name="unique_sender_message_submission",
            ),
            models.CheckConstraint(
                condition=~Q(body=""),
                name="romantic_message_body_not_empty",
            ),
            models.CheckConstraint(
                condition=~Q(sender=F("recipient")),
                name="romantic_message_sender_not_recipient",
            ),
        ]

    def __str__(self):
        return f"Message from {self.sender} to {self.recipient}"

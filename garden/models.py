from django.conf import settings
from django.db import models
from django.db.models import Q


class FlowerPlant(models.Model):
    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="flower_plant",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_watered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(last_watered_at__isnull=True)
                | Q(last_watered_at__gte=models.F("created_at")),
                name="plant_watered_at_after_creation",
            ),
        ]

    def __str__(self):
        return f"Flower plant belonging to {self.owner}"

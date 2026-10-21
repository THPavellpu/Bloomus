from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from .models import FlowerPlant


@dataclass(frozen=True)
class PlantCare:
    stage: str
    stage_key: str
    stage_description: str
    status: str
    status_description: str
    needs_water: bool
    animation: str


GROWTH_STAGES = (
    (7, "Bloom", "bloom", "Seven little notes have helped your flower bloom."),
    (3, "Bud", "bud", "Three kind notes have brought a little bud to life."),
    (1, "Sprout", "sprout", "Your first note has helped fresh leaves unfold."),
    (0, "Baby plant", "baby", "A tiny beginning, ready to grow with love."),
)
WATERING_INTERVAL = timedelta(hours=3)


def get_plant_care(
    plant: FlowerPlant,
    *,
    has_partner: bool,
    has_unread_note: bool = False,
    now: datetime | None = None,
) -> PlantCare:
    """Derive growth from watering notes and care from server time, without writes."""
    current_time = timezone.now() if now is None else now
    if timezone.is_naive(current_time):
        raise ValueError("Plant care requires a timezone-aware timestamp.")
    watering_count = plant.watering_messages.filter(
        recipient_id=plant.owner_id,
    ).exclude(sender_id=plant.owner_id).count()
    stage, stage_key, stage_description = next(
        (stage, key, description)
        for threshold, stage, key, description in GROWTH_STAGES
        if watering_count >= threshold
    )
    last_care_at = plant.last_watered_at or plant.created_at
    elapsed = max(current_time - last_care_at, timedelta(0))
    needs_water = has_partner and elapsed > WATERING_INTERVAL
    animation = "idle"

    if not has_partner:
        status = "Waiting for your person"
        status_description = "Your partner can care for this flower after joining."
    elif needs_water:
        status = "Ready for a little care"
        status_description = "It's been over three hours. A partner note will perk it up."
        animation = "wilting"
    elif plant.last_watered_at is None:
        status = "Ready for a little care"
        status_description = "A kind note from your partner will be the first drop."
    else:
        status = "Feeling loved"
        status_description = "Your partner has been here for your flower."
        if has_unread_note:
            animation = "growing"

    return PlantCare(
        stage=stage,
        stage_key=stage_key,
        stage_description=stage_description,
        status=status,
        status_description=status_description,
        needs_water=needs_water,
        animation=animation,
    )

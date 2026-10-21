from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from .models import FlowerPlant


@dataclass(frozen=True)
class PlantCare:
    stage: str
    stage_description: str
    status: str
    status_description: str


GROWTH_STAGES = (
    (timedelta(days=21), "Bloom", "Your flower is in full bloom."),
    (timedelta(days=8), "Bud", "A little bud is beginning to open."),
    (timedelta(days=3), "Sprout", "Fresh leaves are reaching for the light."),
    (timedelta(0), "Seedling", "Every lovely garden starts somewhere."),
)
WELL_CARED_FOR = timedelta(hours=36)
NEEDS_CARE = timedelta(hours=72)


def get_plant_care(
    plant: FlowerPlant,
    *,
    has_partner: bool,
    now: datetime | None = None,
) -> PlantCare:
    """Calculate display state from server time; never mutate plant data."""
    current_time = timezone.now() if now is None else now
    if timezone.is_naive(current_time):
        raise ValueError("Plant care requires a timezone-aware timestamp.")
    age = max(current_time - plant.created_at, timedelta(0))
    stage, stage_description = next(
        (stage, description)
        for threshold, stage, description in GROWTH_STAGES
        if age >= threshold
    )

    if not has_partner:
        status = "Waiting for your person"
        status_description = "Your partner can care for this flower after joining."
    elif plant.last_watered_at is None:
        status = "Ready for a little care"
        status_description = "A kind note from your partner will be the first drop."
    else:
        elapsed = max(current_time - plant.last_watered_at, timedelta(0))
        if elapsed <= WELL_CARED_FOR:
            status = "Feeling loved"
            status_description = "Your partner has been here for your flower."
        elif elapsed <= NEEDS_CARE:
            status = "Could use a little care"
            status_description = "A little note from your partner would mean a lot."
        else:
            status = "Ready for a little care"
            status_description = "Your flower is waiting for a kind word."

    return PlantCare(
        stage=stage,
        stage_description=stage_description,
        status=status,
        status_description=status_description,
    )

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from couples.models import CoupleMembership

from .models import FlowerPlant
from .services import get_plant_care


@login_required
def dashboard(request):
    membership = (
        CoupleMembership.objects.filter(user=request.user)
        .select_related("couple")
        .first()
    )
    partner = None
    plants = []
    has_partner = False
    just_watered_plant_id = request.session.pop("just_watered_plant_id", None)

    if membership is not None:
        partner_membership = (
            CoupleMembership.objects.filter(couple=membership.couple)
            .exclude(user=request.user)
            .select_related("user")
            .first()
        )
        if partner_membership is not None:
            partner = partner_membership.user
            has_partner = True

        memberships = CoupleMembership.objects.filter(
            couple=membership.couple
        ).select_related("user")
        owners = [member.user for member in memberships]
        plant_by_owner = {
            plant.owner_id: plant
            for plant in FlowerPlant.objects.filter(owner__in=owners)
        }
        for member in memberships:
            plant = plant_by_owner.get(member.user_id)
            if plant is not None:
                plants.append(
                    {
                        "owner": member.user,
                        "is_mine": member.user_id == request.user.pk,
                        "plant": plant,
                        "just_watered": plant.pk == just_watered_plant_id,
                        "care": get_plant_care(
                            plant,
                            has_partner=has_partner,
                            now=timezone.now(),
                        ),
                    }
                )

    context = {
        "membership": membership,
        "partner": partner,
        "plants": plants,
        "new_invitation_code": request.session.pop(
            "new_invitation_code", None
        ),
    }
    return render(request, "garden/dashboard.html", context)

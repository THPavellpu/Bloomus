from django.contrib import messages as flash_messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from couples.models import CoupleMembership

from .forms import RomanticMessageForm
from .services import (
    DuplicateSubmission,
    MessageRateLimitExceeded,
    MessageRuleError,
    received_notes_for,
    send_message_and_water,
)


@login_required
@require_http_methods(["GET", "POST"])
def write_message(request):
    membership = (
        CoupleMembership.objects.filter(user=request.user)
        .select_related("couple")
        .first()
    )
    if membership is None:
        raise Http404

    partner_membership = (
        CoupleMembership.objects.filter(couple=membership.couple)
        .exclude(user=request.user)
        .select_related("user")
        .first()
    )
    if partner_membership is None:
        raise Http404

    form = RomanticMessageForm(
        request.POST if request.method == "POST" else None
    )
    if request.method == "POST" and form.is_valid():
        try:
            message = send_message_and_water(
                request.user,
                body=form.cleaned_data["body"],
                idempotency_key=form.cleaned_data["idempotency_key"],
            )
        except DuplicateSubmission as exc:
            flash_messages.info(request, str(exc))
            return redirect("garden:dashboard")
        except MessageRateLimitExceeded as exc:
            form.add_error(None, str(exc))
        except MessageRuleError as exc:
            flash_messages.error(request, str(exc))
            return redirect("garden:dashboard")
        else:
            request.session["just_watered_plant_id"] = message.watered_plant_id
            flash_messages.success(request, "Your note was sent with a little water.")
            return redirect("garden:dashboard")

    return render(
        request,
        "messages/write.html",
        {
            "form": form,
            "recipient": partner_membership.user,
        },
    )


@login_required
@require_GET
def message_history(request):
    received_messages = received_notes_for(request.user).select_related("sender")
    return render(
        request,
        "messages/history.html",
        {"received_messages": received_messages},
    )


@login_required
@require_POST
def mark_message_read(request, message_id):
    with transaction.atomic():
        message = get_object_or_404(
            received_notes_for(request.user).select_for_update(),
            pk=message_id,
            recipient=request.user,
        )
        if message.read_at is None:
            message.read_at = timezone.now()
            message.save(update_fields=("read_at",))
    return redirect("romantic_messages:history")

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .services import CoupleRuleError, accept_invitation, create_invitation


@login_required
@require_POST
def invite_partner(request):
    try:
        created = create_invitation(request.user)
    except CoupleRuleError as exc:
        messages.error(request, str(exc))
    else:
        request.session["new_invitation_code"] = created.code
        messages.success(
            request,
            "Your invitation is ready. Share the code with your partner.",
        )
    return redirect("garden:dashboard")


@login_required
@require_POST
def join_couple(request):
    try:
        accept_invitation(request.user, request.POST.get("code", ""))
    except CoupleRuleError as exc:
        messages.error(request, str(exc))
    else:
        request.session.pop("new_invitation_code", None)
        messages.success(request, "You and your partner are connected!")
    return redirect("garden:dashboard")

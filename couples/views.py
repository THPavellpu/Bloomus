from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .services import (
    CoupleRuleError,
    accept_invitation,
    available_invitation,
    create_invitation,
    normalize_invitation_code,
)


def complete_pending_invitation(request):
    code = request.session.pop("pending_invitation_code", None)
    if code is None:
        return
    try:
        accept_invitation(request.user, code)
    except CoupleRuleError as exc:
        messages.error(request, f"We couldn't connect you: {exc}")
    else:
        request.session.pop("new_invitation_code", None)
        messages.success(request, "You and your partner are connected!")


@never_cache
@require_http_methods(["GET", "POST"])
def invitation_link(request, code):
    error = None
    inviter = None
    try:
        invitation = available_invitation(code)
    except CoupleRuleError as exc:
        error = str(exc)
    else:
        inviter = invitation.couple.memberships.select_related("user").get(slot=1).user
        if request.method == "POST":
            request.session["pending_invitation_code"] = normalize_invitation_code(code)
            if request.user.is_authenticated:
                complete_pending_invitation(request)
                return redirect("garden:dashboard")
            return redirect("signup")

    response = render(
        request,
        "couples/invitation.html",
        {"inviter": inviter, "invitation_error": error},
        status=400 if error else 200,
    )
    response["Referrer-Policy"] = "no-referrer"
    return response


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
            "Your invitation is ready. Share the code or link with your partner.",
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

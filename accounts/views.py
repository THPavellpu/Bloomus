from django.contrib.auth import login
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView

from couples.views import complete_pending_invitation

from .forms import SignUpForm


class InvitationContextMixin:
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["has_pending_invitation"] = bool(
            self.request.session.get("pending_invitation_code")
        )
        return context


class PartnerLoginView(InvitationContextMixin, LoginView):
    template_name = "accounts/login.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.session.get("pending_invitation_code"):
            complete_pending_invitation(self.request)
            return redirect("garden:dashboard")
        return response


class SignUpView(InvitationContextMixin, CreateView):
    form_class = SignUpForm
    template_name = "accounts/signup.html"
    success_url = reverse_lazy("login")

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.session.get("pending_invitation_code"):
            login(self.request, self.object)
            complete_pending_invitation(self.request)
            return redirect("garden:dashboard")
        return response

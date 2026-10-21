from django.urls import path

from . import views

app_name = "couples"

urlpatterns = [
    path("join/<str:code>/", views.invitation_link, name="invitation_link"),
    path("invite/", views.invite_partner, name="invite"),
    path("accept/", views.join_couple, name="accept"),
]

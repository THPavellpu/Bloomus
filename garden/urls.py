from django.urls import path

from . import views

app_name = "garden"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
]

from django.urls import path

from . import views

app_name = "romantic_messages"

urlpatterns = [
    path("", views.message_history, name="history"),
    path("write/", views.write_message, name="write"),
    path("<int:message_id>/read/", views.mark_message_read, name="mark_read"),
]

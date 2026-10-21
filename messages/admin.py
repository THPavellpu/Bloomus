from django.contrib import admin

from .models import RomanticMessage


@admin.register(RomanticMessage)
class RomanticMessageAdmin(admin.ModelAdmin):
    list_display = ("sender", "recipient", "sent_at", "read_at")
    list_filter = ("sent_at", "read_at")
    search_fields = ("sender__username", "recipient__username", "body")
    readonly_fields = ("sent_at", "read_at")

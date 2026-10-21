from .services import received_notes_for


def note_notifications(request):
    return {
        "unread_note_count": received_notes_for(request.user)
        .filter(read_at__isnull=True)
        .count(),
    }

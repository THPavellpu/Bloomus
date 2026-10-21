import uuid

from django import forms


class RomanticMessageForm(forms.Form):
    body = forms.CharField(
        label="Your message",
        max_length=500,
        strip=True,
        error_messages={"required": "Write a little note before sending."},
        widget=forms.Textarea(
            attrs={
                "rows": 5,
                "maxlength": 500,
                "placeholder": "Write something that will make their day...",
                "autofocus": True,
                "aria-describedby": "message-help message-preview",
            }
        ),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["idempotency_key"].initial = uuid.uuid4()

    def clean_body(self):
        body = self.cleaned_data["body"].strip()
        if not body:
            raise forms.ValidationError("Write a little note before sending.")
        return body

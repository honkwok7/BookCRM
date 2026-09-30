from django import forms


class BookingDetailsForm(forms.Form):
    """The visitor's contact details in the public booking wizard.

    ``website`` is a honeypot: hidden from people (and from assistive technology), but bots
    that fill in every field fill it too, and the booking is refused.
    """

    name = forms.CharField(max_length=255, label="Full name")
    email = forms.EmailField(help_text="We'll send your confirmation here.")
    phone = forms.CharField(max_length=30, required=False)
    notes = forms.CharField(
        max_length=2000,
        required=False,
        label="Anything we should know?",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    website = forms.CharField(
        required=False,
        label="Leave this empty",
        widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs["autocomplete"] = "name"
        self.fields["email"].widget.attrs["autocomplete"] = "email"
        self.fields["phone"].widget.attrs.update({"autocomplete": "tel", "type": "tel"})

    def clean_website(self):
        if self.cleaned_data.get("website"):
            raise forms.ValidationError("Please leave this field empty.")
        return ""

    def customer(self) -> dict:
        data = self.cleaned_data
        return {
            "name": data["name"].strip(),
            "email": data["email"].strip(),
            "phone": data["phone"].strip(),
            "notes": data["notes"].strip(),
        }

"""Forms for the facilitator dashboard and the owner portal."""

import secrets

from django import forms
from django.contrib.auth import get_user_model
from django.utils.text import slugify

from .models import Complaint, Household, Notification, Segment

User = get_user_model()


class HouseholdForm(forms.ModelForm):
    """
    Facilitator-side household record.

    Ticking create_account provisions the owner login, matching the paper:
    the facilitator maintains the household database and there is no open
    self sign-up on the owner portal.
    """

    create_account = forms.BooleanField(
        required=False,
        initial=True,
        label="Create an Owner Portal login for this household",
        help_text="A username and temporary password are generated and shown once after saving.",
    )

    class Meta:
        model = Household
        fields = [
            "household_id",
            "owner_name",
            "address",
            "contact_number",
            "segment",
            "account_status",
            "notes",
        ]
        widgets = {
            "address": forms.TextInput(),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "contact_number": forms.TextInput(attrs={"placeholder": "+639171234567"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["segment"].queryset = Segment.objects.filter(is_active=True)
        self.fields["segment"].empty_label = "Not assigned"
        if self.instance.pk and self.instance.user_id:
            # The account already exists; do not offer to create a second one.
            del self.fields["create_account"]

    def generate_credentials(self):
        """Build a unique username and a temporary password for the owner."""
        base = slugify(self.cleaned_data["owner_name"]).replace("-", ".")[:20] or "owner"
        username = base
        suffix = 1
        while User.objects.filter(username=username).exists():
            suffix += 1
            username = f"{base}{suffix}"
        return username, secrets.token_urlsafe(6)


class ComplaintForm(forms.ModelForm):
    """Owner-side complaint submission."""

    class Meta:
        model = Complaint
        fields = ["category", "subject", "body"]
        labels = {"body": "Describe the problem"}
        widgets = {
            "subject": forms.TextInput(attrs={"placeholder": "Short summary of the problem"}),
            "body": forms.Textarea(
                attrs={"rows": 5, "placeholder": "When did it start? What exactly are you seeing?"}
            ),
        }


class ComplaintResponseForm(forms.ModelForm):
    """Facilitator reply to a complaint."""

    class Meta:
        model = Complaint
        fields = ["status", "response"]
        widgets = {"response": forms.Textarea(attrs={"rows": 4})}


class NotificationForm(forms.Form):
    """Targeted or broadcast notice from the facilitator."""

    AUDIENCE_ALL = "all"
    AUDIENCE_SEGMENT = "segment"
    AUDIENCE_HOUSEHOLD = "household"

    audience = forms.ChoiceField(
        choices=[
            (AUDIENCE_ALL, "All active households (broadcast)"),
            (AUDIENCE_SEGMENT, "Every household on one supply line"),
            (AUDIENCE_HOUSEHOLD, "One household"),
        ],
        initial=AUDIENCE_ALL,
    )
    segment = forms.ModelChoiceField(queryset=Segment.objects.none(), required=False)
    household = forms.ModelChoiceField(queryset=Household.objects.none(), required=False)
    title = forms.CharField(max_length=150, widget=forms.TextInput(attrs={"placeholder": "e.g. Scheduled repair on Line 2"}))
    body = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Include the expected restoration time if you know it."})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["segment"].queryset = Segment.objects.filter(is_active=True)
        self.fields["household"].queryset = Household.objects.filter(
            account_status=Household.AccountStatus.ACTIVE
        )

    def clean(self):
        cleaned = super().clean()
        audience = cleaned.get("audience")
        if audience == self.AUDIENCE_SEGMENT and not cleaned.get("segment"):
            self.add_error("segment", "Choose the supply line to notify.")
        if audience == self.AUDIENCE_HOUSEHOLD and not cleaned.get("household"):
            self.add_error("household", "Choose the household to notify.")
        return cleaned

    def recipients(self):
        """Resolve the audience choice into the households to write rows for."""
        audience = self.cleaned_data["audience"]
        if audience == self.AUDIENCE_HOUSEHOLD:
            return [self.cleaned_data["household"]], Notification.Kind.TARGETED
        if audience == self.AUDIENCE_SEGMENT:
            return (
                list(
                    Household.objects.filter(
                        segment=self.cleaned_data["segment"],
                        account_status=Household.AccountStatus.ACTIVE,
                    )
                ),
                Notification.Kind.TARGETED,
            )
        return (
            list(Household.objects.filter(account_status=Household.AccountStatus.ACTIVE)),
            Notification.Kind.BROADCAST,
        )


class SegmentForm(forms.ModelForm):
    """
    Distance markers and optional coordinates per monitored hose line.

    start_m / end_m are what turn a module-level alert into the location the
    facilitator actually walks to, so they are editable here as well as in
    the Django admin.
    """

    class Meta:
        model = Segment
        fields = [
            "name",
            "start_m",
            "end_m",
            "start_lat",
            "start_lng",
            "end_lat",
            "end_lng",
            "maintenance_note",
            "is_active",
        ]
        help_texts = {
            "start_lat": "Optional. Fill all four coordinates to draw this line on the map view.",
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_m"), cleaned.get("end_m")
        if start is not None and end is not None and end <= start:
            self.add_error("end_m", "The end marker must be further from the valve than the start marker.")
        return cleaned


class MaintenanceForm(forms.Form):
    """Park a segment under maintenance, or return it to live monitoring."""

    note = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Replacing coupling at the 22 m marker"}),
    )

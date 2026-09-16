"""Forms for the facilitator dashboard and the owner portal."""

import secrets

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils.text import slugify

from .models import (
    Complaint,
    ComplaintMessage,
    Household,
    Notification,
    Segment,
    WaterSource,
)


def check_within_municipality(form, lat, lng):
    """
    Flag coordinates that fall outside the served municipality.

    A transposed or missing digit lands hundreds of kilometres away, and on
    the map that reads as a pin in the sea rather than an obvious mistake.
    Shared by every form that takes a location.
    """
    if lat is None or lng is None:
        return
    south, west, north, east = settings.MUNICIPALITY_BOUNDS
    margin = settings.MUNICIPALITY_BOUNDS_MARGIN_DEG
    if not (south - margin <= lat <= north + margin):
        form.add_error(
            "latitude",
            f"That is outside {settings.MUNICIPALITY_NAME}. "
            f"Expected roughly {south:.2f} to {north:.2f}.",
        )
    if not (west - margin <= lng <= east + margin):
        form.add_error(
            "longitude",
            f"That is outside {settings.MUNICIPALITY_NAME}. "
            f"Expected roughly {west:.2f} to {east:.2f}.",
        )


class WaterSourceForm(forms.ModelForm):
    """Where the main line starts. Every distance marker is measured from it."""

    class Meta:
        model = WaterSource
        fields = ["name", "latitude", "longitude", "notes"]
        widgets = {
            "latitude": forms.NumberInput(attrs={"step": "any", "placeholder": "9.6620"}),
            "longitude": forms.NumberInput(attrs={"step": "any", "placeholder": "125.5290"}),
            "notes": forms.TextInput(attrs={"placeholder": "e.g. beside the barangay hall"}),
        }

    def clean(self):
        cleaned = super().clean()
        lat, lng = cleaned.get("latitude"), cleaned.get("longitude")
        if (lat is None) != (lng is None):
            missing = "longitude" if lng is None else "latitude"
            self.add_error(missing, "Enter both latitude and longitude, or leave both blank.")
            return cleaned
        check_within_municipality(self, lat, lng)
        return cleaned

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
        # household_id is deliberately absent: it is assigned sequentially by
        # Household.save(). Editing keeps the id it was given.
        fields = [
            "owner_name",
            "address",
            "contact_number",
            "segment",
            "account_status",
            "latitude",
            "longitude",
            "notes",
        ]
        widgets = {
            "address": forms.TextInput(),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "contact_number": forms.TextInput(attrs={"placeholder": "+639171234567"}),
            # step=any allows full decimal precision; a plain number input
            # otherwise rejects anything with decimals in some browsers.
            "latitude": forms.NumberInput(attrs={"step": "any", "placeholder": "9.7167"}),
            "longitude": forms.NumberInput(attrs={"step": "any", "placeholder": "125.5333"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["segment"].queryset = Segment.objects.filter(is_active=True)
        self.fields["segment"].empty_label = "Not assigned"
        if self.instance.pk and self.instance.user_id:
            # The account already exists; do not offer to create a second one.
            del self.fields["create_account"]

    def clean(self):
        cleaned = super().clean()
        lat = cleaned.get("latitude")
        lng = cleaned.get("longitude")
        # One without the other cannot be plotted, and silently ignoring the
        # half that was filled in would look like the value was not saved.
        if (lat is None) != (lng is None):
            missing = "longitude" if lng is None else "latitude"
            self.add_error(missing, "Enter both latitude and longitude, or leave both blank.")
            return cleaned
        check_within_municipality(self, lat, lng)
        return cleaned

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


class ComplaintMessageForm(forms.ModelForm):
    """One message added to a complaint thread, by either side."""

    class Meta:
        model = ComplaintMessage
        fields = ["body"]
        labels = {"body": ""}
        widgets = {
            "body": forms.Textarea(
                attrs={"rows": 2, "placeholder": "Write a message...", "class": "thread-input"}
            )
        }

    def clean_body(self):
        body = (self.cleaned_data.get("body") or "").strip()
        if not body:
            raise forms.ValidationError("Write something before sending.")
        return body


class ComplaintStatusForm(forms.ModelForm):
    """Facilitator changing where a complaint stands, without writing a reply."""

    class Meta:
        model = Complaint
        fields = ["status"]


class ComplaintResponseForm(forms.ModelForm):
    """
    Legacy single-reply form.

    Superseded by ComplaintMessageForm now that complaints are threads; kept
    only so older code paths and the Django admin keep working.
    """

    class Meta:
        model = Complaint
        fields = ["status", "response"]
        widgets = {
            "response": forms.Textarea(
                attrs={"rows": 4, "placeholder": "Write your reply to the household owner..."}
            )
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The reply that was already sent is shown above the form, so echoing
        # it back into the box only makes it look like the message failed to
        # send. Start empty; status keeps its value because that is a current
        # state rather than something being composed.
        self.new_reply = False
        if not self.is_bound:
            self.initial["response"] = ""

    def clean_response(self):
        """
        An empty box means "no new reply", not "delete the reply I sent".

        Now that the box starts empty, changing only the status would
        otherwise submit a blank response and silently wipe the message the
        owner has already received.
        """
        value = (self.cleaned_data.get("response") or "").strip()
        self.new_reply = bool(value)
        if not value and self.instance.pk:
            return self.instance.response
        return value


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
    One household's hose line: its board, its threshold, its route on the map.

    Distance markers used to live here, back when three sensor pairs shared a
    single run and a leak could only be narrowed to a stretch of it. Each
    house now has its own hose and its own board, so a leak is attributed to
    the household and there is no distance left to record.
    """

    class Meta:
        model = Segment
        fields = [
            "name",
            "device",
            "color",
            "low_pressure_bar",
            "start_lat",
            "start_lng",
            "end_lat",
            "end_lng",
            "maintenance_note",
            "is_active",
        ]
        help_texts = {
            "device": (
                "The board fitted to this line. Leave blank until one is installed - "
                "the dashboard then shows the line as awaiting hardware rather than "
                "as a fault."
            ),
            "low_pressure_bar": (
                "Below this, the line reports Low Water Supply. The device picks the "
                "new value up within seconds - no re-flashing."
            ),
            "start_lat": "Optional. Fill all four coordinates to draw this line on the map view.",
        }
        widgets = {
            "color": forms.RadioSelect(attrs={"class": "swatch-input"}),
            "low_pressure_bar": forms.NumberInput(attrs={"step": "0.05", "min": "0", "max": "12"}),
        }

    def clean_low_pressure_bar(self):
        """
        Warn about a threshold that would make the line useless.

        Both mistakes are easy to make and neither announces itself: set it
        at 0 and a line can drain to nothing without ever raising Low Water
        Supply, while a gravity-fed line set above about 4 bar would have to
        be 40 m below its source to ever read "normal", so it alarms forever
        and the facilitator learns to ignore it.
        """
        value = self.cleaned_data["low_pressure_bar"]
        if value <= 0:
            raise forms.ValidationError(
                "A threshold of 0 can never be crossed, so this line would never "
                "report low supply. Use at least 0.05 bar."
            )
        if value > 4:
            raise forms.ValidationError(
                "That is higher than a gravity-fed line normally reaches, so it would "
                "report low supply permanently. Rural systems usually sit between "
                "0.3 and 2 bar."
            )
        return value


class SegmentCreateForm(forms.ModelForm):
    """
    Adding a new hose line.

    Narrower than SegmentForm on purpose: a brand-new line has no readings,
    no maintenance note and nothing to deactivate, so those fields would only
    be noise. It is created with no device - the board is attached from Edit
    once one is actually fitted to the hose.
    """

    class Meta:
        model = Segment
        fields = ["name", "color"]
        widgets = {
            "color": forms.RadioSelect(attrs={"class": "swatch-input"}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop("device", None)  # accepted and ignored; lines start unfitted
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            # Offer the next unused colour rather than always the first one,
            # so two lines added in a row do not come out looking identical.
            self.fields["color"].initial = Segment.next_color(None)

    def save(self, commit=True):
        segment = super().save(commit=False)
        segment.module_index = 1  # one board, one sensor pair, one house
        if commit:
            segment.save()
        return segment


class MaintenanceForm(forms.Form):
    """Park a segment under maintenance, or return it to live monitoring."""

    note = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Replacing coupling at the 22 m marker"}),
    )

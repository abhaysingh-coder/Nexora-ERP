from django import forms

from core.organization_context import available_organizations


class OrganizationSelectionForm(forms.Form):
    organization = forms.ModelChoiceField(
        queryset=None,
        label='Organization',
        empty_label='Choose an organization',
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['organization'].queryset = available_organizations(user)

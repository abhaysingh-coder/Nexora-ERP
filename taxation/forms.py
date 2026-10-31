from django import forms

from .models import ProductTaxClassification, TaxCode, TaxRate, TaxRegistration


class TaxCodeForm(forms.ModelForm):
    class Meta:
        model = TaxCode
        fields = ['code', 'name', 'category', 'description', 'is_active']
        widgets = {
            'code': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def clean_code(self):
        code = self.cleaned_data['code']
        organization_id = self.instance.organization_id
        duplicate = TaxCode.objects.filter(organization_id=organization_id, code=code)
        if self.instance.pk:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError('This tax code already exists in the organization.')
        return code


class TaxRateForm(forms.ModelForm):
    class Meta:
        model = TaxRate
        fields = ['tax_code', 'tax_type', 'rate', 'state_code', 'effective_from', 'effective_to', 'is_active']
        widgets = {
            'tax_code': forms.Select(attrs={'class': 'form-control'}),
            'tax_type': forms.Select(attrs={'class': 'form-control'}),
            'rate': forms.NumberInput(attrs={'class': 'form-control'}),
            'state_code': forms.TextInput(attrs={'class': 'form-control'}),
            'effective_from': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'effective_to': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class TaxRegistrationForm(forms.ModelForm):
    class Meta:
        model = TaxRegistration
        fields = ['branch', 'gstin', 'state_code', 'is_registered', 'is_reverse_charge_applicable']
        widgets = {
            'branch': forms.Select(attrs={'class': 'form-control'}),
            'gstin': forms.TextInput(attrs={'class': 'form-control'}),
            'state_code': forms.TextInput(attrs={'class': 'form-control'}),
            'is_registered': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_reverse_charge_applicable': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class ProductTaxClassificationForm(forms.ModelForm):
    class Meta:
        model = ProductTaxClassification
        fields = [
            'product',
            'tax_code',
            'hsn_sac',
            'tax_inclusive',
            'is_exempt',
            'is_zero_rated',
            'is_reverse_charge',
            'is_input_tax_credit_eligible',
            'notes',
        ]
        widgets = {
            'product': forms.Select(attrs={'class': 'form-control'}),
            'tax_code': forms.Select(attrs={'class': 'form-control'}),
            'hsn_sac': forms.TextInput(attrs={'class': 'form-control'}),
            'tax_inclusive': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_exempt': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_zero_rated': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_reverse_charge': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_input_tax_credit_eligible': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

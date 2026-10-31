from django import forms
from django.db import models

from .models import (
    AccountingBudget,
    AccountingPeriod,
    FinancialYear,
    FixedAsset,
    FixedAssetCategory,
)
from .models import Account
from organizations.models import Branch


class FinancialYearForm(forms.ModelForm):
    class Meta:
        model = FinancialYear
        fields = ['name', 'starts_on', 'ends_on', 'retained_earnings_account']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'starts_on': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'ends_on': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'retained_earnings_account': forms.Select(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.organization = organization
        self.fields['retained_earnings_account'].required = False
        self.fields['retained_earnings_account'].queryset = Account.objects.filter(
            organization=organization,
            account_type='equity',
            is_active=True,
            branch__isnull=True,
        ).order_by('code')


class YearEndCloseForm(forms.Form):
    retained_earnings_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    reason = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    confirm = forms.BooleanField(
        required=False,
        label='I have reviewed the closing checklist and confirm this irreversible close.',
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['retained_earnings_account'].queryset = Account.objects.filter(
            organization=organization,
            account_type='equity',
            is_active=True,
            branch__isnull=True,
        ).order_by('code')

    def clean(self):
        cleaned = super().clean()
        if self.data.get('action') == 'confirm':
            if not cleaned.get('reason', '').strip():
                self.add_error('reason', 'A reason is required to close the financial year.')
            if not cleaned.get('confirm'):
                self.add_error('confirm', 'Confirm that you reviewed the checklist before closing.')
        return cleaned


class PeriodReopenForm(forms.Form):
    reason = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )


class BudgetForm(forms.ModelForm):
    class Meta:
        model = AccountingBudget
        fields = ['period', 'account', 'branch', 'department', 'cost_center', 'amount']
        widgets = {
            'period': forms.Select(attrs={'class': 'form-control'}),
            'account': forms.Select(attrs={'class': 'form-control'}),
            'branch': forms.Select(attrs={'class': 'form-control'}),
            'department': forms.TextInput(attrs={'class': 'form-control'}),
            'cost_center': forms.TextInput(attrs={'class': 'form-control'}),
            'amount': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        }

    def __init__(self, *args, organization, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.organization = organization
        self.fields['period'].queryset = AccountingPeriod.objects.filter(
            financial_year__organization=organization
        ).order_by('-starts_on')
        accounts = Account.objects.filter(organization=organization, is_active=True)
        branches = organization.branches.filter(is_active=True)
        if branch:
            accounts = accounts.filter(models.Q(branch=branch) | models.Q(branch__isnull=True))
            branches = branches.filter(pk=branch.pk)
            self.fields['branch'].initial = branch
            self.fields['branch'].disabled = True
        self.fields['account'].queryset = accounts.order_by('code')
        self.fields['branch'].queryset = branches


class FixedAssetCategoryForm(forms.ModelForm):
    class Meta:
        model = FixedAssetCategory
        fields = [
            'name',
            'asset_account',
            'accumulated_depreciation_account',
            'depreciation_expense_account',
            'depreciation_method',
            'annual_rate',
            'default_useful_life_months',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'asset_account': forms.Select(attrs={'class': 'form-control'}),
            'accumulated_depreciation_account': forms.Select(attrs={'class': 'form-control'}),
            'depreciation_expense_account': forms.Select(attrs={'class': 'form-control'}),
            'depreciation_method': forms.Select(attrs={'class': 'form-control'}),
            'annual_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.001'}),
            'default_useful_life_months': forms.NumberInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, organization, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.organization = organization
        accounts = Account.objects.filter(organization=organization, is_active=True)
        if branch:
            accounts = accounts.filter(models.Q(branch=branch) | models.Q(branch__isnull=True))
        self.fields['asset_account'].queryset = accounts.filter(account_type='asset').order_by('code')
        self.fields['accumulated_depreciation_account'].queryset = accounts.filter(account_type='asset').order_by('code')
        self.fields['depreciation_expense_account'].queryset = accounts.filter(account_type='expense').order_by('code')


class FixedAssetForm(forms.ModelForm):
    class Meta:
        model = FixedAsset
        fields = [
            'category',
            'asset_code',
            'name',
            'acquisition_date',
            'acquisition_cost',
            'salvage_value',
            'useful_life_months',
            'branch',
        ]
        widgets = {
            'category': forms.Select(attrs={'class': 'form-control'}),
            'asset_code': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'acquisition_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'acquisition_cost': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'salvage_value': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'useful_life_months': forms.NumberInput(attrs={'class': 'form-control'}),
            'branch': forms.Select(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, organization, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.organization = organization
        self.fields['category'].queryset = FixedAssetCategory.objects.filter(
            organization=organization
        ).order_by('name')
        branches = organization.branches.filter(is_active=True)
        if branch:
            branches = branches.filter(pk=branch.pk)
            self.fields['branch'].initial = branch
            self.fields['branch'].disabled = True
        self.fields['branch'].queryset = branches

    def clean(self):
        cleaned = super().clean()
        category = cleaned.get('category')
        if category and not cleaned.get('useful_life_months'):
            cleaned['useful_life_months'] = category.default_useful_life_months
        return cleaned


class AssetDisposalForm(forms.Form):
    disposal_date = forms.DateField(widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}))
    proceeds = forms.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=0,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
    )


class AssetTransferForm(forms.Form):
    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), required=False)

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['branch'].queryset = organization.branches.filter(is_active=True).order_by('name')
        self.fields['branch'].widget.attrs.update({'class': 'form-control'})

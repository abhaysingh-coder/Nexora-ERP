from django import forms
from django.db.models import Q

from purchases.models import PurchaseInvoice
from sales.models import SalesInvoice

from .models import BankAccount, BankTransaction


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ['name', 'account_type', 'account_number', 'opening_balance', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'account_type': forms.Select(attrs={'class': 'form-control'}),
            'account_number': forms.TextInput(attrs={'class': 'form-control'}),
            'opening_balance': forms.NumberInput(attrs={'class': 'form-control'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class BankTransactionForm(forms.ModelForm):
    class Meta:
        model = BankTransaction
        fields = ['account', 'related_account', 'transaction_type', 'reference_number', 'transaction_date', 'amount', 'payment_reference', 'description', 'customer', 'supplier']
        widgets = {
            'account': forms.Select(attrs={'class': 'form-control'}),
            'related_account': forms.Select(attrs={'class': 'form-control'}),
            'transaction_type': forms.Select(attrs={'class': 'form-control'}),
            'reference_number': forms.TextInput(attrs={'class': 'form-control'}),
            'transaction_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'amount': forms.NumberInput(attrs={'class': 'form-control'}),
            'payment_reference': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'customer': forms.Select(attrs={'class': 'form-control'}),
            'supplier': forms.Select(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        organization = kwargs.pop('organization', None)
        branch = kwargs.pop('branch', None)
        super().__init__(*args, **kwargs)
        if organization:
            accounts = organization.bank_accounts.all()
            if branch:
                from django.db.models import Q

                accounts = accounts.filter(Q(branch=branch) | Q(branch__isnull=True))
            self.fields['account'].queryset = accounts
            self.fields['related_account'].queryset = accounts
            self.fields['customer'].queryset = organization.customers.all()
            self.fields['supplier'].queryset = organization.suppliers.all()


class InvoiceAllocationForm(forms.Form):
    invoice = forms.ModelChoiceField(queryset=SalesInvoice.objects.none(), label='Outstanding invoice')
    amount = forms.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=0.01,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
    )

    def __init__(self, *args, bank_transaction, **kwargs):
        super().__init__(*args, **kwargs)
        if bank_transaction.transaction_type == 'receipt' and bank_transaction.customer_id:
            invoices = SalesInvoice.objects.filter(
                organization_id=bank_transaction.organization_id,
                customer_id=bank_transaction.customer_id,
                status='posted',
            )
        elif bank_transaction.transaction_type == 'payment' and bank_transaction.supplier_id:
            invoices = PurchaseInvoice.objects.filter(
                organization_id=bank_transaction.organization_id,
                supplier_id=bank_transaction.supplier_id,
                status='posted',
            )
        else:
            invoices = SalesInvoice.objects.none()
        if bank_transaction.branch_id:
            invoices = invoices.filter(Q(branch_id=bank_transaction.branch_id) | Q(branch__isnull=True))
        self.fields['invoice'].queryset = invoices.order_by('due_date', 'invoice_date', 'id')


class AllocationReversalForm(forms.Form):
    reason = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

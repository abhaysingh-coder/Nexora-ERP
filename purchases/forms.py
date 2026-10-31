from decimal import Decimal

from django import forms
from django.db.models import Sum
from django.forms import inlineformset_factory

from accounting.models import round_currency
from inventory.models import Product, Warehouse
from organizations.models import Branch

from .models import (
    GoodsReceiptLine,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
)


class PurchaseOrderForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = ['supplier', 'branch', 'order_number', 'order_date']
        widgets = {'order_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'})}

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.user = user
        self.fields['supplier'].queryset = Supplier.objects.filter(organization=organization)
        branches = Branch.objects.filter(organization=organization, is_active=True)
        if user.branch_id:
            branches = branches.filter(pk=user.branch_id)
            self.fields['branch'].initial = user.branch
            self.fields['branch'].widget = forms.HiddenInput()
        self.fields['branch'].queryset = branches

    def clean(self):
        cleaned = super().clean()
        branch = cleaned.get('branch')
        if self.user.branch_id:
            cleaned['branch'] = self.user.branch
        elif branch and branch.organization_id != self.organization.pk:
            self.add_error('branch', 'Select a branch belonging to this organization.')
        return cleaned

    def clean_order_number(self):
        number = self.cleaned_data['order_number'].strip()
        if PurchaseOrder.objects.filter(order_number=number).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('This purchase order number is already in use.')
        return number


class PurchaseOrderLineForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrderLine
        fields = ['product', 'quantity', 'unit_cost', 'discount_amount']
        widgets = {
            'quantity': forms.NumberInput(attrs={'step': '0.001', 'min': '0.001', 'class': 'form-control'}),
            'unit_cost': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
            'discount_amount': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
        self.fields['product'].queryset = Product.objects.filter(
            organization=organization,
            is_active=True,
        ).order_by('name')

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get('product')
        quantity = cleaned.get('quantity')
        unit_cost = cleaned.get('unit_cost')
        discount = cleaned.get('discount_amount') or Decimal('0')
        cleaned['discount_amount'] = discount
        if not product:
            if quantity is not None or unit_cost is not None or discount:
                self.add_error('product', 'Select a product for this order line.')
            return cleaned
        if quantity is None or quantity <= 0:
            self.add_error('quantity', 'Quantity must be greater than zero.')
        if unit_cost is None or unit_cost < 0:
            self.add_error('unit_cost', 'Enter a non-negative unit cost.')
        if discount < 0:
            self.add_error('discount_amount', 'Discount cannot be negative.')
        if quantity is not None and unit_cost is not None and discount > quantity * unit_cost:
            self.add_error('discount_amount', 'Discount cannot exceed the line amount.')
        return cleaned


def purchase_order_line_formset(*, organization, **kwargs):
    formset_class = inlineformset_factory(
        PurchaseOrder,
        PurchaseOrderLine,
        form=PurchaseOrderLineForm,
        extra=3,
        min_num=1,
        validate_min=True,
        can_delete=True,
    )

    class OrganizationPurchaseOrderLineFormSet(formset_class):
        def get_form_kwargs(self, index):
            form_kwargs = super().get_form_kwargs(index)
            form_kwargs['organization'] = organization
            return form_kwargs

    return OrganizationPurchaseOrderLineFormSet(**kwargs)


class GoodsReceiptForm(forms.Form):
    receipt_number = forms.CharField(max_length=50, widget=forms.TextInput(attrs={'class': 'form-control'}))
    receipt_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}))
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), widget=forms.Select(attrs={'class': 'form-control'}))

    def __init__(self, *args, order, organization, receipt=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order
        self.receipt = receipt
        self.order_lines = list(order.lines.select_related('product').order_by('id'))
        if receipt:
            self.fields['receipt_number'].initial = receipt.receipt_number
            self.fields['receipt_date'].initial = receipt.receipt_date
            self.fields['warehouse'].initial = receipt.warehouse_id
        self.fields['warehouse'].queryset = Warehouse.objects.filter(
            organization=organization,
            branch=order.branch,
            is_active=True,
        ).order_by('name')
        for line in self.order_lines:
            received = GoodsReceiptLine.objects.filter(
                order_line=line,
                goods_receipt__status='posted',
            ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            remaining = max(Decimal('0'), line.quantity - received)
            self.fields[f'quantity_{line.pk}'] = forms.DecimalField(
                max_digits=18,
                decimal_places=3,
                required=False,
                min_value=Decimal('0'),
                max_value=remaining,
                label=f'{line.product.sku} — {line.product.name} (outstanding {remaining})',
                widget=forms.NumberInput(attrs={'step': '0.001', 'min': '0', 'max': str(remaining), 'class': 'form-control'}),
            )
            if receipt:
                self.fields[f'quantity_{line.pk}'].initial = receipt.lines.filter(
                    order_line=line
                ).values_list('quantity', flat=True).first()

    def clean_receipt_number(self):
        number = self.cleaned_data['receipt_number'].strip()
        from .models import GoodsReceipt

        duplicate = GoodsReceipt.objects.filter(receipt_number=number)
        if self.receipt:
            duplicate = duplicate.exclude(pk=self.receipt.pk)
        if duplicate.exists():
            raise forms.ValidationError('This goods receipt number is already in use.')
        return number

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('receipt_date') and cleaned['receipt_date'] < self.order.order_date:
            self.add_error('receipt_date', 'Receipt date cannot be earlier than the purchase order date.')
        if not any(cleaned.get(f'quantity_{line.pk}') for line in self.order_lines):
            raise forms.ValidationError('Enter a positive quantity for at least one order line.')
        return cleaned


class ReceiptInvoiceForm(forms.Form):
    invoice_number = forms.CharField(max_length=50, widget=forms.TextInput(attrs={'class': 'form-control'}))
    invoice_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}))
    due_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    place_of_supply = forms.CharField(required=False, max_length=2, widget=forms.TextInput(attrs={'class': 'form-control'}))

    def __init__(self, *args, receipt, **kwargs):
        super().__init__(*args, **kwargs)
        self.receipt = receipt
        self.receipt_lines = list(
            receipt.lines.select_related('order_line__product').order_by('id')
        )
        for line in self.receipt_lines:
            invoiced = PurchaseInvoiceLine.objects.filter(
                receipt_line=line,
                invoice__status='posted',
            ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            remaining = max(Decimal('0'), line.quantity - invoiced)
            prefix = f'line_{line.pk}'
            self.fields[f'{prefix}_quantity'] = forms.DecimalField(
                max_digits=18,
                decimal_places=3,
                required=False,
                min_value=Decimal('0'),
                max_value=remaining,
                label=f'{line.order_line.product.sku} — {line.order_line.product.name} (uninvoiced {remaining})',
                widget=forms.NumberInput(attrs={'step': '0.001', 'min': '0', 'max': str(remaining), 'class': 'form-control'}),
            )
            self.fields[f'{prefix}_unit_cost'] = forms.DecimalField(
                max_digits=18,
                decimal_places=2,
                required=False,
                min_value=Decimal('0'),
                initial=line.order_line.unit_cost,
                label='Invoice unit cost',
                widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
            )
            self.fields[f'{prefix}_discount'] = forms.DecimalField(
                max_digits=18,
                decimal_places=2,
                required=False,
                min_value=Decimal('0'),
                initial=Decimal('0'),
                label='Discount',
                widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
            )

    def clean_invoice_number(self):
        number = self.cleaned_data['invoice_number'].strip()
        if PurchaseInvoice.objects.filter(invoice_number=number).exists():
            raise forms.ValidationError('This invoice number is already in use.')
        return number

    def clean(self):
        cleaned = super().clean()
        selected = 0
        for line in self.receipt_lines:
            prefix = f'line_{line.pk}'
            quantity = cleaned.get(f'{prefix}_quantity') or Decimal('0')
            unit_cost = cleaned.get(f'{prefix}_unit_cost')
            discount = cleaned.get(f'{prefix}_discount') or Decimal('0')
            if quantity:
                selected += 1
                if unit_cost is None:
                    self.add_error(f'{prefix}_unit_cost', 'Enter the supplier invoice unit cost.')
                elif discount >= round_currency(quantity * unit_cost):
                    self.add_error(f'{prefix}_discount', 'Discount must be less than the invoice line amount.')
        if not selected:
            raise forms.ValidationError('Enter a positive quantity for at least one receipt line.')
        return cleaned


class PurchaseInvoiceForm(forms.ModelForm):
    class Meta:
        model = PurchaseInvoice
        fields = ['supplier', 'branch', 'invoice_number', 'invoice_date', 'due_date', 'place_of_supply']
        widgets = {
            'invoice_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'due_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        }

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.user = user
        self.fields['supplier'].queryset = Supplier.objects.filter(organization=organization)
        branches = Branch.objects.filter(organization=organization, is_active=True)
        if user.branch_id:
            branches = branches.filter(pk=user.branch_id)
            self.fields['branch'].initial = user.branch
            self.fields['branch'].widget = forms.HiddenInput()
        self.fields['branch'].queryset = branches
        self.fields['branch'].required = False

    def clean(self):
        cleaned = super().clean()
        branch = cleaned.get('branch')
        if self.user.branch_id:
            cleaned['branch'] = self.user.branch
        elif branch and branch.organization_id != self.organization.pk:
            self.add_error('branch', 'Select a branch belonging to this organization.')
        return cleaned


class PurchaseInvoiceLineForm(forms.ModelForm):
    class Meta:
        model = PurchaseInvoiceLine
        fields = ['product', 'quantity', 'unit_cost', 'discount_amount']
        widgets = {
            'quantity': forms.NumberInput(attrs={'step': '0.001', 'min': '0.001', 'class': 'form-control'}),
            'unit_cost': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
            'discount_amount': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
        self.fields['product'].queryset = Product.objects.filter(
            organization=organization,
            is_active=True,
        ).order_by('name')

    def clean(self):
        cleaned = super().clean()
        quantity = cleaned.get('quantity')
        cost = cleaned.get('unit_cost')
        discount = cleaned.get('discount_amount') or Decimal('0')
        cleaned['discount_amount'] = discount
        if not cleaned.get('product'):
            if quantity is not None or cost is not None or discount:
                self.add_error('product', 'Select a product for this invoice line.')
            return cleaned
        if quantity is None:
            self.add_error('quantity', 'Enter a quantity.')
        if cost is None:
            self.add_error('unit_cost', 'Enter a unit cost.')
        if quantity is not None and quantity <= 0:
            self.add_error('quantity', 'Quantity must be greater than zero.')
        if cost is not None and cost < 0:
            self.add_error('unit_cost', 'Unit cost cannot be negative.')
        if quantity is not None and cost is not None and discount > quantity * cost:
            self.add_error('discount_amount', 'Discount cannot exceed the line amount.')
        return cleaned


def purchase_line_formset(*, organization, **kwargs):
    formset_class = inlineformset_factory(
        PurchaseInvoice,
        PurchaseInvoiceLine,
        form=PurchaseInvoiceLineForm,
        extra=3,
        min_num=1,
        validate_min=True,
        can_delete=True,
    )

    class OrganizationPurchaseLineFormSet(formset_class):
        def get_form_kwargs(self, index):
            form_kwargs = super().get_form_kwargs(index)
            form_kwargs['organization'] = organization
            return form_kwargs

    return OrganizationPurchaseLineFormSet(**kwargs)


def calculate_purchase_subtotal(formset):
    subtotal = Decimal('0')
    for form in formset.forms:
        if not form.cleaned_data or form.cleaned_data.get('DELETE'):
            continue
        data = form.cleaned_data
        if not data.get('product'):
            continue
        subtotal += round_currency(data['quantity'] * data['unit_cost']) - (
            data.get('discount_amount') or Decimal('0')
        )
    return round_currency(subtotal)

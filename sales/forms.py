from decimal import Decimal

from django import forms
from django.db.models import Sum
from django.forms import inlineformset_factory

from accounting.models import round_currency
from inventory.models import Product, Warehouse
from organizations.models import Branch

from .models import (
    Customer,
    DeliveryNote,
    SalesInvoice,
    SalesInvoiceLine,
    SalesOrder,
    SalesOrderLine,
)


class SalesInvoiceForm(forms.ModelForm):
    class Meta:
        model = SalesInvoice
        fields = ['customer', 'branch', 'invoice_number', 'invoice_date', 'due_date', 'place_of_supply']
        widgets = {
            'invoice_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'due_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        }

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.user = user
        self.fields['customer'].queryset = Customer.objects.filter(organization=organization)
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


class SalesInvoiceLineForm(forms.ModelForm):
    class Meta:
        model = SalesInvoiceLine
        fields = ['product', 'quantity', 'unit_price', 'discount_amount']
        widgets = {
            'quantity': forms.NumberInput(attrs={'step': '0.001', 'min': '0.001', 'class': 'form-control'}),
            'unit_price': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
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
        price = cleaned.get('unit_price')
        discount = cleaned.get('discount_amount') or Decimal('0')
        if not cleaned.get('product'):
            if quantity is not None or price is not None or discount:
                self.add_error('product', 'Select a product for this invoice line.')
            return cleaned
        if quantity is None:
            self.add_error('quantity', 'Enter a quantity.')
        if price is None:
            self.add_error('unit_price', 'Enter a unit price.')
        if quantity is not None and quantity <= 0:
            self.add_error('quantity', 'Quantity must be greater than zero.')
        if price is not None and price < 0:
            self.add_error('unit_price', 'Unit price cannot be negative.')
        if quantity is not None and price is not None and discount > quantity * price:
            self.add_error('discount_amount', 'Discount cannot exceed the line amount.')
        return cleaned


def sales_line_formset(*, organization, **kwargs):
    formset_class = inlineformset_factory(
        SalesInvoice,
        SalesInvoiceLine,
        form=SalesInvoiceLineForm,
        extra=3,
        min_num=1,
        validate_min=True,
        can_delete=True,
    )

    class OrganizationSalesLineFormSet(formset_class):
        def get_form_kwargs(self, index):
            form_kwargs = super().get_form_kwargs(index)
            form_kwargs['organization'] = organization
            return form_kwargs

    return OrganizationSalesLineFormSet(**kwargs)


def calculate_sales_subtotal(formset):
    subtotal = Decimal('0')
    for form in formset.forms:
        if not form.cleaned_data or form.cleaned_data.get('DELETE'):
            continue
        data = form.cleaned_data
        if not data.get('product'):
            continue
        subtotal += round_currency(data['quantity'] * data['unit_price']) - (
            data.get('discount_amount') or Decimal('0')
        )
    return round_currency(subtotal)


class SalesOrderForm(forms.ModelForm):
    class Meta:
        model = SalesOrder
        fields = ['customer', 'branch', 'order_number', 'order_date']
        widgets = {'order_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'})}

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.user = user
        self.fields['customer'].queryset = Customer.objects.filter(organization=organization)
        branches = Branch.objects.filter(organization=organization, is_active=True)
        if user.branch_id:
            branches = branches.filter(pk=user.branch_id)
            self.fields['branch'].initial = user.branch
            self.fields['branch'].widget = forms.HiddenInput()
        self.fields['branch'].queryset = branches
        self.fields['branch'].required = True

    def clean(self):
        cleaned = super().clean()
        if self.user.branch_id:
            cleaned['branch'] = self.user.branch
        return cleaned


class SalesOrderLineForm(forms.ModelForm):
    class Meta:
        model = SalesOrderLine
        fields = ['product', 'quantity', 'unit_price', 'discount_amount']
        widgets = {
            'quantity': forms.NumberInput(attrs={'step': '0.001', 'min': '0.001', 'class': 'form-control'}),
            'unit_price': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
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
        price = cleaned.get('unit_price')
        discount = cleaned.get('discount_amount') or Decimal('0')
        if not product:
            if quantity is not None or price is not None or discount:
                self.add_error('product', 'Select a product for this order line.')
            return cleaned
        if quantity is None or quantity <= 0:
            self.add_error('quantity', 'Quantity must be greater than zero.')
        if price is None or price < 0:
            self.add_error('unit_price', 'Enter a non-negative unit price.')
        if quantity is not None and price is not None and discount > quantity * price:
            self.add_error('discount_amount', 'Discount cannot exceed the line amount.')
        return cleaned


def sales_order_line_formset(*, organization, **kwargs):
    formset_class = inlineformset_factory(
        SalesOrder,
        SalesOrderLine,
        form=SalesOrderLineForm,
        extra=3,
        min_num=1,
        validate_min=True,
        can_delete=True,
    )

    class OrganizationSalesOrderLineFormSet(formset_class):
        def get_form_kwargs(self, index):
            form_kwargs = super().get_form_kwargs(index)
            form_kwargs['organization'] = organization
            return form_kwargs

    return OrganizationSalesOrderLineFormSet(**kwargs)


class DeliveryNoteForm(forms.Form):
    delivery_number = forms.CharField(max_length=50)
    delivery_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}))
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none())

    def __init__(self, *args, order, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order
        self.order_lines = list(order.lines.select_related('product'))
        self.fields['delivery_number'].widget.attrs.update({'class': 'form-control'})
        self.fields['delivery_date'].widget.attrs.update({'class': 'form-control'})
        self.fields['warehouse'].queryset = Warehouse.objects.filter(
            organization=organization,
            branch=order.branch,
            is_active=True,
        ).order_by('name')
        self.fields['warehouse'].widget.attrs.update({'class': 'form-control'})
        for line in self.order_lines:
            self.fields[f'quantity_{line.pk}'] = forms.DecimalField(
                max_digits=18,
                decimal_places=3,
                required=False,
                min_value=Decimal('0'),
                label=f'{line.product.sku} — {line.product.name} (ordered {line.quantity})',
                widget=forms.NumberInput(attrs={'step': '0.001', 'min': '0', 'class': 'form-control'}),
            )

    def clean(self):
        cleaned = super().clean()
        picked = 0
        for line in self.order_lines:
            qty = cleaned.get(f'quantity_{line.pk}') or Decimal('0')
            delivered = line.delivery_lines.filter(
                delivery_note__status='dispatched',
            ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            if qty and delivered + qty > line.quantity:
                self.add_error(f'quantity_{line.pk}', 'Quantity exceeds the undelivered order quantity.')
            if qty:
                picked += 1
        if not picked:
            raise forms.ValidationError('Enter a positive quantity for at least one order line.')
        return cleaned

    def clean_delivery_number(self):
        number = self.cleaned_data['delivery_number']
        if DeliveryNote.objects.filter(delivery_number=number).exists():
            raise forms.ValidationError('This delivery number is already in use.')
        return number


class DeliveryInvoiceForm(forms.Form):
    invoice_number = forms.CharField(max_length=50)
    invoice_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}))
    due_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    place_of_supply = forms.CharField(required=False, max_length=2)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'

    def clean_invoice_number(self):
        number = self.cleaned_data['invoice_number']
        if SalesInvoice.objects.filter(invoice_number=number).exists():
            raise forms.ValidationError('This invoice number is already in use.')
        return number

from decimal import Decimal

from django import forms
from django.db.models import Sum

from inventory.models import Warehouse
from purchases.models import GoodsReceipt, GoodsReceiptLine, PurchaseInvoice, PurchaseInvoiceLine
from sales.models import SalesInvoice

from .models import CustomerReturn, CustomerReturnLine, SupplierReturn, SupplierReturnLine


class ReturnHeaderForm(forms.Form):
    return_number = forms.CharField(max_length=50)
    return_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}))
    reference = forms.CharField(max_length=80, required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}))

    def __init__(self, *args, record=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.record = record
        if record:
            for field in ('return_number', 'return_date', 'reference', 'notes'):
                self.fields[field].initial = getattr(record, field)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')


class CustomerReturnForm(ReturnHeaderForm):
    def __init__(self, *args, invoice, organization, user, record=None, **kwargs):
        super().__init__(*args, record=record, **kwargs)
        self.invoice = invoice
        self.organization = organization
        self.user = user
        self.invoice_lines = list(invoice.lines.select_related('product').order_by('pk'))
        warehouses = Warehouse.objects.filter(organization=organization, is_active=True)
        if invoice.branch_id:
            warehouses = warehouses.filter(branch_id=invoice.branch_id)
        if user.branch_id:
            warehouses = warehouses.filter(branch_id=user.branch_id)
        self.fields['warehouse'] = forms.ModelChoiceField(
            queryset=warehouses.order_by('name'), required=False, label='Restock warehouse'
        )
        if record and record.warehouse_id:
            self.fields['warehouse'].initial = record.warehouse_id
        for line in self.invoice_lines:
            returned = CustomerReturnLine.objects.filter(
                invoice_line=line,
                customer_return__status='posted',
            ).exclude(customer_return_id=record.pk if record else None).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            remaining = max(Decimal('0'), line.quantity - returned)
            key = f'quantity_{line.pk}'
            self.fields[key] = forms.DecimalField(
                max_digits=18,
                decimal_places=3,
                required=False,
                min_value=Decimal('0'),
                max_value=remaining,
                label=f'{line.product.sku} — {line.product.name} (remaining {remaining})',
                widget=forms.NumberInput(attrs={'step': '0.001', 'min': '0', 'max': str(remaining), 'class': 'form-control'}),
            )
            self.fields[f'reason_{line.pk}'] = forms.CharField(
                max_length=200, required=False, label='Return reason', widget=forms.TextInput(attrs={'class': 'form-control'})
            )
            self.fields[f'condition_{line.pk}'] = forms.ChoiceField(
                choices=[('', 'Select condition'), *CustomerReturn.CONDITION_CHOICES], required=False,
                label='Condition', widget=forms.Select(attrs={'class': 'form-control'}),
            )
            self.fields[f'restock_{line.pk}'] = forms.BooleanField(
                required=False, label='Restock as sellable stock',
                widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            )
            if record:
                old_line = record.lines.filter(invoice_line=line).first()
                if old_line:
                    self.fields[key].initial = old_line.quantity
                    self.fields[f'reason_{line.pk}'].initial = old_line.reason
                    self.fields[f'condition_{line.pk}'].initial = old_line.condition
                    self.fields[f'restock_{line.pk}'].initial = old_line.restock

    def clean_return_number(self):
        value = self.cleaned_data['return_number'].strip()
        duplicate = CustomerReturn.objects.filter(return_number=value)
        if self.record:
            duplicate = duplicate.exclude(pk=self.record.pk)
        if duplicate.exists():
            raise forms.ValidationError('This customer return number is already in use.')
        return value

    def clean(self):
        cleaned = super().clean()
        if self.invoice.status != 'posted':
            raise forms.ValidationError('Only posted sales invoices can be returned.')
        picked = False
        restock = False
        for line in self.invoice_lines:
            quantity = cleaned.get(f'quantity_{line.pk}') or Decimal('0')
            if quantity:
                picked = True
                if not cleaned.get(f'reason_{line.pk}'):
                    self.add_error(f'reason_{line.pk}', 'Enter a return reason.')
                if not cleaned.get(f'condition_{line.pk}'):
                    self.add_error(f'condition_{line.pk}', 'Select the returned item condition.')
                restock = restock or bool(cleaned.get(f'restock_{line.pk}'))
        if not picked:
            raise forms.ValidationError('Enter a positive quantity for at least one invoice line.')
        if restock and not cleaned.get('warehouse'):
            self.add_error('warehouse', 'Choose a warehouse for sellable returned goods.')
        if cleaned.get('return_date') and cleaned['return_date'] < self.invoice.invoice_date:
            self.add_error('return_date', 'Return date cannot be earlier than the invoice date.')
        return cleaned

    def save_lines(self, record):
        record.lines.all().delete()
        for line in self.invoice_lines:
            quantity = self.cleaned_data.get(f'quantity_{line.pk}') or Decimal('0')
            if not quantity:
                continue
            CustomerReturnLine.objects.create(
                customer_return=record,
                invoice_line=line,
                quantity=quantity,
                reason=self.cleaned_data[f'reason_{line.pk}'].strip(),
                condition=self.cleaned_data[f'condition_{line.pk}'],
                restock=bool(self.cleaned_data.get(f'restock_{line.pk}')),
            )


class SupplierReturnForm(ReturnHeaderForm):
    def __init__(self, *args, source, organization, user, record=None, **kwargs):
        super().__init__(*args, record=record, **kwargs)
        self.source = source
        self.organization = organization
        self.user = user
        self.receipt = source if isinstance(source, GoodsReceipt) else None
        self.invoice = source if isinstance(source, PurchaseInvoice) else None
        self.return_lines = self._eligible_lines(record)
        if self.receipt:
            warehouse_ids = [self.receipt.warehouse_id]
            self.fields['warehouse'] = forms.ModelChoiceField(
                queryset=Warehouse.objects.filter(pk__in=warehouse_ids, organization=organization, is_active=True),
                initial=self.receipt.warehouse_id,
                widget=forms.HiddenInput(),
            )
        else:
            linked_warehouses = {
                line.receipt_line.goods_receipt.warehouse_id
                for line in self.invoice.lines.select_related('receipt_line__goods_receipt')
                if line.receipt_line_id
            }
            if len(linked_warehouses) == 1:
                warehouse_id = next(iter(linked_warehouses))
            else:
                warehouses = Warehouse.objects.filter(organization=organization, is_active=True)
                if self.invoice.branch_id:
                    warehouses = warehouses.filter(branch=self.invoice.branch)
                warehouse = warehouses.order_by('pk').first()
                warehouse_id = warehouse.pk if warehouse else None
            if self.invoice.branch_id:
                warehouses = Warehouse.objects.filter(
                    organization=organization,
                    branch=self.invoice.branch,
                    is_active=True,
                )
            else:
                warehouses = Warehouse.objects.filter(organization=organization, is_active=True)
            if warehouse_id:
                warehouses = warehouses.filter(pk=warehouse_id)
            if user.branch_id:
                warehouses = warehouses.filter(branch_id=user.branch_id)
            self.fields['warehouse'] = forms.ModelChoiceField(
                queryset=warehouses, required=True, initial=warehouse_id,
                widget=forms.HiddenInput() if warehouse_id else forms.Select(attrs={'class': 'form-control'}),
            )
        for source_line, product, remaining, invoice_line, receipt_line in self.return_lines:
            key = f'quantity_{source_line.pk}'
            self.fields[key] = forms.DecimalField(
                max_digits=18,
                decimal_places=3,
                required=False,
                min_value=Decimal('0'),
                max_value=remaining,
                label=f'{product.sku} — {product.name} (remaining {remaining})',
                widget=forms.NumberInput(attrs={'step': '0.001', 'min': '0', 'max': str(remaining), 'class': 'form-control'}),
            )
            self.fields[f'reason_{source_line.pk}'] = forms.CharField(
                max_length=200, required=False, label='Return reason', widget=forms.TextInput(attrs={'class': 'form-control'})
            )
            self.fields[f'condition_{source_line.pk}'] = forms.ChoiceField(
                choices=[('', 'Select condition'), *CustomerReturn.CONDITION_CHOICES], required=False,
                label='Condition', widget=forms.Select(attrs={'class': 'form-control'}),
            )
            if record:
                if self.receipt:
                    old_line = record.lines.filter(receipt_line=source_line, invoice_line__isnull=True).first()
                else:
                    old_line = record.lines.filter(invoice_line=source_line).first()
                if old_line:
                    self.fields[key].initial = old_line.quantity
                    self.fields[f'reason_{source_line.pk}'].initial = old_line.reason
                    self.fields[f'condition_{source_line.pk}'].initial = old_line.condition

    def _eligible_lines(self, record):
        rows = []
        if self.receipt:
            receipt_lines = self.receipt.lines.select_related('order_line__product').order_by('pk')
            for source_line in receipt_lines:
                billed = PurchaseInvoiceLine.objects.filter(
                    receipt_line=source_line, invoice__status='posted'
                ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
                returned = SupplierReturnLine.objects.filter(
                    receipt_line=source_line, supplier_return__status='posted'
                ).exclude(supplier_return_id=record.pk if record else None).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
                remaining = max(Decimal('0'), source_line.quantity - billed - returned)
                rows.append((source_line, source_line.order_line.product, remaining, None, source_line))
        else:
            for source_line in self.invoice.lines.select_related('product', 'receipt_line__goods_receipt').order_by('pk'):
                returned = SupplierReturnLine.objects.filter(
                    invoice_line=source_line, supplier_return__status='posted'
                ).exclude(supplier_return_id=record.pk if record else None).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
                remaining = max(Decimal('0'), source_line.quantity - returned)
                receipt_line = source_line.receipt_line
                if receipt_line:
                    receipt_returned = SupplierReturnLine.objects.filter(
                        receipt_line=receipt_line, supplier_return__status='posted'
                    ).exclude(supplier_return_id=record.pk if record else None).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
                    remaining = min(remaining, max(Decimal('0'), receipt_line.quantity - receipt_returned))
                rows.append((source_line, source_line.product, remaining, source_line, receipt_line))
        return rows

    def clean_return_number(self):
        value = self.cleaned_data['return_number'].strip()
        duplicate = SupplierReturn.objects.filter(return_number=value)
        if self.record:
            duplicate = duplicate.exclude(pk=self.record.pk)
        if duplicate.exists():
            raise forms.ValidationError('This supplier return number is already in use.')
        return value

    def clean(self):
        cleaned = super().clean()
        if self.receipt and self.receipt.status != 'posted':
            raise forms.ValidationError('Only posted goods receipts can be returned.')
        if self.invoice and self.invoice.status != 'posted':
            raise forms.ValidationError('Only posted supplier invoices can be returned.')
        picked = False
        for source_line, _product, _remaining, _invoice_line, _receipt_line in self.return_lines:
            quantity = cleaned.get(f'quantity_{source_line.pk}') or Decimal('0')
            if quantity:
                picked = True
                if not cleaned.get(f'reason_{source_line.pk}'):
                    self.add_error(f'reason_{source_line.pk}', 'Enter a return reason.')
                if not cleaned.get(f'condition_{source_line.pk}'):
                    self.add_error(f'condition_{source_line.pk}', 'Select the returned item condition.')
        if not picked:
            raise forms.ValidationError('Enter a positive quantity for at least one eligible source line.')
        source_date = self.receipt.receipt_date if self.receipt else self.invoice.invoice_date
        if cleaned.get('return_date') and cleaned['return_date'] < source_date:
            self.add_error('return_date', 'Return date cannot be earlier than the source document date.')
        return cleaned

    def save_lines(self, record):
        record.lines.all().delete()
        for source_line, _product, _remaining, invoice_line, receipt_line in self.return_lines:
            quantity = self.cleaned_data.get(f'quantity_{source_line.pk}') or Decimal('0')
            if not quantity:
                continue
            SupplierReturnLine.objects.create(
                supplier_return=record,
                receipt_line=receipt_line,
                invoice_line=invoice_line,
                quantity=quantity,
                reason=self.cleaned_data[f'reason_{source_line.pk}'].strip(),
                condition=self.cleaned_data[f'condition_{source_line.pk}'],
            )

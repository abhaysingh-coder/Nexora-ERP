from decimal import Decimal

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import models, transaction

from accounting.models import JournalEntry
from inventory.models import Warehouse
from organizations.models import Branch, Organization


class ProtectedReturnQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.exclude(status='draft').exists() or kwargs.get('status') not in {None, 'draft'}:
            raise ValidationError('Posted or cancelled returns cannot be changed in bulk.')
        return super().update(**kwargs)

    def delete(self):
        if self.exclude(status='draft').exists():
            raise ValidationError('Posted or cancelled returns cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if 'status' in fields and any(obj.status != 'draft' for obj in objects):
            raise ValidationError('Use the return workflow to change return status.')
        if self.exclude(status='draft').filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('Posted or cancelled returns cannot be changed in bulk.')
        return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        if any(obj.status != 'draft' for obj in objects):
            raise ValidationError('Returns must be created as drafts.')
        for obj in objects:
            obj.full_clean()
        return super().bulk_create(objects, **kwargs)


class ProtectedNoteQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.filter(status='posted').exists() or kwargs.get('status') == 'posted':
            raise ValidationError('Posted credit and debit notes cannot be changed in bulk.')
        return super().update(**kwargs)

    def delete(self):
        if self.filter(status='posted').exists():
            raise ValidationError('Posted credit and debit notes cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self.filter(pk__in=[obj.pk for obj in objects], status='posted').exists():
            raise ValidationError('Posted credit and debit notes cannot be changed in bulk.')
        if 'status' in fields and any(obj.status == 'posted' for obj in objects):
            raise ValidationError('Credit and debit notes are posted through their return workflows.')
        return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        if any(obj.status != 'draft' for obj in objects):
            raise ValidationError('Credit and debit notes must be created as drafts.')
        for obj in objects:
            obj.full_clean()
        return super().bulk_create(objects, **kwargs)


class ProtectedNoteLineQuerySet(models.QuerySet):
    parent_field = None
    parent_model = None

    def _posted_parents(self, ids):
        model = apps.get_model('returns', self.parent_model)
        return model.objects.filter(pk__in=ids, status='posted')

    def _locked_parents(self, ids):
        model = apps.get_model('returns', self.parent_model)
        return list(model.objects.select_for_update().filter(pk__in=ids).order_by('pk'))

    def update(self, **kwargs):
        parent_name = self.parent_field.removesuffix('_id')
        if parent_name in kwargs or self.parent_field in kwargs:
            raise ValidationError('Note lines cannot be moved between credit or debit notes.')
        with transaction.atomic():
            ids = self.values_list(self.parent_field, flat=True).distinct()
            if any(parent.status == 'posted' for parent in self._locked_parents(ids)):
                raise ValidationError('Lines on posted credit and debit notes cannot be changed.')
            return super().update(**kwargs)

    def delete(self):
        with transaction.atomic():
            ids = self.values_list(self.parent_field, flat=True).distinct()
            if any(parent.status == 'posted' for parent in self._locked_parents(ids)):
                raise ValidationError('Lines on posted credit and debit notes cannot be deleted.')
            return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        parent_name = self.parent_field.removesuffix('_id')
        if parent_name in fields or self.parent_field in fields:
            raise ValidationError('Note lines cannot be moved between credit or debit notes.')
        with transaction.atomic():
            ids = self.filter(pk__in=[obj.pk for obj in objects]).values_list(self.parent_field, flat=True).distinct()
            if any(parent.status == 'posted' for parent in self._locked_parents(ids)):
                raise ValidationError('Lines on posted credit and debit notes cannot be changed.')
            return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        ids = {getattr(obj, self.parent_field) for obj in objects}
        with transaction.atomic():
            if any(parent.status == 'posted' for parent in self._locked_parents(ids)):
                raise ValidationError('Lines cannot be added to posted credit and debit notes.')
            for obj in objects:
                obj.full_clean()
            return super().bulk_create(objects, **kwargs)


class SalesCreditNoteLineQuerySet(ProtectedNoteLineQuerySet):
    parent_field = 'credit_note_id'
    parent_model = 'SalesCreditNote'


class SupplierDebitNoteLineQuerySet(ProtectedNoteLineQuerySet):
    parent_field = 'debit_note_id'
    parent_model = 'SupplierDebitNote'


class ProtectedReturnLineQuerySet(models.QuerySet):
    return_field = None
    return_model = None
    reference_fields = ()

    def _locked_returns(self, ids):
        model = apps.get_model('returns', self.return_model)
        return list(model.objects.select_for_update().filter(pk__in=ids).order_by('pk'))

    def update(self, **kwargs):
        if any(field in kwargs for field in self.reference_fields):
            raise ValidationError('Return source links cannot be changed in bulk.')
        with transaction.atomic():
            ids = self.values_list(self.return_field, flat=True).distinct()
            if any(record.status != 'draft' for record in self._locked_returns(ids)):
                raise ValidationError('Lines on posted or cancelled returns cannot be changed.')
            return super().update(**kwargs)

    def delete(self):
        with transaction.atomic():
            ids = self.values_list(self.return_field, flat=True).distinct()
            if any(record.status != 'draft' for record in self._locked_returns(ids)):
                raise ValidationError('Lines on posted or cancelled returns cannot be deleted.')
            return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if any(field in fields for field in self.reference_fields):
            raise ValidationError('Return source links cannot be changed in bulk.')
        with transaction.atomic():
            ids = set(self.filter(pk__in=[obj.pk for obj in objects]).values_list(self.return_field, flat=True))
            ids.update(getattr(obj, self.return_field) for obj in objects)
            if any(record.status != 'draft' for record in self._locked_returns(ids)):
                raise ValidationError('Lines on posted or cancelled returns cannot be changed.')
            return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        ids = {getattr(obj, self.return_field) for obj in objects}
        with transaction.atomic():
            if any(record.status != 'draft' for record in self._locked_returns(ids)):
                raise ValidationError('Lines can only be added to draft returns.')
            for obj in objects:
                obj.full_clean()
            return super().bulk_create(objects, **kwargs)


class CustomerReturnLineQuerySet(ProtectedReturnLineQuerySet):
    return_field = 'customer_return_id'
    return_model = 'CustomerReturn'
    reference_fields = ('customer_return', 'customer_return_id', 'invoice_line', 'invoice_line_id')


class SupplierReturnLineQuerySet(ProtectedReturnLineQuerySet):
    return_field = 'supplier_return_id'
    return_model = 'SupplierReturn'
    reference_fields = ('supplier_return', 'supplier_return_id', 'receipt_line', 'receipt_line_id', 'invoice_line', 'invoice_line_id')


class CustomerReturn(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('posted', 'Posted'), ('cancelled', 'Cancelled')]
    CONDITION_CHOICES = [
        ('good', 'Good'),
        ('damaged', 'Damaged'),
        ('defective', 'Defective'),
        ('other', 'Other'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='customer_returns')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='customer_returns')
    invoice = models.ForeignKey('sales.SalesInvoice', on_delete=models.PROTECT, related_name='customer_returns')
    return_number = models.CharField(max_length=50, unique=True)
    return_date = models.DateField()
    reference = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, null=True, blank=True, related_name='customer_returns'
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    objects = ProtectedReturnQuerySet.as_manager()

    class Meta:
        ordering = ['-return_date', '-id']
        constraints = [models.UniqueConstraint(fields=['organization', 'return_number'], name='uniq_customer_return_org_number')]

    def clean(self):
        errors = {}
        if self.invoice_id:
            if self.invoice.organization_id != self.organization_id:
                errors['invoice'] = 'Invoice must belong to the return organization.'
            if self.branch_id != self.invoice.branch_id:
                errors['branch'] = 'Return branch must match the original invoice branch.'
        if self.branch_id and self.branch.organization_id != self.organization_id:
            errors['branch'] = 'Branch must belong to the return organization.'
        if self.warehouse_id:
            if self.warehouse.organization_id != self.organization_id:
                errors['warehouse'] = 'Warehouse must belong to the return organization.'
            elif self.branch_id and self.warehouse.branch_id != self.branch_id:
                errors['warehouse'] = 'Warehouse must belong to the return branch.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_for_update().get(pk=self.pk)
                if previous.status != 'draft':
                    raise ValidationError('Posted or cancelled returns cannot be changed.')
                if self.status != previous.status:
                    raise ValidationError('Use the return workflow to change return status.')
            elif self.status != 'draft':
                raise ValidationError('Returns must be created as drafts.')
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != 'draft':
            raise ValidationError('Posted or cancelled returns cannot be deleted.')
        return super().delete(*args, **kwargs)

    def post(self, *, user=None):
        from .services import post_customer_return

        posted = post_customer_return(self.pk, user=user)
        self.status = posted.status
        self.posted_at = posted.posted_at
        return self

    def cancel(self, *, user=None):
        from core.models import AuditEvent

        with transaction.atomic():
            record = type(self).objects.select_for_update().get(pk=self.pk)
            if record.status == 'cancelled':
                self.status = record.status
                return self
            if record.status != 'draft':
                raise ValidationError('Only draft returns can be cancelled.')
            record.status = 'cancelled'
            models.Model.save(record, update_fields=['status'])
            AuditEvent.objects.create(
                user=user, organization=record.organization, action='customer_return_cancelled',
                details=f'Customer return {record.return_number} was cancelled.',
            )
            self.status = record.status
            return self

    def __str__(self):
        return self.return_number


class CustomerReturnLine(models.Model):
    customer_return = models.ForeignKey(CustomerReturn, on_delete=models.CASCADE, related_name='lines')
    invoice_line = models.ForeignKey('sales.SalesInvoiceLine', on_delete=models.PROTECT, related_name='return_lines')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    reason = models.CharField(max_length=200)
    condition = models.CharField(max_length=12, choices=CustomerReturn.CONDITION_CHOICES)
    restock = models.BooleanField(default=False)
    line_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    taxable_value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    cgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    sgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    igst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    utgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    cess = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_tax = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    credit_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    stock_cost = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    objects = CustomerReturnLineQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name='customer_return_line_qty_positive'),
            models.UniqueConstraint(fields=['customer_return', 'invoice_line'], name='uniq_customer_return_invoice_line'),
        ]

    def clean(self):
        if self.customer_return_id and self.invoice_line_id:
            if self.customer_return.invoice_id != self.invoice_line.invoice_id:
                raise ValidationError({'invoice_line': 'Return line must belong to the original invoice.'})

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_related('customer_return').get(pk=self.pk)
                if previous.customer_return_id != self.customer_return_id:
                    raise ValidationError('Customer return lines cannot be moved between returns.')
                if previous.customer_return.status != 'draft':
                    raise ValidationError('Lines on posted or cancelled returns cannot be changed.')
            record = CustomerReturn.objects.select_for_update().get(pk=self.customer_return_id)
            if record.status != 'draft':
                raise ValidationError('Lines on posted or cancelled returns cannot be changed.')
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.customer_return.status != 'draft':
            raise ValidationError('Lines on posted or cancelled returns cannot be deleted.')
        return super().delete(*args, **kwargs)


class SalesCreditNote(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('posted', 'Posted')]

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='sales_credit_notes')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='sales_credit_notes')
    customer = models.ForeignKey('sales.Customer', on_delete=models.PROTECT, related_name='credit_notes')
    original_invoice = models.ForeignKey('sales.SalesInvoice', on_delete=models.PROTECT, related_name='credit_notes')
    customer_return = models.OneToOneField(CustomerReturn, on_delete=models.PROTECT, related_name='credit_note')
    credit_note_number = models.CharField(max_length=60, unique=True)
    credit_date = models.DateField()
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, null=True, blank=True, related_name='sales_credit_note')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-credit_date', '-id']

    objects = ProtectedNoteQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if previous.status == 'posted':
                raise ValidationError('Posted credit notes cannot be changed.')
            if self.status != previous.status:
                raise ValidationError('Credit notes are posted by their customer return.')
        elif self.status != 'draft':
            raise ValidationError('Credit notes must be created as drafts.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == 'posted':
            raise ValidationError('Posted credit notes cannot be deleted.')
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.credit_note_number


class SalesCreditNoteLine(models.Model):
    credit_note = models.ForeignKey(SalesCreditNote, on_delete=models.PROTECT, related_name='lines')
    return_line = models.OneToOneField(CustomerReturnLine, on_delete=models.PROTECT, related_name='credit_note_line')
    invoice_line = models.ForeignKey('sales.SalesInvoiceLine', on_delete=models.PROTECT, related_name='credit_note_lines')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    line_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    taxable_value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    cgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    sgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    igst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    utgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    cess = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_tax = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0), name='credit_note_line_qty_positive')]

    objects = SalesCreditNoteLineQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.credit_note_id:
            if self.pk:
                previous = type(self).objects.select_related('credit_note').get(pk=self.pk)
                if previous.credit_note_id != self.credit_note_id:
                    raise ValidationError('Credit note lines cannot be moved between notes.')
                if previous.credit_note.status == 'posted':
                    raise ValidationError('Posted credit note lines cannot be changed.')
            if self.credit_note.status == 'posted':
                raise ValidationError('Posted credit note lines cannot be changed.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.credit_note.status == 'posted':
            raise ValidationError('Posted credit note lines cannot be deleted.')
        return super().delete(*args, **kwargs)


class SupplierReturn(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('posted', 'Posted'), ('cancelled', 'Cancelled')]

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='supplier_returns')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='supplier_returns')
    supplier = models.ForeignKey('purchases.Supplier', on_delete=models.PROTECT, related_name='returns')
    goods_receipt = models.ForeignKey('purchases.GoodsReceipt', on_delete=models.PROTECT, null=True, blank=True, related_name='supplier_returns')
    purchase_invoice = models.ForeignKey('purchases.PurchaseInvoice', on_delete=models.PROTECT, null=True, blank=True, related_name='supplier_returns')
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name='supplier_returns')
    return_number = models.CharField(max_length=50, unique=True)
    return_date = models.DateField()
    reference = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    objects = ProtectedReturnQuerySet.as_manager()

    class Meta:
        ordering = ['-return_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'return_number'], name='uniq_supplier_return_org_number'),
            models.CheckConstraint(
                condition=(models.Q(goods_receipt__isnull=False, purchase_invoice__isnull=True)
                           | models.Q(goods_receipt__isnull=True, purchase_invoice__isnull=False)),
                name='supplier_return_one_source',
            ),
        ]

    def clean(self):
        errors = {}
        if bool(self.goods_receipt_id) == bool(self.purchase_invoice_id):
            errors['goods_receipt'] = 'Choose exactly one posted goods receipt or supplier invoice.'
        source = self.goods_receipt if self.goods_receipt_id else self.purchase_invoice if self.purchase_invoice_id else None
        if source:
            if source.organization_id != self.organization_id:
                errors['goods_receipt' if self.goods_receipt_id else 'purchase_invoice'] = 'Source must belong to the return organization.'
            if source.branch_id and source.branch_id != self.branch_id:
                errors['branch'] = 'Return branch must match the source branch.'
            if self.goods_receipt_id and source.order.supplier_id != self.supplier_id:
                errors['supplier'] = 'Supplier must match the goods receipt.'
            if self.purchase_invoice_id and source.supplier_id != self.supplier_id:
                errors['supplier'] = 'Supplier must match the supplier invoice.'
        if self.branch_id and self.branch.organization_id != self.organization_id:
            errors['branch'] = 'Branch must belong to the return organization.'
        if self.warehouse_id and (self.warehouse.organization_id != self.organization_id or self.warehouse.branch_id != self.branch_id):
            errors['warehouse'] = 'Warehouse must belong to the return branch and organization.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_for_update().get(pk=self.pk)
                if previous.status != 'draft':
                    raise ValidationError('Posted or cancelled returns cannot be changed.')
                if self.status != previous.status:
                    raise ValidationError('Use the return workflow to change return status.')
            elif self.status != 'draft':
                raise ValidationError('Returns must be created as drafts.')
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != 'draft':
            raise ValidationError('Posted or cancelled returns cannot be deleted.')
        return super().delete(*args, **kwargs)

    def post(self, *, user=None):
        from .services import post_supplier_return

        posted = post_supplier_return(self.pk, user=user)
        self.status = posted.status
        self.posted_at = posted.posted_at
        return self

    def cancel(self, *, user=None):
        from core.models import AuditEvent

        with transaction.atomic():
            record = type(self).objects.select_for_update().get(pk=self.pk)
            if record.status == 'cancelled':
                self.status = record.status
                return self
            if record.status != 'draft':
                raise ValidationError('Only draft returns can be cancelled.')
            record.status = 'cancelled'
            models.Model.save(record, update_fields=['status'])
            AuditEvent.objects.create(
                user=user, organization=record.organization, action='supplier_return_cancelled',
                details=f'Supplier return {record.return_number} was cancelled.',
            )
            self.status = record.status
            return self

    def __str__(self):
        return self.return_number


class SupplierReturnLine(models.Model):
    supplier_return = models.ForeignKey(SupplierReturn, on_delete=models.CASCADE, related_name='lines')
    receipt_line = models.ForeignKey('purchases.GoodsReceiptLine', on_delete=models.PROTECT, null=True, blank=True, related_name='supplier_return_lines')
    invoice_line = models.ForeignKey('purchases.PurchaseInvoiceLine', on_delete=models.PROTECT, null=True, blank=True, related_name='supplier_return_lines')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    reason = models.CharField(max_length=200)
    condition = models.CharField(max_length=12, choices=CustomerReturn.CONDITION_CHOICES)
    line_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    taxable_value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    cgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    sgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    igst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    utgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    cess = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_tax = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    debit_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    stock_cost = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    is_reverse_charge = models.BooleanField(default=False)
    is_input_tax_credit_eligible = models.BooleanField(default=True)

    objects = SupplierReturnLineQuerySet.as_manager()

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0), name='supplier_return_line_qty_positive')]

    def clean(self):
        if not self.supplier_return_id:
            return
        record = self.supplier_return
        if record.goods_receipt_id:
            if not self.receipt_line_id:
                raise ValidationError('Receipt-based returns require a goods receipt line.')
            if self.receipt_line.goods_receipt_id != record.goods_receipt_id:
                raise ValidationError({'receipt_line': 'Line must belong to the selected goods receipt.'})
            if self.invoice_line_id and (
                self.invoice_line.receipt_line_id != self.receipt_line_id
                or self.invoice_line.invoice.status != 'posted'
            ):
                raise ValidationError({'invoice_line': 'Invoice line must be posted and match the selected receipt line.'})
        elif record.purchase_invoice_id:
            if not self.invoice_line_id:
                raise ValidationError('Invoice-based returns require a supplier invoice line.')
            if self.invoice_line.invoice_id != record.purchase_invoice_id:
                raise ValidationError({'invoice_line': 'Line must belong to the selected supplier invoice.'})
            if self.receipt_line_id and self.invoice_line.receipt_line_id != self.receipt_line_id:
                raise ValidationError({'receipt_line': 'Receipt line must match the supplier invoice line.'})

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = type(self).objects.select_related('supplier_return').get(pk=self.pk)
                if previous.supplier_return_id != self.supplier_return_id:
                    raise ValidationError('Supplier return lines cannot be moved between returns.')
                if previous.supplier_return.status != 'draft':
                    raise ValidationError('Lines on posted or cancelled returns cannot be changed.')
            record = SupplierReturn.objects.select_for_update().get(pk=self.supplier_return_id)
            if record.status != 'draft':
                raise ValidationError('Lines on posted or cancelled returns cannot be changed.')
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.supplier_return.status != 'draft':
            raise ValidationError('Lines on posted or cancelled returns cannot be deleted.')
        return super().delete(*args, **kwargs)


class SupplierDebitNote(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('posted', 'Posted')]

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='supplier_debit_notes')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='supplier_debit_notes')
    supplier = models.ForeignKey('purchases.Supplier', on_delete=models.PROTECT, related_name='debit_notes')
    original_receipt = models.ForeignKey('purchases.GoodsReceipt', on_delete=models.PROTECT, null=True, blank=True, related_name='debit_notes')
    original_invoice = models.ForeignKey('purchases.PurchaseInvoice', on_delete=models.PROTECT, null=True, blank=True, related_name='debit_notes')
    supplier_return = models.OneToOneField(SupplierReturn, on_delete=models.PROTECT, related_name='debit_note')
    debit_note_number = models.CharField(max_length=60, unique=True)
    debit_date = models.DateField()
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, null=True, blank=True, related_name='supplier_debit_note')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-debit_date', '-id']

    objects = ProtectedNoteQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if previous.status == 'posted':
                raise ValidationError('Posted debit notes cannot be changed.')
            if self.status != previous.status:
                raise ValidationError('Debit notes are posted by their supplier return.')
        elif self.status != 'draft':
            raise ValidationError('Debit notes must be created as drafts.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == 'posted':
            raise ValidationError('Posted debit notes cannot be deleted.')
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.debit_note_number


class SupplierDebitNoteLine(models.Model):
    debit_note = models.ForeignKey(SupplierDebitNote, on_delete=models.PROTECT, related_name='lines')
    return_line = models.OneToOneField(SupplierReturnLine, on_delete=models.PROTECT, related_name='debit_note_line')
    invoice_line = models.ForeignKey('purchases.PurchaseInvoiceLine', on_delete=models.PROTECT, null=True, blank=True, related_name='debit_note_lines')
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    line_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    taxable_value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    cgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    sgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    igst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    utgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    cess = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_tax = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    is_reverse_charge = models.BooleanField(default=False)
    is_input_tax_credit_eligible = models.BooleanField(default=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0), name='debit_note_line_qty_positive')]

    objects = SupplierDebitNoteLineQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.debit_note_id:
            if self.pk:
                previous = type(self).objects.select_related('debit_note').get(pk=self.pk)
                if previous.debit_note_id != self.debit_note_id:
                    raise ValidationError('Debit note lines cannot be moved between notes.')
                if previous.debit_note.status == 'posted':
                    raise ValidationError('Posted debit note lines cannot be changed.')
            if self.debit_note.status == 'posted':
                raise ValidationError('Posted debit note lines cannot be changed.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.debit_note.status == 'posted':
            raise ValidationError('Posted debit note lines cannot be deleted.')
        return super().delete(*args, **kwargs)

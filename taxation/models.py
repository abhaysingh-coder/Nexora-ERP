import re
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from organizations.models import Branch, Organization


GSTIN_PATTERN = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$')
STATE_CODE_PATTERN = re.compile(r'^(0[1-9]|[12][0-9]|3[0-8])$')


def normalize_tax_amount(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def validate_gstin(value):
    if not value:
        return
    if not GSTIN_PATTERN.fullmatch(value.strip().upper()):
        raise ValidationError('GSTIN format is invalid.')


def validate_state_code(value):
    if value and not STATE_CODE_PATTERN.fullmatch(value.strip()):
        raise ValidationError('State code must be a valid two-digit Indian GST state code.')


class TaxCode(models.Model):
    CATEGORY_CHOICES = [
        ('goods', 'Goods'),
        ('services', 'Services'),
        ('exempt', 'Exempt'),
        ('zero_rated', 'Zero-rated'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='tax_codes')
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='goods')
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['code']
        unique_together = [('organization', 'code')]

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.code} - {self.name}'


class TaxRate(models.Model):
    TAX_TYPE_CHOICES = [
        ('cgst', 'CGST'),
        ('sgst', 'SGST'),
        ('igst', 'IGST'),
        ('utgst', 'UTGST'),
        ('cess', 'CESS'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='tax_rates')
    tax_code = models.ForeignKey(TaxCode, on_delete=models.CASCADE, related_name='rates')
    tax_type = models.CharField(max_length=20, choices=TAX_TYPE_CHOICES)
    rate = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal('0.0000'))
    state_code = models.CharField(max_length=10, blank=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-effective_from']
        constraints = [
            models.CheckConstraint(condition=Q(rate__gte=0, rate__lte=100), name='tax_rate_between_zero_and_100'),
            models.UniqueConstraint(
                fields=['organization', 'tax_code', 'tax_type', 'state_code', 'effective_from'],
                name='unique_tax_component_start_date',
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True) | Q(effective_to__gte=models.F('effective_from')),
                name='tax_rate_valid_effective_range',
            ),
        ]

    def clean(self):
        errors = {}
        if self.tax_code_id and self.organization_id and self.tax_code.organization_id != self.organization_id:
            errors['tax_code'] = 'Tax code must belong to the same organization.'
        try:
            validate_state_code(self.state_code)
        except ValidationError as exc:
            errors['state_code'] = exc.messages
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            errors['effective_to'] = 'End date cannot be before start date.'
        if self.is_active and self.organization_id and self.tax_code_id and self.effective_from:
            overlaps = TaxRate.objects.filter(
                organization_id=self.organization_id,
                tax_code_id=self.tax_code_id,
                tax_type=self.tax_type,
                state_code=self.state_code,
                is_active=True,
                effective_from__lte=self.effective_to or self.effective_from,
            ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=self.effective_from))
            if self.pk:
                overlaps = overlaps.exclude(pk=self.pk)
            if overlaps.exists():
                errors['effective_from'] = 'An active rate with an overlapping effective period already exists.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.tax_code.code} {self.tax_type} {self.rate}'


class TaxRegistration(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='tax_registrations')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='tax_registrations')
    gstin = models.CharField(max_length=20, blank=True)
    state_code = models.CharField(max_length=10, blank=True)
    is_registered = models.BooleanField(default=False)
    is_reverse_charge_applicable = models.BooleanField(default=False)

    class Meta:
        ordering = ['organization__name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'branch'],
                condition=Q(branch__isnull=False),
                name='unique_tax_registration_per_branch',
            ),
            models.UniqueConstraint(
                fields=['organization'],
                condition=Q(branch__isnull=True),
                name='unique_tax_registration_per_organization',
            ),
        ]

    def clean(self):
        errors = {}
        self.gstin = self.gstin.strip().upper()
        try:
            validate_gstin(self.gstin)
            validate_state_code(self.state_code)
        except ValidationError as exc:
            errors['gstin' if self.gstin else 'state_code'] = exc.messages
        if self.branch_id and self.organization_id and self.branch.organization_id != self.organization_id:
            errors['branch'] = 'Branch must belong to the same organization.'
        if self.is_registered and (not self.gstin or not self.state_code):
            errors['gstin'] = 'Registered businesses require a GSTIN and state code.'
        if self.gstin and self.state_code and self.gstin[:2] != self.state_code:
            errors['gstin'] = 'GSTIN state prefix must match the registration state code.'
        duplicates = TaxRegistration.objects.filter(
            organization_id=self.organization_id,
            branch_id=self.branch_id,
        )
        if self.pk:
            duplicates = duplicates.exclude(pk=self.pk)
        if self.organization_id and duplicates.exists():
            errors['branch'] = 'A registration already exists for this organization and branch.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.gstin or f'{self.organization.name} registration'


class ProductTaxClassification(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='product_tax_classifications')
    product = models.ForeignKey('inventory.Product', on_delete=models.CASCADE, related_name='tax_classifications')
    tax_code = models.ForeignKey(TaxCode, on_delete=models.PROTECT, related_name='product_classifications')
    hsn_sac = models.CharField(max_length=16, blank=True)
    tax_inclusive = models.BooleanField(default=False)
    is_exempt = models.BooleanField(default=False)
    is_zero_rated = models.BooleanField(default=False)
    is_reverse_charge = models.BooleanField(default=False)
    is_input_tax_credit_eligible = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = [('organization', 'product')]

    def clean(self):
        errors = {}
        if self.product_id and self.organization_id and self.product.organization_id != self.organization_id:
            errors['product'] = 'Product must belong to the same organization.'
        if self.tax_code_id and self.organization_id and self.tax_code.organization_id != self.organization_id:
            errors['tax_code'] = 'Tax code must belong to the same organization.'
        if self.is_exempt and self.is_zero_rated:
            errors['is_zero_rated'] = 'A classification cannot be both exempt and zero-rated.'
        if self.hsn_sac and (
            not self.hsn_sac.isdigit() or len(self.hsn_sac) not in {4, 6, 8}
        ):
            errors['hsn_sac'] = 'HSN/SAC must contain 4, 6, or 8 digits.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.product.name} -> {self.tax_code.code}'


class TaxTransaction(models.Model):
    TRANSACTION_KIND_CHOICES = [
        ('sale', 'Sale'),
        ('purchase', 'Purchase'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='tax_transactions')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='tax_transactions')
    transaction_kind = models.CharField(max_length=20, choices=TRANSACTION_KIND_CHOICES)
    transaction_date = models.DateField()
    source_model = models.CharField(max_length=80)
    source_id = models.PositiveIntegerField()
    source_line_id = models.PositiveIntegerField(null=True, blank=True)
    tax_code = models.ForeignKey(TaxCode, on_delete=models.PROTECT, related_name='transactions')
    hsn_sac = models.CharField(max_length=16, blank=True)
    origin_state = models.CharField(max_length=2, blank=True, default='')
    place_of_supply = models.CharField(max_length=2, blank=True, default='')
    line_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    is_inclusive = models.BooleanField(default=False)
    taxable_value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    tax_rate = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal('0.0000'))
    cgst_rate = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal('0.0000'))
    sgst_rate = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal('0.0000'))
    igst_rate = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal('0.0000'))
    utgst_rate = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal('0.0000'))
    cess_rate = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal('0.0000'))
    cgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    sgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    igst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    utgst = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    cess = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    total_tax = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    invoice_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    is_reverse_charge = models.BooleanField(default=False)
    is_zero_rated = models.BooleanField(default=False)
    is_exempt = models.BooleanField(default=False)
    is_input_tax_credit_eligible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'transaction_kind', 'source_model', 'source_id', 'source_line_id'],
                condition=Q(source_line_id__isnull=False),
                name='unique_tax_transaction_per_source_line',
            ),
            models.UniqueConstraint(
                fields=['organization', 'transaction_kind', 'source_model', 'source_id'],
                condition=Q(source_line_id__isnull=True),
                name='unique_tax_transaction_per_source_document',
            ),
        ]

    def __str__(self):
        return f'{self.source_model}-{self.source_id} {self.total_tax}'

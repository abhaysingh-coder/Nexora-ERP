import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from accounting.models import JournalEntry, JournalEntryLine, ensure_default_accounts
from organizations.models import Branch, Organization


class BankAccount(models.Model):
    ACCOUNT_TYPE_CHOICES = [('cash', 'Cash'), ('bank', 'Bank')]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='bank_accounts')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='bank_accounts')
    name = models.CharField(max_length=150)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default='bank')
    account_number = models.CharField(max_length=80, unique=True)
    opening_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    current_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.account_number})'

    def clean(self):
        if self.branch_id and self.organization_id and self.branch.organization_id != self.organization_id:
            raise ValidationError('Bank account branch must belong to its organization.')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class BankTransaction(models.Model):
    TRANSACTION_TYPE_CHOICES = [('receipt', 'Receipt'), ('payment', 'Payment'), ('transfer', 'Transfer')]
    STATUS_CHOICES = [('draft', 'Draft'), ('posted', 'Posted'), ('reconciled', 'Reconciled')]
    SETTLEMENT_MODE_CHOICES = [('legacy', 'Legacy receivable/payable'), ('advance', 'Advance account')]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='bank_transactions')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='bank_transactions')
    account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name='transactions')
    related_account = models.ForeignKey(BankAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name='transfer_transactions')
    customer = models.ForeignKey('sales.Customer', on_delete=models.SET_NULL, null=True, blank=True, related_name='bank_transactions')
    supplier = models.ForeignKey('purchases.Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='bank_transactions')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES)
    reference_number = models.CharField(max_length=80, unique=True)
    transaction_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    description = models.TextField(blank=True)
    payment_reference = models.CharField(max_length=120, blank=True)
    settlement_mode = models.CharField(
        max_length=20,
        choices=SETTLEMENT_MODE_CHOICES,
        default='advance',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    is_reconciled = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-transaction_date', '-id']

    def __str__(self):
        return f'{self.reference_number} - {self.amount}'

    def validate_allocation(self):
        if self.amount <= 0:
            raise ValidationError('Transaction amount must be greater than zero.')
        if self.transaction_type == 'receipt' and not self.customer:
            raise ValidationError('Customer receipt transactions require a customer.')
        if self.transaction_type == 'payment' and not self.supplier:
            raise ValidationError('Supplier payment transactions require a supplier.')
        if self.transaction_type == 'transfer' and not self.related_account:
            raise ValidationError('Transfer transactions require a destination account.')
        if self.transaction_type == 'transfer' and self.account == self.related_account:
            raise ValidationError('A transfer cannot target the same account.')
        accounts = [self.account]
        if self.related_account_id:
            accounts.append(self.related_account)
        if any(account.organization_id != self.organization_id for account in accounts):
            raise ValidationError('Bank transaction accounts must belong to its organization.')
        if any(not account.is_active for account in accounts):
            raise ValidationError('Bank transaction accounts must be active.')
        if any(
            account.branch_id and account.branch_id != self.branch_id
            for account in accounts
        ):
            raise ValidationError('Bank transaction account branch does not match the transaction branch.')
        if self.branch_id and self.branch.organization_id != self.organization_id:
            raise ValidationError('Bank transaction branch must belong to its organization.')
        if self.customer_id and self.customer.organization_id != self.organization_id:
            raise ValidationError('Customer must belong to the bank transaction organization.')
        if self.supplier_id and self.supplier.organization_id != self.organization_id:
            raise ValidationError('Supplier must belong to the bank transaction organization.')

    def post(self):
        with transaction.atomic():
            transaction_record = BankTransaction.objects.select_for_update().select_related(
                'account', 'related_account', 'customer', 'supplier', 'branch'
            ).get(pk=self.pk)
            if transaction_record.status in {'posted', 'reconciled'}:
                self.status = transaction_record.status
                return self

            transaction_record.validate_allocation()
            accounts_to_lock = [transaction_record.account_id]
            if transaction_record.related_account_id:
                accounts_to_lock.append(transaction_record.related_account_id)
            locked_accounts = {
                account.pk: account
                for account in BankAccount.objects.select_for_update().filter(pk__in=accounts_to_lock)
            }
            transaction_record.account = locked_accounts[transaction_record.account_id]
            if transaction_record.related_account_id:
                transaction_record.related_account = locked_accounts[transaction_record.related_account_id]
            if transaction_record.transaction_type == 'receipt':
                default_accounts = ensure_default_accounts(
                    transaction_record.organization,
                    branch=transaction_record.branch,
                )
                bank_gl_account = default_accounts['bank' if transaction_record.account.account_type == 'bank' else 'cash']
                receivable_account = default_accounts['accounts_receivable']
                customer_advance_account = default_accounts['customer_advances']
                transaction_record.account.current_balance += transaction_record.amount
                journal = JournalEntry.objects.create(
                    organization=transaction_record.organization,
                    branch=transaction_record.branch,
                    reference=f'BANK-REC-{transaction_record.reference_number}',
                    entry_date=transaction_record.transaction_date,
                    narration=f'Bank receipt {transaction_record.reference_number}',
                    created_by=transaction_record.created_by,
                )
                JournalEntryLine.objects.create(entry=journal, account=bank_gl_account, description=transaction_record.description, debit=transaction_record.amount)
                settlement_account = (
                    receivable_account
                    if transaction_record.settlement_mode == 'legacy'
                    else customer_advance_account
                )
                JournalEntryLine.objects.create(entry=journal, account=settlement_account, description='Customer receipt settlement' if transaction_record.settlement_mode == 'legacy' else 'Unapplied customer advance', credit=transaction_record.amount)
                journal.post()
            elif transaction_record.transaction_type == 'payment':
                default_accounts = ensure_default_accounts(
                    transaction_record.organization,
                    branch=transaction_record.branch,
                )
                bank_gl_account = default_accounts['bank' if transaction_record.account.account_type == 'bank' else 'cash']
                payable_account = default_accounts['accounts_payable']
                supplier_advance_account = default_accounts['supplier_advances']
                transaction_record.account.current_balance -= transaction_record.amount
                journal = JournalEntry.objects.create(
                    organization=transaction_record.organization,
                    branch=transaction_record.branch,
                    reference=f'BANK-PAY-{transaction_record.reference_number}',
                    entry_date=transaction_record.transaction_date,
                    narration=f'Bank payment {transaction_record.reference_number}',
                    created_by=transaction_record.created_by,
                )
                settlement_account = (
                    payable_account
                    if transaction_record.settlement_mode == 'legacy'
                    else supplier_advance_account
                )
                JournalEntryLine.objects.create(entry=journal, account=settlement_account, description='Supplier payment settlement' if transaction_record.settlement_mode == 'legacy' else 'Unapplied supplier advance', debit=transaction_record.amount)
                JournalEntryLine.objects.create(entry=journal, account=bank_gl_account, description=transaction_record.description, credit=transaction_record.amount)
                journal.post()
            else:
                default_accounts = ensure_default_accounts(
                    transaction_record.organization,
                    branch=transaction_record.branch,
                )
                source_gl = default_accounts['bank' if transaction_record.account.account_type == 'bank' else 'cash']
                target_gl = default_accounts['bank' if transaction_record.related_account.account_type == 'bank' else 'cash']
                transaction_record.account.current_balance -= transaction_record.amount
                transaction_record.related_account.current_balance += transaction_record.amount
                journal = JournalEntry.objects.create(
                    organization=transaction_record.organization,
                    branch=transaction_record.branch,
                    reference=f'BANK-TRF-{transaction_record.reference_number}',
                    entry_date=transaction_record.transaction_date,
                    narration=f'Bank transfer {transaction_record.reference_number}',
                    created_by=transaction_record.created_by,
                )
                JournalEntryLine.objects.create(entry=journal, account=source_gl, description=f'Transfer from {transaction_record.account.name}', debit=transaction_record.amount)
                JournalEntryLine.objects.create(entry=journal, account=target_gl, description=f'Transfer to {transaction_record.related_account.name}', credit=transaction_record.amount)
                journal.post()
                transaction_record.related_account.save(update_fields=['current_balance'])

            transaction_record.account.save(update_fields=['current_balance'])
            transaction_record.status = 'posted'
            transaction_record.is_reconciled = False
            transaction_record.save(update_fields=['status', 'is_reconciled'])
            from .services import recalculate_customer_outstanding, recalculate_supplier_outstanding

            if transaction_record.customer_id:
                recalculate_customer_outstanding(transaction_record.customer_id)
            if transaction_record.supplier_id:
                recalculate_supplier_outstanding(transaction_record.supplier_id)
            self.status = transaction_record.status
            self.is_reconciled = transaction_record.is_reconciled
        return self

    def mark_reconciled(self, statement_line, *, user=None):
        with transaction.atomic():
            transaction_record = BankTransaction.objects.select_for_update().get(pk=self.pk)
            line = BankStatementLine.objects.select_for_update().select_related('account').get(pk=statement_line.pk)
            if transaction_record.status != 'posted' or transaction_record.is_reconciled:
                raise ValidationError('Only unreconciled posted transactions can be reconciled.')
            if line.status != 'unmatched' or line.account_id != transaction_record.account_id:
                raise ValidationError('Statement line must be unmatched and belong to the same bank account.')
            if (
                line.import_record.organization_id != transaction_record.organization_id
                or (
                    line.import_record.branch_id
                    and transaction_record.branch_id
                    and line.import_record.branch_id != transaction_record.branch_id
                )
            ):
                raise ValidationError('Statement line and transaction must belong to the same organization and branch.')
            expected_statement_amount = (
                -transaction_record.amount
                if transaction_record.transaction_type in {'payment', 'transfer'}
                else transaction_record.amount
            )
            if line.amount != expected_statement_amount or line.statement_date != transaction_record.transaction_date:
                raise ValidationError('Statement date and amount must match the transaction before confirmation.')
            line.matched_transaction = transaction_record
            line.status = 'matched'
            line.save(update_fields=['matched_transaction', 'status'])
            transaction_record.is_reconciled = True
            transaction_record.status = 'reconciled'
            transaction_record.save(update_fields=['is_reconciled', 'status'])
            BankReconciliationEvent.objects.create(
                statement_line=line,
                bank_transaction=transaction_record,
                action='matched',
                user=user,
            )
            self.status = transaction_record.status
            self.is_reconciled = True
        return self

    def mark_unreconciled(self, *, user, reason):
        with transaction.atomic():
            transaction_record = BankTransaction.objects.select_for_update().get(pk=self.pk)
            if transaction_record.status != 'reconciled' or not transaction_record.is_reconciled:
                raise ValidationError('Only reconciled transactions can be unreconciled.')
            if not reason or not reason.strip():
                raise ValidationError('A reason is required to undo reconciliation.')
            line = BankStatementLine.objects.select_for_update().get(
                matched_transaction=transaction_record
            )
            line.status = 'unmatched'
            line.matched_transaction = None
            line.save(update_fields=['status', 'matched_transaction'])
            transaction_record.status = 'posted'
            transaction_record.is_reconciled = False
            transaction_record.save(update_fields=['status', 'is_reconciled'])
            BankReconciliationEvent.objects.create(
                statement_line=line,
                bank_transaction=transaction_record,
                action='unmatched',
                user=user,
                reason=reason.strip()[:255],
            )
            self.status = transaction_record.status
            self.is_reconciled = False
        return self


class BankStatementImport(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('processed', 'Processed'), ('failed', 'Failed')]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='bank_statement_imports')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='bank_statement_imports')
    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='statement_imports')
    file_name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    imported_at = models.DateTimeField(default=timezone.now)
    duplicate_count = models.IntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def process_csv(self, csv_content):
        if self.account.organization_id != self.organization_id:
            raise ValidationError('Statement account must belong to the import organization.')
        if self.account.branch_id and self.account.branch_id != self.branch_id:
            raise ValidationError('Statement account branch must match the import branch.')
        try:
            reader = csv.DictReader(io.StringIO(csv_content))
            if not reader.fieldnames:
                raise ValidationError('CSV file must include a header row.')
            normalized_fields = {field.strip().lower() for field in reader.fieldnames if field}
            reference_key = next((key for key in ('reference', 'reference_number', 'transaction_ref') if key in normalized_fields), None)
            date_key = next((key for key in ('transaction_date', 'date') if key in normalized_fields), None)
            amount_key = 'amount' if 'amount' in normalized_fields else None
            if not reference_key or not date_key or not amount_key:
                raise ValidationError('CSV must include reference, amount and transaction_date/date columns.')
            rows = list(reader)
            if len(rows) > 10000:
                raise ValidationError('CSV import is limited to 10,000 statement rows.')

            created = 0
            duplicates = 0
            seen_references = set()
            with transaction.atomic():
                for index, raw_row in enumerate(rows, start=2):
                    row = {(key or '').strip().lower(): (value or '').strip() for key, value in raw_row.items()}
                    if not any(row.values()):
                        continue
                    reference = row.get(reference_key, '')
                    if not reference:
                        raise ValidationError(f'Row {index}: reference is required.')
                    if len(reference) > 120:
                        raise ValidationError(f'Row {index}: reference cannot exceed 120 characters.')
                    try:
                        amount = Decimal(row.get(amount_key, '').replace(',', ''))
                    except (InvalidOperation, ValueError) as exc:
                        raise ValidationError(f'Row {index}: amount must be a valid decimal.') from exc
                    if not amount.is_finite():
                        raise ValidationError(f'Row {index}: amount must be finite.')
                    if (
                        amount.as_tuple().exponent < -2
                        or len(amount.as_tuple().digits) + max(amount.as_tuple().exponent, 0) > 18
                    ):
                        raise ValidationError(f'Row {index}: amount must fit a two-decimal currency value.')
                    try:
                        statement_date = date.fromisoformat(row.get(date_key, ''))
                    except ValueError as exc:
                        raise ValidationError(f'Row {index}: date must use YYYY-MM-DD format.') from exc
                    if reference in seen_references or BankStatementLine.objects.filter(
                        account=self.account,
                        reference=reference,
                    ).exists():
                        duplicates += 1
                        seen_references.add(reference)
                        continue
                    seen_references.add(reference)
                    BankStatementLine.objects.create(
                        import_record=self,
                        account=self.account,
                        reference=reference,
                        statement_date=statement_date,
                        amount=amount,
                        description=row.get('description') or row.get('narration', ''),
                        status='unmatched',
                    )
                    created += 1
                self.status = 'processed'
                self.duplicate_count = duplicates
                self.save(update_fields=['status', 'duplicate_count'])
            return created, duplicates
        except csv.Error as exc:
            raise ValidationError('CSV file could not be parsed.') from exc
        except IntegrityError as exc:
            raise ValidationError('A statement reference was imported concurrently; retry the import.') from exc


class BankStatementLine(models.Model):
    STATUS_CHOICES = [('matched', 'Matched'), ('unmatched', 'Unmatched'), ('duplicate', 'Duplicate')]

    import_record = models.ForeignKey(BankStatementImport, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='statement_lines')
    reference = models.CharField(max_length=120)
    statement_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unmatched')
    matched_transaction = models.OneToOneField(
        BankTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='statement_match',
    )

    class Meta:
        ordering = ['-statement_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['account', 'reference'], name='unique_statement_reference_per_account'),
        ]

    def __str__(self):
        return f'{self.reference} - {self.amount}'


class BankReconciliationEvent(models.Model):
    ACTION_CHOICES = [('matched', 'Matched'), ('unmatched', 'Unmatched')]

    statement_line = models.ForeignKey(
        BankStatementLine,
        on_delete=models.PROTECT,
        related_name='reconciliation_events',
    )
    bank_transaction = models.ForeignKey(
        BankTransaction,
        on_delete=models.PROTECT,
        related_name='reconciliation_events',
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']


class InvoiceAllocation(models.Model):
    bank_transaction = models.ForeignKey(
        BankTransaction,
        on_delete=models.PROTECT,
        related_name='invoice_allocations',
    )
    sales_invoice = models.ForeignKey(
        'sales.SalesInvoice',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='receipt_allocations',
    )
    purchase_invoice = models.ForeignKey(
        'purchases.PurchaseInvoice',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='payment_allocations',
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    journal_entry = models.OneToOneField(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='invoice_allocation',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_allocations_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_allocations_reversed',
    )
    reversal_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['created_at', 'id']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(sales_invoice__isnull=False, purchase_invoice__isnull=True)
                    | models.Q(sales_invoice__isnull=True, purchase_invoice__isnull=False)
                ),
                name='allocation_exactly_one_invoice',
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='allocation_amount_positive',
            ),
        ]

    @property
    def is_active(self):
        return self.reversed_at is None

    @property
    def invoice(self):
        return self.sales_invoice or self.purchase_invoice

    def clean(self):
        transaction_record = self.bank_transaction
        invoice = self.invoice
        if invoice is None:
            raise ValidationError('Allocation must reference exactly one invoice.')
        if transaction_record.status not in {'posted', 'reconciled'}:
            raise ValidationError('Only posted bank transactions can be allocated.')
        if self.sales_invoice_id:
            if transaction_record.transaction_type != 'receipt':
                raise ValidationError('Customer invoices require customer receipt transactions.')
            if invoice.status != 'posted' or invoice.customer_id != transaction_record.customer_id:
                raise ValidationError('Sales invoice must be posted and belong to the receipt customer.')
        if self.purchase_invoice_id:
            if transaction_record.transaction_type != 'payment':
                raise ValidationError('Purchase invoices require supplier payment transactions.')
            if invoice.status != 'posted' or invoice.supplier_id != transaction_record.supplier_id:
                raise ValidationError('Purchase invoice must be posted and belong to the payment supplier.')
        if invoice.organization_id != transaction_record.organization_id:
            raise ValidationError('Invoice and payment must belong to the same organization.')
        if invoice.branch_id and transaction_record.branch_id and invoice.branch_id != transaction_record.branch_id:
            raise ValidationError('Invoice and payment must belong to the same branch.')
        if self.amount <= 0:
            raise ValidationError('Allocation amount must be greater than zero.')

    def save(self, *args, **kwargs):
        if self.pk:
            current = InvoiceAllocation.objects.get(pk=self.pk)
            fields = (
                'bank_transaction_id',
                'sales_invoice_id',
                'purchase_invoice_id',
                'amount',
                'created_by_id',
                'created_at',
                'reversed_at',
                'reversed_by_id',
                'reversal_reason',
            )
            metadata_unchanged = all(
                getattr(current, field) == getattr(self, field)
                for field in fields
            ) and current.journal_entry_id == self.journal_entry_id
            journal_link_only = (
                current.journal_entry_id is None
                and self.journal_entry_id is not None
                and all(
                    getattr(current, field) == getattr(self, field)
                    for field in fields
                    if field != 'created_at'
                )
            )
            if not metadata_unchanged and not journal_link_only:
                raise ValidationError('Allocations are immutable; reverse and create a new allocation instead.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Allocation history cannot be deleted; reverse the allocation instead.')

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounting.models import JournalEntry, JournalEntryLine, ensure_default_accounts, round_currency
from core.models import AuditEvent
from purchases.models import PurchaseInvoice
from sales.models import SalesInvoice

from .models import BankTransaction, InvoiceAllocation


def recalculate_customer_outstanding(customer_id):
    from sales.models import Customer, CustomerReceipt, SalesInvoice

    customer = Customer.objects.select_for_update().get(pk=customer_id)
    invoice_total = SalesInvoice.objects.filter(
        customer_id=customer_id,
        status='posted',
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    from returns.models import SalesCreditNote

    credit_total = SalesCreditNote.objects.filter(
        customer_id=customer_id,
        status='posted',
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    legacy_bank_receipts = BankTransaction.objects.filter(
        customer_id=customer_id,
        transaction_type='receipt',
        settlement_mode='legacy',
        status__in=['posted', 'reconciled'],
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    legacy_receipts = CustomerReceipt.objects.filter(
        customer_id=customer_id,
        status='posted',
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    advance_allocations = InvoiceAllocation.objects.filter(
        sales_invoice__customer_id=customer_id,
        bank_transaction__settlement_mode='advance',
        reversed_at__isnull=True,
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    customer.outstanding_balance = round_currency(
        invoice_total - credit_total - legacy_bank_receipts - legacy_receipts - advance_allocations
    )
    customer.save(update_fields=['outstanding_balance'])
    return customer.outstanding_balance


def recalculate_supplier_outstanding(supplier_id):
    from purchases.models import Supplier

    supplier = Supplier.objects.select_for_update().get(pk=supplier_id)
    invoice_total = PurchaseInvoice.objects.filter(
        supplier_id=supplier_id,
        status='posted',
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    from returns.models import SupplierDebitNote

    debit_total = SupplierDebitNote.objects.filter(
        supplier_id=supplier_id,
        status='posted',
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    legacy_payments = BankTransaction.objects.filter(
        supplier_id=supplier_id,
        transaction_type='payment',
        settlement_mode='legacy',
        status__in=['posted', 'reconciled'],
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    advance_allocations = InvoiceAllocation.objects.filter(
        purchase_invoice__supplier_id=supplier_id,
        bank_transaction__settlement_mode='advance',
        reversed_at__isnull=True,
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    supplier.outstanding_balance = round_currency(
        invoice_total - debit_total - legacy_payments - advance_allocations
    )
    supplier.save(update_fields=['outstanding_balance'])
    return supplier.outstanding_balance


def active_allocated_amount(*, bank_transaction=None, invoice=None):
    allocations = InvoiceAllocation.objects.filter(reversed_at__isnull=True)
    if bank_transaction is not None:
        allocations = allocations.filter(bank_transaction=bank_transaction)
    if invoice is not None:
        if isinstance(invoice, SalesInvoice):
            allocations = allocations.filter(sales_invoice=invoice)
        else:
            allocations = allocations.filter(purchase_invoice=invoice)
    return allocations.aggregate(total=Sum('amount'))['total'] or Decimal('0')


def invoice_open_amount(invoice):
    if isinstance(invoice, SalesInvoice):
        from returns.models import SalesCreditNote

        adjustment = SalesCreditNote.objects.filter(original_invoice=invoice, status='posted').aggregate(
            total=Sum('total_amount')
        )['total'] or Decimal('0')
    else:
        from returns.models import SupplierDebitNote

        adjustment = SupplierDebitNote.objects.filter(original_invoice=invoice, status='posted').aggregate(
            total=Sum('total_amount')
        )['total'] or Decimal('0')
    return max(invoice.total_amount - adjustment - active_allocated_amount(invoice=invoice), Decimal('0'))


def transaction_unapplied_amount(bank_transaction):
    return max(
        bank_transaction.amount - active_allocated_amount(bank_transaction=bank_transaction),
        Decimal('0'),
    )


@transaction.atomic
def allocate_payment(*, bank_transaction_id, invoice_id, amount, user):
    payment = BankTransaction.objects.select_for_update().select_related(
        'organization', 'branch', 'customer', 'supplier'
    ).get(pk=bank_transaction_id)
    if payment.transaction_type == 'receipt':
        invoice = SalesInvoice.objects.select_for_update().get(pk=invoice_id)
        invoice_field = 'sales_invoice'
        counterparty_matches = invoice.customer_id == payment.customer_id
    elif payment.transaction_type == 'payment':
        invoice = PurchaseInvoice.objects.select_for_update().get(pk=invoice_id)
        invoice_field = 'purchase_invoice'
        counterparty_matches = invoice.supplier_id == payment.supplier_id
    else:
        raise ValidationError('Only customer receipts and supplier payments can be allocated.')

    if payment.status not in {'posted', 'reconciled'}:
        raise ValidationError('Only posted receipts or payments can be allocated.')
    if invoice.status != 'posted':
        raise ValidationError('Only posted invoices can be allocated.')
    if not counterparty_matches:
        raise ValidationError('Invoice must belong to the same customer or supplier.')
    if payment.organization_id != invoice.organization_id:
        raise ValidationError('Invoice and payment must belong to the same organization.')
    if payment.branch_id and invoice.branch_id and payment.branch_id != invoice.branch_id:
        raise ValidationError('Invoice and payment must belong to the same branch.')
    amount = Decimal(amount)
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValidationError('Allocation must be a positive currency amount with no more than two decimal places.')

    unapplied = transaction_unapplied_amount(payment)
    open_amount = invoice_open_amount(invoice)
    if amount > unapplied:
        raise ValidationError(f'Allocation exceeds the unapplied payment amount of {unapplied}.')
    if amount > open_amount:
        raise ValidationError(f'Allocation exceeds the invoice balance of {open_amount}.')

    allocation = InvoiceAllocation(
        bank_transaction=payment,
        amount=amount,
        created_by=user,
        **{invoice_field: invoice},
    )
    allocation.full_clean()
    allocation.save()

    if payment.settlement_mode == 'advance':
        accounts = ensure_default_accounts(payment.organization, branch=payment.branch)
        if invoice_field == 'sales_invoice':
            debit_account = accounts['customer_advances']
            credit_account = accounts['accounts_receivable']
            description = f'Apply customer receipt to {invoice.invoice_number}'
        else:
            debit_account = accounts['accounts_payable']
            credit_account = accounts['supplier_advances']
            description = f'Apply supplier payment to {invoice.invoice_number}'
        entry = JournalEntry.objects.create(
            organization=payment.organization,
            branch=payment.branch,
            reference=f'AL-{allocation.pk}',
            entry_date=payment.transaction_date,
            narration=description,
            created_by=user,
        )
        JournalEntryLine.objects.create(
            entry=entry,
            account=debit_account,
            description=description,
            debit=amount,
        )
        JournalEntryLine.objects.create(
            entry=entry,
            account=credit_account,
            description=description,
            credit=amount,
        )
        entry.post()
        allocation.journal_entry = entry
        allocation.save(update_fields=['journal_entry'])

    AuditEvent.objects.create(
        user=user,
        organization=payment.organization,
        action='invoice_payment_allocated',
        details=(
            f'Bank transaction {payment.reference_number}: allocated {amount} '
            f'to invoice {invoice.invoice_number} (allocation {allocation.pk}).'
        ),
    )
    if invoice_field == 'sales_invoice':
        recalculate_customer_outstanding(invoice.customer_id)
    else:
        recalculate_supplier_outstanding(invoice.supplier_id)
    return allocation


@transaction.atomic
def reverse_allocation(*, allocation_id, user, reason):
    allocation = InvoiceAllocation.objects.select_for_update().select_related(
        'bank_transaction', 'sales_invoice', 'purchase_invoice', 'journal_entry'
    ).get(pk=allocation_id)
    if allocation.reversed_at is not None:
        raise ValidationError('This allocation has already been reversed.')
    if not reason or not reason.strip():
        raise ValidationError('A reason is required to reverse an allocation.')

    invoice = allocation.invoice
    if allocation.journal_entry_id:
        allocation.journal_entry.reverse(
            reference=f'ALR-{allocation.pk}',
            entry_date=timezone.localdate(),
            narration=f'Reversal of allocation {allocation.pk}: {reason.strip()}',
            created_by=user,
        )
    reversed_at = timezone.now()
    reversal_reason = reason.strip()[:255]
    InvoiceAllocation.objects.filter(pk=allocation.pk).update(
        reversed_at=reversed_at,
        reversed_by=user,
        reversal_reason=reversal_reason,
    )
    allocation.reversed_at = reversed_at
    allocation.reversed_by = user
    allocation.reversal_reason = reversal_reason
    AuditEvent.objects.create(
        user=user,
        organization=allocation.bank_transaction.organization,
        action='invoice_payment_allocation_reversed',
        details=(
            f'Allocation {allocation.pk} for invoice {invoice.invoice_number} '
            f'was reversed. Reason: {allocation.reversal_reason}'
        ),
    )
    if allocation.sales_invoice_id:
        recalculate_customer_outstanding(invoice.customer_id)
    else:
        recalculate_supplier_outstanding(invoice.supplier_id)
    return allocation

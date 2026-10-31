from collections import defaultdict
from decimal import Decimal
import time

from django.core.exceptions import ValidationError
from django.db import OperationalError, connections, models, transaction
from django.db.models import Sum
from django.utils import timezone

from accounting.models import (
    JournalEntry,
    JournalEntryLine,
    ensure_accounting_period_open,
    ensure_default_accounts,
    round_currency,
)
from core.models import AuditEvent
from inventory.models import (
    InventoryValuationEntry,
    Product,
    StockMovement,
    adjust_stock,
    warehouse_stock_balance,
)
from taxation.models import TaxTransaction

from .models import (
    CustomerReturn,
    CustomerReturnLine,
    SalesCreditNote,
    SalesCreditNoteLine,
    SupplierDebitNote,
    SupplierDebitNoteLine,
    SupplierReturn,
    SupplierReturnLine,
)

MONEY_FIELDS = ('line_amount', 'discount_amount', 'taxable_value', 'cgst', 'sgst', 'igst', 'utgst', 'cess')
TAX_COMPONENTS = ('cgst', 'sgst', 'igst', 'utgst', 'cess')


def _share(total, quantity, original_quantity):
    if original_quantity <= 0:
        raise ValidationError('Original transaction quantity must be positive.')
    if quantity <= 0:
        return Decimal('0.00')
    if quantity >= original_quantity:
        return round_currency(total)
    return round_currency(Decimal(total) * Decimal(quantity) / Decimal(original_quantity))


def _invoice_basis(invoice, line, kind):
    source = TaxTransaction.objects.filter(
        organization_id=invoice.organization_id,
        transaction_kind=kind,
        source_model='SalesInvoice' if kind == 'sale' else 'PurchaseInvoice',
        source_id=invoice.pk,
        source_line_id=line.pk,
    ).first()
    if source:
        basis = {
            'line_amount': source.line_amount,
            'discount_amount': source.discount_amount,
            'taxable_value': source.taxable_value,
            **{component: getattr(source, component) for component in TAX_COMPONENTS},
            'total_tax': source.total_tax,
            'invoice_total': source.invoice_total,
            'tax_source': source,
            'is_reverse_charge': source.is_reverse_charge,
            'is_input_tax_credit_eligible': source.is_input_tax_credit_eligible,
        }
        return basis

    lines = list(invoice.lines.order_by('pk'))
    net_amounts = {
        current.pk: round_currency(Decimal(current.line_total) - Decimal(current.discount_amount))
        for current in lines
    }
    total_net = sum(net_amounts.values(), Decimal('0'))
    fallback_tax = Decimal('0')
    if total_net:
        if line.pk == max(net_amounts, key=lambda pk: next(i for i, x in enumerate(lines) if x.pk == pk)):
            already = sum(
                (_fallback_tax(invoice, current, net_amounts, total_net) for current in lines[:-1]),
                Decimal('0'),
            )
            fallback_tax = round_currency(invoice.tax_amount - already)
        else:
            fallback_tax = _fallback_tax(invoice, line, net_amounts, total_net)
    base = net_amounts.get(line.pk, Decimal('0'))
    return {
        'line_amount': round_currency(line.line_total),
        'discount_amount': round_currency(line.discount_amount),
        'taxable_value': base,
        **{component: Decimal('0.00') for component in TAX_COMPONENTS},
        'total_tax': fallback_tax,
        'invoice_total': round_currency(base + fallback_tax),
        'tax_source': None,
        'is_reverse_charge': False,
        'is_input_tax_credit_eligible': True,
    }


def _fallback_tax(invoice, line, net_amounts, total_net):
    return round_currency(Decimal(invoice.tax_amount) * net_amounts[line.pk] / total_net)


def _return_snapshot(basis, old_quantity, add_quantity, original_quantity):
    prior = {field: _share(basis[field], old_quantity, original_quantity) for field in MONEY_FIELDS}
    cumulative = {
        field: _share(basis[field], old_quantity + add_quantity, original_quantity)
        for field in MONEY_FIELDS
    }
    values = {field: cumulative[field] - prior[field] for field in MONEY_FIELDS}
    prior_tax = _share(basis['total_tax'], old_quantity, original_quantity)
    cumulative_tax = _share(basis['total_tax'], old_quantity + add_quantity, original_quantity)
    values['total_tax'] = cumulative_tax - prior_tax
    prior_total = _share(basis['invoice_total'], old_quantity, original_quantity)
    cumulative_total = _share(basis['invoice_total'], old_quantity + add_quantity, original_quantity)
    values['invoice_total'] = cumulative_total - prior_total
    return values


def _create_reversal_tax_transaction(*, note, note_line, source, transaction_kind, transaction_date, branch):
    if source is None:
        return None
    def negative(value):
        return -Decimal(value)
    calculation = {
        'hsn_sac': source.hsn_sac,
        'origin_state': source.origin_state,
        'place_of_supply': source.place_of_supply,
        'line_amount': negative(note_line.line_amount),
        'discount_amount': negative(note_line.discount_amount),
        'is_inclusive': source.is_inclusive,
        'taxable_value': negative(note_line.taxable_value),
        'tax_rate': source.tax_rate,
        'cgst_rate': source.cgst_rate,
        'sgst_rate': source.sgst_rate,
        'igst_rate': source.igst_rate,
        'utgst_rate': source.utgst_rate,
        'cess_rate': source.cess_rate,
        'cgst': negative(note_line.cgst),
        'sgst': negative(note_line.sgst),
        'igst': negative(note_line.igst),
        'utgst': negative(note_line.utgst),
        'cess': negative(note_line.cess),
        'total_tax': negative(note_line.total_tax),
        'invoice_total': negative(note_line.total_amount),
        'is_reverse_charge': source.is_reverse_charge,
        'is_zero_rated': source.is_zero_rated,
        'is_exempt': source.is_exempt,
    }
    from taxation.services import persist_tax_transaction

    return persist_tax_transaction(
        organization=note.organization,
        branch=branch,
        transaction_kind=transaction_kind,
        transaction_date=transaction_date,
        source_model='SalesCreditNote' if transaction_kind == 'sale' else 'SupplierDebitNote',
        source_id=note.pk,
        source_line_id=note_line.pk,
        tax_code=source.tax_code,
        calculation=calculation,
        is_input_tax_credit_eligible=source.is_input_tax_credit_eligible,
    )


def _add_journal_line(*, entry, account, amount, description, side):
    amount = round_currency(amount)
    if amount <= 0:
        return
    kwargs = {'debit': amount, 'credit': Decimal('0')} if side == 'debit' else {
        'debit': Decimal('0'), 'credit': amount
    }
    JournalEntryLine.objects.create(entry=entry, account=account, description=description[:200], **kwargs)


@transaction.atomic
def _post_customer_return_once(return_id, *, user=None):
    invoice_id = CustomerReturn.objects.values_list('invoice_id', flat=True).get(pk=return_id)
    from sales.models import SalesInvoice

    invoice = SalesInvoice.objects.select_for_update().select_related('organization', 'branch', 'customer').get(pk=invoice_id)
    record = CustomerReturn.objects.select_for_update().select_related(
        'organization', 'branch', 'invoice__customer', 'warehouse'
    ).get(pk=return_id)
    if record.status == 'posted':
        return record
    if record.status != 'draft' or invoice.status != 'posted':
        raise ValidationError('Only a draft return against a posted sales invoice can be posted.')
    if (
        record.organization_id != invoice.organization_id
        or record.branch_id != invoice.branch_id
        or invoice.customer.organization_id != invoice.organization_id
    ):
        raise ValidationError('Return, customer and original invoice must share the organization and branch.')
    if record.return_date < invoice.invoice_date:
        raise ValidationError('Return date cannot be earlier than the original invoice date.')
    ensure_accounting_period_open(record.organization_id, record.return_date)
    if not record.lines.exists():
        raise ValidationError('Customer return has no lines.')
    if SalesCreditNote.objects.filter(customer_return=record).exists():
        raise ValidationError('A credit note already exists for this return.')

    invoice_lines = {
        line.pk: line for line in invoice.lines.select_for_update().select_related('product').order_by('pk')
    }
    existing_lines = list(
        CustomerReturnLine.objects.select_for_update().filter(
            invoice_line_id__in=invoice_lines,
            customer_return__status='posted',
        ).exclude(customer_return_id=record.pk)
    )
    returned_quantity = defaultdict(lambda: Decimal('0'))
    for item in existing_lines:
        returned_quantity[item.invoice_line_id] += item.quantity

    return_lines = list(record.lines.select_for_update().select_related('invoice_line__product').order_by('pk'))
    note = SalesCreditNote.objects.create(
        organization=record.organization,
        branch=record.branch,
        customer=invoice.customer,
        original_invoice=invoice,
        customer_return=record,
        credit_note_number=f'CN-{record.return_number}',
        credit_date=record.return_date,
    )
    total_base = Decimal('0')
    total_tax = Decimal('0')
    total_credit = Decimal('0')
    restock = defaultdict(lambda: {'product': None, 'quantity': Decimal('0'), 'cost': Decimal('0')})
    restock_cost = Decimal('0')
    prior_restocked_quantity = defaultdict(lambda: Decimal('0'))
    for previous_line in CustomerReturnLine.objects.filter(
        invoice_line__invoice=invoice,
        customer_return__status='posted',
        restock=True,
    ).select_related('invoice_line__product'):
        prior_restocked_quantity[previous_line.invoice_line.product_id] += previous_line.quantity
    source_sale_costs = {}

    for line in return_lines:
        invoice_line = invoice_lines.get(line.invoice_line_id)
        if invoice_line is None or invoice_line.invoice_id != invoice.pk:
            raise ValidationError('Each return line must belong to the original invoice.')
        old_quantity = returned_quantity[line.invoice_line_id]
        if old_quantity + line.quantity > invoice_line.quantity:
            raise ValidationError(f'Returned quantity exceeds the remaining sold quantity for {invoice_line.product.name}.')
        basis = _invoice_basis(invoice, invoice_line, 'sale')
        snapshot = _return_snapshot(basis, old_quantity, line.quantity, invoice_line.quantity)
        stock_amount = Decimal('0.00')
        if line.restock:
            if invoice_line.product_id not in source_sale_costs:
                source_model = 'DeliveryNote' if invoice.delivery_note_id else 'SalesInvoice'
                source_id = invoice.delivery_note_id or invoice.pk
                source_sale_costs[invoice_line.product_id] = StockMovement.objects.filter(
                    reference_model=source_model,
                    reference_id=source_id,
                    product_id=invoice_line.product_id,
                    movement_type='sale',
                ).first()
            source_movement = source_sale_costs[invoice_line.product_id]
            if source_movement is None:
                raise ValidationError(
                    f'No source stock-cost snapshot exists for {invoice_line.product.name}; '
                    'this return cannot be restocked at an invented cost.'
                )
            source_quantity = abs(source_movement.quantity)
            if source_quantity <= 0:
                raise ValidationError('The source sale movement has an invalid quantity.')
            try:
                source_value = abs(source_movement.valuation_entry.value_change)
            except InventoryValuationEntry.DoesNotExist:
                source_value = round_currency(source_quantity * source_movement.unit_cost)
            previous_product_quantity = prior_restocked_quantity[invoice_line.product_id]
            stock_amount = _share(
                source_value,
                previous_product_quantity + line.quantity,
                source_quantity,
            ) - _share(source_value, previous_product_quantity, source_quantity)
            prior_restocked_quantity[invoice_line.product_id] += line.quantity
        line.line_amount = snapshot['line_amount']
        line.discount_amount = snapshot['discount_amount']
        line.taxable_value = snapshot['taxable_value']
        for component in TAX_COMPONENTS:
            setattr(line, component, snapshot[component])
        line.total_tax = snapshot['total_tax']
        line.credit_amount = snapshot['invoice_total']
        line.stock_cost = stock_amount if line.restock else Decimal('0')
        line.save(update_fields=['line_amount', 'discount_amount', 'taxable_value', *TAX_COMPONENTS, 'total_tax', 'credit_amount', 'stock_cost'])

        note_line = SalesCreditNoteLine.objects.create(
            credit_note=note,
            return_line=line,
            invoice_line=invoice_line,
            quantity=line.quantity,
            line_amount=line.line_amount,
            discount_amount=line.discount_amount,
            taxable_value=line.taxable_value,
            cgst=line.cgst,
            sgst=line.sgst,
            igst=line.igst,
            utgst=line.utgst,
            cess=line.cess,
            total_tax=line.total_tax,
            total_amount=line.credit_amount,
        )
        _create_reversal_tax_transaction(
            note=note, note_line=note_line, source=basis['tax_source'], transaction_kind='sale',
            transaction_date=record.return_date, branch=record.branch,
        )
        total_base += line.taxable_value
        total_tax += line.total_tax
        total_credit += line.credit_amount
        returned_quantity[line.invoice_line_id] += line.quantity
        if line.restock:
            if not record.warehouse_id or not record.warehouse.is_active:
                raise ValidationError('Select an active authorized warehouse for restockable goods.')
            if record.warehouse.organization_id != record.organization_id or (
                record.branch_id and record.warehouse.branch_id != record.branch_id
            ):
                raise ValidationError('Restock warehouse must belong to the return branch and organization.')
            item = restock[(invoice_line.product_id, record.warehouse_id)]
            item['product'] = invoice_line.product
            item['quantity'] += line.quantity
            item['cost'] += line.stock_cost
            restock_cost += line.stock_cost

    note.subtotal = round_currency(total_base)
    note.tax_amount = round_currency(total_tax)
    note.total_amount = round_currency(total_credit)
    models.Model.save(note, update_fields=['subtotal', 'tax_amount', 'total_amount'])

    defaults = ensure_default_accounts(record.organization, branch=record.branch)
    entry = JournalEntry.objects.create(
        organization=record.organization,
        branch=record.branch,
        reference=f'CN-{note.credit_note_number}',
        entry_date=record.return_date,
        narration=f'Sales credit note {note.credit_note_number} against {invoice.invoice_number}',
        created_by=user,
    )
    # A sales credit note reduces revenue, output GST and the customer receivable.
    _add_journal_line(entry=entry, account=defaults['sales_revenue'], amount=total_base, description='Sales return', side='debit')
    _add_journal_line(entry=entry, account=defaults['gst_payable'], amount=total_tax, description='Output GST reversal', side='debit')
    if restock_cost:
        _add_journal_line(entry=entry, account=defaults['inventory'], amount=restock_cost, description='Restocked inventory', side='debit')
        _add_journal_line(entry=entry, account=defaults['cost_of_goods_sold'], amount=restock_cost, description='Reverse cost of goods sold', side='credit')
    _add_journal_line(
        entry=entry,
        account=defaults['accounts_receivable'],
        amount=total_credit,
        description=f'Customer credit {invoice.customer.name}',
        side='credit',
    )
    if round_currency(entry.total_debit) != round_currency(entry.total_credit):
        difference = round_currency(entry.total_debit - entry.total_credit)
        if difference > 0:
            _add_journal_line(entry=entry, account=defaults['sales_revenue'], amount=difference, description='Credit note rounding adjustment', side='credit')
        else:
            _add_journal_line(entry=entry, account=defaults['sales_revenue'], amount=-difference, description='Credit note rounding adjustment', side='debit')
    entry.post()
    note.journal_entry = entry
    note.status = 'posted'
    models.Model.save(note, update_fields=['journal_entry', 'status'])

    for (product_id, warehouse_id), item in restock.items():
        if StockMovement.objects.filter(
            product_id=product_id, warehouse_id=warehouse_id, reference_model='CustomerReturn', reference_id=record.pk
        ).exists():
            raise ValidationError('A stock movement already exists for this customer return.')
        average_cost = item['cost'] / item['quantity'] if item['quantity'] else Decimal('0')
        adjust_stock(
            item['product'], record.warehouse, item['quantity'], 'sales_return', 'CustomerReturn', record.pk,
            average_cost, user=user,
        )

    record.status = 'posted'
    record.posted_at = timezone.now()
    models.Model.save(record, update_fields=['status', 'posted_at'])
    from banking.services import recalculate_customer_outstanding

    recalculate_customer_outstanding(invoice.customer_id)
    AuditEvent.objects.create(
        user=user,
        organization=record.organization,
        action='customer_return_posted',
        details=(
            f'Customer return {record.return_number} posted against invoice {invoice.invoice_number}; '
            f'credit note {note.credit_note_number}; journal {entry.reference}; stock cost restored {restock_cost}.'
        ),
    )
    return record


def _receipt_unit_cost(receipt_line):
    movement = StockMovement.objects.filter(
        reference_model='GoodsReceipt',
        reference_id=receipt_line.goods_receipt_id,
        product_id=receipt_line.order_line.product_id,
        warehouse_id=receipt_line.goods_receipt.warehouse_id,
    ).first()
    if movement:
        return Decimal(movement.unit_cost)
    order_line = receipt_line.order_line
    if order_line.quantity:
        return (Decimal(order_line.quantity * order_line.unit_cost) - Decimal(order_line.discount_amount)) / Decimal(order_line.quantity)
    return Decimal('0')


@transaction.atomic
def _post_supplier_return_once(return_id, *, user=None):
    initial = SupplierReturn.objects.select_related('goods_receipt', 'purchase_invoice').get(pk=return_id)
    source_invoice_id = initial.purchase_invoice_id
    receipt_id = initial.goods_receipt_id
    from purchases.models import GoodsReceipt, GoodsReceiptLine, PurchaseInvoice, PurchaseInvoiceLine

    invoice = None
    receipt = None
    if source_invoice_id:
        invoice = PurchaseInvoice.objects.select_for_update().select_related('organization', 'branch', 'supplier').get(pk=source_invoice_id)
        if invoice.status != 'posted':
            raise ValidationError('Supplier returns require a posted supplier invoice.')
        locked_invoice_lines = list(invoice.lines.select_for_update().order_by('pk'))
        receipt_ids = [line.receipt_line.goods_receipt_id for line in locked_invoice_lines if line.receipt_line_id]
        if receipt_ids:
            list(GoodsReceipt.objects.select_for_update().filter(pk__in=receipt_ids).order_by('pk'))
            list(
                GoodsReceiptLine.objects.select_for_update()
                .filter(pk__in=[line.receipt_line_id for line in locked_invoice_lines if line.receipt_line_id])
                .order_by('pk')
            )
    if receipt_id:
        receipt = GoodsReceipt.objects.select_for_update().select_related('organization', 'branch', 'warehouse', 'order__supplier').get(pk=receipt_id)
        list(receipt.lines.select_for_update().order_by('pk'))
    record = SupplierReturn.objects.select_for_update().select_related(
        'organization', 'branch', 'supplier', 'warehouse'
    ).get(pk=return_id)
    if record.status == 'posted':
        return record
    if record.status != 'draft':
        raise ValidationError('Only a draft supplier return can be posted.')
    if receipt and receipt.status != 'posted':
        raise ValidationError('Supplier returns require a posted goods receipt.')
    source = invoice or receipt
    if source is None or record.organization_id != source.organization_id or (
        source.branch_id and record.branch_id != source.branch_id
    ):
        raise ValidationError('Return and original receipt or invoice must share an organization and branch.')
    source_supplier = invoice.supplier if invoice else receipt.order.supplier
    if record.supplier_id != source_supplier.pk or record.warehouse.organization_id != record.organization_id:
        raise ValidationError('Supplier and warehouse must belong to the return organization.')
    if record.warehouse.branch_id != record.branch_id or not record.warehouse.is_active:
        raise ValidationError('Select an active warehouse belonging to the return branch.')
    source_date = invoice.invoice_date if invoice else receipt.receipt_date
    if record.return_date < source_date:
        raise ValidationError('Return date cannot be earlier than the source document date.')
    ensure_accounting_period_open(record.organization_id, record.return_date)
    if not record.lines.exists():
        raise ValidationError('Supplier return has no lines.')
    if SupplierDebitNote.objects.filter(supplier_return=record).exists():
        raise ValidationError('A debit note already exists for this return.')

    return_lines = list(record.lines.select_for_update().select_related(
        'receipt_line__goods_receipt__warehouse', 'receipt_line__order_line__product',
        'invoice_line__product', 'invoice_line__receipt_line__goods_receipt__warehouse',
    ).order_by('pk'))
    note = SupplierDebitNote.objects.create(
        organization=record.organization,
        branch=record.branch,
        supplier=record.supplier,
        original_receipt=receipt,
        original_invoice=invoice,
        supplier_return=record,
        debit_note_number=f'DN-{record.return_number}',
        debit_date=record.return_date,
    )
    total_base = Decimal('0')
    total_tax = Decimal('0')
    total_payable = Decimal('0')
    eligible_tax = Decimal('0')
    noneligible_tax = Decimal('0')
    reverse_charge_tax = Decimal('0')
    reverse_charge_eligible_tax = Decimal('0')
    reverse_charge_noneligible_tax = Decimal('0')
    stock_credit = Decimal('0')
    stock_movements = defaultdict(lambda: {
        'product': None,
        'quantity': Decimal('0'),
        'lines': [],
    })

    for line in return_lines:
        invoice_line = line.invoice_line
        receipt_line = line.receipt_line or (invoice_line.receipt_line if invoice_line else None)
        if receipt:
            if receipt_line is None or receipt_line.goods_receipt_id != receipt.pk:
                raise ValidationError('Each receipt-based return line must belong to the selected receipt.')
        elif invoice_line is None or invoice_line.invoice_id != invoice.pk:
            raise ValidationError('Each invoice-based return line must belong to the selected supplier invoice.')
        product = invoice_line.product if invoice_line else receipt_line.order_line.product
        if product.organization_id != record.organization_id:
            raise ValidationError('Returned product must belong to the return organization.')

        previous_receipt_quantity = Decimal('0')
        if receipt_line:
            previous_receipt_quantity = SupplierReturnLine.objects.filter(
                receipt_line=receipt_line,
                supplier_return__status='posted',
            ).exclude(supplier_return_id=record.pk).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            if invoice_line is None:
                billed = PurchaseInvoiceLine.objects.filter(
                    receipt_line=receipt_line, invoice__status='posted'
                ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
                eligible = receipt_line.quantity - billed - previous_receipt_quantity
                if line.quantity > eligible:
                    raise ValidationError(f'Return quantity exceeds unbilled received quantity for {product.name}.')
            elif previous_receipt_quantity + line.quantity > receipt_line.quantity:
                raise ValidationError(f'Returned quantity exceeds received quantity for {product.name}.')

        basis = None
        old_quantity = Decimal('0')
        if invoice_line:
            if invoice_line.invoice.status != 'posted':
                raise ValidationError('Supplier return invoice lines must be posted.')
            old_quantity = SupplierReturnLine.objects.filter(
                invoice_line=invoice_line, supplier_return__status='posted'
            ).exclude(supplier_return_id=record.pk).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            if old_quantity + line.quantity > invoice_line.quantity:
                raise ValidationError(f'Returned quantity exceeds invoiced quantity for {product.name}.')
            basis = _invoice_basis(invoice_line.invoice, invoice_line, 'purchase')
            snapshot = _return_snapshot(basis, old_quantity, line.quantity, invoice_line.quantity)
            stock_source = receipt_line.goods_receipt if receipt_line else invoice_line.invoice
            stock_reference = 'GoodsReceipt' if receipt_line else 'PurchaseInvoice'
            source_pk = receipt_line.goods_receipt_id if receipt_line else invoice_line.invoice_id
            source_cost_movement = StockMovement.objects.filter(
                reference_model=stock_reference,
                reference_id=source_pk,
                product=product,
            ).select_related('warehouse').first()
            if source_cost_movement:
                if source_cost_movement.warehouse_id != record.warehouse_id:
                    raise ValidationError('Return warehouse must match the warehouse that received the original goods.')
                stock_unit_cost = Decimal(source_cost_movement.unit_cost)
            else:
                stock_unit_cost = Decimal(invoice_line.unit_cost)
        else:
            order_line = receipt_line.order_line
            stock_unit_cost = _receipt_unit_cost(receipt_line)
            payable_unit = (
                Decimal(order_line.quantity * order_line.unit_cost) - Decimal(order_line.discount_amount)
            ) / Decimal(order_line.quantity)
            payable = round_currency(payable_unit * line.quantity)
            snapshot = {
                'line_amount': payable,
                'discount_amount': Decimal('0.00'),
                'taxable_value': payable,
                **{component: Decimal('0.00') for component in TAX_COMPONENTS},
                'total_tax': Decimal('0.00'),
                'invoice_total': payable,
            }

        if record.warehouse_id != (receipt_line.goods_receipt.warehouse_id if receipt_line else record.warehouse_id):
            raise ValidationError('Return warehouse must match the original receipt warehouse.')
        stock_cost = Decimal('0.00')
        line.line_amount = snapshot['line_amount']
        line.discount_amount = snapshot['discount_amount']
        line.taxable_value = snapshot['taxable_value']
        for component in TAX_COMPONENTS:
            setattr(line, component, snapshot[component])
        line.total_tax = snapshot['total_tax']
        line.debit_amount = snapshot['invoice_total']
        line.stock_cost = stock_cost
        line.is_reverse_charge = basis['is_reverse_charge'] if basis else False
        line.is_input_tax_credit_eligible = basis['is_input_tax_credit_eligible'] if basis else True
        line.save(update_fields=[
            'line_amount', 'discount_amount', 'taxable_value', *TAX_COMPONENTS, 'total_tax', 'debit_amount', 'stock_cost',
            'is_reverse_charge', 'is_input_tax_credit_eligible',
        ])
        note_line = SupplierDebitNoteLine.objects.create(
            debit_note=note,
            return_line=line,
            invoice_line=invoice_line,
            quantity=line.quantity,
            line_amount=line.line_amount,
            discount_amount=line.discount_amount,
            taxable_value=line.taxable_value,
            cgst=line.cgst,
            sgst=line.sgst,
            igst=line.igst,
            utgst=line.utgst,
            cess=line.cess,
            total_tax=line.total_tax,
            total_amount=line.debit_amount,
            is_reverse_charge=line.is_reverse_charge,
            is_input_tax_credit_eligible=line.is_input_tax_credit_eligible,
        )
        if basis and basis['tax_source']:
            _create_reversal_tax_transaction(
                note=note, note_line=note_line, source=basis['tax_source'], transaction_kind='purchase',
                transaction_date=record.return_date, branch=record.branch,
            )
        total_base += line.taxable_value
        total_tax += line.total_tax
        total_payable += line.debit_amount
        if line.is_reverse_charge:
            reverse_charge_tax += line.total_tax
            if line.is_input_tax_credit_eligible:
                reverse_charge_eligible_tax += line.total_tax
            else:
                reverse_charge_noneligible_tax += line.total_tax
        elif line.is_input_tax_credit_eligible:
            eligible_tax += line.total_tax
        else:
            noneligible_tax += line.total_tax
        stock_key = (product.pk, record.warehouse_id)
        stock_movements[stock_key]['product'] = product
        stock_movements[stock_key]['quantity'] += line.quantity
        stock_movements[stock_key]['lines'].append(line)

    # Consume the current weighted-average value, rather than the original purchase
    # price, so the stock subledger and inventory control posting use the same amount.
    for (product_id, warehouse_id), item in stock_movements.items():
        if StockMovement.objects.filter(
            product_id=product_id,
            warehouse_id=warehouse_id,
            reference_model='SupplierReturn',
            reference_id=record.pk,
        ).exists():
            raise ValidationError('A stock movement already exists for this supplier return.')
        if warehouse_stock_balance(item['product'], record.warehouse) < item['quantity']:
            raise ValidationError(f'Insufficient stock to return {item["product"].name}.')
        movement = adjust_stock(
            item['product'],
            record.warehouse,
            -item['quantity'],
            'purchase_return',
            'SupplierReturn',
            record.pk,
            user=user,
        )
        removed_value = abs(movement.valuation_entry.value_change)
        stock_credit += removed_value
        allocated_quantity = Decimal('0')
        allocated_value = Decimal('0.00')
        for return_line in item['lines']:
            allocated_quantity += return_line.quantity
            line_value = _share(removed_value, allocated_quantity, item['quantity']) - allocated_value
            allocated_value += line_value
            return_line.stock_cost = line_value
            return_line.save(update_fields=['stock_cost'])

    note.subtotal = round_currency(total_base)
    note.tax_amount = round_currency(total_tax)
    note.total_amount = round_currency(total_payable)
    models.Model.save(note, update_fields=['subtotal', 'tax_amount', 'total_amount'])
    defaults = ensure_default_accounts(record.organization, branch=record.branch)
    entry = JournalEntry.objects.create(
        organization=record.organization,
        branch=record.branch,
        reference=f'DN-{note.debit_note_number}',
        entry_date=record.return_date,
        narration=f'Supplier debit note {note.debit_note_number} against return {record.return_number}',
        created_by=user,
    )
    _add_journal_line(entry=entry, account=defaults['accounts_payable'], amount=total_payable, description='Supplier liability reduction', side='debit')
    _add_journal_line(entry=entry, account=defaults['inventory'], amount=stock_credit, description='Returned inventory at weighted-average cost', side='credit')
    _add_journal_line(
        entry=entry, account=defaults['gst_input'], amount=eligible_tax + reverse_charge_eligible_tax,
        description='Input GST reversal', side='credit',
    )
    _add_journal_line(entry=entry, account=defaults['gst_payable'], amount=reverse_charge_tax, description='Reverse charge GST reversal', side='debit')
    if round_currency(entry.total_debit) != round_currency(entry.total_credit):
        difference = round_currency(entry.total_debit - entry.total_credit)
        if difference > 0:
            _add_journal_line(
                entry=entry,
                account=defaults['inventory_adjustment_gain'],
                amount=difference,
                description='Supplier return cost variance gain',
                side='credit',
            )
        else:
            _add_journal_line(
                entry=entry,
                account=defaults['inventory_adjustment_loss'],
                amount=-difference,
                description='Supplier return cost variance loss',
                side='debit',
            )
    entry.post()
    note.journal_entry = entry
    note.status = 'posted'
    models.Model.save(note, update_fields=['journal_entry', 'status'])

    record.status = 'posted'
    record.posted_at = timezone.now()
    models.Model.save(record, update_fields=['status', 'posted_at'])
    from banking.services import recalculate_supplier_outstanding

    recalculate_supplier_outstanding(record.supplier_id)
    AuditEvent.objects.create(
        user=user,
        organization=record.organization,
        action='supplier_return_posted',
        details=(
            f'Supplier return {record.return_number} posted against '
            f'{invoice.invoice_number if invoice else receipt.receipt_number}; debit note {note.debit_note_number}; '
            f'journal {entry.reference}; stock cost removed {stock_credit}.'
        ),
    )
    return record


def _retry_sqlite_locked_return_post(post_once, return_id, *, user=None):
    """Retry a fully rolled-back return post on SQLite lock contention only."""
    for attempt in range(5):
        try:
            return post_once(return_id, user=user)
        except OperationalError as exc:
            if (
                connections['default'].vendor != 'sqlite'
                or 'locked' not in str(exc).lower()
                or attempt == 4
            ):
                raise
            time.sleep(0.05 * (2 ** attempt))


def post_customer_return(return_id, *, user=None):
    return _retry_sqlite_locked_return_post(_post_customer_return_once, return_id, user=user)


def post_supplier_return(return_id, *, user=None):
    return _retry_sqlite_locked_return_post(_post_supplier_return_once, return_id, user=user)

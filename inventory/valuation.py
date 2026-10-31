from decimal import Decimal

from django.db.models import DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from .models import InventoryValuationBalance, InventoryValuationEntry, Product, StockMovement


def valuation_position_queryset(organization, as_of, *, warehouses=None, search=''):
    """Return valuation positions using the same posting-date snapshots as the report."""
    movements_for_date = StockMovement.objects.filter(
        product_id=OuterRef('product_id'),
        warehouse_id=OuterRef('warehouse_id'),
        created_at__date__lte=as_of,
    ).order_by().values('product_id').annotate(total=Sum('quantity')).values('total')[:1]
    any_movement_for_date = StockMovement.objects.filter(
        product_id=OuterRef('product_id'),
        warehouse_id=OuterRef('warehouse_id'),
        created_at__date__lte=as_of,
    )
    history_for_date = InventoryValuationEntry.objects.filter(
        balance_id=OuterRef('pk'),
        effective_at__date__lte=as_of,
    ).order_by('-effective_at', '-pk')

    positions = InventoryValuationBalance.objects.filter(
        organization=organization,
        warehouse__organization=organization,
        warehouse__is_active=True,
    )
    if warehouses is not None:
        positions = positions.filter(warehouse__in=warehouses)
    if search:
        positions = positions.filter(
            Q(product__sku__icontains=search) | Q(product__name__icontains=search)
        )
    return positions.annotate(
        ledger_quantity_asof=Coalesce(
            Subquery(movements_for_date, output_field=DecimalField(max_digits=18, decimal_places=3)),
            Value(Decimal('0')),
            output_field=DecimalField(max_digits=18, decimal_places=3),
        ),
        has_movement_asof=Exists(any_movement_for_date),
        has_history_asof=Exists(history_for_date),
        latest_quantity_asof=Subquery(history_for_date.values('quantity_after')[:1]),
        latest_value_asof=Subquery(history_for_date.values('value_after')[:1]),
        latest_valued_asof=Subquery(history_for_date.values('is_valued_after')[:1]),
        latest_valuation_date=Subquery(history_for_date.values('effective_at')[:1]),
    ).select_related('product', 'warehouse', 'warehouse__branch').order_by(
        'product__sku', 'warehouse__code'
    )


def summarize_inventory_valuation(organization, as_of, *, branch=None):
    """Summarize known value and data gaps for a branch or an organization."""
    warehouses = organization.warehouses.filter(is_active=True)
    if branch is not None:
        warehouses = warehouses.filter(branch=branch)

    known_value = Decimal('0.00')
    unvalued_count = 0
    negative_count = 0
    quantity_mismatch_count = 0
    position_exception_count = 0
    for position in valuation_position_queryset(organization, as_of, warehouses=warehouses):
        if position.has_history_asof:
            quantity = position.latest_quantity_asof
            value = position.latest_value_asof
            is_valued = position.latest_valued_asof
        elif position.has_movement_asof:
            quantity = position.ledger_quantity_asof
            value = None
            is_valued = False
        else:
            quantity = Decimal('0')
            value = Decimal('0.00')
            is_valued = True

        if is_valued:
            known_value += value or Decimal('0.00')
        else:
            unvalued_count += 1
        if quantity < 0:
            negative_count += 1
        if quantity != position.ledger_quantity_asof:
            quantity_mismatch_count += 1
        if not is_valued or quantity < 0 or quantity != position.ledger_quantity_asof:
            position_exception_count += 1

    balance_exists = InventoryValuationBalance.objects.filter(
        organization=organization,
        product_id=OuterRef('product_id'),
        warehouse_id=OuterRef('warehouse_id'),
    )
    movement_positions = StockMovement.objects.filter(
        product__organization=organization,
        warehouse__organization=organization,
        created_at__date__lte=as_of,
    )
    if branch is not None:
        movement_positions = movement_positions.filter(warehouse__branch=branch)
    missing_balance_count = movement_positions.values('product_id', 'warehouse_id').annotate(
        has_balance=Exists(balance_exists),
        quantity=Sum('quantity'),
    ).filter(has_balance=False).count()

    # Product-level quantities predate warehouse allocation. They cannot be assigned
    # to a branch or costed from cost_price, so expose the gap without inventing value.
    unallocated_products = Product.objects.filter(organization=organization).annotate(
        ledger_quantity=Coalesce(
            Sum('stock_movements__quantity'),
            Value(Decimal('0')),
            output_field=DecimalField(max_digits=18, decimal_places=3),
        )
    ).exclude(stock_quantity=F('ledger_quantity'))
    unallocated_count = unallocated_products.count()

    exception_count = position_exception_count + missing_balance_count + unallocated_count
    return {
        'value': known_value,
        'is_complete': exception_count == 0,
        'exception_count': exception_count,
        'unvalued_count': unvalued_count,
        'negative_count': negative_count,
        'quantity_mismatch_count': quantity_mismatch_count,
        'missing_balance_count': missing_balance_count,
        'unallocated_count': unallocated_count,
    }

import csv
from decimal import Decimal, ROUND_HALF_UP

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounting.models import (
    JournalEntry,
    JournalEntryLine,
    ensure_accounting_period_open,
    ensure_default_accounts,
    round_currency,
)
from core.models import AuditEvent
from core.organization_context import get_active_organization
from core.permissions import has_organization_role
from organizations.models import Branch
from purchases.models import GoodsReceiptLine, PurchaseInvoiceLine
from returns.models import SupplierReturnLine

from .models import (
    InventoryValuationBalance,
    InventoryValuationEntry,
    Product,
    StockMovement,
    StockTransfer,
    Warehouse,
    adjust_stock,
    record_opening_valuation,
)
from .valuation import valuation_position_queryset


def _organization_for_request(request):
    return get_active_organization(request)


def _can_manage_inventory(user, organization):
    return has_organization_role(
        user, organization,
        {'owner', 'admin', 'inventory_manager', 'warehouse_operator', 'manager'},
    )


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            'sku', 'name', 'category', 'unit', 'cost_price', 'sales_price',
            'reorder_level', 'is_active',
        ]
        widgets = {
            'cost_price': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
            'sales_price': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
            'reorder_level': forms.NumberInput(attrs={'step': '0.001', 'min': '0', 'class': 'form-control'}),
        }


class WarehouseForm(forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ['name', 'code', 'branch', 'is_active']

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        branches = Branch.objects.filter(organization=organization, is_active=True)
        if user.branch_id:
            branches = branches.filter(pk=user.branch_id)
            self.fields['branch'].initial = user.branch
            self.fields['branch'].widget = forms.HiddenInput()
        self.fields['branch'].queryset = branches


class StockAdjustmentForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none())
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none())
    reference_number = forms.IntegerField(min_value=1)
    quantity_change = forms.DecimalField(
        max_digits=18,
        decimal_places=3,
        widget=forms.NumberInput(attrs={'step': '0.001', 'class': 'form-control'}),
    )
    unit_cost = forms.DecimalField(
        max_digits=18,
        decimal_places=6,
        required=False,
        min_value=Decimal('0'),
        widget=forms.NumberInput(attrs={'step': '0.000001', 'min': '0', 'class': 'form-control'}),
        help_text='Required for a positive adjustment. Negative adjustments use the current weighted-average cost.',
    )
    reason = forms.CharField(max_length=255)

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        products = Product.objects.filter(organization=organization, is_active=True)
        warehouses = Warehouse.objects.filter(organization=organization, is_active=True)
        if user.branch_id:
            warehouses = warehouses.filter(branch_id=user.branch_id)
        self.fields['product'].queryset = products.order_by('name')
        self.fields['warehouse'].queryset = warehouses.order_by('name')

    def clean(self):
        cleaned = super().clean()
        quantity = cleaned.get('quantity_change')
        product = cleaned.get('product')
        warehouse = cleaned.get('warehouse')
        if quantity == 0:
            self.add_error('quantity_change', 'Enter a non-zero stock adjustment.')
        if quantity is not None and quantity > 0 and cleaned.get('unit_cost') is None:
            self.add_error('unit_cost', 'Enter the approved unit cost for stock being added.')
        if product and warehouse and product.organization_id != warehouse.organization_id:
            self.add_error('warehouse', 'Select a warehouse in the product organization.')
        if not cleaned.get('reason', '').strip():
            self.add_error('reason', 'Provide a reason for the stock adjustment.')
        return cleaned


class InventoryOpeningForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none())
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none())
    quantity = forms.DecimalField(
        max_digits=18,
        decimal_places=3,
        min_value=Decimal('0'),
        widget=forms.NumberInput(attrs={'step': '0.001', 'min': '0', 'class': 'form-control'}),
        help_text='Must match the current warehouse ledger quantity, or the unallocated legacy quantity.',
    )
    inventory_value = forms.DecimalField(
        max_digits=24,
        decimal_places=2,
        min_value=Decimal('0'),
        widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'form-control'}),
        help_text='Enter the approved current total value. This does not rewrite old movements or journals.',
    )

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = Product.objects.filter(
            organization=organization,
            is_active=True,
        ).order_by('sku')
        warehouses = Warehouse.objects.filter(organization=organization, is_active=True)
        if user.branch_id:
            warehouses = warehouses.filter(branch_id=user.branch_id)
        self.fields['warehouse'].queryset = warehouses.order_by('code')

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get('product')
        warehouse = cleaned.get('warehouse')
        quantity = cleaned.get('quantity')
        value = cleaned.get('inventory_value')
        if product and warehouse and product.organization_id != warehouse.organization_id:
            self.add_error('warehouse', 'Choose a warehouse in the product organization.')
        if quantity == 0 and value not in (None, Decimal('0.00')):
            self.add_error('inventory_value', 'A zero quantity must have a zero total value.')
        return cleaned


class StockTransferForm(forms.ModelForm):
    class Meta:
        model = StockTransfer
        fields = ['reference', 'transfer_date', 'product', 'source_warehouse', 'destination_warehouse', 'quantity']
        widgets = {
            'transfer_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'quantity': forms.NumberInput(attrs={'step': '0.001', 'min': '0.001', 'class': 'form-control'}),
        }

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.instance.organization = organization
        self.fields['product'].queryset = Product.objects.filter(
            organization=organization,
            is_active=True,
        ).order_by('name')
        warehouses = Warehouse.objects.filter(
            organization=organization,
            is_active=True,
        )
        if user.branch_id:
            warehouses = warehouses.filter(branch_id=user.branch_id)
        self.fields['source_warehouse'].queryset = warehouses.order_by('name')
        self.fields['destination_warehouse'].queryset = warehouses.order_by('name')

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get('source_warehouse')
        destination = cleaned.get('destination_warehouse')
        if source and destination and source.pk == destination.pk:
            self.add_error('destination_warehouse', 'Choose a different destination warehouse.')
        product = cleaned.get('product')
        if product and source and product.organization_id != source.organization_id:
            self.add_error('product', 'Product and source warehouse must belong to the same organization.')
        if product and destination and product.organization_id != destination.organization_id:
            self.add_error('product', 'Product and destination warehouse must belong to the same organization.')
        return cleaned


@login_required
def inventory_home(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    products = Product.objects.filter(organization=organization)
    warehouses = Warehouse.objects.filter(organization=organization).select_related('branch')
    movements = StockMovement.objects.filter(
        product__organization=organization,
        warehouse__organization=organization,
    ).select_related('product', 'warehouse')
    if request.user.branch_id:
        warehouses = warehouses.filter(branch_id=request.user.branch_id)
        movements = movements.filter(warehouse__branch_id=request.user.branch_id)
        products = products.annotate(
            available_quantity=Coalesce(
                Sum(
                    'stock_movements__quantity',
                    filter=Q(stock_movements__warehouse__branch_id=request.user.branch_id),
                ),
                Value(0),
                output_field=DecimalField(max_digits=18, decimal_places=3),
            )
        )
    else:
        products = products.annotate(available_quantity=F('stock_quantity'))
    selected_warehouse = None
    warehouse_id = request.GET.get('warehouse')
    if warehouse_id:
        selected_warehouse = warehouses.filter(pk=warehouse_id).first()
        if selected_warehouse is None:
            from django.http import Http404

            raise Http404('Warehouse not found.')
        products = Product.objects.filter(organization=organization).annotate(
            available_quantity=Coalesce(
                Sum(
                    'stock_movements__quantity',
                    filter=Q(stock_movements__warehouse=selected_warehouse),
                ),
                Value(0),
                output_field=DecimalField(max_digits=18, decimal_places=3),
            )
        )
        if query := request.GET.get('q', '').strip():
            products = products.filter(Q(sku__icontains=query) | Q(name__icontains=query))
    query = request.GET.get('q', '').strip()
    if query:
        products = products.filter(Q(sku__icontains=query) | Q(name__icontains=query))
    low_stock = request.GET.get('low') == '1'
    if low_stock:
        products = products.filter(available_quantity__lte=F('reorder_level'))
    products_page = Paginator(products.order_by('name'), 25).get_page(request.GET.get('page'))
    valuation_positions = InventoryValuationBalance.objects.filter(
        organization=organization,
        product_id__in=[product.pk for product in products_page.object_list],
    ).select_related('product', 'warehouse')
    if request.user.branch_id:
        valuation_positions = valuation_positions.filter(warehouse__branch_id=request.user.branch_id)
    if selected_warehouse:
        valuation_positions = valuation_positions.filter(warehouse=selected_warehouse)
    totals_by_product = {}
    for position in valuation_positions:
        item = totals_by_product.setdefault(
            position.product_id,
            {'quantity': Decimal('0'), 'value': Decimal('0.00'), 'incomplete': False},
        )
        item['quantity'] += position.quantity_on_hand
        if position.is_valued:
            item['value'] += position.inventory_value
        elif position.quantity_on_hand:
            item['incomplete'] = True
    for product in products_page.object_list:
        item = totals_by_product.get(product.pk, {'quantity': Decimal('0'), 'value': Decimal('0.00'), 'incomplete': False})
        product.valuation_complete = (
            not item['incomplete'] and item['quantity'] == product.available_quantity
        )
        product.inventory_value = item['value'] if product.valuation_complete else None
        product.average_unit_cost = (
            (item['value'] / item['quantity']).quantize(Decimal('0.000001'))
            if product.valuation_complete and item['quantity'] else None
        )
    return render(request, 'inventory/index.html', {
        'organization': organization,
        'products': products_page,
        'warehouses': warehouses.order_by('name'),
        'movements': Paginator(movements.order_by('-created_at'), 20).get_page(
            request.GET.get('movement_page')
        ),
        'query': query,
        'low_stock': low_stock,
        'selected_warehouse': selected_warehouse,
        'can_manage': _can_manage_inventory(request.user, organization),
        'active_page': 'inventory',
    })


@login_required
def stock_transfer(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_inventory(request.user, organization):
        return HttpResponseForbidden('You do not have permission to transfer stock.')
    form = StockTransferForm(
        request.POST or None,
        organization=organization,
        user=request.user,
    )
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        transfer = None
        try:
            with transaction.atomic():
                transfer = StockTransfer.objects.filter(
                    organization=organization,
                    reference=data['reference'],
                ).first()
                if transfer:
                    expected = (
                        transfer.product_id == data['product'].pk
                        and transfer.source_warehouse_id == data['source_warehouse'].pk
                        and transfer.destination_warehouse_id == data['destination_warehouse'].pk
                        and transfer.quantity == data['quantity']
                    )
                    if not expected:
                        raise ValidationError('This transfer reference was already used with different values.')
                else:
                    transfer = form.save(commit=False)
                    transfer.organization = organization
                    transfer.created_by = request.user
                    transfer.save()
                transfer.post()
        except IntegrityError:
            transfer = StockTransfer.objects.filter(
                organization=organization,
                reference=data['reference'],
            ).first()
            if transfer is None:
                raise
            if (
                transfer.product_id != data['product'].pk
                or transfer.source_warehouse_id != data['source_warehouse'].pk
                or transfer.destination_warehouse_id != data['destination_warehouse'].pk
                or transfer.quantity != data['quantity']
            ):
                form.add_error('reference', 'This transfer reference was already used with different values.')
            else:
                transfer.post()
                messages.success(request, f'Stock transfer {transfer.reference} was already posted.')
                return redirect('inventory_home')
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
        else:
            messages.success(request, f'Stock transfer {transfer.reference} posted.')
            return redirect('inventory_home')
    return render(request, 'inventory/transfer_form.html', {
        'organization': organization,
        'form': form,
        'active_page': 'inventory',
    })


@login_required
def product_create(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_inventory(request.user, organization):
        return HttpResponseForbidden('You do not have permission to manage products.')
    form = ProductForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        product = form.save(commit=False)
        product.organization = organization
        product.stock_quantity = 0
        product.save()
        messages.success(request, f'Product {product.sku} created.')
        return redirect('inventory_home')
    return render(request, 'inventory/product_form.html', {
        'organization': organization,
        'form': form,
        'active_page': 'inventory',
    })


@login_required
def warehouse_create(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_inventory(request.user, organization):
        return HttpResponseForbidden('You do not have permission to manage warehouses.')
    form = WarehouseForm(
        request.POST or None,
        organization=organization,
        user=request.user,
    )
    if request.method == 'POST' and form.is_valid():
        warehouse = form.save(commit=False)
        warehouse.organization = organization
        if request.user.branch_id:
            warehouse.branch = request.user.branch
        warehouse.save()
        messages.success(request, f'Warehouse {warehouse.name} created.')
        return redirect('inventory_home')
    return render(request, 'inventory/warehouse_form.html', {
        'organization': organization,
        'form': form,
        'active_page': 'inventory',
    })


@login_required
def stock_adjustment(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_inventory(request.user, organization):
        return HttpResponseForbidden('You do not have permission to adjust stock.')
    form = StockAdjustmentForm(
        request.POST or None,
        organization=organization,
        user=request.user,
    )
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        product = data['product']
        warehouse = data['warehouse']
        quantity = data['quantity_change']
        ref_id = data['reference_number']
        try:
            with transaction.atomic():
                product = Product.objects.select_for_update().get(
                    pk=product.pk,
                    organization=organization,
                )
                existing = StockMovement.objects.filter(
                    product=product,
                    warehouse=warehouse,
                    movement_type='adjustment',
                    reference_model='ManualAdjustment',
                    reference_id=ref_id,
                ).first()
                if existing and existing.quantity != quantity:
                    raise ValidationError('This adjustment reference was already used with a different quantity.')
                if existing:
                    movement = existing
                else:
                    from django.utils import timezone

                    ensure_accounting_period_open(organization.pk, timezone.localdate())
                    movement = adjust_stock(
                        product,
                        warehouse,
                        quantity,
                        'adjustment',
                        'ManualAdjustment',
                        ref_id,
                        data['unit_cost'] if quantity > 0 else None,
                        user=request.user,
                    )
                    value = abs(movement.valuation_entry.value_change)
                    if value:
                        accounts = ensure_default_accounts(organization)
                        entry = JournalEntry.objects.create(
                            organization=organization,
                            branch=warehouse.branch,
                            reference=f'IA-{movement.pk}',
                            entry_date=timezone.localdate(),
                            narration=f'Inventory adjustment {ref_id}: {data["reason"].strip()}',
                            created_by=request.user,
                        )
                        if quantity > 0:
                            JournalEntryLine.objects.create(
                                entry=entry,
                                account=accounts['inventory'],
                                description=f'Inventory increase - {product.name}',
                                debit=value,
                            )
                            JournalEntryLine.objects.create(
                                entry=entry,
                                account=accounts['inventory_adjustment_gain'],
                                description='Inventory adjustment gain',
                                credit=value,
                            )
                        else:
                            JournalEntryLine.objects.create(
                                entry=entry,
                                account=accounts['inventory_adjustment_loss'],
                                description='Inventory adjustment loss',
                                debit=value,
                            )
                            JournalEntryLine.objects.create(
                                entry=entry,
                                account=accounts['inventory'],
                                description=f'Inventory reduction - {product.name}',
                                credit=value,
                            )
                        entry.post()
                    AuditEvent.objects.create(
                        user=request.user,
                        organization=organization,
                        action='stock_adjustment',
                        details=(
                            f'Adjustment {ref_id}: product {product.sku}, warehouse '
                            f'{warehouse.code}, quantity {quantity}, value '
                            f'{movement.valuation_entry.value_change}. Reason: {data["reason"].strip()}'
                        ),
                    )
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
        else:
            messages.success(request, f'Stock adjustment {ref_id} recorded as movement {movement.pk}.')
            return redirect('inventory_home')
    return render(request, 'inventory/adjustment_form.html', {
        'organization': organization,
        'form': form,
        'active_page': 'inventory',
    })


@login_required
def inventory_opening_valuation(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_inventory(request.user, organization):
        return HttpResponseForbidden('You do not have permission to record opening valuations.')
    form = InventoryOpeningForm(request.POST or None, organization=organization, user=request.user)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                ensure_accounting_period_open(organization.pk, timezone.localdate())
                record_opening_valuation(
                    product=data['product'],
                    warehouse=data['warehouse'],
                    quantity=data['quantity'],
                    inventory_value=data['inventory_value'],
                    user=request.user,
                )
        except (IntegrityError, ValidationError) as exc:
            messages.error(request, '; '.join(getattr(exc, 'messages', [str(exc)])))
        else:
            messages.success(
                request,
                'Opening valuation recorded. Review the inventory-to-ledger reconciliation before relying on the balance.',
            )
            return redirect('inventory_valuation')
    return render(request, 'inventory/opening_valuation_form.html', {
        'organization': organization,
        'form': form,
        'active_page': 'inventory',
    })


def _report_date(request):
    raw_date = request.GET.get('as_of', '').strip()
    selected = parse_date(raw_date) if raw_date else timezone.localdate()
    return selected or timezone.localdate()


@login_required
def inventory_valuation(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not request.user.is_superuser and not request.user.organizations_memberships.filter(
        organization=organization,
        is_active=True,
    ).exists():
        return HttpResponseForbidden('You do not have permission to view inventory valuation.')

    as_of = _report_date(request)
    branches = Branch.objects.filter(organization=organization, is_active=True).order_by('name')
    warehouses = Warehouse.objects.filter(organization=organization, is_active=True).select_related('branch')
    if request.user.branch_id:
        branches = branches.filter(pk=request.user.branch_id)
        warehouses = warehouses.filter(branch_id=request.user.branch_id)
    branch = None
    branch_id = request.GET.get('branch', '').strip()
    if branch_id and not request.user.branch_id:
        branch = branches.filter(pk=branch_id).first()
        if branch is None:
            return HttpResponseNotFound('Branch not found.')
        warehouses = warehouses.filter(branch=branch)
    elif request.user.branch_id:
        branch = request.user.branch

    warehouse_options = warehouses
    selected_warehouse = None
    warehouse_id = request.GET.get('warehouse', '').strip()
    if warehouse_id:
        selected_warehouse = warehouses.filter(pk=warehouse_id).first()
        if selected_warehouse is None:
            return HttpResponseNotFound('Warehouse not found.')
        warehouses = warehouses.filter(pk=selected_warehouse.pk)

    search = request.GET.get('q', '').strip()
    balances = valuation_position_queryset(
        organization,
        as_of,
        warehouses=warehouses,
        search=search,
    )

    rows = []
    for balance in balances:
        if balance.has_history_asof:
            quantity = balance.latest_quantity_asof
            value = balance.latest_value_asof
            is_valued = balance.latest_valued_asof
        elif balance.has_movement_asof:
            quantity = balance.ledger_quantity_asof
            value = None
            is_valued = False
        else:
            quantity = Decimal('0')
            value = Decimal('0.00')
            is_valued = True
        unit_cost = None
        if is_valued and quantity:
            unit_cost = (value / quantity).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
        exception = ''
        if quantity < 0:
            exception = 'Negative stock'
        elif not is_valued:
            exception = 'Legacy value or history unknown'
        elif quantity != balance.ledger_quantity_asof:
            exception = 'Valuation quantity differs from stock ledger'
        elif is_valued and quantity and unit_cost == 0:
            exception = 'Explicit zero-cost stock'
        rows.append({
            'product': balance.product,
            'warehouse': balance.warehouse,
            'quantity': quantity,
            'unit_cost': unit_cost,
            'value': value if is_valued else None,
            'is_valued': is_valued,
            'exception': exception,
        })

    history = InventoryValuationEntry.objects.filter(
        organization=organization,
        warehouse__in=warehouses,
        effective_at__date__lte=as_of,
    ).select_related('product', 'warehouse', 'warehouse__branch', 'created_by').order_by('-effective_at', '-pk')
    if search:
        history = history.filter(Q(product__sku__icontains=search) | Q(product__name__icontains=search))
    if request.GET.get('history_csv') == '1':
        if history.count() > 20000:
            return HttpResponse('CSV export is limited to 20,000 valuation events.', status=413)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="inventory-valuation-history-{as_of.isoformat()}.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Posting timestamp', 'SKU', 'Product', 'Warehouse', 'Source', 'Source ID',
            'Quantity change', 'Value change', 'Unit cost', 'Quantity after', 'Value after', 'Valued',
        ])
        for event in history:
            writer.writerow([
                event.effective_at.isoformat(), event.product.sku, event.product.name,
                event.warehouse.code, event.source_model, event.source_id,
                event.quantity_change, event.value_change, event.unit_cost,
                event.quantity_after, event.value_after, event.is_valued_after,
            ])
        return response

    total_value = sum((row['value'] or Decimal('0') for row in rows), Decimal('0.00'))
    unvalued_count = sum(1 for row in rows if not row['is_valued'])
    negative_count = sum(1 for row in rows if row['quantity'] < 0)

    gl_lines = JournalEntryLine.objects.filter(
        entry__organization=organization,
        entry__status='posted',
        entry__created_at__date__lte=as_of,
        account__code='1200',
    )
    if branch:
        gl_lines = gl_lines.filter(entry__branch=branch)
    if selected_warehouse:
        gl_lines = gl_lines.filter(entry__branch=selected_warehouse.branch)
    gl_totals = gl_lines.aggregate(debit=Sum('debit'), credit=Sum('credit'))
    gl_inventory = (gl_totals['debit'] or Decimal('0')) - (gl_totals['credit'] or Decimal('0'))

    receipt_lines = GoodsReceiptLine.objects.filter(
        goods_receipt__organization=organization,
        goods_receipt__status='posted',
        goods_receipt__created_at__date__lte=as_of,
        goods_receipt__warehouse__in=warehouses,
    ).select_related('order_line', 'goods_receipt')
    unbilled_receipt_value = Decimal('0.00')
    for receipt_line in receipt_lines:
        invoiced = PurchaseInvoiceLine.objects.filter(
            receipt_line=receipt_line,
            invoice__status='posted',
            invoice__created_at__date__lte=as_of,
        ).aggregate(quantity=Sum('quantity'))['quantity'] or Decimal('0')
        unbilled_quantity = max(Decimal('0'), receipt_line.quantity - invoiced)
        order_line = receipt_line.order_line
        unit_cost = (
            order_line.quantity * order_line.unit_cost - order_line.discount_amount
        ) / order_line.quantity
        unbilled_receipt_value += round_currency(unbilled_quantity * unit_cost)

    unbilled_return_lines = SupplierReturnLine.objects.filter(
        supplier_return__organization=organization,
        supplier_return__status='posted',
        supplier_return__created_at__date__lte=as_of,
        supplier_return__purchase_invoice__isnull=True,
        supplier_return__warehouse__in=warehouses,
    ).select_related('receipt_line')
    unbilled_return_cost = Decimal('0.00')
    for return_line in unbilled_return_lines:
        if return_line.receipt_line_id is None:
            continue
        billed = PurchaseInvoiceLine.objects.filter(
            receipt_line_id=return_line.receipt_line_id,
            invoice__status='posted',
            invoice__created_at__date__lte=as_of,
        ).aggregate(quantity=Sum('quantity'))['quantity'] or Decimal('0')
        if billed < return_line.receipt_line.quantity:
            unbilled_return_cost += return_line.stock_cost

    from sales.models import DeliveryNote

    pending_deliveries = DeliveryNote.objects.filter(
        organization=organization,
        status='dispatched',
        created_at__date__lte=as_of,
        warehouse__in=warehouses,
    ).filter(Q(invoice__isnull=True) | ~Q(invoice__status='posted')).values_list('pk', flat=True)
    pending_delivery_cost = InventoryValuationEntry.objects.filter(
        source_model='DeliveryNote',
        source_id__in=pending_deliveries,
        effective_at__date__lte=as_of,
    ).aggregate(total=Sum('value_change'))['total'] or Decimal('0')
    pending_delivery_cost = abs(min(Decimal('0'), pending_delivery_cost))
    adjusted_gl = gl_inventory + unbilled_receipt_value - unbilled_return_cost - pending_delivery_cost
    reconciliation_difference = round_currency(total_value - adjusted_gl)
    reconciliation_status = 'Review required'

    unallocated = []
    if not request.user.branch_id:
        products = Product.objects.filter(organization=organization).annotate(
            ledger_quantity=Coalesce(
                Sum('stock_movements__quantity'),
                Value(Decimal('0')),
                output_field=DecimalField(max_digits=18, decimal_places=3),
            )
        ).order_by('sku')
        unallocated = [
            {'product': product, 'quantity': product.stock_quantity - product.ledger_quantity}
            for product in products
            if product.stock_quantity != product.ledger_quantity
        ]
    if reconciliation_difference == 0 and not unvalued_count and not negative_count and not unallocated:
        reconciliation_status = 'Reconciled'

    if request.GET.get('format') == 'csv':
        if len(rows) > 20000:
            return HttpResponse('CSV export is limited to 20,000 valuation positions.', status=413)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="inventory-valuation-{as_of.isoformat()}.csv"'
        writer = csv.writer(response)
        writer.writerow(['As of', as_of.isoformat()])
        writer.writerow(['SKU', 'Product', 'Warehouse', 'Branch', 'Quantity', 'Unit cost', 'Inventory value', 'Status'])
        for row in rows:
            writer.writerow([
                row['product'].sku,
                row['product'].name,
                row['warehouse'].code,
                row['warehouse'].branch.name,
                row['quantity'],
                row['unit_cost'] if row['unit_cost'] is not None else 'Unknown',
                row['value'] if row['value'] is not None else 'Unknown',
                row['exception'] or 'Valued',
            ])
        return response

    return render(request, 'inventory/valuation.html', {
        'organization': organization,
        'rows': Paginator(rows, 25).get_page(request.GET.get('page')),
        'history': Paginator(history, 50).get_page(request.GET.get('history_page')),
        'branches': branches,
        'warehouses': warehouse_options.select_related('branch').order_by('code'),
        'branch': branch,
        'selected_warehouse': selected_warehouse,
        'query': search,
        'as_of': as_of,
        'total_value': total_value,
        'unvalued_count': unvalued_count,
        'negative_count': negative_count,
        'gl_inventory': gl_inventory,
        'unbilled_receipt_value': unbilled_receipt_value,
        'unbilled_return_cost': unbilled_return_cost,
        'pending_delivery_cost': pending_delivery_cost,
        'reconciliation_difference': reconciliation_difference,
        'reconciliation_status': reconciliation_status,
        'unallocated': unallocated,
        'can_manage': _can_manage_inventory(request.user, organization),
        'active_page': 'inventory',
    })

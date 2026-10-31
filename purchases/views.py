from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from accounting.models import round_currency
from banking.permissions import can_manage_banking_operations
from core.organization_context import get_active_organization
from core.permissions import has_organization_role
from .forms import (
    GoodsReceiptForm,
    PurchaseInvoiceForm,
    PurchaseOrderForm,
    ReceiptInvoiceForm,
    calculate_purchase_subtotal,
    purchase_line_formset,
    purchase_order_line_formset,
)
from .models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    PurchaseOrder,
    PurchaseOrderLine,
)


def _organization_for_request(request):
    return get_active_organization(request)


def _can_manage_purchases(user, organization):
    return has_organization_role(
        user, organization,
        {'owner', 'admin', 'purchase_manager', 'purchases', 'accountant', 'finance_manager'},
    )


def _branch_scope(queryset, user):
    return queryset.filter(branch_id=user.branch_id) if user.branch_id else queryset


@login_required
def purchase_order_list(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    orders = _branch_scope(
        PurchaseOrder.objects.filter(organization=organization).select_related('supplier', 'branch'),
        request.user,
    )
    status = request.GET.get('status')
    valid_statuses = {value for value, _ in PurchaseOrder.STATUS_CHOICES}
    if status in valid_statuses:
        orders = orders.filter(status=status)
    query = request.GET.get('q', '').strip()
    if query:
        orders = orders.filter(Q(order_number__icontains=query) | Q(supplier__name__icontains=query))
    return render(request, 'purchases/order_list.html', {
        'organization': organization,
        'orders': Paginator(orders, 25).get_page(request.GET.get('page')),
        'query': query,
        'status_filter': status or '',
        'status_choices': PurchaseOrder.STATUS_CHOICES,
        'can_manage': _can_manage_purchases(request.user, organization),
        'can_manage_banking_operations': can_manage_banking_operations(request.user, organization),
        'active_page': 'purchases',
    })


@login_required
def purchase_order_create(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_purchases(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create purchase orders.')
    if request.method == 'POST':
        form = PurchaseOrderForm(request.POST, organization=organization, user=request.user)
        formset = purchase_order_line_formset(organization=organization, data=request.POST, prefix='lines')
        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    order = form.save(commit=False)
                    order.organization = organization
                    order.branch = form.cleaned_data['branch']
                    order.created_by = request.user
                    order.save()
                    formset.instance = order
                    for line_form in formset.forms:
                        if not line_form.cleaned_data or line_form.cleaned_data.get('DELETE'):
                            continue
                        if not line_form.cleaned_data.get('product'):
                            continue
                        line = line_form.save(commit=False)
                        line.order = order
                        line.discount_amount = line.discount_amount or Decimal('0')
                        line.save()
            except ValidationError as exc:
                for message in exc.messages:
                    form.add_error(None, message)
            except IntegrityError:
                form.add_error('order_number', 'This purchase order number is already in use.')
            else:
                messages.success(request, f'Draft purchase order {order.order_number} created.')
                return redirect('purchase_order_detail', pk=order.pk)
    else:
        form = PurchaseOrderForm(organization=organization, user=request.user)
        formset = purchase_order_line_formset(organization=organization, prefix='lines')
    return render(request, 'purchases/order_form.html', {
        'organization': organization,
        'form': form,
        'formset': formset,
        'order': None,
        'active_page': 'purchases',
    })


@login_required
def purchase_order_edit(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_purchases(request.user, organization):
        return HttpResponseForbidden('You do not have permission to edit purchase orders.')
    orders = _branch_scope(
        PurchaseOrder.objects.filter(organization=organization, status='draft').select_related('supplier', 'branch'),
        request.user,
    )
    order = get_object_or_404(orders, pk=pk)
    if request.method == 'POST':
        form = PurchaseOrderForm(request.POST, organization=organization, user=request.user, instance=order)
        formset = purchase_order_line_formset(
            organization=organization,
            data=request.POST,
            instance=order,
            prefix='lines',
        )
        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    order = form.save(commit=False)
                    order.organization = organization
                    order.branch = form.cleaned_data['branch']
                    order.save()
                    formset.instance = order
                    for line_form in formset.forms:
                        if not line_form.cleaned_data:
                            continue
                        line = line_form.instance
                        if line_form.cleaned_data.get('DELETE'):
                            if line.pk:
                                line.delete()
                            continue
                        if not line_form.cleaned_data.get('product'):
                            continue
                        line = line_form.save(commit=False)
                        line.order = order
                        line.discount_amount = line.discount_amount or Decimal('0')
                        line.save()
            except ValidationError as exc:
                for message in exc.messages:
                    form.add_error(None, message)
            except IntegrityError:
                form.add_error('order_number', 'This purchase order number is already in use.')
            else:
                messages.success(request, f'Draft purchase order {order.order_number} updated.')
                return redirect('purchase_order_detail', pk=order.pk)
    else:
        form = PurchaseOrderForm(organization=organization, user=request.user, instance=order)
        formset = purchase_order_line_formset(organization=organization, instance=order, prefix='lines')
    return render(request, 'purchases/order_form.html', {
        'organization': organization,
        'form': form,
        'formset': formset,
        'order': order,
        'active_page': 'purchases',
    })


@login_required
def purchase_order_detail(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    orders = _branch_scope(
        PurchaseOrder.objects.filter(organization=organization).select_related('supplier', 'branch'),
        request.user,
    )
    order = get_object_or_404(orders, pk=pk)
    can_manage = _can_manage_purchases(request.user, organization)
    if request.method == 'POST':
        if not can_manage:
            return HttpResponseForbidden('You do not have permission to change purchase orders.')
        action = request.POST.get('action')
        try:
            if action == 'confirm':
                order.confirm(user=request.user)
                messages.success(request, f'Purchase order {order.order_number} confirmed.')
            elif action == 'cancel':
                order.cancel(user=request.user)
                messages.success(request, f'Purchase order {order.order_number} cancelled.')
            else:
                return HttpResponseForbidden('Unknown purchase order action.')
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            return redirect('purchase_order_detail', pk=order.pk)

    line_rows = []
    for line in order.lines.select_related('product').all():
        received = GoodsReceiptLine.objects.filter(
            order_line=line,
            goods_receipt__status='posted',
        ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
        line_rows.append({'line': line, 'received': received, 'remaining': max(Decimal('0'), line.quantity - received)})
    receipts = order.goods_receipts.select_related('warehouse').all()
    return render(request, 'purchases/order_detail.html', {
        'organization': organization,
        'order': order,
        'line_rows': line_rows,
        'receipts': receipts,
        'can_manage': can_manage,
        'active_page': 'purchases',
    })


@login_required
def goods_receipt_create(request, order_pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_purchases(request.user, organization):
        return HttpResponseForbidden('You do not have permission to receive purchase orders.')
    orders = _branch_scope(
        PurchaseOrder.objects.filter(organization=organization, status__in=['confirmed', 'partially_received'])
        .select_related('supplier', 'branch'),
        request.user,
    )
    order = get_object_or_404(orders, pk=order_pk)
    if request.method == 'POST':
        form = GoodsReceiptForm(request.POST, order=order, organization=organization)
        if form.is_valid():
            try:
                with transaction.atomic():
                    receipt = GoodsReceipt.objects.create(
                        organization=organization,
                        branch=order.branch,
                        warehouse=form.cleaned_data['warehouse'],
                        order=order,
                        receipt_number=form.cleaned_data['receipt_number'],
                        receipt_date=form.cleaned_data['receipt_date'],
                        created_by=request.user,
                    )
                    for line in form.order_lines:
                        quantity = form.cleaned_data.get(f'quantity_{line.pk}') or Decimal('0')
                        if quantity:
                            GoodsReceiptLine.objects.create(
                                goods_receipt=receipt,
                                order_line=line,
                                quantity=quantity,
                            )
            except ValidationError as exc:
                for message in exc.messages:
                    form.add_error(None, message)
            except IntegrityError:
                form.add_error('receipt_number', 'This goods receipt number is already in use.')
            else:
                messages.success(request, f'Draft goods receipt {receipt.receipt_number} created.')
                return redirect('goods_receipt_detail', pk=receipt.pk)
    else:
        form = GoodsReceiptForm(order=order, organization=organization)
    return render(request, 'purchases/receipt_form.html', {
        'organization': organization,
        'order': order,
        'form': form,
        'line_fields': [
            (line, form[f'quantity_{line.pk}'])
            for line in form.order_lines
        ],
        'active_page': 'purchases',
    })


@login_required
def goods_receipt_edit(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_purchases(request.user, organization):
        return HttpResponseForbidden('You do not have permission to edit goods receipts.')
    receipts = _branch_scope(
        GoodsReceipt.objects.filter(organization=organization, status='draft').select_related('order__supplier'),
        request.user,
    )
    receipt = get_object_or_404(receipts, pk=pk)
    if receipt.order.status not in {'confirmed', 'partially_received'}:
        return HttpResponseForbidden('This purchase order cannot accept a goods receipt.')
    order = receipt.order
    if request.method == 'POST':
        form = GoodsReceiptForm(request.POST, order=order, organization=organization, receipt=receipt)
        if form.is_valid():
            try:
                with transaction.atomic():
                    receipt.receipt_number = form.cleaned_data['receipt_number']
                    receipt.receipt_date = form.cleaned_data['receipt_date']
                    receipt.warehouse = form.cleaned_data['warehouse']
                    receipt.save()
                    receipt.lines.all().delete()
                    for line in form.order_lines:
                        quantity = form.cleaned_data.get(f'quantity_{line.pk}') or Decimal('0')
                        if quantity:
                            GoodsReceiptLine.objects.create(
                                goods_receipt=receipt,
                                order_line=line,
                                quantity=quantity,
                            )
            except ValidationError as exc:
                for message in exc.messages:
                    form.add_error(None, message)
            except IntegrityError:
                form.add_error('receipt_number', 'This goods receipt number is already in use.')
            else:
                messages.success(request, f'Draft goods receipt {receipt.receipt_number} updated.')
                return redirect('goods_receipt_detail', pk=receipt.pk)
    else:
        form = GoodsReceiptForm(order=order, organization=organization, receipt=receipt)
    return render(request, 'purchases/receipt_form.html', {
        'organization': organization,
        'order': order,
        'receipt': receipt,
        'form': form,
        'line_fields': [(line, form[f'quantity_{line.pk}']) for line in form.order_lines],
        'active_page': 'purchases',
    })


@login_required
def goods_receipt_detail(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    receipts = _branch_scope(
        GoodsReceipt.objects.filter(organization=organization).select_related(
            'order__supplier', 'branch', 'warehouse'
        ),
        request.user,
    )
    receipt = get_object_or_404(receipts, pk=pk)
    can_manage = _can_manage_purchases(request.user, organization)
    if request.method == 'POST':
        if not can_manage:
            return HttpResponseForbidden('You do not have permission to post goods receipts.')
        if request.POST.get('action') != 'post':
            return HttpResponseForbidden('Unknown goods receipt action.')
        try:
            receipt.post()
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            messages.success(request, f'Goods receipt {receipt.receipt_number} posted to stock.')
            return redirect('goods_receipt_detail', pk=receipt.pk)

    line_rows = []
    has_uninvoiced = False
    for line in receipt.lines.select_related('order_line__product').all():
        invoiced = PurchaseInvoiceLine.objects.filter(
            receipt_line=line,
            invoice__status='posted',
        ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
        remaining = max(Decimal('0'), line.quantity - invoiced)
        has_uninvoiced = has_uninvoiced or remaining > 0
        line_rows.append({'line': line, 'invoiced': invoiced, 'remaining': remaining})
    return render(request, 'purchases/receipt_detail.html', {
        'organization': organization,
        'receipt': receipt,
        'line_rows': line_rows,
        'has_uninvoiced': has_uninvoiced,
        'can_manage': can_manage,
        'active_page': 'purchases',
    })


@login_required
def purchase_invoice_from_receipt(request, receipt_pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_purchases(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create purchase invoices.')
    receipts = _branch_scope(
        GoodsReceipt.objects.filter(organization=organization, status='posted').select_related(
            'order__supplier', 'branch'
        ),
        request.user,
    )
    receipt = get_object_or_404(receipts, pk=receipt_pk)
    form = ReceiptInvoiceForm(request.POST or None, receipt=receipt)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                invoice = PurchaseInvoice.objects.create(
                    organization=organization,
                    branch=receipt.branch,
                    supplier=receipt.order.supplier,
                    invoice_number=form.cleaned_data['invoice_number'],
                    invoice_date=form.cleaned_data['invoice_date'],
                    due_date=form.cleaned_data['due_date'],
                    place_of_supply=form.cleaned_data['place_of_supply'].strip().upper(),
                    subtotal=Decimal('0.00'),
                    tax_amount=Decimal('0.00'),
                    total_amount=Decimal('0.00'),
                )
                subtotal = Decimal('0')
                for receipt_line in form.receipt_lines:
                    prefix = f'line_{receipt_line.pk}'
                    quantity = form.cleaned_data.get(f'{prefix}_quantity') or Decimal('0')
                    if not quantity:
                        continue
                    unit_cost = form.cleaned_data[f'{prefix}_unit_cost']
                    discount = form.cleaned_data.get(f'{prefix}_discount') or Decimal('0')
                    line_total = round_currency(quantity * unit_cost)
                    PurchaseInvoiceLine.objects.create(
                        invoice=invoice,
                        product=receipt_line.order_line.product,
                        receipt_line=receipt_line,
                        purchase_order_line=receipt_line.order_line,
                        quantity=quantity,
                        unit_cost=unit_cost,
                        tax_rate=Decimal('0'),
                        discount_amount=discount,
                        line_total=line_total,
                    )
                    subtotal += line_total - discount
                invoice.subtotal = round_currency(subtotal)
                invoice.total_amount = invoice.subtotal
                invoice.save(update_fields=['subtotal', 'total_amount'])
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
        except IntegrityError:
            form.add_error('invoice_number', 'This invoice number is already in use.')
        else:
            messages.success(request, f'Draft purchase invoice {invoice.invoice_number} created from receipt {receipt.receipt_number}.')
            return redirect('purchase_invoice_detail', pk=invoice.pk)
    invoice_line_fields = []
    for line in form.receipt_lines:
        prefix = f'line_{line.pk}'
        invoice_line_fields.append({
            'line': line,
            'quantity': form[f'{prefix}_quantity'],
            'unit_cost': form[f'{prefix}_unit_cost'],
            'discount': form[f'{prefix}_discount'],
        })
    return render(request, 'purchases/receipt_invoice_form.html', {
        'organization': organization,
        'receipt': receipt,
        'form': form,
        'line_fields': invoice_line_fields,
        'active_page': 'purchases',
    })


@login_required
def purchase_invoice_list(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    invoices = PurchaseInvoice.objects.filter(organization=organization).select_related(
        'supplier', 'branch'
    )
    if request.user.branch_id:
        invoices = invoices.filter(branch_id=request.user.branch_id)
    status = request.GET.get('status')
    if status in {'draft', 'posted', 'cancelled'}:
        invoices = invoices.filter(status=status)
    query = request.GET.get('q', '').strip()
    if query:
        invoices = invoices.filter(
            Q(invoice_number__icontains=query) | Q(supplier__name__icontains=query)
        )
    return render(request, 'purchases/invoice_list.html', {
        'organization': organization,
        'invoices': Paginator(invoices, 25).get_page(request.GET.get('page')),
        'query': query,
        'status_filter': status or '',
        'can_manage': _can_manage_purchases(request.user, organization),
        'active_page': 'purchases',
    })


@login_required
def purchase_invoice_create(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_purchases(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create purchase invoices.')
    if request.method == 'POST':
        form = PurchaseInvoiceForm(request.POST, organization=organization, user=request.user)
        formset = purchase_line_formset(
            organization=organization,
            data=request.POST,
            prefix='lines',
        )
        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    invoice = form.save(commit=False)
                    invoice.organization = organization
                    invoice.branch = form.cleaned_data.get('branch')
                    invoice.subtotal = calculate_purchase_subtotal(formset)
                    invoice.tax_amount = Decimal('0.00')
                    invoice.total_amount = invoice.subtotal
                    invoice.save()
                    formset.instance = invoice
                    for line_form in formset.forms:
                        if (
                            not line_form.cleaned_data
                            or line_form.cleaned_data.get('DELETE')
                            or not line_form.cleaned_data.get('product')
                        ):
                            continue
                        line = line_form.save(commit=False)
                        line.invoice = invoice
                        line.tax_rate = Decimal('0')
                        line.line_total = round_currency(line.quantity * line.unit_cost)
                        line.save()
            except ValidationError as exc:
                for message in exc.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, f'Draft purchase invoice {invoice.invoice_number} created.')
                return redirect('purchase_invoice_list')
    else:
        form = PurchaseInvoiceForm(organization=organization, user=request.user)
        formset = purchase_line_formset(organization=organization, prefix='lines')
    return render(request, 'purchases/invoice_form.html', {
        'organization': organization,
        'form': form,
        'formset': formset,
        'active_page': 'purchases',
    })


@login_required
def purchase_invoice_detail(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    invoices = PurchaseInvoice.objects.filter(organization=organization)
    if request.user.branch_id:
        invoices = invoices.filter(branch_id=request.user.branch_id)
    invoice = get_object_or_404(invoices.select_related('supplier', 'branch'), pk=pk)
    if request.method == 'POST':
        if not _can_manage_purchases(request.user, organization):
            return HttpResponseForbidden('You do not have permission to post purchase invoices.')
        if request.POST.get('action') != 'post':
            return HttpResponseForbidden('Unknown purchase invoice action.')
        try:
            invoice.post()
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            messages.success(request, f'Purchase invoice {invoice.invoice_number} posted.')
            return redirect('purchase_invoice_detail', pk=invoice.pk)
    return render(request, 'purchases/invoice_detail.html', {
        'organization': organization,
        'invoice': invoice,
        'lines': invoice.lines.select_related('product').all(),
        'can_manage': _can_manage_purchases(request.user, organization),
        'active_page': 'purchases',
    })

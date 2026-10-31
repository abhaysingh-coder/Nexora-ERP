from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from accounting.models import round_currency
from banking.permissions import can_manage_banking_operations
from core.organization_context import get_active_organization
from core.permissions import has_organization_role
from .forms import (
    DeliveryInvoiceForm,
    DeliveryNoteForm,
    SalesInvoiceForm,
    SalesOrderForm,
    calculate_sales_subtotal,
    sales_line_formset,
    sales_order_line_formset,
)
from .models import (
    DeliveryNote,
    DeliveryNoteLine,
    SalesInvoice,
    SalesInvoiceLine,
    SalesOrder,
)


def _organization_for_request(request):
    return get_active_organization(request)


def _can_manage_sales(user, organization):
    return has_organization_role(
        user, organization,
        {'owner', 'admin', 'sales_manager', 'sales', 'accountant', 'finance_manager'},
    )


@login_required
def sales_invoice_list(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    invoices = SalesInvoice.objects.filter(organization=organization).select_related(
        'customer', 'branch'
    )
    if request.user.branch_id:
        invoices = invoices.filter(branch_id=request.user.branch_id)
    status = request.GET.get('status')
    if status in {'draft', 'posted', 'cancelled'}:
        invoices = invoices.filter(status=status)
    query = request.GET.get('q', '').strip()
    if query:
        invoices = invoices.filter(
            Q(invoice_number__icontains=query) | Q(customer__name__icontains=query)
        )
    return render(request, 'sales/invoice_list.html', {
        'organization': organization,
        'invoices': Paginator(invoices, 25).get_page(request.GET.get('page')),
        'query': query,
        'status_filter': status or '',
        'can_manage': _can_manage_sales(request.user, organization),
        'can_manage_banking_operations': can_manage_banking_operations(request.user, organization),
        'active_page': 'sales',
    })


@login_required
def sales_invoice_create(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_sales(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create sales invoices.')
    if request.method == 'POST':
        form = SalesInvoiceForm(request.POST, organization=organization, user=request.user)
        formset = sales_line_formset(
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
                    invoice.subtotal = calculate_sales_subtotal(formset)
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
                        line.unit_cost = line.product.cost_price
                        line.tax_rate = Decimal('0')
                        line.line_total = round_currency(line.quantity * line.unit_price)
                        line.save()
            except ValidationError as exc:
                for message in exc.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, f'Draft sales invoice {invoice.invoice_number} created.')
                return redirect('sales_invoice_list')
    else:
        form = SalesInvoiceForm(organization=organization, user=request.user)
        formset = sales_line_formset(organization=organization, prefix='lines')
    return render(request, 'sales/invoice_form.html', {
        'organization': organization,
        'form': form,
        'formset': formset,
        'active_page': 'sales',
    })


@login_required
def sales_invoice_detail(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    invoices = SalesInvoice.objects.filter(organization=organization)
    if request.user.branch_id:
        invoices = invoices.filter(branch_id=request.user.branch_id)
    invoice = get_object_or_404(invoices.select_related('customer', 'branch'), pk=pk)
    if request.method == 'POST':
        if not _can_manage_sales(request.user, organization):
            return HttpResponseForbidden('You do not have permission to post sales invoices.')
        if request.POST.get('action') != 'post':
            return HttpResponseForbidden('Unknown sales invoice action.')
        try:
            invoice.post()
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            messages.success(request, f'Sales invoice {invoice.invoice_number} posted.')
            return redirect('sales_invoice_detail', pk=invoice.pk)
    return render(request, 'sales/invoice_detail.html', {
        'organization': organization,
        'invoice': invoice,
        'lines': invoice.lines.select_related('product').all(),
        'can_manage': _can_manage_sales(request.user, organization),
        'active_page': 'sales',
    })


@login_required
def sales_order_list(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    orders = SalesOrder.objects.filter(organization=organization).select_related('customer', 'branch')
    if request.user.branch_id:
        orders = orders.filter(branch_id=request.user.branch_id)
    status = request.GET.get('status')
    if status in {'draft', 'confirmed', 'fulfilled', 'cancelled'}:
        orders = orders.filter(status=status)
    query = request.GET.get('q', '').strip()
    if query:
        orders = orders.filter(Q(order_number__icontains=query) | Q(customer__name__icontains=query))
    return render(request, 'sales/order_list.html', {
        'organization': organization,
        'orders': Paginator(orders, 25).get_page(request.GET.get('page')),
        'query': query,
        'status_filter': status or '',
        'status_choices': SalesOrder.STATUS_CHOICES,
        'can_manage': _can_manage_sales(request.user, organization),
        'active_page': 'sales',
    })


@login_required
def sales_order_create(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_sales(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create sales orders.')
    if request.method == 'POST':
        form = SalesOrderForm(request.POST, organization=organization, user=request.user)
        formset = sales_order_line_formset(
            organization=organization,
            data=request.POST,
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
                        if (
                            not line_form.cleaned_data
                            or line_form.cleaned_data.get('DELETE')
                            or not line_form.cleaned_data.get('product')
                        ):
                            continue
                        line = line_form.save(commit=False)
                        line.order = order
                        line.save()
            except ValidationError as exc:
                for message in exc.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, f'Draft sales order {order.order_number} created.')
                return redirect('sales_order_detail', pk=order.pk)
    else:
        form = SalesOrderForm(organization=organization, user=request.user)
        formset = sales_order_line_formset(organization=organization, prefix='lines')
    return render(request, 'sales/order_form.html', {
        'organization': organization,
        'form': form,
        'formset': formset,
        'active_page': 'sales',
    })


@login_required
def sales_order_detail(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    orders = SalesOrder.objects.filter(organization=organization)
    if request.user.branch_id:
        orders = orders.filter(branch_id=request.user.branch_id)
    order = get_object_or_404(orders.select_related('customer', 'branch'), pk=pk)
    if request.method == 'POST':
        if not _can_manage_sales(request.user, organization):
            return HttpResponseForbidden('You do not have permission to confirm sales orders.')
        action = request.POST.get('action')
        if action not in {'confirm', 'cancel'}:
            return HttpResponseForbidden('Unknown sales order action.')
        try:
            if action == 'confirm':
                order.confirm(user=request.user)
            else:
                order.cancel(user=request.user)
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            messages.success(request, f'Sales order {order.order_number} {order.status}.')
            return redirect('sales_order_detail', pk=order.pk)
    return render(request, 'sales/order_detail.html', {
        'organization': organization,
        'order': order,
        'lines': order.lines.select_related('product'),
        'deliveries': order.delivery_notes.all(),
        'can_manage': _can_manage_sales(request.user, organization),
        'active_page': 'sales',
    })


@login_required
def sales_delivery_create(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_sales(request.user, organization):
        return HttpResponseForbidden('You do not have permission to dispatch sales orders.')
    orders = SalesOrder.objects.filter(organization=organization)
    if request.user.branch_id:
        orders = orders.filter(branch_id=request.user.branch_id)
    order = get_object_or_404(orders, pk=pk)
    if order.status not in {'confirmed', 'fulfilled'}:
        messages.error(request, 'Only confirmed orders can be delivered.')
        return redirect('sales_order_detail', pk=order.pk)
    form = DeliveryNoteForm(request.POST or None, order=order, organization=organization)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                delivery = DeliveryNote.objects.create(
                    organization=organization,
                    branch=order.branch,
                    warehouse=form.cleaned_data['warehouse'],
                    order=order,
                    delivery_number=form.cleaned_data['delivery_number'],
                    delivery_date=form.cleaned_data['delivery_date'],
                    created_by=request.user,
                )
                for order_line in form.order_lines:
                    quantity = form.cleaned_data.get(f'quantity_{order_line.pk}') or Decimal('0')
                    if quantity:
                        DeliveryNoteLine.objects.create(
                            delivery_note=delivery,
                            order_line=order_line,
                            quantity=quantity,
                        )
                delivery.post()
        except IntegrityError:
            form.add_error('delivery_number', 'This delivery number was claimed by another request.')
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
        else:
            messages.success(request, f'Delivery {delivery.delivery_number} dispatched.')
            return redirect('sales_order_detail', pk=order.pk)
    return render(request, 'sales/delivery_form.html', {
        'organization': organization,
        'order': order,
        'form': form,
        'active_page': 'sales',
    })


@login_required
def sales_invoice_from_delivery(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_sales(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create sales invoices.')
    deliveries = DeliveryNote.objects.filter(organization=organization, status='dispatched')
    if request.user.branch_id:
        deliveries = deliveries.filter(branch_id=request.user.branch_id)
    delivery = get_object_or_404(deliveries.select_related('order__customer', 'branch'), pk=pk)
    existing_invoice = SalesInvoice.objects.filter(delivery_note=delivery).first()
    if existing_invoice:
        return redirect('sales_invoice_detail', pk=existing_invoice.pk)
    form = DeliveryInvoiceForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                invoice = SalesInvoice.objects.create(
                    organization=organization,
                    branch=delivery.branch,
                    customer=delivery.order.customer,
                    delivery_note=delivery,
                    invoice_number=form.cleaned_data['invoice_number'],
                    invoice_date=form.cleaned_data['invoice_date'],
                    due_date=form.cleaned_data['due_date'],
                    place_of_supply=form.cleaned_data['place_of_supply'],
                    subtotal=Decimal('0.00'),
                    total_amount=Decimal('0.00'),
                )
                subtotal = Decimal('0')
                for delivery_line in delivery.lines.select_related('order_line__product'):
                    order_line = delivery_line.order_line
                    amount = round_currency(delivery_line.quantity * order_line.unit_price)
                    SalesInvoiceLine.objects.create(
                        invoice=invoice,
                        product=order_line.product,
                        quantity=delivery_line.quantity,
                        unit_cost=order_line.product.cost_price,
                        unit_price=order_line.unit_price,
                        discount_amount=delivery_line.discount_amount,
                        line_total=amount,
                    )
                    subtotal += amount - delivery_line.discount_amount
                invoice.subtotal = round_currency(subtotal)
                invoice.total_amount = invoice.subtotal
                invoice.save(update_fields=['subtotal', 'total_amount'])
        except IntegrityError:
            existing_invoice = SalesInvoice.objects.filter(delivery_note=delivery).first()
            if existing_invoice:
                return redirect('sales_invoice_detail', pk=existing_invoice.pk)
            form.add_error('invoice_number', 'This invoice number was claimed by another request.')
        except ValidationError as exc:
            for message in exc.messages:
                form.add_error(None, message)
        else:
            messages.success(request, f'Draft sales invoice {invoice.invoice_number} created from delivery.')
            return redirect('sales_invoice_detail', pk=invoice.pk)
    return render(request, 'sales/delivery_invoice_form.html', {
        'organization': organization,
        'delivery': delivery,
        'form': form,
        'active_page': 'sales',
    })

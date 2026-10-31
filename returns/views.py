from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from inventory.models import StockMovement
from inventory.models import Warehouse
from purchases.models import GoodsReceipt, PurchaseInvoice
from sales.models import SalesInvoice

from .forms import CustomerReturnForm, SupplierReturnForm
from core.organization_context import get_active_organization
from core.permissions import has_organization_role
from .models import CustomerReturn, SalesCreditNote, SupplierDebitNote, SupplierReturn


def _organization_for_request(request):
    return get_active_organization(request)


def _can_manage_sales(user, organization):
    return has_organization_role(
        user, organization,
        {'owner', 'admin', 'sales_manager', 'sales', 'accountant', 'finance_manager'},
    )


def _can_manage_purchases(user, organization):
    return has_organization_role(
        user, organization,
        {'owner', 'admin', 'purchase_manager', 'purchases', 'accountant', 'finance_manager'},
    )


def _branch_scope(queryset, user):
    return queryset.filter(branch_id=user.branch_id) if user.branch_id else queryset


def _messages_from_validation(request, exc):
    for message in exc.messages:
        messages.error(request, message)


@login_required
def customer_return_list(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    returns = _branch_scope(
        CustomerReturn.objects.filter(organization=organization).select_related('invoice__customer', 'branch'),
        request.user,
    )
    status = request.GET.get('status')
    if status in {value for value, _ in CustomerReturn.STATUS_CHOICES}:
        returns = returns.filter(status=status)
    query = request.GET.get('q', '').strip()
    if query:
        returns = returns.filter(
            Q(return_number__icontains=query)
            | Q(reference__icontains=query)
            | Q(invoice__invoice_number__icontains=query)
            | Q(invoice__customer__name__icontains=query)
        )
    return render(request, 'returns/customer_return_list.html', {
        'organization': organization,
        'returns': Paginator(returns, 25).get_page(request.GET.get('page')),
        'query': query,
        'status_filter': status or '',
        'status_choices': CustomerReturn.STATUS_CHOICES,
        'can_manage': _can_manage_sales(request.user, organization),
        'active_page': 'returns',
    })


@login_required
def supplier_return_list(request):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    returns = _branch_scope(
        SupplierReturn.objects.filter(organization=organization).select_related(
            'supplier', 'branch', 'goods_receipt', 'purchase_invoice'
        ),
        request.user,
    )
    status = request.GET.get('status')
    if status in {value for value, _ in SupplierReturn.STATUS_CHOICES}:
        returns = returns.filter(status=status)
    query = request.GET.get('q', '').strip()
    if query:
        returns = returns.filter(
            Q(return_number__icontains=query)
            | Q(reference__icontains=query)
            | Q(supplier__name__icontains=query)
            | Q(goods_receipt__receipt_number__icontains=query)
            | Q(purchase_invoice__invoice_number__icontains=query)
        )
    return render(request, 'returns/supplier_return_list.html', {
        'organization': organization,
        'returns': Paginator(returns, 25).get_page(request.GET.get('page')),
        'query': query,
        'status_filter': status or '',
        'status_choices': SupplierReturn.STATUS_CHOICES,
        'can_manage': _can_manage_purchases(request.user, organization),
        'active_page': 'returns',
    })


@login_required
def customer_return_create(request, invoice_pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_sales(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create customer returns.')
    invoices = _branch_scope(SalesInvoice.objects.filter(organization=organization, status='posted'), request.user)
    invoice = get_object_or_404(invoices.select_related('customer', 'branch'), pk=invoice_pk)
    return _customer_return_form(request, organization, invoice)


def _customer_return_form(request, organization, invoice, record=None):
    if request.method == 'POST':
        form = CustomerReturnForm(
            request.POST, invoice=invoice, organization=organization, user=request.user, record=record
        )
        if form.is_valid():
            try:
                with transaction.atomic():
                    if record is None:
                        record = CustomerReturn(
                            organization=organization,
                            branch=invoice.branch,
                            invoice=invoice,
                            created_by=request.user,
                            return_number=form.cleaned_data['return_number'],
                            return_date=form.cleaned_data['return_date'],
                        )
                    else:
                        record.return_number = form.cleaned_data['return_number']
                        record.return_date = form.cleaned_data['return_date']
                    record.reference = form.cleaned_data.get('reference', '').strip()
                    record.notes = form.cleaned_data.get('notes', '')
                    record.warehouse = form.cleaned_data.get('warehouse')
                    record.save()
                    form.save_lines(record)
            except ValidationError as exc:
                _messages_from_validation(request, exc)
            except IntegrityError:
                form.add_error('return_number', 'This customer return number is already in use.')
            else:
                messages.success(request, f'Draft customer return {record.return_number} saved.')
                return redirect('customer_return_detail', pk=record.pk)
    else:
        form = CustomerReturnForm(invoice=invoice, organization=organization, user=request.user, record=record)
    return render(request, 'returns/customer_return_form.html', {
        'organization': organization,
        'form': form,
        'invoice': invoice,
        'return_record': record,
        'source_lines': form.invoice_lines,
        'line_fields': [
            {
                'line': line,
                'quantity': form[f'quantity_{line.pk}'],
                'reason': form[f'reason_{line.pk}'],
                'condition': form[f'condition_{line.pk}'],
                'restock': form[f'restock_{line.pk}'],
            }
            for line in form.invoice_lines
        ],
        'active_page': 'returns',
    })


@login_required
def customer_return_edit(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_sales(request.user, organization):
        return HttpResponseForbidden('You do not have permission to edit customer returns.')
    record = get_object_or_404(
        _branch_scope(CustomerReturn.objects.filter(organization=organization, status='draft'), request.user)
        .select_related('invoice__customer', 'invoice__branch'),
        pk=pk,
    )
    return _customer_return_form(request, organization, record.invoice, record)


@login_required
def customer_return_detail(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    record = get_object_or_404(
        _branch_scope(CustomerReturn.objects.filter(organization=organization), request.user)
        .select_related('invoice__customer', 'branch', 'warehouse'),
        pk=pk,
    )
    if request.method == 'POST':
        if not _can_manage_sales(request.user, organization):
            return HttpResponseForbidden('You do not have permission to post or cancel customer returns.')
        action = request.POST.get('action')
        try:
            if action == 'post':
                record.post(user=request.user)
            elif action == 'cancel':
                record.cancel(user=request.user)
            else:
                return HttpResponseForbidden('Unknown customer return action.')
        except ValidationError as exc:
            _messages_from_validation(request, exc)
        else:
            messages.success(request, f'Customer return {record.return_number} {"posted" if action == "post" else "cancelled"}.')
            return redirect('customer_return_detail', pk=record.pk)
    note = SalesCreditNote.objects.filter(customer_return=record).select_related('journal_entry').first()
    movements = StockMovement.objects.filter(reference_model='CustomerReturn', reference_id=record.pk).select_related('product', 'warehouse')
    return render(request, 'returns/customer_return_detail.html', {
        'organization': organization,
        'return_record': record,
        'lines': record.lines.select_related('invoice_line__product').all(),
        'credit_note': note,
        'movements': movements,
        'can_manage': _can_manage_sales(request.user, organization),
        'active_page': 'returns',
    })


@login_required
def supplier_return_create_from_receipt(request, receipt_pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_purchases(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create supplier returns.')
    receipts = _branch_scope(GoodsReceipt.objects.filter(organization=organization, status='posted'), request.user)
    receipt = get_object_or_404(receipts.select_related('branch', 'warehouse', 'order__supplier'), pk=receipt_pk)
    return _supplier_return_form(request, organization, receipt)


@login_required
def supplier_return_create_from_invoice(request, invoice_pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_purchases(request.user, organization):
        return HttpResponseForbidden('You do not have permission to create supplier returns.')
    invoices = _branch_scope(PurchaseInvoice.objects.filter(organization=organization, status='posted'), request.user)
    invoice = get_object_or_404(invoices.select_related('branch', 'supplier'), pk=invoice_pk)
    return _supplier_return_form(request, organization, invoice)


def _supplier_return_form(request, organization, source, record=None):
    if request.method == 'POST':
        form = SupplierReturnForm(request.POST, source=source, organization=organization, user=request.user, record=record)
        if form.is_valid():
            try:
                with transaction.atomic():
                    warehouse = form.cleaned_data['warehouse']
                    branch = source.branch if source.branch_id else warehouse.branch
                    if record is None:
                        record = SupplierReturn(
                            organization=organization,
                            branch=branch,
                            supplier=source.order.supplier if isinstance(source, GoodsReceipt) else source.supplier,
                            goods_receipt=source if isinstance(source, GoodsReceipt) else None,
                            purchase_invoice=source if isinstance(source, PurchaseInvoice) else None,
                            warehouse=warehouse,
                            created_by=request.user,
                            return_number=form.cleaned_data['return_number'],
                            return_date=form.cleaned_data['return_date'],
                        )
                    else:
                        record.return_number = form.cleaned_data['return_number']
                        record.return_date = form.cleaned_data['return_date']
                        record.warehouse = warehouse
                        record.branch = branch
                    record.reference = form.cleaned_data.get('reference', '').strip()
                    record.notes = form.cleaned_data.get('notes', '')
                    record.save()
                    form.save_lines(record)
            except ValidationError as exc:
                _messages_from_validation(request, exc)
            except IntegrityError:
                form.add_error('return_number', 'This supplier return number is already in use.')
            else:
                messages.success(request, f'Draft supplier return {record.return_number} saved.')
                return redirect('supplier_return_detail', pk=record.pk)
    else:
        form = SupplierReturnForm(source=source, organization=organization, user=request.user, record=record)
    return render(request, 'returns/supplier_return_form.html', {
        'organization': organization,
        'form': form,
        'source': source,
        'source_lines': form.return_lines,
        'line_fields': [
            {
                'line': row[0],
                'product': row[1],
                'remaining': row[2],
                'quantity': form[f'quantity_{row[0].pk}'],
                'reason': form[f'reason_{row[0].pk}'],
                'condition': form[f'condition_{row[0].pk}'],
            }
            for row in form.return_lines
        ],
        'return_record': record,
        'active_page': 'returns',
    })


@login_required
def supplier_return_edit(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_purchases(request.user, organization):
        return HttpResponseForbidden('You do not have permission to edit supplier returns.')
    record = get_object_or_404(
        _branch_scope(SupplierReturn.objects.filter(organization=organization, status='draft'), request.user)
        .select_related('goods_receipt__branch', 'goods_receipt__warehouse', 'goods_receipt__order__supplier',
                        'purchase_invoice__branch', 'purchase_invoice__supplier'),
        pk=pk,
    )
    source = record.goods_receipt or record.purchase_invoice
    return _supplier_return_form(request, organization, source, record)


@login_required
def supplier_return_detail(request, pk):
    organization = _organization_for_request(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    record = get_object_or_404(
        _branch_scope(SupplierReturn.objects.filter(organization=organization), request.user)
        .select_related('supplier', 'branch', 'warehouse', 'goods_receipt', 'purchase_invoice'),
        pk=pk,
    )
    if request.method == 'POST':
        if not _can_manage_purchases(request.user, organization):
            return HttpResponseForbidden('You do not have permission to post or cancel supplier returns.')
        action = request.POST.get('action')
        try:
            if action == 'post':
                record.post(user=request.user)
            elif action == 'cancel':
                record.cancel(user=request.user)
            else:
                return HttpResponseForbidden('Unknown supplier return action.')
        except ValidationError as exc:
            _messages_from_validation(request, exc)
        else:
            messages.success(request, f'Supplier return {record.return_number} {"posted" if action == "post" else "cancelled"}.')
            return redirect('supplier_return_detail', pk=record.pk)
    note = SupplierDebitNote.objects.filter(supplier_return=record).select_related('journal_entry').first()
    movements = StockMovement.objects.filter(reference_model='SupplierReturn', reference_id=record.pk).select_related('product', 'warehouse')
    return render(request, 'returns/supplier_return_detail.html', {
        'organization': organization,
        'return_record': record,
        'lines': record.lines.select_related('receipt_line__order_line__product', 'invoice_line__product').all(),
        'debit_note': note,
        'movements': movements,
        'can_manage': _can_manage_purchases(request.user, organization),
        'active_page': 'returns',
    })

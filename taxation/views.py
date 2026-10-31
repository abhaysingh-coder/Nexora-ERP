import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils.dateparse import parse_date

from core.models import AuditEvent
from core.organization_context import get_active_organization
from core.permissions import has_organization_role
from inventory.models import Product
from organizations.models import Branch

from .forms import ProductTaxClassificationForm, TaxCodeForm, TaxRateForm, TaxRegistrationForm
from .models import ProductTaxClassification, TaxCode, TaxRate, TaxRegistration, TaxTransaction


def get_user_organization(request):
    return get_active_organization(request)


def _organization_context(request):
    return get_user_organization(request)


def _can_manage_taxation(request, organization):
    return has_organization_role(
        request.user, organization, {'owner', 'admin', 'accountant', 'finance_manager'}
    )


def _scoped_transactions(request, organization):
    transactions = TaxTransaction.objects.filter(organization=organization).select_related(
        'branch', 'tax_code'
    ).order_by('-transaction_date', '-id')
    if request.user.branch_id:
        transactions = transactions.filter(branch_id=request.user.branch_id)
    start = parse_date(request.GET.get('start', ''))
    end = parse_date(request.GET.get('end', ''))
    if start:
        transactions = transactions.filter(transaction_date__gte=start)
    if end:
        transactions = transactions.filter(transaction_date__lte=end)
    return transactions, start, end


@login_required
def taxation_home(request):
    organization = _organization_context(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    transactions, start, end = _scoped_transactions(request, organization)
    registrations = TaxRegistration.objects.filter(organization=organization)
    if request.user.branch_id:
        registrations = registrations.filter(Q(branch=request.user.branch) | Q(branch__isnull=True))
    context = {
        'organization': organization,
        'tax_codes': TaxCode.objects.filter(organization=organization),
        'rates': TaxRate.objects.filter(organization=organization).select_related('tax_code')[:20],
        'registrations': registrations,
        'classifications': ProductTaxClassification.objects.filter(organization=organization).select_related(
            'product', 'tax_code'
        )[:20],
        'transactions': Paginator(transactions, 25).get_page(request.GET.get('page')),
        'start': start,
        'end': end,
        'active_page': 'taxation',
    }
    return render(request, 'taxation/index.html', context)


def _save_form(request, form_class, title, success_message, configure_queryset=None, get_instance=None):
    organization = _organization_context(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    if not _can_manage_taxation(request, organization):
        return HttpResponseForbidden('You do not have permission to change taxation configuration.')
    if request.method == 'POST':
        instance = get_instance(request, organization) if get_instance else None
        form = form_class(request.POST, instance=instance)
        form.instance.organization = organization
        if configure_queryset:
            configure_queryset(form, organization)
        if form.is_valid():
            with transaction.atomic():
                obj = form.save(commit=False)
                obj.organization = organization
                obj.save()
                AuditEvent.objects.create(
                    user=request.user,
                    organization=organization,
                    action='taxation.configuration_saved',
                    details=f'{obj._meta.label} id={obj.pk}',
                )
            messages.success(request, success_message)
            return redirect('taxation_home')
    else:
        form = form_class()
        if configure_queryset:
            configure_queryset(form, organization)
    return render(
        request,
        'taxation/form.html',
        {'form': form, 'title': title, 'organization': organization, 'active_page': 'taxation'},
    )


@login_required
def tax_code_create(request):
    return _save_form(request, TaxCodeForm, 'Create tax code', 'Tax code created.')


@login_required
def tax_rate_create(request):
    def configure(form, organization):
        form.fields['tax_code'].queryset = TaxCode.objects.filter(organization=organization)

    return _save_form(request, TaxRateForm, 'Create tax rate', 'Tax rate created.', configure)


@login_required
def tax_registration_create(request):
    def configure(form, organization):
        if request.user.branch_id:
            form.fields['branch'].queryset = Branch.objects.filter(pk=request.user.branch_id)
            form.fields['branch'].required = True
            form.fields['branch'].initial = request.user.branch_id
        else:
            form.fields['branch'].queryset = Branch.objects.filter(organization=organization)

    def get_instance(request, organization):
        branch_id = request.POST.get('branch') or None
        return TaxRegistration.objects.filter(organization=organization, branch_id=branch_id).first()

    return _save_form(
        request,
        TaxRegistrationForm,
        'Create GST registration',
        'GST registration saved.',
        configure,
        get_instance,
    )


@login_required
def product_tax_classification_create(request):
    def configure(form, organization):
        form.fields['product'].queryset = Product.objects.filter(organization=organization)
        form.fields['tax_code'].queryset = TaxCode.objects.filter(organization=organization)

    def get_instance(request, organization):
        product_id = request.POST.get('product')
        return ProductTaxClassification.objects.filter(
            organization=organization,
            product_id=product_id,
        ).first()

    return _save_form(
        request,
        ProductTaxClassificationForm,
        'Map product tax classification',
        'Product tax classification saved.',
        configure,
        get_instance,
    )


def _tax_register(request, kind, title, template_name):
    organization = _organization_context(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    transactions, start, end = _scoped_transactions(request, organization)
    transactions = transactions.filter(transaction_kind=kind)
    transactions = transactions.order_by('-transaction_date', '-id')
    return render(
        request,
        template_name,
        {
            'organization': organization,
            'transactions': Paginator(transactions, 50).get_page(request.GET.get('page')),
            'title': title,
            'kind': kind,
            'start': start,
            'end': end,
            'active_page': 'taxation',
        },
    )


@login_required
def sales_tax_register(request):
    return _tax_register(request, 'sale', 'Sales GST register', 'taxation/register.html')


@login_required
def purchase_tax_register(request):
    return _tax_register(request, 'purchase', 'Purchase GST register', 'taxation/register.html')


@login_required
def tax_register_csv(request):
    organization = _organization_context(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    transactions, _, _ = _scoped_transactions(request, organization)
    kind = request.GET.get('kind')
    if kind in {'sale', 'purchase'}:
        transactions = transactions.filter(transaction_kind=kind)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="tax-register.csv"'
    writer = csv.writer(response)
    writer.writerow(
        ['Date', 'Kind', 'Reference', 'Tax code', 'Taxable value', 'CGST', 'SGST', 'IGST', 'UTGST', 'Cess', 'Total tax']
    )
    for transaction in transactions.order_by('created_at'):
        writer.writerow(
            [
                transaction.transaction_date,
                transaction.transaction_kind,
                f'{transaction.source_model}-{transaction.source_id}',
                transaction.tax_code.code,
                transaction.taxable_value,
                transaction.cgst,
                transaction.sgst,
                transaction.igst,
                transaction.utgst,
                transaction.cess,
                transaction.total_tax,
            ]
        )
    return response


@login_required
def output_tax_summary(request):
    return _tax_summary(request, 'sale', 'Output tax summary')


@login_required
def input_tax_summary(request):
    return _tax_summary(request, 'purchase', 'Input tax summary')


@login_required
def tax_liability_summary(request):
    organization = _organization_context(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    transactions, start, end = _scoped_transactions(request, organization)
    output_tax = transactions.filter(
        Q(transaction_kind='sale', is_reverse_charge=False)
        | Q(transaction_kind='purchase', is_reverse_charge=True)
    ).aggregate(total=Sum('total_tax'))['total'] or 0
    input_tax = transactions.filter(
        transaction_kind='purchase',
        is_input_tax_credit_eligible=True,
    ).aggregate(total=Sum('total_tax'))['total'] or 0
    return render(
        request,
        'taxation/liability.html',
        {
            'organization': organization,
            'title': 'Tax liability summary',
            'output_tax': output_tax,
            'input_tax': input_tax,
            'net_tax': output_tax - input_tax,
            'start': start,
            'end': end,
            'active_page': 'taxation',
        },
    )


@login_required
def _tax_summary(request, kind, title):
    organization = _organization_context(request)
    if organization is None:
        return HttpResponseForbidden('An authorized organization is required.')
    transactions, start, end = _scoped_transactions(request, organization)
    if kind == 'sale':
        summary = transactions.filter(
            Q(transaction_kind='sale', is_reverse_charge=False)
            | Q(transaction_kind='purchase', is_reverse_charge=True)
        )
    else:
        summary = transactions.filter(
            transaction_kind='purchase', is_input_tax_credit_eligible=True
        )
    totals = summary.aggregate(
        taxable=Sum('taxable_value'),
        cgst=Sum('cgst'),
        sgst=Sum('sgst'),
        igst=Sum('igst'),
        utgst=Sum('utgst'),
        cess=Sum('cess'),
        tax=Sum('total_tax'),
    )
    return render(
        request,
        'taxation/summary.html',
        {'organization': organization, 'title': title, 'totals': totals, 'start': start, 'end': end, 'active_page': 'taxation'},
    )

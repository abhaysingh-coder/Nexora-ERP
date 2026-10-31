from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import (
    ProductTaxClassification,
    TaxCode,
    TaxRate,
    TaxTransaction,
    normalize_tax_amount,
    validate_state_code,
)

ZERO = Decimal('0.00')
HUNDRED = Decimal('100')


def _rate_on_date(organization, tax_code, transaction_date, tax_type, state_code, required=True):
    rates = TaxRate.objects.filter(
        organization=organization,
        tax_code=tax_code,
        tax_type=tax_type,
        is_active=True,
        effective_from__lte=transaction_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=transaction_date)).filter(
        state_code__in=['', state_code]
    )
    rates = list(rates.order_by('-effective_from'))
    if not rates:
        if not required:
            return None
        raise ValidationError(f'No active {tax_type.upper()} rate is effective on {transaction_date}.')
    best_date = max((rate.effective_from, bool(rate.state_code)) for rate in rates)
    selected = [rate for rate in rates if (rate.effective_from, bool(rate.state_code)) == best_date]
    if len(selected) != 1:
        raise ValidationError(f'Multiple active {tax_type.upper()} rates match this transaction.')
    return selected[0]


def calculate_tax(
    *,
    organization,
    tax_code,
    transaction_date,
    taxable_amount,
    origin_state,
    place_of_supply,
    is_inclusive=False,
    is_exempt=False,
    is_zero_rated=False,
    reverse_charge=False,
    hsn_sac='',
):
    """Calculate GST using effective component rates and an explicit place of supply."""
    if not isinstance(tax_code, TaxCode):
        try:
            tax_code = TaxCode.objects.get(organization=organization, code=tax_code, is_active=True)
        except TaxCode.DoesNotExist as exc:
            raise ValidationError('The selected active tax code does not exist in this organization.') from exc
    if tax_code.organization_id != organization.pk or not tax_code.is_active:
        raise ValidationError('The tax code must be active and belong to this organization.')
    is_exempt = is_exempt or tax_code.category == 'exempt'
    is_zero_rated = is_zero_rated or tax_code.category == 'zero_rated'
    if is_exempt and is_zero_rated:
        raise ValidationError('A tax classification cannot be both exempt and zero-rated.')
    if not transaction_date:
        raise ValidationError('A transaction date is required to select effective tax rates.')
    if not origin_state or not place_of_supply:
        raise ValidationError('Origin state and place of supply are required to determine GST treatment.')
    validate_state_code(origin_state)
    validate_state_code(place_of_supply)
    if origin_state == place_of_supply:
        supply_type = 'intra'
    else:
        supply_type = 'inter'
    try:
        amount = Decimal(str(taxable_amount))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValidationError('Taxable amount must be a valid decimal number.') from exc
    if not amount.is_finite() or amount < 0:
        raise ValidationError('Taxable amount must be a finite, non-negative amount.')
    amount = normalize_tax_amount(amount)
    if reverse_charge and is_inclusive:
        raise ValidationError('Reverse-charge pricing cannot be interpreted as tax-inclusive.')

    result = {
        'taxable_value': amount,
        'tax_rate': Decimal('0.0000'),
        'cgst_rate': Decimal('0.0000'),
        'sgst_rate': Decimal('0.0000'),
        'igst_rate': Decimal('0.0000'),
        'utgst_rate': Decimal('0.0000'),
        'cess_rate': Decimal('0.0000'),
        'cgst': ZERO,
        'sgst': ZERO,
        'igst': ZERO,
        'utgst': ZERO,
        'cess': ZERO,
        'total_tax': ZERO,
        'invoice_total': amount,
        'is_reverse_charge': bool(reverse_charge),
        'is_exempt': bool(is_exempt),
        'is_zero_rated': bool(is_zero_rated),
        'hsn_sac': hsn_sac,
        'origin_state': origin_state,
        'place_of_supply': place_of_supply,
        'is_inclusive': bool(is_inclusive),
        'supply_type': supply_type,
    }
    if amount == 0 or is_exempt or is_zero_rated:
        return result

    if supply_type == 'inter':
        component_types = ['igst']
    else:
        local_components = ['cgst', 'utgst'] if place_of_supply in {'04', '26', '31', '35', '38'} else ['cgst', 'sgst']
        component_types = local_components
    component_types.append('cess')

    rate_objects = {
        component: _rate_on_date(
            organization,
            tax_code,
            transaction_date,
            component,
            place_of_supply,
            required=component != 'cess',
        )
        for component in component_types
    }
    rates = {
        component: Decimal(str(rate.rate)) if rate else Decimal('0')
        for component, rate in rate_objects.items()
    }
    total_rate = sum(rates.values(), Decimal('0'))
    if total_rate > HUNDRED:
        raise ValidationError('Combined tax component rates cannot exceed 100 percent.')
    result['tax_rate'] = total_rate.quantize(Decimal('0.0001'))
    for component, rate in rates.items():
        result[f'{component}_rate'] = rate.quantize(Decimal('0.0001'))

    if is_inclusive:
        taxable_value = normalize_tax_amount(amount / (Decimal('1') + total_rate / HUNDRED))
    else:
        taxable_value = amount
    result['taxable_value'] = taxable_value
    for component, rate in rates.items():
        result[component] = normalize_tax_amount(taxable_value * rate / HUNDRED)
    result['total_tax'] = normalize_tax_amount(
        sum((result[component] for component in component_types), Decimal('0'))
    )
    result['invoice_total'] = normalize_tax_amount(
        taxable_value if reverse_charge else taxable_value + result['total_tax']
    )
    if is_inclusive:
        difference = amount - taxable_value - result['total_tax']
        if difference:
            result[next(component for component in reversed(component_types) if component != 'cess')] += difference
            result['total_tax'] = normalize_tax_amount(
                sum((result[component] for component in component_types), Decimal('0'))
            )
        result['invoice_total'] = amount
    return result


def persist_tax_transaction(
    *,
    organization,
    branch,
    transaction_kind,
    transaction_date,
    source_model,
    source_id,
    source_line_id,
    tax_code,
    calculation,
    is_input_tax_credit_eligible=True,
):
    if branch and branch.organization_id != organization.pk:
        raise ValidationError('Branch must belong to the transaction organization.')
    if tax_code.organization_id != organization.pk:
        raise ValidationError('Tax code must belong to the transaction organization.')
    transaction, _ = TaxTransaction.objects.update_or_create(
        organization=organization,
        transaction_kind=transaction_kind,
        source_model=source_model,
        source_id=source_id,
        source_line_id=source_line_id,
        defaults={
            'branch': branch,
            'transaction_date': transaction_date,
            'tax_code': tax_code,
            'hsn_sac': calculation['hsn_sac'],
            'origin_state': calculation['origin_state'],
            'place_of_supply': calculation['place_of_supply'],
            'line_amount': calculation['line_amount'],
            'discount_amount': calculation['discount_amount'],
            'is_inclusive': calculation['is_inclusive'],
            'taxable_value': calculation['taxable_value'],
            'tax_rate': calculation['tax_rate'],
            'cgst_rate': calculation['cgst_rate'],
            'sgst_rate': calculation['sgst_rate'],
            'igst_rate': calculation['igst_rate'],
            'utgst_rate': calculation['utgst_rate'],
            'cess_rate': calculation['cess_rate'],
            'cgst': calculation['cgst'],
            'sgst': calculation['sgst'],
            'igst': calculation['igst'],
            'utgst': calculation['utgst'],
            'cess': calculation['cess'],
            'total_tax': calculation['total_tax'],
            'invoice_total': calculation['invoice_total'],
            'is_reverse_charge': calculation['is_reverse_charge'],
            'is_exempt': calculation['is_exempt'],
            'is_zero_rated': calculation['is_zero_rated'],
            'is_input_tax_credit_eligible': is_input_tax_credit_eligible,
        },
    )
    return transaction


def get_tax_registration(organization, branch=None):
    registrations = organization.tax_registrations.filter(is_registered=True)
    if branch:
        registration = registrations.filter(branch=branch).first()
        if registration:
            return registration
    return registrations.filter(branch__isnull=True).first()


def calculate_document_tax_lines(invoice, transaction_kind):
    lines = list(invoice.lines.select_related('product').all())
    if invoice.branch_id and invoice.branch.organization_id != invoice.organization_id:
        raise ValidationError('Invoice branch must belong to the invoice organization.')
    counterparty = invoice.customer if transaction_kind == 'sale' else invoice.supplier
    if counterparty.organization_id != invoice.organization_id:
        raise ValidationError('Invoice customer or supplier must belong to the invoice organization.')
    classifications = {
        classification.product_id: classification
        for classification in ProductTaxClassification.objects.filter(
            organization=invoice.organization,
            product_id__in=[line.product_id for line in lines],
        ).select_related('tax_code')
    }
    if not classifications:
        return None
    if any(line.product_id not in classifications for line in lines):
        raise ValidationError('Tax-aware invoices require a tax classification for every product line.')
    if not invoice.place_of_supply:
        raise ValidationError('Place of supply is required for GST-calculated invoices.')
    if transaction_kind == 'sale':
        registration = get_tax_registration(invoice.organization, invoice.branch)
        if registration is None:
            raise ValidationError('A registered organization/branch tax registration is required for GST sales.')
        origin_state = registration.state_code
        reverse_charge_applicable = False
    else:
        origin_state = invoice.supplier.state_code
        if not origin_state:
            raise ValidationError('Supplier state is required to calculate purchase GST.')
        registration = get_tax_registration(invoice.organization, invoice.branch)
        reverse_charge_applicable = (
            invoice.supplier.is_reverse_charge_applicable
            or (registration is not None and registration.is_reverse_charge_applicable)
        )

    results = []
    for line in lines:
        classification = classifications[line.product_id]
        if line.product.organization_id != invoice.organization_id:
            raise ValidationError('Invoice product must belong to the invoice organization.')
        discount = Decimal(line.discount_amount)
        if discount < 0 or discount > line.line_total:
            raise ValidationError(f'Discount for {line.product.name} must be between zero and the line amount.')
        calculation = calculate_tax(
            organization=invoice.organization,
            tax_code=classification.tax_code,
            transaction_date=invoice.invoice_date,
            taxable_amount=line.line_total - discount,
            origin_state=origin_state,
            place_of_supply=invoice.place_of_supply,
            is_inclusive=classification.tax_inclusive,
            is_exempt=classification.is_exempt,
            is_zero_rated=classification.is_zero_rated,
            reverse_charge=(
                transaction_kind == 'purchase'
                and (classification.is_reverse_charge or reverse_charge_applicable)
            ),
            hsn_sac=classification.hsn_sac,
        )
        calculation['line_amount'] = normalize_tax_amount(line.line_total)
        calculation['discount_amount'] = normalize_tax_amount(discount)
        results.append((line, classification, calculation))
    return results


def document_tax_totals(results):
    return {
        'subtotal': normalize_tax_amount(sum((result['taxable_value'] for _, _, result in results), Decimal('0'))),
        'tax_amount': normalize_tax_amount(
            sum(
                (
                    result['total_tax']
                    for _, _, result in results
                    if not result['is_reverse_charge']
                ),
                Decimal('0'),
            )
        ),
        'total_amount': normalize_tax_amount(sum((result['invoice_total'] for _, _, result in results), Decimal('0'))),
    }

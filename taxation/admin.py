from django.contrib import admin
from core.admin import SuperuserReadOnlyAdmin

from .models import ProductTaxClassification, TaxCode, TaxRate, TaxRegistration, TaxTransaction


class TaxationAdmin(SuperuserReadOnlyAdmin):
    pass


@admin.register(TaxCode)
class TaxCodeAdmin(TaxationAdmin):
    list_display = ('organization', 'code', 'name', 'category', 'is_active')
    list_filter = ('organization', 'category', 'is_active')
    search_fields = ('code', 'name')


@admin.register(TaxRate)
class TaxRateAdmin(TaxationAdmin):
    list_display = ('organization', 'tax_code', 'tax_type', 'rate', 'state_code', 'effective_from', 'effective_to')
    list_filter = ('organization', 'tax_type', 'state_code', 'is_active')
    date_hierarchy = 'effective_from'


@admin.register(TaxRegistration)
class TaxRegistrationAdmin(TaxationAdmin):
    list_display = ('organization', 'branch', 'gstin', 'state_code', 'is_registered')
    list_filter = ('organization', 'state_code', 'is_registered')
    search_fields = ('gstin',)


@admin.register(ProductTaxClassification)
class ProductTaxClassificationAdmin(TaxationAdmin):
    list_display = ('organization', 'product', 'tax_code', 'hsn_sac', 'tax_inclusive', 'is_exempt', 'is_zero_rated')
    list_filter = ('organization', 'tax_code', 'is_exempt', 'is_zero_rated')
    search_fields = ('product__name', 'product__sku', 'hsn_sac')


@admin.register(TaxTransaction)
class TaxTransactionAdmin(TaxationAdmin):
    list_display = ('transaction_date', 'organization', 'branch', 'transaction_kind', 'source_model', 'source_id', 'total_tax')
    list_filter = ('organization', 'branch', 'transaction_kind', 'is_reverse_charge')
    date_hierarchy = 'transaction_date'
    readonly_fields = tuple(field.name for field in TaxTransaction._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

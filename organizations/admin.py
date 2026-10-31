from django.contrib import admin
from django import forms

from core.models import AuditEvent
from .models import Branch, Organization, OrganizationMembership


class SuperuserOnlyAdmin:
    """Keep tenant provisioning in Django's explicit superuser control plane."""

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request)


@admin.register(Organization)
class OrganizationAdmin(SuperuserOnlyAdmin, admin.ModelAdmin):
    list_display = ('name', 'slug', 'currency', 'is_active', 'updated_at')
    list_filter = ('is_active', 'currency')
    search_fields = ('name', 'slug', 'gstin')
    ordering = ('name',)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        previous = None
        if change:
            previous = Organization.objects.get(pk=obj.pk)
        super().save_model(request, obj, form, change)
        AuditEvent.objects.create(
            user=request.user,
            organization=obj,
            action='organization.updated' if change else 'organization.created',
            details=(
                f'id={obj.pk}; name={previous.name if previous else ""}->{obj.name}; '
                f'active={previous.is_active if previous else ""}->{obj.is_active}'
            ),
        )


@admin.register(Branch)
class BranchAdmin(SuperuserOnlyAdmin, admin.ModelAdmin):
    list_display = ('name', 'code', 'organization', 'is_active')
    list_filter = ('organization', 'is_active')
    search_fields = ('name', 'code', 'organization__name')
    ordering = ('organization__name', 'name')

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        previous = None
        if change:
            previous = Branch.objects.get(pk=obj.pk)
        super().save_model(request, obj, form, change)
        AuditEvent.objects.create(
            user=request.user,
            organization=obj.organization,
            action='organization.branch_updated' if change else 'organization.branch_created',
            details=(
                f'id={obj.pk}; code={previous.code if previous else ""}->{obj.code}; '
                f'active={previous.is_active if previous else ""}->{obj.is_active}'
            ),
        )


class OrganizationMembershipAdminForm(forms.ModelForm):
    reason = forms.CharField(max_length=300, label='Assignment/change reason')

    class Meta:
        model = OrganizationMembership
        fields = '__all__'


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(SuperuserOnlyAdmin, admin.ModelAdmin):
    form = OrganizationMembershipAdminForm
    list_display = ('user', 'organization', 'role', 'is_active')
    list_filter = ('organization', 'role', 'is_active')
    search_fields = ('user__username', 'user__email', 'organization__name')
    list_select_related = ('user', 'organization')
    ordering = ('organization__name', 'user__username')
    fieldsets = (
        (None, {
            'fields': ('user', 'organization', 'role', 'is_active', 'reason'),
            'description': 'Membership assignment is explicit. Select only a role approved for this user; no membership is granted automatically.',
        }),
    )

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        previous = None
        if change:
            previous = OrganizationMembership.objects.get(pk=obj.pk)
        super().save_model(request, obj, form, change)
        reason = form.cleaned_data['reason'].strip()
        AuditEvent.objects.create(
            user=request.user,
            organization=obj.organization,
            action='organization.membership_changed' if change else 'organization.membership_assigned',
            details=(
                f'user_id={obj.user_id}; role={previous.role if previous else ""}->{obj.role}; '
                f'active={previous.is_active if previous else ""}->{obj.is_active}; reason={reason}'
            ),
        )

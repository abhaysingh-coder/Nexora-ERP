from django.contrib import admin
from django.contrib.auth.models import Group
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters

from .models import AuditEvent, User


class SuperuserReadOnlyAdmin(admin.ModelAdmin):
    """Expose an inspection-only admin view until scoped admin workflows exist."""

    def _superuser(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_module_permission(self, request):
        return self._superuser(request)

    def has_view_permission(self, request, obj=None):
        return self._superuser(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)


class OrganizationScopedUserChangeForm(UserChangeForm):
    def clean(self):
        cleaned_data = super().clean()
        branch = cleaned_data.get('branch')
        if branch and self.instance.pk and not self.instance.is_superuser:
            if not self.instance.organizations_memberships.filter(
                organization_id=branch.organization_id,
                is_active=True,
                organization__is_active=True,
            ).exists():
                self.add_error('branch', 'Assign an active membership for the branch organization first.')
        return cleaned_data


class SuperuserOnlyUserAdmin(DjangoUserAdmin):
    form = OrganizationScopedUserChangeForm
    readonly_fields = DjangoUserAdmin.readonly_fields + ('organization',)
    fieldsets = DjangoUserAdmin.fieldsets + (
        ('Legacy organization context', {'fields': ('organization', 'branch')}),
    )

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        previous = None
        if change:
            previous = User.objects.only(
                'branch_id', 'organization_id', 'is_active', 'is_staff', 'is_superuser'
            ).get(pk=obj.pk)
        changed_fields = sorted(set(form.changed_data) - {'password'})
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            if change and not changed_fields:
                return
            organization_id = (
                obj.branch.organization_id if obj.branch_id
                else obj.organization_id or (previous.organization_id if previous else None)
            )
            details = [
                f'target_user_id={obj.pk}',
                f'changed_fields={",".join(changed_fields)}',
            ]
            if previous:
                for field in ('branch_id', 'organization_id', 'is_active', 'is_staff', 'is_superuser'):
                    old_value = getattr(previous, field)
                    new_value = getattr(obj, field)
                    if old_value != new_value:
                        details.append(f'{field}={old_value}->{new_value}')
            AuditEvent.objects.create(
                user=request.user,
                organization_id=organization_id,
                action='identity.user_updated' if change else 'identity.user_created',
                details='; '.join(details),
            )

    @method_decorator(sensitive_post_parameters())
    def user_change_password(self, request, id, form_url=''):
        target = self.get_object(request, id)
        previous_password_hash = target.password if target else None
        with transaction.atomic():
            response = super().user_change_password(request, id, form_url)
            if request.method == 'POST' and target is not None and response.status_code in {301, 302, 303}:
                target.refresh_from_db(fields=['password', 'branch', 'organization'])
                if target.password != previous_password_hash:
                    organization_id = (
                        target.branch.organization_id if target.branch_id
                        else target.organization_id
                    )
                    AuditEvent.objects.create(
                        user=request.user,
                        organization_id=organization_id,
                        action='identity.user_password_changed',
                        details=f'target_user_id={target.pk}; password_based_authentication_changed=true',
                    )
        return response

admin.site.register(User, SuperuserOnlyUserAdmin)
admin.site.unregister(Group)

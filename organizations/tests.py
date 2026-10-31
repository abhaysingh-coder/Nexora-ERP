from django.test import TestCase
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse

from types import SimpleNamespace

from core.models import AuditEvent, User
from core.permissions import has_organization_role
from organizations.models import Branch, Organization, OrganizationMembership


class OrganizationAccessTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Primary Co', slug='primary-co')
        self.second_organization = Organization.objects.create(name='Second Co', slug='second-co')
        self.branch = Branch.objects.create(
            organization=self.organization,
            name='Main branch',
            code='MAIN',
        )
        self.user = User.objects.create_user(
            username='member',
            email='member@example.test',
            password='test-password',
        )
        self.client.force_login(self.user)

    def test_unassigned_user_is_sent_to_access_required_without_dashboard_kpis(self):
        response = self.client.get(reverse('dashboard'), follow=True)

        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, 'organizations/access_required.html')
        self.assertNotIn('sales_total', response.context)

    def test_active_membership_grants_only_its_organization_context(self):
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='sales',
        )
        self.user.organization = self.second_organization
        self.user.save(update_fields=['organization'])

        response = self.client.get(reverse('dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['organization'], self.organization)

    def test_inactive_membership_does_not_authorize_access(self):
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='sales',
            is_active=False,
        )

        response = self.client.get(reverse('dashboard'), follow=True)

        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, 'organizations/access_required.html')

    def test_membership_to_inactive_organization_does_not_authorize_access(self):
        self.organization.is_active = False
        self.organization.save(update_fields=['is_active'])
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='sales',
        )

        response = self.client.get(reverse('dashboard'), follow=True)

        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, 'organizations/access_required.html')

    def test_branch_must_belong_to_an_active_membership_organization(self):
        other_branch = Branch.objects.create(
            organization=self.second_organization,
            name='Other branch',
            code='OTHER',
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='sales',
        )
        self.user.branch = other_branch
        self.user.save(update_fields=['branch'])

        response = self.client.get(reverse('inventory_home'), follow=True)

        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, 'organizations/access_required.html')

    def test_unselected_multiple_memberships_redirect_to_selector_then_scope_dashboard(self):
        OrganizationMembership.objects.create(user=self.user, organization=self.organization, role='sales')
        OrganizationMembership.objects.create(user=self.user, organization=self.second_organization, role='sales')

        response = self.client.get(reverse('dashboard'))
        self.assertRedirects(response, f'{reverse("organizations:select")}?next={reverse("dashboard")}')

        selected = self.client.post(
            reverse('organizations:select'),
            {'organization': self.second_organization.pk, 'next': reverse('dashboard')},
        )
        self.assertRedirects(selected, reverse('dashboard'))
        dashboard = self.client.get(reverse('dashboard'))
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.context['organization'], self.second_organization)

    def test_user_cannot_select_an_organization_without_active_membership(self):
        OrganizationMembership.objects.create(user=self.user, organization=self.organization, role='sales')

        response = self.client.post(
            reverse('organizations:select'),
            {'organization': self.second_organization.pk},
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('active_organization_id', self.client.session)

    def test_business_module_direct_url_requires_authorized_organization(self):
        response = self.client.get(reverse('trial_balance'), follow=True)

        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, 'organizations/access_required.html')


class OrganizationProvisioningAdminTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='root-admin',
            email='root@example.test',
            password='test-password',
        )
        self.staff_user = User.objects.create_user(
            username='ordinary-staff',
            email='staff@example.test',
            password='test-password',
            is_staff=True,
        )

    def test_only_superuser_can_access_organization_provisioning_admin(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('admin:organizations_organization_changelist'))
        self.assertEqual(response.status_code, 200)

        self.client.force_login(self.staff_user)
        denied = self.client.get(reverse('admin:organizations_organization_changelist'))
        self.assertIn(denied.status_code, {302, 403})

    def test_admin_permissions_are_denied_to_non_superuser_staff(self):
        model_admin = admin.site._registry[Organization]
        request = RequestFactory().get('/admin/')
        request.user = self.staff_user

        self.assertFalse(model_admin.has_module_permission(request))
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    def test_membership_assignment_requires_reason_and_writes_actor_audit(self):
        organization = Organization.objects.create(name='Audit Co', slug='audit-co')
        target = User.objects.create_user(
            username='target-user',
            email='target@example.test',
            password='test-password',
        )
        model_admin = admin.site._registry[OrganizationMembership]
        request = RequestFactory().post('/admin/')
        request.user = self.superuser
        membership = OrganizationMembership(
            user=target,
            organization=organization,
            role='sales',
            is_active=True,
        )
        form = SimpleNamespace(cleaned_data={'reason': 'Approved sales access'})

        model_admin.save_model(request, membership, form, change=False)

        event = AuditEvent.objects.get(action='organization.membership_assigned')
        self.assertEqual(event.user, self.superuser)
        self.assertEqual(event.organization, organization)
        self.assertIn('user_id=', event.details)
        self.assertIn('role=->sales', event.details)
        self.assertIn('reason=Approved sales access', event.details)
        self.assertFalse(model_admin.has_delete_permission(request, membership))


class SharedOrganizationRoleTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Role Co', slug='role-co')
        self.other_organization = Organization.objects.create(name='Other Role Co', slug='other-role-co')
        self.user = User.objects.create_user(username='role-member', password='test-password')

    def test_role_check_requires_matching_active_membership_and_preserves_role_sets(self):
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='sales',
        )

        self.assertTrue(has_organization_role(self.user, self.organization, {'sales', 'sales_manager'}))
        self.assertFalse(has_organization_role(self.user, self.organization, {'accountant'}))
        self.assertFalse(has_organization_role(self.user, self.other_organization, {'sales'}))
        self.assertFalse(has_organization_role(self.user, None, {'sales'}))

    def test_inactive_membership_and_unauthenticated_user_are_denied(self):
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='sales',
            is_active=False,
        )
        anonymous = AnonymousUser()

        self.assertFalse(has_organization_role(self.user, self.organization, {'sales'}))
        self.assertFalse(has_organization_role(anonymous, self.organization, {'sales'}))

    def test_inactive_user_with_active_membership_is_denied(self):
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='sales',
        )
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])

        self.assertFalse(has_organization_role(self.user, self.organization, {'sales'}))

    def test_inactive_superuser_does_not_bypass_role_guard(self):
        superuser = User.objects.create_superuser(
            username='inactive-role-root',
            email='inactive-role-root@example.test',
            password='test-password',
        )
        superuser.is_active = False
        superuser.save(update_fields=['is_active'])

        self.assertFalse(has_organization_role(superuser, self.organization, set()))

    def test_superuser_bypass_is_retained_for_callers_that_use_it(self):
        superuser = User.objects.create_superuser(
            username='role-root',
            email='role-root@example.test',
            password='test-password',
        )

        self.assertTrue(has_organization_role(superuser, self.organization, set()))


class BusinessAdminAccessTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='admin-root',
            email='admin-root@example.test',
            password='test-password',
        )
        self.staff_user = User.objects.create_user(
            username='business-staff',
            email='business-staff@example.test',
            password='test-password',
            is_staff=True,
        )

    def test_unscoped_business_admins_are_superuser_only_and_read_only(self):
        from purchases.models import PurchaseOrder, Supplier
        from returns.models import CustomerReturn, SalesCreditNote
        from taxation.models import TaxCode, TaxTransaction

        protected_models = (
            PurchaseOrder,
            Supplier,
            CustomerReturn,
            SalesCreditNote,
            TaxCode,
            TaxTransaction,
        )
        for model in protected_models:
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                staff_request = RequestFactory().get('/admin/')
                staff_request.user = self.staff_user
                root_request = RequestFactory().get('/admin/')
                root_request.user = self.superuser

                self.assertFalse(model_admin.has_module_permission(staff_request))
                self.assertFalse(model_admin.has_view_permission(staff_request))
                self.assertTrue(model_admin.has_module_permission(root_request))
                self.assertTrue(model_admin.has_view_permission(root_request))
                self.assertFalse(model_admin.has_add_permission(root_request))
                self.assertFalse(model_admin.has_change_permission(root_request))
                self.assertFalse(model_admin.has_delete_permission(root_request))

    def test_group_admin_is_not_exposed_as_a_second_role_management_path(self):
        from django.contrib.auth.models import Group

        self.assertNotIn(Group, admin.site._registry)

    def test_non_superuser_staff_cannot_open_business_admin_changelists(self):
        self.client.force_login(self.staff_user)
        for url_name in (
            'admin:purchases_purchaseorder_changelist',
            'admin:returns_customerreturn_changelist',
            'admin:taxation_taxcode_changelist',
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertIn(response.status_code, {302, 403})

    def test_registered_model_permissions_and_admin_requests_for_every_model(self):
        writable_models = {User, Organization, Branch, OrganizationMembership}
        active_request = RequestFactory().get('/admin/')
        active_request.user = self.superuser

        inactive_superuser = User.objects.create_superuser(
            username='inactive-admin-root',
            email='inactive-admin-root@example.test',
            password='test-password',
        )
        inactive_superuser.is_active = False
        inactive_superuser.save(update_fields=['is_active'])
        inactive_request = RequestFactory().get('/admin/')
        inactive_request.user = inactive_superuser

        self.assertEqual(len(admin.site._registry), 24)
        self.client.force_login(self.superuser)
        for model, model_admin in admin.site._registry.items():
            opts = model._meta
            prefix = f'admin:{opts.app_label}_{opts.model_name}'
            with self.subTest(model=opts.label):
                self.assertTrue(model_admin.has_module_permission(active_request))
                self.assertTrue(model_admin.has_view_permission(active_request))
                self.assertEqual(model_admin.has_add_permission(active_request), model in writable_models)
                self.assertEqual(model_admin.has_change_permission(active_request), model in writable_models)
                self.assertFalse(model_admin.has_delete_permission(active_request))
                self.assertFalse(model_admin.has_module_permission(inactive_request))
                self.assertFalse(model_admin.has_view_permission(inactive_request))
                self.assertFalse(model_admin.has_add_permission(inactive_request))
                self.assertFalse(model_admin.has_change_permission(inactive_request))
                self.assertFalse(model_admin.has_delete_permission(inactive_request))

                changelist = self.client.get(reverse(f'{prefix}_changelist'))
                self.assertEqual(changelist.status_code, 200)
                add_response = self.client.get(reverse(f'{prefix}_add'))
                self.assertEqual(
                    add_response.status_code,
                    200 if model in writable_models else 403,
                )

        self.client.force_login(self.staff_user)
        for model in admin.site._registry:
            opts = model._meta
            with self.subTest(model=opts.label, actor='staff'):
                response = self.client.get(
                    reverse(f'admin:{opts.app_label}_{opts.model_name}_changelist')
                )
                self.assertIn(response.status_code, {302, 403})

        self.client.force_login(inactive_superuser)
        for model in admin.site._registry:
            opts = model._meta
            with self.subTest(model=opts.label, actor='inactive-superuser'):
                response = self.client.get(
                    reverse(f'admin:{opts.app_label}_{opts.model_name}_changelist')
                )
                self.assertIn(response.status_code, {302, 403})


class UserAdminAuditTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Audit tenant', slug='audit-tenant')
        self.branch = Branch.objects.create(
            organization=self.organization,
            name='Audit branch',
            code='AUD',
        )
        self.other_branch = Branch.objects.create(
            organization=self.organization,
            name='Second audit branch',
            code='AUD2',
        )
        self.actor = User.objects.create_superuser(
            username='user-audit-admin',
            email='user-audit-admin@example.test',
            password='test-password',
        )
        self.model_admin = admin.site._registry[User]
        self.request = RequestFactory().post('/admin/core/user/')
        self.request.user = self.actor

    def test_user_creation_audit_omits_password_and_personal_values(self):
        from types import SimpleNamespace

        target = User(username='audit-target', email='target-private@example.test')
        target.set_password('private-password')
        form = SimpleNamespace(changed_data=['username', 'email', 'password', 'is_active'])

        self.model_admin.save_model(self.request, target, form, change=False)

        event = AuditEvent.objects.get(action='identity.user_created')
        self.assertEqual(event.user_id, self.actor.pk)
        self.assertEqual(event.organization_id, None)
        self.assertIn(f'target_user_id={target.pk}', event.details)
        self.assertIn('changed_fields=email,is_active,username', event.details)
        self.assertNotIn('target-private@example.test', event.details)
        self.assertNotIn('private-password', event.details)
        self.assertNotIn('password;', event.details)

    def test_user_updates_create_one_audit_event_with_security_flags_and_branch_ids(self):
        from types import SimpleNamespace

        target = User.objects.create_user(
            username='audit-edited-user',
            email='audit-edited@example.test',
            password='test-password',
            organization=self.organization,
            branch=self.branch,
        )
        target.is_active = False
        target.is_staff = True
        target.branch = self.other_branch
        form = SimpleNamespace(changed_data=['branch', 'is_active', 'is_staff', 'email', 'password'])

        self.model_admin.save_model(self.request, target, form, change=True)

        events = AuditEvent.objects.filter(action='identity.user_updated', organization=self.organization)
        self.assertEqual(events.count(), 1)
        details = events.get().details
        self.assertIn(f'branch_id={self.branch.pk}->{self.other_branch.pk}', details)
        self.assertIn('is_active=True->False', details)
        self.assertIn('is_staff=False->True', details)
        self.assertNotIn('audit-edited@example.test', details)
        self.assertNotIn('test-password', details)

    def test_password_admin_change_creates_event_without_recording_secret(self):
        target = User.objects.create_user(
            username='password-audit-target',
            email='password-audit@example.test',
            password='old-password-value',
            organization=self.organization,
        )
        self.client.force_login(self.actor)
        url = reverse('admin:auth_user_password_change', args=[target.pk])

        response = self.client.post(url, {
            'password1': 'N4c$8pV!z_Q2m7',
            'password2': 'N4c$8pV!z_Q2m7',
            'usable_password': 'true',
        })

        self.assertEqual(response.status_code, 302, response.context['form'].errors.as_text() if response.context else response.content[:500])
        event = AuditEvent.objects.get(action='identity.user_password_changed')
        self.assertIn(f'target_user_id={target.pk}', event.details)
        self.assertNotIn('N4c$8pV!z_Q2m7', event.details)
        target.refresh_from_db()
        self.assertTrue(target.check_password('N4c$8pV!z_Q2m7'))

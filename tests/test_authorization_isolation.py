from decimal import Decimal

from django.test import Client, TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from core.models import User
from inventory.models import Product
from organizations.models import Branch, Organization, OrganizationMembership
from purchases.models import PurchaseOrder, Supplier
from sales.models import Customer, SalesInvoice


class PurchaseTenantAndBranchIsolationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Tenant A', slug='tenant-a')
        self.other_org = Organization.objects.create(name='Tenant B', slug='tenant-b')
        self.branch_a = Branch.objects.create(organization=self.org, name='Branch A', code='A')
        self.branch_b = Branch.objects.create(organization=self.org, name='Branch B', code='B')
        self.foreign_branch = Branch.objects.create(organization=self.other_org, name='Foreign', code='F')
        self.product = Product.objects.create(
            organization=self.org,
            sku='ISOLATION-1',
            name='Isolation test product',
            cost_price=Decimal('2.00'),
            sales_price=Decimal('3.00'),
        )
        self.user = User.objects.create_user(
            username='branch-owner', password='test-password', branch=self.branch_a,
        )
        OrganizationMembership.objects.create(user=self.user, organization=self.org, role='owner')
        self.client.force_login(self.user)
        self.orders = [
            self._order(self.org, self.branch_a, 'PO-A'),
            self._order(self.org, self.branch_b, 'PO-B'),
            self._order(self.other_org, self.foreign_branch, 'PO-F'),
        ]

    def _order(self, organization, branch, number):
        supplier = Supplier.objects.create(organization=organization, name=f'Supplier {number}')
        return PurchaseOrder.objects.create(
            organization=organization,
            branch=branch,
            supplier=supplier,
            order_number=number,
            order_date='2026-10-01',
            status='draft',
        )

    def test_list_is_scoped_to_membership_organization_and_assigned_branch(self):
        response = self.client.get(reverse('purchase_order_list'), {'branch': self.branch_b.pk})

        self.assertEqual(response.status_code, 200)
        visible = list(response.context['orders'].object_list)
        self.assertEqual([row.order_number for row in visible], ['PO-A'])

    def test_foreign_branch_and_foreign_tenant_ids_are_not_readable_or_mutable(self):
        for order in self.orders[1:]:
            with self.subTest(order=order.order_number):
                detail = self.client.get(reverse('purchase_order_detail', args=[order.pk]))
                self.assertEqual(detail.status_code, 404)
                mutation = self.client.post(
                    reverse('purchase_order_detail', args=[order.pk]),
                    {'action': 'confirm'},
                )
                self.assertEqual(mutation.status_code, 404)
                order.refresh_from_db()
                self.assertEqual(order.status, 'draft')

    def test_unrecognized_read_only_role_cannot_confirm_an_owned_order(self):
        reader = User.objects.create_user(
            username='read-only', email='read-only@example.test',
            password='test-password', branch=self.branch_a,
        )
        OrganizationMembership.objects.create(user=reader, organization=self.org, role='auditor')
        self.client.force_login(reader)

        response = self.client.post(
            reverse('purchase_order_detail', args=[self.orders[0].pk]),
            {'action': 'confirm'},
        )

        self.assertEqual(response.status_code, 403)
        self.orders[0].refresh_from_db()
        self.assertEqual(self.orders[0].status, 'draft')

    def test_submitted_branch_and_foreign_supplier_ids_cannot_change_order_scope(self):
        base = {
            'branch': str(self.branch_b.pk),
            'order_number': 'PO-SUBMITTED-BRANCH',
            'order_date': '2026-10-01',
            'lines-TOTAL_FORMS': '3',
            'lines-INITIAL_FORMS': '0',
            'lines-MIN_NUM_FORMS': '1',
            'lines-MAX_NUM_FORMS': '1000',
        }
        for index in range(3):
            base.update({
                f'lines-{index}-product': '',
                f'lines-{index}-quantity': '',
                f'lines-{index}-unit_cost': '',
                f'lines-{index}-discount_amount': '',
            })
        base.update({
            'lines-0-product': str(self.product.pk),
            'lines-0-quantity': '1',
            'lines-0-unit_cost': '2.00',
            'lines-0-discount_amount': '0.00',
        })
        base['supplier'] = str(self.orders[0].supplier_id)

        response = self.client.post(reverse('purchase_order_create'), base)

        self.assertEqual(response.status_code, 200)
        self.assertIn('branch', response.context['form'].errors)
        self.assertFalse(PurchaseOrder.objects.filter(order_number='PO-SUBMITTED-BRANCH').exists())

        base['order_number'] = 'PO-FOREIGN-SUPPLIER'
        base['branch'] = str(self.branch_a.pk)
        base['supplier'] = str(self.orders[2].supplier_id)
        response = self.client.post(reverse('purchase_order_create'), base)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PurchaseOrder.objects.filter(order_number='PO-FOREIGN-SUPPLIER').exists())

    def test_tampered_session_organization_resolves_only_to_membership_scope(self):
        session = self.client.session
        session['active_organization_id'] = self.other_org.pk
        session.save()

        response = self.client.get(reverse('purchase_order_list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['organization'], self.org)
        self.assertEqual(
            [row.order_number for row in response.context['orders'].object_list],
            ['PO-A'],
        )

    def test_dashboard_sales_aggregate_excludes_foreign_tenant_and_branch_data(self):
        own_customer = Customer.objects.create(organization=self.org, name='Own dashboard customer')
        other_branch_customer = Customer.objects.create(
            organization=self.org,
            name='Other branch dashboard customer',
        )
        foreign_customer = Customer.objects.create(
            organization=self.other_org,
            name='Foreign dashboard customer',
        )
        SalesInvoice.objects.create(
            organization=self.org,
            branch=self.branch_a,
            customer=own_customer,
            invoice_number='DASH-A',
            invoice_date='2026-10-01',
            status='posted',
            total_amount=Decimal('10.00'),
        )
        SalesInvoice.objects.create(
            organization=self.org,
            branch=self.branch_b,
            customer=other_branch_customer,
            invoice_number='DASH-B',
            invoice_date='2026-10-01',
            status='posted',
            total_amount=Decimal('90.00'),
        )
        SalesInvoice.objects.create(
            organization=self.other_org,
            branch=self.foreign_branch,
            customer=foreign_customer,
            invoice_number='DASH-F',
            invoice_date='2026-10-01',
            status='posted',
            total_amount=Decimal('900.00'),
        )

        response = self.client.get(
            reverse('dashboard'),
            {'branch': self.branch_b.pk, 'organization': self.other_org.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['sales_total'], Decimal('10.00'))
        self.assertNotContains(response, 'DASH-B')
        self.assertNotContains(response, 'DASH-F')

    def test_get_does_not_confirm_order(self):
        response = self.client.get(reverse('purchase_order_detail', args=[self.orders[0].pk]))

        self.assertEqual(response.status_code, 200)
        self.orders[0].refresh_from_db()
        self.assertEqual(self.orders[0].status, 'draft')


class OrganizationSelectorCsrfTests(TestCase):
    def test_organization_change_post_requires_csrf_token(self):
        organization = Organization.objects.create(name='CSRF tenant', slug='csrf-tenant')
        user = User.objects.create_user(username='csrf-user', password='test-password')
        OrganizationMembership.objects.create(user=user, organization=organization, role='owner')
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)

        response = client.post(
            reverse('organizations:select'),
            {'organization': organization.pk, 'next': reverse('dashboard')},
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('active_organization_id', client.session)


def _application_routes(patterns=None, prefix=''):
    patterns = patterns or get_resolver().url_patterns
    for pattern in patterns:
        route = f'{prefix}{pattern.pattern}'
        if route.startswith('admin/'):
            continue
        if isinstance(pattern, URLResolver):
            yield from _application_routes(pattern.url_patterns, route)
        elif isinstance(pattern, URLPattern):
            kwargs = {}
            for name, converter in pattern.pattern.converters.items():
                kwargs[name] = converter.to_url(999999)
            path = route
            for name, value in kwargs.items():
                import re

                path = re.sub(rf'<[^:>]+:{name}>', str(value), path)
            yield pattern.name, '/' + path.lstrip('/'), pattern.callback.nexora_allowed_methods


class CompleteRouteMethodAndAuthenticationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Route audit', slug='route-audit')
        self.branch = Branch.objects.create(
            organization=self.organization,
            name='Route audit branch',
            code='ROUTE',
        )
        self.user = User.objects.create_user(
            username='route-audit-user',
            email='route-audit@example.test',
            password='test-password',
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role='owner',
        )
        self.routes = list(_application_routes())

    def test_every_application_route_has_an_explicit_allowlist_and_rejects_put_patch_delete(self):
        self.assertEqual(len(self.routes), 72)
        for name, path, methods in self.routes:
            with self.subTest(route=name):
                for method in ('PUT', 'PATCH', 'DELETE'):
                    response = self.client.generic(method, path, data=b'{}', content_type='application/json')
                    self.assertEqual(response.status_code, 405, f'{method} {path}: {response.status_code}')

    def test_every_application_route_enforces_authentication_on_get(self):
        for name, path, methods in self.routes:
            with self.subTest(route=name):
                response = self.client.get(path)
                if 'GET' not in methods:
                    self.assertEqual(response.status_code, 405)
                elif name == 'login':
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertEqual(response.status_code, 302)

    def test_every_get_capable_route_reaches_a_scoped_view_for_active_owner(self):
        self.client.force_login(self.user)
        for name, path, methods in self.routes:
            if 'GET' not in methods:
                continue
            with self.subTest(route=name):
                response = self.client.get(path)
                expected = (
                    {403} if name == 'access_required'
                    else {302} if name == 'home'
                    else {200, 404}
                )
                self.assertIn(
                    response.status_code,
                    expected,
                    f'Unexpected status for {name}: {response.status_code}',
                )

    def test_inactive_members_and_inactive_superusers_cannot_read_business_routes(self):
        inactive_member = User.objects.create_user(
            username='inactive-route-member',
            email='inactive-route-member@example.test',
            password='test-password',
            is_active=False,
        )
        OrganizationMembership.objects.create(
            user=inactive_member,
            organization=self.organization,
            role='owner',
        )
        inactive_superuser = User.objects.create_superuser(
            username='inactive-route-superuser',
            email='inactive-route-superuser@example.test',
            password='test-password',
        )
        inactive_superuser.is_active = False
        inactive_superuser.save(update_fields=['is_active'])

        business_prefixes = (
            '/dashboard/',
            '/accounting/',
            '/banking/',
            '/inventory/',
            '/purchases/',
            '/returns/',
            '/sales/',
            '/taxation/',
        )
        for user in (inactive_member, inactive_superuser):
            self.client.force_login(user)
            for name, path, methods in self.routes:
                if 'GET' not in methods or not path.startswith(business_prefixes):
                    continue
                with self.subTest(user=user.username, route=name):
                    response = self.client.get(path)
                    self.assertIn(
                        response.status_code,
                        {302, 403, 404, 405},
                        f'Inactive account reached {name}: {response.status_code}',
                    )

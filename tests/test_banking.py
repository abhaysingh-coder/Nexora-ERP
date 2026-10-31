from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounting.models import Account, JournalEntry
from banking.models import BankAccount, BankStatementImport, BankTransaction, InvoiceAllocation
from banking.services import allocate_payment, invoice_open_amount, transaction_unapplied_amount
from inventory.models import Product, Warehouse
from organizations.models import Branch, Organization, OrganizationMembership
from purchases.models import PurchaseInvoice, PurchaseInvoiceLine, Supplier
from sales.models import Customer, SalesInvoice
from core.models import User


class BankingModuleTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Coastal Retail', slug='coastal-retail', currency='INR')
        self.branch = Branch.objects.create(organization=self.organization, name='Bengaluru', code='BLR')
        self.customer = Customer.objects.create(organization=self.organization, name='Kapoor Traders', gstin='27AAAAA0000A1Z5')
        self.supplier = Supplier.objects.create(organization=self.organization, name='Wholesale Partner')
        self.product = Product.objects.create(
            organization=self.organization,
            sku='PAYABLE-ITEM',
            name='Payable item',
            cost_price=Decimal('10.00'),
            sales_price=Decimal('15.00'),
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            branch=self.branch,
            name='Purchasing warehouse',
            code='PUR-WH',
        )
        self.account = BankAccount.objects.create(
            organization=self.organization,
            branch=self.branch,
            name='HDFC Primary',
            account_type='bank',
            account_number='HDFC-001',
            opening_balance=Decimal('0.00'),
            current_balance=Decimal('0.00'),
        )

    def _sales_invoice(self, number, amount, customer=None):
        return SalesInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            customer=customer or self.customer,
            invoice_number=number,
            invoice_date='2026-02-01',
            due_date='2026-02-28',
            status='posted',
            subtotal=amount,
            total_amount=amount,
        )

    def _purchase_invoice(self, number, amount):
        invoice = PurchaseInvoice.objects.create(
            organization=self.organization,
            branch=self.branch,
            supplier=self.supplier,
            invoice_number=number,
            invoice_date='2026-02-01',
            due_date='2026-02-28',
            subtotal=amount,
            tax_amount=Decimal('0.00'),
            total_amount=amount,
        )
        PurchaseInvoiceLine.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=Decimal('1.000'),
            unit_cost=amount,
            line_total=amount,
        )
        invoice.post()
        return invoice

    def _bank_transaction(self, reference, amount, kind='receipt', customer=None, supplier=None):
        bank_transaction = BankTransaction.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=self.account,
            customer=customer or (self.customer if kind == 'receipt' else None),
            supplier=supplier or (self.supplier if kind == 'payment' else None),
            transaction_type=kind,
            reference_number=reference,
            transaction_date='2026-02-10',
            amount=amount,
        )
        bank_transaction.post()
        return bank_transaction

    def test_receipt_transaction_posts_and_updates_balance(self):
        transaction = BankTransaction.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=self.account,
            customer=self.customer,
            transaction_type='receipt',
            reference_number='BR-1001',
            transaction_date='2026-02-01',
            amount=Decimal('2500.00'),
            description='Customer advance',
            payment_reference='INV-2001',
        )

        transaction.post()
        self.account.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('2500.00'))
        self.assertEqual(self.customer.outstanding_balance, Decimal('0.00'))
        self.assertEqual(transaction.status, 'posted')

    def test_duplicate_transaction_post_is_idempotent(self):
        transaction = BankTransaction.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=self.account,
            customer=self.customer,
            transaction_type='receipt',
            reference_number='BR-1002',
            transaction_date='2026-02-02',
            amount=Decimal('800.00'),
            description='Repeat check',
            payment_reference='INV-2002',
        )

        transaction.post()
        transaction.post()
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('800.00'))
        self.assertEqual(BankTransaction.objects.filter(reference_number='BR-1002').count(), 1)

    def test_statement_import_detects_matches_and_duplicates(self):
        base = BankTransaction.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=self.account,
            customer=self.customer,
            transaction_type='receipt',
            reference_number='BR-1003',
            transaction_date='2026-02-03',
            amount=Decimal('400.00'),
            description='Existing row',
            payment_reference='INV-2003',
        )
        base.post()

        statement = BankStatementImport.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=self.account,
            file_name='statement.csv',
            uploaded_by=None,
        )
        created, duplicates = statement.process_csv(
            'reference,amount,description,transaction_date\n'
            'BR-1003,400.00,Existing row,2026-02-03\n'
            'BR-1004,150.00,New row,2026-02-04\n'
            'BR-1004,150.00,New row,2026-02-04\n'
        )

        self.assertEqual(created, 2)
        self.assertEqual(duplicates, 1)
        self.assertEqual(statement.status, 'processed')
        imported_line = statement.lines.get(reference='BR-1003')
        self.assertEqual(imported_line.status, 'unmatched')
        base.mark_reconciled(imported_line)
        imported_line.refresh_from_db()
        base.refresh_from_db()
        self.assertEqual(imported_line.status, 'matched')
        self.assertEqual(imported_line.matched_transaction_id, base.pk)
        self.assertEqual(base.status, 'reconciled')

    def test_invalid_statement_rows_fail_without_partial_import(self):
        statement = BankStatementImport.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=self.account,
            file_name='invalid.csv',
        )
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            statement.process_csv(
                'reference,amount,transaction_date\n'
                'GOOD,100.00,2026-02-01\n'
                'BAD,not-money,2026-02-02\n'
            )
        self.assertEqual(statement.lines.count(), 0)

    def test_statement_line_requires_explicit_match_and_correct_date_amount_and_account(self):
        tx = BankTransaction.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=self.account,
            customer=self.customer,
            transaction_type='receipt',
            reference_number='BR-1004',
            transaction_date='2026-02-03',
            amount=Decimal('400.00'),
        )
        tx.post()
        statement = BankStatementImport.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=self.account,
            file_name='statement-2.csv',
        )
        statement.process_csv(
            'reference,amount,transaction_date\n'
            'BR-1004,401.00,2026-02-03\n'
        )
        line = statement.lines.get()
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            tx.mark_reconciled(line)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'posted')
        self.assertEqual(line.status, 'unmatched')

    def test_customer_receipt_can_be_split_across_invoices_and_keeps_unapplied_advance(self):
        invoice_one = self._sales_invoice('SI-ALLOC-1', Decimal('80.00'))
        invoice_two = self._sales_invoice('SI-ALLOC-2', Decimal('70.00'))
        self.customer.outstanding_balance = Decimal('150.00')
        self.customer.save(update_fields=['outstanding_balance'])
        receipt = self._bank_transaction('BR-ALLOC-1', Decimal('120.00'))

        allocate_payment(bank_transaction_id=receipt.pk, invoice_id=invoice_one.pk, amount=Decimal('80.00'), user=None)
        allocate_payment(bank_transaction_id=receipt.pk, invoice_id=invoice_two.pk, amount=Decimal('30.00'), user=None)

        self.assertEqual(invoice_open_amount(invoice_one), Decimal('0.00'))
        self.assertEqual(invoice_open_amount(invoice_two), Decimal('40.00'))
        self.assertEqual(transaction_unapplied_amount(receipt), Decimal('10.00'))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_balance, Decimal('40.00'))
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('120.00'))
        self.assertEqual(receipt.invoice_allocations.filter(reversed_at__isnull=True).count(), 2)

    def test_multiple_receipts_can_partially_settle_one_invoice(self):
        invoice = self._sales_invoice('SI-ALLOC-3', Decimal('100.00'))
        self.customer.outstanding_balance = Decimal('100.00')
        self.customer.save(update_fields=['outstanding_balance'])
        receipt_one = self._bank_transaction('BR-ALLOC-2', Decimal('40.00'))
        receipt_two = self._bank_transaction('BR-ALLOC-3', Decimal('60.00'))

        allocate_payment(bank_transaction_id=receipt_one.pk, invoice_id=invoice.pk, amount=Decimal('40.00'), user=None)
        allocate_payment(bank_transaction_id=receipt_two.pk, invoice_id=invoice.pk, amount=Decimal('60.00'), user=None)

        self.assertEqual(invoice_open_amount(invoice), Decimal('0.00'))
        self.assertEqual(InvoiceAllocation.objects.filter(sales_invoice=invoice, reversed_at__isnull=True).count(), 2)

    def test_supplier_payment_allocation_and_reversal_preserve_audit_and_balances(self):
        invoice = self._purchase_invoice('PI-ALLOC-1', Decimal('95.00'))
        self.supplier.outstanding_balance = Decimal('95.00')
        self.supplier.save(update_fields=['outstanding_balance'])
        payment = self._bank_transaction('BP-ALLOC-1', Decimal('110.00'), kind='payment')
        allocation = allocate_payment(
            bank_transaction_id=payment.pk,
            invoice_id=invoice.pk,
            amount=Decimal('95.00'),
            user=None,
        )
        self.assertEqual(invoice_open_amount(invoice), Decimal('0.00'))
        self.assertEqual(transaction_unapplied_amount(payment), Decimal('15.00'))
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('-110.00'))
        original_journal_count = JournalEntry.objects.count()
        from banking.services import reverse_allocation

        reverse_allocation(allocation_id=allocation.pk, user=None, reason='Incorrect invoice selected')
        allocation.refresh_from_db()
        self.supplier.refresh_from_db()
        self.assertFalse(allocation.is_active)
        self.assertEqual(allocation.reversal_reason, 'Incorrect invoice selected')
        self.assertEqual(invoice_open_amount(invoice), Decimal('95.00'))
        self.assertEqual(self.supplier.outstanding_balance, Decimal('95.00'))
        self.assertEqual(JournalEntry.objects.count(), original_journal_count + 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('-110.00'))

    def test_allocation_rejects_wrong_party_overpayment_and_unposted_invoice(self):
        invoice = self._sales_invoice('SI-ALLOC-4', Decimal('50.00'))
        other_customer = Customer.objects.create(organization=self.organization, name='Other Customer')
        wrong_party = self._bank_transaction(
            'BR-ALLOC-4',
            Decimal('100.00'),
            customer=other_customer,
        )
        with self.assertRaises(ValidationError):
            allocate_payment(
                bank_transaction_id=wrong_party.pk,
                invoice_id=invoice.pk,
                amount=Decimal('10.00'),
                user=None,
            )

        receipt = self._bank_transaction('BR-ALLOC-5', Decimal('60.00'))
        with self.assertRaises(ValidationError):
            allocate_payment(
                bank_transaction_id=receipt.pk,
                invoice_id=invoice.pk,
                amount=Decimal('51.00'),
                user=None,
            )
        draft = SalesInvoice.objects.create(
            organization=self.organization,
            customer=self.customer,
            invoice_number='SI-ALLOC-DRAFT',
            invoice_date='2026-02-01',
            total_amount=Decimal('10.00'),
        )
        with self.assertRaises(ValidationError):
            allocate_payment(
                bank_transaction_id=receipt.pk,
                invoice_id=draft.pk,
                amount=Decimal('10.00'),
                user=None,
            )

    def test_allocation_views_enforce_membership_and_export_balances(self):
        invoice = self._sales_invoice('SI-ALLOC-5', Decimal('75.00'))
        self.customer.outstanding_balance = Decimal('75.00')
        self.customer.save(update_fields=['outstanding_balance'])
        receipt = self._bank_transaction('BR-ALLOC-6', Decimal('75.00'))
        user = User.objects.create_user(
            username='allocator',
            email='allocator@example.test',
            password='test-password',
            organization=self.organization,
            branch=self.branch,
        )
        OrganizationMembership.objects.create(user=user, organization=self.organization, role='accountant')
        self.client.force_login(user)
        response = self.client.get('/banking/receivables/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SI-ALLOC-5')
        response = self.client.post(
            f'/banking/transactions/{receipt.pk}/allocate/',
            {'invoice': invoice.pk, 'amount': '75.00'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(InvoiceAllocation.objects.filter(bank_transaction=receipt, sales_invoice=invoice).exists())
        csv_response = self.client.get('/banking/receivables/?format=csv')
        self.assertEqual(csv_response.status_code, 200)
        self.assertNotContains(csv_response, 'SI-ALLOC-5')

    def test_receivables_and_payables_csv_scope_survives_tenant_branch_and_filter_tampering(self):
        other_branch = Branch.objects.create(
            organization=self.organization,
            name='Other banking branch',
            code='OTHER-BANK',
        )
        other_customer = Customer.objects.create(
            organization=self.organization,
            name='Other branch customer',
        )
        other_supplier = Supplier.objects.create(
            organization=self.organization,
            name='Other branch supplier',
        )
        other_warehouse = Warehouse.objects.create(
            organization=self.organization,
            branch=other_branch,
            name='Other banking warehouse',
            code='OTHER-BANK-WH',
        )
        foreign_organization = Organization.objects.create(
            name='Foreign balance tenant',
            slug='foreign-balance-tenant',
        )
        foreign_branch = Branch.objects.create(
            organization=foreign_organization,
            name='Foreign banking branch',
            code='FOREIGN-BANK',
        )
        foreign_product = Product.objects.create(
            organization=foreign_organization,
            sku='FOREIGN-BALANCE-ITEM',
            name='Foreign balance item',
            cost_price=Decimal('10.00'),
            sales_price=Decimal('15.00'),
        )
        foreign_warehouse = Warehouse.objects.create(
            organization=foreign_organization,
            branch=foreign_branch,
            name='Foreign balance warehouse',
            code='FOREIGN-BALANCE-WH',
        )
        foreign_customer = Customer.objects.create(
            organization=foreign_organization,
            name='Foreign customer',
        )
        foreign_supplier = Supplier.objects.create(
            organization=foreign_organization,
            name='Foreign supplier',
        )
        own_customer_invoice = self._sales_invoice('SI-SCOPE-OWN', Decimal('10.00'))
        other_customer_invoice = SalesInvoice.objects.create(
            organization=self.organization,
            branch=other_branch,
            customer=other_customer,
            invoice_number='SI-SCOPE-BRANCH',
            invoice_date='2026-03-01',
            due_date='2026-03-31',
            status='posted',
            total_amount=Decimal('20.00'),
        )
        foreign_customer_invoice = SalesInvoice.objects.create(
            organization=foreign_organization,
            branch=None,
            customer=foreign_customer,
            invoice_number='SI-SCOPE-FOREIGN',
            invoice_date='2026-03-01',
            due_date='2026-03-31',
            status='posted',
            total_amount=Decimal('30.00'),
        )

        def create_posted_purchase_invoice(
            organization, branch, supplier, product, warehouse, invoice_number, amount
        ):
            invoice = PurchaseInvoice.objects.create(
                organization=organization,
                branch=branch,
                supplier=supplier,
                invoice_number=invoice_number,
                invoice_date='2026-03-01',
                due_date='2026-03-31',
                subtotal=amount,
                total_amount=amount,
            )
            PurchaseInvoiceLine.objects.create(
                invoice=invoice,
                product=product,
                quantity=Decimal('1.000'),
                unit_cost=amount,
                line_total=amount,
            )
            invoice.post()
            return invoice

        own_supplier_invoice = create_posted_purchase_invoice(
            self.organization,
            self.branch,
            self.supplier,
            self.product,
            self.warehouse,
            'PI-SCOPE-OWN',
            Decimal('11.00'),
        )
        create_posted_purchase_invoice(
            self.organization,
            other_branch,
            other_supplier,
            self.product,
            other_warehouse,
            'PI-SCOPE-BRANCH',
            Decimal('21.00'),
        )
        create_posted_purchase_invoice(
            foreign_organization,
            foreign_branch,
            foreign_supplier,
            foreign_product,
            foreign_warehouse,
            'PI-SCOPE-FOREIGN',
            Decimal('31.00'),
        )

        user = User.objects.create_user(
            username='branch-register-reader',
            email='branch-register-reader@example.test',
            password='test-password',
            organization=self.organization,
            branch=self.branch,
        )
        OrganizationMembership.objects.create(
            user=user,
            organization=self.organization,
            role='accountant',
        )
        self.client.force_login(user)

        cases = (
            (
                '/banking/receivables/',
                'SI-SCOPE-OWN',
                ('SI-SCOPE-BRANCH', 'SI-SCOPE-FOREIGN'),
                own_customer_invoice.customer_id,
                foreign_customer.pk,
            ),
            (
                '/banking/payables/',
                'PI-SCOPE-OWN',
                ('PI-SCOPE-BRANCH', 'PI-SCOPE-FOREIGN'),
                own_supplier_invoice.supplier_id,
                foreign_supplier.pk,
            ),
        )
        for path, expected, excluded, own_party_id, foreign_party_id in cases:
            with self.subTest(path=path):
                response = self.client.get(
                    path,
                    {
                        'format': 'csv',
                        'branch': other_branch.pk,
                        'organization': foreign_organization.pk,
                        'party': own_party_id,
                        'start': '2026-02-01',
                        'end': '2026-03-31',
                    },
                )
                contents = response.content.decode()
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, contents)
                for invoice_number in excluded:
                    self.assertNotIn(invoice_number, contents)

                foreign_party_response = self.client.get(
                    path,
                    {'format': 'csv', 'party': foreign_party_id},
                )
                self.assertNotIn('SCOPE-FOREIGN', foreign_party_response.content.decode())

                empty_date_response = self.client.get(
                    path,
                    {'format': 'csv', 'start': '2026-04-01', 'end': '2026-04-30'},
                )
                self.assertNotIn('SCOPE-OWN', empty_date_response.content.decode())

    def test_reconciliation_suggestions_require_confirmation_and_allow_audited_undo(self):
        receipt = self._bank_transaction('BR-SUGGEST-1', Decimal('400.00'))
        statement = BankStatementImport.objects.create(
            organization=self.organization,
            branch=self.branch,
            account=self.account,
            file_name='suggestions.csv',
        )
        statement.process_csv(
            'reference,amount,description,transaction_date\n'
            'BR-SUGGEST-1,400.00,Kapoor Traders,2026-02-10\n'
        )
        line = statement.lines.get()
        user = User.objects.create_user(
            username='reconciler',
            email='reconciler@example.test',
            password='test-password',
            organization=self.organization,
            branch=self.branch,
        )
        OrganizationMembership.objects.create(user=user, organization=self.organization, role='accountant')
        self.client.force_login(user)

        response = self.client.get('/banking/reconcile/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'reference matches')
        line.refresh_from_db()
        receipt.refresh_from_db()
        self.assertEqual(line.status, 'unmatched')
        self.assertEqual(receipt.status, 'posted')

        response = self.client.post(
            '/banking/reconcile/',
            {'transaction_id': receipt.pk, 'statement_line_id': line.pk},
        )
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.status, 'matched')
        self.assertEqual(line.reconciliation_events.filter(action='matched').count(), 1)

        response = self.client.post(
            f'/banking/transactions/{receipt.pk}/unreconcile/',
            {'reason': 'Bank line imported against the wrong batch'},
        )
        self.assertEqual(response.status_code, 302)
        receipt.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(receipt.status, 'posted')
        self.assertEqual(line.status, 'unmatched')
        self.assertEqual(line.reconciliation_events.count(), 2)


class BankingAuthorizationViewTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Bank Security Co', slug='bank-security-co')
        self.branch = Branch.objects.create(organization=self.organization, name='Main', code='MAIN')
        self.account = BankAccount.objects.create(
            organization=self.organization,
            branch=self.branch,
            name='Operating',
            account_type='bank',
            account_number='SAFE-001',
        )
        self.customer = Customer.objects.create(
            organization=self.organization,
            name='Bank authorization customer',
        )
        self.sales_user = User.objects.create_user(
            username='bank-sales', email='bank-sales@example.test', password='test-password'
        )
        OrganizationMembership.objects.create(
            user=self.sales_user,
            organization=self.organization,
            role='sales',
        )
        self.finance_alias_user = User.objects.create_user(
            username='bank-finance', email='bank-finance@example.test', password='test-password'
        )
        OrganizationMembership.objects.create(
            user=self.finance_alias_user,
            organization=self.organization,
            role='finance',
        )
        self.accountant = User.objects.create_user(
            username='bank-accountant', email='bank-accountant@example.test', password='test-password'
        )
        OrganizationMembership.objects.create(
            user=self.accountant,
            organization=self.organization,
            role='accountant',
        )

    def test_non_core_finance_members_cannot_open_banking_mutation_routes(self):
        mutation_routes = (
            'bank_account_create',
            'bank_transaction_create',
            'bank_import_csv',
        )
        for user in (self.sales_user, self.finance_alias_user):
            self.client.force_login(user)
            for route in mutation_routes:
                with self.subTest(user=user.username, route=route, method='GET'):
                    self.assertEqual(self.client.get(reverse(route)).status_code, 403)
                with self.subTest(user=user.username, route=route, method='POST'):
                    self.assertEqual(self.client.post(reverse(route), {}).status_code, 403)
            self.assertEqual(self.client.get(reverse('bank_reconcile')).status_code, 200)
            self.assertEqual(
                self.client.post(
                    reverse('bank_reconcile'),
                    {'transaction_id': '1', 'statement_line_id': '1'},
                ).status_code,
                403,
            )

    def test_accountant_can_reach_approved_banking_operations(self):
        self.client.force_login(self.accountant)
        for route in ('bank_account_create', 'bank_transaction_create', 'bank_import_csv', 'bank_reconcile'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 200)

        # The operation-role guard passes; these invalid object IDs then fail safely at lookup.
        response = self.client.post(
            reverse('bank_reconcile'),
            {'transaction_id': '999999', 'statement_line_id': '999999'},
        )
        self.assertEqual(response.status_code, 404)

    def test_accountant_can_create_account_post_transaction_import_and_reconcile(self):
        self.client.force_login(self.accountant)
        account_response = self.client.post(
            reverse('bank_account_create'),
            {
                'name': 'New operating account',
                'account_type': 'bank',
                'account_number': 'SAFE-002',
                'opening_balance': '0.00',
                'is_active': 'on',
            },
        )
        self.assertEqual(account_response.status_code, 302)
        bank_account = BankAccount.objects.get(account_number='SAFE-002')
        self.assertEqual(bank_account.organization, self.organization)

        transaction_response = self.client.post(
            reverse('bank_transaction_create'),
            {
                'account': bank_account.pk,
                'transaction_type': 'receipt',
                'reference_number': 'BANK-AUTH-TX-1',
                'transaction_date': '2026-03-01',
                'amount': '25.00',
                'description': 'Authorization test receipt',
                'payment_reference': 'AUTH-REF-1',
                'customer': self.customer.pk,
                'supplier': '',
                'related_account': '',
            },
        )
        self.assertEqual(transaction_response.status_code, 302)
        bank_transaction = BankTransaction.objects.get(reference_number='BANK-AUTH-TX-1')
        self.assertEqual(bank_transaction.status, 'posted')

        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile(
            'bank-auth.csv',
            (
                'reference,amount,description,transaction_date\n'
                'BANK-AUTH-TX-1,25.00,Authorization test receipt,2026-03-01\n'
            ).encode('utf-8'),
            content_type='text/csv',
        )
        import_response = self.client.post(
            reverse('bank_import_csv'),
            {'account': bank_account.pk, 'csv_file': upload},
        )
        self.assertEqual(import_response.status_code, 302)
        statement = BankStatementImport.objects.get(account=bank_account, file_name='bank-auth.csv')
        line = statement.lines.get(reference='BANK-AUTH-TX-1')

        reconciliation_response = self.client.post(
            reverse('bank_reconcile'),
            {'transaction_id': bank_transaction.pk, 'statement_line_id': line.pk},
        )
        self.assertEqual(reconciliation_response.status_code, 302)
        bank_transaction.refresh_from_db()
        self.assertEqual(bank_transaction.status, 'reconciled')

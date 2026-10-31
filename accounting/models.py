from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from organizations.models import Branch, Organization


def round_currency(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class FinancialYearQuerySet(models.QuerySet):
    def _finalized(self):
        return self.filter(year_end_closing__status='closed')

    def update(self, **kwargs):
        if self._finalized().exists():
            raise ValidationError('A finalized financial year cannot be changed.')
        return super().update(**kwargs)

    def delete(self):
        if self._finalized().exists():
            raise ValidationError('A finalized financial year cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self._finalized().filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('A finalized financial year cannot be changed.')
        return super().bulk_update(objects, fields, batch_size=batch_size)


class FinancialYear(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='financial_years',
    )
    name = models.CharField(max_length=80)
    starts_on = models.DateField()
    ends_on = models.DateField()
    retained_earnings_account = models.ForeignKey(
        'accounting.Account',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='retained_earnings_years',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-starts_on']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'name'], name='unique_financial_year_name'),
            models.CheckConstraint(condition=models.Q(ends_on__gte=models.F('starts_on')), name='financial_year_valid_dates'),
        ]

    objects = FinancialYearQuerySet.as_manager()

    def clean(self):
        if self.pk and YearEndClosing.objects.filter(
            financial_year_id=self.pk,
            status='closed',
        ).exists():
            stored = FinancialYear.objects.filter(pk=self.pk).values(
                'organization_id',
                'name',
                'starts_on',
                'ends_on',
                'retained_earnings_account_id',
            ).first()
            if stored and any(
                stored[field] != value
                for field, value in (
                    ('organization_id', self.organization_id),
                    ('name', self.name),
                    ('starts_on', self.starts_on),
                    ('ends_on', self.ends_on),
                    ('retained_earnings_account_id', self.retained_earnings_account_id),
                )
            ):
                raise ValidationError('A finalized financial year cannot be changed.')
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValidationError('Financial-year end date must not be before its start date.')
        if self.organization_id and self.starts_on and self.ends_on:
            overlaps = FinancialYear.objects.filter(
                organization_id=self.organization_id,
                starts_on__lte=self.ends_on,
                ends_on__gte=self.starts_on,
            ).exclude(pk=self.pk)
            if overlaps.exists():
                raise ValidationError('Financial years for the same organization cannot overlap.')
        if self.retained_earnings_account_id:
            account = self.retained_earnings_account
            if account.organization_id != self.organization_id:
                raise ValidationError({
                    'retained_earnings_account': 'Retained earnings account must belong to this organization.'
                })
            if account.account_type != 'equity' or not account.is_active:
                raise ValidationError({
                    'retained_earnings_account': 'Retained earnings must be an active equity account.'
                })
            if account.branch_id:
                raise ValidationError({
                    'retained_earnings_account': 'Select an organization-wide equity account for retained earnings.'
                })

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if YearEndClosing.objects.filter(financial_year_id=self.pk, status='closed').exists():
            raise ValidationError('A finalized financial year cannot be deleted.')
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.name


class AccountingPeriodQuerySet(models.QuerySet):
    def _finalized(self):
        return self.filter(financial_year__year_end_closing__status='closed')

    def update(self, **kwargs):
        if kwargs.get('status') == 'closed':
            raise ValidationError('Accounting periods must be closed through the period service.')
        if self._finalized().exists():
            raise ValidationError('Periods in a finalized financial year are immutable.')
        return super().update(**kwargs)

    def delete(self):
        if self._finalized().exists():
            raise ValidationError('Periods in a finalized financial year cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if 'status' in fields and any(obj.status == 'closed' for obj in objects):
            raise ValidationError('Accounting periods must be closed through the period service.')
        if self._finalized().filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('Periods in a finalized financial year are immutable.')
        return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        year_ids = {obj.financial_year_id for obj in objects}
        if YearEndClosing.objects.filter(
            financial_year_id__in=year_ids,
            status='closed',
        ).exists():
            raise ValidationError('Periods cannot be added to a finalized financial year.')
        for obj in objects:
            obj.full_clean()
        return super().bulk_create(objects, **kwargs)


class AccountingPeriod(models.Model):
    STATUS_CHOICES = [('open', 'Open'), ('closing', 'Closing'), ('closed', 'Closed')]

    financial_year = models.ForeignKey(
        FinancialYear,
        on_delete=models.CASCADE,
        related_name='periods',
    )
    name = models.CharField(max_length=80)
    starts_on = models.DateField()
    ends_on = models.DateField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='open')
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='accounting_periods_closed',
    )

    class Meta:
        ordering = ['starts_on']
        constraints = [
            models.UniqueConstraint(fields=['financial_year', 'starts_on'], name='unique_period_start_per_year'),
            models.CheckConstraint(condition=models.Q(ends_on__gte=models.F('starts_on')), name='accounting_period_valid_dates'),
        ]

    objects = AccountingPeriodQuerySet.as_manager()

    def clean(self):
        if not self.starts_on or not self.ends_on or not self.financial_year_id:
            return
        if self.ends_on < self.starts_on:
            raise ValidationError('Accounting-period end date must not be before its start date.')
        year = self.financial_year
        if self.starts_on < year.starts_on or self.ends_on > year.ends_on:
            raise ValidationError('Accounting period must be contained within its financial year.')
        if AccountingPeriod.objects.filter(
            financial_year_id=self.financial_year_id,
            starts_on__lte=self.ends_on,
            ends_on__gte=self.starts_on,
        ).exclude(pk=self.pk).exists():
            raise ValidationError('Accounting periods in a financial year cannot overlap.')

    def save(self, *args, **kwargs):
        if YearEndClosing.objects.filter(
            financial_year_id=self.financial_year_id,
            status='closed',
        ).exists():
            raise ValidationError('Periods in a finalized financial year are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if YearEndClosing.objects.filter(
            financial_year_id=self.financial_year_id,
            status='closed',
        ).exists():
            raise ValidationError('Periods in a finalized financial year cannot be deleted.')
        return super().delete(*args, **kwargs)

    def close(self, *, user):
        from banking.models import BankStatementLine
        from purchases.models import PurchaseInvoice
        from sales.models import SalesInvoice

        with transaction.atomic():
            period = AccountingPeriod.objects.select_for_update().select_related(
                'financial_year'
            ).get(pk=self.pk)
            if period.status != 'open':
                raise ValidationError('Only open accounting periods can be closed.')
            period.status = 'closing'
            period.save(update_fields=['status'])
            organization_id = period.financial_year.organization_id
            open_journals = JournalEntry.objects.filter(
                organization_id=organization_id,
                status='draft',
                entry_date__range=(period.starts_on, period.ends_on),
            ).count()
            draft_sales = SalesInvoice.objects.filter(
                organization_id=organization_id,
                status='draft',
                invoice_date__range=(period.starts_on, period.ends_on),
            ).count()
            draft_purchases = PurchaseInvoice.objects.filter(
                organization_id=organization_id,
                status='draft',
                invoice_date__range=(period.starts_on, period.ends_on),
            ).count()
            unmatched_lines = BankStatementLine.objects.filter(
                import_record__organization_id=organization_id,
                status='unmatched',
                statement_date__range=(period.starts_on, period.ends_on),
            ).count()
            exceptions = []
            if open_journals:
                exceptions.append(f'{open_journals} draft journal entr{"y" if open_journals == 1 else "ies"}')
            if draft_sales:
                exceptions.append(f'{draft_sales} draft sales invoice{"s" if draft_sales != 1 else ""}')
            if draft_purchases:
                exceptions.append(f'{draft_purchases} draft purchase invoice{"s" if draft_purchases != 1 else ""}')
            if unmatched_lines:
                exceptions.append(f'{unmatched_lines} unmatched bank statement line{"s" if unmatched_lines != 1 else ""}')
            if exceptions:
                raise ValidationError('Cannot close period: ' + ', '.join(exceptions) + '.')
            period.status = 'closed'
            period.closed_at = timezone.now()
            period.closed_by = user
            period.save(update_fields=['status', 'closed_at', 'closed_by'])
            PeriodAuditEvent.objects.create(
                period=period,
                action='closed',
                user=user,
            )
            self.status = period.status
            self.closed_at = period.closed_at
            self.closed_by = user
        return self

    def reopen(self, *, user, reason):
        with transaction.atomic():
            period = AccountingPeriod.objects.select_for_update().get(pk=self.pk)
            if period.status != 'closed':
                raise ValidationError('Only closed accounting periods can be reopened.')
            if YearEndClosing.objects.filter(
                financial_year=period.financial_year,
                status='closed',
            ).exists():
                raise ValidationError(
                    'A period in a finalized financial year cannot be reopened. '
                    'Use an approved correcting journal in an open period.'
                )
            if not reason or not reason.strip():
                raise ValidationError('A reason is required to reopen a period.')
            period.status = 'open'
            period.closed_at = None
            period.closed_by = None
            period.save(update_fields=['status', 'closed_at', 'closed_by'])
            PeriodAuditEvent.objects.create(
                period=period,
                action='reopened',
                user=user,
                reason=reason.strip()[:255],
            )
            self.status = period.status
            self.closed_at = None
            self.closed_by = None
        return self

    def __str__(self):
        return self.name


class PeriodAuditEventQuerySet(models.QuerySet):
    def _finalized(self):
        return self.filter(period__financial_year__year_end_closing__status='closed')

    def update(self, **kwargs):
        if self._finalized().exists():
            raise ValidationError('Period audit records in a finalized year are immutable.')
        return super().update(**kwargs)

    def delete(self):
        if self._finalized().exists():
            raise ValidationError('Period audit records in a finalized year cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self._finalized().filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('Period audit records in a finalized year are immutable.')
        return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        period_ids = {obj.period_id for obj in objects}
        if AccountingPeriod.objects.filter(
            pk__in=period_ids,
            financial_year__year_end_closing__status='closed',
        ).exists():
            raise ValidationError('Audit records cannot be added to a finalized year.')
        return super().bulk_create(objects, **kwargs)


class PeriodAuditEvent(models.Model):
    period = models.ForeignKey(AccountingPeriod, on_delete=models.PROTECT, related_name='audit_events')
    action = models.CharField(max_length=20, choices=[('closed', 'Closed'), ('reopened', 'Reopened')])
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    objects = PeriodAuditEventQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if YearEndClosing.objects.filter(
            financial_year_id=self.period.financial_year_id,
            status='closed',
        ).exists():
            raise ValidationError('Audit records cannot be added to a finalized year.')
        if self.pk and PeriodAuditEvent.objects.filter(
            pk=self.pk,
            period__financial_year__year_end_closing__status='closed',
        ).exists():
            raise ValidationError('Period audit records in a finalized year are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if YearEndClosing.objects.filter(
            financial_year_id=self.period.financial_year_id,
            status='closed',
        ).exists():
            raise ValidationError('Period audit records in a finalized year cannot be deleted.')
        return super().delete(*args, **kwargs)


class YearEndClosingQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if kwargs.get('status') == 'closed':
            raise ValidationError('A year-end close can only be finalized by the closing service.')
        if self.filter(status='closed').exists():
            raise ValidationError('A finalized year-end closing is immutable.')
        return super().update(**kwargs)

    def delete(self):
        if self.filter(status='closed').exists():
            raise ValidationError('A finalized year-end closing cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if 'status' in fields and any(obj.status == 'closed' for obj in objects):
            raise ValidationError('A year-end close can only be finalized by the closing service.')
        if self.filter(status='closed').filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('A finalized year-end closing is immutable.')
        return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        if any(obj.status == 'closed' for obj in objects):
            raise ValidationError('A finalized year-end close must be created by the closing service.')
        for obj in objects:
            obj.full_clean()
        return super().bulk_create(objects, **kwargs)


class YearEndClosing(models.Model):
    STATUS_CHOICES = [('closing', 'Closing'), ('closed', 'Closed')]

    financial_year = models.OneToOneField(
        FinancialYear,
        on_delete=models.PROTECT,
        related_name='year_end_closing',
    )
    retained_earnings_account = models.ForeignKey(
        'accounting.Account',
        on_delete=models.PROTECT,
        related_name='year_end_closings',
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='closing')
    profit_loss = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    reason = models.CharField(max_length=255)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='year_end_closings',
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-financial_year__ends_on']

    objects = YearEndClosingQuerySet.as_manager()

    def clean(self):
        if self.retained_earnings_account_id:
            account = self.retained_earnings_account
            if account.organization_id != self.financial_year.organization_id:
                raise ValidationError('Retained earnings account must belong to the financial year organization.')
            if account.account_type != 'equity' or not account.is_active or account.branch_id:
                raise ValidationError('Retained earnings must be an active organization-wide equity account.')
        if self.pk:
            previous = YearEndClosing.objects.filter(pk=self.pk).values(
                'financial_year_id',
                'retained_earnings_account_id',
                'status',
                'profit_loss',
                'reason',
                'closed_by_id',
                'closed_at',
            ).first()
            if previous and previous['status'] == 'closed' and any(
                previous[field] != value
                for field, value in (
                    ('financial_year_id', self.financial_year_id),
                    ('retained_earnings_account_id', self.retained_earnings_account_id),
                    ('status', self.status),
                    ('profit_loss', self.profit_loss),
                    ('reason', self.reason),
                    ('closed_by_id', self.closed_by_id),
                    ('closed_at', self.closed_at),
                )
            ):
                raise ValidationError('A finalized year-end closing is immutable.')

    def save(self, *args, **kwargs):
        if self.pk:
            stored_status = YearEndClosing.objects.filter(pk=self.pk).values_list(
                'status',
                flat=True,
            ).first()
            if stored_status == 'closed':
                raise ValidationError('A finalized year-end closing is immutable.')
            if stored_status == 'closing' and self.status == 'closed':
                raise ValidationError('A year-end close can only be finalized by the closing service.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def finalize(self, *, profit_loss, closed_by, closed_at):
        if self.status != 'closing':
            raise ValidationError('Only an in-progress year-end close can be finalized.')
        journals = list(
            self.journals.select_related('journal_entry').prefetch_related('journal_entry__lines')
        )
        if any(
            journal.journal_entry.status != 'posted'
            or not journal.journal_entry.is_balanced()
            for journal in journals
        ):
            raise ValidationError('All generated year-end journals must be posted and balanced.')
        event = self.audit_events.filter(action='closed').first()
        if event is None or event.reason != self.reason:
            raise ValidationError('A matching close audit event is required before finalization.')
        self.profit_loss = profit_loss
        self.closed_by = closed_by
        self.closed_at = closed_at
        self.status = 'closed'
        self.full_clean()
        return super().save(update_fields=['status', 'profit_loss', 'closed_by', 'closed_at'])

    def delete(self, *args, **kwargs):
        if self.status == 'closed':
            raise ValidationError('A finalized year-end closing cannot be deleted.')
        return super().delete(*args, **kwargs)


class YearEndClosingJournalQuerySet(models.QuerySet):
    def _finalized(self):
        return self.filter(closing__status='closed')

    def update(self, **kwargs):
        if self._finalized().exists():
            raise ValidationError('Generated journals for a finalized close are immutable.')
        return super().update(**kwargs)

    def delete(self):
        if self._finalized().exists():
            raise ValidationError('Generated journals for a finalized close cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self._finalized().filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('Generated journals for a finalized close are immutable.')
        return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        if any(obj.closing_id and obj.closing.status == 'closed' for obj in objects):
            raise ValidationError('Generated journals for a finalized close are immutable.')
        for obj in objects:
            obj.full_clean()
        return super().bulk_create(objects, **kwargs)


class YearEndClosingJournal(models.Model):
    ROLE_CHOICES = [('closing', 'Income and expense closing'), ('opening', 'Next-year opening balances')]

    closing = models.ForeignKey(
        YearEndClosing,
        on_delete=models.PROTECT,
        related_name='journals',
    )
    journal_entry = models.OneToOneField(
        'accounting.JournalEntry',
        on_delete=models.PROTECT,
        related_name='year_end_link',
    )
    role = models.CharField(max_length=12, choices=ROLE_CHOICES)
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='year_end_journals',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['role', 'branch_id', 'journal_entry_id']
        constraints = [
            models.UniqueConstraint(
                fields=['closing', 'role', 'branch'],
                condition=models.Q(branch__isnull=False),
                name='unique_year_end_journal_branch',
            ),
            models.UniqueConstraint(
                fields=['closing', 'role'],
                condition=models.Q(branch__isnull=True),
                name='unique_year_end_journal_org',
            ),
        ]

    objects = YearEndClosingJournalQuerySet.as_manager()

    def clean(self):
        if not self.closing_id or not self.journal_entry_id:
            return
        if self.journal_entry.organization_id != self.closing.financial_year.organization_id:
            raise ValidationError('Year-end journal must belong to the closing organization.')
        if self.journal_entry.branch_id != self.branch_id:
            raise ValidationError('Year-end journal branch must match the linked branch.')
        expected_date = (
            self.closing.financial_year.ends_on
            if self.role == 'closing'
            else self.closing.financial_year.ends_on.fromordinal(
                self.closing.financial_year.ends_on.toordinal() + 1
            )
        )
        if self.journal_entry.entry_date != expected_date:
            raise ValidationError('Year-end journal date does not match its closing role.')

    def save(self, *args, **kwargs):
        if self.closing_id and self.closing.status == 'closed':
            if self._state.adding:
                raise ValidationError('Generated journals cannot be added to a finalized close.')
            raise ValidationError('Generated journals for a finalized close are immutable.')
        if self.pk and YearEndClosingJournal.objects.filter(
            pk=self.pk,
            closing__status='closed',
        ).exists():
            raise ValidationError('Generated journals for a finalized close are immutable.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.closing.status == 'closed':
            raise ValidationError('Generated journals for a finalized close cannot be deleted.')
        return super().delete(*args, **kwargs)


class YearEndClosingEventQuerySet(models.QuerySet):
    def _finalized(self):
        return self.filter(closing__status='closed')

    def update(self, **kwargs):
        if self._finalized().exists():
            raise ValidationError('Audit records for a finalized close are immutable.')
        return super().update(**kwargs)

    def delete(self):
        if self._finalized().exists():
            raise ValidationError('Audit records for a finalized close cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self._finalized().filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('Audit records for a finalized close are immutable.')
        return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        if any(obj.closing_id and obj.closing.status == 'closed' for obj in objects):
            raise ValidationError('Audit records cannot be added to a finalized close.')
        for obj in objects:
            obj.full_clean()
        return super().bulk_create(objects, **kwargs)


class YearEndClosingEvent(models.Model):
    closing = models.ForeignKey(
        YearEndClosing,
        on_delete=models.PROTECT,
        related_name='audit_events',
    )
    action = models.CharField(max_length=20, choices=[('closed', 'Closed')])
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    reason = models.CharField(max_length=255)
    journal_references = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    objects = YearEndClosingEventQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.closing_id and self.closing.status == 'closed':
            raise ValidationError('Audit records for a finalized close are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.closing.status == 'closed':
            raise ValidationError('Audit records for a finalized close cannot be deleted.')
        return super().delete(*args, **kwargs)


class AccountingBudget(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='accounting_budgets')
    period = models.ForeignKey(AccountingPeriod, on_delete=models.PROTECT, related_name='budgets')
    account = models.ForeignKey('accounting.Account', on_delete=models.PROTECT, related_name='budgets')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='accounting_budgets')
    department = models.CharField(max_length=100, blank=True)
    cost_center = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['period__starts_on', 'account__code']
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gte=0), name='budget_amount_nonnegative'),
        ]

    def clean(self):
        if self.organization_id and self.period_id and self.period.financial_year.organization_id != self.organization_id:
            raise ValidationError('Budget period must belong to its organization.')
        if self.organization_id and self.account_id and self.account.organization_id != self.organization_id:
            raise ValidationError('Budget account must belong to its organization.')
        if self.branch_id and self.branch.organization_id != self.organization_id:
            raise ValidationError('Budget branch must belong to its organization.')
        if self.branch_id and self.account.branch_id and self.account.branch_id != self.branch_id:
            raise ValidationError('Budget account branch does not match the budget branch.')
        duplicate = AccountingBudget.objects.filter(
            organization_id=self.organization_id,
            period_id=self.period_id,
            account_id=self.account_id,
            branch_id=self.branch_id,
            department=self.department,
            cost_center=self.cost_center,
        ).exclude(pk=self.pk)
        if duplicate.exists():
            raise ValidationError('A budget already exists for this account, period and dimensions.')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class FixedAssetCategory(models.Model):
    DEPRECIATION_CHOICES = [
        ('straight_line', 'Straight line'),
        ('reducing_balance', 'Reducing balance'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='asset_categories')
    name = models.CharField(max_length=120)
    asset_account = models.ForeignKey('accounting.Account', on_delete=models.PROTECT, related_name='asset_categories')
    accumulated_depreciation_account = models.ForeignKey('accounting.Account', on_delete=models.PROTECT, related_name='depreciation_categories')
    depreciation_expense_account = models.ForeignKey('accounting.Account', on_delete=models.PROTECT, related_name='depreciation_expense_categories')
    depreciation_method = models.CharField(max_length=20, choices=DEPRECIATION_CHOICES, default='straight_line')
    annual_rate = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal('0'))
    default_useful_life_months = models.PositiveIntegerField(default=60)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'name'], name='unique_asset_category_per_org'),
            models.CheckConstraint(condition=models.Q(annual_rate__gte=0, annual_rate__lte=100), name='asset_rate_between_zero_and_hundred'),
            models.CheckConstraint(condition=models.Q(default_useful_life_months__gt=0), name='asset_category_life_positive'),
        ]

    def clean(self):
        accounts = [
            account
            for account in (
                self.asset_account if self.asset_account_id else None,
                self.accumulated_depreciation_account if self.accumulated_depreciation_account_id else None,
                self.depreciation_expense_account if self.depreciation_expense_account_id else None,
            )
            if account is not None
        ]
        if self.organization_id and any(account.organization_id != self.organization_id for account in accounts):
            raise ValidationError('All fixed-asset accounts must belong to the category organization.')
        if self.asset_account_id and self.asset_account.account_type != 'asset':
            raise ValidationError({'asset_account': 'Asset account must be an asset account.'})
        if (
            self.accumulated_depreciation_account_id
            and self.accumulated_depreciation_account.account_type != 'asset'
        ):
            raise ValidationError({
                'accumulated_depreciation_account': 'Accumulated depreciation must use an asset account.'
            })
        if (
            self.depreciation_expense_account_id
            and self.depreciation_expense_account.account_type != 'expense'
        ):
            raise ValidationError({
                'depreciation_expense_account': 'Depreciation expense must use an expense account.'
            })
        if self.depreciation_method == 'reducing_balance' and self.annual_rate is not None and self.annual_rate <= 0:
            raise ValidationError('Reducing-balance depreciation requires a positive annual rate.')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class FixedAsset(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='fixed_assets')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='fixed_assets')
    category = models.ForeignKey(FixedAssetCategory, on_delete=models.PROTECT, related_name='assets')
    asset_code = models.CharField(max_length=80)
    name = models.CharField(max_length=150)
    acquisition_date = models.DateField()
    acquisition_cost = models.DecimalField(max_digits=18, decimal_places=2)
    salvage_value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    useful_life_months = models.PositiveIntegerField()
    acquisition_entry = models.OneToOneField('accounting.JournalEntry', on_delete=models.PROTECT, null=True, blank=True, related_name='capitalized_asset')
    disposed_at = models.DateField(null=True, blank=True)
    disposal_entry = models.OneToOneField('accounting.JournalEntry', on_delete=models.PROTECT, null=True, blank=True, related_name='disposed_asset')
    disposal_proceeds = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['asset_code']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'asset_code'], name='unique_fixed_asset_code'),
            models.CheckConstraint(condition=models.Q(acquisition_cost__gt=0), name='fixed_asset_cost_positive'),
            models.CheckConstraint(condition=models.Q(salvage_value__gte=0), name='fixed_asset_salvage_nonnegative'),
            models.CheckConstraint(condition=models.Q(useful_life_months__gt=0), name='fixed_asset_life_positive'),
        ]

    def clean(self):
        if self.category_id and self.category.organization_id != self.organization_id:
            raise ValidationError('Asset category must belong to the asset organization.')
        if self.branch_id and self.branch.organization_id != self.organization_id:
            raise ValidationError('Asset branch must belong to its organization.')
        category_accounts = [
            self.category.asset_account,
            self.category.accumulated_depreciation_account,
            self.category.depreciation_expense_account,
        ] if self.category_id else []
        if any(account.branch_id and account.branch_id != self.branch_id for account in category_accounts):
            raise ValidationError('Fixed-asset accounts must be available to the asset branch.')
        if (
            self.salvage_value is not None
            and self.acquisition_cost is not None
            and self.salvage_value > self.acquisition_cost
        ):
            raise ValidationError('Salvage value cannot exceed acquisition cost.')
        if self.acquisition_date and self.disposed_at and self.disposed_at < self.acquisition_date:
            raise ValidationError('Disposal date cannot be before acquisition date.')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def accumulated_depreciation(self):
        return self.depreciation_records.filter(journal_entry__status='posted').aggregate(
            total=models.Sum('amount')
        )['total'] or Decimal('0')

    @property
    def net_book_value(self):
        return max(self.acquisition_cost - self.accumulated_depreciation, self.salvage_value)

    def __str__(self):
        return f'{self.asset_code} - {self.name}'


class FixedAssetDepreciation(models.Model):
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name='depreciation_records')
    period = models.ForeignKey(AccountingPeriod, on_delete=models.PROTECT, related_name='asset_depreciations')
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    journal_entry = models.OneToOneField('accounting.JournalEntry', on_delete=models.PROTECT, null=True, blank=True, related_name='asset_depreciation')
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['period__starts_on', 'asset__asset_code']
        constraints = [
            models.UniqueConstraint(fields=['asset', 'period'], name='unique_asset_depreciation_period'),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='asset_depreciation_positive'),
        ]


class FixedAssetEvent(models.Model):
    EVENT_CHOICES = [('registered', 'Registered'), ('transferred', 'Transferred'), ('disposed', 'Disposed')]

    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name='events')
    event_type = models.CharField(max_length=20, choices=EVENT_CHOICES)
    from_branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='asset_events_from')
    to_branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='asset_events_to')
    journal_entry = models.ForeignKey('accounting.JournalEntry', on_delete=models.PROTECT, null=True, blank=True, related_name='asset_events')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    details = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']


def ensure_accounting_period_open(organization_id, entry_date):
    years = FinancialYear.objects.filter(organization_id=organization_id)
    if not years.exists():
        return
    period = AccountingPeriod.objects.select_for_update().filter(
        financial_year__organization_id=organization_id,
        starts_on__lte=entry_date,
        ends_on__gte=entry_date,
    ).first()
    if period is None:
        raise ValidationError('No accounting period is configured for this transaction date.')
    if period.status != 'open':
        raise ValidationError(f'Accounting period {period.name} is {period.status} and does not accept postings.')


class Account(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ('asset', 'Asset'),
        ('liability', 'Liability'),
        ('equity', 'Equity'),
        ('income', 'Income'),
        ('expense', 'Expense'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='accounts')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='accounts')
    code = models.CharField(max_length=25)
    name = models.CharField(max_length=150)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children')
    current_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [('organization', 'code')]
        ordering = ['code']

    def __str__(self):
        return f'{self.code} - {self.name}'


class JournalEntryQuerySet(models.QuerySet):
    def _finalized(self):
        return self.filter(year_end_link__closing__status='closed')

    def update(self, **kwargs):
        if self._finalized().exists():
            raise ValidationError('Journals generated by a finalized close are immutable.')
        return super().update(**kwargs)

    def delete(self):
        if self._finalized().exists():
            raise ValidationError('Journals generated by a finalized close cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self._finalized().filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('Journals generated by a finalized close are immutable.')
        return super().bulk_update(objects, fields, batch_size=batch_size)


class JournalEntry(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('reversed', 'Reversed'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='journal_entries')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')
    reference = models.CharField(max_length=80, unique=True)
    entry_date = models.DateField()
    narration = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-entry_date', '-id']

    objects = JournalEntryQuerySet.as_manager()

    def __str__(self):
        return self.reference

    def save(self, *args, **kwargs):
        if self.pk and YearEndClosingJournal.objects.filter(
            journal_entry_id=self.pk,
            closing__status='closed',
        ).exists():
            raise ValidationError('Journals generated by a finalized close are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if YearEndClosingJournal.objects.filter(
            journal_entry_id=self.pk,
            closing__status='closed',
        ).exists():
            raise ValidationError('Journals generated by a finalized close cannot be deleted.')
        return super().delete(*args, **kwargs)

    @property
    def total_debit(self):
        return sum((line.debit for line in self.lines.all()), Decimal('0'))

    @property
    def total_credit(self):
        return sum((line.credit for line in self.lines.all()), Decimal('0'))

    def is_balanced(self):
        return self.total_debit == self.total_credit

    def post(self):
        return self._post()

    def post_year_end_close(self, *, closing):
        return self._post(year_end_closing=closing)

    def post_year_end_opening(self, *, closing):
        return self._post(year_end_opening=closing)

    def _post(self, *, year_end_closing=None, year_end_opening=None):
        if year_end_closing is not None and year_end_opening is not None:
            raise ValidationError('A journal cannot be both a closing and opening entry.')
        if self.status == 'posted':
            return self
        with transaction.atomic():
            entry = JournalEntry.objects.select_for_update().get(pk=self.pk)
            if entry.status == 'posted':
                self.status = entry.status
                return self
            if entry.status != 'draft':
                raise ValidationError('Only draft journal entries can be posted.')
            if entry.branch_id and entry.branch.organization_id != entry.organization_id:
                raise ValidationError('Journal entry branch must belong to its organization.')
            if year_end_closing is None:
                if year_end_opening is None:
                    ensure_accounting_period_open(entry.organization_id, entry.entry_date)
                else:
                    closing = YearEndClosing.objects.select_for_update().select_related(
                        'financial_year'
                    ).get(pk=year_end_opening.pk)
                    link_exists = YearEndClosingJournal.objects.filter(
                        closing=closing,
                        journal_entry=entry,
                        role='opening',
                    ).exists()
                    next_year = FinancialYear.objects.filter(
                        organization_id=entry.organization_id,
                        starts_on=closing.financial_year.ends_on.fromordinal(
                            closing.financial_year.ends_on.toordinal() + 1
                        ),
                    ).first()
                    if (
                        closing.status != 'closing'
                        or closing.financial_year.organization_id != entry.organization_id
                        or next_year is None
                        or entry.entry_date != next_year.starts_on
                        or not link_exists
                    ):
                        raise ValidationError('This journal is not an authorized year-end opening entry.')
                    ensure_accounting_period_open(entry.organization_id, entry.entry_date)
            else:
                closing = YearEndClosing.objects.select_for_update().select_related(
                    'financial_year'
                ).get(pk=year_end_closing.pk)
                is_authorized_close_entry = YearEndClosingJournal.objects.filter(
                    closing=closing,
                    journal_entry=entry,
                    role='closing',
                ).exists()
                period = AccountingPeriod.objects.select_for_update().filter(
                    financial_year=closing.financial_year,
                    starts_on__lte=entry.entry_date,
                    ends_on__gte=entry.entry_date,
                ).first()
                if (
                    closing.status != 'closing'
                    or closing.financial_year.organization_id != entry.organization_id
                    or entry.entry_date != closing.financial_year.ends_on
                    or not is_authorized_close_entry
                    or period is None
                    or period.status != 'closed'
                ):
                    raise ValidationError('This journal is not an authorized year-end closing entry.')

            lines = list(entry.lines.select_related('account').all())
            if not lines:
                raise ValidationError('Journal entry must contain at least one line.')
            if sum((line.debit for line in lines), Decimal('0')) != sum(
                (line.credit for line in lines), Decimal('0')
            ):
                raise ValidationError('Journal entry is not balanced.')

            account_ids = {line.account_id for line in lines}
            accounts = {
                account.pk: account
                for account in Account.objects.select_for_update().filter(pk__in=account_ids)
            }
            for line in lines:
                if line.debit < 0 or line.credit < 0 or bool(line.debit) == bool(line.credit):
                    raise ValidationError('Each journal line must contain one positive debit or credit.')
                account = accounts[line.account_id]
                if account.organization_id != entry.organization_id:
                    raise ValidationError('Journal lines must use accounts from the entry organization.')
                if not account.is_active:
                    raise ValidationError('Journal lines cannot use inactive accounts.')
                if account.branch_id and account.branch_id != entry.branch_id:
                    raise ValidationError('Journal line account branch does not match the entry branch.')

            if year_end_opening is None:
                for line in lines:
                    account = accounts[line.account_id]
                    account.current_balance += line.debit - line.credit
                    account.save(update_fields=['current_balance'])
            entry.status = 'posted'
            entry.save(update_fields=['status'])
            self.status = entry.status
        return self

    def reverse(self, *, reference, entry_date, narration='', created_by=None):
        with transaction.atomic():
            entry = JournalEntry.objects.select_for_update().get(pk=self.pk)
            if entry.status != 'posted':
                raise ValidationError('Only posted journal entries can be reversed.')
            if JournalEntry.objects.filter(reference=reference).exists():
                raise ValidationError('A journal entry already uses this reversal reference.')
            lines = list(entry.lines.select_related('account').all())
            reversal = JournalEntry.objects.create(
                organization=entry.organization,
                branch=entry.branch,
                reference=reference,
                entry_date=entry_date,
                narration=narration or f'Reversal of {entry.reference}',
                created_by=created_by,
            )
            JournalEntryLine.objects.bulk_create(
                [
                    JournalEntryLine(
                        entry=reversal,
                        account=line.account,
                        description=f'Reversal: {line.description}'[:200],
                        debit=line.credit,
                        credit=line.debit,
                    )
                    for line in lines
                ]
            )
            reversal.post()
            entry.status = 'reversed'
            entry.save(update_fields=['status'])
            self.status = entry.status
            return reversal


class JournalEntryLineQuerySet(models.QuerySet):
    def _finalized(self):
        return self.filter(entry__year_end_link__closing__status='closed')

    def update(self, **kwargs):
        if self._finalized().exists():
            raise ValidationError('Journal lines for a finalized close are immutable.')
        return super().update(**kwargs)

    def delete(self):
        if self._finalized().exists():
            raise ValidationError('Journal lines for a finalized close cannot be deleted.')
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        objects = list(objs)
        if self._finalized().filter(pk__in=[obj.pk for obj in objects]).exists():
            raise ValidationError('Journal lines for a finalized close are immutable.')
        return super().bulk_update(objects, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        objects = list(objs)
        entry_ids = {obj.entry_id for obj in objects if obj.entry_id is not None}
        if entry_ids and YearEndClosingJournal.objects.filter(
            journal_entry_id__in=entry_ids,
            closing__status='closed',
        ).exists():
            raise ValidationError('Journal lines cannot be added to a finalized close.')
        return super().bulk_create(objects, **kwargs)


class JournalEntryLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='journal_lines')
    description = models.CharField(max_length=200, blank=True)
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    def __str__(self):
        return f'{self.account.name} - {self.debit or self.credit}'

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(debit__gt=0, credit=0) | models.Q(credit__gt=0, debit=0)),
                name='journal_line_one_positive_side',
            ),
        ]

    objects = JournalEntryLineQuerySet.as_manager()

    def clean(self):
        if self.entry_id and self.account_id:
            if self.entry.organization_id != self.account.organization_id:
                raise ValidationError('Journal line account must belong to the entry organization.')
            if self.account.branch_id and self.entry.branch_id != self.account.branch_id:
                raise ValidationError('Journal line account must belong to the entry branch.')
        if self.debit < 0 or self.credit < 0:
            raise ValidationError('Journal debit and credit amounts cannot be negative.')
        if bool(self.debit) == bool(self.credit):
            raise ValidationError('Each journal line must contain exactly one positive debit or credit.')

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.entry_id:
                entry = JournalEntry.objects.select_for_update().get(pk=self.entry_id)
                if entry.status != 'draft':
                    raise ValidationError('Posted journal entry lines are immutable.')
                self.entry = entry
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            entry = JournalEntry.objects.select_for_update().get(pk=self.entry_id)
            if entry.status != 'draft':
                raise ValidationError('Posted journal entry lines are immutable.')
            return super().delete(*args, **kwargs)


def ensure_default_accounts(organization, branch=None):
    defaults = [
        ('1001', 'Cash', 'asset'),
        ('1100', 'Accounts Receivable', 'asset'),
        ('1150', 'Supplier Advances', 'asset'),
        ('1200', 'Inventory', 'asset'),
        ('1300', 'Bank', 'asset'),
        ('2000', 'Accounts Payable', 'liability'),
        ('2050', 'Customer Advances', 'liability'),
        ('2100', 'GST Payable', 'liability'),
        ('2150', 'Goods Received Not Invoiced', 'liability'),
        ('2200', 'GST Input', 'asset'),
        ('3000', 'Equity', 'equity'),
        ('4000', 'Sales Revenue', 'income'),
        ('4050', 'Inventory Adjustment Gain', 'income'),
        ('4900', 'Fixed Asset Disposal Gain', 'income'),
        ('5000', 'Cost of Goods Sold', 'expense'),
        ('5050', 'Inventory Adjustment Loss', 'expense'),
        ('5900', 'Fixed Asset Disposal Loss', 'expense'),
    ]
    created = {}
    for code, name, account_type in defaults:
        account, _ = Account.objects.get_or_create(
            organization=organization,
            code=code,
            defaults={
                'branch': branch,
                'name': name,
                'account_type': account_type,
            },
        )
        if account.branch_id is not None:
            account.branch = None
            account.save(update_fields=['branch'])
        created[name.lower().replace(' ', '_')] = account
    return created

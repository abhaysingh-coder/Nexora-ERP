# Financial controls and settlement workflows

This document describes the accounting periods, budgets, fixed assets and invoice-settlement features currently implemented in Nexora ERP. These are application-level accounting controls; they are not a substitute for an organization's approved financial policies or jurisdiction-specific accounting advice.

## Receivables and payables

New bank receipts and supplier payments can be allocated to one or more posted invoices. Allocations may be partial, and a payment may be split across invoices. The service checks the counterparty, invoice state, organization/branch scope, unapplied transaction amount and invoice balance inside a database transaction.

An unallocated receipt/payment remains an advance. When an allocation is made, the application posts a journal reclassification between the relevant advance and receivable/payable control account. It does not post cash or bank a second time. Allocation reversals require a reason, preserve the original allocation, and post a correcting journal entry.

The receivables and payables registers provide search/filtering, pagination, ageing, invoice allocation history, unapplied advance balances and CSV output. Existing historical receipts/payments are migrated as legacy settlement transactions and are included in aggregate outstanding balances; the application does not fabricate invoice assignments for them. The older `CustomerReceipt` model remains a separate legacy workflow.

Posted customer credit notes reduce the customer balance and original invoice open amount. Posted supplier debit notes reduce supplier balances and, when tied to an invoice, that invoice's open amount. A supplier return from a receipt that is not yet invoiced creates an aggregate supplier credit and can make the supplier balance negative. Cash/payment allocations do not yet transfer these credit-note balances to another invoice or automatically issue a refund; the credit remains visible in the counterparty balance.

Returns post a separate balanced journal and preserve the original transaction. Customer credits reverse revenue/output GST and receivables; restockable goods also reverse inventory/COGS. Supplier debits reverse payables, inventory and eligible input tax, including the payable/input sides of reverse-charge GST. Return dates are subject to the same open-period checks as other journals. See [RETURNS.md](RETURNS.md) for quantity controls and source-document rules.

## Bank reconciliation

CSV imports are validated atomically and duplicate imports are detected. Imported statement lines begin unmatched. Candidate suggestions may use account, signed amount, date within the configured tolerance, reference and description. Suggestions show matching reasons but are not confirmations.

Reconciliation requires a user to select a posted transaction and confirm an exact account, transaction date and signed amount match. Unreconciliation requires the authorized path and a reason. Match and unmatch events are retained with user and timestamp. Suggested matching tolerance is configured as `BANK_RECONCILIATION_DATE_TOLERANCE_DAYS` and defaults to three days; it does not relax the exact confirmation rules.

## Financial years and periods

Organization-level finance users can create non-overlapping financial years. The application generates monthly periods covering the selected year dates. Periods are opened by default; closing performs checks for draft journal entries, draft sales/purchase invoices and unmatched bank statement lines in that date range. Reopening a closed period requires a reason and creates an audit event.

Journal posting is rejected when the journal date falls into a closed period. If an organization has not configured financial years, the application preserves legacy behavior and allows posting without period validation. Period management is organization-level; branch-bound finance users cannot create financial years or close periods.

## Year-end close and opening balances

An organization-level finance user can open the year-end page from the financial-periods screen. Before close, create the immediately following financial year and an open first period covering its first date. Configure an active organization-wide equity account for retained earnings, either on year creation or on the close page.

The preview is read-only. It checks full period coverage and period closure, draft journals, draft sales/purchase invoices, draft customer receipts, draft bank transactions, pending bank statement imports, unmatched bank statement lines, posted journal balance and organization/branch integrity for all posted activity through year-end, cumulative trial-balance balance, retained-earnings account validity, next-year readiness and opening-balance balance. Historical posted activity is included in the opening-balance calculation, so its journal and account relationships are validated as well. It also rejects nonzero income/expense balances left from before the year being closed; earlier financial years must be closed first.

### Accounting convention

The journal line balance is debit minus credit. Income and expenses are summarized from posted activity dated inclusively within the financial year. Each nonzero income/expense account is cleared in a closing journal dated on the year end. The net profit is credited to retained earnings; a net loss debits retained earnings. These close journals are grouped by branch, preserving the original branch attribution.

Opening balances are calculated from the posted balance-sheet account activity through year-end, with the current-year profit/loss adjustment included once in retained earnings. Assets, liabilities and equity are carried; income and expense accounts are not. A positive debit-minus-credit balance opens as a debit, while a negative balance opens as a credit. Zero balances create no journal lines. Branch and organization-wide ledger dimensions remain separate, and each opening journal must balance independently. Generated journal-to-branch links protect branch records from deletion so the audit attribution remains intact.

This application also maintains a cumulative ledger across dates. Therefore an opening journal is a fiscal-year snapshot, not a second economic transaction: selected-financial-year reports include it, while cumulative reports and the account balance cache exclude its effect. This avoids double-counting the previous year's balances. The special opening-posting path still validates the journal and the next year's open period; it only avoids applying the snapshot a second time to the cumulative balance cache.

Closing journals use the dedicated `JournalEntry.post_year_end_close()` path. It is limited to journals linked to the in-progress close, dated exactly on the financial-year end, with a closed period, and it performs the same line, account and balancing checks as normal posting. It is not a general closed-period posting bypass. Opening journals are linked to the same close record and use a separately validated posting path for the next year's open period.

### Authorization, audit and corrections

Only an active organization-level owner, admin, accountant or finance manager (or an organization-level superuser) can preview or confirm a close. Branch-bound users cannot close or reopen it. Confirmation requires a reason and an explicit checkbox. One database-unique close record per financial year and row locking make sequential retries idempotent and prevent duplicate generated journals. The close record stores the user, timestamp, reason and profit/loss snapshot; linked journal rows and an audit event retain all generated references.

A finalized year cannot be reopened or edited, its periods cannot be reopened, and its generated journals and journal links cannot be edited or deleted through model operations. This is deliberate: deleting or reversing opening entries could invalidate later activity. Corrections must use a controlled, approved journal in an open period. If a close is found incorrect, stop posting, preserve the records, and have an authorized accountant design a separately approved correction; there is no automated recovery/reversal workflow.

### Database and migration considerations

The configured default backend is SQLite (`db.sqlite3` in the project root). SQLite does not provide effective `SELECT ... FOR UPDATE` row locking; database uniqueness constraints still prevent duplicate close records, but concurrent requests may fail with a database lock error and should be retried only after checking the close page for a completed close. Concurrency behavior should be tested on the intended production backend (typically PostgreSQL) before live use.

Before applying migrations to a business database:

1. Confirm the active settings module and inspect `settings.DATABASES['default']` (do not assume the development database is the target).
2. Back up the database and verify that the backup can be read/restored in a separate development location.
3. Run `.\.venv\Scripts\python.exe manage.py showmigrations accounting` and `.\.venv\Scripts\python.exe manage.py migrate --plan`.
4. Apply migrations only to the intended database during a controlled maintenance window; retain the backup until migration and application checks complete.

For the default SQLite setup, make a consistent file backup while the application is stopped (or use SQLite's online backup facility). Restore by preserving the original file and copying the verified backup back only after stopping the application. Never reset or delete the business database to apply this feature.

To exercise a close safely, use a separate development database, create two consecutive financial years and their monthly periods, enter posted and balanced test journals, configure retained earnings, close every period in the prior year, review the preview, then confirm with a reason. Verify both the selected-year reports and cumulative reports after posting. Do not test by closing a live business year.

## Budgets

Budgets are configured by accounting period and account, with optional branch, department and cost centre. The budget-versus-actual report derives actual activity from posted journals and provides CSV export. Branch users are scoped to their branch and organization-wide budget rows.

Budget approval thresholds, workflow routing, commitments and purchase-order encumbrances are not implemented.

## Fixed assets

Organization finance users configure asset categories with an asset account, accumulated-depreciation account, depreciation expense account, method and defaults. Branch finance users can register assets within their branch and perform allowed operations on assets in scope; creating shared asset categories and transferring assets between branches is restricted to organization-level finance users.

Capitalization creates an asset record, a journal entry and history event transactionally. Straight-line and reducing-balance depreciation are available, and an asset cannot receive a duplicate depreciation posting for the same period. Transfers and disposals create history events; disposal posts the asset cost, accumulated depreciation, proceeds and resulting gain/loss. The register includes pagination and CSV export.

Current limitation: capitalization posts the acquisition cost against the default cash ledger account. There is no supplier-invoice capitalization workflow, component accounting, impairment, revaluation, tax depreciation, or statutory depreciation certification. Confirm the acquisition and ledger configuration before using this workflow for live books.

## Migrations and environment

Apply migrations only after backing up the target database and confirming the correct environment:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
```

The automated tests use Django's configured test database. This implementation work did not intentionally modify business records in the local application database.

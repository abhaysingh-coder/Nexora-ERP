# Nexora ERP Implementation Status

## Current state

- Foundation: Implemented
- Authentication and organization layer: Implemented, verification incomplete
- Core accounting: Tested and verified
- Inventory: Tested and verified
- Purchase and sales vertical slice: Implemented, verification incomplete
- Banking module: Implemented and verified
- Dashboard shell: Implemented
- Latest automated verification: `manage.py check` and migration drift check passed; full Django suite passed 149 tests; pytest passed 73 tests and 366 subtests. See the Phase 4 report; do not infer authorization completion or production readiness.

## Phase 4 status (2026-10-24)

- Organization context now uses active `OrganizationMembership` as the authority for non-superusers, with a scoped selector for multiple organizations. A superuser-only Django admin workflow provisions organizations, branches, memberships and user branch assignments. Existing non-superusers are not granted a membership automatically. This unit is implemented; full security verification remains incomplete.
- User explicitly confirmed the Phase 3 no-GRNI rule. `GoodsReceipt.post()` no longer posts Inventory/GRNI or creates accrual rows; the unused `GoodsReceiptAccrual` models and their unapplied, generated migration were removed. The inventory report retains the unbilled receipt adjustment. The pre-fix -100.00 reconciliation test failure is documented in the Phase 4 report; post-fix Django and pytest suites pass.
- Membership role checks across sales, purchases, returns, inventory, accounting, banking and taxation now use shared `core.permissions.has_organization_role()`, with existing module role sets preserved. This is a code reuse improvement, not a final approved capability matrix.
- Banking account creation, transaction posting, CSV statement import and reconciliation confirmation now require the explicitly approved active membership roles `owner`, `admin`, `accountant` or `finance_manager`. Allocation continues to use its pre-existing broader role set.
- Concurrent return posting retries only bounded SQLite database-lock errors after a full atomic rollback. This helps the existing SQLite concurrency test; PostgreSQL locking and concurrency remain unverified.
- Authorization audit confirmed that the shared membership-role predicate previously accepted inactive user objects if they held a matching active membership. It now rejects inactive users before superuser bypass; regression tests cover ordinary and superuser cases. `PHASE_4_AUTHORIZATION_INVENTORY.md` records resolver/admin/export/task/upload totals and the still-unverified operations.
- `manage.py check` and migration drift checks must be read from the latest validation results in `PHASE_4_IMPLEMENTATION_REPORT.md`; no migration has been applied to the business database.
- The full organization and permission review is not complete across every screen, export, API, admin action and background operation. A deployed ordinary user needs an authorized superuser to provision an active membership and role before accessing business screens.

## Phase 4 authorization milestone update (2026-10-31)

- Live resolver enumeration remains **72 application routes**; **24 admin models** are registered. Tests exercise common unauthenticated/active-owner GET behavior and reject PUT/PATCH/DELETE for each route, but do not test each route's supported POST, every role, tenant/branch/object combination, or query manipulation. See the route-by-route manifest and explicit test status in `PHASE_4_AUTHORIZATION_INVENTORY.md`.
- The prior export inventory missed inventory valuation. The current source supports 12 CSV output modes across 11 route patterns (accounting 7, banking 2, tax 1, inventory valuation 2). Report/export grants are not implemented as a unified capability and the missing owner matrix blocks changes.
- The earlier note about user creation/update audit gaps is stale for the current tree: `core/admin.py` emits user create/update/password-change events and `organizations/tests.py` covers those flows. Full per-model admin request and exactly-once audit verification remains incomplete.
- The owner referred to a revised role matrix but did not include its contents, then explicitly deferred approval. No unresolved access was silently granted. The proposal is marked **DEFERRED / NOT APPROVED** in `ROLE_SCREEN_MATRIX.md`; auditor, read-only, finance-manager, branch-manager, organization-wide-viewer, and export-operator access remains blocked pending a complete decision.
- Continuation checks at that milestone: Python 3.12.0, Django system check passed, migration drift check reported no changes, full Django suite passed 140 tests, and pytest passed 70 tests and 366 subtests. The test runner used an isolated database. No business database migration was run.
- A read-only security review found no confirmed exploitable vulnerability in its reviewed scope; route-, role-, export-, admin-, and branch-level gaps remain and are not represented as secured. **Phase 4 remains in progress; Phase 5 has not started.**

## Phase 4 export-isolation continuation (2026-10-31)

- Added direct CSV-content regressions for the tax register and banking receivables/payables. Tests cover records from two organizations and multiple branches, tampered organization/branch parameters, related party IDs, date ranges, invalid dates, and exclusion of unauthorized rows. The focused taxation/banking run passed 34 tests.
- Added content-level regression tests for the remaining six accounting CSV modes: general ledger, profit and loss, balance sheet, year-end opening, budget-vs-actual, and fixed assets. The tests parse the downloaded CSV, use two organizations and multiple branches, and exercise applicable foreign IDs and query tampering.
- The fixed-asset CSV annotation/property collision and uncaught invalid-calendar-date `ValueError` were reproduced and narrowly fixed. No access grant was added.
- Latest complete validation: `python manage.py check` passed; `python manage.py makemigrations --check --dry-run` reported no changes; `python manage.py test` passed 146 tests; `python -m pytest -q` passed 70 tests and 366 subtests. `python manage.py test accounting.tests` passed 34 tests.
- All 12 CSV output modes now have selected direct content-level tenant/branch regression evidence. This does not cover every role, filter permutation, date, object or branch-null policy. At that milestone, dashboard isolation was directly asserted only for the sales KPI; see the subsequent dashboard continuation below.
- Branch-null ledger, asset and budget actual visibility remains policy-dependent; year-end export is organization-level and branch-assigned users are denied. Report/export role matrix remains **DEFERRED / NOT APPROVED** in `ROLE_SCREEN_MATRIX.md`.
- Phase 4 remains **IN PROGRESS** and Phase 5 has **NOT STARTED**. Tests use an isolated SQLite database; PostgreSQL concurrency and business-database migration behavior were not tested.

## Phase 4 owner policy and dashboard continuation

- Recorded owner selections on 2026-11-01 for all eight policy models in `docs/PHASE_4_OWNER_POLICY_DECISIONS.md`. Implemented the approved P4-POL-04/05 separation: branch-null ledger entries/aggregates and budget actuals are displayed separately from branch-attributed totals/variance. No role grants were added. Role-to-capability assignments for report, export, auditor, shared-asset, organization-wide, and year-end capabilities remain blocked; `ROLE_SCREEN_MATRIX.md` remains unchanged and **DEFERRED / NOT APPROVED**.
- Added focused dashboard isolation tests covering branch-assigned and organization-level owners, active membership resolution, session/query tampering, posted branch-null/branch-specific sales and purchases, inventory value and exceptions, low-stock counts, cash/bank balances, receivables/payables, and recent journal activity. Tests assert rendered values and references for two organizations, two branches, and branch-null/shared records.
- Dashboard source inspection found no displayed tax summary, charts, customer/supplier counts, or separate sales/purchase activity list. Branch-null treatment varies by metric: branch sales/purchase KPI totals exclude branch-null invoices; their posted journals now appear separately under approved P4-POL-04; posting routes stock movements through an organization warehouse, affecting that warehouse's branch valuation. The branch dashboard excludes null-branch cash accounts and shows branch-null journal activity separately. The inventory helper also exposes organization-wide legacy unallocated-stock exception counts to branch views, for which no policy was approved.
- Focused dashboard and accounting scope-separation tests pass (3 dashboard tests and 5 accounting tests across trial balance, general ledger, profit and loss, balance sheet, and budget CSV/UI). Full validation for the approved code changes is pending. The previous full-suite results predate the code changes. Phase 4 remains **IN PROGRESS**; Phase 5 has **NOT STARTED**.

## Completed work

- Django project scaffold created with a modular app structure.
- Custom user model and organization/branch models added.
- Accounting posting locks entries and accounts, validates balanced non-empty journals and organization/branch ownership, rejects inactive accounts, and prevents duplicate posting.
- Posted journal lines cannot be edited or deleted through model methods; reversals are recorded as separate counter-entries. A database constraint rejects lines without exactly one positive debit or credit.
- Trial balance, general ledger, profit-and-loss and balance-sheet reports use posted journal lines, enforce organization/branch scoping, support date filters and CSV output, and include account-specific opening movements or current earnings where appropriate.
- Inventory product and stock bookkeeping support stock movements without duplicate postings.
- Stock posting consolidates repeated product lines into one document movement and re-reads stock under a database row lock.
- Purchase and sales invoices validate totals and reject invalid postings.
- Banking app supports cash/bank accounts, receipts, payments, transfers, statement import, invoice allocations, advances and explicit reconciliation.
- Statement imports validate CSV structure and row values atomically; imported lines remain unmatched until a user explicitly confirms a one-to-one exact account/date/signed-amount match.
- Dashboard derives quick metrics from actual records and banking navigation is integrated.
- Taxation now has configurable organization tax codes, effective-dated tax components, registrations, product classifications, centralized tax calculation, tax-aware sales/purchase posting, per-line snapshots, registers, summaries and CSV exports.
- Legacy draft invoices without product tax classifications continue using the existing stored-total validation path.
- Customer receipts and supplier payments support partial and split invoice allocations, unapplied advances and controlled audited reversals. Historical settlements are retained without assigning them to invoices by inference.
- Reconciliation offers optional match suggestions, still requires explicit exact-match confirmation, and records match/unmatch audit events.
- Financial years generate monthly periods; close checks known draft/unmatched-bank blockers, closed periods reject journal posting, and reopening requires a reason and is audited.
- Budgets compare posted journal activity with period/account/branch/department/cost-centre budget lines and support CSV export.
- Fixed assets support category setup, capitalization, straight-line/reducing-balance depreciation, branch transfer, disposal gain/loss, history and CSV register output. Depreciation posting is idempotent per asset and period.
- Year-end close has a read-only preview and explicit confirmation page with a blocker checklist, profit/loss and proposed opening-balance report, CSV export, and generated-journal/audit history.
- Closing journals transfer current-year net income/expense balances to a configured retained earnings account. Opening journals carry only balance-sheet balances into the immediately following financial year, split by branch and posted to its open first period.
- Year-end close is idempotent, atomic, organization-finance-only and immutable after finalization. Closed periods cannot be reopened after their financial year has been finalized.
- Trial balance, general ledger, profit-and-loss and balance-sheet screens support a financial-year filter. Opening journal snapshots are included in selected-year reports and excluded from cumulative totals to prevent duplicate balances.
- Finalized close records, their journal links and generated journals/lines, period records, and related audit events reject supported instance and bulk ORM mutations; closing uses a dedicated validation transition.
- Navigation now links Sales, Purchases and Inventory to functional invoice and stock screens rather than placeholder URLs.
- Sales and purchase screens now provide organization/branch-scoped invoice search and status filtering, draft creation with line items, detail views and POST-only invoice posting through existing tax, stock and accounting services.
- Purchasing now supports draft purchase-order creation and editing, confirmation/cancellation, partial goods receipts, warehouse-specific stock posting, and receipt-linked supplier invoices with received-quantity limits. Receipt and invoice posting reuse the existing stock, tax, accounting and payables services; linked invoices do not post duplicate stock movements.
- Sales orders support draft creation, confirmation/cancellation, partial dispatch through warehouse-selected delivery notes, and invoicing from dispatched quantities. Order quantity is checked against cumulative dispatches; linked invoice posting does not deduct stock a second time.
- Inventory screens provide product/warehouse creation, per-warehouse movement-ledger balances and low-stock filtering, reference-protected adjustments, and atomic warehouse transfers using paired signed movements. Adjustments, transfers, order confirmations/cancellations and deliveries create audit events.
- Perpetual weighted-average valuation is maintained by organization, product and warehouse through immutable movement-cost snapshots and valuation events. Explicit opening valuations handle legacy/unallocated stock; the valuation screen provides as-of positions, event history, CSV exports and inventory-control-account reconciliation. Transfers preserve value, outgoing movements use warehouse average cost, and positive adjustments require an explicit cost.
- Sales invoices and deliveries use movement valuation for COGS; receipt-linked supplier invoices adjust valued stock and the presumed consumed share in COGS when actual cost differs. Returns use the costing service, and adjustment/return variance journals use designated inventory gain/loss accounts.
- Customer returns can be created from posted sales invoices, with cumulative per-line quantity controls, item condition/reason, explicit restocking, source-snapshot credit notes, negative tax rows and balanced reversal journals. Non-restockable returns do not increase stock or reverse COGS.
- Supplier returns can be created from posted receipts for still-unbilled quantities or from posted supplier invoice lines. They validate prior returns, warehouse stock and branch scope, then create linked debit notes, purchase-tax reversals, balanced journals and one negative stock movement.
- Return lists, search/status filters, paginated screens, scoped create/edit/detail routes and permission-checked post/cancel actions are implemented. Customer/supplier balances and invoice open amounts account for posted notes. Accounting period checks reject posting in closed periods.

## Not implemented or partial

- Manufacturing, point of sale, CRM, HR/payroll, projects, service management and production order workflows are not implemented.
- Sales quotations, landed-cost allocation and purchase approval limits remain incomplete. Sales order drafts do not yet have an edit workflow.
- Legacy product-level aggregate stock is not automatically allocated to warehouses. FIFO, reservation/batch/serial tracking and formal stock-count reconciliation remain incomplete. Legacy positions stay unvalued until an authorized explicit opening value is recorded.
- Invoice detail links into the existing Banking transaction workflow for receipts/payments; automatic invoice-specific payment prefill is not implemented.
- Accounting, banking and tax screens provide selected real workflows; the application is not a complete role/permission matrix, statutory filing system, or production-ready Wings ERP equivalent.

## Defects fixed

- Repaired invoice subtotal and tax validation to prevent posting invalid totals.
- Added duplicate stock movement guards so repeated posting does not create duplicate ledger entries.
- Added transactional safeguards for bank payment/receipt posting to avoid duplicate posted entries.
- Added bank account and statement import idempotence checks for duplicate imports.
- Corrected default chart accounts so they remain organization-wide when reused by multiple branches; branch-specific accounts must match the journal's branch.
- Prevented reconciliation from marking a transaction complete without an explicit compatible statement-line match.
- Corrected report balances: trial balance is cumulative through its end date, ledger running balances are tracked per account and include opening movements, and the balance sheet includes current earnings in equity.
- Fixed host/test-client configuration for Django validation and login smoke checks.
- Restored the taxation URL imports by implementing the missing authenticated views and templates.
- Added atomic, duplicate-safe tax snapshot writes during invoice posting and tax calculation coverage for same-state/interstate supplies, inclusive pricing, discounts, exemptions, zero-rated items, UTGST, reverse charge, effective dates and rounding.
- Added invoice allocation, advance balance, reversal and reconciliation audit workflows with regression tests.
- Added financial-year/period controls, budget-versus-actual reporting, fixed-asset accounting services and screens.
- Restricted period management and shared asset-category creation to organization-level finance users; branch finance users retain scoped asset operations.
- Rounded recalculated customer/supplier outstanding balances to currency precision after full-suite testing exposed a Decimal scale validation error.
- Rounded sales and purchase line totals with the shared currency helper; adjustment retries now avoid duplicate audit events, and conflicting retries are rejected.
- Added the tested sales order → partial delivery/dispatch → invoice path with atomic stock posting, bounded quantities, tax-aware invoice posting, balanced accounting, and duplicate-safe conversions.
- Added warehouse transfers with paired ledger entries, source-balance validation, period checks and application-level protection for posted transfers and their movement rows.
- Added a narrowly scoped year-end posting path for closing entries after all periods are closed; ordinary posting remains blocked in closed periods.
- Added purchase-order and goods-receipt migrations, scoped workflow screens, partial receiving and receipt-linked supplier billing. Drafts can be edited before confirmation/posting; committed rows reject supported instance and bulk ORM changes.
- Added separate customer/supplier return records, credit/debit note models, signed tax snapshot adjustments, stock movement protections, payment-balance integration, user screens and return workflow coverage. Migrations `inventory.0004` and `returns.0001` are generated but not applied to the business database.
- Added warehouse-level perpetual weighted-average valuation with an append-only valuation event trail, six-decimal cost snapshots, explicit opening allocation/valuation, valued transfers, inventory gain/loss journals, supplier invoice price adjustments, CSV/as-of/reconciliation reporting and legacy completeness flags. Migrations `inventory.0005` and `inventory.0006` are generated but have not been applied to the business database.
- Prevented opening-balance snapshots from double-counting cumulative ledger reports or cached account balances; selected-year reports still include the opening journals.
- Blocked reopening periods or editing financial-year configuration after a year-end close has been finalized.

## Files touched

- `nexora_erp/settings.py`
- `nexora_erp/urls.py`
- `core/models.py`
- `core/views.py`
- `core/urls.py`
- `organizations/models.py`
- `accounting/models.py`
- `accounting/views.py`
- `accounting/urls.py`
- `accounting/tests.py`
- `accounting/migrations/0004_financialyear_accountingperiod_periodauditevent_and_more.py`
- `accounting/migrations/0005_fixedassetcategory_fixedasset_fixedassetdepreciation_and_more.py`
- `accounting/migrations/0006_financialyear_retained_earnings_account_and_more.py`
- `accounting/migrations/0007_alter_yearendclosingjournal_branch.py`
- `accounting/services.py`
- `accounting/forms.py`
- `accounting/migrations/0003_journalentryline_journal_line_one_positive_side.py`
- `templates/accounting/report.html`
- `templates/accounting/ledger.html`
- `inventory/models.py`
- `inventory/views.py`
- `inventory/urls.py`
- `purchases/models.py`
- `purchases/forms.py`
- `purchases/views.py`
- `purchases/urls.py`
- `purchases/admin.py`
- `purchases/migrations/0005_goodsreceipt_goodsreceiptline_and_more.py`
- `purchases/migrations/0006_purchaseorderline_po_discount_nonnegative_and_more.py`
- `returns/models.py`
- `returns/forms.py`
- `returns/services.py`
- `returns/views.py`
- `returns/urls.py`
- `returns/admin.py`
- `returns/migrations/0001_initial.py`
- `inventory/migrations/0004_alter_stockmovement_movement_type_and_more.py`
- `inventory/migrations/0005_alter_stockmovement_product_and_more.py`
- `inventory/migrations/0006_alter_inventoryvaluationentry_event_type.py`
- `templates/inventory/valuation.html`
- `templates/inventory/opening_valuation_form.html`
- `templates/returns/*.html`
- `templates/accounting/journal_entry_detail.html`
- `tests/test_returns.py`
- `tests/test_inventory_valuation.py`
- `docs/RETURNS.md`
- `docs/INVENTORY_COSTING.md`
- `docs/INVENTORY_COSTING_IMPLEMENTATION_REPORT.md`
- `sales/models.py`
- `dashboard/views.py`
- `dashboard/urls.py`
- `banking/models.py`
- `banking/forms.py`
- `banking/views.py`
- `banking/urls.py`
- `banking/migrations/0002_bankstatementline_matched_transaction_and_more.py`
- `banking/migrations/0003_banktransaction_settlement_mode_invoiceallocation.py`
- `banking/migrations/0004_bankreconciliationevent.py`
- `banking/services.py`
- `templates/base.html`
- `templates/dashboard/index.html`
- `templates/registration/login.html`
- `templates/banking/*.html`
- `templates/purchases/order_list.html`
- `templates/purchases/order_form.html`
- `templates/purchases/order_detail.html`
- `templates/purchases/receipt_form.html`
- `templates/purchases/receipt_detail.html`
- `templates/purchases/receipt_invoice_form.html`
- `templates/accounting/periods.html`
- `templates/accounting/budgets.html`
- `templates/accounting/fixed_assets.html`
- `templates/accounting/year_end_closing.html`
- `static/css/styles.css`
- `tests/test_vertical_slice.py`
- `tests/test_workflow_screens.py`
- `tests/test_banking.py`
- `taxation/models.py`
- `taxation/services.py`
- `taxation/forms.py`
- `taxation/views.py`
- `taxation/urls.py`
- `taxation/admin.py`
- `taxation/migrations/0001_initial.py`
- `taxation/migrations/0002_taxtransaction_cess_rate_taxtransaction_cgst_rate_and_more.py`
- `sales/migrations/0002_customer_state_code_salesinvoice_place_of_supply.py`
- `sales/migrations/0003_salesinvoiceline_discount_amount.py`
- `purchases/migrations/0002_purchaseinvoice_place_of_supply_supplier_state_code.py`
- `purchases/migrations/0003_purchaseinvoiceline_discount_amount.py`
- `purchases/migrations/0004_supplier_is_reverse_charge_applicable.py`
- `taxation/tests.py`
- `templates/taxation/*.html`
- `docs/BANKING.md`
- `docs/PURCHASING.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/MASTER_IMPLEMENTATION_PLAN.md`
- `docs/GST_RULES.md`
- `docs/DATABASE_DESIGN.md`
- `docs/FINANCIAL_CONTROLS.md`
- `README.md`

## GST/taxation limitations

- GST is an internal calculation and reporting feature, not legal advice, government certification, return filing or an official integration.
- Return tax transactions reverse original tax snapshots internally; the application does not create statutory filing packages, provide legal eligibility decisions, or integrate with government services.
- Tax component rates and classifications must be entered and reviewed by the organization; the application does not determine statutory rates or eligibility.
- The current screens provide create/update setup flows for key configuration and paginated tax registers, but do not yet provide a full tax configuration audit/versioning workflow.

## Accounting and banking limitations

- Historical legacy receipts/payments are not backfilled to invoices, and the legacy `CustomerReceipt` workflow remains separate from bank-transaction allocation.
- Year-end close cannot be reopened or automatically reversed; corrections require an approved journal in an open period.
- SQLite does not provide PostgreSQL-equivalent row locks. Unique constraints protect against duplicate close records, but production concurrency behavior must be tested against the deployment backend.
- Opening-balance accounting is handled as an annual snapshot. Reports without a selected financial year intentionally exclude opening snapshots from cumulative totals.
- Budgets have no approval workflow, thresholds, commitments or purchase-order encumbrances.
- Fixed-asset capitalization currently debits the default cash ledger account; supplier-invoice capitalization is not integrated. Statutory/tax depreciation equivalence is not claimed.
- Ledger rows are currently built before pagination; a database-streamed approach is needed for very large organizations.
- Reconciliation suggestions are advisory; confirmation remains explicit. Bank feeds and bank-specific formats are not implemented.
- Authentication does not yet provide a full role/permission matrix for every report and financial action.
- Row-locking behavior has not been validated with production PostgreSQL concurrency or large-volume workloads; SQLite is the default local database.

## Inventory costing limitations

- Goods receipts do not create GRNI accrual journals. The valuation report lists uninvoiced receipts, receipt-only returns and deliveries awaiting invoices as reconciliation items.
- Receipt-linked invoice price differences are allocated using current on-hand quantity because the ledger does not retain FIFO receipt layers. Mixed-receipt cases need finance review.
- Opening valuation records the approved value and allocation without creating a journal. Opening-balance accounting must be reconciled and posted separately where needed.
- As-of history follows system posting timestamps; backdated document dates do not reorder weighted-average costing. Pre-valuation historical values remain incomplete.
- Valuation and accounting integration has been verified on SQLite. PostgreSQL row-lock concurrency and high-volume report behavior have not been verified.

## Verification results

The previously referenced `INVENTORY_COSTING_IMPLEMENTATION_REPORT.md` is absent from this workspace (as recorded in `PHASE_1_REPORT.md`). Current Phase 4 inventory-costing findings are recorded in [PHASE_4_IMPLEMENTATION_REPORT.md](PHASE_4_IMPLEMENTATION_REPORT.md). Earlier dated results below document the preceding returns implementation.

- 2026-10-22 `manage.py check` - passed; no issues.
- 2026-10-22 `manage.py makemigrations --check --dry-run` - passed; no missing model migrations.
- 2026-10-22 `manage.py test` - passed; 88 tests, including customer and supplier returns.
- 2026-10-22 `python -m pytest tests/ -q` - passed; 42 tests.
- Focused purchasing screen and lifecycle run - passed; 15 tests.
- The repository `.venv\Scripts\python.exe` launcher points to a Python installation that is missing from this machine. The current verification report records the runtime used and results.
- Test-runner migrations apply only to a temporary test database. No migration has been applied to `db.sqlite3` or another business database; inventory migrations through `inventory.0006` and returns migration `returns.0001` still require reviewed deployment.
- PostgreSQL row-lock and concurrency behavior has not been tested; the test database uses SQLite.

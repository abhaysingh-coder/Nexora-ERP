# Nexora ERP Master Implementation Plan

This checklist reflects verified project functionality, not feature parity claims. Update items only after implementation and tests confirm the behavior.

## Baseline (2026-10-19)

- [x] Existing project and applications inspected; project files are present as untracked files in this workspace.
- [x] `manage.py check` passed before this work.
- [x] `manage.py test` passed before this work (24 tests).
- [x] `makemigrations --check --dry-run` passed before this work.
- [x] `python -m pytest tests/ -q` passed before this work (6 tests).
- [x] Local data database was not reset or migrated as part of the baseline.

## Platform and security

- [ ] Organization/branch access is consistently enforced in every existing banking, accounting, sales, purchase and admin workflow.
- [ ] Role/permission matrix covers view, create, approve, post, reverse and export.
- [ ] Company switching, invitations, password recovery, throttling and optional 2FA.
- [ ] Audit history covers material financial actions and configuration edits.
- [ ] Production security settings, backups and deployment guidance.

## Accounting and financial control

- [x] Journal posting validates non-empty, balanced, same-organization and branch-consistent lines; serializes repeated posting and locks affected accounts.
- [x] Posted journal lines are immutable through model methods; reversals create separate posted counter-entries.
- [x] Trial balance, general ledger, profit/loss and balance sheet use posted ledger lines and provide CSV exports.
- [x] Financial years, period closing/locking, budgets and fixed-asset accounting.
- [x] Audited year-end close with income/expense transfer to retained earnings and next-year opening-balance journals.
- [ ] Cash-flow statement.
- [x] Core accounting report totals derive from posted journal records; trial balance is cumulative through its end date and balance sheet includes accumulated earnings.

## Inventory and purchasing/sales

- [x] Purchase and sales invoice posting integrates stock and journals with atomic rollback and duplicate protection.
- [x] Authenticated, organization/branch-scoped sales and purchase invoice list, draft creation, detail and posting screens use the existing transaction services.
- [x] Inventory product/warehouse setup, stock-movement history, low-stock filtering and reference-protected manual adjustments.
- [x] Repeated product lines are aggregated into stock movement per document.
- [x] Per-warehouse perpetual weighted-average valuation, explicit legacy opening valuation, valued transfers and inventory-to-GL reconciliation.
- [ ] Reservations, batch/serial tracking, formal stock counts and FIFO costing.
- [x] Purchase-order drafting/confirmation, partial warehouse receipts, receipt-linked partial billing and posting safeguards.
- [ ] Purchase requisitions, landed-cost allocation and purchase approval limits.
- [ ] CRM, quotations, orders, delivery and advanced credit management.
- [x] Sales returns, credit/debit notes and controlled stock/account adjustments.

## Banking and cash

- [x] Basic bank/cash account postings, transfers, statement import and reconciliation screens.
- [x] Banking views filter data by organization and assigned branch; statement parsing validates required headers and row amounts/dates atomically.
- [x] Reconciliation requires explicit statement-line/transaction match and confirmation; imported lines remain unmatched and are not auto-reconciled.
- [x] Customer receipt and supplier payment allocations, unapplied advances and controlled reversals.
- [x] Reconciliation suggestions and audited match/unmatch actions.
- [ ] Cheque clearing, statement fees, payment approval limits and complete role matrix.

## GST and taxation

- [x] Organization-scoped codes, dated component rates, registrations, product classifications and line snapshots.
- [x] Tax-aware sales/purchase posting and internal registers/summaries/CSV export.
- [ ] Credit/debit notes, returns, statutory reconciliation, return-data exports and complete tax configuration audit/versioning.

## Other business modules

- [ ] Expenses and employee reimbursements.
- [ ] HR, attendance, leave and payroll with jurisdiction-reviewed rules.
- [ ] Manufacturing, BOM, work orders, WIP, quality and traceability.
- [ ] POS, projects and customer service.
- [ ] Approval workflow, notifications and document generation.
- [ ] API/versioning and documented integration boundaries.

## Reports, UX and operations

- [x] Remove dead placeholder navigation links; label incomplete Sales, Purchases and Inventory areas as unavailable.
- [ ] Confirm key workflows through the browser UI.
- [ ] Global search, saved filters, accessible responsive components and export controls.
- [x] Core dashboard figures and financial reports are database-driven; financial reports use posted ledger lines.
- [ ] Deployment operations, static files, initial admin/organization setup, backup/restore and health check.
- [ ] Full regression, security/deployment checks and migration review after each milestone.

## Execution note

This is a substantial ERP roadmap. Work should proceed by delivering complete, tested vertical workflows; unchecked work is explicitly incomplete. Do not claim comprehensive Wings ERP parity, statutory compliance or production readiness without separate verification.

## Purchasing workflow update (2026-10-22)

- Purchase orders support scoped draft create/edit, confirmation and cancellation.
- Goods receipts support draft create/edit, partial quantities and warehouse selection; posting updates stock atomically and is retry-safe.
- Supplier invoices can be drafted from posted receipt lines, with posting limited to quantities received and not yet invoiced. Linked invoice posting skips a second stock movement.
- Supplier returns, debit notes, landed costs and purchase approval workflows remain unchecked.
- Focused purchasing regressions passed during this update; see `IMPLEMENTATION_STATUS.md` for final full-suite results.

## Current accounting and banking caveats

- Historical legacy receipts/payments are not assigned to invoices; new advance-mode bank transactions can be allocated. The legacy `CustomerReceipt` model remains separate.
- Financial years support controlled close and opening-balance carry-forward. The close is irreversible through the app; automated recovery or reversing the close is not implemented.
- Opening journals are year-scoped snapshots and excluded from cumulative balances to avoid lifetime double-counting.
- Fixed-asset capitalization currently posts against the default cash account; supplier-invoice acquisition is not integrated.
- Budget approvals, thresholds and commitments are not implemented.
- Reconciliation suggestions are advisory; explicit confirmation still requires an exact match.
- General-ledger rows are assembled before pagination and should be optimized for large datasets.
- No production-database concurrency or high-volume performance validation has been run. SQLite does not provide PostgreSQL-equivalent row-lock guarantees.
- No `manage.py migrate` command was run during the current implementation work; verify migration state and take a database backup before applying migrations to a business database.

## Phase 4 update (2026-10-24)

- Work Unit A added active-membership organization authority, a validated multi-organization selector, protected module-route context, and superuser-only provisioning. No membership was auto-granted. See `PHASE_4_IMPLEMENTATION_REPORT.md` for implementation and permission verification limits.
- Work Unit B introduced a shared active-membership role predicate while preserving the existing role sets in each module; organization/tax focused tests passed after the refactor. A unified capability and maker-checker model remains unresolved and was not invented.
- Earlier pre-fix Django and pytest runs reproduced the GRNI/reconciliation discrepancy. The owner explicitly confirmed the Phase 3 no-GRNI policy. Receipt posting was corrected to omit the interim journal and accrual rows. The latest validation in `PHASE_4_IMPLEMENTATION_REPORT.md` is 128 Django tests passed and 63 pytest tests plus 18 subtests passed. The authorization audit also fixed inactive-user role authorization and added direct tenant/branch request probes; operation coverage remains partial.
- The generated purchases `0007` accrual migration was unapplied and has been removed along with the unused model definitions. No business database migration was applied. Local verification used SQLite and does not establish PostgreSQL concurrency safety.
- Work Units B–I and end-to-end Phase 4 are not complete. Permission audit gaps and other unresolved business decisions remain open.

## Phase 4 authorization milestone update (2026-10-31)

- [x] Re-enumerated the live route surface: 72 non-admin application patterns and 24 registered admin models. The route manifest records names, paths, supported methods, scope/operation summaries, and the precise common request-probe status.
- [x] Latest validation: `manage.py check` passed; `makemigrations --check --dry-run` reported no changes; Django suite passed 140 tests; pytest passed 70 tests and 366 subtests.
- [x] Corrected the report/export inventory: 11 route patterns provide CSV support and 12 distinct CSV output modes, including both inventory valuation exports.
- [x] Reconciled the stale user-admin audit note with current code and tests for user creation, update, and password-change events.
- [ ] Complete per-route role, active-user, tenant, branch, object ownership, supported POST, filter-tampering, and export authorization tests. Common generated GET and unsupported-method probes do not satisfy this item.
- [ ] Obtain an explicit owner-approved matrix for each report/export category, role, organization/branch scope, and action. The response referenced a revised matrix, but no matrix content was supplied; no unresolved rights were implemented.
- [ ] Verify all dashboard/report/export aggregates, state-changing operations, all registered admin routes/actions, and exactly-once audit coverage with at least two organizations and multiple branches.
- [ ] Close critical cross-tenant/branch isolation gaps and complete the remaining route/admin review before declaring authorization complete.

### Phase 4 export-isolation continuation (2026-10-31)

- [x] Add CSV-content isolation tests for tax register and banking receivables/payables, including organization/branch parameter tampering, foreign party IDs and date filtering; focused taxation/banking tests passed (34 tests).
- [x] Verify latest system/migration checks and full suites: Django 140 tests passed; pytest 70 tests and 366 subtests passed.
- [ ] Complete direct content-level isolation evidence for all 12 CSV output modes (currently 6 modes have selected content-level regressions) and expand filter/date/query coverage.
- [ ] Test cross-tenant/branch inputs for dashboard KPIs beyond sales totals; classification and branch-null policy remain owner-dependent.
- [ ] Continue route-specific authorization, supported POST, admin action/audit, and service-boundary verification. Existing smoke probes are not full authorization evidence.
- [ ] Keep report/export role matrix **DEFERRED / NOT APPROVED**; do not broaden access pending an explicit policy.

**Current stop status:** Owner deferred the report/export matrix approval. Phase 4 is **IN PROGRESS**; authorization is not complete. The test runner used an isolated database and no business database migration was run. Phase 5 has **not started**.

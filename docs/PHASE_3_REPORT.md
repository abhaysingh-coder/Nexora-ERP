# Nexora ERP — Phase 3 Business Logic Design Report

**Status: COMPLETE — specification only.** Phase 3 business rules and workflow algorithms are documented. This completion status means the requested specification package is reviewable and traceable to Phases 1/2 and inspected source. It is **not** business-policy approval, legal/accounting advice, implementation, or a claim that tests passed. No Phase 4 implementation was started. Wait for explicit authorization before Phase 4.

## Scope and preservation

This phase read the six Phase 1 documents and six Phase 2 documents, the Phase 2 decision register/report, relevant existing app models, posting/services, and existing control notes. It produced seven documents only:

1. `BUSINESS_RULES.md`
2. `ALGORITHM_SPECIFICATIONS.md`
3. `WORKFLOW_STATE_MACHINES.md`
4. `ACCOUNTING_POSTING_RULES.md`
5. `INVENTORY_RULES.md`
6. `ERROR_HANDLING_AND_TRANSACTIONS.md`
7. `PHASE_3_REPORT.md`

No Python application code, models, migrations, settings, templates, tests, or database contents were changed. The Phase 2 decision register remains the source list; this report classifies each unresolved item as approved by evidence, provisional, or blocked and documents the consequences.

## Decision classification

“Approved by evidence” here means a documented Phase 1/2 contract or observable current implementation behavior. It does not imply a business owner has approved future scope. Any change to that behavior still requires owner approval.

| Decision | Classification | Recorded evidence / condition |
|---|---|---|
| Weighted-average valuation by org/product/warehouse; decimal rounding; reject negative stock | Approved by evidence | `INVENTORY_COSTING.md`, inventory costing service, Phase 2 design. |
| Opening stock value and unknown historical cost | Provisional | Use only supported quantity/value evidence; unknown remains unvalued; opening action does not journal by current behavior. Finance + Inventory must approve actual opening evidence and any GL adjustment. |
| Negative stock | Approved by documented current behavior | Costing service rejects negative stock. Exceptions remain unapproved. |
| Backdating and reservation semantics | Blocked | Valuation order uses posting/event sequence; no reservation ledger. Operations + Finance approval required before changing availability/order semantics. |
| GRNI accounting | Blocked | Current goods receipt affects stock valuation without interim GRNI journal. Finance + Purchasing must approve recognition, amount, accounts, reversal, returns and aging. |
| Receipt/invoice matching | Current checks approved by evidence; price/quantity tolerance policy blocked | Posted receipt quantities cap posted invoice quantities; linked invoice does not add stock again. Tolerances and exception approval are undecided. |
| Document numbering, scope and resets | Blocked | Global and org-scoped constraints conflict/mix. Finance + Operations approval and collision scan required. |
| Currency and FX | Blocked | Current organization has currency string and single-currency behavior; no FX ledger. Finance must choose INR-only or approve full FX policy. |
| Tax snapshots and historical reversals | Approved by documented current behavior | `GST_RULES.md` and `RETURNS.md` preserve source snapshots; posted transactions are not recalculated. |
| Tax law, registrations, statutory returns/provider integration | Blocked | Internal reports only; no government integration. Tax + Finance + legal/provider review required. |
| Retention and deletion | Provisional preservation rule; statutory duration blocked | Preserve posted/audit evidence via reversal; Legal + Security set retention/privacy/hold policy. |
| Roles, branch permissions, maker-checker | Blocked | Current route guards vary; User and Membership scope overlap; target role matrix is proposed only. Product + Security + Finance must approve. |
| Interbranch accounting | Blocked | Branches and warehouse transfers exist, but entity boundary, clearing accounts and settlement policy do not. Finance approval required. |
| Fixed asset policy | Current mechanics approved by evidence; accounting policy blocked | Straight-line/reducing balance and duplicate asset-period protection exist; capitalization assumes cash ledger. Finance must approve accounts, schedule, tax/book treatment, disposal and periods. |
| API, bank formats and integrations | Blocked | DRF installed but no exposed API routes; current file import is not a bank feed. Product + Security + Finance scope/auth decisions required. |
| Production database/concurrency | Blocked technical decision | SQLite configured; PostgreSQL dependency is not deployment evidence. Architecture + Operations choose engine and validate locks/isolation. |
| Candidate module scope | Blocked per module | Phase 1 marks POS, distribution/dealership, manufacturing, HR/payroll, CRM, projects and related domains missing/unknown. Product and named business owners must decide scope. |

## Workflow coverage

The specifications cover these workflows and identify for each the preconditions, permissions/scope, validations, calculations, reads/writes, atomic transaction boundary, state transition, ledger effects, idempotency, failure behavior, rollback/correction, audit evidence and proposed acceptance tests:

- Inventory receipts/issues, average cost, opening stock and valuation, transfers, adjustments, returns, legacy unvalued stock, and valuation-to-GL reconciliation.
- Purchase order confirmation, partial goods receipts, the current no-GRNI behavior, supplier invoice direct and receipt-linked posting, quantity matching and price variance limitations.
- Sales order confirmation, partial dispatch, delivery-linked versus direct invoicing, revenue/COGS/tax/stock posting.
- Customer and supplier returns, credit/debit notes, tax snapshot reversal and stock cost handling.
- Double-entry journals, reversals, open-period enforcement, period close, year-end close/opening snapshot and fixed asset lifecycle.
- Bank transactions, partial/split invoice allocations, advance balances, allocation reversals, CSV imports and explicit statement reconciliation.
- GST calculation snapshots and internal reporting boundaries; reporting scope, caches, reconciliation exceptions, and known dashboard KPI ambiguities.
- Common authorization, tenant validation, concurrency, idempotency, error taxonomy, rollback and audit requirements.

The specifications explicitly identify places where current behavior is only a compatibility fact, where the Phase 1 status is partial, and where policy approval is required. Return status vocabulary, low-stock/available-cash KPI meaning, maker-checker thresholds and several accounting treatments are not silently normalized into assumed policy.

## Repository evidence consulted

- Phase 1: feature matrix, UI/UX spec, screen inventory, role/screen matrix, navigation map, completion report.
- Phase 2: database design, ER diagram, data dictionary, constraints/decision register, migration strategy, completion report.
- Existing technical notes: `INVENTORY_COSTING.md`, `PURCHASING.md`, `RETURNS.md`, `BANKING.md`, `GST_RULES.md`, `FINANCIAL_CONTROLS.md`, `IMPLEMENTATION_STATUS.md`, and return implementation report.
- Source points reviewed: costing and opening functions in `inventory/models.py`/`inventory/valuation.py`; `SalesOrder.confirm`, `DeliveryNote.post`, `SalesInvoice.post`; `PurchaseOrder.confirm`, `GoodsReceipt.post`, `PurchaseInvoice.post`; return posting services; banking allocation/reversal services; tax calculation/persistence services; journal/period/year-end and fixed asset functions; dashboard and banking overview views/templates.
- Current state observations include: stock valuation by product/warehouse; direct sales invoice stock issue versus delivery-linked no-double-issue; direct supplier invoice stock receipt versus receipt-linked no-double-receipt; source tax snapshots; allocation reclassification journals; exact statement reconciliation after suggestions; period/year-end special handling; and current SQLite configuration.

## Consistency and validation performed

- The seven requested Phase 3 files were created, with shared terminology for current/evidenced, provisional, blocked, idempotent posting, immutable posted records, and source-ledger reconciliation.
- Cross-checked feature references against Phase 1 IDs and decision classes against the Phase 2 register. Existing return workflows are identified as `SAL-07`/`PUR-07`; banking `BNK`; tax `GST`. `BUSINESS_RULES.md` provides an explicit feature traceability map, including workflows that remain missing or blocked.
- Static existence and relative Markdown-link checks on all seven new documents found no missing deliverables and no broken local links. A representative feature-reference scan initially found absent references; the feature traceability map was added and the scan was rerun to verify its coverage.
- Git status was inspected in the workspace; project files are untracked and must be preserved. No staging, reset, or removal was done. `git diff --stat` and staged diff are empty because the workspace files are untracked; this is not evidence that the new untracked documents are unchanged.
- Ran `\.venv\Scripts\python.exe manage.py check`; it could not start Django because `.venv` points to a missing Python 3.12 installation. Earlier environment inspection reported that `py -0p` found no installed interpreters.

## Checks not performed / blocked

No Django system checks, test suite, migration check, applied migration inspection, live database inspection, or concurrency test was possible with the missing interpreter. No test listed in the algorithm specifications is claimed to have run. PostgreSQL concurrency and backend-specific constraint support remain unverified. No migrations, database changes, external integrations or business logic changes were performed.

## Outstanding decisions and impacts

Before implementing affected rules, the responsible parties must resolve:

1. Inventory opening evidence, approval, zero/unknown value, backdating ordering, stock reservation and negative-stock exceptions (Finance + Inventory + Operations): affects available quantities, valuation history, and COGS.
2. GRNI recognition/timing/accounts, invoice variances, receipt-only returns, landed cost and tolerance (Finance + Purchasing): affects inventory, AP, accruals, COGS and period-close reconciliation.
3. Numbering scope, legal entity/branch/year boundaries, reset and gaps (Finance + Operations): affects legal documents, uniqueness, collision migration and concurrency.
4. INR-only or multicurrency, rate source/date, precision and FX gain/loss (Finance): affects all monetary fields, journals, valuation and reports.
5. GST rates, place-of-supply/registration, ITC/reverse-charge policy, statutory exports/filing and provider security (Tax + Finance + legal): affects tax snapshots, reports, invoice obligations and integration scope.
6. Retention, privacy, deletion/anonymization, legal holds and attachment security (Legal + Security): affects audit and document lifecycle.
7. User/Membership authority, role capabilities, branch access, exports and maker-checker limits (Product + Security + Finance): affects every read/write workflow and audit attribution.
8. Interbranch legal ownership, transfer valuation and due-to/due-from accounting (Finance): affects whether a warehouse transfer is inventory-only or an accounting transaction.
9. Fixed-asset acquisition account/source, depreciation method/schedule/book-vs-tax and disposal policy (Finance): affects capitalization and period journals.
10. Supported APIs, external bank formats/feed, idempotency/authentication and integration operations (Product + Security + Finance): affects security, reconciliation and external side effects.
11. Production database, isolation/locking guarantees, backup/recovery and concurrency target (Architecture + Operations): blocks assurance of duplicate-safe concurrent posting.
12. Scope by module for POS, CRM, distribution/dealership, manufacturing, HR/payroll, projects and services (Product + business owners): blocks reliable workflow and data design for those areas.

## Completion gate

**Phase 3 was complete as a business logic specification package at the time of this report.** Policy-dependent sections were explicitly blocked and must not be implemented as if approved. At that time Phase 4 had not started and required separate authorization.

## Post-Phase 3 owner decision (2026-10-24)

Phase 4 was subsequently authorized. The owner explicitly selected “Keep Phase 3 no-GRNI rule.” This later decision resolves the GRNI recognition question in item 2 above: goods receipts must not post interim GRNI. The related receipt accrual models and unapplied migration were removed. The remaining item 2 decisions (invoice price variance/tolerances, receipt-only returns, landed cost and period treatment) remain unresolved. This addendum records a later decision; it does not change the historical Phase 3 completion assessment or authorize other Phase 4 work.

# Phase 4 Implementation Report

Date: 2026-10-24
Overall status: **IN PROGRESS — Authorization and tenant-isolation audit remains partial; confirmed inactive-user role guard defect corrected; no-GRNI policy discrepancy corrected per explicit owner decision; Phase 4 not complete.**

## Scope completed in this work period

### Work Unit A — Organization context and access foundation

The prior Phase 4 clarification approved active `OrganizationMembership` as the authority for non-superuser organization access, an organization selector for users with multiple active memberships, and superuser-only provisioning. The following implementation is present:

- Added centralized organization resolution in `core/organization_context.py`. Non-superusers receive only active organizations through active memberships. A user's direct `User.organization` value does not grant business access. An assigned branch must be active and belong to the selected organization. Superusers can select active organizations but still use explicit organization context for business views.
- Added organization selection/access routes, scoped form, templates, middleware protection for business module routes, and a navigation selector. Selection is stored in the authenticated session and validated against current access on each request.
- Added superuser-only organization/branch/membership administration. Membership changes require a reason and create audit events; hard deletion is disabled for protected setup records. Custom user administration is registered, direct organization assignment is read-only, and branch assignment is checked against active membership.
- Replaced duplicated view-level organization resolution in the reviewed business views with the shared resolver and removed the taxation `is_staff` permission bypass.
- Added `core/permissions.py` as a shared predicate for active membership role checks. Sales, purchases, returns, inventory, accounting, banking and taxation call it while retaining their prior module-specific role sets and superuser bypass. It does not grant new capabilities or define branch policy.
- Added shared-role regression tests for matching and wrong-organization roles, inactive membership, anonymous users, and preserved superuser behavior. Focused `organizations` and `taxation` suites passed 32 tests after the refactor.
- Enumerated the live Django URL resolver and searched for DRF views/routers and background-task registrations. No first-party API endpoint or background worker/scheduled task was found; `rest_framework` is installed but not mounted as an API.
- The route/admin audit found default Django admin registrations for purchasing and returns, editable tax configuration models, and a standard `auth.Group` administration route. These paths bypassed the business screens' tenant/role checks or audit workflow. They are now inspection-only and active-superuser-only for purchasing, returns and taxation; `Group` admin is unregistered because groups are not the application's membership-role authority. Adds/changes/deletes are disabled through these admin registrations. Three focused admin regression tests pass; broader verification is recorded below.
- Banking account creation, transaction posting, CSV statement import and statement reconciliation confirmation lacked role/action checks. The owner approved active membership roles `owner`, `admin`, `accountant`, and `finance_manager` for those four operations. `banking/permissions.py` defines that exact set and the routes enforce it before processing. Non-core members can still view reconciliation data; match/undo forms are hidden and POST requests are denied. Allocation retains its existing broader role set, and undo retains its narrower set. Focused tests verify denied roles plus the authorized create, post, import, and reconcile path.
- Return posting now retries only SQLite `OperationalError` lock contention, and only after the decorated atomic attempt has exited and rolled back. The retry is bounded to five attempts with exponential delay; non-SQLite databases and all other operational errors are re-raised unchanged. This resolves the observed concurrent customer-return test failure on SQLite while preserving idempotency checks in the transactional service. It is not evidence of PostgreSQL concurrency safety.
- The authorization audit found that `core.permissions.has_organization_role()` did not check `user.is_active` before a matching active membership (and checked the superuser bypass first). Normal Django login blocks inactive accounts, but an indirect caller of the shared guard could pass an inactive user object. The predicate now rejects unauthenticated/inactive users before superuser bypass. Two regression tests cover inactive ordinary membership and inactive superuser denial.
- A resolver-backed operation inventory is recorded in `PHASE_4_AUTHORIZATION_INVENTORY.md`: 72 non-admin application URL patterns, 155 generated admin URL patterns across 24 registered models, 0 mounted DRF routes, 0 management commands, 0 background-task registrations, 0 model attachment fields, one CSV upload route, and 11 CSV-capable route patterns / 12 output modes. These are enumeration totals, not a completion claim; representative direct request tests are added, while the complete route/action/role/branch matrix remains unfinished.
- Added direct request isolation tests using two organizations and multiple branches. Foreign organization/branch purchase-order IDs return 404 for GET and POST, a forged branch query parameter does not widen the list, a roleless membership cannot confirm an in-scope order, tampering the active-organization session falls back to an authorized membership, GET leaves the order draft, and organization selector POST is denied without a CSRF token.
- The existing non-superuser without an active membership remains unprovisioned. No membership or business role was automatically granted. A superuser must make that explicit assignment.
- The root route now redirects to the single dashboard route to avoid divergent dashboard behavior.

### Purchasing correction — no interim GRNI

The user explicitly confirmed “Keep Phase 3 no-GRNI rule.” `GoodsReceipt.post()` continues to validate and post the receipt's stock movement and weighted-average valuation, then updates receipt/order states and audit history, without posting an Inventory/GRNI journal or recording a receipt accrual row. The incomplete-valuation guard remains, with its message corrected from “accrue” to “post.” The valuation report continues to include unbilled receipt value in its inventory-to-GL reconciliation, matching the approved no-GRNI treatment.

The unused `GoodsReceiptAccrual` and `GoodsReceiptAccrualEvent` models were removed. Migration `purchases/migrations/0007_goodsreceiptaccrual_goodsreceiptaccrualevent_and_more.py` had been generated during the earlier audit but was confirmed unapplied (`showmigrations purchases` showed `[ ] 0007`); it was removed rather than creating an unapproved schema. No database migration was applied. A regression assertion now requires receipt posting not to create a `GRN-...` journal.

## Repository and environment evidence

- The checked-in environment launcher `.venv\Scripts\python.exe` cannot run because its `pyvenv.cfg` references a missing Python 3.12 base installation. The virtual environment was not deleted or recreated.
- A compatible Python 3.12.14 runtime was available at `C:\Users\meabh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`. Django and declared dependencies were available in the existing `.venv\Lib\site-packages` and were used through `PYTHONPATH`; no dependency installation or business database change was performed.
- Configured database backend: SQLite. The test runner created and destroyed its Django test database; no business database migration/reset was run. SQLite results do not verify PostgreSQL locking/concurrency behavior.
- Git reports the project and docs as untracked (`??`), so normal tracked-file diff output is empty and cannot distinguish earlier workspace work from this work period. No files were staged or reset. The only removal was our previously generated, unapplied GRNI migration, removed after the owner selected no-GRNI; no pre-existing user file was removed. This report and the implementation changes remain in the shared untracked workspace.

## GRNI discrepancy, owner decision, and correction

The pre-fix audit established the following evidence:

1. Phase 3 `BUSINESS_RULES.md` and `PHASE_3_REPORT.md`, along with `PURCHASING.md` and `INVENTORY_COSTING.md`, specify that receipts do **not** post interim GRNI until Finance and Purchasing approve recognition timing, amount basis, accounts, settlement/reversal and returns treatment.
2. Before the correction, `GoodsReceipt.post()` created a journal debiting the inventory control account and crediting the goods-received-not-invoiced account, posted it, then created a `GoodsReceiptAccrual` row per receipt line.
3. `PurchaseInvoice.post()` validates receipt-linked quantities, applies invoice price differences to inventory/COGS, and posts inventory, tax and payable lines. Inspection found no corresponding clearing/reclassification of the receipt accrual rows or GRNI journal.
4. `inventory/views.py` computes `unbilled_receipt_value` from posted receipt quantity less posted linked invoice quantity and adds it to the raw inventory GL balance (`adjusted_gl = gl_inventory + unbilled_receipt_value - ...`). That treatment assumes the receipt value is not already in the GL.
5. Before correction, full regression test `tests.test_inventory_valuation.InventoryValuationTests.test_receipt_is_valued_once_and_later_invoice_posts_purchase_price_variance` failed with `Decimal('-100.00') != Decimal('0.00')`; diagnostic values showed inventory valuation `50.00`, inventory GL `50.00`, unbilled receipt `100.00`, and difference `-100.00` before the later invoice. The receipt journal conflicted with the report's unbilled-receipt adjustment.

The owner chose the documented Phase 3 policy: no interim GRNI. Receipt journal/accrual creation and the unapplied accrual migration were removed; the test now asserts no GRNI journal is created and reconciliation retains its no-GRNI adjustment. The existing supplier-invoice workflow still posts purchase accounting when invoiced. Broader invoice variance, receipt-only return, landed-cost and tolerance decisions listed in Phase 3 remain unresolved.

## Validation performed

| Check | Actual result |
|---|---|
| Django system check | `manage.py check` passed; no issues reported. |
| Migration state | After removing the unapplied GRNI migration/models, `manage.py makemigrations --check --dry-run` reported no changes detected. `showmigrations purchases` lists applied migrations through `0006`; no GRNI migration is present. |
| Focused organization/tax permission tests | 32 tests passed after the shared membership-role refactor, including membership selection/provisioning guards, module policy, and finance-role audit behavior. |
| Focused admin hardening tests | 3 passed: non-superuser staff denied purchasing/returns/tax admin routes; business admin records are superuser-only and read-only; auth Group admin is absent. |
| Focused banking authorization tests | 3 passed: sales and `finance` alias membership denied the four operations; accountant role allowed; integration test created an account, posted a receipt, imported a statement and reconciled the exact match. |
| Pre-fix full Django test suite | 110 tests ran; 109 passed and 1 failed on the inventory valuation reconciliation/GRNI conflict documented above. |
| Previous full Django test suite | `manage.py test`: 119 tests ran; all 119 passed before the authorization-inventory regression tests and inactive-user guard fix. |
| Earlier full Django test suite (2026-10-24) | `manage.py test`: 128 tests ran; all 128 passed. This is historical evidence; current run is recorded below. |
| Live organization data inspection | Read-only ORM inspection found 1 active organization, 2 active users, 0 membership rows, and one active non-superuser with no organization FK or membership. No membership was created automatically. |
| Database mutation | No migration or reset was run against the business database. Tests used the isolated Django test database. |
| Pre-fix pytest suite | 53 tests ran; 52 passed and 1 failed on the same inventory valuation reconciliation conflict. |
| Previous full pytest suite | `python -m pytest tests/ -q`: 56 passed and 16 subtests passed before the latest authorization-inventory regression tests and inactive-user guard fix. |
| Previous full pytest suite (earlier authorization snapshot) | `python -m pytest tests/ -q`: 63 passed and 18 subtests passed. |
| Current focused authorization tests | `manage.py test tests.test_authorization_isolation organizations.tests.SharedOrganizationRoleTests`: 12 tests passed, including tenant/branch identifiers, list filtering, submitted foreign form IDs, roleless mutation, session tampering, CSRF, inactive user and inactive superuser checks. |
| Concurrent return retry check | Focused `ConcurrentReturnRetryTests.test_concurrent_customer_return_retries_do_not_duplicate_note_journal_or_stock`: 1 passed after the SQLite-only bounded retry. |
| Current system/migration checks | `manage.py check`: no issues. `manage.py makemigrations --check --dry-run`: no changes detected. |
| Latest focused tax and banking export isolation tests | `python manage.py test taxation.tests tests.test_banking`: **34 tests passed**, including `TaxationTestCase.test_tax_csv_keeps_branch_and_tenant_scope_under_filter_tampering` and `BankingModuleTests.test_receivables_and_payables_csv_scope_survives_tenant_branch_and_filter_tampering`. |
| Latest full Django suite | `python manage.py test`: **140 tests passed**. The runner created and destroyed an isolated test database. |
| Latest full pytest suite | `python -m pytest -q`: **70 tests and 366 subtests passed**. |
| Syntax validation | Python `compile()` check over all 143 repository `.py` files: no syntax errors; no bytecode written. |
| Whitespace validation | Custom trailing-whitespace scan across repository Python, Markdown, HTML, CSS, JS and config sources found no trailing whitespace in files changed for this audit. Five findings remain in unrelated `ALGORITHM_SPECIFICATIONS.md`, `DATABASE_DESIGN.md`, `PHASE_1_REPORT.md` and `PHASE_2_REPORT.md`. |
| Documentation references | Local Markdown link scan across 30 docs: zero broken local references. |
| Focused post-fix receipt test | Passed; confirms stock valuation/reconciliation succeeds and no `GRN-...` journal is created. |
| Post-fix migration/system checks | `manage.py check` passed; `makemigrations --check --dry-run` reported no changes. |
| Git status/diff | Inspected. All project files are untracked, so tracked diff/stat is empty and not a reliable change inventory. No staging/reset performed. |

## Verification still incomplete or blocked

- The initial direct `.venv` command remains unavailable due to its stale interpreter reference; checks were run with the compatible external Python runtime and existing site-packages.
- No PostgreSQL environment was available. Row-lock guarantees, concurrent duplicate posting and backend-specific constraints remain unverified.
- No browser end-to-end test was run because no isolated browser demo account/database is configured. Focused Django client tests exercise selected request paths but are not browser-rendering tests.
- The earlier pre-fix pytest result (52 passed, 1 failed), prior post-retry result (56 passed plus 16 subtests), and previous authorization-snapshot result (63 passed plus 18 subtests) are historical. The latest full results appear in the validation table above; the concurrent customer-return case passes following the SQLite-only retry.
- The current resolver/source inventory found 72 application patterns and all registered admin models, but direct request verification does not cover each route, action, HTTP method, export, branch combination or related-object payload. Operation-by-operation status and evidence are in `PHASE_4_AUTHORIZATION_INVENTORY.md`.
- Report/export role assignment, auditor access, visibility of organization-wide records from branch accounts, full branch policy, and comprehensive admin audit semantics remain unresolved. Existing access is documented as observed behavior rather than approved least-privilege policy.
- In the latest continuation, added export-content regressions for the tax register and banking receivables/payables. The tax test asserts that a branch user's own-branch row is retained while another-branch and foreign-organization rows remain absent despite tampered organization/branch, date and search-like query parameters; an invalid date does not expose the foreign row. The banking test asserts output contents under branch/organization tampering, an own-party filter, a foreign-party ID, and an empty date range. These tests do not establish unresolved branch-null visibility policy.
- Content-level tenant/branch evidence currently exists for six of the 12 CSV output modes: trial balance; both inventory valuation modes; receivables; payables; and tax register. General ledger, profit and loss, balance sheet, year-end opening, budget-vs-actual, and fixed-asset exports remain without equivalent direct cross-tenant/branch content assertions. The 11 route patterns / 12 modes are inventoried; this is partial verification, not complete export isolation certification.
- Dashboard isolation evidence remains limited to the sales KPI regression. Purchase totals, valuation, receivables/payables, cash/bank, tax, stock movements, customer/supplier counts and other dashboard aggregates have not all been tested with cross-tenant and cross-branch source records.
- No first-party API route, management command, scheduled task, background worker, or attachment field/route was found in the current repository. If one is added, its tenant and authorization boundary must be reviewed before use.
- Work Units B–I have not been completed. Feature-wide Phase 1/2/3 traceability and end-to-end browser verification are outstanding.
- The current non-superuser has no active membership and therefore must be provisioned by an authorized superuser before the account can enter business screens.

## Work-unit gate summary

| Work unit | Current assessment |
|---|---|
| A — Environment, authorization and foundations | Implemented, verification incomplete: membership authority/selector and superuser provisioning are tested, but every route/export/background access path and approved role capability are not fully audited. |
| B — Shared data and business services | Partially implemented: shared organization resolution and a shared membership-role predicate are used across reviewed views. Existing module role sets were preserved. A unified capability policy is blocked on role/branch/maker-checker decisions; shared idempotency/audit coverage is not fully inventoried. |
| C — Inventory | Existing movement/weighted-average workflows pass the current regression suites. Full acceptance is incomplete pending opening-stock, backdating, return-cost and reconciliation decisions plus production-database concurrency verification. |
| D — Purchasing | Goods receipt and linked purchase-invoice regression suite passes under the confirmed no-GRNI rule. Remaining price-variance/tolerance, receipt-only return, landed-cost and approval-limit policies remain open. |
| E — Sales and returns | Existing workflows pass current tests; complete Phase 1 screen traceability, permission segregation, concurrency and every edge-case acceptance test remain incomplete. |
| F — Accounting and banking | Existing accounting/banking workflows pass current regression tests. Permission separation, production locking and complete workflow traceability are not verified. |
| G — Tax, fixed assets, budgets and reports | Existing implementations pass current regression tests. Statutory scope, asset policy decisions, report permission/filter review and external verification remain outstanding. |
| H — Remaining modules | Not implemented; scope decisions are still required for POS, CRM, distribution/dealership, manufacturing, HR/payroll, projects and services. |
| I — Integration and readiness review | Not complete: no PostgreSQL or browser-wide verification, full permission audit, complete Phase 1 feature/screen traceability review, or deployment readiness review. |

## Outstanding decisions and Phase 2 dependencies

1. **Product/Security — role and branch policy:** confirm authoritative role capabilities, branch scope, maker-checker thresholds, exports and who may provision initial memberships. Membership authority is implemented per the explicit user decision, but the full role model remains unresolved.
2. **Architecture/Operations — production database and concurrency target:** approve backend, isolation/locking expectations, backups/recovery, and concurrency validation. Current validation is SQLite-only.
3. **Finance/Operations — numbering:** approve legal entity/branch/year scope, reset and gap rules before changing numbering or guaranteeing concurrent uniqueness.
4. **Finance — currency:** decide INR-only versus multicurrency, rate source/date, precision, realized/unrealized FX and gain/loss posting. This affects monetary storage, invoices, valuation, journals and reports.
5. **Finance — inventory valuation:** resolve opening evidence/unknown values, backdating order, negative-stock exceptions, return valuation and inventory-control reconciliation where unresolved in Phase 3.
6. **Finance/Legal/Tax — GST:** verify registration/place-of-supply and rate policy, ITC/reverse-charge decisions, statutory output scope and any filing/provider integration. Internal reports do not establish statutory compliance.
7. **Finance — fixed assets:** approve acquisition source/account, depreciation schedules/useful lives, book-versus-tax treatment and disposal rules.
8. **Finance — interbranch transfers:** decide legal ownership, transfer valuation and due-to/due-from accounting where branches are separate legal entities.
9. **Legal/Security — retention and privacy:** approve record retention, deletion/anonymization, legal holds, attachment security and audit data scope.
10. **Product/Security/Finance — APIs and bank integrations:** confirm supported APIs, formats/providers, authentication, idempotency and external integration operations before implementation.
11. **Product/business owners — additional modules:** confirm scope for POS, CRM, distribution/dealership, manufacturing, HR/payroll, projects and services before implementation.

## Phase 4 completion decision

**Phase 4 is not complete.** Work Unit A, approved banking operation role gates, SQLite-only retry for concurrent return posting, and the no-GRNI receipt correction are implemented. Full permission coverage remains incomplete; Work Units B–I remain open. No Phase 5 readiness or production-readiness claim is made. The generated GRNI migration was removed before application; no business database migration was run.

## Authorization milestone reassessment (2026-10-31; continuation validation)

### Results and exact coverage

- The current resolver returns **72 non-admin application URL patterns** and **24 registered Django admin models** (155 generated admin URL patterns, as previously enumerated). The route-level test `CompleteRouteMethodAndAuthenticationTests` confirms the expected count of 72.
- For all 72 routes, the tests exercise generated-path GET behavior for an unauthenticated client and an authenticated active owner, plus rejected PUT/PATCH/DELETE. The active-owner GET accepts 200/404/redirect according to route class and generated object IDs. This is common route smoke coverage—not proof of route-specific role, organization, branch, object, POST, query-string, or export authorization. `HEAD` appears in the live allowlists but was not separately requested. Anonymous POST and supported POST behavior are not tested for every route.
- The route manifest and route-specific evidence limitations are recorded in `PHASE_4_AUTHORIZATION_INVENTORY.md`. It distinguishes common request probes from selected workflow, negative-role, foreign-object, branch, export, and admin tests; remaining operation-level checks are not marked complete.
- The inventory previously counted 10 CSV exports and omitted inventory valuation. The source has **11 route patterns with CSV support and 12 distinct CSV output modes**: 7 accounting, 2 banking, 1 tax, and 2 inventory valuation modes on `/inventory/valuation/`. Inventory CSV authorization/filter-tampering tests remain incomplete.
- Added focused inventory export isolation regression coverage for both valuation CSV modes using two organizations, multiple branches, a tampered branch filter, and foreign-branch/tenant warehouse IDs. `manage.py test` focused test passed; `pytest tests/test_inventory_valuation.py -q` passed 12 tests and 6 subtests.
- Added an inactive-account request probe over every GET-capable business route for inactive ordinary members and inactive superusers. This supplements (but does not replace) direct shared-role predicate tests; it does not exercise inactive POST or admin operations.
- Added selected aggregate/export regressions: dashboard sales KPI excludes a second organization and another branch despite supplied organization/branch query parameters; trial-balance CSV excludes uniquely named posted accounts from another branch and organization. Latest continuation tests also verify tax-register CSV content and both banking register CSV contents under tenant, branch, party and date-filter tampering. Other dashboard KPIs, six remaining export modes, and complete tax/banking filter combinations remain partially tested.
- The current tree contains user create/update/password-change audit event handling in `core/admin.py`; `organizations/tests.py` covers those events. This corrects the older report's user-admin audit gap statement for the current source. Other registered admin models are read-only or audited organization provisioning; per-model HTTP verification and a complete exactly-once audit review remain incomplete.
- A read-only security review found **no confirmed exploitable vulnerability in the reviewed scope**. This does not certify all routes: the reviewer did not exercise every supported mutation, role, branch, export, admin model, deployment setting, or production database.

### Validation in this work period

| Check | Result |
|---|---|
| Python environment | Python 3.12.0. The initially selected interpreter lacked pytest and DRF; installed declared `requirements.txt` into the active Python environment to run checks. No dependency manifest was changed. |
| `python manage.py check` | Passed; no issues. |
| `python manage.py makemigrations --check --dry-run` | Passed; no changes detected. No migrations were applied to the business database. |
| `python manage.py test` (latest) | Passed: **140 tests**. Isolated test database created and destroyed. |
| `python -m pytest -q` (latest) | Passed: **70 tests and 366 subtests**. |
| Focused tax and banking export tests | Passed: **34 tests** with isolated test database. |
| Resolver-backed common route probes | Test suite contains assertions for all **72** routes as described above; these do not constitute full operation authorization coverage. |
| Admin registration enumeration | **24** registered models. |
| Git/workspace | Project files are untracked (`??`) in this workspace. No staging, commit, reset, clean, push, or source-code change was made. |

### Policy decision and stop condition

The owner response first referred to a revised matrix, but no matrix content was present; the owner then explicitly deferred approval. No role access was changed. `ROLE_SCREEN_MATRIX.md` contains a clearly marked, **DEFERRED / NOT APPROVED** proposal with data sensitivity, justification, and security implications; it is not implemented policy. Approval remains required for role × information category × organization/branch scope × read/export rights, including auditor/read-only access, finance managers, branch managers, organization-wide viewers, and export operators.

No production authorization policy or application behavior was changed in this continuation; the changes add test coverage and evidence only. Export endpoints do not yet have a new independent export-capability guard; enforcing such a guard without the missing policy could silently grant or deny access contrary to owner intent. Sensitive information categories, branch-specific versus organization-wide versus shared data, and separate-permission records remain partially classified rather than fully approved. Cross-branch/tenant aggregates, all exports, and all state-changing operations are not fully tested. **Authorization and branch isolation are not complete; Phase 4 remains in progress and Phase 5 has not started.**

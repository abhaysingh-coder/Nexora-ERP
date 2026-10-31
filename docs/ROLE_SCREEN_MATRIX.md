# Nexora ERP role and screen matrix — Phase 1

This matrix is a review artifact, not authorization code. The Phase 1 “existing behavior” rows below are the Phase 1 source-review snapshot; see the dated Phase 4 verification note for later changes. The project uses a custom user with organization/branch fields plus organization membership role strings. App views use module-specific guards, with common membership and organization-context helpers. The proposed capability matrix is not an approved policy.

## Access notation

| Mark | Existing behavior represented |
|---|---|
| **G** | The relevant view/action checks organization membership and an explicit role set, with object/branch scoping where implemented. |
| **S** | Login plus organization/branch-scoped read is present; no per-role read policy is consistently applied. |
| **L** | Login/organization checks exist, but a module-role check for that action was not found. |
| **P** | Partial/ad-hoc guard; the precise role/action behavior differs by route or helper. |
| **—** | No first-party screen/route was found. |

`G` does not imply every action (view/create/edit/approve/post/reverse/export) is separately authorized. Object scope, branch scope, membership activity and service-level checks must be reviewed per route. Roles in this document are case-sensitive examples found in source.

## Existing role strings observed in source

| Module/action | Current accepted membership roles | Notes |
|---|---|---|
| Sales invoices/orders and customer returns | `owner`, `admin`, `sales_manager`, `sales`, `accountant`, `finance_manager`; superuser bypass | Includes create/post/return paths via shared helper; no separate approve capability. |
| Purchase orders/receipts/invoices and supplier returns | `owner`, `admin`, `purchase_manager`, `purchases`, `accountant`, `finance_manager`; superuser bypass | Same role family often prepares and posts. |
| Inventory mutations | `owner`, `admin`, `inventory_manager`, `warehouse_operator`, `manager`; superuser bypass | Product/warehouse/adjustment/transfer/opening actions. |
| Accounting finance changes | `owner`, `admin`, `accountant`, `finance_manager`; superuser bypass | Period/year close also requires no assigned branch. Reports generally require authentication and organization resolution, not this finance role. |
| Tax configuration | `owner`, `admin`, `accountant`, `finance_manager`; active superuser bypass | The former `is_staff` bypass was removed; export/read role policy remains open. |
| Banking allocations | `owner`, `admin`, `accountant`, `finance`, `finance_manager`, `manager`, `bookkeeper`; superuser bypass | Reversal uses narrower set: `owner`, `admin`, `accountant`, `finance_manager`. |
| Banking account/transaction creation, reconciliation confirmation/import | `owner`, `admin`, `accountant`, `finance_manager` | Explicitly approved by owner on 2026-10-24 and enforced by `banking/permissions.py`; transaction/account reads remain broader. |
| Organization/user administration | Active-superuser-only Django admin | Organization, branch, and membership provisioning are audited in admin; custom user administration is active-superuser-only, with branch changes audited. |

## Current screen-family matrix

| Screen family (screen IDs) | Owner/admin | Sales roles | Purchasing roles | Finance roles | Inventory roles | Cashier / bank role | Auditor / read-only | Current interpretation |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Sign in/out (`SCR-AUTH-01`) | S | S | S | S | S | S | S | Authentication screens exist; provisioning/recovery absent. |
| Dashboard (`SCR-DASH-01`) | S | S | S | S | S | S | S | Login required; metrics use organization/branch logic; no role-specific dashboard visibility. |
| Sales list/detail and sales reports (`SCR-SAL-05-*`, `SCR-SAL-03-*`) | S/G | G for mutations | S | G for mutations | S | S | S | Reads are broader than mutation helpers; role and branch enforcement vary. |
| Sales create/post and customer returns (`SCR-SAL-05-F/D`, `SCR-RET-01–03`) | G | G | — | G | — | — | — | Explicit sales role helper; return post/cancel uses role check. |
| Purchase lists/detail (`SCR-PUR-*`) | S/G | — | G for mutations | G for mutations | S | S | S | Purchasing helper restricts mutations; read routes use organization/branch scope. |
| Purchase create/confirm/receipt/invoice/return (`SCR-PUR-*`, `SCR-RET-04–06`) | G | — | G | G | Some receipt visibility | — | — | Explicit purchase role helper on mutations. |
| Inventory list/valuation (`SCR-INV-01/05`) | S | S | S | S | S | S | S | Organization member and branch scoping; valuation reader is not limited to inventory manager. |
| Inventory product/warehouse/adjustment/transfer/opening (`SCR-INV-02/03/06–08`) | G | — | — | — | G | — | — | Explicit inventory mutation role helper. |
| Accounting reports and journal detail (`SCR-ACC-01–04`, `12`) | S | S | S | S | S | S | S | Login plus authorized organization; detail/read role may be broader than intended. |
| Period/year close, budget and fixed asset mutations (`SCR-ACC-06–09`) | G | — | — | G | — | — | — | Finance helper; period/year-end close requires organization-level user. Budget/assets read screens are broader than edit controls. |
| Banking overview/registers (`SCR-BNK-01`, `06–07`) | S | S | S | S | S | S | S | Login and org/branch scope; export access is not separately modeled. |
| Banking account/transaction/import/reconcile (`SCR-BNK-02/03/05/08`) | P | — | — | G for approved roles | — | P | P | Account/transaction creation, posting, CSV import and reconciliation confirmation require `owner`, `admin`, `accountant`, or `finance_manager`; reads remain login/org/branch scoped, and complete read/export branch policy remains unresolved. |
| Banking allocate/reverse/unreconcile (`SCR-BNK-04`) | P | — | — | G/P | P | P | — | Allocation role set is wider; reverse/unreconcile narrower. Route-specific audit and POST behavior exist. |
| Tax setup (`SCR-TAX-02`) | G | — | — | G | — | — | — | Requires an active organization membership with an existing finance role, or an active superuser; Django `is_staff` alone no longer bypasses the guard. |
| Tax registers/summaries (`SCR-TAX-01`, `03–07`) | S | S | S | S | S | S | S | Organization/branch scoped; separate report/export role policy is not complete. |
| Audit log (`SCR-AUD-01`) | — | — | — | — | — | — | — | No first-party audit browser found; selected action records exist. |
| All proposed screens | — | — | — | — | — | — | — | Permission design belongs to the Phase 1 target; routes do not exist. |

## Target capability matrix for approval

Legend: `R` read; `C` create; `E` edit draft/configuration; `A` approve; `P` post/commit; `V` reverse/cancel/unreconcile; `X` export; `—` none. The entries below are **proposals** to make review concrete, not user-approved policy. They intentionally separate maker and checker for high-impact actions.

| Screen family | Owner / org admin | Finance manager / accountant | Sales / sales manager | Purchasing / purchase manager | Inventory manager / warehouse | Cashier / bank reconciler | Branch manager | Auditor |
|---|---|---|---|---|---|---|---|---|
| Organization, branch, users, roles (`SCR-ORG-*`, `SCR-IAM-*`) | R/C/E/A | R | — | — | — | — | R branch | R scoped |
| Customer/supplier/product/UOM/account masters (`SCR-MST-*`, `SCR-ACC-10`) | R/C/E | R/C/E finance/tax mappings | R/C/E customer | R/C/E supplier | R/C/E product/UOM/warehouse | R counterparties | R/C/E branch-only as granted | R scoped |
| Sales enquiry, quote, order, delivery, invoice (`SCR-SAL-*`) | R/A by policy | R, approve/post per segregation | R/C/E; manager A; designated finance P | R | R stock/dispatch, manager A for adjustment | R receipts only | R branch; approve by grant | R/export scoped |
| Sales returns/credit notes (`SCR-RET-01–03`) | R/A | R/P/V finance correction | R/C/E; manager A; finance P | — | R restock | — | R branch | R/export scoped |
| Requisition, PO, receipt, supplier bill (`SCR-PUR-*`) | R/A by policy | R/P finance bill | — | R/C/E; manager A; designated receiver P receipt | R/C receipt only | — | R branch | R/export scoped |
| Supplier returns/debit notes (`SCR-RET-04–06`) | R/A | R/P/V finance correction | — | R/C/E; manager A; finance P | R stock deduction | — | R branch | R/export scoped |
| Inventory moves/counts/valuation (`SCR-INV-*`) | R/A by policy | R/X valuation and variance journals | R availability | R availability/planning | R/C/E; independent count A; scoped P | — | R branch | R/X scoped |
| Accounting journals/reports/period/close/assets (`SCR-ACC-*`) | R/A per governance | R/C/E; separate A/P/V; X | R selected reports | R selected reports | R selected reports | R selected cash reports | R branch reports | R/X scoped |
| Banking/reconciliation/registers (`SCR-BNK-*`) | R/A by policy | R/C/E/A/P/V/X per separation | R own customer receipts | R own supplier payments | — | R/C; allocate; reconciler confirm; separate reverse | R branch | R/X scoped |
| GST setup/register/statutory prep (`SCR-TAX-*`) | R/A setup | R/C/E tax setup and reports; designated P statutory | R source invoice | R source bill | — | — | R branch | R/X scoped |
| Audit, approvals, API/system (`SCR-AUD-*`, `SCR-OPS-*`, `SCR-API-*`) | Administer business roles; no log edit | R business audit / approve in assigned scope | Submit/view assigned | Submit/view assigned | Submit/view assigned | Submit/view assigned | R branch inbox | R log only |
| POS/manufacturing/HR/CRM/projects | R/config by selected product | R accounting side | Role by module after scope decision | Role by module after scope decision | Role by module after scope decision | Retail tender if POS selected | Branch scope only | R scoped |

## Decisions needed before enforcement work

1. Confirm the role names, whether users may hold multiple roles, and whether scope comes from `User.organization`, active `OrganizationMembership`, assigned branch or an explicit active-workspace selection.
2. Define which capabilities are independent: read/create/edit/submit/approve/post/cancel/reverse/export and configuration. Specify amount thresholds and maker-checker conflicts.
3. Decide whether branch users may view organization-wide accounts, cash, tax totals, unassigned journals, budgets or stock values.
4. Review staff/superuser bypasses and login-only bank mutations. Decide how audit/export access is assigned.
5. Define auditor retention, PII access and export permissions; define emergency access and revocation/audit policy.

## Phase 4 verification note (2026-10-24)

- The owner explicitly selected active `OrganizationMembership` as the organization authority for non-superusers, with a validated selector for users who have multiple active memberships. `User.organization` does not itself grant tenant access. Membership creation/activation and role assignment are explicit superuser-only provisioning actions; no automatic membership backfill occurred.
- Business module routes are protected by the organization-context middleware, and reviewed app views use the shared active-organization resolver. This verifies tenant context, not a complete role/capability policy.
- Membership role matching now uses the shared `core.permissions.has_organization_role()` predicate across sales, purchasing, returns, inventory, accounting, banking and tax views. Each module retains its existing role set, so this refactor does not approve new capabilities.
- Tax setup no longer treats Django `is_staff` as a finance capability. It requires superuser or an active membership with an existing finance role string. Superuser bypasses continue only where currently implemented.
- Organization, branch, membership and custom-user administration are now registered in Django admin and restricted to active superusers. Membership edits require a reason and produce audit events.
- Purchasing, returns and tax Django-admin registrations are now active-superuser-only and read-only, because the admin paths did not apply the business-view tenant/role checks or all required audit behavior. Django `auth.Group` administration was removed; membership role strings remain the current application authorization source.
- The owner approved `owner`, `admin`, `accountant`, and `finance_manager` for bank-account creation, transaction posting, statement CSV import, and reconciliation confirmation. These actions now enforce that exact active-membership role set server-side. Reconciliation undo continues using the same narrow set. General allocation retains its separately existing wider set, including `finance`, `manager`, and `bookkeeper`; access to allocation/report reads remains a separate policy concern.
- The shared role predicate now rejects inactive users as well as inactive memberships; inactive superusers are also rejected before the existing superuser bypass. Focused regression tests cover both cases.
- The role names, action separation, branch-level grants, maker-checker thresholds, report/export roles, and most login-only banking mutations remain unresolved. The target capability matrix above remains a proposal and must not be mistaken for enforced policy.
- Resolution: decision 1 is closed for organization authority; decisions 2–5 remain open for capability policy, branch scope, banking controls, exports and audit access.

## Phase 4 report and export policy proposal (2026-10-31 — NOT APPROVED)

**Owner decision: DEFERRED (2026-10-31).** This proposal is not approved policy, authorization code, or an implemented grant. The owner response referred to a revised matrix, but no matrix content accompanied it, then explicitly deferred approval. Do not add, remove, or infer report/export permissions unless the owner later supplies and approves a complete role × category × scope × action matrix. Existing observed behavior is not evidence of policy approval.

| Information category | Sensitivity | Business justification | Main security implication | Proposed default for explicit approval |
|---|---|---|---|---|
| Operational | Moderate | Daily workflow status, documents, and process monitoring | Cross-branch records can reveal staffing, throughput, and counterparties | Operational roles read their assigned organization/branch; branch managers remain branch-only |
| Inventory | High | Stock availability, warehouse operations, valuation, and reconciliation | Stock/value disclosure can enable fraud, shrinkage, and commercial inference | Inventory roles read assigned scope; valuation and its exports are separately granted |
| Sales and purchasing | High | Order, delivery, invoice, supplier, customer, pricing, and return operations | Exposes customer/supplier information, pricing, and commercial terms | Sales/purchasing roles read their module and assigned branch; no cross-branch by default |
| Accounting and financial | Very high | Ledger, statements, close, budgets, assets, and financial control | Enables exposure of financial position and sensitive account activity | Finance roles receive only approved report categories and assigned organization/branch scope |
| Banking | Very high | Cash, bank transactions, allocations, receivables/payables, and reconciliation | Exposes payment behavior, balances, counterparties, and reconciliation evidence | Explicit banking read and export grants; no access inferred solely from another report role |
| Tax | Very high | Internal tax registers, summaries, and liability review | Exposes tax identifiers, transaction-level tax, and filing-sensitive data | Explicit tax read and export grants; reports remain internal, not statutory filings |
| Audit | Very high | Independent review of sensitive actions and control evidence | Can expose user identities, access changes, and security-sensitive event details | Read-only and explicitly assigned tenant/branch scope; export is a separate grant |

| Role/persona (proposed) | Operational | Inventory | Sales/purchasing | Accounting/financial | Banking | Tax | Audit | Export rule |
|---|---|---|---|---|---|---|---|---|
| Owner / organization administrator | No new report grant implied | No new report grant implied | No new report grant implied | No new report grant implied | No new report grant implied | No new report grant implied | No new report grant implied | Existing admin control-plane access does not automatically approve business exports |
| Finance manager | R only where specifically granted; assigned scope | R only where specifically granted; assigned scope | R only where specifically granted; assigned scope | Proposed R for explicitly assigned financial reports and scope | Separate explicit R grant | Separate explicit R grant | No default grant | Separate category × scope X grant |
| Accountant | No default grant | No default grant | No default grant | Proposed R for assigned accounting reports and scope | Separate explicit R grant | Separate explicit R grant | No default grant | Separate category × scope X grant |
| Branch manager | Proposed operational R, assigned branch only | Proposed R, assigned branch only | Proposed R, assigned branch only | No default grant | No default grant | No default grant | No default grant | Separate category × branch X grant |
| Auditor | Proposed read-only R only for explicitly assigned categories and scope | Same | Same | Same | Same | Same | Proposed read-only R, explicitly assigned scope | No export unless separately granted |
| Read-only user | No implicit access; explicit category and scope grant required | Same | Same | Same | Same | Same | Same | No export unless separately granted |
| Organization-wide report viewer | No implicit access; explicit category grant required, organization-wide scope only when approved | Same | Same | Same | Same | Same | Same | No export unless separately granted |
| Export operator | No independent read grant | No independent read grant | No independent read grant | No independent read grant | No independent read grant | No independent read grant | No independent read grant | X is additive to matching read permission and exactly inherits its filters/scope; audit each export |

`R` means read and `X` means export. Every proposed cell remains blocked until its scope is approved. Export endpoints must independently check the approved export capability; they must not treat a visible report page, navigation link, or query parameter as authorization. An export must preserve server-derived tenant and branch scope regardless of organization, branch, date, object-ID, or filter parameters.

The proposal intentionally does not reconcile with current free-text membership role strings or decide organization-wide visibility for unbranched accounts, journals, tax configuration, or shared reference/configuration data. Those mappings and record classifications require explicit owner approval before enforcement.

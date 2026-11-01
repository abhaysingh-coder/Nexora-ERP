# Phase 4 Owner Policy Decisions

Status: **Policy models approved on 2026-11-01; role-to-capability assignments and implementation remain partially blocked as specified below.** The current application behavior was not treated as approval. No permissions have been changed. The role assignment proposal in `ROLE_SCREEN_MATRIX.md` remains **DEFERRED / NOT APPROVED**.

## Decision register

| ID | Policy decision | Approval status |
|---|---|---|
| P4-POL-01 | Report viewing capabilities | Policy model **APPROVED**; role assignments **BLOCKED — OWNER APPROVAL REQUIRED** |
| P4-POL-02 | CSV export capabilities | Separate-capability model **APPROVED**; role assignments **BLOCKED — OWNER APPROVAL REQUIRED** |
| P4-POL-03 | Auditor access | Time-bound audited-access model **APPROVED**; scope and eligible roles **BLOCKED — OWNER APPROVAL REQUIRED** |
| P4-POL-04 | Branch-null ledger visibility | Separate shared/unallocated presentation **APPROVED AND IMPLEMENTED** |
| P4-POL-05 | Branch-null budget actuals | Separate unallocated presentation **APPROVED AND IMPLEMENTED** |
| P4-POL-06 | Shared fixed-asset visibility | Separate shared-assets capability model **APPROVED**; eligible roles **BLOCKED — OWNER APPROVAL REQUIRED** |
| P4-POL-07 | Organization-wide report access | Per-category capability model **APPROVED**; role assignments **BLOCKED — OWNER APPROVAL REQUIRED** |
| P4-POL-08 | Year-end export permissions | Separate year-end-export capability model **APPROVED**; eligible roles **BLOCKED — OWNER APPROVAL REQUIRED** |

## Approval record

On **2026-11-01**, the owner selected the policy options interactively in this conversation. This date is the date the selections were received; no separate approval identity or external record was supplied. The selections approve the policy models below, not unspecified role grants.

| Decision | Owner-selected policy | Approval boundary |
|---|---|---|
| P4-POL-01 | Role × information category × organization/branch capability | Policy model approved. The owner has not supplied which roles receive `view` for each category and scope. |
| P4-POL-02 | Separate export capability | Policy model approved. The owner has not supplied which roles receive export capability per category and scope. |
| P4-POL-03 | Time-limited, separately approved access with logged downloads | Auditor-access model approved. Eligible roles, permitted categories/scopes, approval process, duration, and audit-event design remain unspecified. |
| P4-POL-04 | Show branch-null ledger entries separately as shared/unallocated | Presentation and attribution rule approved. Do not mix these amounts into branch-specific totals. |
| P4-POL-05 | Show branch-null budget actuals separately as unallocated | Presentation and variance-attribution rule approved. Do not treat them as branch actuals. |
| P4-POL-06 | Separate shared-assets view/capability | Capability model approved. Eligible roles and whether the capability permits view and/or export remain unspecified. |
| P4-POL-07 | Organization-wide capability per information category | Policy model approved. The owner has not supplied which roles receive each category's organization-wide capability. |
| P4-POL-08 | Separate year-end-export capability | Policy model approved. The owner has not supplied which roles receive the capability or how auditor access interacts with it. |

## P4-POL-01 — Report viewing capabilities

- **Existing behavior:** Report routes use their existing route-specific authentication, active-organization, and module-role checks. There is no approved, unified report-view capability matrix. Role requirements therefore vary by report and are not an owner-approved role policy.
- **Sensitivity and justification:** Reports aggregate operational, inventory, sales/purchasing, accounting/financial, banking, tax, and audit information. Aggregates may reveal commercially sensitive or personally identifiable details even when underlying records are not directly editable.
- **Affected roles:** Owner, admin, accountant, finance manager, branch manager, read-only user, auditor, and any organization-wide report viewer.
- **Affected modules/routes:** `/dashboard/`; accounting `/accounting/reports/trial-balance/`, `/accounting/reports/general-ledger/`, `/accounting/reports/profit-and-loss/`, `/accounting/reports/balance-sheet/`, `/accounting/year-end/<year_id>/`, `/accounting/budgets/`, `/accounting/fixed-assets/`; banking `/banking/`, `/banking/receivables/`, `/banking/payables/`, `/banking/reconcile/`; taxation `/taxation/summary/output/`, `/taxation/summary/input/`, `/taxation/summary/liability/`, `/taxation/register/sales/`, `/taxation/register/purchases/`; inventory `/inventory/valuation/`.
- **Choices:** (1) Grant report view by role and information category; (2) grant report view by role plus organization/branch scope; (3) require separately assigned report capabilities per report/category.
- **Trade-offs:** (1) is easy to administer but can grant excessive access across categories; (2) constrains tenant/branch scope but still couples broad categories to roles; (3) is most precise and auditable but needs more policy maintenance.
- **Recommendation:** Approve an explicit capability for each role × information category × organization/branch scope. Until approval, preserve current route checks and do not infer that a screen being reachable grants a policy-approved entitlement.
- **Tests required after approval:** Positive and negative tests per approved role and report category; wrong-organization and wrong-branch requests; direct URL access; object IDs and filter tampering; no unexpected disclosure in rendered aggregates.
- **Owner approval required:** Yes.
- **Approval status:** Policy model **APPROVED 2026-11-01**; assignment of roles to each category/scope **BLOCKED — OWNER APPROVAL REQUIRED**.
- **Approval field:** Approved model: role × category × organization/branch capability. Role mappings not supplied.

## P4-POL-02 — CSV export capabilities

- **Existing behavior:** Selected content-isolation tests cover 12 CSV output modes across 11 route patterns. Export access currently follows route-specific report behavior; a unified independent export-capability guard has not been approved or implemented.
- **Sensitivity and justification:** Downloadable files can be retained, copied, and redistributed outside the ERP. They contain the same high-sensitivity information as reports and may provide larger, machine-readable extracts.
- **Affected roles:** All report roles plus export operators and read-only users.
- **Affected modules/routes:** `/accounting/reports/trial-balance/?format=csv`, `/accounting/reports/general-ledger/?format=csv`, `/accounting/reports/profit-and-loss/?format=csv`, `/accounting/reports/balance-sheet/?format=csv`, `/accounting/year-end/<year_id>/?format=csv`, `/accounting/budgets/?format=csv`, `/accounting/fixed-assets/?format=csv`, `/banking/receivables/?format=csv`, `/banking/payables/?format=csv`, `/taxation/register/export.csv`, `/inventory/valuation/?format=csv`, and `/inventory/valuation/?history_csv=1`.
- **Choices:** (1) Export rights match report-view rights; (2) export requires a separate capability; (3) allow exports only for named roles/categories and approved scopes.
- **Trade-offs:** (1) is simple but makes every report reader able to extract data; (2) adds a clear control boundary but requires separate capability assignment and audit design; (3) limits high-risk exports most tightly but needs a maintained route/category allowlist.
- **Recommendation:** Require a separate export capability, scoped by information category and organization/branch, with explicit handling for especially sensitive finance, banking, tax, and audit extracts. Do not add a grant before approval.
- **Tests required after approval:** Each export denied/allowed per policy; parse actual CSV contents; test direct export URL, foreign IDs, organization/branch/date/search/order tampering, malformed filters, and branch-null scope.
- **Owner approval required:** Yes.
- **Approval status:** Separate-capability model **APPROVED 2026-11-01**; assignment of roles to export categories/scopes **BLOCKED — OWNER APPROVAL REQUIRED**.
- **Approval field:** Approved model: independent export capability. Role mappings not supplied.

## P4-POL-03 — Auditor access

- **Existing behavior:** No separate auditor read-only policy is approved in the role matrix. Existing route behavior must not be interpreted as an auditor grant.
- **Sensitivity and justification:** Auditors may need evidence across periods, branches, and audit history. Broad access could disclose employee, customer, supplier, banking, tax, and organization-wide financial data; write access would undermine independence.
- **Affected roles:** Auditor, owner, admin, accountant, finance manager, branch manager, and read-only user.
- **Affected modules/routes:** The report/export paths listed in P4-POL-01 and P4-POL-02; `/banking/` and `/banking/reconcile/`; taxation reports/registers; Django admin user, organization, membership, and audit-event model routes (generated admin URLs); and `/dashboard/`.
- **Choices:** (1) No auditor role; (2) read-only access to explicitly enumerated categories and scopes; (3) time-bounded, separately approved access with auditable downloads.
- **Trade-offs:** (1) avoids new disclosure but may force shared accounts or manual evidence handling; (2) supports independence with predictable least privilege but requires careful scope design; (3) limits duration and improves traceability but adds provisioning/review overhead and cannot replace category-level restrictions.
- **Recommendation:** If an auditor role is approved, make it read-only by default, enumerate permitted records and export rights separately, and log sensitive access. Exclude user/organization administration unless separately approved.
- **Tests required after approval:** Prove read-only denial for all mutations and admin routes; verify permitted report/object scope and denial for forbidden categories, other organizations/branches, export paths, and expired/disabled access.
- **Owner approval required:** Yes.
- **Approval status:** Time-limited, separately approved access with logged downloads **APPROVED 2026-11-01**; eligible roles, data scopes, duration, approval and audit implementation **BLOCKED — OWNER APPROVAL REQUIRED**.
- **Approval field:** Model approved. No eligible-role or category/scope grant supplied.

## P4-POL-04 — Branch-null ledger visibility

- **Existing behavior before approval:** Tested general-ledger, profit-and-loss, balance-sheet, and dashboard receivable/payable amounts combined branch-null lines into branch results; the dashboard recent-journal list included only exact-branch entries.
- **Sensitivity and justification:** Unassigned journal entries may represent shared or centrally managed activity. Including them can expose organization-level financial balances; excluding them may make branch totals incomplete.
- **Affected roles:** Branch-assigned users, branch managers, accountants, finance managers, auditors, and organization-wide viewers.
- **Affected modules/routes:** `/accounting/reports/trial-balance/`, `/accounting/reports/general-ledger/`, `/accounting/reports/profit-and-loss/`, `/accounting/reports/balance-sheet/`, `/accounting/journal-entries/<id>/`, `/accounting/budgets/`, `/accounting/year-end/<year_id>/`, and `/dashboard/` receivables/payables/recent journal activity.
- **Choices:** (1) Exclude branch-null entries from branch views; (2) include them in a separately labeled shared/unallocated category; (3) allow only a separately authorized role to include them.
- **Trade-offs:** (1) reduces cross-branch disclosure but may make branch totals incomplete; (2) preserves transparency without attributing shared records to a branch but requires UI/report changes; (3) limits disclosure but may complicate reconciliation for ordinary branch users.
- **Recommendation:** Do not merge unassigned entries into branch totals by default. Make shared/unallocated amounts separately visible only under an explicit capability, without changing current behavior until the policy is approved.
- **Tests required after approval:** For trial balance, ledger, P&L, balance sheet, dashboard aggregates and recent activity, verify branch/null/other-branch/foreign-tenant content and totals; include direct URLs and query tampering.
- **Owner approval required:** Yes.
- **Approval status:** Separate shared/unallocated presentation **APPROVED 2026-11-01 and IMPLEMENTED** for trial balance, general ledger, profit and loss, balance sheet, dashboard working-capital aggregates, and dashboard recent journal activity. Branch-null entries remain visible but are labeled separately and excluded from branch-specific totals/balances.
- **Approval field:** Approved option: show separately as shared/unallocated.
- **Implementation evidence:** Branch users receive a distinct shared/unallocated section on the report pages; branch CSV rows carry a `Scope` column distinguishing assigned-branch and shared/unallocated rows. Dashboard working-capital KPIs exclude branch-null ledger lines and render shared receivable/payable amounts separately; journal activity is likewise split. Direct journal details label null-branch entries `Shared / unallocated`. Organization-level consolidation remains aggregated across its organization.

## P4-POL-05 — Branch-null budget actuals

- **Existing behavior before approval:** The tested budget-vs-actual CSV included branch-null journal actuals in branch-specific budget actual and variance, while excluding another branch's actuals.
- **Sensitivity and justification:** Budget-versus-actual figures expose financial performance and may misstate a branch's performance if centrally posted actuals are implicitly allocated to it.
- **Affected roles:** Branch managers, accountants, finance managers, auditors, and organization-wide report viewers.
- **Affected modules/routes:** `/accounting/budgets/` including CSV output and any budget variance display.
- **Choices:** (1) Exclude branch-null actuals from branch budgets; (2) present them as separate unallocated actuals; (3) include only where a documented allocation rule assigns them.
- **Trade-offs:** (1) keeps branch comparisons attributable but can omit shared costs; (2) avoids false attribution but requires budget report presentation changes; (3) supports allocations when justified but depends on a controlled, auditable allocation rule.
- **Recommendation:** Keep branch-null actuals separate and unallocated unless an approved allocation rule exists. Do not silently include them in branch variance.
- **Tests required after approval:** CSV and on-screen budget output for branch, null, other-branch, foreign-tenant, account/period filters; assert each line and computed variance, including allocation behavior if approved.
- **Owner approval required:** Yes.
- **Approval status:** Separate unallocated presentation **APPROVED 2026-11-01 and IMPLEMENTED** for branch-specific budget lines. Actual/variance now use only the assigned branch's entries; branch-null actual is a separate `Shared / unallocated actual` value in the on-screen and CSV outputs.
- **Approval field:** Approved option: show separately as unallocated.
- **Implementation evidence:** The regression asserts the branch actual, separately reported null-branch actual, and variance from the branch actual in CSV and template context. Organization-wide budget rows retain their existing consolidated calculation; branch-user access to null-branch budget records and full role/category scope remain pending.

## P4-POL-06 — Shared fixed-asset visibility

- **Existing behavior:** Tested fixed-asset output for a branch-scoped user includes branch-null assets and excludes other-branch and foreign-organization assets. Current behavior does not establish that these shared assets should be visible or reportable by every branch user.
- **Sensitivity and justification:** Asset registers and net book values disclose organization-wide financial position and may include assets centrally held or assigned to another operating unit.
- **Affected roles:** Branch managers, accountants, finance managers, auditors, and organization-wide viewers.
- **Affected modules/routes:** `/accounting/fixed-assets/` and fixed-asset CSV output; related depreciation/disposal reports.
- **Choices:** (1) Exclude branch-null assets from branch views; (2) allow a separate shared-assets view; (3) grant organization-wide asset visibility to a named role.
- **Trade-offs:** (1) limits exposure but hides centrally-held assets from branch operations; (2) makes shared property explicit but needs a separate screen/filter and permission; (3) simplifies consolidated reporting but grants broad asset values to the named role.
- **Recommendation:** Treat branch-null assets as shared records and require explicit shared-asset or organization-wide capability to view/export them from branch accounts.
- **Tests required after approval:** Asset list/detail/CSV and depreciation/disposal paths; assert branch, null, other-branch and foreign-tenant records, direct IDs, search/filter tampering, and mutation denial for read-only roles.
- **Owner approval required:** Yes.
- **Approval status:** Separate shared-assets capability model **APPROVED 2026-11-01**; eligible roles and view/export rights **BLOCKED — OWNER APPROVAL REQUIRED**.
- **Approval field:** Model approved. No capability assignment supplied.

## P4-POL-07 — Organization-wide report access

- **Existing behavior:** Scope is derived per route from the authenticated user's active organization and, on some screens, branch assignment. Selected tests show organization-level access aggregating records across branches, while other routes use their own restrictions. No universal organization-wide report entitlement is approved.
- **Sensitivity and justification:** Cross-branch aggregation is useful for oversight and consolidation but exposes the full organization's operational and financial position.
- **Affected roles:** Owner, admin, finance manager, accountant, branch manager, auditor, and proposed organization-wide report viewer.
- **Affected modules/routes:** `/dashboard/`; sales `/sales/`; purchasing `/purchases/`; inventory `/inventory/valuation/`; accounting report paths listed in P4-POL-01; banking `/banking/`, `/banking/receivables/`, `/banking/payables/`; and taxation report/register paths listed in P4-POL-01.
- **Choices:** (1) Organization-wide access limited to named roles; (2) per-category organization-wide capabilities; (3) branch-only access except for separately authorized consolidation users.
- **Trade-offs:** (1) is straightforward but grants broad cross-category visibility; (2) best matches different sensitivity and business duties but needs explicit capability administration; (3) minimizes default exposure but may hinder consolidated operations and reporting.
- **Recommendation:** Use explicit organization-wide capabilities per information category; do not infer them from membership alone or from access to an organization selector.
- **Tests required after approval:** For each permitted category, compare authorized organization-wide vs branch user outputs across every branch and null-branch data; verify foreign organization exclusion and tampered session/query/object IDs.
- **Owner approval required:** Yes.
- **Approval status:** Per-category organization-wide capability model **APPROVED 2026-11-01**; role assignments **BLOCKED — OWNER APPROVAL REQUIRED**.
- **Approval field:** Model approved. No role/category assignments supplied.

## P4-POL-08 — Year-end export permissions

- **Existing behavior:** The year-end opening CSV route is an organization-level operation. The existing regression denies a branch-assigned user and verifies authorized organization-level finance output across that organization's branch and branch-null opening rows. This is not a decision on which roles should receive the export.
- **Sensitivity and justification:** Opening balances summarize the entire prior-period financial position and can reveal organization-wide account-level balances.
- **Affected roles:** Owner, admin, accountant, finance manager, auditor, branch manager, and export operator.
- **Affected modules/routes:** `/accounting/year-end/<year_id>/` and its `?format=csv` output mode.
- **Choices:** (1) Restrict to explicitly named finance roles; (2) require an additional year-end-export capability; (3) permit auditors only through separately approved, logged read access.
- **Trade-offs:** (1) is easy to enforce but can conflate close/post authority with export authority; (2) separates high-risk download permission but needs assignment and review; (3) supports independent audit but requires a defined read-only scope and reliable access logging.
- **Recommendation:** Keep branch-assigned users denied and require a distinct year-end-export capability for organization-level users. Review/export access should be logged if supported by the approved audit policy.
- **Tests required after approval:** Allowed/denied roles, branch-assigned users, foreign financial year IDs, output row contents across branch and null-branch rows, query/session tampering, and audit event/access record behavior.
- **Owner approval required:** Yes.
- **Approval status:** Separate year-end-export capability model **APPROVED 2026-11-01**; eligible roles and auditor interaction **BLOCKED — OWNER APPROVAL REQUIRED**.
- **Approval field:** Model approved. No capability assignment supplied.

## Dashboard isolation evidence (not a policy decision)

The current dashboard has no tax summary, charts, customer/supplier counts, or separate sales/purchase activity list. It displays posted sales and purchases, cash-and-bank balances, low-stock count, known inventory value and valuation-completeness exceptions, receivables, payables, and recent journal activity. Three Django client regressions exercise branch-assigned and organization-level owners using posted, multi-organization/multi-branch records and assert rendered amounts and journal references, including posted branch-null sales and purchase invoices. Those invoice totals are excluded from branch sales/purchase KPIs. Branch-null journal lines now display as separate shared/unallocated receivables/payables and recent journal activity, pursuant to P4-POL-04. Posting still routes null-branch stock movements through an organization warehouse, affecting that warehouse's branch valuation; this remains a separate unresolved operational scope concern. The unallocated-stock exception count remains organization-wide for a branch dashboard and requires a separate owner decision.

Focused results and exact scope are recorded in the Phase 4 implementation report and authorization inventory. The full Phase 4 authorization audit remains incomplete.

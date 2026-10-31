# Nexora ERP â€” Database Constraints and Decisions

This register distinguishes constraints enforced by the database from service/model checks. A Django `save()`/`clean()` check can be bypassed by bulk SQL, another client, or certain bulk ORM paths. No constraint described as proposed is implemented by this document.

## Existing verified controls

| Invariant | Current control | Gap / design action |
|---|---|---|
| Unique organization slug; branch code per org; membership user/org | Database unique fields/constraints | Branch and User.organization must match tenant; currently not a composite FK. |
| One organization/product/warehouse valuation balance; nonnegative inventory value | Database unique and check constraints | Does not prove product/warehouse organization matches balance organization. |
| Stock movement/valuation immutability | Model save/queryset overrides and valuation write service | Not database-level; protect DB role/access and add append-only strategy if external writers are introduced. |
| Positive sales order/delivery quantities and line price rules | Some DB checks plus model/service validation | Audit exact migration constraints; never assume all line models are covered. |
| Posted sales/purchase workflows and matching tenant/source records | Atomic services/model validation | Cross-row invariant, bypassable by direct DB writes; add tenant-aware DB strategy. |
| Journal line has exactly one positive debit or credit | Database CheckConstraint on line | Journal-level debit equals credit remains multirow transactional check. |
| Period open, posting, source totals, receipt/invoice matching | Atomic application services and row locks where supported | SQLite lacks PostgreSQL row lock semantics; backend choice is an unresolved production dependency. |
| Return numbers and note links | Model constraints and unique fields | Some current number fields are globally unique while others also declare org-scoped uniqueness; resolve redundant/conflicting semantics. |
| Invoice allocation source XOR and positive amount | Database checks/constraints | Allocation sum versus invoice outstanding and transaction balance is a locked cross-row calculation. |
| Tax rate bounds/effective intervals and registration slot | Model/migration checks and conditional uniqueness | Partial index support depends on backend; test chosen backend before relying on it. |

## Target constraint policy

1. **Keys:** stable surrogate PK plus tenant-scoped natural keys for organization-owned identifiers, e.g. `(organization_id, code)`. Choose global vs tenant-scoped document numbering only after numbering policy decision. Preserve old values and reference them through dedicated aliases if a re-numbering is approved; never silently rewrite posted numbers.
2. **Tenant consistency:** validate `organization_id`/branch/FK combinations at service boundary and in DB when feasible. A robust relational approach duplicates tenant key on child and uses composite unique parent `(organization_id,id)` plus composite FK; ordinary Django FKs do not enforce same-tenant membership. Confirm Django/backend support or use triggers/RLS on chosen backend. An `organization_id` column alone does not provide isolation.
3. **Lifecycle:** status values should be enumerated, transitions recorded as events where history matters, posted financial and stock rows immutable; correction through reversal. Use `PROTECT` for referenced history and deactivation for master records.
4. **Arithmetic:** decimals only, with explicit scale per currency and unit. Add database checks for positive quantities, nonnegative prices/discounts, bounded percentage, and discount <= line gross where single-row expression permits. Use atomic service validation for aggregates and totals.
5. **Idempotency:** every posting/import/retry command needs a stable unique source key and database uniqueness. Avoid generic source strings where a typed FK or explicit source registry can be used.
6. **Concurrency:** lock/serialize at the aggregate or ledger key, then enforce a unique idempotency key and re-check balances in the same transaction. Select PostgreSQL or document SQLite single-writer constraints before concurrent posting is accepted.

## Decision register for Phase 2 approval

| Decision (owner) | Evidence / impact | Recommended default for approval | Status |
|---|---|---|---|
| Inventory opening quantity and valuation (Finance + Inventory) | Current design has weighted-average value by product/warehouse; opening can be explicitly valued; legacy value may be unknown. | Import quantity and value separately with source evidence; mark unknown value `is_valued=false`; reconcile quantities to immutable movements. No invented cost. | Unresolved policy; current capability partly exists. |
| Negative stock, backdating, reservations (Operations + Finance) | Sale checks stock, but strict rules across concurrent sales and future reservations are not approved. | Disallow negative available stock at posting, preserve explicit correction process; decide backdate cutoff and reservation model. | Unresolved. |
| GRNI and receipt/invoice matching (Finance + Purchasing) | Phase 2 inspection observed draft accrual models; they were unapplied and removed in Phase 4 after the owner explicitly reaffirmed no interim GRNI. Receipt-linked quantity matching remains implemented. | No receipt accrual entity or journal. Receipt value is operational reconciliation data until supplier invoice posts; any later policy change requires a new approved design. | No-GRNI decision resolved by explicit owner decision 2026-10-24; invoice tolerances, price variance, receipt-only returns and aging remain unresolved. |
| Number scope and reset rules (Finance + Operations) | Many current numbers globally unique; returns combine global and org uniqueness. | Tenant-scoped series with immutable org/branch/year/doctype context and atomic sequence allocation; confirm legal branch/GST requirements before adopting. | Unresolved; global current fields cannot just be relaxed without collision audit. |
| Currency, exchange rates and precision (Finance) | Organization has currency string; most amounts have 2dp and no FX ledger. | Approve INR-only initial scope or functional transaction/base currency with dated FX rates and realized/unrealized gain postings. | Unresolved. |
| Tax snapshots and filing boundary (Tax + Finance) | TaxTransaction stores line snapshots; screens are internal reports; no API routes expose a statutory integration. | Preserve posted snapshots; clarify internal compliance reports versus filing/portal integration, GST registration/branch model and amendment handling. | Unresolved. |
| Retention, deletion and privacy (Legal + Security) | AuditEvent is sparse; no full document attachment/retention model. | No delete of posted/audit evidence; set statutory retention, legal hold, and personal data anonymization policy before schema. | Unresolved. |
| Roles, branch scope, maker-checker (Product + Security + Finance) | Membership role is free text; User also carries optional org/branch. | Capability assignments scoped to org and optionally branch, least privilege and separate maker/checker for approved thresholds; define backfill/authority first. | Unresolved. |
| Interbranch accounting (Finance) | Branch fields exist, but transfer and journal balancing policy across branches is not finalized. | Require explicit source/destination branch and balanced interbranch clearing entries if legal accounting requires it. | Unresolved. |
| Fixed asset/depreciation policy (Finance) | Asset models exist with configurable method/rate/life. | Approve book versus tax depreciation, rounding, calendars, disposal, and effective-date changes. | Unresolved. |
| APIs, bank formats, external integration (Product + Security + Finance) | DRF installed, but root URLs expose no API routes; imports are files/manual flows. | Define versioning, authentication, idempotency, audit and supported bank formats before integration schema. | Unresolved. |
| Deployment database/concurrency (Architecture + Operations) | Settings point to SQLite file; PostgreSQL dependency exists but is not configured. | Select and provision supported production backend before final backend-specific constraints or concurrency claims. | Unresolved. |
| Scope of candidate modules (Product + business owners) | Phase 1 contains planned/unknown domains without approved business rules. | Explicitly include/exclude CRM, POS, manufacturing, payroll, projects, distribution/dealership and services. Model only approved scope. | Unresolved. |

## Cross-row rules that require atomic services

Balanced journal; invoice/header totals equal lines; invoice allocations <= source invoice remaining and settlement remaining; a receipt is not overinvoiced; delivered amounts do not exceed confirmed order quantities; return quantities do not exceed eligible source quantities; inventory quantity/value snapshots roll forward; period is open at posting; tax component sum equals total tax; and all linked records share tenant/branch context. For each operation: validate under transaction/lock, persist ledger and derived snapshots together, then verify reconciliation. If the backend cannot provide needed locking/isolation, limit concurrent writes or block release pending backend decision.

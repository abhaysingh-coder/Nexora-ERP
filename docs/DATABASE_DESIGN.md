# Nexora ERP â€” Phase 2 Database Design

**Status:** Proposed design; no schema or database change is authorized by this document.  
**Basis:** repository models and migrations, settings/URLs, and the Phase 1 documents.  
**Scope:** database design for the Phase 1 feature set only. Unapproved ERP domains remain decisions, not implicit schema scope.

## 1. Design principles

1. Keep posted business events as the audit trail. Correct posted sales, purchase, stock, tax, bank, and journal records by reversal/correction workflows, never silent destructive edits.
2. Scope every business record to an Organization, and where relevant a Branch. Enforce that referenced parties, products, accounts, warehouses, documents, and branch belong to that same scope. Existing FKs alone do not guarantee this.
3. Store monetary amounts as fixed-precision decimals in the organization currency. The code uses mostly `DecimalField(18,2)`; quantities commonly use `(18,3)`; cost snapshots use six decimal places. These scales are existing implementation details, not a decision on foreign currency or rounding policy.
4. Treat source ledgers as authoritative. Denormalized balances and document totals are caches/snapshots and must reconcile to their sources.
5. Preserve historical tax and inventory cost facts on posted rows. A later master-data change must not rewrite a posted transaction's tax or valuation.
6. Add constraints in stages after profiling legacy data; never assume current data meets a new invariant.

## 2. Existing implementation versus target design

| Area | Repository evidence | Design status |
|---|---|---|
| Organization, branch, membership, user | `organizations` and `core` models | Existing partial foundation; membership role is free text and User also has optional organization/branch fields. |
| Sales and purchase documents | `sales` and `purchases` models | Existing partial workflows, including orders, delivery/receipt, invoices and posting. |
| Stock and valuation | `inventory` models | Existing movement ledger and weighted-average valuation by product/warehouse, plus cached position. |
| Accounting | `accounting` models | Existing account, journal, period, year-end, budget and fixed-asset models. |
| Banking and tax | `banking`, `taxation` models | Existing internal transactions, allocations/reconciliation and tax configuration/snapshots. They do not imply bank connectivity or statutory filing. |
| Returns | `returns` models | Existing returns/credit/debit notes; return workflow states need one agreed vocabulary. |
| Roles, contacts, units, audit coverage | partial or absent | Proposed foundation only; exact capability rules and coverage require decisions. |
| CRM, POS, manufacturing, payroll, projects, API | no complete implementation evidenced | Deferred scope; do not model until Phase 2 business decisions approve it. |

Phase 1 IDs are the traceability keys: IAM-01/02, ORG-01, MST-01..05, SAL-03..07, PUR-03..07, INV-01..04, ACC-01..05, BNK-01..02, GST-01..04, SAL-07/PUR-07, RPT-01, AUD-01/OPS-01, API-01. Confirm exact feature status/acceptance details in `FEATURE_MATRIX.md` and `PHASE_1_REPORT.md`.

## 3. Proposed logical ownership

- **Identity and tenant:** Organization â†’ Branch; User â†” Organization through Membership. Target membership is the canonical org/role assignment. Keep current User organization/branch fields until their data is profiled and a backfill/cutover is approved. Never let two fields silently compete as the access-control source.
- **Master data:** Customer, Supplier, Product, Warehouse, Account, TaxCode and classification belong to one Organization. Existing Customer/Supplier records are embedded in transaction apps; a normalized Party/Address/UOM design is a proposed migration, not present schema.
- **Operational documents:** header owns organization/branch, date, stable number, status and lifecycle metadata; lines own product/service, quantity, rate/discount, tax snapshot and source-line links. Posted header and lines are immutable except through explicit correction/reversal.
- **Inventory:** StockMovement records quantity movement. InventoryValuationEntry records immutable quantity/value snapshots and points to its balance; InventoryValuationBalance is a derived current position. Weighted average is currently calculated per product and warehouse. Legacy stock without cost evidence remains explicitly unvalued; do not manufacture a cost.
- **Accounting:** JournalEntryLine is the posting detail. Journal and account current balances are derived/cached. Posting, balanced journal, open-period check, and associated source-document updates must commit atomically.
- **Banking:** BankTransaction and InvoiceAllocation represent internal settlement/allocation. Statement import rows and reconciliation events are separate evidence. CustomerReceipt is a legacy parallel sales record; migration/retirement must reconcile it with banking before any merge.
- **Tax:** TaxRate/config is effective-dated input. TaxTransaction stores the calculation snapshot per posted source line. Current generic `source_model`/integer source IDs are not referential FKs and can orphan; a typed source relationship or registry is a future design decision.
- **Audit/documents:** AuditEvent is sparse generic logging, not complete immutable audit history. A future audit record should carry tenant, actor, timestamp, event/action, object identity, correlation, and safe before/after metadata. Attachment/document model and retention rules are not yet approved.

## 4. Integrity model

### Database-enforceable invariants

Use FK `PROTECT` for posted/financial history and restrictive deletion of masters once referenced. Use database uniqueness/check constraints for nonempty key collisions, positive quantities/amounts, valid ranges, one-to-one relationships, and conditional uniqueness where backend support is confirmed. Candidate tenant-safe unique keys include `(organization, code)` and `(organization, document_number)` after legacy collision analysis.

### Transactional application invariants

The following span rows and cannot be expressed as an ordinary row-level CHECK: journal debit equals credit; document totals equal line aggregates; stock cannot go below zero when that policy is selected; allocations cannot exceed invoice/transaction availability; receipt/invoice quantities cannot overmatch; tax split and GST place-of-supply calculations; period-open checks; and matching organization/branch across related FKs. Enforce them in atomic services and add database-level protections where supported and justified. SQLite does not provide PostgreSQL row-lock semantics; `select_for_update()` is not equivalent concurrency protection there.

### Tenant isolation

Current child rows may have `organization_id` plus independent FKs to Customer/Product/Branch/etc. A valid FK does not prove both organizations match. Reject cross-tenant references in every write service and scope all reads by authorized tenant. Before production, choose a database strategy: composite tenant-aware FKs through unique `(organization_id,id)` keys and matching duplicated scope columns, or database triggers/row-level security if the selected backend supports it. ORM filtering alone is not a database integrity guarantee.

## 5. Authority, derived values, and reconciliation

| Stored value | Authority | Required reconciliation |
|---|---|---|
| Product `stock_quantity` | StockMovement quantities (warehouse level) | Sum movement quantities to product and warehouse position; investigate legacy aggregate-only balances. |
| InventoryValuationBalance quantity/value | InventoryValuationEntry snapshots | Rebuild/replay comparison and explicit `is_valued`; never treat unvalued as zero-cost. |
| Customer/Supplier outstanding balance | Posted invoices minus credits/allocations/payments per agreed rules | Recalculate from source records; CustomerReceipt legacy records need reconciliation. |
| Account/BankAccount current balance | Posted journal or bank transactions and opening entries | Reconcile cached balance to ledger; define opening balance conversion. |
| Invoice/order/return totals/status | Posted line snapshots and lifecycle events | Validate aggregate to header totals before posting and in periodic integrity reports. |
| TaxTransaction | Posted source-line snapshot | Compare source identity and totals; generic integer references can orphan. |

## 6. Deletion and retention

Masters may be deactivated, not deleted, once referenced. Draft-only documents may be deletable under explicit policy; posted financial, tax, inventory and audit records should be retained and corrected by reversal. Retention periods, legal holds, personal data erasure/anonymization, attachment storage, and audit immutability need jurisdictional approval; no duration is assumed here.

## 7. Open decisions blocking final physical schema

Resolve currency/multi-currency and precision; legal entity/GST registration versus organization/branch boundaries; fiscal year and document numbering scopes; account chart and opening balances; role/capability and maker-checker permissions; negative stock and backdating; UOM conversion, batch/serial/bin/reservation/count needs; GRNI and receipt matching; landed cost and purchase variances; cash flow definition; depreciation policy; tax filing versus internal reports; retention/attachments; bank/API formats; and deployment database. Recommendations and owners are tracked in `DATABASE_CONSTRAINTS.md` and `PHASE_2_REPORT.md`.

## 8. Dependencies and implementation boundary

Current settings use SQLite `db.sqlite3`; PostgreSQL packages are present but no configured PostgreSQL connection or row-level security is evidenced. The physical design must be revisited after backend and deployment are selected. This phase creates documentation only: no model edits, migration files, migration execution, database introspection, or data conversion.

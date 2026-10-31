# Nexora ERP â€” Data Dictionary

This is a design inventory from the checked-in model definitions, not a live database catalog. Django implicit `id` primary keys, `created_at`/`updated_at`, choices, and model-specific fields are omitted from the short summaries unless material. Confirm exact migrations and deployed database before coding. **Current** = model exists; **Proposed** = not implemented.

## Shared conventions

- FK means foreign key; 1:1 means one-to-one; nullable fields allow no value. Most document amounts currently use fixed precision `(18,2)`, quantities `(18,3)`, percentages/rates vary; specific values below are called out where verified.
- `organization` is the tenant key. Child tenant consistency must be validated; an independent FK does not enforce matching tenant ownership.
- Common status fields are strings/choices, not a workflow engine. Historical status wording differs between models.

## Identity and organization â€” current (IAM-01/02, ORG-01)

| Entity | Key fields and relationships | Integrity / notes |
|---|---|---|
| `core.User` | Django AbstractUser fields; unique email; nullable organization FK and branch FK | Userâ€™s org/branch is a second membership context; authority and transition are unresolved. |
| `organizations.Organization` | name, globally unique slug, currency (default INR), GSTIN, address, active, timestamps | Currency is a short string; no currency table or multi-currency ledger. |
| `organizations.Branch` | organization FK, name, code, address, active | `(organization, code)` unique. |
| `organizations.OrganizationMembership` | user FK, organization FK, free-text role default `staff`, active | `(user, organization)` unique; no normalized role/capability schema. |
| `core.AuditEvent` | nullable user/org FKs, action, details, created_at | Generic and sparse; not complete immutable audit coverage. |

## Master data â€” current partial and proposed (MST-01..05)

| Entity | Key fields and relationships | Notes |
|---|---|---|
| `sales.Customer` | organization FK; name, GSTIN, state code, phone, email, outstanding balance | Address is not normalized; outstanding is cached/recalculated. |
| `purchases.Supplier` | organization FK; identity/contact/GST fields and outstanding balance | Embedded in purchases; profile fields need model-level confirmation before exact migration design. |
| `inventory.Product` | organization FK; globally unique SKU, name, category, unit string, cost/sales price, reorder level, stock quantity, active | Quantity is 3dp; cost/price 2dp; stock_quantity is a legacy aggregate/cache. |
| `inventory.Warehouse` | organization and branch FKs, name/code, active | `(organization, code)` unique; branch must belong to same organization. |
| Proposed `Party` / `PartyAddress` | organization, party kind, legal/display names, identifiers, contact and address components | Replaces/normalizes customer/supplier only after duplicate and data ownership analysis. Candidate MST-01..05. |
| Proposed `UnitOfMeasure` / conversion | organization or shared code, symbol, dimension, conversion factor/effective dates | Unit conversion policy unresolved; current unit is a product string. |
| Proposed service/tax/pricelist masters | org-scoped service/item, tax classification, price list and effective prices | Candidate only; approval and pricing precedence needed. |

## Sales â€” current (SAL-03..07)

| Entity | Main fields / links | Important notes |
|---|---|---|
| `SalesOrder` | organization, optional branch, customer, order number/date/status; has lines | Number currently globally unique; draft/confirmed/fulfilled/cancelled. |
| `SalesOrderLine` | order, product, quantity, unit price, discount | Positive quantity/nonnegative price checks are present; product tenant check in confirmation service. |
| `DeliveryNote` | organization/branch, warehouse, sales order, delivery number/date/status/creator | Warehouse/order/branch tenant relationships require validation. |
| `DeliveryNoteLine` | delivery, order line, quantity, discount | unique delivery/order-line; positive quantity check. |
| `SalesInvoice` | organization/branch, customer, optional 1:1 delivery note; invoice number/date/place of supply/due date/status; subtotal/tax/total | Number globally unique; totals stored and validated at posting. |
| `SalesInvoiceLine` | invoice, product, quantity, unit cost, unit price, tax rate, discount, line total | Tax snapshot is stored separately in TaxTransaction on posting. |
| `CustomerReceipt` | organization/branch, customer, receipt number/date/amount/method/status | Legacy separate from banking InvoiceAllocation; number global. |

## Purchasing â€” current (PUR-03..07)

| Entity | Main fields / links | Important notes |
|---|---|---|
| `Supplier` | org-scoped supplier identity and contact/tax data, cached outstanding | See master-data caveat. |
| `PurchaseOrder`, `PurchaseOrderLine` | organization/branch/supplier, order identity/date/status; product, quantity, unit cost, discount | Order and line business rules are service/model logic. |
| `GoodsReceipt`, `GoodsReceiptLine` | organization/branch, warehouse, purchase order; order line and received quantity | Receipt lifecycle and overreceipt constraints must be preserved. |
| `PurchaseInvoice`, `PurchaseInvoiceLine` | organization/branch/supplier, invoice identity/date/status/totals; product, optional receipt/order line, qty/cost/tax/discount/total | Posting validates tenant and receipt matches, updates inventory valuation, journals and tax snapshot. |

## Inventory â€” current (INV-01..04)

| Entity | Main fields / links | Important notes |
|---|---|---|
| `StockMovement` | product, warehouse, signed quantity, movement type, generic source model/id, unit cost (24,6), created_at | Immutable via model manager/save; source pointer is not FK. |
| `InventoryValuationBalance` | organization, product, warehouse, quantity_on_hand (18,3), inventory_value (24,2), is_valued, valuation_start_date, updated_at | unique organization/product/warehouse; nonnegative value check. Derived current position. |
| `InventoryValuationEntry` | organization/product/warehouse/balance; optional 1:1 stock movement; event/source/effective time; quantity/value change and after snapshot; unit cost (30,6); valued flag/user | Immutable history; supports movement/opening/revaluation. |
| `StockTransfer` | source/destination warehouse with product/quantity and transfer state | Exact lifecycle/field inventory should be confirmed directly in model before any implementation design. |

## Accounting â€” current (ACC-01..05)

| Entity | Main fields / links | Notes |
|---|---|---|
| `Account` | organization, optional branch, code/name/type, self-parent, current_balance, active | unique org/code; cached balance. |
| `JournalEntry`, `JournalEntryLine` | org/branch, globally unique reference, date/narration/status/creator; entry/account, debit/credit (18,2) | Check constraint requires exactly one positive side per line; journal balance across lines is transactional logic. |
| `FinancialYear`, `AccountingPeriod`, `PeriodAuditEvent` | org/year/date bounds/status and event evidence | Period overlap/open state rules exist in application code and migrations. |
| `YearEndClosing`, `YearEndClosingJournal`, `YearEndClosingEvent` | org/year closing state, generated journal, lifecycle event | Preserve generated posting and idempotency semantics. |
| `AccountingBudget` | org/branch/account/period and budget amount | Budget policy and dimension scope remain a decision. |
| `FixedAssetCategory`, `FixedAsset` | org/category/accounts; depreciation method/rate (6,3)/life; asset code/name/date/cost/salvage and journals | Category unique org/name; asset code unique org/code; positive cost/life checks. |
| `FixedAssetDepreciation`, `FixedAssetEvent` | asset/period, amount, optional journal, actor and event details | unique asset/period; depreciation method and statutory policy unresolved. |

## Banking â€” current (BNK-01..02)

| Entity | Main fields / links | Notes |
|---|---|---|
| `BankAccount` | org/optional branch, name/type, globally unique account number, opening/current balance, active | current balance cached; global uniqueness may be wrong for tenant scope. |
| `BankTransaction` | org/branch/account, related account, optional customer/supplier, type, globally unique reference, date/amount/method/settlement mode/status/reconciled/creator | No external bank connection implied. |
| `BankStatementImport`, `BankStatementLine` | account/import metadata/status/count; date/reference/amount/description/status, optional 1:1 matched transaction | Duplicate and idempotency rules are partial; import format decision pending. |
| `BankReconciliationEvent` | statement line, transaction, action, actor/reason/time | Event history for matching changes. |
| `InvoiceAllocation` | bank transaction, optional sales or purchase invoice, amount, journal, creator, reversal fields | Check amount positive and exactly one invoice source; allocation total is cross-row logic. |

## Tax and returns â€” current (GST-01..04, SAL-07/PUR-07)

| Entity | Main fields / links | Notes |
|---|---|---|
| `TaxCode`, `TaxRate` | org/code/name/category; effective-dated rate/component/state | Rate bounds and effective-date/uniqueness constraints in models/migrations. |
| `TaxRegistration` | org/optional branch, GSTIN/state and registration/reverse charge flags | Conditional unique registration slot. |
| `ProductTaxClassification` | org/product/tax code, HSN/SAC, tax and ITC flags | unique organization/product. |
| `TaxTransaction` | org/branch, kind/date, generic source model/id/line id; tax code, HSN/SAC, origin/place of supply; taxable value/rates/component amounts, total/invoice amount, flags | Immutable historical calculation intent; generic source IDs lack FK. Internal reports are not statutory filing or government integration. |
| `CustomerReturn`, `CustomerReturnLine` | org/branch, invoice, number/date/status/warehouse/creator; invoice line, quantity, reason/condition/restock and tax/credit/cost snapshots | Existing number uniqueness has both global and org-scoped definitions in code; resolve before number redesign. |
| `SalesCreditNote`, `SalesCreditNoteLine` | org/branch/customer/original invoice/return, number/date/totals/status/journal; return-line snapshot details | Preserve source and tax snapshots. |
| `SupplierReturn`, `SupplierReturnLine` | org/branch/supplier, exactly one eligible source among receipt/invoice, warehouse/number/date/status; source line and qty/tax/amount | Verify line source XOR and source-scope invariants before migration. |
| `SupplierDebitNote`, `SupplierDebitNoteLine` | org/branch/supplier/return, number/date/totals/status/journal; return-line snapshot | Number and lifecycle rules require consistency review. |

## Proposed foundation and deferred domains

- **Proposed authorization:** normalized Role, Capability/Permission, OrganizationMembership assignment, optional Branch scope and effective dates (IAM-02). Do not infer permissions from free-text role values.
- **Proposed audit/document:** immutable business event and metadata/attachment references (AUD-01/OPS-01), with retention, privacy, storage and legal hold policy TBD.
- **Proposed missing Phase 1 business records:** inquiry/quotation, requisition, cash-flow projection, approval inbox and formal stock count/reservation/batch/serial/bin require explicit scope decisions before field-level schema.
- **Deferred candidate domains:** POS, CRM, manufacturing, payroll, projects and distribution have no approved data dictionary here. Their required entities depend on Phase 1 sign-off.

## Source map

See `DATABASE_DESIGN.md` for authority/reconciliation rules, `DATABASE_CONSTRAINTS.md` for enforceability, and `ENTITY_RELATIONSHIP_DIAGRAM.md` for relationships. Feature acceptance remains in Phase 1's `FEATURE_MATRIX.md` and `SCREEN_INVENTORY.md`.

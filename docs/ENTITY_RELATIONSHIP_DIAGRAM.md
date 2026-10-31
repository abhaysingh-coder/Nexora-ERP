# Nexora ERP â€” Entity Relationship Diagram

**Current** means an entity exists in repository models. **Proposed** means a design target only. This diagram shows major Phase 1 relationships, not every field or every historical event. It is not an implemented schema.

```mermaid
flowchart LR
  classDef current fill:#d9ead3,stroke:#38761d,color:#111
  classDef proposed fill:#fff2cc,stroke:#bf9000,color:#111

  Org[Organization] -->|1 to many| Branch[Branch]
  User[User] -->|many to many through| Membership[OrganizationMembership]
  Org --> Membership
  Org -->|1 to many| Customer[Customer]
  Org --> Supplier[Supplier]
  Org --> Product[Product]
  Org --> Warehouse[Warehouse]
  Branch --> Warehouse

  Customer --> SalesOrder[SalesOrder]
  SalesOrder --> SalesOrderLine[SalesOrderLine]
  SalesOrder --> Delivery[DeliveryNote]
  Delivery --> DeliveryLine[DeliveryNoteLine]
  Customer --> SalesInvoice[SalesInvoice]
  Delivery --> SalesInvoice
  SalesInvoice --> SalesLine[SalesInvoiceLine]
  SalesInvoice --> SalesReturn[CustomerReturn]
  SalesReturn --> SalesReturnLine[CustomerReturnLine]
  SalesReturn --> Credit[SalesCreditNote]
  Credit --> CreditLine[SalesCreditNoteLine]

  Supplier --> PurchaseOrder[PurchaseOrder]
  PurchaseOrder --> POLine[PurchaseOrderLine]
  PurchaseOrder --> Receipt[GoodsReceipt]
  Receipt --> ReceiptLine[GoodsReceiptLine]
  Supplier --> PurchaseInvoice[PurchaseInvoice]
  Receipt --> PurchaseInvoice
  PurchaseInvoice --> PILine[PurchaseInvoiceLine]
  Supplier --> SupplierReturn[SupplierReturn]
  SupplierReturn --> SupplierReturnLine[SupplierReturnLine]
  SupplierReturn --> Debit[SupplierDebitNote]
  Debit --> DebitLine[SupplierDebitNoteLine]

  Product --> Stock[StockMovement]
  Warehouse --> Stock
  Stock --> ValEntry[InventoryValuationEntry]
  Product --> ValBalance[InventoryValuationBalance]
  Warehouse --> ValBalance
  ValBalance --> ValEntry

  Org --> Account[Account]
  Account --> JournalLine[JournalEntryLine]
  Journal[JournalEntry] --> JournalLine
  Org --> Fiscal[FinancialYear / AccountingPeriod]
  Org --> Asset[FixedAsset]
  Asset --> Depreciation[FixedAssetDepreciation]

  Bank[BankAccount] --> BankTxn[BankTransaction]
  BankTxn --> Allocation[InvoiceAllocation]
  SalesInvoice --> Allocation
  PurchaseInvoice --> Allocation
  Bank --> Import[BankStatementImport]
  Import --> StatementLine[BankStatementLine]
  StatementLine --> Recon[BankReconciliationEvent]
  BankTxn --> Recon

  Org --> TaxCode[TaxCode / TaxRate]
  Product --> Classification[ProductTaxClassification]
  TaxCode --> Classification
  SalesLine -. snapshot reference .-> TaxTxn[TaxTransaction]
  PILine -. snapshot reference .-> TaxTxn

  Party[Party / Address]:::proposed
  Role[Role / Capability / Assignment]:::proposed
  UOM[Unit / Conversion]:::proposed
  Doc[Attachment / Document]:::proposed
  Audit[Complete immutable audit event]:::proposed

  class Org,Branch,User,Membership,Customer,Supplier,Product,Warehouse,SalesOrder,SalesOrderLine,Delivery,DeliveryLine,SalesInvoice,SalesLine,SalesReturn,SalesReturnLine,Credit,CreditLine,PurchaseOrder,POLine,Receipt,ReceiptLine,GRNI,PurchaseInvoice,PILine,SupplierReturn,SupplierReturnLine,Debit,DebitLine,Stock,ValEntry,ValBalance,Account,Journal,JournalLine,Fiscal,Asset,Depreciation,Bank,BankTxn,Allocation,Import,StatementLine,Recon,TaxCode,Classification,TaxTxn current
  class Party,Role,UOM,Doc,Audit proposed
```

## Cardinality and traceability notes

- Each operational document belongs to one organization; it may have a branch. The database does not consistently prove referenced rows share that organization, as discussed in `DATABASE_DESIGN.md`.
- Header-to-line is one-to-many. Current constraints differ by document; verify a specific model before relying on a uniqueness rule.
- SalesInvoice may reference a DeliveryNote one-to-one; SalesCreditNote may reference a CustomerReturn one-to-one. PurchaseInvoice lines can optionally reference receipt and order lines.
- InventoryValuationEntry has an optional one-to-one StockMovement link; opening and revaluation events need not have one. Generic source model/id is not an FK.
- InvoiceAllocation has one bank transaction and exactly one optional source invoice in current constraints. A bank transaction can have multiple allocations.
- TaxTransaction source link is drawn dotted because it is a generic string/integer pair rather than a relational FK.
- `CustomerReceipt` is an existing legacy entity outside the banking allocation path; reconcile its semantics before merging or retiring it.
- Proposed Party/Address, Role/Capability, UOM, Document and complete audit entities map to MST-01..05, IAM-02, OPS-01, AUD-01 and inventory/purchase/sales requirements. Their inclusion and scope must be approved before schema work.
- CRM, POS, manufacturing, payroll, projects, and other Phase 1 candidate features are intentionally not diagrammed as approved entities; see `PHASE_1_REPORT.md` for unresolved scope.

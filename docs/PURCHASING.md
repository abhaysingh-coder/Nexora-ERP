# Purchasing workflow

Nexora's purchasing screens are available under **Purchases** and **Purchase orders**.

## Purchase orders

Users with an active organization membership in an owner, admin, purchasing or finance role can create a draft order. Branch users are restricted to their assigned branch; organization-level users select an active branch. Supplier and product choices are limited to the active organization.

Draft orders can be revised before confirmation. Confirmation requires at least one valid line and an open accounting period for the order date. Confirmed orders can be cancelled only before a posted receipt exists. Confirmed quantities and prices are protected through supported model and bulk ORM operations.

## Goods receipts

A confirmed or partially received order can have one or more draft goods receipts. Each receipt uses an active warehouse in the order's branch and records quantities against order lines. Receipt dates cannot precede the order date, and cumulative posted receipt quantities cannot exceed ordered quantities.

Posting a receipt runs in a database transaction, validates the order, branch, warehouse, period and quantities, then creates warehouse-specific stock movements through the perpetual weighted-average costing service. Repeated product lines are consolidated into one movement at their quantity-weighted purchase cost. Repeating a post does not create another movement. The order status becomes partially received or received according to posted quantities. Posted receipt headers and lines reject supported edits and deletions.

## Supplier invoices

An invoice can be drafted from a posted receipt. The form defaults to the receipt products and purchase-order costs while allowing the supplier invoice quantity, unit cost, discount and tax place of supply to be entered. Quantities are capped at the received quantity not already covered by posted invoices. Invoice posting rechecks this limit while holding database locks.

The existing purchase invoice posting service handles the tax snapshot, accounting journal, payable balance and supplier outstanding balance. A receipt-linked invoice does not create a second stock movement. When its actual net cost differs from the purchase-order receipt basis, the difference is allocated between the current inventory value and COGS for the presumed consumed share. Recoverable input tax is excluded from inventory cost; non-creditable purchase tax is capitalized. Direct purchase invoices create the stock receipt and valuation at invoice cost.

## Supplier returns and debit notes

A posted goods receipt can be returned for quantities that remain unbilled. Quantities already billed must be returned from the posted supplier invoice, where the debit note reverses the original payable and stored purchase tax snapshot. Both screens account for previous posted returns; posting checks quantities and warehouse stock again under locks. The return movement removes value at the warehouse's current weighted-average cost, and the journal records any difference from the supplier credit in designated inventory adjustment gain/loss accounts.

The debit note and journal preserve the source receipt/invoice. Receipt-only returns record a supplier credit against accounts payable; their inventory credit uses current weighted-average cost. Invoiced returns reverse eligible GST input and reverse-charge liability using the source invoice's tax snapshot. A receipt-only supplier credit is reflected in the aggregate supplier balance and is not yet allocated to a particular invoice or payment.

## Scope and limitations

Per the explicit owner decision reaffirming Phase 3, goods receipt posting updates stock quantity/valuation but creates no interim GRNI journal or receipt accrual row. The valuation report adds unbilled receipt value when comparing the valuation subledger with raw inventory GL. Supplier invoice price variances are allocated using current on-hand quantity relative to the invoice quantity because weighted-average costing does not retain receipt layers; unusual mixes of receipts, returns and issues require finance review. These are application-layer organization and branch checks. They do not protect the database from direct SQL, database administrators or external processes. SQLite is used for local tests and does not provide PostgreSQL-equivalent row locks; production concurrency behavior still needs PostgreSQL testing.

Purchase requisitions, approval limits, landed-cost allocation, and FIFO valuation are not implemented. Perpetual weighted-average costing is implemented; legacy stock without complete valuation history remains unvalued until an authorized opening valuation is recorded. Return/costing migrations are `inventory.0004` through `inventory.0006` and `returns.0001`; apply them only to the intended business database after reviewing the migration plan and verifying a backup. The test suite applies migrations to its temporary test database only.

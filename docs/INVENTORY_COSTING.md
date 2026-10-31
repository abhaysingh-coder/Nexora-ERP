# Inventory valuation and costing design

## Audit findings and compatibility assumptions

- `StockMovement` is the existing quantity ledger. Warehouse balances are sums of signed movement quantities; `Product.stock_quantity` is a legacy organization-level aggregate.
- Movements store a two-decimal `unit_cost` snapshot, but the existing code does not maintain a stock value or costing method. Receipt costs come from purchase-order lines, direct purchase invoice costs come from invoice lines, and sales costs have been taken from invoice/order/product cost snapshots rather than a moving warehouse cost.
- Goods receipts and sales deliveries move stock before their linked supplier or sales invoices. Linked invoices avoid a second quantity movement. Per the explicit owner decision reaffirming Phase 3, goods receipt posting creates no GRNI journal or receipt accrual rows; supplier invoice posting records purchase accounting. The valuation reconciliation adds still-unbilled receipt value to raw GL inventory for comparison with the stock valuation. Sales delivery cost is recognized when its linked sales invoice posts.
- Transfers use a pair of movements but currently assign zero cost. Positive manual adjustments use `Product.cost_price` implicitly. Legacy product quantities can be unallocated to any warehouse.
- Returns use source invoice/receipt snapshots for the note and, in some cases, the stock movement cost. There is no complete historical valuation trail to rebuild reliable weighted average from legacy movements.
- Existing posted journals and movement rows must remain unchanged. Valuation starts prospectively; legacy quantities without an explicitly approved opening value remain visible as unvalued.

## Selected method

Use perpetual weighted-average costing independently for each organization, product, and warehouse. A receipt or positive adjustment adds its explicit cost value to the current inventory value; the average is the resulting value divided by resulting quantity. Outgoing movements consume the current warehouse average. The last movement that takes a balance to zero consumes the remaining value exactly, avoiding residual cents.

Quantities retain three decimal places. Inventory value and accounting amounts use two decimal places with `ROUND_HALF_UP`; average unit cost is kept to six decimal places for movement snapshots. The currency residual stays in the warehouse value and is cleared on the final issue. A known zero-value opening quantity is supported. Negative inventory is rejected.

For a receipt-linked supplier invoice with a different net cost from the purchase-order receipt basis, the invoice posting updates inventory value for the presumed quantity still on hand. The remaining price difference is recognized in COGS. Since weighted-average costing does not retain receipt layers, the allocation estimates remaining receipt quantity as the lesser of the current warehouse quantity and the invoice quantity. Review the reconciliation exception when stock from multiple receipts, returns or issues is mixed. Non-creditable purchase tax is capitalized; recoverable input tax is excluded.

## Legacy and opening valuation

The schema migration records the current per-warehouse movement quantity as an unvalued balance; it does not infer value from historical `unit_cost` snapshots or change old movements or journals. A controlled opening-valuation action records an authorized explicit value against the current ledger quantity. An organization-wide aggregate quantity that has no warehouse allocation is reported as unallocated and must be allocated explicitly with a valued opening entry before it can be costed or issued. The opening action does not create an accounting journal; compare its value to the inventory control account and post a separately approved adjustment if needed.

History before the valuation start date is reported as incomplete. No migration rewrites old documents, journals, or movements. The chosen method is fixed to weighted average for all new valuation positions; changing method later requires an approved migration and reconciliation procedure.

## Accounting and operational notes

Stock movements are costed in the same database transaction as the existing posting operation. Linked delivery and receipt invoices reuse source movement valuation and do not create a second quantity change. Inventory journal entries are made by the relevant business posting workflow; transfers move the same value between warehouse subledgers and do not create company profit or loss. Stock adjustment journals use designated inventory gain/loss accounts. The valuation report displays uninvoiced receipt value, receipt-only returns, deliveries awaiting invoice, raw GL inventory balance, adjusted balance and residual difference. The report's unbilled-receipt adjustment is consistent with the approved no-GRNI receipt behavior.

The report's **as-of date is the posting date**, based on immutable movement and valuation event timestamps. Backdated accounting dates do not rewrite the weighted-average event sequence. Before the first valuation snapshot for a position, historical value is shown as unknown. The inventory control account reconciliation is incomplete whenever a position is unvalued or a legacy quantity is unallocated.

SQLite does not provide PostgreSQL-equivalent row locks. Posting locks the product and valuation position where supported, while database uniqueness constraints protect duplicate events. Production concurrency must be exercised against PostgreSQL before relying on concurrent posting guarantees.

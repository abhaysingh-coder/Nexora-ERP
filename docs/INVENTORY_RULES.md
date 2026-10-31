# Nexora ERP — Inventory Rules

Current behavior is grounded in `inventory/models.py`, `inventory/valuation.py`, `purchases/models.py`, `sales/models.py`, return services and `docs/INVENTORY_COSTING.md`. Proposed acceptance criteria are not test results. No inventory behavior changed in this phase.

## Decision baseline

| Rule | Classification | Consequence |
|---|---|---|
| Perpetual weighted average per organization/product/warehouse | Approved by existing design/code evidence | Do not add FIFO or share average across warehouses without Finance + Inventory approval and data conversion plan. |
| Quantity 3 decimals; value 2 decimals `ROUND_HALF_UP`; unit-cost snapshot 6 decimals | Approved compatibility behavior | Currency/UOM-specific precision remains blocked if multi-currency/conversions are approved. |
| Negative stock rejected by costing service | Approved existing behavior | Exception/backorder policy is blocked; don't weaken check. |
| Legacy quantity without evidence remains unvalued | Approved by Phase 2 design | Never infer cost from product cost price or historic movement unit_cost where continuity is incomplete. |
| Opening valuation action does not post a GL journal | Approved documented current behavior | Reconcile separately; Finance approval required for any automatic opening journal. |
| Backdated transaction ordering | Existing valuation follows posting/event sequence; as-of report date uses posting date | Exact effective-time vs accounting-date policy is unresolved; changing it can rewrite downstream average and GL. |
| Reservations, batches, serials, bins, expiry, UOM conversion, formal stock count | Blocked/not implemented | No availability or traceability algorithm should assume these exist. |

## Authoritative records and available quantity

- `StockMovement` is the quantity event ledger; signed quantities aggregate per product and warehouse.
- `InventoryValuationEntry` is the immutable valuation event with before/after snapshot, quantity/value change, unit-cost snapshot and source identity.
- `InventoryValuationBalance` is a current materialized position, unique by organization/product/warehouse. It must equal replayed valuation entries. `Product.stock_quantity` is a legacy organization-level quantity projection, not authority for branch warehouse availability.
- `is_valued=false` means quantity exists but cost history/value is not supported. It is not equivalent to a proven zero-cost stock position.
- Until reservation policy exists, “available” in new algorithm design means on-hand movement quantity only. Existing UI/dashboard calculations differ; no common low-stock or availability formula is approved yet.

## Cost calculations

Let `Q` be current warehouse quantity, `V` current valued inventory amount, `A = V/Q` average unit cost (six decimals). For a receipt with positive quantity `q` and explicit unit cost `c`, set `V' = round_currency(V + q*c)`, `Q'=Q+q`, and average `A'=V'/Q'` at six decimals. For an issue quantity `q` (positive magnitude), normal consumed value is `round_currency(q*A)`; if issue empties the position, consume exact remaining `V` to clear residual cents. Reject if `q > Q`.

These formulas apply only to valued positions with valid evidence. For a new known-zero-cost quantity, value remains zero only when explicit approval/source says zero; absence of cost is unknown, not zero. Currency amount uses decimal, not binary float. Avoid cumulative per-line rounding drift by follow existing consolidated movement service; reconcile total line value to journal.

## Workflow-specific effects

### Purchase receipt

- Only confirmed/partially received order; active warehouse belongs to order branch/organization; receipt date not earlier than order date and period open.
- Posted received quantity per order line plus current receipt <= ordered quantity. Consolidate same product/warehouse and use PO line unit cost weighted by received quantities (current behavior).
- Atomic receipt status + one movement per consolidated key + valuation balance/entry + order received/partial status. Retry must not repeat quantity/value.
- No interim GRNI journal or receipt-accrual entity. Per the explicit owner decision of 2026-10-24, the valuation-to-GL report treats uninvoiced receipt inventory as a reconciling item. Receipt-linked quantity matching remains active; invoice tolerances and price-variance policy remain separate unresolved decisions.

### Supplier invoice

- Direct invoice creates quantity and value at invoice net cost plus non-creditable tax; recoverable input tax is outside inventory cost.
- Receipt-linked invoice changes no stock quantity. It compares actual invoice cost with receipt/PO basis and revalues currently retained amount, allocating remaining estimated variance to COGS. This is an approximation because weighted-average pool is not a receipt layer.
- If stock history is mixed (other receipts, returns/issues), flag exception for finance review. Exact variance/tolerance and approval policy is blocked.

### Sales dispatch/invoice

- Dispatch consumes available warehouse quantity and weighted-average value once; delivery-linked invoice reuses movement valuation for COGS.
- Direct invoice (without delivery) consumes from chosen eligible warehouse at current warehouse average in same post transaction.
- COGS and inventory GL must match stock value decrease, with explicit rounding residual handling. Closed period or insufficient stock rejects entire posting.

### Returns

- Customer restock uses original sale movement cost snapshot; if absent, fail restocking instead of guessing. Non-restockable return produces credit but no sellable quantity/COGS reversal.
- Supplier return removes current warehouse average cost; supplier credit/tax comes from source invoice snapshot when invoiced; configured adjustment account balances difference. Receipt-only return has no GRNI reversal.
- Posted source documents stay unchanged; return adds correction movements and note/journal snapshots once.

### Transfer and adjustment

- Intra-organization transfer requires source sufficient stock and active destination. Source value removed equals destination value added; paired entries atomically conserve company quantity/value; no P&L journal for pure warehouse move.
- Interbranch ownership and journal effects remain blocked. Do not assume transfer is intra-legal-entity if branch is separate entity.
- Positive adjustment requires explicit unit value/reason and approval; negative adjustment cannot exceed quantity and must use current average. Adjustment accounts and maker/checker limits await Finance approval.

## Opening quantity and valuation

1. Profile old stock aggregate, warehouse movement totals, existing valuation positions and inventory control GL before conversion.
2. Separate three cases: (a) quantity already assigned and valued, (b) quantity assigned but unvalued, (c) legacy aggregate quantity not assigned to a warehouse.
3. Require source record, approved as-of date, product/warehouse, quantity, value or explicit unknown flag, actor/approver, and reconciliation reference.
4. Add quantity only for a genuine opening quantity not already represented by movements. For an existing unvalued balance, add value to same quantity without duplicating quantity. For unallocated quantity, explicitly allocate by warehouse with evidence and never exceed aggregate source quantity.
5. Opening valuation records subledger event only under current design. Report the GL difference and require separately approved adjustment if finance wants one.
6. Do not use Product.cost_price, current purchase price, average of incomplete historic unit costs, or arbitrary zero to fabricate legacy value.

## Reconciliation and controls

Run/review at product/warehouse and as-of date: sum movements vs quantity position; replay valuation entries vs balance quantity/value; valued/unvalued positions; unallocated legacy stock; total valued inventory vs inventory control account; outstanding receipt/invoice difference; transfer source/destination conservation; COGS vs source valuation; tax/discount capitalized cost. Every difference needs source, period, owner and resolution; do not silently adjust ledger.

## Concurrency and failures

Costing transaction should lock valuation balance/product key, re-read quantity/value, validate, append movement/event and update balance. Unique source event prevents duplicate post. On unavailable row lock, rely only on backend-verified serialization/constraints; SQLite remains a technical blocker for reliable concurrent posting. Failed transaction rolls back all stock, valuation, document status and linked journal changes. A retry re-reads source status before repeating. Direct database writes bypass model-level immutability; database credentials and operational controls must restrict writers.

## Acceptance tests

Weighted average after multiple receipts; partial issues; exact final issue residual; positive/negative adjustment; explicit zero and unvalued opening distinction; legacy aggregate allocation once; receipt consolidated movement; partial receipts and duplicate prevention; receipt-linked invoice no second qty; mixed-pool variance exception; direct sale and delivery-linked invoice no double stock; return cost snapshot and supplier average; transfer conservation; closed period; cross-tenant product/warehouse; failed transaction atomic rollback; source retry idempotency; reconciliation after each event. Add concurrent tests on selected production backend before release.

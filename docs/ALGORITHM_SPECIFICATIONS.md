# Nexora ERP — Algorithm Specifications

**Phase 3 design only; no executable algorithm or application behavior changed.** Acceptance cases below are proposed tests, not executed test results. Permission role names remain provisional until the Phase 1 role matrix is approved. All workflows derive tenant/branch from authenticated scope and revalidate related records server-side.

## Shared command contract

Every mutating command accepts an authenticated actor, target object/source IDs, business date, validated input, and an idempotency/source key where the event can be retried. It returns the resulting record IDs and status. It must never trust browser-supplied tenant IDs. Within one atomic transaction: lock/re-read authoritative state on a backend that supports it; validate scope, state, period and available quantity/value; write source event, ledger, snapshot, journal, status, cache update and audit event; commit. On failure, rollback all writes and return a stable field or workflow error. SQLite concurrency caveat applies until backend decision.

## A-01 Inventory costing and movement

**Preconditions/permission:** Actor has current inventory mutation permission (verified route guard required); organization and warehouse are active and match product; period open for accounting effects; movement type/source valid. Adjustment requires reason and explicit positive adjustment value. 

**Read:** product, warehouse, current valuation balance, prior immutable valuation entries, corresponding movement/source, open period, adjustment accounts if journal effect applies.

**Calculate:** For current quantity `Q`, value `V`, incoming positive quantity `q` and explicit cost `c`, new value is `round_currency(V + q*c)` and average is `(new value)/(Q+q)` at six decimals. For issue `q<0`, reject if resulting quantity would be negative; if issue empties position, consume all residual `V`, otherwise outgoing value is rounded at current average. Transfer preserves value from source position to destination and has no company P&L effect. Exact rounding and zero-quantity logic follow `inventory/models.py`/`INVENTORY_COSTING.md`; no cost inferred from Product.cost_price for unvalued legacy stock.

**Writes / transaction / states:** Append StockMovement and InventoryValuationEntry, update InventoryValuationBalance, and update legacy quantity projection only through the centralized costing service; add accounting journal only for workflows whose current rule requires it (adjustment account policy must be configured). Position transitions valued/unvalued must preserve explicit status. Immutable ledger rows are not edited/deleted.

**Idempotency/failure/audit:** Unique source event per product/warehouse/type/source key; duplicate request returns prior posting or conflict if payload differs. Roll back movement, balance, valuation, journal and source status together. Audit actor/source/reason. SQLite duplicate/lock failure must be surfaced safely; caller re-reads outcome before retry.

**Acceptance tests:** weighted-average receipt and issue; rounding residual cleared on final issue; zero-cost opening; rejects negative stock, cross-tenant warehouse, missing positive adjustment cost and closed-period posting; transfer conserves company quantity/value; duplicate retry creates no extra movement; unvalued legacy balance remains unvalued.

## A-02 Opening stock and opening valuation

**Class:** explicit quantity allocation is current workflow; opening monetary value is an approval-dependent input. **Preconditions:** active product/warehouse/tenant; authorized inventory/finance actor; source evidence and approval reference supplied; current unassigned quantity and existing position read under lock. Quantity/value nonnegative; no cost guessed. Decide whether this is allocation of pre-existing quantity or a new quantity introduction before implementation; do not conflate them.

**Read/calculate:** legacy product aggregate, warehouse movement quantity, valuation balance/entries, inventory control GL and organization/branch. For existing unvalued quantity, value-only opening writes value against same quantity; for unallocated quantity, quantity movement and value assignment require a separate explicit, approved quantity allocation. Check that no already-valued amount is double-counted; GL difference is reported, not automatically journaled by the current screen.

**Writes:** append opening valuation event and update balance snapshot; if actual physical movement is created, append corresponding movement only once and link it. Do not create GL journal under documented current behavior; any balancing journal is a separately approved operation. Atomic and idempotent by approved source key.

**Failure/audit/tests:** any mismatch, absent evidence, duplicate source or period error rolls back. Audit source document, approver, before/after quantity/value/valued flag. Test valued and unvalued openings, unknown cost, aggregate mismatch, repeat submission, and report that opening value has no implicit journal.

## A-03 Purchase order and goods receipt

**Preconditions:** active confirmed/partially received order; valid supplier/products/branch and active warehouse; receipt date not before order date and in open period; each quantity positive. Actor permission from current guard; inspection/rejection policy is not specified, so accepted quantity semantics require approval before adding quality workflow.

**Read:** order/lines, prior posted receipts for cumulative quantity, warehouse, stock valuation balance, period, product scope. Lock order lines/position where supported. Ensure prior posted received + current <= ordered quantity.

**Calculate:** consolidate repeated product/warehouse receipt lines into one stock event with quantity-weighted receipt basis cost from PO line; weighted-average costing then applies explicit receipt cost. Update order status from posted cumulative quantities to partially received or received. Receipt value basis is PO cost in current workflow.

**Writes:** post receipt, append stock movements/valuation entries, update balance and order fulfillment status. Do not create GRNI accrual rows or journals; the owner reaffirmed this Phase 3 policy on 2026-10-24. Atomic across receipt and inventory. Repeated posting returns prior result.

**Failure/audit/tests:** no partial receipt on any invalid row. Audit actor, date, warehouse, order, quantity and source; preserve draft edit/cancel separately. Tests: partial receipts, cumulative cap under concurrent submissions, multi-line consolidation, wrong branch/supplier/product, closed period, idempotent retry, and explicit no-GRNI-current behavior.

## A-04 GRNI decision boundary

The earlier code contained unused GoodsReceiptAccrual and event models, removed in Phase 4 before their migration was applied. The user explicitly reaffirmed **no interim GRNI** on 2026-10-24. Receipt posting creates no accounting journal or accrual event; the inventory-to-GL report treats unbilled receipt value as an operational reconciling item.

Any future proposal to add GRNI requires a new explicit Finance + Purchasing approval and a replacement design decision covering recognition event (receipt/inspection), accrual basis and tax treatment, debit inventory/expense versus credit GRNI, supplier invoice matching/reversal/reclassification, receipt-only return handling, tolerance/aging, account mapping, period-close treatment, and idempotency key. Current acceptance: receive stock without a GL journal and show uninvoiced receipt reconciling data; no GRNI account posting or accrual record is created.

## A-05 Supplier invoice posting

**Preconditions:** draft with lines; supplier/product and branch match; open period; positive quantities/net line amount; tax config available for tax-aware lines. Receipt-linked line must use posted receipt of matching supplier/branch/product/order; aggregate posted invoice quantity plus this request <= received quantity.

**Read:** invoice + lines, receipt/order lines, previous posted invoices, product classifications and effective tax configuration, supplier registration/settings, warehouse, valuation position, period, accounts. Lock invoice/receipt line/valuation rows on supported backend.

**Calculate:** tax snapshots and header totals per `GST_RULES.md`. Direct invoice creates stock at net cost plus non-creditable tax; recoverable input tax is excluded from inventory. Receipt-linked invoice does not change quantity again; compare actual net cost to receipt basis, allocate difference between remaining inventory and COGS using existing estimate, with exception when mixed receipt history makes it unreliable. Do not call this a fully layer-accurate method.

**Writes:** inventory quantity/value only for direct path; for receipt-linked path valuation revalue and COGS variance only; balanced AP/tax/inventory journals; TaxTransaction per source line; mark invoice posted and recalculate supplier balance. Atomic. Source invoice post is idempotent.

**Failure/audit/tests:** any tax/receipt/period/account failure leaves no journal, valuation, tax row or posted state. Capture actor, invoice/receipt line source, rates, cost and variance evidence. Tests cover direct versus receipt-linked no double stock, partial invoices, over-invoice under concurrency, tax input-credit/non-creditable/reverse-charge, price variance, missing warehouse, duplicate retry and balanced posting.

## A-06 Sales confirmation, delivery and invoice

**Order confirmation:** require valid nonempty draft, same-tenant customer/products/branch, positive qty, valid price/discount. Writes confirmation and audit only; no stock reservation in current behavior. Idempotent for already-confirmed order.

**Delivery preconditions:** confirmed/partially fulfilled order; quantities positive and within cumulative remaining to ship; active warehouse in permitted scope; enough stock under approved no-negative rule; date/period valid. Read posted delivery history and warehouse quantity/value. Calculate remaining per source order line, consolidate product movement and consume warehouse average at dispatch. Atomic delivery + StockMovement + ValuationEntry + order fulfillment state; no repeat movement on retry. Status is draft → dispatched/current vocabulary; invoice links only dispatched delivery. Failed dispatch rolls back all lines.

**Invoice:** delivery-linked invoice quantity/product set must match dispatched delivery and must not issue stock again; direct invoice chooses current eligible warehouse and consumes stock on posting. Compute tax/header totals from current config and snapshots, revenue/receivable journal, tax liability and COGS/inventory journal. The source delivery cost snapshot is used for delivery-linked COGS. Atomic across invoice status, tax rows, stock where direct, journals and customer balance. Idempotency keyed by invoice; do not post second set of entries on retry.

**Permissions/acceptance:** role labels depend on approved matrix. Tests: partial deliveries, cumulative cap, exact source-line linkage, direct vs delivered inventory effect, tenant mismatch, stock shortage, period closed, tax snapshot consistency, balanced journals and replay.

## A-07 Customer and supplier returns

**Customer return:** source posted invoice; lines point to source invoice lines; cumulative prior posted quantity + new <= source qty; restock warehouse valid if any restock line. Calculate proportionate tax/credit from original snapshot using cumulative rounding; for older no-snapshot invoice use documented aggregate fallback and flag that it is not a line-level statutory reversal. Restock cost uses original movement cost and fails if absent. Writes return, credit note, negative tax snapshot, revenue/output-tax/AR journal, optional stock/valuation and COGS reversal, balance recalc, audit. No source invoice mutation.

**Supplier return:** source either posted receipt for unbilled quantity or posted invoice line; require source-specific available return quantity, matching supplier/product/branch and warehouse stock. Stock out at current average; credit basis and tax reversal from invoice snapshot where applicable. Difference goes to configured inventory adjustment gain/loss. Receipt-only credit creates AP/supplier credit but currently has no GRNI reversal. Writes debit note, journal, negative tax rows if source snapshot, movement/valuation, balance recalc, event and audit.

Both flows require one transaction and idempotency by return ID/source line; retry returns existing note. Tests cover cumulative partial returns, over-return, repeated posting, missing original cost, restock false, wrong source/tenant, no-stock supplier return, tax snapshot proportional rounding, receipt-only supplier credit and journal balancing. Return statuses must be reconciled to actual model choices before workflow UI changes.

## A-08 Bank transaction, invoice allocation and reconciliation

**Transaction:** validate active account and tenant/branch/counterparty; amount and direction; date/period; ensure reference uniqueness under existing rule. Create/post transaction and its journal once. Bank movement is not repeated during allocation.

**Allocation:** read transaction, invoice, active allocations and reversals, source counterparty/tenant/branch and journal accounts. Under transaction recompute invoice open amount and transaction unapplied amount. Require positive amount <= both; create allocation and reclassification journal; update cached outstanding. Reversal requires authorized actor and nonempty reason, preserves allocation row and posts correcting journal/recalc. No negative allocation or delete.

**Statement import/reconcile:** validate whole CSV (required date/ref/signed amount) before writes; duplicate detection; atomic import. Lines begin unmatched. Suggestion is read-only and may use configured date tolerance; confirmation requires exact account/date/signed amount against posted transaction and one-to-one match. Unmatch reason and actor required.

Tests: partial/split allocation, over-allocation under concurrency, cross-counterparty, duplicate reference/import, reversal and replay, import one bad row leaves zero partial batch, suggestion never auto-match, exact match success and near-date/date-direction mismatch failure.

## A-09 Journal, period close and year-end

**Journal:** draft only; active in-scope accounts; exactly one positive debit/credit per line; decimal amounts; journal debits exactly equal credits; period open; valid organization/branch. Atomic status posted plus account caches and source updates. Repeat post is no-op. Reverse creates linked opposite lines in an open date/period, never alters original. Tests cover imbalance, line-side checks, inactive/cross-tenant account, closed period, duplicate retry, reversal linkage, original immutability.

**Period close:** actor must be an organization-level finance role as currently guarded; open period and no configured blockers (draft journal, draft sales/purchase invoice, unmatched statement rows in range). Close status and audit event atomically. Reopen requires reason, allowed role, audit and policy (currently implemented period audit). Do not introduce a missing blocker as fact; evaluate additional stock/tax/AP/AR blockers as a decision. Tests cover blockers, boundary dates, duplicate close, unauthorized/branch actor and reopen reason.

**Year-end:** call documented preview first; validate complete periods, no drafts/pending imports/unmatched lines, balanced posted activity, retained earnings account and next-year first open period. In transaction create unique close record, income/expense close journals by branch, then balance-sheet opening journals for new year; post only through special validated paths. Preserve cumulative ledger cache semantics so opening snapshot is not double-counted. Unique close and state locks provide idempotency where backend supports it. Final close is immutable and has no automated reversal; recovery is stop posting and approved manual correction. Tests mirror financial control docs, including retry and cumulative vs selected-year report comparison.

## A-10 Fixed assets

Read asset/category, period, accounts, prior depreciation by period, and lifecycle events. Validate asset belongs to tenant/branch, acquisition/disposal state, positive cost/life, open period. Capitalization, depreciation, transfer, disposal writes journal and event atomically; unique asset/period prevents duplicate depreciation. Existing methods include straight-line and reducing balance; do not infer statutory tax treatment. Existing capitalization uses cash account assumption; supplier-invoice capitalization is absent. Approval is blocked for acquisition account, book/tax method, day count, salvage handling, rounding and disposal proceeds. Tests: duplicate period, closed period, method result versus approved schedule, transfer audit, disposal journal balance, failed journal rolls back event, and cash-ledger limitation asserted/documented.

## A-11 Tax snapshots and reporting

Tax calculation must select effective component rates for document date; state-specific rate wins over all-state when both match. Same state uses configured CGST+SGST or CGST+UTGST; interstate uses IGST; cess only supported as taxable-value percentage. Inclusive and exclusive math, discounts, exemption/zero rating and reverse-charge treatment follow `GST_RULES.md`. Validate registration/place-of-supply/classification explicitly; do not infer missing state. Persist component percentages, taxable base, amounts, flags and source line once on posting. Return uses signed reversal from snapshot, never current rate.

Reports query posted snapshots with tenant/date/branch filters and expose period basis. Internal report totals reconcile to snapshots; no submission. Acceptance: state/effective boundary, inclusive/exclusive rounding, missing rate, reverse-charge, exempt, snapshot immutability after rate edit, return exact reversal, tenant filters and export-to-screen equality. Law correctness and statutory schema tests block on Tax owner/legal approval.

## A-12 Management/financial reporting

Read posted authoritative ledger/doc snapshots only unless report explicitly labels drafts or operational data. Filter and aggregate by selected organization/branch/date basis and state that basis in output. Inventory value uses valuation subledger; low stock must use one consistent available-quantity/reorder rule (current dashboard differs, so no implementation formula is declared approved). Available cash must aggregate eligible accounts only under an approved account type/status definition; current first-account label is not authoritative aggregate. Reports include source drilldowns and reconciliation exceptions. Tests tie reports to fixture ledgers and prove branch isolation, date inclusion, export parity, low-stock boundary, multi-bank balance and unvalued inventory signaling. KPI semantic decisions block before a definitive acceptance threshold is coded.

# Nexora ERP — Accounting Posting Rules

This specification describes current posting shapes and controls, then identifies unresolved accounting policy. It is not professional accounting or tax advice. Posting algorithms remain design-only; no code, model, migration, or ledger changed in Phase 3.

## Common journal contract

**Preconditions:** valid organization and optional branch; all accounts active and in-scope; entry date in open period if periods are configured; at least two valid lines; decimal debit/credit values; each line has exactly one positive side; total debits exactly equal credits after currency rounding; source event is valid and not already posted.

**Atomic writes:** journal header, lines, status, account balance projection, source status/links, source-specific tax/stock/settlement effects, and audit event must commit together. Query authoritative ledger rows, not cached account balances, to verify a journal. Posted entries and lines are immutable; corrections use a linked reversal in a valid open period. Idempotency key is source type + source ID + posting purpose (or a dedicated unique source link); same key with conflicting payload is an error.

**Failure:** any failed account, balance, period, tenant, source, or concurrency validation rolls back all effects. Existing configured behavior when no financial year exists currently permits posting; changing that requires migration/cutover approval. SQLite does not provide effective row-level `select_for_update` guarantees; production posting requires selected backend verification.

## Current posting templates

| Event | Debit | Credit | Evidence / qualification |
|---|---|---|---|
| Sales invoice revenue | Accounts receivable for total | Sales revenue for subtotal; output tax payable for tax | Sales post service; tax-aware lines use TaxTransaction snapshots. |
| Sales COGS | COGS | Inventory | Cost from delivery valuation when linked; direct invoice consumes warehouse weighted average. |
| Direct purchase invoice | Inventory for subtotal plus non-creditable tax; eligible input tax debit; reverse-charge sides as configured | Accounts payable for invoice total; reverse-charge GST payable as applicable | Recoverable tax excluded from inventory; non-creditable tax capitalized. |
| Receipt-linked purchase price correction | Revalue inventory for estimated still-on-hand share; recognize sold share variance in COGS as existing algorithm dictates | Accounts payable balance remains based on supplier invoice; exact journal balancing lines depend on positive/negative variance direction | Existing weighted-average estimate, not receipt-layer exact. See exception risk. |
| Receipt | No journal currently | No journal currently | Goods receipt changes inventory valuation only. GRNI is not currently booked. |
| Payment/receipt bank transaction | Cash/bank plus receivable/payable or advance accounts per type | Counterpart account | Exact directions depend on transaction type/account config. |
| Invoice allocation | Advance/unapplied liability or asset reclassification to AR/AP control | Opposite reclassification | Does not touch cash/bank again. Reversal is a correcting journal. |
| Customer return/credit note | Sales revenue and output tax reversal; optionally inventory | Accounts receivable; optionally COGS reversal | Tax/credit source snapshots, cost reversal only for restocked goods with source cost. |
| Supplier return/debit note | Accounts payable; eligible input-tax reversal/RCM reversal as applicable | Inventory at current average; adjustment gain/loss balances basis difference | Receipt-only return has no GRNI reversal under current behavior. |
| Stock adjustment | Inventory or adjustment expense/loss depending on sign | Adjustment gain/loss or inventory | Account mapping and exact posting side must be approved/configured; no default account policy invented here. |
| Warehouse transfer | No company GL effect | No company GL effect | Value moves between warehouse subledger positions; interbranch accounting is unresolved. |
| Fixed asset acquisition | Fixed asset account | Current code uses configured/default cash ledger | Existing assumption is a limitation; supplier invoice/AP capitalization is not implemented. Finance decision blocks live policy. |
| Depreciation | Depreciation expense | Accumulated depreciation | Method and amount per configured asset category; book/tax, period convention, salvage and effective changes require approval. |
| Asset disposal | Cash/proceeds and accumulated depreciation; loss where needed | Asset cost; gain where needed | Current service computes gain/loss; approval of residual/schedule policy required. |
| Period close | Not an accounting journal | Not an accounting journal | Operational lock and audit event; no automatic accruals/adjustments implied. |
| Year-end income/expense close | Close revenue/credit-balance and expense/debit-balance accounts appropriately | Opposite side; net profit credited or net loss debited to retained earnings | Existing close groups by branch and is linked/idempotent; generated closing period treatment is special. |
| Year-end opening snapshot | Balance sheet account balances opened in next year | Opposite side to balance | Not a second cumulative economic posting; separate selected-year report snapshot path. |

The purchase variance row notation describes the business effect; exact debit/credit sign for positive/negative variance must follow `PurchaseInvoice.post()` and be tested against the balanced-journal invariant.

## Period and closing controls

1. Validate entry date against organization periods; reject closed period. If no configured years exist, existing legacy allowance is a compatibility behavior pending decision.
2. Period close records actor/reason and checks existing blockers documented in `FINANCIAL_CONTROLS.md`. Additional blockers (unvalued stock, open AR/AP, GST reconciliation, unbilled receipts) are proposed review items, not current close behavior; Finance must approve whether they block close or appear as warnings.
3. Year-end preview is read-only. Confirm revalidates under transaction: complete/closed prior year, required next year and open first period, valid retained earnings, no prohibited drafts/imports/unmatched statements, balanced posted history and branch consistency.
4. Create close record once; create income/expense close journals by branch and balance sheet opening snapshot journals for next year. Unique close linkage and final state prevent duplicate generation. Cumulative balance cache must not count opening snapshots twice.
5. A finalized close is not automatically reversible. On detected error, freeze relevant posting, preserve history, and require accountant-approved correction plan. No delete/reopen path is specified.

## Reversal, correction, and settlement rules

- Reversal is a new journal with opposite source lines, a reference to original entry, explicit reason and an allowed accounting date. Never mutate posted lines.
- If source transaction has inventory or tax effects, use its domain reversal workflow so quantity, valuation and snapshot rows are corrected consistently; a standalone journal cannot silently repair stock/tax subledgers.
- Invoice allocation posts a balance reclassification only. Reversing it retains original allocation and posts the reverse classification; cash/bank transaction remains posted.
- Return notes reverse only source amounts supported by transaction snapshots. Older transactions without tax detail may use the documented aggregate fallback but must be marked as lacking line-level tax evidence.
- External/manual journal posting cannot impersonate an existing source event; unique source identity prevents a retry or alternate path from double-posting.

## Outstanding accounting approvals

Finance must approve chart/control accounts, opening GL balances, landed costs/price variance, cash-flow statement classification, interbranch due-to/from, foreign currency, tax filing/account treatment, fixed asset acquisition and book/tax depreciation, close blocker policy and retention. The owner explicitly approved the existing no-GRNI receipt rule on 2026-10-24; any future proposal to add GRNI requires new approval and a separate posting design. Until other policies are approved, only evidenced current entries are specified; no new posting policy may be assumed.

## Acceptance tests to implement later

Balanced and unbalanced journal; single-side constraint; closed period and no-year compatibility; idempotent source posting; cross-tenant account rejection; original remains immutable on reversal; sales and purchase journal balance for inclusive/exclusive and reverse-charge tax; allocation does not duplicate cash; receipt adds no journal under current policy; return tax/cost reversal; close and opening snapshot not double-counting; fixed asset duplicate depreciation rejection; concurrency replay on selected production engine; every failed workflow leaves no partial journal/source state.

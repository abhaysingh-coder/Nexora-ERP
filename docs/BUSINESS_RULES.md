# Nexora ERP — Phase 3 Business Rules

**Status:** specification only. This document does not change models, application behavior, migrations, or data. “Existing behavior” records repository evidence, not an unqualified legal/accounting approval. Rule IDs are stable references for `ALGORITHM_SPECIFICATIONS.md`, `ACCOUNTING_POSTING_RULES.md`, `INVENTORY_RULES.md`, and acceptance tests.

## Decision classifications

- **Approved by evidence:** explicitly documented in Phase 1/2 specifications or established as current code behavior. Where it is code-only, treat as compatibility contract, not business-owner signoff.
- **Provisional:** safe working specification with assumptions and consequences. Must be confirmed before implementation where it affects production behavior.
- **Blocked:** policy or technical choice materially changes books, stock availability, security, legal compliance, or retention; no default may be implemented until named owner approves.

## Decision register disposition

| Decision | Class | Current evidence / provisional assumption | Impact and required approval |
|---|---|---|---|
| Costing method | Approved by evidence | Perpetual weighted average per organization/product/warehouse; two-decimal value rounding `ROUND_HALF_UP`, six-decimal unit cost snapshot; negative inventory rejected in costing service. | Compatibility contract. Finance + Inventory must approve before changing method, zero/negative stock semantics, or precision. |
| Opening stock quantity/value | Provisional | Import quantity from a reconciled ledger; value only from approved evidence. Unknown cost remains unvalued; opening valuation itself does not post GL. | No invented cost. Finance + Inventory must approve quantity evidence, unit value, warehouse allocation, date and any separate GL adjustment. |
| Negative stock, backdating, reservation | Split: negative stock approved by documented code/design; backdating and reservations blocked | Existing costing rejects negative issue and valuation sequence follows posting time; no reservation ledger or policy. | Operations + Finance must approve exceptions, event ordering/time basis, reservation priority/expiry and availability rules. |
| GRNI and receipt/invoice accounting | Approved no-GRNI rule | Owner confirmed “Keep Phase 3 no-GRNI rule.” Goods receipts update stock quantity and valuation without an interim GRNI journal; supplier invoice posting recognizes purchase accounting, and valuation reconciliation adds unbilled receipt value. | Do not post interim GRNI or create receipt accrual ledger rows. Any future change requires Finance + Purchasing approval of recognition timing, account, amount basis, settlement/reversal, returns before invoice, aging and tolerance. |
| Receipt/invoice matching | Approved current control; policy tolerances blocked | Posted receipt quantity is checked against cumulative posted invoice quantity; receipt-linked invoice avoids a second quantity movement. | Finance + Purchasing approve price/quantity tolerances, split invoices, over/under billing and exception approval. |
| Document numbering/reset | Blocked | Existing fields mix global and tenant uniqueness; return constraints are inconsistent. | Finance + Operations approve legal entity, branch, fiscal-year, document type, format, gaps, reset and concurrency policy. Collision scan precedes migration. |
| Currency/exchange rates | Blocked | Organization has currency string default INR; current amounts and posting assume a single organization currency; no FX ledger/rate source. | Finance approves INR-only scope or transaction/base currency, source/timing of rates, precision, realized/unrealized gains and rounding. |
| Tax configuration/snapshots | Approved by evidence for snapshot behavior; law/rates/filing blocked | Effective-dated component rates; posted line snapshots retained; returns reverse original snapshot; reports are internal. | Tax + Finance approve applicable law, registrations, place-of-supply cases, ITC/reverse charge and statutory filing/provider scope. Never present software calculation as legal advice/compliance. |
| Retention, deletion, privacy | Provisional preservation rule; durations blocked | Posted finance/tax/stock and audit evidence are corrected by reversal, not silent deletion. Draft deletion policy is not fully uniform. | Legal + Security approve statutory retention, personal-data erasure, legal hold, archival and attachment security before implementation. |
| Roles, branch scope, maker-checker | Blocked | Existing route role guards vary; free-text Membership.role overlaps User org/branch. No approved permission matrix. | Product + Security + Finance approve capability matrix, privileged roles, branch visibility, export rights, thresholds and segregation. No new maker-checker thresholds assumed. |
| Interbranch accounting | Blocked | Branch dimensions and warehouse transfers exist; interbranch request/settlement flow and due-to/due-from policy are not established. | Finance approves legal entity boundary, inventory ownership, transfer pricing, clearing accounts and balancing timing. |
| Fixed asset/depreciation | Existing behavior documented; accounting policy blocked | Straight-line/reducing balance functionality exists; asset capitalization currently assumes cash ledger; duplicate asset-period depreciation is prevented. | Finance approves book/tax methods, useful life/rates, calendar, rounding, capitalization source/account, disposal and effective-dated changes. |
| APIs, bank formats, external integrations | Blocked | DRF installed but no exposed API routes; CSV and manual reconciliation exist. | Product + Security + Finance approve API scope/version/auth/idempotency, bank formats/feed, credentials, audit and provider contracts. |
| Database/concurrency | Technical blocker | Settings use SQLite; PostgreSQL package alone does not prove configuration; `select_for_update` behavior differs. | Architecture + Operations select production engine, transaction/isolation/locking strategy, performance targets and operations. Do not claim safe concurrent posting until verified there. |
| Candidate module scope | Blocked per module | Phase 1 marks CRM, POS, distribution/dealership, manufacturing, HR/payroll and projects missing or unknown. | Product + named business owners approve inclusion, jurisdictions, dependencies and acceptance scope before workflow/schema design. |

## Workflow rules

### Identity, tenant scope and permissions (IAM-01/02, ORG-01/02)

1. Derive organization and branch scope from authenticated membership/session context, not submitted form values. Reject cross-organization references on both reads and writes.
2. Enforce currently implemented permission guards as observed, but use `ROLE_SCREEN_MATRIX.md` only as a proposed target until approved. Any role not explicitly verified is not granted by assumption.
3. Proposed permission dimensions: action (`read/create/edit/approve/post/reverse/export`), organization, optional branch, object scope, and effective active membership. No amount thresholds are specified.
4. Fail closed when membership is inactive or ambiguous. Preserve user, actor, tenant, timestamp, action, source and reason for material state changes; audit completeness remains a gap.

### Master data (MST-01..05)

- Organization owns customer, supplier, product, warehouse, tax and account references. Identifiers are unique only at scopes approved by the numbering/master-data decision.
- Existing product `unit` is text; conversions and service catalog are not implied. Do not convert historical quantities without an approved map and effective date.
- Referenced masters are deactivated rather than hard-deleted (provisional preservation rule); correction of posted documents never mutates historical master snapshots.

### Sales order, dispatch, invoice (SAL-03..05)

- Confirm only a nonempty valid draft whose customer/products/branch match tenant. Confirmation does not consume stock in current code.
- Dispatch requires confirmed/partially fulfilled order, open period for the date, active warehouse in allowed branch, positive quantity within cumulative unshipped order balance, and stock sufficient under approved stock rule. One successful dispatch produces exactly one source-linked movement per consolidated product/warehouse.
- Invoice from delivery may post only against a dispatched note; invoice lines/qty match delivered quantities. Direct invoice is a supported current path and consumes stock at posting. A delivery-linked invoice does not consume stock again.
- Header totals and tax snapshots are calculated/validated at posting. Posted invoice, tax snapshot, stock and journals commit together; retry returns existing posting result.

### Purchase order, receipt, supplier invoice (PUR-03..05)

- Confirm nonempty draft order after supplier/product/branch and date/period checks. Receipt is only from a confirmed/partially received order and uses its branch warehouse.
- Receipt quantities are positive and cumulative accepted receipts cannot exceed ordered line quantity. Receipt posting creates stock movement/valuation once and updates order receipt status, without an interim GRNI journal or accrual row. The explicit owner decision reaffirmed this Phase 3 rule during Phase 4.
- Invoice may be direct (creates stock at invoice cost) or receipt-linked. Receipt-linked quantities cannot exceed posted received quantity remaining after prior posted invoices; source supplier, branch, product and order line must match. It must not create a second quantity movement.
- Price/quantity variances beyond existing calculation are not automatically approved policy; exception tolerances and approval remain blocked.

### Returns (SAL-07, PUR-07)

- Return is a separate correction linked to posted source invoice/receipt. Original source rows remain unchanged.
- Customer return cumulative quantity cannot exceed invoice line. Restock flag controls stock/COGS reversal; restock requires valid warehouse and original sale cost snapshot, otherwise posting fails. Tax and credit amount use source invoice snapshot; no current tax recalculation.
- Supplier return source is a posted receipt for unbilled quantity or posted invoice for billed quantity. Cumulative return cannot exceed eligible source balance; stock cannot become negative. Use current warehouse average for stock removal; supplier credit uses source invoice/receipt basis; difference is posted to the designated adjustment account as configured by existing code.
- Return, note, journal, tax reversal, stock movement and balance recalculation are atomic and duplicate-safe. Exact return status label vocabulary across docs and code needs normalization before UI changes.

### Banking and settlement (BNK-01/02, SAL-06, PUR-06)

- A posted receipt/payment may remain unapplied. Allocation is allowed only to a posted invoice of the same counterparty, tenant and compatible branch; allocation amount must be positive and not exceed either invoice open amount or transaction unapplied amount.
- Allocation posts a reclassification journal only; it must not post cash/bank twice. Reversal retains original allocation and creates a correcting journal with reason.
- Statement import validates all rows before atomic creation; duplicate detection is deterministic. Suggestions never reconcile automatically. User confirmation requires exact account, date, signed amount and posted transaction; date tolerance applies only to suggestions.

### Accounting, periods, year-end and assets (ACC-01..06)

- Journal draft requires in-scope active accounts, one positive side per line, same tenant/allowed branch, and exact debit-credit equality before posting. Posted lines immutable; reverse using a new linked opposite journal in an open period and retain original.
- Posting date in a configured closed period is rejected. If no financial year is configured, current code allows legacy posting; do not silently change this compatibility rule without an explicit cutover decision.
- Period close uses current blocker set (draft journals, draft sales/purchase invoices, unmatched statement lines) and records actor/reason. Year-end close has broader documented readiness checks and is idempotent; finalized close is not reopened through generic routes. No general closed-period bypass.
- Fixed asset acquisition/depreciation/disposal operations are atomic with journal/history. Existing capitalization assumes a cash ledger; this is a known limitation, not a recommended policy. Depreciation once per asset/period.

### GST and reports (GST-01..04, RPT-01)

- Use configured effective rate on transaction date; require explicit party/registration/state/place-of-supply and item classification where tax-aware posting applies. Inclusive/exclusive calculation, component split, reverse charge, and rounding follow `GST_RULES.md` as current implementation contract.
- Reports derive from posted snapshots and show scope/date basis. Internal tax reports must be labelled internal; statutory prep/filing, official rate verification and government integration are blocked pending legal/provider approval.
- Reconciliation reports must expose unvalued inventory, unapplied balances, unposted/draft records, unmatched statement lines and source exceptions rather than silently netting them.

## Global nonfunctional business rules

- Every side effect must be tenant-scoped, auditable, idempotent and committed atomically with its source state transition.
- Decimal arithmetic only; do not use binary floating point for money or quantity. Use established component rounding for current behavior, but multi-currency precision is blocked.
- No external notifications, file uploads, bank feeds, government submission or API promises without approved scope, security model and failure/retry policy.
- Acceptance criteria in Phase 1 are required regression targets; this specification adds process tests but does not report tests as executed.

## Phase 1 traceability map

| Rule area | Feature IDs covered | Specification |
|---|---|---|
| Sign-in, organization and role/branch authority | PLT-01, PLT-02, ORG-01, ORG-02, ORG-03, IAM-01, IAM-02 | This file's identity section; `ERROR_HANDLING_AND_TRANSACTIONS.md`. |
| Party, item, UOM, warehouse and account masters | MST-01, MST-02, MST-03, MST-04, MST-05 | This file's master-data section; proposed parts remain blocked by scope/numbering decisions. |
| Sales orders, dispatch, invoices, receipts, returns, AR/credit | SAL-01, SAL-02, SAL-03, SAL-04, SAL-05, SAL-06, SAL-07, SAL-08, SAL-09 | Sales and return sections; Algorithms A-06/A-07/A-08. Enquiries/quotations (SAL-01/02) and pricing (SAL-08) are missing/blocked. |
| Purchase orders, receipts, invoices, payments, returns, AP | PUR-01, PUR-02, PUR-03, PUR-04, PUR-05, PUR-06, PUR-07, PUR-08, PUR-09 | Purchase and return sections; Algorithms A-03/A-04/A-05/A-07/A-08. Requisitions/quotes (PUR-01/02), landed cost (PUR-08) are blocked. |
| Stock ledger, valuation, opening, transfers, adjustments, counts, traceability, UOM and interbranch | INV-01, INV-02, INV-03, INV-04, INV-05, INV-06, INV-07, INV-08, INV-09 | `INVENTORY_RULES.md`; Algorithms A-01..04/A-06/A-07. Formal counts/reservations/batch/serial/bins/UOM conversions (INV-06..08) and interbranch settlement (INV-09) remain blocked. |
| Journals, financial statements, cash flow, close, budget, assets | ACC-01, ACC-02, ACC-03, ACC-04, ACC-05, ACC-06 | `ACCOUNTING_POSTING_RULES.md`; Algorithms A-09/A-10. Cash flow policy (ACC-03), budget approval/encumbrance and fixed-asset policy remain open. |
| Bank accounts/transactions, statements, cheque/PDC | BNK-01, BNK-02, BNK-03 | Algorithm A-08; `ACCOUNTING_POSTING_RULES.md`. Statement reconciliation is specified; cheque/PDC/card clearing (BNK-03) is missing and blocked. |
| Audit, tax setup/calculation/reports/statutory filing, shared reporting | AUD-01, GST-01, GST-02, GST-03, GST-04, RPT-01 | This file's GST/report section; Algorithms A-11/A-12. Statutory filing (GST-04) is explicitly blocked. |
| Approval inbox/archive, APIs/operations and candidate ERP domains | OPS-01, OPS-02, API-01, RET-01, DIST-01, MFG-01, HR-01, CRM-01, PROJ-01, REF-01 | `ERROR_HANDLING_AND_TRANSACTIONS.md`; candidate modules are out of approved workflow scope pending owner decisions. |

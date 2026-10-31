# Nexora ERP — Error Handling and Transaction Boundaries

This is a cross-workflow contract for future implementation and review. No exception handling or transaction code was modified in Phase 3.

## Command boundary

Each state-changing business action is a command, not a collection of independent form saves. The command:

1. Authenticates actor; resolves organization/branch from trusted session/membership and checks permission for action and object.
2. Parses and validates inputs into Decimal/date/enum values; rejects unknown IDs and never trusts posted tenant IDs.
3. Opens `transaction.atomic()` before authoritative reads that govern availability or uniqueness. On PostgreSQL lock aggregate/source rows in stable primary-key order; on SQLite do not claim row-lock safety, constrain posting concurrency or require production backend test.
4. Re-reads source state, active allocations/receipts/returns/movements and period state within transaction. Validate tenant consistency, status, available quantity/value, totals and idempotency key.
5. Persists source transition, child rows, stock/tax/accounting ledgers, derived cache updates and audit event together. Add unique source/event key to protect duplicate retries across requests.
6. Commits once. Only after commit may response claim success or send noncritical side effect; external call outbox/retry design is required before adding integrations.

## Validation stages

- **Input validation:** required fields, parsing, decimal precision/rounding, bounded positive quantities/amounts, allowed status transition, valid dates.
- **Authorization validation:** active user and membership, role capability/action, object tenant and branch scope, separation-of-duty checks if later approved.
- **Business validation:** state transition, source quantities, invoice open/unapplied amount, tax config/date, closed period, account mapping, movement cost evidence.
- **Integrity validation:** matching organization/branch on every FK, unique reference/source key, total equality, journal balance, stock and valuation roll-forward.
- **Commit validation:** database constraints, expected row counts, unique source effect, then reconciliation/return response IDs.

Errors should be stable and actionable but avoid leaking another tenant's record existence. Form errors should attach to field or source line. Conflict errors should identify stale state and ask the user to reload/review, not suggest blind retry. Permission failures should not reveal restricted values. Internal logs should include correlation ID and safe object IDs; never log passwords, session secrets, full bank account numbers, or sensitive payroll data.

## Failure and retry policy

| Failure class | Response / persistence rule | Retry behavior |
|---|---|---|
| Validation or permission rejection | No write; return field/workflow error or access denied. | Correct input/permission; do not auto-retry. |
| Stale state / availability changed | Roll back whole command; indicate conflict and current state only if in scope. | User reloads; command revalidates. |
| Duplicate idempotency key, same payload | Return original result/status. | Safe retry. |
| Duplicate key, conflicting payload | Roll back; conflict and audit/security signal. | Never silently reuse key. |
| Period closed or source already consumed | Roll back; explicit blocked state. | Requires authorized correction/reopen policy; no retry loop. |
| Database integrity/serialization/deadlock | Roll back; log sanitized correlation and backend error. | Bounded retry only for known transient backend errors, using same idempotency key and re-reading all state. SQLite lock retries must first check whether source effect committed. |
| External integration timeout | Not applicable until integration approved. Future use outbox plus delivery status; DB posting must not be duplicated by external retry. | Retry integration independently using stable external request key. |
| Audit write failure | For required audit, fail/rollback business command. | Alert operator; retry after audit sink restored. |

Never catch broad exceptions and return success. Avoid `on_commit` side effects that are required for accounting integrity. Never allow a duplicate retry to recreate stock movement, journal, tax snapshot, bank allocation or note.

## Workflow atomicity matrix

| Command | Must commit atomically | Authoritative rows re-read / lock target | Idempotency and compensation |
|---|---|---|---|
| Stock movement/valuation | movement + valuation entry + position + related document status + required journal | product/warehouse balance, source doc/line, open period | Unique source event; correct via new movement/reversal. |
| Opening valuation | opening event + balance + source allocation link + audit | product/warehouse position, legacy qty, prior opening source | Unique opening source; no implicit GL journal. |
| Goods receipt | receipt status + movement/valuation entries + PO cumulative state + audit | PO lines, posted receipts, warehouse position, period | Unique receipt/source movements; no GRNI journal currently. |
| Supplier invoice | tax rows + inventory/revaluation/COGS + journal + invoice posted + supplier balance | invoice/receipt lines, prior invoices, stock position, period | Unique invoice posting/source tax rows. |
| Delivery | delivery state + consolidated movements/valuation + order fulfilled balance | order lines, prior deliveries, stock position, period | Unique delivery movement key; reverse via approved return/correction. |
| Sales invoice | tax snapshots + direct stock effect or delivery COGS + revenue/COGS journals + posted state + customer balance | invoice, delivery, source movement, stock position, period | Unique invoice posting; credit note corrects posted invoice. |
| Return | return state + note/lines + tax reversals + journal + optional stock + counterparty balance + audit | source invoice/receipt lines, prior returns, stock, period | One note per source return; retry returns existing result. |
| Bank transaction/allocation | transaction and journal; allocation plus reclassification journal/cache; reversal event and correcting journal | account, transaction, invoice open balance, active allocations, period | Transaction reference/idempotency; allocation source unique; reverse without deleting. |
| Statement import/reconcile | whole validated import batch; line match + event + transaction reconciled state | account, import fingerprint/lines, exact candidate and current match | Import fingerprint/row unique; one-to-one line match; suggestions read-only. |
| Journal/period/year close | journal lines/status/cache; period state + audit; close record + generated journals/events | accounts, period, all blockers, prior close and next year | Unique source journal/period close/year close; reverse journal only. |
| Asset lifecycle | asset event/state + journal + depreciation record | asset/category/accounts/prior depreciation/period | Unique asset-period depreciation; no duplicate capitalize/dispose. |

## Isolation and locking

- Lock source aggregate rows and relevant balance rows in deterministic order to reduce deadlock risk. Lock the PO line before recalculating receipts/invoices; invoice and transaction before computing allocation headroom; valuation balance before consuming stock.
- Querysets must include tenant predicates even where object IDs are known. Validate each FK relationship's tenant; database FKs enforce existence, not tenant match.
- Locking is only meaningful if the selected database provides the required semantics. Current SQLite setup cannot support the same concurrent `select_for_update` contract as PostgreSQL. Select backend and test interleavings (two deliveries, two invoice allocations, duplicate imports, simultaneous post) before release.
- Optimistic version fields may be considered for editable drafts, but not as substitute for atomic ledger checks. Draft edit conflict needs user-visible refresh behavior.

## Audit, rollback, and recovery

Audit event for material action should capture actor, organization/branch, timestamp, action, source object and IDs, prior/new state or safe diff, reason/approval reference, correlation ID, and posting links. Existing AuditEvent coverage is incomplete; Phase 3 specifies this as target requirement, subject to retention/privacy approval.

Rollback means transaction rollback before commit. Once committed, business correction is a new compensating event. No posted ledger row is deleted as “rollback.” If a year-end close or other irreversible multi-journal operation is inconsistent, stop dependent posting, preserve generated rows, and require an authorized accountant to approve an explicit correction plan; no automated reversal is specified.

## Acceptance tests

For each command, assert success creates all expected rows and state; failure at every validation/persistence step leaves none; same-key retry returns same IDs; conflicting retry is rejected; tenant A cannot infer/mutate tenant B data; concurrent over-issue/over-allocation has at most one successful excess-consuming action; journal/valuation totals reconcile; required audit event exists; and behavior is exercised on the actual production database backend. These tests are planned, not run.

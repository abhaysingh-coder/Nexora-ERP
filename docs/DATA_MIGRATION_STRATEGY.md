# Nexora ERP â€” Data Migration Strategy

**Design only.** This document does not authorize or perform migration generation, execution, data edits, database reset, or schema changes. Existing migration history is to be treated as immutable production history.

## 1. Current baseline and limits

Repository migration files exist for `core`, `organizations`, `accounting`, `inventory`, `sales`, `purchases`, `banking`, `taxation`, and `returns`; dashboard has no business models. The project settings point to SQLite `db.sqlite3`. This review could not inspect applied migration records, database contents, or a live catalog. The configured Python 3.12 executable is missing, and no installed Python was available. Therefore row counts, data quality, unapplied migrations, SQLite version, active users, and backup state are **unknown**, not assumed clean.

## 2. Non-negotiable preservation rules

- Add new forward migrations; do not edit/delete/reorder an applied migration.
- Take a verified backup and record database identity/hash, app version, migration plan, and restore rehearsal before production conversion.
- Use reversible schema changes where possible. A data migration that cannot safely reverse must document a recovery path and retain pre-migration values.
- Never rewrite posted document numbers, tax snapshots, stock cost history, journal entries, or audit evidence to make the new schema fit. Use aliases, correction events, or explicit exception records.
- Do not infer historical stock cost where source evidence is absent. Preserve unvalued status and record explicit opening valuation decisions.
- Keep changes additive and backward compatible until profiling, backfill verification, and cutover approval complete.

## 3. Recommended staged sequence

### Stage A â€” baseline and profiling (read-only)

1. Freeze exact source commit/file hashes and enumerate migration graph and installed apps.
2. On a protected copy, determine applied migration state, database backend/version, table/row counts, nulls, duplicates, orphan FKs, tenant mismatches, number collisions, overallocated invoices, unbalanced journals, stock quantity/value roll-forward, and cached balance differences.
3. Export exception reports without changing source. Reconcile every monetary/quantity difference with Finance and domain owner; record approved mappings and counts.
4. Back up; verify a restore into isolated staging. Confirm operational downtime/concurrency and rollback owner.

### Stage B â€” additive schema

1. Add new nullable fields/tables/indexes, explicit stable keys and migration metadata. Keep old columns readable.
2. Avoid adding `NOT NULL` or unique constraints over unprofiled historical values. Add nullable field, populate/backfill, validate, then constrain in a later migration.
3. For tenant-safe relational keys, add duplicate tenant key columns and supporting unique constraints only after proving every source relationship matches; then add DB constraints supported by selected backend.
4. For authorization migration, introduce normalized roles/capabilities while preserving membership role and User organization/branch until mapping and precedence are approved.

### Stage C â€” deterministic backfill

1. Write idempotent, batched backfills with stable source PK mapping and a migration-run ledger (version, start/end, count, checksum, errors).
2. Map existing customers/suppliers to any approved Party model using deterministic source IDs; retain old IDs and report duplicates rather than auto-merge.
3. Reconstruct tenant/branch values only from validated ownership. Quarantine missing or conflicting links for manual resolution.
4. Backfill balances from authoritative ledger sources where complete; preserve original cached values for comparison. Do not create synthetic ledger events without approved source facts.
5. Reconcile CustomerReceipt with BankTransaction/InvoiceAllocation before transforming; detect duplicate payment/reference records and preserve source evidence.
6. Carry tax calculations and valuation snapshots byte-for-byte/decimal-exact where possible; do not recompute old posted transactions using current tax/master data.

### Stage D â€” verify, constrain, cut over

1. Compare source/target row counts, tenant groupings, unique key distributions, sums by currency/period, invoice/credit/payment balances, journal debits/credits, stock quantity and valuation roll-forward, tax snapshots, and opening/closing balances.
2. Require zero unexplained loss; publish signed exception counts and sample evidence. Do not hide mismatches by adjusting totals.
3. Add checks, non-null, unique, tenant constraints and indexes in separate migrations after data passes. Re-test query plans and backend support.
4. Cut over using a documented maintenance window or proven dual-write mechanism. Avoid dual-write unless idempotency and reconciliation are implemented. Switch reads only after parity passes; retain rollback copy.
5. Monitor reconciliation and error rates; maintain a bounded rollback window and archive approved conversion reports.

## 4. Candidate migration ordering

Ordering is a dependency proposal, not a command to generate migrations:

1. Backend decision and baseline snapshots; organization/branch/user/membership integrity.
2. Tenant-scoped keys and organization master data, then account/tax/product/warehouse and party/address normalization if approved.
3. Fiscal year/period/accounting policy and opening balances.
4. Sales/purchase document identifiers and source-line links; receipt accrual and return lineage.
5. Stock movement/warehouse mapping and valuation opening balances with valued/unvalued evidence.
6. Banking transactions/imports, allocation and reconciliation; reconcile legacy receipts.
7. Tax snapshot/source-link strategy and internal report reconciliation.
8. Audit/document retention and optional modules only after scope/policy approvals.

Do not force this order if current migration dependencies or live data indicate another safe order; inspect the real migration graph first.

## 5. Rollback and failure handling

- Stop on failed row mapping, nonzero unexplained reconciliation, duplicate key collision, checksum mismatch, or failed restore. Do not partially proceed to new constraints.
- Schema rollback may remove only newly added unused structures after verifying no writes depend on them; never reverse by deleting original transaction data.
- For partially completed backfill, restart idempotently using migration-run markers or restore the staging copy. Production rollback must restore the verified backup and known application version, then reconcile writes made during the window.
- Keep a signed exception log with source ID, reason, owner, decision, and resolution; unresolved exceptions block cutover.

## 6. Required artifacts before implementation

Approved decision register; actual migration graph; data profiling/exception outputs; field mapping; tested migration script and reversal/recovery plan; backup/restore record; staging rehearsal; before/after reconciliation report; downtime/cutover plan; named business sign-offs. Phase 2 review did not perform any of these live-data operations.

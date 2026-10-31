# Banking module

The banking module integrates with the ERP accounting layer and uses the same organization and branch boundaries as the rest of the business logic.

## Features

- Bank and cash account management
- Cash receipts and supplier payments
- Customer and supplier invoice allocations, including partial payments and one payment split across invoices
- Unapplied customer and supplier advances
- Controlled allocation reversals with correcting journal entries and retained history
- Internal transfers between accounts
- Payment reference and transaction-date tracking
- UTF-8 CSV statement import with required reference, amount and date fields, row validation and duplicate detection
- Receivables and payables registers with search/filtering, ageing, allocation history and CSV export
- Suggested statement matches with an explanation and confidence, followed by explicit manual confirmation
- Audited reconciliation and authorized unreconciliation
- One-to-one statement-line matching with account, exact date and signed amount checks

## Business rules

- Transactions are only posted once unless the status remains draft.
- Duplicate references are rejected by the database unique constraint.
- Statement imports are atomic: malformed rows do not leave partial statement lines.
- Imported statement lines always begin unmatched. Suggestions do not change that status or reconcile automatically.
- A user must explicitly select and confirm a posted transaction with the same bank account, transaction date and signed amount. A near-date suggestion is not sufficient for confirmation.
- Match suggestions compare account, signed amount/direction, a configurable transaction-date tolerance, reference and counterparty description. `BANK_RECONCILIATION_DATE_TOLERANCE_DAYS` defaults to `3`.
- Reconciliation and unreconciliation actions are retained as audit events; unreconciliation requires an authorized user and reason.
- Allocation amounts are checked against invoice outstanding and transaction unapplied balances. Reversal retains the original allocation record and posts a reclassification counter-entry instead of deleting history.
- Journal postings happen inside a transaction, keeping balance updates and ledger entries consistent.

## Access

All banking screens require login and active organization context, with assigned-branch filtering where applicable. Bank-account creation, transaction posting, statement CSV import, and reconciliation confirmation require an active organization membership with role `owner`, `admin`, `accountant`, or `finance_manager`, per the owner-approved Phase 4 decision. The same narrow set authorizes reconciliation undo. Allocation retains the existing wider set (`owner`, `admin`, `accountant`, `finance`, `finance_manager`, `manager`, `bookkeeper`); allocation reversal retains the narrow set. Allocation services also validate that the transaction, counterparty, invoice and user scope agree; browser-submitted organization IDs are not treated as authority.

## Limitations

- Historical legacy receipts/payments are not backfilled to invoices; they remain part of aggregate balances until explicitly handled. New advance-mode receipts/payments support invoice allocations.
- The legacy `CustomerReceipt` workflow remains separate from bank-transaction allocations.
- Imported formats beyond the documented CSV columns, bank feeds, statement fee handling, cheque clearing and payment approval limits are not implemented.
- Reconciliation confirms a date/amount/account match; it does not verify that a bank statement is an official bank-issued record.
- Concurrency behavior has been exercised with database row locks in the code path, but no production-database concurrency or large-volume performance test has been run. SQLite does not provide the same row-lock guarantees as PostgreSQL.

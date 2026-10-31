# Returns, Credit Notes, and Debit Notes — Implementation Report

## Summary

Implemented customer returns and supplier returns in Nexora ERP. Customer returns create sales credit notes; supplier returns create supplier debit notes. Posting updates the related accounting, tax, and inventory records while preserving links to the original business documents.

## Changes made

### Customer returns

- Returns can be created from posted sales invoices, with one or more original invoice lines.
- Each line records quantity, reason, condition, and whether sellable goods should be restocked.
- Posting validates cumulative return quantities against the source invoice, creates a linked sales credit note, reverses the original tax snapshot, and records a balanced reversal journal.
- Restocked items are added to the authorized warehouse, with the associated cost of goods sold reversed. Non-restocked items do not change stock or cost of goods sold.
- Customer return restocking now uses the original sale movement's recorded cost. Supplier returns remove stock at current warehouse weighted-average cost; journals record any supplier-credit-to-stock-cost variance.

### Supplier returns

- Returns can be created from posted supplier invoices or posted goods receipts for quantities that have not yet been invoiced.
- Posting checks cumulative returned quantities and available warehouse stock. It creates a linked debit note, reverses eligible purchase tax, creates the accounting correction, and records a single stock deduction.
- Reverse-charge tax corrections are reflected in the tax liability summary.

### Controls and user interface

- Added organization and branch scoping, role checks, period validation, duplicate-post protection, and restrictions on editing posted documents and movements.
- Added return lists, search and status filters, draft editing and cancellation, detail pages, and links from relevant sales and purchasing screens.
- Updated customer and supplier outstanding balances, invoice open amounts, and tax summaries to include posted notes.

## Implementation approach

The feature is implemented as a new Django `returns` app with data models, forms, views, routes, admin registration, and posting services. Those services use the existing sales, purchasing, inventory, accounting, banking, and tax components so each posted return creates linked records in the relevant ledgers. Database constraints and model/queryset guards protect note and movement relationships and prevent duplicate or post-posting edits.

The primary files are:

- `returns/models.py` — return and note records, relationships, and integrity protections.
- `returns/forms.py` and `returns/views.py` — scoped creation, editing, listing, posting, cancellation, and detail flows.
- `returns/services.py` — quantity checks, tax calculations, stock movements, journals, and idempotent posting.
- `tests/test_returns.py` — return workflow and regression coverage.
- `docs/RETURNS.md` — workflow rules and operational limitations.

## Verification

Verification was run on 2026-10-22:

- `manage.py check` — passed with no issues.
- `manage.py makemigrations --check --dry-run` — passed; no missing model migrations.
- `manage.py test` — 88 tests passed.
- `python -m pytest tests/ -q` — 42 tests passed.

The local `.venv` Python launcher referenced a missing Python installation, so the commands were run with the available Python 3.12.14 runtime and the project's existing `.venv\Lib\site-packages` on `PYTHONPATH`.

## Migrations and database

The returns workflow originally added `inventory.0004` and `returns.0001`; subsequent costing work adds `inventory.0005` and `inventory.0006`. Automated tests apply migrations only to temporary test databases. No migration was applied to `db.sqlite3` or another business database.

## Known limitations

- Supplier credits created from receipt-only returns remain aggregate supplier credits and are not allocated to specific invoices or payments.
- Goods receipt posting does not create a GRNI accrual journal, so receipt-only returns do not reverse an accrual.
- Goods receipts do not post a GRNI accrual; uninvoiced receipt values appear as reconciliation items. See [INVENTORY_COSTING.md](INVENTORY_COSTING.md) for the current costing method, legacy opening process and reconciliation limits.
- Concurrency was tested with the project's SQLite test database; PostgreSQL production locking behavior and large-volume workloads have not been validated.
- Tax reversals are internal accounting records; this feature does not file statutory returns or connect to government services.

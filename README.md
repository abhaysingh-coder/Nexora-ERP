# Nexora ERP

Nexora ERP is a Django-based ERP starter with tested accounting, invoice-posting, banking and taxation workflows. It is a partial implementation, not a complete ERP or production-ready system.

## Stack

- Python 3.12
- Django 5.1
- Django REST Framework
- SQLite by default for local development
- pytest + pytest-django for automated validation

## Getting started

```bash
python -m venv .venv
. .venv/bin/activate  # or .\.venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Then open http://127.0.0.1:8000/ and log in with a created superuser or via the Django admin.

## Included workflow

The application includes a working vertical slice covering:

- purchasing supplier invoices and inventory receipt
- stock ledger updates
- sales invoice posting
- customer returns with credit notes, GST reversals and optional sellable-stock restocking
- supplier returns with debit notes, GST reversals and warehouse stock deductions
- inventory reduction on dispatch
- customer receipt posting
- balanced accounting journal entries
- trial balance, general ledger, profit-and-loss and balance-sheet reports from posted journal lines
- banking account management and cash flow postings
- validated CSV statement import and explicit manual reconciliation
- organization-scoped GST setup and tax-aware sales/purchase posting

Authenticated sales screens support invoice drafting/posting and a sales-order lifecycle: create and confirm an order, partially dispatch from a selected warehouse, then create one invoice for each dispatched delivery. Dispatch updates stock once; posting its linked invoice does not deduct stock again. Purchasing supports editable draft purchase orders, confirmation/cancellation, partial goods receipts into a selected branch warehouse, and supplier invoices linked to posted receipt quantities. Receipt posting updates stock once; posting a linked supplier invoice does not add that stock again. Inventory supports product/warehouse setup, per-warehouse movement balances and low-stock filtering, auditable adjustments, and paired warehouse transfers. Customer receipts and supplier payments continue through Banking; invoice allocation uses the existing banking allocation workflow.

## Purchasing module

Purchase orders and goods receipts are organization- and branch-scoped. Users with purchasing or finance roles can create and revise draft orders, confirm or cancel eligible orders, record partial receipts, and post a receipt to its selected warehouse. Posted receipts update the existing stock-movement ledger atomically and can be retried without duplicating stock. Phase 4 authorization now resolves a non-superuser's organizations only from active memberships; a superuser must explicitly provision/activate memberships and roles in Django admin. Users with multiple eligible organizations select one through `/organizations/select/`.

Supplier invoices can be drafted from a posted receipt with quantities limited to received units not already covered by posted invoices. Posting uses the existing tax, accounting, and payables services and does not deduct receipt stock a second time. Direct supplier invoices remain available through the existing invoice screen.

Supplier returns and debit notes are supported from posted receipts and supplier invoices. Debit notes reduce supplier balances and invoice open amounts; receipt-only returns can leave a supplier credit balance. Per the explicit owner decision reaffirming Phase 3, goods receipts do not create a GRNI accrual journal; purchase invoices record purchase accounting, and inventory reconciliation includes unbilled receipts. Purchase invoice price differences update valued stock and allocate the presumed consumed share to COGS. Landed-cost allocation and purchase approval limits are not implemented.

Inventory uses perpetual weighted-average costing by organization, product and warehouse. Stock movements carry immutable valuation snapshots; delivery invoices use the dispatched movement cost, returns restore/remove valued cost, transfers carry the same value between warehouses, and positive stock adjustments require an explicit unit cost. The valuation report includes quantity, average cost, value, movement history, CSV export and inventory-control-account reconciliation. Legacy warehouse stock is marked unvalued until an authorized opening value is entered; aggregate stock without a warehouse allocation must be allocated in that workflow. Valuation history before its start date is incomplete and is not inferred from product cost prices.

See [docs/PURCHASING.md](docs/PURCHASING.md) for purchasing rules and [docs/INVENTORY_COSTING.md](docs/INVENTORY_COSTING.md) for costing assumptions, rounding, opening balances and limitations.

## Returns and credit/debit notes

Posted sales invoices support partial or full customer returns. A return creates a linked sales credit note and a separate balanced journal; tax reversals use the original line snapshots. Only lines marked sellable/restock are added back to an authorized warehouse. Posted goods receipts support unbilled supplier returns; invoiced goods are returned from their posted supplier invoice. Supplier debit notes reduce payable balances, reverse eligible purchase tax and deduct stock once.

Return lists, searches, status filters, draft editing/cancellation, scoped forms and detail pages are available from **Returns and credit notes**. Posting is limited to the existing sales or purchasing manager roles and rejects closed accounting periods. Customer and supplier balances include posted notes; note amounts are reflected in invoice open balances when linked to an invoice. Supplier credits from receipt-only returns remain aggregate supplier credits and are not yet allocated to specific invoices or payments.

See [docs/RETURNS.md](docs/RETURNS.md) for return accounting and [docs/INVENTORY_COSTING.md](docs/INVENTORY_COSTING.md) for how returns affect inventory value. The return and costing migrations are `inventory.0004` through `inventory.0006` and `returns.0001`; review the migration plan and back up the target before applying them to a business database.

## Banking module

The project now includes a functional banking area with:

- cash and bank account records
- customer receipts and supplier payments
- customer and supplier invoice allocations, including partial allocations and unapplied advances
- allocation reversals that retain the original record and post a correcting reclassification
- internal transfers
- payment reference tracking
- CSV statement import with duplicate detection
- imported rows stay unmatched until explicitly reconciled
- reconciliation suggestions based on account, signed amount, reference/description and a configurable date tolerance
- explicit reconciliation confirmation against a posted transaction for the same account, exact date and signed amount
- audited unreconciliation with a required reason

Receivables and payables registers show invoice balances, allocation history and unapplied advances, with search/filtering, ageing and CSV export. Suggestions never reconcile automatically. The date tolerance is configured with `BANK_RECONCILIATION_DATE_TOLERANCE_DAYS` (default `3`); confirmation remains exact-match only. Historical receipt records are retained in their legacy settlement mode and are not automatically assigned to invoices.

## Financial controls

- Organization-level financial years generate monthly accounting periods.
- Open periods can be closed only after blocker checks; reopening requires a reason and creates an audit event.
- Posted journals are rejected for closed periods. If an organization has not configured financial years, legacy journal posting remains allowed.
- Budgets can be set by period, account, branch, department and cost centre; the report compares them with posted journal activity.
- Fixed assets support categories, capitalization, straight-line or reducing-balance depreciation, branch transfers, disposal gains/losses and history.
- Duplicate asset depreciation for the same period returns the existing posting rather than creating a second journal.
- Year-end close previews blockers and proposed balances, closes income/expense accounts to retained earnings, and creates opening journals for the next financial year.
- Accounting reports can be filtered by financial year. Opening journals appear in that year's reports but are excluded from cumulative lifetime totals to prevent double-counting.

These controls are internal accounting workflows, not a complete approval matrix or statutory certification. A finalized financial year and its periods cannot be reopened through the application; corrections must be made with authorized journals in an open period. Closing requires an active organization-wide equity account and a consecutive next year with an open first period. Budget approval thresholds are not implemented. Fixed-asset capitalization currently posts against the default cash ledger account; confirm the acquisition workflow is suitable before using it for live books.

Finalized close records, generated journals and related audit history are protected through supported Django model and ORM operations. This is application-layer protection; it does not prevent database administrators, raw SQL, or external processes from changing the database. Database-level permissions or triggers would be needed for stronger enforcement.

Manufacturing, POS, CRM, HR/payroll, project management and customer service are not implemented. Sales quotations, landed-cost allocation and purchase approval limits are not implemented. Legacy aggregate stock is no longer auto-allocated at dispatch; it must be explicitly allocated and valued from the opening valuation workflow. Weighted-average valuation is implemented; FIFO, formal stock counts, reservations and batch/serial tracking are not. Sales order drafts do not yet have an edit workflow.

Legacy product balances with no warehouse allocation are shown as exceptions and block dispatch until an authorized opening valuation allocates the current quantity and supplies its explicit value. Legacy movement costs are preserved as snapshots but are not treated as a complete weighted-average history. Historical inventory reports before the recorded valuation start date are marked incomplete.

The accounting reports are organization/branch scoped and can be downloaded as CSV. Trial balance and balance sheet are cumulative through the selected end date unless a financial year is selected; profit-and-loss supports a date range or financial-year filter. Reversals are recorded as separate journal entries and posted lines are immutable through model methods.

## Taxation module

Taxation provides configurable tax codes, effective-dated component rates, GST registrations, product HSN/SAC classifications, GSTIN format checks, inclusive/exclusive price calculations, discounts, CGST/SGST/IGST/UTGST and optional cess/reverse-charge handling. A tax-aware draft invoice requires an origin registration or supplier state, an explicit place of supply, and a classification for every product line. Its line-level tax snapshot, invoice totals, stock movements and accounting postings are committed in the same database transaction. Existing invoices without product tax classifications retain their legacy total validation and posting behavior.

Authenticated organization-scoped pages provide tax setup, transaction history, sales and purchase registers, output/input summaries, date filters and CSV exports. Posted credit/debit notes create signed tax-snapshot reversals; reverse-charge supplier note tax is included in the output-tax liability summary. Reports are internal records derived from posted transactions; the application does not submit GST returns, issue e-invoices or e-way bills, or connect to government services.

After pulling model changes, apply migrations before starting the application:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
```

Back up any existing database before applying new migrations.

See [docs/GST_RULES.md](docs/GST_RULES.md) and [docs/DATABASE_DESIGN.md](docs/DATABASE_DESIGN.md) for assumptions and limitations.

## Verification

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

See [docs/BANKING.md](docs/BANKING.md) and [docs/FINANCIAL_CONTROLS.md](docs/FINANCIAL_CONTROLS.md) for workflow details and limitations.

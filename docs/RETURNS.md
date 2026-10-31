# Returns, credit notes and debit notes

Nexora's return workflows create separate posted correction records. They preserve the original sales invoice, goods receipt, supplier invoice, tax rows, stock movements and accounting journals.

## Customer returns

Create a customer return from a posted sales invoice. Each line refers to an original invoice line and records quantity, reason, condition and an explicit restock decision. Multiple partial returns are allowed. Posting locks the invoice and return, then checks all earlier posted returns so the total returned quantity cannot exceed the invoice-line quantity.

Posting creates a linked sales credit note. The journal debits sales revenue and output GST, then credits accounts receivable. Taxable values and each tax component are taken from the original `TaxTransaction` snapshot; if an older unclassified invoice has no tax snapshot, its stored amounts are allocated proportionally with cumulative currency rounding. The original invoice stays posted and unchanged.

Only lines marked for restocking increase stock. A return with any restockable line must use an active warehouse in the permitted organization/branch scope. Restocking adds the returned quantities at the original sale movement's recorded cost and reverses COGS at that same amount. If no source movement cost snapshot exists, posting rejects restocking rather than inventing a cost. Damaged/non-restockable goods create the credit note but do not increase sellable stock or reverse COGS. A stock movement and valuation entry record the return reference and can be created only once per product/warehouse.

## Supplier returns

Create supplier returns from either:

- a posted goods receipt, for quantities not yet included on a posted supplier invoice; or
- a posted supplier invoice, for quantities on its posted lines, including receipt-linked invoice lines.

Receipt-only forms limit quantities to unbilled received balances. Invoice-based forms limit quantities to both the invoice line's unreturned balance and, when linked, the receipt line's remaining received balance. Posting repeats these checks under database locks, verifies warehouse stock can cover the return, and deducts it once. Returned stock cannot make the warehouse balance negative.

Posting creates a supplier debit note and separate accounting journal. The stock movement removes current warehouse weighted-average cost. For an invoiced return, the journal debits accounts payable for the supplier credit, credits inventory at that weighted-average cost and eligible input GST, and reverses reverse-charge GST payable where applicable. A designated inventory adjustment gain/loss account balances the difference between supplier credit value and stock value removed. For an unbilled receipt return, the debit note still creates a supplier credit; the receipt and any later invoice remain unchanged.

The original invoice or receipt remains unchanged. Each debit note line records the source invoice line when available, while the return links to its source document. Signed tax transaction rows reverse the original purchase snapshot where one exists.

## Balances, periods and audit

Posted sales credit notes reduce the customer's outstanding balance and the open amount of their original sales invoice. Posted supplier debit notes reduce supplier outstanding and the open amount of a linked purchase invoice. A debit note based only on a goods receipt reduces the aggregate supplier balance and may produce a negative supplier balance (credit). The current banking allocation service applies cash/bank transactions to invoices; it does not yet allocate receipt-only supplier credits or customer credit balances to other invoices, and it does not automatically refund them.

Return posting uses the original branch and accounting period rules. Closed or missing configured periods reject posting. Each posted return is audited with source and note references. The return, note, journal, line and stock movement relationships preserve the correction history. Retrying a posted return returns the existing result; unique note references, tax source keys and stock movement constraints prevent duplicate effects.

## UI and routes

- `/returns/customers/` lists customer returns; create from `/returns/customers/new/<invoice-id>/`.
- `/returns/suppliers/` lists supplier returns; create from `/returns/suppliers/new/receipt/<receipt-id>/` or `/returns/suppliers/new/invoice/<invoice-id>/`.
- Each return detail page shows its source, line history, linked credit/debit note, journal and stock movements. Drafts can be edited or cancelled; only authorized sales/purchasing roles can post.

## Limitations

- GST is an internal snapshot reversal, not a statutory filing, legal determination or government integration. Older tax-free/manual invoices with no line tax snapshots cannot produce line-level statutory tax rows.
- Receipt-only supplier returns may create a debit supplier balance because goods receipts do not post a GRNI accrual. The debit note creates the supplier credit and stock/accounting correction; the later supplier invoice remains a separate transaction.
- The application does not allocate supplier credit notes to other invoices or automatically refund customer credits.
- Return stock quantities and values use the centralized weighted-average costing service. See [INVENTORY_COSTING.md](INVENTORY_COSTING.md) for legacy opening valuation, rounding, reconciliation and cost allocation assumptions.
- Receipt-only supplier credits can be posted without a GRNI accrual; the valuation report exposes uninvoiced receipts and their inventory-control-account difference as a reconciling item.
- Database row locks and this SQLite concurrent retry test do not establish production concurrency behavior. Run concurrency tests on the intended database backend, including PostgreSQL if used, before live posting.
- Application-layer model guards do not protect against direct SQL, database administrators or external processes. Use database permissions and operational controls for stronger immutability.

## Migration and verification

New return and valuation schema is in `inventory.0004` through `inventory.0006` and `returns.0001`. Automated tests apply migrations only to a temporary test database. No migration was applied to `db.sqlite3` or another business database. Review migration plans and back up/verify the target database before applying them.

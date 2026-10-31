# GST calculation rules and assumptions

## Internal calculation behavior

- Monetary values use Python `Decimal` and are rounded to two decimal places with `ROUND_HALF_UP`.
- Each `TaxRate` stores one percentage component (CGST, SGST, IGST, UTGST or cess) for an organization, tax code, optional state and effective date range. Rates are percentages, such as `9.0000` for nine percent, not fractions.
- The posting service chooses a rate effective on the invoice date. It prefers a state-specific row over an all-state row when both match that effective date.
- Same-state supplies require explicitly configured CGST plus SGST, or CGST plus UTGST for the application's configured Union Territory state codes. Different origin and place-of-supply codes require an IGST rate. A configured cess component is added when present.
- Intra/inter-state treatment is based on explicit origin and place of supply. Missing or malformed state codes are rejected; GSTIN prefixes are not used to guess the transaction's place of supply.
- Inclusive pricing decomposes the gross line amount into taxable value and tax. Exclusive pricing adds calculated tax to taxable value. The line discount is deducted before tax calculation.
- The posted line snapshot preserves the original line value, discount, place-of-supply context and each applied component percentage so later rate edits do not rewrite posted tax details.
- Exempt and zero-rated classifications return zero tax. They are preserved as separate classification flags in the posted tax snapshot.
- For reverse charge, calculated tax is recorded but excluded from the supplier/customer invoice total. Purchase posting records the tax liability and the configured eligible input credit in balanced accounting entries.
- Reverse charge is only applied to purchase documents using the product classification, supplier setting or recipient registration setting. It is not applied as seller output tax on sales.
- A tax-aware invoice needs an organization/branch GST registration for a sale, a supplier state for a purchase, a place of supply, and a product tax classification on every line.
- Posting a tax-aware draft recalculates its header totals from the line snapshots. Posted invoices are idempotent: subsequent `post()` calls do not recalculate tax or create extra journal entries, stock movements or tax rows.
- Invoices without product tax classifications retain the prior invoice-total validation and posting behavior.
- Posted sales credit notes and supplier debit notes create signed negative tax rows from the original invoice-line snapshot. Partial notes use cumulative proportional rounding so multiple returns cannot reverse more than the original line tax.
- Reverse-charge supplier debit notes reverse the GST payable and eligible input-credit sides in the journal. The output-tax liability summary includes purchase reverse-charge tax and its negative debit-note adjustment; the purchase register continues to show the negative input-tax adjustment where eligible.

## Compliance boundaries

These rules are an internal software calculation contract, not a legal interpretation of current GST law. Users must obtain and maintain applicable rates, classifications, place-of-supply treatment, cess, reverse-charge status and input-tax-credit eligibility using current official guidance and qualified advice. The software does not verify rates against government data.

The reports are prepared from Nexora's posted transaction snapshots. They are not filed returns, do not constitute government acceptance, and do not imply GSTN/CBIC certification. There is no live return submission, e-invoicing, e-way bill or government API integration.

## Known limitations and follow-up

- Older/manual invoices without a `TaxTransaction` line snapshot can reverse their stored document tax amount proportionally for the journal, but cannot create a line-level statutory tax-register reversal without a source tax code and component snapshot.
- Export schemas, blocked-credit statutory rules, tax period closure, interest/late fees, or complete statutory reconciliation are not implemented.
- Place of supply is entered explicitly on an invoice; the application does not infer place-of-supply rules for every goods or services scenario.
- The UTGST state-code set is an application configuration assumption and should be confirmed for the organization's specific transactions.
- Cess is currently modeled as an optional percentage of taxable value; per-unit cess and specialized cess formulas are not supported.
- Component rates and exemptions are only as accurate as the organization's configuration.

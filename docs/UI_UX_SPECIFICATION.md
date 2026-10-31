# Nexora ERP UI/UX specification — Phase 1

## 1. Product experience

Nexora is a transaction workspace for Indian organizations with multiple branches. The UI should make three things clear on every business page: **which organization/branch and date scope is active, what state the document is in, and which action is allowed next**. Keep the current original Nexora identity and reusable server-rendered Django templates. Do not imitate Wings screens, logos, artwork, proprietary wording or page layouts.

The scope catalog in `FEATURE_MATRIX.md` is the design baseline. The exhaustive route and screen register is `SCREEN_INVENTORY.md`; screen IDs there map to feature IDs and acceptance criteria here. Screens marked current are evidence of a route/template, not a claim that every described target interaction is already present.

## 2. Shared visual and layout system

### Design tokens

Reuse `static/css/styles.css` as the base. It already defines an original teal/green palette, pale canvas, white cards, restrained borders/shadows, Inter/Segoe UI typography, visible keyboard focus, status/message styles, responsive grids, form controls and horizontally scrollable tables.

| Token | Existing value | Use |
|---|---|---|
| Ink | `#172b2a` | Main copy and headings |
| Muted | `#687b79` | Hints, timestamps and secondary labels |
| Line | `#e4ece9` | Dividers, card and table borders |
| Surface / canvas | `#ffffff` / `#f3f7f5` | Cards/forms and app background |
| Brand / dark | `#14766f` / `#102f2e` | Primary actions/links and sidebar |
| Brand soft | `#e6f4f1` | Selected states and quiet highlights |
| Warning | `#a96713` | Review-needed states; pair color with text/icon |

Use semantic HTML, readable contrast, consistent spacing, page title + one-line purpose, compact status chips with text, and the existing shared button/card/table styles. Do not add a chart or an icon-only control without a defined data/action purpose. Show monetary amounts with organization currency and consistent currency precision; expose full precision where the inventory cost snapshot requires it.

### App shell and navigation

- **Desktop:** persistent left sidebar, content region, workspace header, page heading, breadcrumbs, optional actions, then page content. Retain the existing 250px sidebar, dark Nexora branding, Workspace/Business/Finance groupings and current palette. Group invoices under Sales/Purchases; keep orders, receipts, returns, stock valuation, accounting reports, banking and tax tools discoverable under their module.
- **Header:** show organization, branch selector when authorized, financial year/period selector where it affects posting/reporting, and signed-in user/role. Current header only prints an organization label and a “Secure session” label; branch/year selectors and role context are design requirements, not current functionality.
- **Breadcrumb:** add linked ancestors such as `Purchases / Purchase orders / PO-…`; current pages use eyebrow text but no breadcrumb navigation. Breadcrumb labels must not expose unauthorized document names.
- **Mobile:** below 760px, use a keyboard-operable menu toggle and a collapsible navigation drawer; keep workspace, page title, primary action and status visible. Existing CSS turns the sidebar into a tall block with a 48vh maximum; document this as an existing navigation usability defect to resolve in a later implementation phase. Form fields stack; tables remain horizontally scrollable with the row identifier pinned or repeated where feasible; avoid hiding amounts/actions without an alternate access path.
- **Current and planned screens:** apply the same shell to every `SCR-*` entry in `SCREEN_INVENTORY.md`. A role may not see a navigation link, but every route must also enforce server-side access.

### Forms, tables and feedback

- Forms: label every input, group related fields, mark required/optional, retain submitted valid values on errors, show field-level and form-level errors, and prevent double submission. Separate header data from repeated line items. Provide explicit Save draft and state-transition actions; explain whether a save posts financial/stock effects.
- Tables: include a descriptive caption/heading, column headers, formatted date/amount/quantity, linked document number, visible status, useful empty text, and pagination. Add sorting only for meaningful sortable columns. Preserve all active search/filter/sort parameters across pages. Current generic pagination links drop filters in some screens.
- Filters: put frequent filters above the table; include organization/branch, document status, date range and party/product where relevant. Provide Apply and Clear. Show active filter count and date basis. Invalid ranges should leave data unchanged and explain the error.
- Status and feedback: use existing success/error message styles and add info/warning states. Distinguish Draft, Confirmed, Partially received/dispatched, Posted, Cancelled, Reversed, Closed and Incomplete valuation. Empty states say why and offer an authorized next step. Server-rendered pages have no asynchronous loading behavior today; use a progress indicator only for actual asynchronous import/export/posting jobs, and preserve a recoverable error state.
- Confirmation: require an accessible confirmation step/dialog for irreversible/high-impact actions: invoice posting, receipt/dispatch, stock adjustment/transfer, allocation reversal, reconciliation/unreconciliation, period close/reopen and year-end close. State the document, amount/quantity, branch, stock/accounting/tax side effects and source. Require a reason where policy needs one. The server validates again and accepts mutations only by POST with CSRF protection. Current screens use immediate POST buttons for several transitions and no consistent confirmation pattern.
- Export: place CSV actions beside the relevant report; honor the exact active filters and authorization, label the exported scope/period, and present a filename/result or actionable error. Never imply a report export is a statutory filing.

## 3. Role and access expectations

The role names below describe the target responsibility groups; they do not claim a finalized permission policy. Actual guards are inconsistent by module (see `ROLE_SCREEN_MATRIX.md`). Replace role-name guesses with an approved capability matrix before Phase 2.

| Target role | Primary work | Sensitive actions |
|---|---|---|
| Owner / organization administrator | Organization, branch, users, roles, broad read | Provision access; configuration; no silent edits to posted records |
| Finance manager / accountant | Journals, reports, periods, tax, budget, assets, banking | Post/reverse, close/reopen, export, reconciliation |
| Sales / sales manager | Customers, quotations, orders, delivery, sales invoices, customer returns | Confirm/dispatch/post/credit; approval separation to be decided |
| Purchasing / purchase manager | Suppliers, requisitions, POs, receipts, purchase invoices, supplier returns | Confirm/receive/post/debit; approval separation to be decided |
| Inventory manager / warehouse operator | Product, warehouse, movement, counts, transfer, adjustment | Value-changing stock actions, often with finance approval |
| Cashier / bank reconciler | Receipts/payments, account transactions, allocations, statements | Allocation/reconciliation/reversal separation to be decided |
| Branch manager | Branch operations, branch dashboard and reports | No cross-branch data; no organization-level close/configuration unless separately granted |
| Auditor / read-only viewer | Scoped source documents, ledger, reports and audit | Read/export only; no mutation |
| System / integration administrator | Runtime and integration configuration | Credentials, API clients, backup and deployment controls; segregate from business posting |

Enforce least privilege at the route/service and object-query level. A hidden button is not authorization. Resolve organization membership, branch scope, inactive state and permitted transition on every request. Require explicit review of create/edit/approve/post/reverse/export capabilities and dual control for material transactions.

## 4. Screen interaction contracts

The concrete path, fields, actions, current evidence and screen-specific acceptance links are recorded for each existing and proposed screen in `SCREEN_INVENTORY.md`. These contracts define their intended visible behavior.

### Access, organization and master screens

- `SCR-AUTH-01`: centered responsive sign-in form; User ID and password; sign-in submit; invalid credentials shown without revealing whether an ID exists; keyboard focus; access/recovery guidance. After login route only to an authorized organization. A logged-out state must not leak prior screen data.
- `SCR-ORG-01/02`, `SCR-IAM-01/02`: list/search/status table plus right-sized create/edit form or detail page. Display branch, status and current owner. Deactivation/role changes preview affected users/records and ask for confirmation. User reset links expire; passwords/tokens are never rendered. Role matrix is grouped by module and action, not one unexplained “admin” toggle.
- `SCR-MST-01–04`, `SCR-ACC-10`: searchable masters with stable code/name, active status, tax and branch context. Forms validate required fields, duplicate codes, GSTIN/state/HSN-SAC rules and references before save. Detail pages show related invoices/orders/stock or journal references. Deactivation is blocked or explained when a master is referenced; history remains readable.

### Dashboard and sales

- `SCR-DASH-01`: overview cards for posted sales, posted purchases, aggregate cash/bank, receivables, payables, low stock and **authoritative inventory valuation**. Put selected organization/branch/date basis above the cards. Each card links to its filtered source report. Journal activity shows date/reference/narration and source link. Label valuation completeness and unvalued exceptions. Empty organization setup has a purposeful onboarding state. Do not duplicate an alternate dashboard at `/` with different data context.
- `SCR-SAL-01/02`: enquiry and quote lists with search, owner, status, follow-up/expiry and date filters; detail timeline and source links. Quote detail presents revision, terms, tax estimate and validity. Create/revise/accept/decline/export/convert actions carry clear status and confirmation. Conversion creates one linked order and cannot silently duplicate it.
- `SCR-SAL-03/03-E`: order list filtered by date/customer/status/branch and searchable by number. Detail shows ordered, delivered, invoiced, reserved and remaining quantities plus delivery/invoice children. Draft edit is explicit; confirmation checks credit/stock policy; cancellation is only available before fulfillment and describes consequences.
- `SCR-SAL-04`: dispatch page shows source order, warehouse availability, open quantities, promised date and selected quantities. Reject quantities greater than open order or available stock. Confirmation names warehouse and quantities. Successful dispatch shows delivery note and stock movement reference; retry does not deduct twice.
- `SCR-SAL-05-L/F/D/FD`: invoice table supports number/customer/date/status filters and sort/page/export; detail shows party, branch, tax registration/place of supply, lines, discounts, tax snapshot, totals, posting/journal/stock references and source links. Draft form validates line and header totals before saving. Post confirmation shows financial/stock/tax impact; posted invoice is immutable and any correction uses a return/credit note. Receipt action opens banking workflow with invoice/counterparty prefilled when authorized (currently not implemented).
- `SCR-SAL-06`: price/target/analysis filters include period, branch, party, product and salesperson. Display metric definitions and prior/current comparison basis. Show margin/discount only to authorized roles. Exports use the same filters. Policies for discounts and price overrides must be approved before implementation.

### Purchasing

- `SCR-PUR-01/02`: requisition/enquiry lists distinguish requested, submitted, approved, rejected and converted. Request includes requester, branch, required-by date, products, quantity, budget reference and rationale. Approval outcome and reason link to resulting PO. Quote comparison shows supplier, price, tax, lead time and terms side by side.
- `SCR-PUR-03/03-F/03-D`: PO list filters by supplier/branch/date/status; detail shows ordered/received/billed/outstanding totals, source requisition/quote and receipt/invoice children. Only drafts are editable. Confirm/cancel has a preview; cancellation is blocked once posted receipt exists.
- `SCR-PUR-04/04-F/04-E/04-D`: receipt entry shows source PO quantities and cumulative prior receipts. Capture received, accepted/rejected and optional quality evidence if policy requires. Warehouse/date/quantity validation precedes posting. Confirmation names warehouse and stock value basis; posted receipt links its inventory valuation/movement. Invoice-from-receipt shows received quantity less already billed quantity and a visible price variance.
- `SCR-PUR-05-L/F/D/FR`: supplier invoice list/detail mirrors sales invoice hierarchy, displaying supplier reference, linked order/receipt, tax/place-of-supply, payable, posted journal and stock handling. Receipt-linked invoice does not duplicate receipt stock. Invoice posting confirmation exposes tax/payable/cost variance. Edits are draft-only; corrections use supplier debit note/return.
- `SCR-PUR-06/07`: landed-cost preview shows allocation basis and per-item effect before post; supplier scorecards state formulas and periods. Do not silently change historical item cost. Exports carry period/branch filters.

### Inventory

- `SCR-INV-01`: product table fields are SKU, name, category, UOM, available qty, weighted-average cost, value, reorder level and valuation state. Filters: query, branch/warehouse, low stock and valuation completeness. Product row opens product/stock detail and movement references. Low-stock calculation must use the same signed movement scope and inclusive threshold as inventory.
- `SCR-INV-02/03/06`: create product/warehouse/opening-value forms group identity, tax/UOM, branch/location and status. Opening value requires explicit warehouse allocation, quantity, unit value, date, reference and approver; show before/after valuation and linked audit. Do not imply opening valuation created a GL journal unless it did.
- `SCR-INV-05`: report has as-of date, branch and warehouse filters; opening/on-hand quantity, average cost, inventory value, completeness, movement history and inventory-control-account difference. Unknown value is an explicit exception, never replaced with product list cost. Export has the exact filters and an incomplete-history marker.
- `SCR-INV-07/08`: transfer/adjustment forms show source stock and destination, signed quantity, cost basis, reason/reference and period. Confirmation summarizes stock/accounting effects. Require reason and sufficient source stock; positive adjustment asks for explicit unit cost. Detail/ledger links show paired movement or adjustment journal.
- `SCR-INV-09–13`: count takes a dated snapshot; show expected/count/variance and independent review. Batch/serial searches trace receipt-to-dispatch-to-return. Bin capacity, UOM conversion and interbranch shipment/receipt are visible and linked. Provisional/review states do not affect available stock until policy-defined commit.

### Accounting and banking

- `SCR-ACC-01–04`: shared report filter panel states date basis, year, branch and whether totals are cumulative. Trial balance shows debit/credit/net; GL shows opening and running balance, journal reference and drilldown; P&L shows selected period; balance sheet shows assets/liabilities/equity and current earnings. Show only posted ledger data. CSV exactly matches filters, scope and displayed totals. Invalid date range keeps the last valid report and shows an error.
- `SCR-ACC-05`: cash-flow report shows opening/closing cash tie-out, operating/investing/financing, period comparison and calculation policy; support source-ledger drilldown.
- `SCR-ACC-06/07`: period screen clearly separates open/closed/finalized. Show blockers with direct links to drafts/unmatched statements. Reopen requires reason and records event. Year-end page is a review-first workflow: read-only preview, validation list, proposed closing/opening journal, explicit final confirm and immutable completion state. Confirmation states that UI close cannot be reopened.
- `SCR-ACC-08/09`: budget table shows budget/actual/variance/percent and dimensions, with create/edit/export controls allowed by role. Asset register shows acquisition basis, accumulated depreciation, NBV, method, useful life, branch, history and source journal. Capitalize/depreciate/transfer/dispose each previews journal and requires confirmation; flag current default-cash capitalization assumption until corrected by a later phase.
- `SCR-ACC-10/11`: chart setup and journal forms are proposed. Journal editor has one row per account with debit/credit, add/remove line, live balance indicator, branch/date/period and source reference. Posting is blocked until balanced and authorized; reverse creates a linked counter-entry, never edits posted lines.
- `SCR-BNK-01/02/03/04`: account overview must show account balances with a correctly labeled aggregate. Transaction form specifies direction, account, counterparty, date, reference and amount; transfer has source and destination. Detail shows journal, allocations, unapplied amount and history. Allocation validates same party/invoice and available balances; reversal requires reason and shows correcting entry.
- `SCR-BNK-05/08`: statement import begins with account/file and documents accepted headers/encoding; validate the whole file before import. Results show imported/duplicate/rejected counts and unmatched status. Reconciliation compares statement line to candidate transaction and explains suggestion; user chooses exact match and confirms; mismatches remain unmatched. Undo requires reason and audit history. Cheque/PDC features remain proposed.
- `SCR-BNK-06/07`: AR/AP registers provide party, invoice, invoice/due date, total, open amount, ageing bucket and allocation/note history, with search/date/status/page/export. Clicking allocation opens relevant banking transaction and invoice. Clearly distinguish unapplied advances and supplier credits.

### Tax, audit, reports and extensions

- `SCR-TAX-01/02`: setup home shows configured codes/rates effective date, registration/branch, and product HSN/SAC coverage. Create/update forms identify component, percentage, state, validity, classification and source. Configuration changes are permission checked and eventually version/audit tracked; they never rewrite posted tax snapshot.
- `SCR-TAX-03–08`: sales/purchase register and input/output/liability summary support dates, branch and document links; show internal-report banner. Liability summary is not marked “filed”. Proposed statutory screen separates internal snapshot, portal import/reconciliation, generated return data and external submission status; require legal/provider/security approval before exposing submit.
- `SCR-AUD-01`, `SCR-RPT-01`: authorized audit/report search uses date, user, action, object, branch and status. Source links open only authorized records. Export records filters, row count and actor; read-only audit data cannot be changed from the UI.
- `SCR-OPS-01/02/03`, `SCR-API-01`: approval inbox explains requester/amount/impact and requires approve/reject reason; notification/document screens retain source-object permissions; operations screens expose safe health/backup evidence but never secrets; integration registry shows scope, version, last run and failures with credential rotation rather than displaying keys.
- `SCR-RET-01–07`, `SCR-DIST-01`, `SCR-MFG-01`, `SCR-HR-01`, `SCR-CRM-01`, `SCR-PROJ-01`: use the screen-specific rows in `SCREEN_INVENTORY.md`. All records show lifecycle state, source/child document links, scoped role actions and a recovery path for validation/posting failure. No decorative or nonfunctional actions.

## 5. Cross-screen state and accessibility acceptance

Every screen must provide: page title, current organization/branch context, useful empty state, validation failure state, success feedback, authorization-denied behavior, keyboard-visible focus, accessible labels and table headings, and responsive content at 360px, tablet and desktop widths. Loading is specified only for long-running asynchronous work. Confirm dialogs have a clear cancel action and focus management. No state change depends on color alone. Long identifiers and monetary values wrap or scroll without covering actions.

Acceptance traceability: feature-level criteria are `AC-*` in `FEATURE_MATRIX.md`; each current/proposed screen lists the relevant criterion. A feature is not **Verified** merely because a mockup or page template exists: role authorization, validation, transaction persistence, duplicate safety, linked records, empty/error states and documented tests must all pass in the target database/runtime.


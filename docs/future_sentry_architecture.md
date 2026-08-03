# SOLARA WMS future state

Status: architecture baseline for TEST implementation. Production activation is
not authorised.

## Scale and rollout assumptions

- Hyderabad is the only active fulfilment warehouse today and is the pilot.
- Capacity target is 4x July 2026: approximately 3,200-6,000 D2C parcels/day,
  plus B2B work and short peaks above that range.
- Additional warehouses must be configuration, not a schema migration. Every
  location, policy, task, balance and permission is scoped to an ERPNext
  Warehouse.
- ERPNext remains the system of record for financial inventory. WMS locations
  are physical sub-locations inside an ERPNext warehouse.

## What is adopted from Sentry WMS

| Sentry pattern | SOLARA implementation |
|---|---|
| Warehouse-scoped locations and users | ERPNext Warehouse is mandatory on every WMS location and task |
| Preferred bins | `WMS Item Location` with Home/Reserve/Overflow roles and priority |
| Directed route | `Warehouse Bin.route_sequence`, scoped by warehouse |
| Explicit line states | Expected, scanned, short and exception quantities; never default missing scans to expected |
| Atomic quantity caps | Conditional updates must prevent picked/allocated quantity exceeding demand |
| Deterministic locks | Lock item-location balance rows in warehouse/item/bin order |
| Idempotent scanner writes | One device-generated key per scan command; same key/body replays the prior result |
| Approval boundary | Floor completion may create ERPNext drafts; only authorised reviewers submit |
| Transactional outbox | A later integration phase emits committed changes asynchronously, not inside scanner latency |
| Multi-warehouse transfer approval | Source pick, in-transit, target receipt and variance approval are separate states |

We do not copy Sentry's item, order, warehouse, identity or ERP connector
masters. Atlas already owns those. We also do not import its React/Flask stack;
the existing SOLARA warehouse scan PWA and Frappe permissions remain the user
surface.

## Ledger boundary

There are two deliberately different truths:

1. ERPNext Stock Ledger: quantity and valuation at ERP warehouse level.
2. WMS physical state: where the units are inside that warehouse and whether
   they are available, allocated, picked, packed, held or in transit.

An internal HYD Home-to-Reserve movement changes only WMS physical state. It
must not create a same-warehouse Stock Entry. A movement between two ERPNext
warehouses prepares a draft Material Transfer for review. Cycle-count variances
prepare a draft Stock Reconciliation. Receiving prepares a draft Purchase
Receipt. No floor action submits an accounting or stock document.

The current Shopify automation submits Delivery Notes early to mint AWBs. WMS
must therefore not interpret ERPNext `actual_qty` as the precise on-floor state
of released D2C goods. Allocation/pick/pack/dispatch state is maintained
separately and reconciled to ERPNext at defined control points.

## Target data model

### Master data

- `Warehouse Bin`: physical location, warehouse, zone, barcode, dimensions and
  route sequence.
- `WMS Item Location`: SKU + warehouse + bin placement policy, one active role
  (Home/Reserve/etc.) per SKU and warehouse, min/max/replenishment rules.
- Future `WMS Warehouse Profile`: time zone, operating calendar, cut-offs,
  capacity and allowed users for each active warehouse.

### Operational state

- `WMS Bin Balance` (Phase 1 implemented): compact mutable summary per
  warehouse/item/bin. Available is derived from on-hand minus allocated minus
  hold.
- `WMS Work` and `WMS Work Line` (Phase 1 implemented for Pick and
  Replenishment): warehouse-owned bounded demand with Allocated, In Progress,
  Completed, Short and Cancelled states. Conditional updates prevent
  over-allocation and over-picking.
- `WMS Work Event` (Phase 1 implemented): append-only Allocate, Release, Pick
  Scan, Pick Shortage and Complete Replenishment command evidence.
- `WMS Movement` (Phase 1 implemented): append-only idempotent opening-balance
  and same-warehouse internal-move evidence, including before/after quantities.
- Scanner pick execution (Phase 1 implemented): validates the assigned
  warehouse/bin/SKU, consumes only the remaining work allocation and records a
  compact movement/event pair. A mandatory coded shortage closes the line and
  releases its unexecuted reservation. Picked quantity leaves the source-bin
  physical balance and remains represented by executed work until later
  pack/dispatch integration.
- `WMS Pack Handoff` (Phase 1 implemented, opt-in): matches completed Pick work
  to one Delivery Note parcel by warehouse, AWB, SKU and exact quantity. Pack
  Verify consumes each work once; dispatch requires the resulting handoff while
  the pilot gate is active. The gate defaults off and is ineffective in Disabled
  mode, preserving the established D2C and Shopify flow.
- Future high-volume archive: retain detailed scanner commands hot for a short
  window, then archive outside the constrained Atlas database.

The high-volume scan stream, photographs and verbose request/response payloads
must not be retained indefinitely in Atlas. Atlas was already near its database
quota in July 2026. Business documents and material approvals follow audit
retention; operational telemetry follows a short hot-retention plus cold-archive
policy.

## Multi-warehouse model

- HYD is configured first and remains the default only during the pilot.
- No implicit fallback is allowed when more than one warehouse is active. An
  order/task/import must resolve exactly one source warehouse or stop.
- Each warehouse has independent location codes, route sequence, capacity,
  replenishment and operator access.
- Network allocation is a later phase: choose the fulfilment warehouse using
  stock availability, serviceability, cut-off and workload, then freeze that
  choice on the work order.
- Inter-warehouse transfer states: Draft -> Approved -> Source Picking -> In
  Transit -> Target Receiving -> Closed. Source and target quantities are
  independently confirmed; discrepancies require approval.

## Capacity design

- Scanner endpoints: target p95 under 500 ms excluding label/photo upload.
- Every scanner mutation: idempotency key, bounded payload and one database
  transaction.
- Scheduler work: bounded batches and wall-clock budgets; continuation cursor
  instead of unbounded `limit_page_length=0` on hot paths.
- Locks: narrow balance/work-line rows only; never warehouse-wide or Stock Ledger
  scans in a floor request.
- Dashboards: pre-aggregated/short cached metrics; no live scan of years of task
  rows.
- Photos: object storage with expiry; Atlas retains the evidence reference.

## Delivery phases and gates

### Phase 0 - safety and master data (current local change)

- Remove every legacy automatic `.submit()` from WMS floor controllers.
- Reject blank cycle-count lines rather than assuming they matched.
- Prevent internal picks from generating same-warehouse Stock Entries.
- Add route order and warehouse-scoped SKU placement policy.
- Add a single WMS operating-mode gate defaulting to Disabled; only deliberate
  TEST `Draft Handoff` mode permits legacy completion methods to prepare drafts.
- Validate locally, then install/migrate on `solara-test` only.

Exit: no legacy floor method can submit a stock/accounting document; HYD bins
and top SKUs can be configured without affecting stock.

### Phase 1 - HYD shadow mode (balance/movement foundation implemented)

- Implement physical balance, idempotent opening count and atomic internal-bin
  movement. Both endpoints require Shadow/Draft Handoff mode, System Manager,
  and the configured pilot warehouse. They never create ERP stock documents.
- Implement explicit work lines, pick allocation/release, and policy-directed
  Reserve-to-Home replenishment allocation/completion.
- Implement bounded scanner pick execution with wrong-bin/SKU hard blocks,
  idempotent partial scans, atomic completion and coded shortage closure.
- Integrate an opt-in completed-pick handoff into the existing AWB-level Pack
  Verify and dispatch scans without changing Shopify release, AWB creation,
  Delivery Note submission or financial inventory.
- Import measured HYD layout and top fast-pick SKUs.
- Run receiving, replenishment, batch pick and blind cycle counts in shadow mode;
  compare to current process without driving ERP documents.

Exit: two weeks with no negative WMS balances, duplicate scan mutations or
unexplained warehouse-level variance; p95 scan latency within target.

### Phase 2 - HYD controlled execution

- Enable directed replenishment and pick work for selected SKUs/lines.
- Integrate existing D2C pack verify and dispatch scans.
- Draft-only ERP handoff and exception approval queues.

Exit: throughput and accuracy meet agreed baseline, rollback drill passes, and
Shopify order/inventory flows remain unchanged.

### Phase 3 - second warehouse

- Configure a new warehouse profile and locations.
- Test explicit order routing and inter-warehouse transfers on TEST.
- Activate only after HYD controls are stable and reconciliation is signed off.

## TEST acceptance suite

1. Same idempotency key/body twice changes quantity once; different body returns
   a conflict.
2. Two simultaneous pickers cannot exceed allocated quantity.
3. Internal bin move changes no ERPNext Stock Ledger Entry.
4. Inter-warehouse movement creates a draft Stock Entry only.
5. Count with an uncounted line cannot complete.
6. Count variance creates a draft Stock Reconciliation only.
7. Receipt creates a draft Purchase Receipt only.
8. Operator cannot read or act in an unassigned warehouse.
9. Product Bundles expand to physical components before allocation.
10. Existing Shopify release, pack verify and dispatch behaviour is unchanged.
11. 4x synthetic load meets latency/error targets without lock-wait growth.
12. Disabling the WMS execution gate returns operations to the existing process.

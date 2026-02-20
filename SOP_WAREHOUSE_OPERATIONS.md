# SOLARA WMS - Standard Operating Procedures
## Warehouse Management System for ERPNext

**Company:** Win The Buy Box Private Limited
**Version:** 1.0 | **Date:** February 2026
**Prepared for:** Warehouse Operations Team

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Daily Operations Summary](#2-daily-operations-summary)
3. [SOP-01: Inbound Receiving (B2B Purchase Orders)](#sop-01-inbound-receiving)
4. [SOP-02: B2B Order Fulfillment (Amazon/Blinkit/Zepto)](#sop-02-b2b-order-fulfillment)
5. [SOP-03: B2C Order Fulfillment (Shopify - 1000+ orders/day)](#sop-03-b2c-order-fulfillment)
6. [SOP-04: Internal Stock Transfer](#sop-04-internal-stock-transfer)
7. [SOP-05: Cycle Counting & Stock Verification](#sop-05-cycle-counting)
8. [SOP-06: Stock Freeze (Quality Hold / Investigation)](#sop-06-stock-freeze)
9. [SOP-07: Pack Station Operations (Scan-to-Verify)](#sop-07-pack-station)
10. [SOP-08: Bin Location Management](#sop-08-bin-management)
11. [ERPNext Documents Created Automatically](#erpnext-auto-documents)
12. [Troubleshooting & Error Handling](#troubleshooting)

---

## 1. System Overview

### What is SOLARA WMS?

SOLARA WMS is a custom Frappe/ERPNext application that adds warehouse management capabilities on top of ERPNext's stock module. It provides:

- **Bin locations** (aisle/rack/shelf/level) within ERPNext warehouses
- **Directed tasks** for warehouse operators (putaway, pick, pack, count, transfer)
- **Inbound receiving** with ASN (Advanced Shipping Notice) and variance tracking
- **Wave picking** to consolidate multiple orders into efficient picking batches
- **Outbound dispatch** with full shipping workflow
- **Inventory control** via cycle counting and stock freezing
- **Pack station** with barcode scan-to-verify

### How It Connects to ERPNext

SOLARA WMS sits on top of ERPNext. It does NOT replace ERPNext's stock management - it adds warehouse-level operations and then creates the standard ERPNext documents automatically:

```
SOLARA WMS                          ERPNext (Auto-Created)
-----------                         ----------------------
WMS ASN (Receiving)          --->   Purchase Receipt
WMS Task (Putaway/Pick)      --->   Stock Entry (Material Transfer)
WMS Task (Count)             --->   Stock Reconciliation
WMS Wave Pick                --->   WMS Task (Pick) --> Stock Entry
WMS Dispatch                 --->   Delivery Note + Packing Slip + Shipment
WMS Cycle Count              --->   Stock Reconciliation
WMS Pack Station             --->   Delivery Note + Packing Slip
```

### Your Order Channels

| Channel | Type | Volume | Order Source in ERPNext |
|---------|------|--------|----------------------|
| Amazon | B2B | 1000s of units per order | Sales Order (synced) |
| Blinkit | B2B | 1000s of units per order | Sales Order (synced) |
| Zepto | B2B | 1000s of units per order | Sales Order (synced) |
| Shopify | B2C | ~1000 orders/day, 1-5 units each | Sales Order (synced via Shopify connector) |

---

## 2. Daily Operations Summary

### Morning Shift Start

| Time | Activity | Who | WMS Module |
|------|----------|-----|------------|
| Start | Check pending ASNs for expected deliveries | Receiving Team Lead | WMS ASN |
| Start | Review overnight Shopify orders synced as Sales Orders | Dispatch Lead | ERPNext SO List |
| Start | Create Wave Pick for B2C orders | Dispatch Lead | WMS Wave Pick |
| Start | Check any B2B orders from Amazon/Blinkit/Zepto | B2B Fulfillment Lead | ERPNext SO List |

### Throughout the Day

| Activity | Trigger | Who | WMS Module |
|----------|---------|-----|------------|
| Receive shipments | Truck arrives | Receiving Team | WMS ASN |
| Putaway received goods | After receiving | Putaway Operator | WMS Task (Putaway) |
| Pick B2B orders | B2B SO created | Pick Operator | WMS Dispatch or WMS Task |
| Pick B2C wave | Wave released | Pick Operator | WMS Wave Pick + Task |
| Pack B2C orders | After picking | Pack Operator | WMS Pack Station |
| Dispatch shipments | After packing | Dispatch Team | WMS Dispatch |
| Cycle count | Scheduled | Count Operator | WMS Cycle Count |

### End of Day

| Activity | Who | WMS Module |
|----------|-----|------------|
| Complete all open pick tasks | Pick Team | WMS Task |
| Close out pending dispatches | Dispatch Team | WMS Dispatch |
| Run evening cycle count (if scheduled) | Count Team | WMS Cycle Count |

---

## SOP-01: Inbound Receiving

### When to Use
A supplier shipment (truck/courier) arrives at the warehouse with goods against a Purchase Order.

### Prerequisites
- Purchase Order exists in ERPNext (submitted)
- Receiving warehouse and bin locations are set up

### Step-by-Step Procedure

#### Step 1: Create ASN (Warehouse Manager or Receiving Lead)

1. Go to **WMS ASN** > **+ Add WMS ASN**
2. Fill in:
   - **Supplier**: Select the supplier
   - **Purchase Order**: Link to the PO
   - **Receiving Warehouse**: Select your inbound warehouse (e.g., "Stores - WTBB")
   - **Receiving Bin**: Optional - default bin for unloading (e.g., "RCV-01-01-1")
   - **Expected Arrival**: Date the shipment is expected
   - **Carrier**: Shipping company name
   - **Tracking No**: Waybill/tracking number
3. Click **Actions > Fetch PO Items** to auto-populate items from the Purchase Order
4. Review items and expected quantities
5. Click **Actions > Confirm ASN**

**Status: Draft --> Confirmed**

#### Step 2: Mark Arrival (Receiving Team)

When the truck physically arrives at the warehouse gate:

1. Open the ASN document
2. Click **Actions > Mark Arrived**
3. System records the actual arrival timestamp

**Status: Confirmed --> Arrived**

#### Step 3: Unload and Sort (Receiving Operators)

1. Click **Actions > Start Unloading**
2. **Physically unload** the truck
3. For each item row, enter:
   - **Received Qty**: How many good units were actually received
   - **Damage Qty**: How many units are damaged (if any)
   - The system auto-calculates **Shortage Qty** and **Over Qty**
4. Once all items are checked, click **Actions > Complete Sorting**

**Status: Arrived --> Unloading --> Sorted**

> **What happens:** The system marks each row:
> - **Received**: received_qty matches or exceeds expected_qty
> - **Short**: received_qty is less than expected_qty
> - **Damaged**: damage_qty > 0

#### Step 4: Create Putaway Task (Receiving Lead)

1. Click **Actions > Create Putaway Task**
2. System creates a **WMS Task (Putaway)** with:
   - Target warehouse = ASN's receiving warehouse
   - Only items with received_qty > 0
   - Linked back to this ASN

**Status: Sorted --> Putaway Created**

> **Next:** The putaway task appears in the WMS Task list. See [Putaway Operations](#putaway-sub-procedure) below.

#### Step 5: Complete ASN (Receiving Lead)

After the putaway task is done (goods are in their bins):

1. Open the ASN
2. Click **Actions > Complete ASN**
3. System creates and submits a **Purchase Receipt** in ERPNext
4. Stock levels are updated in ERPNext

**Status: Putaway Created --> Completed**

> **Outputs:**
> - Purchase Receipt (submitted) - updates ERPNext stock ledger
> - WMS Task (Putaway) - directs operators where to put items

#### Putaway Sub-Procedure

When a Putaway task is created (by ASN or manually):

1. Open the **WMS Task** from the task list or the ASN's "Putaway Task" link
2. **Assign** to a putaway operator (Actions > Assign)
3. Operator opens the task, clicks **Actions > Start Task**
4. Operator physically moves items to the target warehouse/bin
5. For each row, verify actual quantity placed
6. Click **Actions > Complete Task**
7. System creates a **Stock Entry (Material Transfer)** moving stock from receiving to target location

---

## SOP-02: B2B Order Fulfillment (Amazon/Blinkit/Zepto)

### When to Use
A large B2B order (typically 100-1000+ units) from Amazon, Blinkit, or Zepto needs to be fulfilled. These are high-unit-count orders, typically for a small number of SKUs.

### Prerequisites
- Sales Order exists in ERPNext (submitted, synced from marketplace)
- Stock is available in the warehouse
- Stock is not frozen

### Option A: Direct Dispatch (Recommended for Single Large B2B Orders)

#### Step 1: Create Dispatch (Dispatch Lead)

1. Go to **WMS Dispatch** > **+ Add WMS Dispatch**
2. Fill in:
   - **Sales Order**: Select the B2B Sales Order
   - **Warehouse**: Source warehouse where stock is stored
   - Customer fields auto-populate from SO
3. Click **Actions > Fetch SO Items** to populate items from the Sales Order
4. Review items and quantities

#### Step 2: Allocate Stock

1. Click **Actions > Allocate**
2. This reserves stock for this order

**Status: Pending --> Allocated**

#### Step 3: Pick Items (Pick Operator)

1. Pick operator physically picks items from shelves/bins
2. Once picked, click **Actions > Mark Picked**
3. System sets picked_qty = ordered_qty for all items

**Status: Allocated --> Picked**

> **For large B2B orders (1000+ units):** The pick operator may need multiple trips. They can update picked_qty per row before clicking Mark Picked.

#### Step 4: Pack Items

1. Pack the items into shipping boxes/pallets
2. Click **Actions > Mark Packed**
3. System sets packed_qty = picked_qty

**Status: Picked --> Packed**

#### Step 5: Weigh

1. Click **Actions > Weigh**
2. Enter the **Total Weight (kg)** in the dialog
3. Click **Confirm Weight**

**Status: Packed --> Weighed**

#### Step 6: Dispatch

1. Click **Actions > Dispatch**
2. Enter:
   - **Carrier**: e.g., "Delhivery", "DTDC", "Amazon Logistics"
   - **Tracking No**: Waybill/AWB number
3. Click **Dispatch Now** and confirm

**Status: Weighed --> Dispatched**

> **What happens automatically:**
> 1. **Delivery Note** created (Draft)
> 2. **Packing Slip** created (linked to Draft DN)
> 3. **Delivery Note** submitted (stock decremented in ERPNext)
> 4. **Shipment** created with carrier and tracking info

#### Step 7: Mark Delivered (When Confirmed)

When the marketplace confirms delivery:
1. Click **Actions > Mark Delivered**

**Status: Dispatched --> Delivered**

---

### Option B: Using Wave Pick (For Multiple B2B Orders Going Out Together)

If you have 3-4 B2B orders for the same items going out at the same time:

1. Create a **WMS Wave Pick** (see [SOP-03 Step 1-3](#step-1-create-wave-pick))
2. Add all the B2B Sales Orders to the wave
3. Consolidate items for efficient picking
4. After picking, create individual **WMS Dispatch** per Sales Order for shipping

---

## SOP-03: B2C Order Fulfillment (Shopify - 1000+ orders/day)

### When to Use
~1000 Shopify orders synced daily as Sales Orders in ERPNext. Each order has 1-5 items. Use Wave Picking to consolidate them into efficient picking batches.

### Prerequisites
- Sales Orders synced from Shopify to ERPNext (submitted)
- Stock available in picking warehouse

### Step 1: Create Wave Pick (Dispatch Lead - Morning)

1. Go to **WMS Wave Pick** > **+ Add WMS Wave Pick**
2. Fill in:
   - **Wave Name**: e.g., "Morning Wave - 20 Feb 2026" or "Shopify Batch 1"
   - **Warehouse**: Your main picking warehouse
   - **Priority**: High (for same-day dispatch)
   - **Assigned To**: Pick team lead
3. Save

#### Step 2: Add Sales Orders to Wave

1. Click **Actions > Add Sales Orders**
2. In the dialog, search and select a Sales Order
3. Click **Add** - repeat for all orders in this wave
4. You can add 50-200 orders per wave depending on your capacity

> **Tip for 1000 orders/day:** Create 4-5 waves of 200-250 orders each. Stagger them through the day:
> - Wave 1: Morning (8 AM) - Orders from overnight
> - Wave 2: Mid-morning (10 AM)
> - Wave 3: Afternoon (1 PM)
> - Wave 4: Late afternoon (3 PM)
> - Wave 5: Evening (5 PM) - Cutoff orders

#### Step 3: Consolidate Items

1. Click **Actions > Consolidate Items**
2. System reads all items from all Sales Orders in the wave
3. Groups by item_code and sums quantities
4. Example: If 200 orders each have 2x "SKU-A", the consolidated list shows "SKU-A: 400 units"

> **Why this matters:** Instead of picking 200 separate orders one by one, the operator picks 400 units of SKU-A in one trip, then 300 units of SKU-B, etc. This is 5-10x faster.

#### Step 4: Release Wave

1. Review the consolidated items list
2. Click **Actions > Release Wave**

**Status: Draft --> Released**

#### Step 5: Create Pick Task

1. Click **Actions > Create Pick Task**
2. System creates a single **WMS Task (Pick)** with:
   - All consolidated items
   - Source warehouse from the wave
   - Linked back to this wave

**Status: Released --> Picking**

#### Step 6: Execute Pick Task (Pick Operators)

1. Open the **WMS Task** (linked from the wave's "Pick Task" field)
2. Assign to pick operator(s)
3. Operator clicks **Start Task**
4. Operator picks items from bins using the consolidated list
5. For each item row, verify/adjust actual_qty picked
6. Click **Complete Task**
7. System creates **Stock Entry (Material Transfer)** moving stock from pick location to staging/dispatch area

#### Step 7: Complete Wave

1. Return to the Wave Pick document
2. Click **Actions > Complete Wave**

**Status: Picking --> Completed**

#### Step 8: Pack Individual Orders (Pack Station)

Now the consolidated items are in the staging area. Each individual order needs to be packed separately:

**For each Sales Order in the wave:**

1. Go to **WMS Pack Station** > **+ Add WMS Pack Station**
2. Select the **Sales Order**
3. Click **Actions > Fetch SO Items**
4. Click **Actions > Start Packing**
5. **Scan each item's barcode** (see [SOP-07 Pack Station](#sop-07-pack-station) for details)
6. **Complete Packing** when done
7. System creates Delivery Note + Packing Slip

> **Scaling to 1000 orders/day:**
> - Set up 3-5 pack stations running simultaneously
> - Each packer handles 200-300 orders per shift
> - Barcode scanning prevents wrong-item errors

#### Step 9: Dispatch (Bulk)

For each packed order:
1. Create **WMS Dispatch** or use the Delivery Note directly
2. Enter carrier and tracking info
3. Handoff to courier

> **Alternative for high volume:** If you don't need the full dispatch workflow for each B2C order, you can skip WMS Dispatch and use the Delivery Notes created by Pack Station directly. The Pack Station already creates the DN and Packing Slip.

---

## SOP-04: Internal Stock Transfer

### When to Use
Moving stock between warehouses or between bins within the same warehouse (e.g., from bulk storage to picking face, or between warehouse locations).

### Step-by-Step

1. Go to **WMS Task** > **+ Add WMS Task**
2. Set **Task Type**: Transfer
3. Fill in:
   - **Source Warehouse**: Where items are now
   - **Source Bin**: Specific bin (optional)
   - **Target Warehouse**: Where items are going
   - **Target Bin**: Specific destination bin (optional)
   - **Priority**: As needed
4. Add items to the Items table:
   - **Item Code**: Select item
   - **Required Qty**: How many to move
5. Save
6. **Assign** to a warehouse operator
7. Operator clicks **Start Task**
8. Operator physically moves the items
9. Operator clicks **Complete Task**
10. System creates **Stock Entry (Material Transfer)**

---

## SOP-05: Cycle Counting & Stock Verification

### When to Use
Scheduled or ad-hoc physical stock verification to ensure system quantities match actual quantities on shelves.

### Types of Counts

| Count Type | When | Frequency | What's Counted |
|------------|------|-----------|---------------|
| Full | Annual or quarterly | Quarterly/Annual | All items in warehouse |
| ABC-A | High-value/fast-moving items | Weekly/Monthly | Top 20% items by value |
| ABC-B | Medium items | Monthly/Quarterly | Middle 30% items |
| ABC-C | Low-value/slow-moving items | Quarterly/Annual | Bottom 50% items |
| Bin-Based | Specific bin audit | As needed | All items in a specific bin |
| Random | Spot check | Daily | Random selection of items |

### Step-by-Step

#### Step 1: Create Cycle Count (Warehouse Manager)

1. Go to **WMS Cycle Count** > **+ Add WMS Cycle Count**
2. Fill in:
   - **Count Name**: e.g., "Weekly ABC-A Count - Week 8"
   - **Count Type**: Select type
   - **Warehouse**: Target warehouse
   - **Bin**: Optional - for bin-based counts
   - **ABC Class**: A/B/C if applicable
   - **Count Frequency**: For scheduling reference
   - **Assigned To**: Count operator

#### Step 2: Populate Items

1. Click **Actions > Populate Items**
2. System fetches all items with stock in the selected warehouse from ERPNext
3. Each row shows:
   - **Item Code** and **Item Name**
   - **Book Qty**: Current system quantity
   - **Valuation Rate**: Current value per unit

#### Step 3: Refresh Book Quantities (Optional)

If some time has passed since populating:
1. Click **Actions > Fetch Book Quantities**
2. System refreshes book_qty from the latest ERPNext data

#### Step 4: Start Count

1. Click **Actions > Start Count**

**Status: Draft --> In Progress**

#### Step 5: Physical Count (Count Operator)

1. Go to each bin/location physically
2. Count the actual units on the shelf
3. Enter **Counted Qty** for each item row
4. System auto-calculates:
   - **Variance Qty** = counted - book
   - **Variance %** = (variance / book) x 100
   - **Variance Value** = variance x valuation_rate
5. Rows turn color-coded:
   - Green = Matched (no variance)
   - Yellow = Small variance (less than or equal to 10%)
   - Red = Large variance (>10%)

#### Step 6: Complete Count

1. Review all variances
2. Click **Actions > Complete Count**
3. System:
   - Marks rows as "Matched" or "Variance"
   - Calculates total variance value
   - **If variances exist:** Creates and submits a **Stock Reconciliation** in ERPNext
   - **If no variances:** Reports clean count, no reconciliation needed

**Status: In Progress --> Completed**

> **What Stock Reconciliation does:** It adjusts ERPNext's stock ledger to match the physical count. If you counted 95 but the system said 100, the reconciliation creates a -5 adjustment.

---

## SOP-06: Stock Freeze (Quality Hold / Investigation)

### When to Use
- Quality issue discovered with a batch
- Customer returns under investigation
- Audit hold on specific items
- Damaged goods awaiting disposal decision

### Step-by-Step

#### Freeze Stock

1. Go to **WMS Stock Freeze** > **+ Add WMS Stock Freeze**
2. Set **Freeze Type**: Freeze
3. Specify scope (at least one required):
   - **Item Code**: Freeze a specific item across all locations
   - **Warehouse**: Freeze an entire warehouse
   - **Bin**: Freeze a specific bin location
   - **Batch No**: Freeze a specific batch
4. Enter **Reason**: Why the stock is being frozen (mandatory)
5. Save
6. Click **Actions > Activate Freeze**

> **What happens:**
> - If a Bin was specified, its status changes to "Blocked"
> - Any WMS Task trying to complete with frozen stock will be BLOCKED
> - The operator sees: "Cannot complete task: Item XXX is on frozen stock"

#### Release Stock

When the issue is resolved:
1. Open the Stock Freeze record
2. Click **Actions > Release Freeze**
3. If a bin was blocked, it's reactivated to "Active"
4. Stock is available for operations again

---

## SOP-07: Pack Station Operations (Scan-to-Verify)

### When to Use
Packing individual orders (especially B2C) with barcode verification to prevent wrong-item shipments.

### Prerequisites
- Items have barcodes set up in ERPNext (Item > Item Barcode table)
- Barcode scanner connected to the workstation
- Sales Order exists

### Step-by-Step

#### Step 1: Set Up Pack Station

1. Go to **WMS Pack Station** > **+ Add WMS Pack Station**
2. Select **Sales Order**
3. Select **Warehouse** (optional)
4. Click **Actions > Fetch SO Items**
5. System populates items with:
   - Item code, name, ordered qty
   - Barcode (auto-fetched from Item master)

#### Step 2: Start Packing

1. Click **Actions > Start Packing**
2. System:
   - Sets packer to current user
   - Records start time
   - Creates first package (Package #1, Box type, Open status)
   - Focuses cursor on the scan input field

**Status: Draft --> Packing**

#### Step 3: Scan Items

1. Pick up an item from the staging area
2. **Scan its barcode** with the handheld scanner
3. System:
   - Matches barcode to item in the order
   - Increments packed_qty by 1
   - Assigns item to current open package
   - Shows green alert: "Packed 1x ITEM-CODE into Package 1"
   - Auto-refocuses on scan field for next item
4. If barcode doesn't match any item: red alert "Barcode XXX not found"
5. If item is already fully packed: red alert "Item XXX is fully packed"
6. If no open package: red alert "No open package. Add a new package first."

#### Step 4: Package Management

- **Seal Package**: When a box is full, click **Actions > Seal Package**
- **Add Package**: Click **Actions > Add Package** to start a new box
- Enter package dimensions (length/width/height) and weights as needed

#### Step 5: Complete Packing

1. Verify the packed/remaining counters at the top
2. Click **Actions > Complete Packing**
3. Any unpacked items are marked as "Short"
4. All open packages are auto-sealed
5. System creates:
   - **Delivery Note** (draft, then submitted)
   - **Packing Slip** (linked to DN)

**Status: Packing --> Completed**

---

## SOP-08: Bin Location Management

### Setting Up Bin Locations

#### Naming Convention
Bins follow the pattern: **Aisle-Rack-Shelf-Level**
Example: `A-01-03-2` = Aisle A, Rack 01, Shelf 03, Level 2

#### Creating Bins

1. Go to **Warehouse Bin** > **+ Add Warehouse Bin**
2. Fill in:
   - **Warehouse**: Select the ERPNext warehouse (must be a leaf warehouse, not a group)
   - **Zone Type**: Picking / Stocking / Receiving / Return / Defective / Staging
   - **Aisle**: e.g., "A"
   - **Rack**: e.g., "01"
   - **Shelf**: e.g., "03"
   - **Level**: e.g., "2"
   - Bin Code auto-generates as "A-01-03-2"
3. Optional: Enter dimensions (length/width/height in cm) - volume auto-calculates
4. Optional: Enter max weight (kg)
5. Save

#### Bin Statuses

| Status | Meaning | Operations Allowed |
|--------|---------|-------------------|
| Active | Normal operations | Putaway, Pick, Transfer |
| Full | No more capacity | Pick, Transfer (no new putaway) |
| Blocked | Frozen/held | None (blocked by Stock Freeze) |
| Maintenance | Under maintenance | None |

#### Changing Bin Status

- **Block Bin**: Actions > Block Bin (prevents all operations)
- **Mark Full**: Actions > Mark Full (prevents putaway)
- **Reactivate**: Actions > Reactivate (restores to Active)

---

## ERPNext Documents Created Automatically

### Quick Reference Table

| WMS Action | ERPNext Document | When Created | Auto-Submitted? |
|------------|-----------------|-------------|-----------------|
| Complete ASN | Purchase Receipt | ASN completion | Yes |
| Complete Putaway Task | Stock Entry (Material Transfer) | Task completion | Yes |
| Complete Pick Task | Stock Entry (Material Transfer) | Task completion | Yes |
| Complete Transfer Task | Stock Entry (Material Transfer) | Task completion | Yes |
| Complete Count Task | Stock Reconciliation | Task completion (if variances) | Yes |
| Dispatch | Delivery Note | Dispatch action | Yes |
| Dispatch | Packing Slip | Dispatch action | No (not submittable) |
| Dispatch | Shipment | Dispatch action (if carrier set) | Yes |
| Complete Cycle Count | Stock Reconciliation | Count completion (if variances) | Yes |
| Complete Pack Station | Delivery Note | Packing completion | Yes |
| Complete Pack Station | Packing Slip | Packing completion | No (not submittable) |

---

## Troubleshooting & Error Handling

### Common Issues

#### "Cannot complete task: Item XXX is on frozen stock"
**Cause:** A WMS Stock Freeze is active that matches this item/warehouse/bin/batch.
**Fix:** Go to WMS Stock Freeze list, find the active freeze, and Release it if the hold is resolved.

#### "Source Warehouse is required for Pick tasks"
**Cause:** Pick task created without a source warehouse.
**Fix:** Set the Source Warehouse field before completing.

#### "No completed items to create Stock Entry for"
**Cause:** No items in the task have row_status = "Completed".
**Fix:** Ensure items are marked as completed before completing the task.

#### "Stock Entry creation failed"
**Cause:** Usually insufficient stock in the source warehouse, or item valuation issues.
**Fix:** Check ERPNext stock levels. Ensure items have valuation rates set (Item master > Valuation Rate).

#### "Delivery Note submission failed"
**Cause:** Stock unavailable, pricing issues, or mandatory fields missing.
**Fix:** Check the error_log field on the Dispatch/Pack Station for the specific error message.

#### Pack Station: "Barcode XXX not found in items"
**Cause:** Item's barcode not set up in ERPNext, or wrong barcode scanned.
**Fix:** Go to Item master > scroll to Item Barcode section > add the barcode.

#### Pack Station: "No open package"
**Cause:** All packages are sealed and a new one hasn't been created.
**Fix:** Click Actions > Add Package to create a new box.

### Error Logs

All WMS DocTypes have an **Error Log** field (collapsible section at the bottom). If a document creation fails:
1. The WMS document still completes (status changes to Completed)
2. The error is recorded in the Error Log field
3. A warning message appears: "Completed with warnings. Check Error Log."
4. You can manually create the missing ERPNext document after fixing the issue

### Checking Stock Freeze Status

To see all active freezes:
1. Go to WMS Stock Freeze list
2. Filter: Status = Active
3. Review the scope (item/warehouse/bin/batch) of each freeze

---

## Appendix: Recommended Warehouse Layout

### Zone Types and Their Usage

```
WAREHOUSE FLOOR PLAN
====================

[RECEIVING ZONE]          [STAGING ZONE]          [DISPATCH ZONE]
 Zone: Receiving           Zone: Staging            Zone: Staging
 Bins: RCV-01-01-1         Bins: STG-01-01-1        Bins: DSP-01-01-1
       RCV-01-01-2               STG-01-01-2              DSP-01-01-2
       ...                       ...                      ...
 Used for: ASN unloading   Used for: Wave pick      Used for: Dispatch
                           staging after pick        ready for carrier

[PICKING ZONE]                              [RETURN ZONE]
 Zone: Picking                               Zone: Return
 Bins: A-01-01-1 through Z-10-05-4          Bins: RTN-01-01-1
 Used for: Active SKUs,                      Used for: Customer
 fast-moving items at                        returns, RMA
 ergonomic pick height

[STOCKING/BULK ZONE]                        [DEFECTIVE ZONE]
 Zone: Stocking                              Zone: Defective
 Bins: BLK-01-01-1 through BLK-10-05-4     Bins: DEF-01-01-1
 Used for: Reserve stock,                   Used for: Damaged
 palletized bulk storage,                   goods, quality
 replenishment source                       inspection holds
```

### Suggested Daily Workflow for 1000 B2C + B2B Orders

```
6:00 AM   Overnight Shopify orders synced (~400 orders)
          Create Wave 1 (200 orders)
          Create Wave 2 (200 orders)

7:00 AM   Pick Team starts Wave 1
          Receiving Team prepares for ASN arrivals

8:00 AM   Wave 1 picking completes
          Pack Station Team starts packing Wave 1 orders (3-5 stations)
          Pick Team starts Wave 2

9:00 AM   New Shopify orders synced (~200 orders)
          Create Wave 3 (200 orders)
          Wave 2 picking completes
          B2B orders from Amazon/Blinkit arrive - create WMS Dispatch

10:00 AM  Pack Station continues Wave 1 + starts Wave 2
          Pick Team starts Wave 3
          B2B picks in progress

11:00 AM  First courier pickup (Wave 1 dispatches)
          ASN trucks arriving - receiving team unloading

12:00 PM  Wave 3 complete, packing continues
          Create Wave 4 from new orders (~200)

1:00 PM   Second courier pickup
          Putaway from morning ASN complete

2:00 PM   Wave 4 picking + packing
          Afternoon cycle count (ABC-A items)

3:00 PM   Final wave creation (Wave 5) for cutoff orders
          B2B dispatches complete

4:00 PM   Final picking + packing
          All B2B orders dispatched

5:00 PM   Final courier pickup
          End-of-day cycle count
          Close out all open tasks

6:00 PM   Review: Check for incomplete tasks, pending ASNs
```

---

**Document Owner:** Warehouse Operations
**Review Frequency:** Monthly
**Next Review Date:** March 2026

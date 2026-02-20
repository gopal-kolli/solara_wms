import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class WMSDispatch(Document):
    """
    WMS Dispatch - Outbound shipping workflow.
    Maps to ModernWMS DispatchEntity + DispatchListEntity.

    Workflow: Pending -> Allocated -> Picked -> Packed -> Weighed
              -> Dispatched -> Delivered

    On dispatch:
      - Creates Delivery Note (Draft)
      - Creates Packing Slip (while DN is Draft)
      - Submits Delivery Note
      - Optionally creates Shipment with carrier/tracking info
    """

    def validate(self):
        self.update_totals()

    def update_totals(self):
        """Update summary totals."""
        self.total_qty = sum(flt(r.ordered_qty) for r in (self.items or []))
        self.total_dispatched = sum(flt(r.dispatched_qty) for r in (self.items or []))

    # ─── STATUS TRANSITIONS ──────────────────────────────────

    @frappe.whitelist()
    def allocate(self):
        """Pending -> Allocated (reserve stock)."""
        if self.status != "Pending":
            frappe.throw(_("Only Pending dispatches can be allocated"))
        if not self.items:
            frappe.throw(_("Add items before allocating"))

        self.status = "Allocated"
        self.save()
        frappe.msgprint(_("Stock allocated for dispatch"), indicator="blue")

    @frappe.whitelist()
    def mark_picked(self):
        """Allocated -> Picked."""
        if self.status != "Allocated":
            frappe.throw(_("Only Allocated dispatches can be marked as picked"))

        for row in self.items or []:
            if not flt(row.picked_qty):
                row.picked_qty = flt(row.ordered_qty)
            row.row_status = "Picked"

        self.status = "Picked"
        self.save()
        frappe.msgprint(_("Items marked as picked"), indicator="blue")

    @frappe.whitelist()
    def mark_packed(self):
        """Picked -> Packed."""
        if self.status != "Picked":
            frappe.throw(_("Only Picked dispatches can be marked as packed"))

        for row in self.items or []:
            if not flt(row.packed_qty):
                row.packed_qty = flt(row.picked_qty)
            row.row_status = "Packed"

        self.status = "Packed"
        self.save()
        frappe.msgprint(_("Items marked as packed"), indicator="blue")

    @frappe.whitelist()
    def mark_weighed(self, total_weight=None):
        """Packed -> Weighed."""
        if self.status != "Packed":
            frappe.throw(_("Only Packed dispatches can be weighed"))

        if total_weight:
            self.total_weight = flt(total_weight)

        self.status = "Weighed"
        self.save()
        frappe.msgprint(
            _("Dispatch weighed: {0} kg").format(self.total_weight),
            indicator="blue"
        )

    @frappe.whitelist()
    def dispatch(self, carrier=None, tracking_no=None):
        """
        Weighed -> Dispatched.
        Creates Delivery Note, Packing Slip, and optionally Shipment.
        IMPORTANT: DN must be Draft when creating Packing Slip.
        """
        if self.status != "Weighed":
            frappe.throw(_("Only Weighed dispatches can be dispatched"))

        if carrier:
            self.carrier = carrier
        if tracking_no:
            self.tracking_no = tracking_no

        error_log = []

        # Set dispatched_qty for all items
        for row in self.items or []:
            if not flt(row.dispatched_qty):
                row.dispatched_qty = flt(row.packed_qty) or flt(row.picked_qty)
            row.row_status = "Dispatched"

        # Step 1: Create Delivery Note (Draft - do NOT submit yet)
        dn = None
        try:
            dn = frappe.new_doc("Delivery Note")
            dn.customer = self.customer
            dn.company = (
                frappe.defaults.get_user_default("company")
                or "Win The Buy Box Private Limited"
            )
            dn.set_warehouse = self.warehouse

            for row in self.items or []:
                if flt(row.dispatched_qty) <= 0:
                    continue
                dn_item = {
                    "item_code": row.item_code,
                    "qty": flt(row.dispatched_qty),
                    "warehouse": self.warehouse,
                    "batch_no": row.batch_no or "",
                    "serial_no": row.serial_no or "",
                }
                if self.sales_order:
                    dn_item["against_sales_order"] = self.sales_order
                dn.append("items", dn_item)

            if dn.items:
                dn.insert()
                self.delivery_note = dn.name
            else:
                error_log.append("No items to create Delivery Note for.")

        except Exception as e:
            error_log.append(f"Delivery Note creation failed: {str(e)}")
            dn = None

        # Step 2: Create Packing Slip (while DN is still Draft)
        if dn and not error_log:
            try:
                ps = frappe.new_doc("Packing Slip")
                ps.delivery_note = dn.name

                for row in self.items or []:
                    if flt(row.dispatched_qty) <= 0:
                        continue
                    ps.append("items", {
                        "item_code": row.item_code,
                        "qty": flt(row.dispatched_qty),
                        "net_weight": flt(row.weight),
                    })

                ps.gross_weight_pkg = flt(self.total_weight)
                ps.insert()

            except Exception as e:
                error_log.append(f"Packing Slip creation failed: {str(e)}")

        # Step 3: Submit Delivery Note
        if dn and self.delivery_note:
            try:
                dn.reload()
                dn.submit()
            except Exception as e:
                error_log.append(f"Delivery Note submission failed: {str(e)}")

        # Step 4: Create Shipment (optional)
        if self.carrier and not error_log:
            try:
                shipment = frappe.new_doc("Shipment")
                shipment.pickup_company = (
                    frappe.defaults.get_user_default("company")
                    or "Win The Buy Box Private Limited"
                )
                shipment.delivery_customer = self.customer
                shipment.awb_number = self.tracking_no or ""

                shipment.append("shipment_delivery_note", {
                    "delivery_note": self.delivery_note,
                })

                shipment.insert()
                shipment.submit()
                self.shipment = shipment.name

            except Exception as e:
                # Shipment is optional - don't block dispatch
                error_log.append(f"Shipment creation failed (non-blocking): {str(e)}")

        self.dispatched_at = now_datetime()
        self.status = "Dispatched"
        self.update_totals()
        self.error_log = "\n".join(error_log) if error_log else ""
        self.save()

        if error_log:
            frappe.msgprint(
                _("Dispatched with warnings. Check Error Log."),
                indicator="orange"
            )
        else:
            frappe.msgprint(
                _("Dispatched successfully. DN: {0}").format(
                    f'<a href="/app/delivery-note/{self.delivery_note}">'
                    f'{self.delivery_note}</a>'
                ),
                indicator="green"
            )

        return {
            "status": self.status,
            "delivery_note": self.delivery_note,
            "shipment": self.shipment,
            "errors": error_log,
        }

    @frappe.whitelist()
    def mark_delivered(self):
        """Dispatched -> Delivered."""
        if self.status != "Dispatched":
            frappe.throw(_("Only Dispatched items can be marked as delivered"))

        self.delivered_at = now_datetime()
        self.status = "Delivered"
        self.save()
        frappe.msgprint(_("Delivery confirmed"), indicator="green")

    @frappe.whitelist()
    def cancel_dispatch(self):
        """Cancel dispatch (not for Dispatched or Delivered)."""
        if self.status in ("Dispatched", "Delivered"):
            frappe.throw(_("Cannot cancel dispatched or delivered shipments"))

        self.status = "Cancelled"
        self.save()
        frappe.msgprint(_("Dispatch cancelled"), indicator="red")

    @frappe.whitelist()
    def fetch_so_items(self):
        """Populate items from linked Sales Order."""
        if not self.sales_order:
            frappe.throw(_("Select a Sales Order first"))

        so = frappe.get_doc("Sales Order", self.sales_order)
        self.items = []

        for so_item in so.items:
            self.append("items", {
                "item_code": so_item.item_code,
                "item_name": so_item.item_name,
                "ordered_qty": flt(so_item.qty),
                "uom": so_item.uom or so_item.stock_uom,
            })

        self.update_totals()
        self.save()

        frappe.msgprint(
            _("Fetched {0} items from Sales Order").format(len(self.items)),
            indicator="green"
        )

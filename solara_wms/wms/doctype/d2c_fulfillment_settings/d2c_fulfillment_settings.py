import frappe
from frappe.model.document import Document

from solara_wms.wms import d2c_fulfillment


class D2CFulfillmentSettings(Document):
    @frappe.whitelist()
    def run_release_now(self):
        """Manual trigger for the release job (respects Dry Run + all gates)."""
        return d2c_fulfillment.release_d2c_shipments()

    @frappe.whitelist()
    def fetch_labels_now(self):
        """Manual trigger for the label-fetch job (respects its gate)."""
        return d2c_fulfillment.fetch_d2c_labels()

    @frappe.whitelist()
    def prepare_now(self, on_date=None):
        """Manual trigger for Prepare Today's Shipments."""
        return d2c_fulfillment.prepare_todays_shipments(on_date=on_date)

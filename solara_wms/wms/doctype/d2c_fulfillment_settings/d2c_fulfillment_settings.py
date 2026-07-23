import frappe
from frappe.model.document import Document

from solara_wms.wms import d2c_fulfillment


class D2CFulfillmentSettings(Document):
    @frappe.whitelist()
    def run_release_now(self):
        """Manual pull — releases up to Max Orders Per Run oldest orders on demand.
        Bypasses the release_enabled pause + cutoff hour (explicit human action);
        respects Dry Run + every per-order gate. Use when auto-release is paused
        and the warehouse has capacity to ship a batch."""
        return d2c_fulfillment.release_d2c_shipments(force=True)

    @frappe.whitelist()
    def run_release_range(self, from_date, to_date):
        """Queue a background release of every order ordered between from_date and
        to_date (manual date-range pull). Returns immediately; a summary posts to
        the wave Slack channel when the whole range is drained."""
        return d2c_fulfillment.enqueue_release_range(from_date, to_date)

    @frappe.whitelist()
    def preview_release(self, from_date=None, to_date=None):
        """SAFE dry-run — how many orders WOULD release right now (by outcome),
        WITHOUT creating any Delivery Note or notifying any customer. This is the
        safe way to 'just look' before pulling a batch. Never writes."""
        settings = d2c_fulfillment._settings()
        return d2c_fulfillment._run_release(
            settings, dry_run=True, from_date=from_date, to_date=to_date)

    @frappe.whitelist()
    def fetch_labels_now(self):
        """Manual trigger for the label-fetch job (respects its gate)."""
        return d2c_fulfillment.fetch_d2c_labels()

    @frappe.whitelist()
    def prepare_now(self, on_date=None):
        """Manual trigger for Prepare Today's Shipments."""
        return d2c_fulfillment.prepare_todays_shipments(on_date=on_date)

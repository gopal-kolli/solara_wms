app_name = "solara_wms"
app_title = "SOLARA WMS"
app_publisher = "Win The Buy Box Private Limited"
app_description = "Warehouse Management System for ERPNext - bin locations and directed warehouse tasks"
app_email = "gopal@solara.in"
app_license = "MIT"
app_version = "1.7.0"

# Required apps
required_apps = ["frappe", "erpnext"]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/solara_wms/css/solara_wms.css"
# app_include_js = "/assets/solara_wms/js/solara_wms.js"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "solara_wms.notifications.get_notification_config"

# Fixtures
# --------
fixtures = [
    {"dt": "Role", "filters": [["name", "in", ["Returns Manager", "HQ Returns Reviewer"]]]},
    {"dt": "Workflow State", "filters": [["name", "in", ["Draft", "Pending HQ Review", "Approved", "Rejected"]]]},
    {"dt": "Workflow", "filters": [["name", "in", ["Return Intake Approval"]]]},
    {"dt": "Custom Field", "filters": [["name", "in", ["Item-custom_boxes_per_unit", "Delivery Note-custom_d2c_defer_si", "Delivery Note-custom_shopify_fulfilled", "Delivery Note-custom_awb_shortfall", "Delivery Note-custom_box_count", "Delivery Note-custom_prepare_batch", "Sales Order-custom_shopify_cancellation_hold", "Sales Order-custom_shopify_cancelled_at", "Sales Order-custom_shopify_cancellation_reason", "Delivery Note-custom_shopify_cancellation_hold", "Delivery Note-custom_shopify_cancelled_at", "Delivery Note-custom_shopify_cancellation_reason", "Sales Order-custom_shopify_address_change_hold", "Sales Order-custom_shopify_address_change_at", "Sales Order-custom_shopify_address_change_reason", "Delivery Note-custom_shopify_address_change_hold", "Delivery Note-custom_shopify_address_change_at", "Delivery Note-custom_shopify_address_change_reason", "Sales Order-custom_opd_resolution_id", "Sales Order-custom_opd_auto_fulfill", "Sales Order-custom_opd_approval_snapshot", "Sales Order-custom_opd_approval_hash", "Sales Order-custom_opd_approved_by", "Sales Order-custom_opd_approved_at", "Delivery Note-custom_opd_resolution_id", "Delivery Note-custom_opd_approval_hash", "Sales Invoice-custom_opd_resolution_id", "Sales Invoice-custom_opd_approval_hash"]]]},
]

# Document Events
# ---------------
doc_events = {
    "WMS Task": {
        "before_save": "solara_wms.wms.utils.check_stock_freeze_on_task"
    }
}

# Scheduled Tasks
# ---------------
scheduler_events = {
    "cron": {
        # D2C fulfillment — both jobs are internally gated (no-op unless enabled
        # in D2C Fulfillment Settings), so wiring them here is safe by default.
        "*/15 * * * *": [
            "solara_wms.wms.d2c_fulfillment.release_opd_replacements",
            "solara_wms.wms.d2c_fulfillment.release_d2c_shipments",
            "solara_wms.wms.d2c_fulfillment.fetch_d2c_labels",
            "solara_wms.wms.d2c_fulfillment.run_prepare_waves",
            "solara_wms.wms.inventory_accuracy.scheduled_inventory_reconciliation",
        ],
        # Customer-facing Shopify fulfillment/AWB sync. This is gated by
        # auto_fulfill_shopify AND custom_dispatched, so label creation alone
        # can never fire a premature "shipped" notification.
        "*/5 * * * *": [
            "solara_wms.wms.d2c_fulfillment.sync_dispatched_shopify_fulfillments",
            "solara_wms.wms.shopify_address_sync.sync_shopify_address_changes",
        ],
        # Ops Google Sheet mirror — gated by ops_sheet_enabled; secrets in site config.
        "*/30 * * * *": [
            "solara_wms.wms.d2c_ops_sheet.push_ops_sheet",
        ],
        # Layer-3 leak monitor — reconcile every order -> dispatch, post the
        # categorized report to Slack. Gated by completeness_report_enabled
        # (default OFF); 11:00 + 18:00 site time (IST).
        "0 11,18 * * *": [
            "solara_wms.wms.d2c_fulfillment.d2c_completeness_report",
        ],
        # Daily address-change exception list. Gated OFF until the report and
        # destination are confirmed on TEST.
        "0 9 * * *": [
            "solara_wms.wms.shopify_address_sync.post_address_change_exception_report",
        ],
        # Pack-verify box photos: 90-day retention (SOP-PACK-QC sheet rule).
        # The D2C Pack Verify RECORD is kept forever; only the image is purged.
        "40 2 * * *": [
            "solara_wms.wms.d2c_pack_verify.purge_pack_photos",
        ],
        # Auto-stamp custom_dispatched from courier first-scan. Gated by
        # dispatch_stamp_enabled (default OFF); twice hourly.
        "5,35 * * * *": [
            "solara_wms.wms.d2c_dispatch.stamp_dispatched",
        ],
    },
}

# Override Methods
# ---------------
override_whitelisted_methods = {
    "solara_wms.d2c.prepare_todays_shipments": "solara_wms.wms.d2c_fulfillment.prepare_todays_shipments",
}

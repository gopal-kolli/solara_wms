app_name = "solara_wms"
app_title = "SOLARA WMS"
app_publisher = "Win The Buy Box Private Limited"
app_description = "Warehouse Management System for ERPNext - bin locations and directed warehouse tasks"
app_email = "gopal@solara.in"
app_license = "MIT"
app_version = "1.0.0"

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
# fixtures = []

# Document Events
# ---------------
doc_events = {
    "WMS Task": {
        "before_save": "solara_wms.wms.utils.check_stock_freeze_on_task"
    }
}

# Scheduled Tasks
# ---------------
# scheduler_events = {}

# Override Methods
# ---------------
# override_whitelisted_methods = {}

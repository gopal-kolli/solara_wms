from frappe import _


def get_data():
    return [
        {
            "module_name": "WMS",
            "category": "Modules",
            "label": _("WMS"),
            "icon": "octicon octicon-package",
            "type": "module",
            "description": _("Warehouse Management - Bin Locations & Directed Tasks"),
        }
    ]

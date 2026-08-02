"""Safety boundaries shared by warehouse-floor workflows."""

import frappe
from frappe import _


SETTINGS_DOCTYPE = "WMS Settings"


def require_wms_mode(*allowed_modes):
    """Stop dormant floor workflows unless an administrator enabled the mode.

    The settings DocType defaults to Disabled, so merely installing/migrating
    the app cannot activate legacy completion methods.
    """
    mode = frappe.db.get_single_value(SETTINGS_DOCTYPE, "operating_mode") or "Disabled"
    if mode not in allowed_modes:
        frappe.throw(
            _(
                "This WMS workflow is disabled in {0} mode. "
                "Use the approved current process or ask a System Manager to "
                "change WMS Settings after TEST sign-off."
            ).format(mode),
            title=_("WMS Execution Disabled"),
        )
    return mode

"""Pure address normalisation used by the Shopify address-sync control."""

import re


_COMPARE_FIELDS = (
    "address_line1", "address_line2", "city", "state", "pincode", "country",
)


def _text(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def address_values(shipping_address):
    """Map Shopify's address payload to the Atlas Address fields we own."""
    source = shipping_address or {}
    return {
        "address_line1": str(source.get("address1") or "").strip(),
        "address_line2": str(source.get("address2") or "").strip(),
        "city": str(source.get("city") or "").strip(),
        "state": str(source.get("province") or source.get("province_code") or "").strip(),
        "pincode": re.sub(r"\D", "", str(source.get("zip") or ""))[:6],
        "country": str(source.get("country") or "India").strip(),
        "phone": str(source.get("phone") or "").strip(),
    }


def addresses_match(atlas, shopify):
    """Compare shipping identities without false differences from casing/formatting."""
    for field in _COMPARE_FIELDS:
        if _text(atlas.get(field)) != _text(shopify.get(field)):
            return False
    remote_phone = _phone(shopify.get("phone"))
    return not remote_phone or remote_phone == _phone(atlas.get("phone"))


def state_changed(atlas, shopify):
    return bool(_text(atlas.get("state")) and _text(shopify.get("state"))
                and _text(atlas.get("state")) != _text(shopify.get("state")))


def render_address_exception_slack(rows, stamp):
    """Render a concise, PII-minimised warehouse exception digest."""
    rows = list(rows or [])
    lines = [
        ":warning: *D2C Address Change Exceptions* — {0} IST".format(stamp),
        "*{0} order(s) held* — do not pack, label or dispatch until CS/Ops clears them.".format(
            len(rows)),
    ]
    if not rows:
        lines.append(":white_check_mark: No open address-change holds.")
        return "\n".join(lines)
    for row in rows[:20]:
        lines.append("• *{0}* · {1} · {2}".format(
            row.get("shopify_order_number") or row.get("name"),
            row.get("delivery_note") or "no DN",
            row.get("reason") or "review required",
        ))
    if len(rows) > 20:
        lines.append("_+{0} more — open Atlas report ‘D2C Address Change Exceptions’._".format(
            len(rows) - 20))
    return "\n".join(lines)

# Copyright (c) 2026, SOLARA and contributors
# For license information, please see license.txt
"""Read-only warehouse control-room metrics and station presence.

The floor applications remain intentionally small and task-specific.  This
module combines their audit records for the management/TV PWA without exposing
customer details.  A short-lived Redis heartbeat answers "is this screen open
now?"; permanent throughput figures continue to come only from Pack Verify,
Return Parcel and Dispatch Scan documents.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

import frappe
from frappe.utils import cint, flt, getdate, get_datetime, now_datetime, nowdate


_HEARTBEAT_TTL = 180
_ACTIVE_SECONDS = 120
_KNOWN_STATIONS = {"Returns Station", "Security", "QC Inspector", "Appliance Express"}


def _value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _dt(value):
    if not value:
        return None
    return get_datetime(value)


def _iso(value):
    value = _dt(value)
    return value.isoformat() if value else None


def _station_name(station):
    station = (station or "").strip()
    if station in _KNOWN_STATIONS:
        return station
    if station.startswith("Line "):
        number = cint(station[5:])
        if 1 <= number <= 30 and station == "Line " + str(number):
            return station
    return None


def _cache():
    cache = frappe.cache
    return cache() if callable(cache) else cache


def _heartbeat_key(station):
    return "warehouse-ops:heartbeat:" + station.lower().replace(" ", "-")


@frappe.whitelist()
def station_heartbeat(station):
    """Keep current station presence in shared Redis; never create audit data."""
    station = _station_name(station)
    if not station:
        return {"ok": False, "message": "Unknown warehouse station."}
    seen = now_datetime()
    _cache().set_value(_heartbeat_key(station), seen.isoformat(),
                       expires_in_sec=_HEARTBEAT_TTL)
    return {"ok": True, "station": station, "seen_at": seen.isoformat()}


def _heartbeats(line_count):
    stations = ["Line " + str(i) for i in range(1, line_count + 1)]
    stations += ["Returns Station", "Security", "QC Inspector", "Appliance Express"]
    cache = _cache()
    out = {}
    for station in stations:
        raw = cache.get_value(_heartbeat_key(station))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw:
            out[station] = raw
    return out


def build_metrics(pack_rows, return_rows, return_items, dispatch_rows,
                  dispatched_awbs, heartbeats, now, line_count=6,
                  pending_return_count=0, qc_rows=None):
    """Pure aggregation shared by the API and tests."""
    now = _dt(now) or datetime.now()
    hour_ago = now - timedelta(hours=1)
    lines = []
    qc_rows = qc_rows or []
    qc_by_station = defaultdict(list)
    for row in qc_rows:
        qc_by_station[_value(row, "station") or "Unassigned"].append(row)
    grouped = defaultdict(list)
    for row in pack_rows:
        grouped[_value(row, "station") or "Unassigned"].append(row)

    def station_metrics(station):
        rows = grouped.get(station, [])
        recent = [r for r in rows
                  if (_dt(_value(r, "verified_at")) or datetime.min) >= hour_ago]
        last_scan = max((_dt(_value(r, "verified_at")) for r in rows
                         if _value(r, "verified_at")), default=None)
        heartbeat = _dt(heartbeats.get(station))
        status = ("active" if heartbeat and
                  (now - heartbeat).total_seconds() <= _ACTIVE_SECONDS else
                  "idle" if last_scan and
                  (now - last_scan).total_seconds() <= 15 * 60 else "offline")
        return {"station": station, "status": status, "parcels": len(rows),
                "orders": len({_value(r, "delivery_note") or _value(r, "awb")
                               for r in rows}),
                "pieces": round(sum(flt(_value(r, "pieces_expected"))
                                    for r in rows), 1),
                "parcels_per_hour": len(recent),
                "last_scan_at": _iso(last_scan)}

    for number in range(1, line_count + 1):
        station = "Line " + str(number)
        rows = grouped.get(station, [])
        recent = [r for r in rows if (_dt(_value(r, "verified_at")) or datetime.min) >= hour_ago]
        order_groups = defaultdict(list)
        for row in rows:
            order_groups[_value(row, "delivery_note") or _value(row, "awb")].append(row)
        completed_orders = 0
        for order_rows in order_groups.values():
            required = max(cint(_value(r, "box_count") or 1) for r in order_rows)
            clean_awbs = {_value(r, "awb") for r in order_rows
                          if not cint(_value(r, "mismatch")) and _value(r, "awb")}
            if len(clean_awbs) >= required:
                completed_orders += 1
        durations = [cint(_value(r, "duration_sec")) for r in rows
                     if cint(_value(r, "duration_sec")) > 0]
        pieces = sum(flt(_value(r, "pieces_expected")) for r in rows)
        last_scan = max((_dt(_value(r, "verified_at")) for r in rows
                         if _value(r, "verified_at")), default=None)
        heartbeat = _dt(heartbeats.get(station))
        if heartbeat and (now - heartbeat).total_seconds() <= _ACTIVE_SECONDS:
            status = "active"
        elif last_scan and (now - last_scan).total_seconds() <= 15 * 60:
            status = "idle"
        else:
            status = "offline"
        lines.append({
            "station": station,
            "status": status,
            "heartbeat_at": _iso(heartbeat),
            "last_scan_at": _iso(last_scan),
            "parcels": len(rows),
            "orders": completed_orders,
            "pieces": round(pieces, 1),
            "multi_piece_parcels": sum(1 for r in rows
                                        if flt(_value(r, "pieces_expected")) > 1),
            "avg_pieces": round(pieces / len(rows), 1) if rows else 0,
            "issues_caught": sum(cint(_value(r, "mismatch")) for r in rows),
            "median_sec": int(median(durations)) if durations else None,
            "parcels_per_hour": len(recent),
            "orders_per_hour": len({_value(r, "delivery_note") or _value(r, "awb")
                                     for r in recent}),
            "pieces_per_hour": round(sum(flt(_value(r, "pieces_expected"))
                                          for r in recent), 1),
            "qc_passed": sum(1 for r in qc_by_station.get(station, [])
                               if _value(r, "status") == "Passed"),
            "qc_failures": sum(cint(_value(r, "recheck_count"))
                                for r in qc_by_station.get(station, [])),
        })

    received = len(return_rows)
    processed = [r for r in return_rows if _value(r, "completed_at")]
    qc_minutes = []
    for row in processed:
        start, end = _dt(_value(row, "received_at")), _dt(_value(row, "completed_at"))
        if start and end and end >= start:
            qc_minutes.append((end - start).total_seconds() / 60)
    conditions = Counter(_value(r, "condition") or "Unclassified" for r in return_items)
    return_types = Counter(_value(r, "return_type") or "Unknown" for r in return_rows)

    dispatch_orders = {_value(r, "delivery_note") for r in dispatch_rows
                       if _value(r, "delivery_note")}
    dispatch_recent = [r for r in dispatch_rows
                       if (_dt(_value(r, "scanned_at")) or datetime.min) >= hour_ago]
    couriers = Counter(_value(r, "courier") or "Unknown" for r in dispatch_rows)
    dispatched_awbs = set(dispatched_awbs or [])
    waiting = [r for r in pack_rows if not cint(_value(r, "mismatch"))
               and _value(r, "awb") not in dispatched_awbs]
    oldest_wait = min((_dt(_value(r, "verified_at")) for r in waiting
                       if _value(r, "verified_at")), default=None)

    qc_open = [r for r in qc_rows if _value(r, "status") in ("Pending", "Failed")]
    qc_passed = [r for r in qc_rows if _value(r, "status") == "Passed"]
    qc_durations = [cint(_value(r, "duration_sec")) for r in qc_passed
                    if cint(_value(r, "duration_sec")) > 0]
    oldest_qc = min((_dt(_value(r, "staged_at")) for r in qc_open
                     if _value(r, "staged_at")), default=None)

    express = station_metrics("Appliance Express")
    return {
        "generated_at": now.isoformat(),
        "line_count": line_count,
        "packing": {
            "parcels": len(pack_rows),
            "orders": sum(line["orders"] for line in lines) + express["orders"],
            "pieces": round(sum(flt(_value(r, "pieces_expected")) for r in pack_rows), 1),
            "issues_caught": sum(cint(_value(r, "mismatch")) for r in pack_rows),
            "active_lines": sum(1 for line in lines if line["status"] == "active"),
            "lines": lines,
        },
        "appliance_express": express,
        "returns": {
            "received": received,
            "processed": len(processed),
            "pending_review": cint(pending_return_count),
            "avg_qc_min": round(sum(qc_minutes) / len(qc_minutes), 1) if qc_minutes else None,
            "conditions": dict(conditions),
            "types": dict(return_types),
            "status": ("active" if _dt(heartbeats.get("Returns Station")) and
                       (now - _dt(heartbeats.get("Returns Station"))).total_seconds()
                       <= _ACTIVE_SECONDS else "offline"),
        },
        "dispatch": {
            "parcels": len(dispatch_rows),
            "orders": len(dispatch_orders),
            "parcels_per_hour": len(dispatch_recent),
            "couriers": dict(couriers),
            "waiting_parcels": len(waiting),
            "oldest_wait_at": _iso(oldest_wait),
            "status": ("active" if _dt(heartbeats.get("Security")) and
                       (now - _dt(heartbeats.get("Security"))).total_seconds()
                       <= _ACTIVE_SECONDS else "offline"),
        },
        "quality": {
            "open_holds": len(qc_open),
            "failed_holds": sum(1 for r in qc_open if _value(r, "status") == "Failed"),
            "passed": len(qc_passed),
            "failures_caught": sum(cint(_value(r, "recheck_count")) for r in qc_rows),
            "avg_qc_sec": (round(sum(qc_durations) / len(qc_durations), 1)
                           if qc_durations else None),
            "oldest_hold_at": _iso(oldest_qc),
            "overdue_holds": sum(1 for r in qc_open
                                  if _dt(_value(r, "staged_at")) and
                                  (now - _dt(_value(r, "staged_at"))).total_seconds() > 300),
            "status": ("active" if _dt(heartbeats.get("QC Inspector")) and
                       (now - _dt(heartbeats.get("QC Inspector"))).total_seconds()
                       <= _ACTIVE_SECONDS else "offline"),
        },
    }


def build_b2b_return_metrics(lot_rows, item_rows):
    """Privacy-safe platform-return totals for the management wallboard."""
    channels = Counter(_value(row, "channel") or "Other" for row in lot_rows)
    treatments = Counter(_value(row, "inventory_treatment") or "Investigation"
                         for row in lot_rows)
    return {
        "lots": len(lot_rows),
        "expected_cartons": sum(cint(_value(row, "expected_cartons")) for row in lot_rows),
        "received_cartons": sum(cint(_value(row, "received_cartons")) for row in lot_rows),
        "expected_units": round(sum(flt(_value(row, "expected_qty")) for row in item_rows), 1),
        "received_units": round(sum(flt(_value(row, "received_qty")) for row in item_rows), 1),
        "good_units": round(sum(flt(_value(row, "good_qty")) for row in item_rows), 1),
        "repairable_units": round(sum(flt(_value(row, "repairable_qty")) for row in item_rows), 1),
        "scrap_units": round(sum(flt(_value(row, "scrap_qty")) for row in item_rows), 1),
        "investigation_units": round(sum(flt(_value(row, "investigation_qty"))
                                         for row in item_rows), 1),
        "pending_review": sum(1 for row in lot_rows
                               if _value(row, "status") == "Pending HQ Review"),
        "exceptions": sum(cint(_value(row, "exception")) for row in lot_rows),
        "channels": dict(channels),
        "treatments": dict(treatments),
    }


@frappe.whitelist()
def warehouse_ops_summary(on_date=None, line_count=6):
    """One privacy-safe payload for the warehouse management and TV PWA."""
    line_count = max(1, min(cint(line_count) or 6, 30))
    day = getdate(on_date) if on_date else getdate(nowdate())
    start, end = str(day) + " 00:00:00", str(day) + " 23:59:59"
    pack_rows = frappe.get_all(
        "D2C Pack Verify", filters={"verified_at": ["between", [start, end]]},
        fields=["station", "delivery_note", "awb", "box_count", "mismatch",
                "pieces_expected", "duration_sec", "verified_at"],
        limit_page_length=0)
    return_rows = frappe.get_all(
        "D2C Return Parcel", filters={"received_at": ["between", [start, end]]},
        fields=["name", "status", "return_type", "received_at", "completed_at"],
        limit_page_length=0)
    return_names = [_value(row, "name") for row in return_rows]
    return_items = frappe.get_all(
        "D2C Return Parcel Item", filters={"parent": ["in", return_names]},
        fields=["parent", "condition"], limit_page_length=0) if return_names else []
    dispatch_rows = frappe.get_all(
        "D2C Dispatch Scan", filters={"scanned_at": ["between", [start, end]]},
        fields=["delivery_note", "awb", "courier", "scanned_at"],
        limit_page_length=0)
    pack_awbs = [_value(row, "awb") for row in pack_rows if _value(row, "awb")]
    dispatched = frappe.get_all(
        "D2C Dispatch Scan", filters={"awb": ["in", pack_awbs]}, fields=["awb"],
        limit_page_length=0) if pack_awbs else []
    pending = frappe.db.count("D2C Return Parcel", {"status": "Pending HQ Review"})
    qc_today = frappe.get_all(
        "D2C Pack QC", filters={"staged_at": ["between", [start, end]]},
        fields=["name", "station", "status", "staged_at", "audited_at",
                "duration_sec", "recheck_count"], limit_page_length=0)
    qc_names = {_value(row, "name") for row in qc_today}
    qc_open = frappe.get_all(
        "D2C Pack QC", filters={"status": ["in", ["Pending", "Failed"]]},
        fields=["name", "station", "status", "staged_at", "audited_at",
                "duration_sec", "recheck_count"], limit_page_length=0)
    qc_rows = qc_today + [row for row in qc_open if _value(row, "name") not in qc_names]
    output = build_metrics(
        pack_rows, return_rows, return_items, dispatch_rows,
        [_value(row, "awb") for row in dispatched],
        _heartbeats(line_count) if day == getdate(nowdate()) else {},
        now_datetime(), line_count=line_count, pending_return_count=pending,
        qc_rows=qc_rows)
    b2b_lots = frappe.get_all(
        "B2B Return Lot", filters={"received_at": ["between", [start, end]]},
        fields=["name", "status", "channel", "inventory_treatment", "expected_cartons",
                "received_cartons", "exception"], limit_page_length=0)
    lot_names = [_value(row, "name") for row in b2b_lots]
    b2b_items = frappe.get_all(
        "B2B Return Lot Item", filters={"parent": ["in", lot_names]},
        fields=["expected_qty", "received_qty", "good_qty", "repairable_qty",
                "scrap_qty", "investigation_qty"], limit_page_length=0) if lot_names else []
    output["b2b_returns"] = build_b2b_return_metrics(b2b_lots, b2b_items)
    return output

"""Repository-level safety checks that run without a Frappe bench.

These tests protect the non-negotiable boundary: warehouse-floor controllers
may prepare drafts but must not post ERPNext documents.
"""

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "solara_wms" / "wms" / "doctype"
FLOOR_CONTROLLERS = (
    "wms_asn/wms_asn.py",
    "wms_cycle_count/wms_cycle_count.py",
    "wms_dispatch/wms_dispatch.py",
    "wms_pack_station/wms_pack_station.py",
    "wms_task/wms_task.py",
)
COMPLETION_METHODS = {
    "wms_asn/wms_asn.py": "complete_asn",
    "wms_cycle_count/wms_cycle_count.py": "complete_count",
    "wms_dispatch/wms_dispatch.py": "dispatch",
    "wms_pack_station/wms_pack_station.py": "complete_packing",
    "wms_task/wms_task.py": "complete_task",
}


def test_floor_controllers_never_submit_erp_documents():
    violations = []
    for relative in FLOOR_CONTROLLERS:
        path = LEGACY / relative
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "submit"
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert violations == []


def test_new_doctype_json_is_valid():
    paths = (
        LEGACY / "wms_bin_balance/wms_bin_balance.json",
        LEGACY / "wms_movement/wms_movement.json",
        LEGACY / "wms_item_location/wms_item_location.json",
        LEGACY / "wms_settings/wms_settings.json",
        LEGACY / "warehouse_bin/warehouse_bin.json",
    )
    for path in paths:
        assert json.loads(path.read_text())["doctype"] == "DocType"


def test_shadow_ledger_doctypes_are_read_only_and_idempotent():
    balance = json.loads(
        (LEGACY / "wms_bin_balance/wms_bin_balance.json").read_text()
    )
    movement = json.loads((LEGACY / "wms_movement/wms_movement.json").read_text())
    balance_fields = {field["fieldname"]: field for field in balance["fields"]}
    movement_fields = {field["fieldname"]: field for field in movement["fields"]}

    assert balance["track_changes"] == 0
    assert movement["track_changes"] == 0
    for fieldname in ("warehouse", "bin", "item_code"):
        assert balance_fields[fieldname]["reqd"] == 1
        assert balance_fields[fieldname]["read_only"] == 1
    assert movement_fields["idempotency_key"]["unique"] == 1
    assert movement_fields["request_hash"]["reqd"] == 1


def test_shadow_inventory_service_has_atomic_lock_and_no_erp_posting():
    path = ROOT / "solara_wms" / "wms" / "inventory.py"
    source = path.read_text()
    tree = ast.parse(source)
    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"submit", "commit"}:
                forbidden.append(f"{node.func.attr}:{node.lineno}")

    assert forbidden == []
    assert "FOR UPDATE" in source
    assert "ORDER BY name" in source
    assert "available_qty >= %s" in source
    assert 'require_wms_mode("Shadow", "Draft Handoff")' in source
    assert "class IdempotencyConflict(frappe.ValidationError)" in source
    assert "http_status_code = 409" in source


def test_item_location_is_warehouse_scoped():
    schema = json.loads(
        (LEGACY / "wms_item_location/wms_item_location.json").read_text()
    )
    fields = {field["fieldname"]: field for field in schema["fields"]}
    assert fields["warehouse"]["reqd"] == 1
    assert fields["bin"]["reqd"] == 1
    assert fields["item_code"]["reqd"] == 1


def test_bin_codes_are_unique_within_not_across_warehouses():
    schema = json.loads((LEGACY / "warehouse_bin/warehouse_bin.json").read_text())
    fields = {field["fieldname"]: field for field in schema["fields"]}
    assert not fields["bin_code"].get("unique")
    controller = (LEGACY / "warehouse_bin/warehouse_bin.py").read_text()
    assert '"warehouse": self.warehouse' in controller
    assert '"bin_code": self.bin_code' in controller


def test_floor_completion_methods_are_execution_gated():
    for relative, method_name in COMPLETION_METHODS.items():
        tree = ast.parse((LEGACY / relative).read_text())
        method = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        )
        calls = {
            node.func.id
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "require_wms_mode" in calls, f"{relative}:{method_name} is ungated"

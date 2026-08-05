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
        LEGACY / "wms_work/wms_work.json",
        LEGACY / "wms_work_event/wms_work_event.json",
        LEGACY / "wms_work_line/wms_work_line.json",
        LEGACY / "wms_pack_handoff/wms_pack_handoff.json",
        LEGACY / "wms_pack_handoff_line/wms_pack_handoff_line.json",
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


def test_work_ledger_is_read_only_idempotent_and_erp_isolated():
    work = json.loads((LEGACY / "wms_work/wms_work.json").read_text())
    event = json.loads((LEGACY / "wms_work_event/wms_work_event.json").read_text())
    line = json.loads((LEGACY / "wms_work_line/wms_work_line.json").read_text())
    work_fields = {field["fieldname"]: field for field in work["fields"]}
    event_fields = {field["fieldname"]: field for field in event["fields"]}

    assert work["track_changes"] == 0
    assert event["track_changes"] == 0
    assert line["istable"] == 1
    assert work_fields["warehouse"]["reqd"] == 1
    assert work_fields["creation_idempotency_key"]["unique"] == 1
    assert event_fields["idempotency_key"]["unique"] == 1

    service = (ROOT / "solara_wms" / "wms" / "work.py").read_text()
    tree = ast.parse(service)
    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"submit", "commit"}:
                forbidden.append(f"{node.func.attr}:{node.lineno}")
    assert forbidden == []
    assert "FOR UPDATE" in service
    assert "available_qty >= %s" in service
    assert "_require_shadow_write(warehouse)" in service
    assert "def scan_pick(" in service
    assert "def close_pick_shortage(" in service
    assert "Pick exceeds work allocation" in (
        ROOT / "solara_wms" / "wms" / "inventory_domain.py"
    ).read_text()
    assert "Pick Scan" in event_fields["event_type"]["options"]
    assert "Pick Shortage" in event_fields["event_type"]["options"]
    movement = json.loads((LEGACY / "wms_movement/wms_movement.json").read_text())
    movement_fields = {field["fieldname"]: field for field in movement["fields"]}
    assert "Pick" in movement_fields["movement_type"]["options"]


def test_pack_handoff_is_opt_in_append_only_and_one_time():
    settings = json.loads((LEGACY / "wms_settings/wms_settings.json").read_text())
    setting_fields = {field["fieldname"]: field for field in settings["fields"]}
    gate = setting_fields["require_pick_handoff_for_pack"]
    assert gate["fieldtype"] == "Check"
    assert gate["default"] == "0"

    handoff = json.loads(
        (LEGACY / "wms_pack_handoff/wms_pack_handoff.json").read_text()
    )
    handoff_fields = {field["fieldname"]: field for field in handoff["fields"]}
    line = json.loads(
        (LEGACY / "wms_pack_handoff_line/wms_pack_handoff_line.json").read_text()
    )
    line_fields = {field["fieldname"]: field for field in line["fields"]}
    assert handoff["track_changes"] == 0
    assert handoff_fields["awb"]["unique"] == 1
    assert handoff_fields["idempotency_key"]["unique"] == 1
    assert line_fields["work"]["unique"] == 1

    service = (ROOT / "solara_wms" / "wms" / "pack_handoff.py").read_text()
    tree = ast.parse(service)
    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"submit", "commit"}:
                forbidden.append(f"{node.func.attr}:{node.lineno}")
    assert forbidden == []
    assert "FOR UPDATE" in service
    assert "pack_handoff IS NULL" in service
    assert 'mode in ("Shadow", "Draft Handoff")' in service

    pack = (ROOT / "solara_wms" / "wms" / "d2c_pack_verify.py").read_text()
    dispatch = (ROOT / "solara_wms" / "wms" / "d2c_dispatch.py").read_text()
    assert "pack_handoff_status" in pack
    assert "consume_pack_handoff" in pack
    assert "dispatch_pack_handoff_status" in dispatch


def test_item_location_is_warehouse_scoped():
    schema = json.loads(
        (LEGACY / "wms_item_location/wms_item_location.json").read_text()
    )
    fields = {field["fieldname"]: field for field in schema["fields"]}
    assert fields["warehouse"]["reqd"] == 1
    assert fields["bin"]["reqd"] == 1
    assert fields["item_code"]["reqd"] == 1


def test_shopify_cancellation_hold_blocks_all_warehouse_exit_points():
    release = (ROOT / "solara_wms" / "wms" / "d2c_fulfillment.py").read_text()
    pack = (ROOT / "solara_wms" / "wms" / "d2c_pack_verify.py").read_text()
    dispatch = (ROOT / "solara_wms" / "wms" / "d2c_dispatch.py").read_text()
    control = (ROOT / "solara_wms" / "wms" / "shopify_cancellations.py").read_text()
    fixtures = json.loads(
        (ROOT / "solara_wms" / "fixtures" / "custom_field.json").read_text()
    )
    fixture_names = {row["name"] for row in fixtures}

    assert 'so.get("custom_shopify_cancellation_hold")' in release
    assert 'filters["custom_shopify_cancellation_hold"] = 0' in release
    assert 'dn.get("custom_shopify_cancellation_hold")' in release
    assert pack.count("delivery_note_cancellation_hold(dn)") >= 2
    assert "delivery_note_cancellation_hold(dn)" in dispatch
    assert "def apply_cancellation_hold" in control
    assert ".cancel(" not in control
    assert ".submit(" not in control
    assert "frappe.db.commit()" in control
    assert "Sales Order-custom_shopify_cancellation_hold" in fixture_names
    assert "Delivery Note-custom_shopify_cancellation_hold" in fixture_names


def test_shopify_cancellation_evidence_is_read_only_and_fail_closed():
    control = (ROOT / "solara_wms" / "wms" / "shopify_cancellations.py").read_text()

    assert "def cancellation_evidence" in control
    assert 'return "MOVED_RTO"' in control
    assert 'return "NOT_MOVED_CAN_VOID"' in control
    assert 'return "CARRIER_REVIEW"' in control
    # The evidence path may read documents and courier state, but must not
    # cancel ERPNext documents or commit shipment mutations.
    evidence_body = control.split("def cancellation_evidence", 1)[1].split(
        "@frappe.whitelist()\ndef apply_cancellation_hold", 1
    )[0]
    assert ".cancel(" not in evidence_body
    assert "frappe.db.commit" not in evidence_body


def test_bin_codes_are_unique_within_not_across_warehouses():
    schema = json.loads((LEGACY / "warehouse_bin/warehouse_bin.json").read_text())
    fields = {field["fieldname"]: field for field in schema["fields"]}
    assert not fields["bin_code"].get("unique")
    controller = (LEGACY / "warehouse_bin/warehouse_bin.py").read_text()
    assert '"warehouse": self.warehouse' in controller
    assert '"bin_code": self.bin_code' in controller


def test_location_master_has_immutable_qr_and_draft_only_import():
    schema = json.loads((LEGACY / "warehouse_bin/warehouse_bin.json").read_text())
    fields = {field["fieldname"]: field for field in schema["fields"]}
    assert fields["location_id"]["unique"] == 1
    assert fields["qr_payload"]["read_only"] == 1
    assert fields["commissioning_status"]["read_only"] == 1
    assert "FW" in fields["hall_code"]["options"]
    assert "L-10X12" in fields["bay_module"]["options"]
    controller = (LEGACY / "warehouse_bin/warehouse_bin.py").read_text()
    assert "Location ID is immutable" in controller

    service = (ROOT / "solara_wms" / "wms" / "location_master.py").read_text()
    tree = ast.parse(service)
    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"submit", "commit"}:
                forbidden.append(f"{node.func.attr}:{node.lineno}")
    assert forbidden == []
    assert '"commissioning_status": "Draft"' in service
    assert '"status": "Blocked"' in service
    assert '"is_active": 0' in service
    assert "Confirmation hash" in service
    assert "Location ID already exists with different master data" in service
    assert "def commission_location(" in service
    assert '"Draft": "Marked"' in service
    assert "signed baseline count reference" in service
    assert "def retire_location(" in service
    assert "controlled_location_transition" in controller
    assert "resolve_location_scan" in (
        ROOT / "solara_wms" / "wms" / "work.py"
    ).read_text()


def test_location_identity_backfill_syncs_schema_before_querying_new_fields():
    patch = (
        ROOT
        / "solara_wms"
        / "patches"
        / "v1_0"
        / "backfill_warehouse_location_identity.py"
    ).read_text()
    assert patch.index('frappe.reload_doc("wms", "doctype", "warehouse_bin")') < patch.index(
        "frappe.get_all("
    )


def test_legacy_bin_generator_cannot_create_or_delete():
    source = (ROOT / "solara_wms" / "wms" / "generate_bins.py").read_text()
    assert "Legacy rack-based bin generation is disabled" in source
    assert "Bulk Warehouse Bin deletion is permanently disabled" in source
    assert "frappe.db.delete" not in source
    assert "frappe.db.commit" not in source


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

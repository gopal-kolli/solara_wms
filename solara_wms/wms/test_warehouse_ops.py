from datetime import datetime, timedelta
from unittest import TestCase

from solara_wms.wms.warehouse_ops import build_b2b_return_metrics, build_metrics


class TestWarehouseOpsMetrics(TestCase):
    def test_b2b_return_lots_keep_channel_and_disposition_visible(self):
        lots = [
            {"channel": "Blinkit", "inventory_treatment": "Consignment Return",
             "status": "Pending HQ Review", "expected_cartons": 10,
             "received_cartons": 9, "exception": 1},
            {"channel": "Swiggy", "inventory_treatment": "Outright Return",
             "status": "QC In Progress", "expected_cartons": 2,
             "received_cartons": 2, "exception": 0},
        ]
        items = [
            {"expected_qty": 20, "received_qty": 18, "good_qty": 14,
             "repairable_qty": 2, "scrap_qty": 1, "investigation_qty": 1},
            {"expected_qty": 3, "received_qty": 3, "good_qty": 3,
             "repairable_qty": 0, "scrap_qty": 0, "investigation_qty": 0},
        ]
        out = build_b2b_return_metrics(lots, items)
        self.assertEqual(out["lots"], 2)
        self.assertEqual(out["received_cartons"], 11)
        self.assertEqual(out["good_units"], 17)
        self.assertEqual(out["pending_review"], 1)
        self.assertEqual(out["exceptions"], 1)
        self.assertEqual(out["channels"], {"Blinkit": 1, "Swiggy": 1})

    def test_lines_show_fair_rates_presence_and_completed_multibox_orders(self):
        now = datetime(2026, 8, 2, 18, 0)
        pack = [
            {"station": "Line 1", "delivery_note": "DN-1", "awb": "A1",
             "box_count": 1, "pieces_expected": 1, "mismatch": 0,
             "duration_sec": 30, "verified_at": now - timedelta(minutes=10),
             "prepare_batch": "D2CB-2026-08-02-001"},
            {"station": "Line 2", "delivery_note": "DN-2", "awb": "B1",
             "box_count": 2, "pieces_expected": 4, "mismatch": 0,
             "duration_sec": 90, "verified_at": now - timedelta(minutes=20),
             "prepare_batch": "D2CB-2026-08-02-002"},
            {"station": "Line 2", "delivery_note": "DN-2", "awb": "B2",
             "box_count": 2, "pieces_expected": 2, "mismatch": 0,
             "duration_sec": 60, "verified_at": now - timedelta(minutes=5),
             "prepare_batch": "D2CB-2026-08-02-002"},
        ]

        out = build_metrics(pack, [], [], [], [], {"Line 1": now.isoformat()},
                            now, line_count=3)

        line1, line2, line3 = out["packing"]["lines"]
        self.assertEqual(line1["status"], "active")
        self.assertEqual(line1["parcels_per_hour"], 1)
        self.assertEqual(line1["orders_last_60m"], 1)
        self.assertEqual(line1["last_prepare_batch"], "D2CB-2026-08-02-001")
        self.assertEqual(line2["orders"], 1)
        self.assertEqual(line2["orders_last_60m"], 1)
        self.assertEqual(line2["parcels_last_60m"], 2)
        self.assertEqual(line2["parcels_per_hour"], 2)
        self.assertEqual(line2["pieces_per_hour"], 6)
        self.assertEqual(line2["pieces_last_60m"], 6)
        self.assertEqual(line2["last_prepare_batch"], "D2CB-2026-08-02-002")
        self.assertEqual(line2["avg_pieces"], 3)
        self.assertEqual(line3["status"], "offline")

    def test_last_60m_orders_exclude_incomplete_multibox_work(self):
        now = datetime(2026, 8, 2, 18, 0)
        pack = [
            {"station": "Line 1", "delivery_note": "DN-1", "awb": "A1",
             "box_count": 2, "pieces_expected": 1, "mismatch": 0,
             "verified_at": now - timedelta(minutes=5),
             "prepare_batch": "D2CB-2026-08-02-003"},
        ]

        line = build_metrics(pack, [], [], [], [], {}, now, line_count=1)[
            "packing"]["lines"][0]

        self.assertEqual(line["parcels_last_60m"], 1)
        self.assertEqual(line["orders_last_60m"], 0)
        self.assertEqual(line["orders"], 0)

    def test_quality_returns_dispatch_and_waiting_are_kept_separate(self):
        now = datetime(2026, 8, 2, 18, 0)
        pack = [
            {"station": "Line 1", "delivery_note": "DN-1", "awb": "A1",
             "box_count": 1, "pieces_expected": 3, "mismatch": 0,
             "verified_at": now - timedelta(minutes=30)},
            {"station": "Line 1", "delivery_note": "DN-2", "awb": "A2",
             "box_count": 1, "pieces_expected": 2, "mismatch": 1,
             "verified_at": now - timedelta(minutes=20)},
        ]
        returns = [{"name": "RET-1", "return_type": "RTO",
                    "received_at": now - timedelta(minutes=40),
                    "completed_at": now - timedelta(minutes=10)}]
        return_items = [{"parent": "RET-1", "condition": "Damaged"}]
        dispatch = [{"delivery_note": "DN-X", "awb": "X1", "courier": "Delhivery",
                     "scanned_at": now - timedelta(minutes=5)}]

        out = build_metrics(pack, returns, return_items, dispatch, ["X1"], {}, now,
                            pending_return_count=2)

        self.assertEqual(out["packing"]["issues_caught"], 1)
        self.assertEqual(out["returns"]["processed"], 1)
        self.assertEqual(out["returns"]["conditions"], {"Damaged": 1})
        self.assertEqual(out["returns"]["pending_review"], 2)
        self.assertEqual(out["dispatch"]["parcels"], 1)
        self.assertEqual(out["dispatch"]["waiting_parcels"], 1)

    def test_independent_qc_queue_and_line_results_are_visible(self):
        now = datetime(2026, 8, 2, 18, 0)
        qc = [
            {"station": "Line 1", "status": "Passed", "recheck_count": 0,
             "staged_at": now - timedelta(minutes=4), "duration_sec": 120},
            {"station": "Line 2", "status": "Failed", "recheck_count": 1,
             "staged_at": now - timedelta(minutes=8), "duration_sec": 150},
        ]

        out = build_metrics([], [], [], [], [], {"QC Inspector": now.isoformat()},
                            now, line_count=2, qc_rows=qc)

        self.assertEqual(out["packing"]["lines"][0]["qc_passed"], 1)
        self.assertEqual(out["packing"]["lines"][1]["qc_failures"], 1)
        self.assertEqual(out["quality"]["open_holds"], 1)
        self.assertEqual(out["quality"]["overdue_holds"], 1)
        self.assertEqual(out["quality"]["failures_caught"], 1)
        self.assertEqual(out["quality"]["status"], "active")

    def test_appliance_express_is_attributed_separately(self):
        now = datetime(2026, 8, 2, 18, 0)
        pack = [{"station": "Appliance Express", "delivery_note": "DN-A",
                 "awb": "AX1", "box_count": 1, "pieces_expected": 3,
                 "mismatch": 0, "verified_at": now - timedelta(minutes=10)}]

        out = build_metrics(pack, [], [], [], [],
                            {"Appliance Express": now.isoformat()}, now)

        self.assertEqual(out["appliance_express"]["status"], "active")
        self.assertEqual(out["appliance_express"]["parcels"], 1)
        self.assertEqual(out["appliance_express"]["parcels_per_hour"], 1)
        self.assertEqual(out["packing"]["orders"], 1)

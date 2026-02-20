frappe.ui.form.on("WMS Cycle Count", {
    refresh: function (frm) {
        // ─── Populate & Fetch Buttons ────────────────────────
        if (frm.doc.status === "Draft") {
            frm.add_custom_button(
                __("Populate Items"),
                function () {
                    frm.call("populate_items_from_warehouse").then(() =>
                        frm.reload_doc()
                    );
                },
                __("Actions")
            );
        }

        if (
            frm.doc.status === "Draft" ||
            frm.doc.status === "In Progress"
        ) {
            if ((frm.doc.items || []).length > 0) {
                frm.add_custom_button(
                    __("Fetch Book Quantities"),
                    function () {
                        frm.call("fetch_book_quantities").then(() =>
                            frm.reload_doc()
                        );
                    },
                    __("Actions")
                );
            }
        }

        // ─── Status Action Buttons ───────────────────────────
        if (frm.doc.status === "Draft" && (frm.doc.items || []).length > 0) {
            frm.add_custom_button(
                __("Start Count"),
                function () {
                    frm.call("start_count").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "In Progress") {
            frm.add_custom_button(
                __("Complete Count"),
                function () {
                    frappe.confirm(
                        __(
                            "Complete this cycle count? Variances will be calculated and a Stock Reconciliation may be created."
                        ),
                        function () {
                            frm.call("complete_count").then(() =>
                                frm.reload_doc()
                            );
                        }
                    );
                },
                __("Actions")
            );
        }

        // Cancel button
        if (
            frm.doc.status !== "Completed" &&
            frm.doc.status !== "Cancelled"
        ) {
            frm.add_custom_button(
                __("Cancel Count"),
                function () {
                    frappe.confirm(
                        __("Cancel this cycle count?"),
                        function () {
                            frm.call("cancel_count").then(() =>
                                frm.reload_doc()
                            );
                        }
                    );
                },
                __("Actions")
            );
        }

        // ─── Status Indicators ───────────────────────────────
        if (frm.doc.status === "Completed") {
            let msg = "Cycle count COMPLETED.";
            if (frm.doc.items_with_variance > 0) {
                msg +=
                    " " +
                    frm.doc.items_with_variance +
                    " item(s) with variance.";
            } else {
                msg += " No variances found.";
            }
            if (frm.doc.stock_reconciliation) {
                msg +=
                    ' <a href="/app/stock-reconciliation/' +
                    frm.doc.stock_reconciliation +
                    '">' +
                    frm.doc.stock_reconciliation +
                    "</a>";
            }
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-success">' + msg + "</div>"
            );
        } else if (frm.doc.status === "In Progress") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-info">Counting IN PROGRESS. Enter counted quantities.</div>'
            );
        } else if (frm.doc.status === "Cancelled") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-danger">This count has been CANCELLED.</div>'
            );
        }

        // ─── Row color coding ────────────────────────────────
        if (frm.doc.items) {
            frm.fields_dict.items.grid.grid_rows.forEach(function (row) {
                let status = row.doc.row_status;
                if (status === "Matched") {
                    row.wrapper.css("background-color", "#d4edda");
                } else if (status === "Variance") {
                    let pct = Math.abs(row.doc.variance_pct || 0);
                    if (pct > 10) {
                        row.wrapper.css("background-color", "#f8d7da"); // Red for >10%
                    } else {
                        row.wrapper.css("background-color", "#fff3cd"); // Yellow for <=10%
                    }
                }
            });
        }

        // ─── Filter queries ──────────────────────────────────
        frm.set_query("warehouse", function () {
            return { filters: { is_group: 0 } };
        });
        frm.set_query("bin", function () {
            if (frm.doc.warehouse) {
                return {
                    filters: {
                        warehouse: frm.doc.warehouse,
                        is_active: 1,
                    },
                };
            }
            return { filters: { is_active: 1 } };
        });
    },
});

// ─── Child Table Events ──────────────────────────────────────
frappe.ui.form.on("WMS Cycle Count Item", {
    counted_qty: function (frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        let variance = flt(row.counted_qty) - flt(row.book_qty);
        frappe.model.set_value(cdt, cdn, "variance_qty", variance);

        let pct = 0;
        if (flt(row.book_qty) !== 0) {
            pct = (variance / flt(row.book_qty)) * 100;
        } else if (variance !== 0) {
            pct = 100;
        }
        frappe.model.set_value(cdt, cdn, "variance_pct", pct);

        let value = variance * flt(row.valuation_rate);
        frappe.model.set_value(cdt, cdn, "variance_value", value);
    },
});

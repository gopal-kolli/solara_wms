frappe.ui.form.on("WMS Task", {
    refresh: function (frm) {
        // ─── Assign Button (Pending) ─────────────────────────
        if (frm.doc.status === "Pending") {
            frm.add_custom_button(
                __("Assign"),
                function () {
                    let d = new frappe.ui.Dialog({
                        title: __("Assign Task"),
                        fields: [
                            {
                                fieldname: "user",
                                fieldtype: "Link",
                                label: __("Assign To"),
                                options: "User",
                                reqd: 1,
                                default: frm.doc.assigned_to || "",
                            },
                        ],
                        primary_action_label: __("Assign"),
                        primary_action(values) {
                            frm.call("assign_task", { user: values.user }).then(() => {
                                d.hide();
                                frm.reload_doc();
                            });
                        },
                    });
                    d.show();
                },
                __("Actions")
            );
        }

        // ─── Start Button (Pending or Assigned) ──────────────
        if (frm.doc.status === "Pending" || frm.doc.status === "Assigned") {
            frm.add_custom_button(
                __("Start Task"),
                function () {
                    frm.call("start_task").then(() => {
                        frm.reload_doc();
                    });
                },
                __("Actions")
            );
        }

        // ─── Complete Button (Assigned or In Progress) ───────
        if (frm.doc.status === "Assigned" || frm.doc.status === "In Progress") {
            frm.add_custom_button(
                __("Complete Task"),
                function () {
                    let item_count = (frm.doc.items || []).length;
                    let msg = __(
                        "Complete this {0} task with {1} item(s)?<br><br>" +
                            "This will create the corresponding ERPNext stock document.",
                        [frm.doc.task_type, item_count]
                    );

                    if (frm.doc.task_type === "Count") {
                        // For Count tasks, check if actual_qty has been filled
                        let unfilled = (frm.doc.items || []).filter(
                            (r) => !r.actual_qty && r.actual_qty !== 0
                        ).length;
                        if (unfilled > 0) {
                            msg +=
                                __("<br><b>Warning:</b> {0} item(s) have no Actual Qty entered. " +
                                    "They will default to Required Qty (no difference).", [unfilled]);
                        }
                    }

                    frappe.confirm(msg, function () {
                        frappe.show_alert({
                            message: __("Completing task..."),
                            indicator: "blue",
                        });
                        frm.call("complete_task").then((r) => {
                            if (r.message) {
                                frm.reload_doc();
                            }
                        });
                    });
                },
                __("Actions")
            );
        }

        // ─── Pick Route Optimization (Pick tasks, not Completed/Cancelled) ──
        if (
            frm.doc.task_type === "Pick" &&
            frm.doc.status !== "Completed" &&
            frm.doc.status !== "Cancelled"
        ) {
            frm.add_custom_button(
                __("Optimize Pick Route"),
                function () {
                    frappe.confirm(
                        __(
                            "Optimize pick route? This will auto-assign bins and reorder items " +
                            "for the fastest warehouse walk (serpentine routing)."
                        ),
                        function () {
                            frappe.call({
                                method: "solara_wms.wms.pick_route.apply_optimized_route",
                                args: { task_name: frm.doc.name },
                                freeze: true,
                                freeze_message: __("Optimizing pick route..."),
                                callback: function () {
                                    frm.reload_doc();
                                },
                            });
                        }
                    );
                },
                __("Actions")
            );

            frm.add_custom_button(
                __("Preview Route"),
                function () {
                    frappe.call({
                        method: "solara_wms.wms.pick_route.get_optimized_pick_route",
                        args: { task_name: frm.doc.name },
                        freeze: true,
                        freeze_message: __("Calculating route..."),
                        callback: function (r) {
                            if (!r.message || r.message.length === 0) {
                                frappe.msgprint(__("No items to optimize."));
                                return;
                            }
                            show_route_preview_dialog(frm, r.message);
                        },
                    });
                },
                __("Actions")
            );
        }

        // ─── Sort grid by pick_sequence if optimized ────────
        if (frm.doc.task_type === "Pick" && frm.doc.items) {
            let has_sequence = frm.doc.items.some((r) => r.pick_sequence > 0);
            if (has_sequence) {
                frm.fields_dict.items.grid.grid_rows.sort(function (a, b) {
                    return (a.doc.pick_sequence || 999) - (b.doc.pick_sequence || 999);
                });
            }
        }

        // ─── Cancel Button (not Completed or Cancelled) ──────
        if (frm.doc.status !== "Completed" && frm.doc.status !== "Cancelled") {
            frm.add_custom_button(
                __("Cancel Task"),
                function () {
                    frappe.confirm(
                        __("Cancel this task? This cannot be undone."),
                        function () {
                            frm.call("cancel_task").then(() => {
                                frm.reload_doc();
                            });
                        }
                    );
                },
                __("Actions")
            );
        }

        // ─── Status Indicators ────────────────────────────────
        if (frm.doc.status === "Completed") {
            let links = [];
            if (frm.doc.stock_entry) {
                links.push(
                    '<a href="/app/stock-entry/' +
                        frm.doc.stock_entry +
                        '">' +
                        frm.doc.stock_entry +
                        "</a>"
                );
            }
            if (frm.doc.stock_reconciliation) {
                links.push(
                    '<a href="/app/stock-reconciliation/' +
                        frm.doc.stock_reconciliation +
                        '">' +
                        frm.doc.stock_reconciliation +
                        "</a>"
                );
            }
            let link_text = links.length ? " Created: " + links.join(", ") : "";
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-success">Task COMPLETED.' + link_text + "</div>"
            );
        } else if (frm.doc.status === "Cancelled") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-danger">This task has been CANCELLED.</div>'
            );
        } else if (frm.doc.status === "In Progress") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-info">Task IN PROGRESS — assigned to ' +
                    (frm.doc.assigned_to || "unassigned") +
                    "</div>"
            );
        }

        // ─── Color code row statuses ──────────────────────────
        if (frm.doc.items) {
            frm.fields_dict.items.grid.grid_rows.forEach(function (row) {
                let status = row.doc.row_status;
                if (status === "Completed") {
                    row.wrapper.css("background-color", "#d4edda");
                } else if (status === "Short") {
                    row.wrapper.css("background-color", "#fff3cd");
                } else if (status === "Skipped") {
                    row.wrapper.css("background-color", "#f8d7da");
                }
            });
        }

        // ─── Filter queries ───────────────────────────────────
        frm.set_query("source_warehouse", function () {
            return { filters: { is_group: 0 } };
        });
        frm.set_query("target_warehouse", function () {
            return { filters: { is_group: 0 } };
        });
        frm.set_query("source_bin", function () {
            if (frm.doc.source_warehouse) {
                return {
                    filters: {
                        warehouse: frm.doc.source_warehouse,
                        is_active: 1,
                    },
                };
            }
            return { filters: { is_active: 1 } };
        });
        frm.set_query("target_bin", function () {
            if (frm.doc.target_warehouse) {
                return {
                    filters: {
                        warehouse: frm.doc.target_warehouse,
                        is_active: 1,
                    },
                };
            }
            return { filters: { is_active: 1 } };
        });

        // ─── Conditional field visibility ─────────────────────
        toggle_fields_for_task_type(frm);
    },

    task_type: function (frm) {
        toggle_fields_for_task_type(frm);
    },

    source_warehouse: function (frm) {
        // Clear source_bin if warehouse changes and bin doesn't match
        if (frm.doc.source_bin) {
            frappe.db.get_value(
                "Warehouse Bin",
                frm.doc.source_bin,
                "warehouse",
                (r) => {
                    if (r && r.warehouse !== frm.doc.source_warehouse) {
                        frm.set_value("source_bin", "");
                    }
                }
            );
        }
    },

    target_warehouse: function (frm) {
        // Clear target_bin if warehouse changes and bin doesn't match
        if (frm.doc.target_bin) {
            frappe.db.get_value(
                "Warehouse Bin",
                frm.doc.target_bin,
                "warehouse",
                (r) => {
                    if (r && r.warehouse !== frm.doc.target_warehouse) {
                        frm.set_value("target_bin", "");
                    }
                }
            );
        }
    },
});

function toggle_fields_for_task_type(frm) {
    let tt = frm.doc.task_type;

    // Source warehouse/bin: relevant for Pick, Transfer, Count
    let show_source = ["Pick", "Transfer", "Count"].includes(tt);
    frm.toggle_display("source_warehouse", show_source);
    frm.toggle_display("source_bin", show_source);

    // Target warehouse/bin: relevant for Putaway, Transfer, Pick (staging)
    let show_target = ["Putaway", "Transfer", "Pick"].includes(tt);
    frm.toggle_display("target_warehouse", show_target);
    frm.toggle_display("target_bin", ["Putaway", "Transfer"].includes(tt));

    // Sales Order: relevant for Pick, Pack
    frm.toggle_display("sales_order", ["Pick", "Pack"].includes(tt));

    // Purchase Order: relevant for Putaway
    frm.toggle_display("purchase_order", tt === "Putaway");
}

// ─── Child Table Events ─────────────────────────────────────
frappe.ui.form.on("WMS Task Item", {
    item_code: function (frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        if (row.item_code) {
            frappe.db.get_value("Item", row.item_code, "item_name", (r) => {
                if (r) {
                    frappe.model.set_value(cdt, cdn, "item_name", r.item_name);
                }
            });
        }
    },

    actual_qty: function (frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        // Auto-calculate difference for Count tasks
        if (frm.doc.task_type === "Count") {
            let diff = (row.actual_qty || 0) - (row.qty || 0);
            frappe.model.set_value(cdt, cdn, "difference_qty", diff);
        }
    },

    qty: function (frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        if (frm.doc.task_type === "Count") {
            let diff = (row.actual_qty || 0) - (row.qty || 0);
            frappe.model.set_value(cdt, cdn, "difference_qty", diff);
        }
    },
});

// ─── Route Preview Dialog ────────────────────────────────────────
function show_route_preview_dialog(frm, route_data) {
    let rows = route_data
        .map(function (r) {
            let location = [r.aisle, r.rack, r.shelf, r.level]
                .filter(Boolean)
                .join("-");
            let error_badge = r.error_message
                ? ' <span class="text-danger">(' + r.error_message + ")</span>"
                : "";
            return (
                "<tr>" +
                "<td>" + r.pick_sequence + "</td>" +
                "<td>" + r.item_code + error_badge + "</td>" +
                "<td>" + (r.item_name || "") + "</td>" +
                "<td>" + (r.bin_code || r.source_bin || "-") + "</td>" +
                "<td>" + (location || "-") + "</td>" +
                "<td>" + (r.zone_type || "-") + "</td>" +
                "<td>" + r.qty + "</td>" +
                "</tr>"
            );
        })
        .join("");

    let html =
        '<div style="max-height: 400px; overflow-y: auto;">' +
        '<table class="table table-bordered table-sm">' +
        "<thead><tr>" +
        "<th>#</th><th>Item</th><th>Name</th><th>Bin</th>" +
        "<th>Location</th><th>Zone</th><th>Qty</th>" +
        "</tr></thead>" +
        "<tbody>" +
        rows +
        "</tbody></table></div>";

    let d = new frappe.ui.Dialog({
        title: __("Pick Route Preview (Serpentine)"),
        size: "extra-large",
        fields: [
            {
                fieldtype: "HTML",
                fieldname: "route_table",
                options: html,
            },
        ],
        primary_action_label: __("Apply Route"),
        primary_action: function () {
            frappe.call({
                method: "solara_wms.wms.pick_route.apply_optimized_route",
                args: { task_name: frm.doc.name },
                freeze: true,
                freeze_message: __("Applying optimized route..."),
                callback: function () {
                    d.hide();
                    frm.reload_doc();
                },
            });
        },
        secondary_action_label: __("Close"),
    });

    d.show();
}

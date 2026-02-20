frappe.ui.form.on("WMS ASN", {
    refresh: function (frm) {
        // Auto-set ASN No
        if (!frm.doc.asn_no && frm.doc.name) {
            frm.set_value("asn_no", frm.doc.name);
        }

        // ─── Action Buttons ──────────────────────────────────
        if (frm.doc.status === "Draft") {
            frm.add_custom_button(
                __("Confirm ASN"),
                function () {
                    frm.call("confirm_asn").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Confirmed") {
            frm.add_custom_button(
                __("Mark Arrived"),
                function () {
                    frm.call("mark_arrived").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Arrived") {
            frm.add_custom_button(
                __("Start Unloading"),
                function () {
                    frm.call("start_unloading").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Unloading") {
            frm.add_custom_button(
                __("Complete Sorting"),
                function () {
                    frappe.confirm(
                        __("Complete sorting? This will calculate variances from received quantities."),
                        function () {
                            frm.call("complete_sorting").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Sorted") {
            frm.add_custom_button(
                __("Create Putaway Task"),
                function () {
                    frm.call("create_putaway").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Putaway Created") {
            frm.add_custom_button(
                __("Complete ASN"),
                function () {
                    frappe.confirm(
                        __("Complete this ASN? This will create a Purchase Receipt."),
                        function () {
                            frm.call("complete_asn").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        // Cancel button (not for Completed or Cancelled)
        if (frm.doc.status !== "Completed" && frm.doc.status !== "Cancelled") {
            frm.add_custom_button(
                __("Cancel ASN"),
                function () {
                    frappe.confirm(
                        __("Cancel this ASN? This cannot be undone."),
                        function () {
                            frm.call("cancel_asn").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        // ─── Populate from PO ────────────────────────────────
        if (frm.doc.status === "Draft" && frm.doc.purchase_order) {
            frm.add_custom_button(
                __("Fetch PO Items"),
                function () {
                    frappe.call({
                        method: "frappe.client.get",
                        args: {
                            doctype: "Purchase Order",
                            name: frm.doc.purchase_order,
                        },
                        callback: function (r) {
                            if (r.message && r.message.items) {
                                frm.clear_table("items");
                                r.message.items.forEach(function (item) {
                                    let row = frm.add_child("items");
                                    row.item_code = item.item_code;
                                    row.item_name = item.item_name;
                                    row.expected_qty = item.qty;
                                    row.uom = item.uom;
                                });
                                frm.refresh_field("items");
                                frappe.show_alert({
                                    message: __("Items fetched from Purchase Order"),
                                    indicator: "green",
                                });
                            }
                        },
                    });
                },
                __("Actions")
            );
        }

        // ─── Status Indicators ───────────────────────────────
        if (frm.doc.status === "Completed") {
            let links = [];
            if (frm.doc.purchase_receipt) {
                links.push(
                    '<a href="/app/purchase-receipt/' +
                        frm.doc.purchase_receipt + '">' +
                        frm.doc.purchase_receipt + "</a>"
                );
            }
            if (frm.doc.putaway_task) {
                links.push(
                    '<a href="/app/wms-task/' +
                        frm.doc.putaway_task + '">' +
                        frm.doc.putaway_task + "</a>"
                );
            }
            let link_text = links.length ? " Created: " + links.join(", ") : "";
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-success">ASN COMPLETED.' + link_text + "</div>"
            );
        } else if (frm.doc.status === "Cancelled") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-danger">This ASN has been CANCELLED.</div>'
            );
        } else if (frm.doc.status === "Arrived") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-info">Shipment has ARRIVED. Ready for unloading.</div>'
            );
        } else if (frm.doc.status === "Unloading") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-info">UNLOADING in progress. Enter received quantities.</div>'
            );
        }

        // ─── Row color coding ────────────────────────────────
        if (frm.doc.items) {
            frm.fields_dict.items.grid.grid_rows.forEach(function (row) {
                let status = row.doc.row_status;
                if (status === "Received") {
                    row.wrapper.css("background-color", "#d4edda");
                } else if (status === "Short") {
                    row.wrapper.css("background-color", "#fff3cd");
                } else if (status === "Damaged") {
                    row.wrapper.css("background-color", "#f8d7da");
                }
            });
        }

        // ─── Filter queries ──────────────────────────────────
        frm.set_query("warehouse", function () {
            return { filters: { is_group: 0 } };
        });
        frm.set_query("receiving_bin", function () {
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
frappe.ui.form.on("WMS ASN Item", {
    received_qty: function (frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        let expected = flt(row.expected_qty);
        let received = flt(row.received_qty);

        if (received < expected) {
            frappe.model.set_value(cdt, cdn, "shortage_qty", expected - received);
            frappe.model.set_value(cdt, cdn, "over_qty", 0);
        } else if (received > expected) {
            frappe.model.set_value(cdt, cdn, "over_qty", received - expected);
            frappe.model.set_value(cdt, cdn, "shortage_qty", 0);
        } else {
            frappe.model.set_value(cdt, cdn, "shortage_qty", 0);
            frappe.model.set_value(cdt, cdn, "over_qty", 0);
        }
    },

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
});

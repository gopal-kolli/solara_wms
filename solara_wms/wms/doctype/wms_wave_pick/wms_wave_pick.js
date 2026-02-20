frappe.ui.form.on("WMS Wave Pick", {
    refresh: function (frm) {
        // ─── Add Sales Orders Button ─────────────────────────
        if (frm.doc.status === "Draft") {
            frm.add_custom_button(
                __("Add Sales Orders"),
                function () {
                    let d = new frappe.ui.Dialog({
                        title: __("Select Sales Orders"),
                        fields: [
                            {
                                fieldname: "sales_orders",
                                fieldtype: "Link",
                                label: __("Sales Order"),
                                options: "Sales Order",
                                reqd: 1,
                                get_query: function () {
                                    return {
                                        filters: {
                                            docstatus: 1,
                                            status: ["not in", ["Completed", "Cancelled", "Closed"]],
                                        },
                                    };
                                },
                            },
                        ],
                        primary_action_label: __("Add"),
                        primary_action(values) {
                            // Check if already added
                            let exists = (frm.doc.orders || []).some(
                                (r) => r.sales_order === values.sales_orders
                            );
                            if (exists) {
                                frappe.show_alert({
                                    message: __("Sales Order already added"),
                                    indicator: "orange",
                                });
                                return;
                            }
                            let row = frm.add_child("orders");
                            row.sales_order = values.sales_orders;
                            frm.refresh_field("orders");
                            frappe.show_alert({
                                message: __("Sales Order added"),
                                indicator: "green",
                            });
                            d.fields_dict.sales_orders.set_value("");
                        },
                    });
                    d.show();
                },
                __("Actions")
            );

            // Consolidate Items button
            if ((frm.doc.orders || []).length > 0) {
                frm.add_custom_button(
                    __("Consolidate Items"),
                    function () {
                        frm.call("consolidate_items").then(() => frm.reload_doc());
                    },
                    __("Actions")
                );
            }
        }

        // ─── Status Action Buttons ───────────────────────────
        if (frm.doc.status === "Draft" && (frm.doc.items || []).length > 0) {
            frm.add_custom_button(
                __("Release Wave"),
                function () {
                    frm.call("release_wave").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Released") {
            frm.add_custom_button(
                __("Create Pick Task"),
                function () {
                    frm.call("create_pick_task").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Picking") {
            frm.add_custom_button(
                __("Complete Wave"),
                function () {
                    frappe.confirm(
                        __("Complete this wave? Ensure all items have been picked."),
                        function () {
                            frm.call("complete_wave").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        // Cancel button
        if (frm.doc.status !== "Completed" && frm.doc.status !== "Cancelled") {
            frm.add_custom_button(
                __("Cancel Wave"),
                function () {
                    frappe.confirm(
                        __("Cancel this wave? This cannot be undone."),
                        function () {
                            frm.call("cancel_wave").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        // ─── Status Indicators ───────────────────────────────
        if (frm.doc.status === "Completed") {
            let link_text = "";
            if (frm.doc.wms_task) {
                link_text =
                    ' Pick Task: <a href="/app/wms-task/' +
                    frm.doc.wms_task + '">' +
                    frm.doc.wms_task + "</a>";
            }
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-success">Wave COMPLETED.' + link_text + "</div>"
            );
        } else if (frm.doc.status === "Cancelled") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-danger">This wave has been CANCELLED.</div>'
            );
        } else if (frm.doc.status === "Picking") {
            let link_text = "";
            if (frm.doc.wms_task) {
                link_text =
                    ' <a href="/app/wms-task/' +
                    frm.doc.wms_task + '">' +
                    frm.doc.wms_task + "</a>";
            }
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-info">PICKING in progress.' + link_text + "</div>"
            );
        }

        // ─── Filter queries ──────────────────────────────────
        frm.set_query("warehouse", function () {
            return { filters: { is_group: 0 } };
        });
    },
});

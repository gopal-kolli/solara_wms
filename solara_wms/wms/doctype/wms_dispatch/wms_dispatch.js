frappe.ui.form.on("WMS Dispatch", {
    refresh: function (frm) {
        // ─── Fetch SO Items ──────────────────────────────────
        if (frm.doc.status === "Pending" && frm.doc.sales_order) {
            frm.add_custom_button(
                __("Fetch SO Items"),
                function () {
                    frm.call("fetch_so_items").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        // ─── Status Action Buttons ───────────────────────────
        if (frm.doc.status === "Pending" && (frm.doc.items || []).length > 0) {
            frm.add_custom_button(
                __("Allocate"),
                function () {
                    frm.call("allocate").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Allocated") {
            frm.add_custom_button(
                __("Mark Picked"),
                function () {
                    frm.call("mark_picked").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Picked") {
            frm.add_custom_button(
                __("Mark Packed"),
                function () {
                    frm.call("mark_packed").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Packed") {
            frm.add_custom_button(
                __("Weigh"),
                function () {
                    let d = new frappe.ui.Dialog({
                        title: __("Enter Weight"),
                        fields: [
                            {
                                fieldname: "total_weight",
                                fieldtype: "Float",
                                label: __("Total Weight (kg)"),
                                reqd: 1,
                                default: frm.doc.total_weight || 0,
                            },
                        ],
                        primary_action_label: __("Confirm Weight"),
                        primary_action(values) {
                            frm.call("mark_weighed", {
                                total_weight: values.total_weight,
                            }).then(() => {
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

        if (frm.doc.status === "Weighed") {
            frm.add_custom_button(
                __("Dispatch"),
                function () {
                    let d = new frappe.ui.Dialog({
                        title: __("Dispatch Details"),
                        fields: [
                            {
                                fieldname: "carrier",
                                fieldtype: "Data",
                                label: __("Carrier"),
                                default: frm.doc.carrier || "",
                            },
                            {
                                fieldname: "tracking_no",
                                fieldtype: "Data",
                                label: __("Tracking No"),
                                default: frm.doc.tracking_no || "",
                            },
                        ],
                        primary_action_label: __("Dispatch Now"),
                        primary_action(values) {
                            frappe.confirm(
                                __(
                                    "Dispatch this shipment? This will create a Delivery Note and Packing Slip."
                                ),
                                function () {
                                    frm.call("dispatch", {
                                        carrier: values.carrier,
                                        tracking_no: values.tracking_no,
                                    }).then(() => {
                                        d.hide();
                                        frm.reload_doc();
                                    });
                                }
                            );
                        },
                    });
                    d.show();
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Dispatched") {
            frm.add_custom_button(
                __("Mark Delivered"),
                function () {
                    frappe.confirm(
                        __("Confirm delivery has been received by customer?"),
                        function () {
                            frm.call("mark_delivered").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        // Cancel button
        if (
            frm.doc.status !== "Dispatched" &&
            frm.doc.status !== "Delivered" &&
            frm.doc.status !== "Cancelled"
        ) {
            frm.add_custom_button(
                __("Cancel Dispatch"),
                function () {
                    frappe.confirm(
                        __("Cancel this dispatch? This cannot be undone."),
                        function () {
                            frm.call("cancel_dispatch").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        // ─── Status Indicators ───────────────────────────────
        if (frm.doc.status === "Dispatched" || frm.doc.status === "Delivered") {
            let links = [];
            if (frm.doc.delivery_note) {
                links.push(
                    '<a href="/app/delivery-note/' +
                        frm.doc.delivery_note + '">' +
                        frm.doc.delivery_note + "</a>"
                );
            }
            if (frm.doc.shipment) {
                links.push(
                    '<a href="/app/shipment/' +
                        frm.doc.shipment + '">' +
                        frm.doc.shipment + "</a>"
                );
            }
            let link_text = links.length ? " Documents: " + links.join(", ") : "";
            let alert_class =
                frm.doc.status === "Delivered" ? "alert-success" : "alert-info";
            let label = frm.doc.status === "Delivered" ? "DELIVERED" : "DISPATCHED";
            frm.dashboard.set_headline_alert(
                '<div class="alert ' + alert_class + '">' + label + "." + link_text + "</div>"
            );
        } else if (frm.doc.status === "Cancelled") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-danger">This dispatch has been CANCELLED.</div>'
            );
        }

        // ─── Row color coding ────────────────────────────────
        if (frm.doc.items) {
            frm.fields_dict.items.grid.grid_rows.forEach(function (row) {
                let status = row.doc.row_status;
                if (status === "Dispatched") {
                    row.wrapper.css("background-color", "#d4edda");
                } else if (status === "Packed") {
                    row.wrapper.css("background-color", "#cce5ff");
                } else if (status === "Picked") {
                    row.wrapper.css("background-color", "#fff3cd");
                }
            });
        }

        // ─── Filter queries ──────────────────────────────────
        frm.set_query("warehouse", function () {
            return { filters: { is_group: 0 } };
        });
        frm.set_query("sales_order", function () {
            return {
                filters: {
                    docstatus: 1,
                    status: ["not in", ["Completed", "Cancelled", "Closed"]],
                },
            };
        });
    },
});

frappe.ui.form.on("WMS Pack Station", {
    refresh: function (frm) {
        // ─── Populate from SO ────────────────────────────────
        if (frm.doc.status === "Draft" && frm.doc.sales_order) {
            frm.add_custom_button(
                __("Fetch SO Items"),
                function () {
                    frm.call("populate_from_sales_order").then(() =>
                        frm.reload_doc()
                    );
                },
                __("Actions")
            );
        }

        // ─── Status Action Buttons ───────────────────────────
        if (frm.doc.status === "Draft" && (frm.doc.items || []).length > 0) {
            frm.add_custom_button(
                __("Start Packing"),
                function () {
                    frm.call("start_packing").then(() => frm.reload_doc());
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Packing") {
            // Add Package button
            frm.add_custom_button(
                __("Add Package"),
                function () {
                    frm.call("add_package").then(() => frm.reload_doc());
                },
                __("Actions")
            );

            // Seal Package button
            if ((frm.doc.packages || []).some((p) => p.row_status === "Open")) {
                frm.add_custom_button(
                    __("Seal Package"),
                    function () {
                        let open_packages = (frm.doc.packages || []).filter(
                            (p) => p.row_status === "Open"
                        );
                        if (open_packages.length === 1) {
                            frm.call("seal_package", {
                                package_no: open_packages[0].package_no,
                            }).then(() => frm.reload_doc());
                        } else {
                            let d = new frappe.ui.Dialog({
                                title: __("Seal Package"),
                                fields: [
                                    {
                                        fieldname: "package_no",
                                        fieldtype: "Select",
                                        label: __("Package No"),
                                        options: open_packages
                                            .map((p) => p.package_no)
                                            .join("\n"),
                                        reqd: 1,
                                    },
                                ],
                                primary_action_label: __("Seal"),
                                primary_action(values) {
                                    frm.call("seal_package", {
                                        package_no: values.package_no,
                                    }).then(() => {
                                        d.hide();
                                        frm.reload_doc();
                                    });
                                },
                            });
                            d.show();
                        }
                    },
                    __("Actions")
                );
            }

            // Complete Packing button
            frm.add_custom_button(
                __("Complete Packing"),
                function () {
                    let remaining = (frm.doc.items || []).filter(
                        (r) => flt(r.remaining_qty) > 0
                    ).length;
                    let msg = __("Complete packing?");
                    if (remaining > 0) {
                        msg += __(
                            " {0} item(s) have unpacked quantities and will be marked as Short.",
                            [remaining]
                        );
                    }
                    msg += __(
                        " This will create a Delivery Note and Packing Slip."
                    );

                    frappe.confirm(msg, function () {
                        frm.call("complete_packing").then(() =>
                            frm.reload_doc()
                        );
                    });
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
                __("Cancel Packing"),
                function () {
                    frappe.confirm(
                        __("Cancel this pack station?"),
                        function () {
                            frm.call("cancel_packing").then(() =>
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
            let links = [];
            if (frm.doc.delivery_note) {
                links.push(
                    '<a href="/app/delivery-note/' +
                        frm.doc.delivery_note +
                        '">' +
                        frm.doc.delivery_note +
                        "</a>"
                );
            }
            if (frm.doc.packing_slip) {
                links.push(
                    '<a href="/app/packing-slip/' +
                        frm.doc.packing_slip +
                        '">' +
                        frm.doc.packing_slip +
                        "</a>"
                );
            }
            let link_text = links.length
                ? " Documents: " + links.join(", ")
                : "";
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-success">Packing COMPLETED.' +
                    link_text +
                    "</div>"
            );
        } else if (frm.doc.status === "Packing") {
            let packed = frm.doc.total_packed || 0;
            let total = frm.doc.total_to_pack || 0;
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-info">PACKING in progress. ' +
                    packed +
                    "/" +
                    total +
                    " items packed.</div>"
            );
        } else if (frm.doc.status === "Cancelled") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-danger">This pack station has been CANCELLED.</div>'
            );
        }

        // ─── Row color coding ────────────────────────────────
        if (frm.doc.items) {
            frm.fields_dict.items.grid.grid_rows.forEach(function (row) {
                let status = row.doc.row_status;
                if (status === "Packed") {
                    row.wrapper.css("background-color", "#d4edda");
                } else if (status === "Short") {
                    row.wrapper.css("background-color", "#fff3cd");
                }
            });
        }

        // ─── Auto-focus scan input ───────────────────────────
        if (frm.doc.status === "Packing") {
            setTimeout(function () {
                let scan_field = frm.fields_dict.scan_input;
                if (scan_field && scan_field.$input) {
                    scan_field.$input.focus();
                }
            }, 500);
        }

        // ─── Filter queries ──────────────────────────────────
        frm.set_query("warehouse", function () {
            return { filters: { is_group: 0 } };
        });
        frm.set_query("sales_order", function () {
            return {
                filters: {
                    docstatus: 1,
                    status: [
                        "not in",
                        ["Completed", "Cancelled", "Closed"],
                    ],
                },
            };
        });
    },

    // ─── Barcode Scan Handler ────────────────────────────────
    scan_input: function (frm) {
        let barcode = frm.doc.scan_input;
        if (!barcode) return;

        if (frm.doc.status !== "Packing") {
            frappe.show_alert({
                message: __("Start packing before scanning items"),
                indicator: "orange",
            });
            frm.set_value("scan_input", "");
            return;
        }

        frm.call("scan_item", { barcode: barcode }).then((r) => {
            if (r.message) {
                let result = r.message;
                if (result.success) {
                    frappe.show_alert({
                        message: result.message,
                        indicator: "green",
                    });
                } else {
                    frappe.show_alert({
                        message: result.message,
                        indicator: "red",
                    });
                }
            }
            // Clear scan input and refocus
            frm.set_value("scan_input", "");
            frm.reload_doc().then(() => {
                setTimeout(function () {
                    let scan_field = frm.fields_dict.scan_input;
                    if (scan_field && scan_field.$input) {
                        scan_field.$input.focus();
                    }
                }, 300);
            });
        });
    },
});

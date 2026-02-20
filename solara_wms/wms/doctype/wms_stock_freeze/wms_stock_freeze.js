frappe.ui.form.on("WMS Stock Freeze", {
    refresh: function (frm) {
        // ─── Action Buttons ──────────────────────────────────
        if (frm.doc.status === "Draft") {
            frm.add_custom_button(
                __("Activate Freeze"),
                function () {
                    frappe.confirm(
                        __(
                            "Activate this stock freeze? Stock matching the specified scope will be prevented from movement."
                        ),
                        function () {
                            frm.call("activate_freeze").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        if (frm.doc.status === "Active") {
            frm.add_custom_button(
                __("Release Freeze"),
                function () {
                    frappe.confirm(
                        __("Release this stock freeze? Stock will be available for movement again."),
                        function () {
                            frm.call("release_freeze").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        // Cancel button
        if (frm.doc.status !== "Released" && frm.doc.status !== "Cancelled") {
            frm.add_custom_button(
                __("Cancel Freeze"),
                function () {
                    frappe.confirm(
                        __("Cancel this freeze?"),
                        function () {
                            frm.call("cancel_freeze").then(() => frm.reload_doc());
                        }
                    );
                },
                __("Actions")
            );
        }

        // ─── Status Indicators ───────────────────────────────
        if (frm.doc.status === "Active") {
            let scope = [];
            if (frm.doc.item_code) scope.push("Item: " + frm.doc.item_code);
            if (frm.doc.warehouse) scope.push("Warehouse: " + frm.doc.warehouse);
            if (frm.doc.bin) scope.push("Bin: " + frm.doc.bin);
            if (frm.doc.batch_no) scope.push("Batch: " + frm.doc.batch_no);
            let scope_text = scope.length ? " (" + scope.join(", ") + ")" : "";

            frm.dashboard.set_headline_alert(
                '<div class="alert alert-danger">STOCK FROZEN' + scope_text + "</div>"
            );
        } else if (frm.doc.status === "Released") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-success">Freeze RELEASED by ' +
                    (frm.doc.released_by || "unknown") + "</div>"
            );
        } else if (frm.doc.status === "Cancelled") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-warning">This freeze has been CANCELLED.</div>'
            );
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

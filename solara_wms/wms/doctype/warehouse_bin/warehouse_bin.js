frappe.ui.form.on("Warehouse Bin", {
    refresh: function (frm) {
        // ─── Status Action Buttons ────────────────────────────
        if (!frm.is_new()) {
            if (frm.doc.status !== "Blocked") {
                frm.add_custom_button(
                    __("Block Bin"),
                    function () {
                        frappe.confirm(
                            __("Block bin {0}? It will not be available for WMS tasks.", [
                                frm.doc.bin_code,
                            ]),
                            function () {
                                frm.call("set_status", { new_status: "Blocked" }).then(() => {
                                    frm.reload_doc();
                                });
                            }
                        );
                    },
                    __("Actions")
                );
            }

            if (frm.doc.status === "Blocked" || frm.doc.status === "Maintenance") {
                frm.add_custom_button(
                    __("Reactivate"),
                    function () {
                        frm.call("set_status", { new_status: "Active" }).then(() => {
                            frm.reload_doc();
                        });
                    },
                    __("Actions")
                );
            }

            if (frm.doc.status !== "Full" && frm.doc.status !== "Blocked") {
                frm.add_custom_button(
                    __("Mark Full"),
                    function () {
                        frm.call("set_status", { new_status: "Full" }).then(() => {
                            frm.reload_doc();
                        });
                    },
                    __("Actions")
                );
            }
        }

        // ─── Status Indicators ────────────────────────────────
        if (frm.doc.status === "Blocked") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-danger">This bin is BLOCKED and unavailable for operations.</div>'
            );
        } else if (frm.doc.status === "Full") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-warning">This bin is marked FULL.</div>'
            );
        } else if (frm.doc.status === "Maintenance") {
            frm.dashboard.set_headline_alert(
                '<div class="alert alert-info">This bin is under MAINTENANCE.</div>'
            );
        }

        // ─── Filter warehouse to leaf nodes only ──────────────
        frm.set_query("warehouse", function () {
            return {
                filters: { is_group: 0 },
            };
        });
    },

    // ─── Auto-build bin_code from location fields ─────────────
    aisle: function (frm) {
        auto_build_bin_code(frm);
    },
    rack: function (frm) {
        auto_build_bin_code(frm);
    },
    shelf: function (frm) {
        auto_build_bin_code(frm);
    },
    level: function (frm) {
        auto_build_bin_code(frm);
    },

    // ─── Auto-calculate volume from dimensions ────────────────
    bin_length: function (frm) {
        calculate_volume(frm);
    },
    bin_width: function (frm) {
        calculate_volume(frm);
    },
    bin_height: function (frm) {
        calculate_volume(frm);
    },
});

function auto_build_bin_code(frm) {
    let parts = [];
    if (frm.doc.aisle) parts.push(frm.doc.aisle);
    if (frm.doc.rack) parts.push(frm.doc.rack);
    if (frm.doc.shelf) parts.push(frm.doc.shelf);
    if (frm.doc.level) parts.push(frm.doc.level);
    if (parts.length > 0) {
        frm.set_value("bin_code", parts.join("-"));
    }
}

function calculate_volume(frm) {
    let vol =
        (frm.doc.bin_length || 0) *
        (frm.doc.bin_width || 0) *
        (frm.doc.bin_height || 0);
    frm.set_value("bin_volume", vol);
}

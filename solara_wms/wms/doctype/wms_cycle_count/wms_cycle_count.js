frappe.ui.form.on("WMS Cycle Count", {
    refresh(frm) {
        if (frm.doc.status === "Draft") {
            frm.add_custom_button(__("Populate Bin Items"), () => {
                frm.call("populate_items_from_warehouse").then(() => frm.reload_doc());
            }, __("Actions"));
            frm.add_custom_button(__("Start Blind Count"), () => {
                frm.call("start_count").then(() => frm.reload_doc());
            }, __("Actions"));
        }
        if (["In Progress", "Recount Required"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Evaluate Evidence"), () => {
                frappe.confirm(
                    __("Evaluate immutable count evidence? No stock document will be created."),
                    () => frm.call("complete_count").then(() => frm.reload_doc())
                );
            }, __("Actions"));
        }
        if (!["Completed", "Invalidated", "Cancelled"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Cancel Count"), () => {
                frappe.confirm(__("Cancel this cycle count?"), () => {
                    frm.call("cancel_count").then(() => frm.reload_doc());
                });
            }, __("Actions"));
        }

        const messages = {
            "In Progress": ["info", "Blind count in progress. Record every item, including zero, through the scanner API."],
            "Recount Required": ["warning", "Independent recount required. The original counter cannot perform attempt 2."],
            "Variance Review": ["warning", "Investigate the source transaction before any adjustment."],
            "Completed": ["success", "Count completed with no unexplained variance."],
            "Invalidated": ["danger", "Stock moved after the frozen snapshot. Start a fresh count."],
            "Cancelled": ["danger", "This count is cancelled."],
        };
        const message = messages[frm.doc.status];
        if (message) {
            frm.dashboard.set_headline_alert(
                `<div class="alert alert-${message[0]}">${__(message[1])}</div>`
            );
        }
        frm.set_query("warehouse", () => ({filters: {is_group: 0}}));
        frm.set_query("bin", () => ({
            filters: {warehouse: frm.doc.warehouse, is_active: 1}
        }));
    },
});

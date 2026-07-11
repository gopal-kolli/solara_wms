frappe.ui.form.on("D2C Fulfillment Settings", {
    refresh(frm) {
        frm.add_custom_button(__("Run Release Now"), () => {
            frm.call("run_release_now").then((r) => {
                frappe.msgprint({
                    title: __("Release Result"),
                    message: "<pre>" + frappe.utils.escape_html(
                        JSON.stringify(r.message || {}, null, 2)) + "</pre>",
                    indicator: "blue",
                });
            });
        });

        frm.add_custom_button(__("Fetch Labels Now"), () => {
            frm.call("fetch_labels_now").then((r) => {
                frappe.msgprint({
                    title: __("Label Fetch Result"),
                    message: "<pre>" + frappe.utils.escape_html(
                        JSON.stringify(r.message || {}, null, 2)) + "</pre>",
                    indicator: "blue",
                });
            });
        });

        frm.add_custom_button(__("Prepare Today's Shipments"), () => {
            frappe.prompt(
                [{ fieldname: "on_date", label: __("Date (blank = today)"),
                   fieldtype: "Date" }],
                (values) => {
                    frm.call("prepare_now", { on_date: values.on_date }).then((r) => {
                        const m = r.message || {};
                        let html = "<pre>" + frappe.utils.escape_html(
                            JSON.stringify(m, null, 2)) + "</pre>";
                        if (m.pick_list_url) {
                            html += `<p><a href="${m.pick_list_url}" target="_blank">`
                                 + __("Open Pick List") + "</a></p>";
                        }
                        if (m.labels_pdf_url) {
                            html += `<p><a href="${m.labels_pdf_url}" target="_blank">`
                                 + __("Open Combined Labels") + "</a></p>";
                        }
                        frappe.msgprint({ title: __("Prepare Result"),
                            message: html, indicator: "green" });
                    });
                },
                __("Prepare Today's D2C Shipments"),
                __("Prepare"),
            );
        });
    },
});

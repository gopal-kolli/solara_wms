frappe.ui.form.on("D2C Fulfillment Settings", {
    refresh(frm) {
        // Readable one-line summary of a release result.
        const fmt_release = (m) => {
            m = m || {};
            const held = (m.skipped_on_hold || 0) + (m.skipped_ppcod || 0)
                + (m.skipped_multibox || 0) + (m.skipped_nostock || 0);
            return `<b>${m.dry_run ? "Would release" : "Released"} ${m.created || 0}</b> `
                + `· held ${held} (on-hold ${m.skipped_on_hold || 0} · PPCOD ${m.skipped_ppcod || 0} `
                + `· multibox ${m.skipped_multibox || 0} · no-stock ${m.skipped_nostock || 0}) `
                + `· bad-data ${m.skipped_bad_data || 0} · failed ${m.failed || 0}`;
        };

        // SAFE: preview what would release, no writes, no customer emails.
        frm.add_custom_button(__("Preview (dry-run)"), () => {
            frm.call("preview_release").then((r) => {
                frappe.msgprint({
                    title: __("Preview — nothing was released"),
                    message: fmt_release(r.message)
                        + "<br><br><i>" + __("This is only a preview — no Delivery Notes were "
                        + "created and no customer was notified. Use Run Release Now / Release "
                        + "for Date Range to actually ship.") + "</i>",
                    indicator: "green",
                });
            });
        });

        // LIVE: date-range background release (with confirm).
        frm.add_custom_button(__("Release Orders for Date Range"), () => {
            frappe.prompt(
                [{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", reqd: 1 },
                 { fieldname: "to_date", label: __("To Date"), fieldtype: "Date", reqd: 1 }],
                (v) => {
                    frappe.confirm(
                        __("This will RELEASE every order from {0} to {1} for real — "
                           + "create labels and email those customers &quot;shipped&quot;. "
                           + "It ignores the pause. Proceed?", [v.from_date, v.to_date]),
                        () => {
                            frm.call("run_release_range",
                                { from_date: v.from_date, to_date: v.to_date }).then((r) => {
                                frappe.msgprint({
                                    title: __("Range Release Queued"),
                                    message: __(
                                        "Releasing all orders from {0} to {1} in the background "
                                        + "(oldest first). A summary posts to #shopify-shipping "
                                        + "when done — then a wave / Prepare prints the pick list "
                                        + "+ labels.", [v.from_date, v.to_date]),
                                    indicator: "blue",
                                });
                            });
                        });
                },
                __("Release Orders for Date Range"),
                __("Next"),
            );
        });

        // LIVE: count-based pull (with confirm).
        frm.add_custom_button(__("Run Release Now"), () => {
            const n = frm.doc.max_orders_per_run || 50;
            frappe.confirm(
                __("This RELEASES up to {0} of the oldest orders for real — creates labels "
                   + "and emails those customers &quot;shipped&quot;, and ignores the pause. "
                   + "Proceed? (Use Preview to look without shipping.)", [n]),
                () => {
                    frm.call("run_release_now").then((r) => {
                        frappe.msgprint({
                            title: __("Release Result"),
                            message: fmt_release(r.message),
                            indicator: "blue",
                        });
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

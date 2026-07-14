// D2C Invoice Run — evening batch invoicing UI for the D2C fulfillment automation.
frappe.ui.form.on("D2C Invoice Run", {
    refresh(frm) {
        frm.add_custom_button(__("Load Pending Orders"), () => {
            const run = () =>
                frm.call({ doc: frm.doc, method: "load_pending", freeze: true,
                           freeze_message: __("Loading dispatched D2C orders…") })
                   .then(() => frm.reload_doc());
            // Ensure the doc is saved before calling a document method.
            (frm.is_new() || frm.is_dirty()) ? frm.save().then(run) : run();
        });

        const has_ticked = (frm.doc.orders || []).some(
            (r) => r.include && !r.sales_invoice);
        if (!frm.is_new() && has_ticked) {
            frm.add_custom_button(__("Create Invoices for Ticked"), () => {
                const n = (frm.doc.orders || []).filter(
                    (r) => r.include && !r.sales_invoice).length;
                frappe.confirm(
                    __("Create SHPSI27 invoices for {0} ticked order(s)? This raises real GST invoices (with IRN) and cannot be undone.", [n]),
                    () => {
                        const go = () =>
                            frm.call({ doc: frm.doc, method: "create_invoices", freeze: true,
                                       freeze_message: __("Creating invoices…") })
                               .then((r) => {
                                   frm.reload_doc();
                                   const m = r.message || {};
                                   frappe.show_alert({
                                       message: __("Created {0}, failed {1}, skipped {2}",
                                           [m.created || 0, m.failed || 0, m.skipped || 0]),
                                       indicator: m.failed ? "orange" : "green",
                                   });
                               });
                        frm.is_dirty() ? frm.save().then(go) : go();
                    }
                );
            }).addClass("btn-primary");
        }
    },
});

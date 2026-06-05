// Return Intake — warehouse returns desk form
frappe.ui.form.on("Return Intake", {
    setup(frm) {
        // Only allow returns against submitted, non-return Sales Invoices.
        frm.set_query("sales_invoice", () => ({
            filters: { docstatus: 1, is_return: 0 },
        }));
    },

    sales_invoice(frm) {
        // Best-effort channel guess from the invoice naming prefix.
        const name = frm.doc.sales_invoice || "";
        const map = [
            ["SHP", "Shopify"],
            ["BLN", "Blinkit"],
            ["ZEP", "Zepto"],
            ["REZ", "Amazon/Retailez"],
            ["AMZ", "Amazon/Retailez"],
            ["FLK", "Flipkart"],
        ];
        if (!frm.doc.channel) {
            for (const [prefix, channel] of map) {
                if (name.toUpperCase().startsWith(prefix)) {
                    frm.set_value("channel", channel);
                    break;
                }
            }
        }
    },
});

frappe.ui.form.on("Return Intake Video", {
    video(frm, cdt, cdn) {
        // Warn (don't block) on large uploads — Frappe Cloud storage is metered.
        const row = locals[cdt][cdn];
        const file = frm.attachments && frm.attachments.get_file
            ? null
            : null; // file size is not directly available post-upload
        if (row.video) {
            frappe.show_alert({
                message: __("Keep QC clips short. For large videos, use a Drive link instead of uploading."),
                indicator: "orange",
            }, 7);
        }
    },
});

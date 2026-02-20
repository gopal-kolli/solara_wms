# SOLARA WMS

Warehouse Management System (WMS) for ERPNext/Frappe.

## Features

- **Warehouse Bin** - Named bin locations (A-01-01) with zone, aisle, rack, level tracking
- **WMS Task** - Directed warehouse tasks (Putaway, Pick, Transfer, Count, Adjust)
- **WMS ASN** - Advanced Shipping Notice for inbound receiving with variance tracking
- **WMS Wave Pick** - Batch picking across multiple Sales Orders
- **WMS Dispatch** - Outbound shipping workflow with Delivery Note + Packing Slip creation
- **WMS Stock Freeze** - Freeze/unfreeze inventory for audits or quality holds
- **WMS Cycle Count** - Scheduled cycle counting with ABC classification and Stock Reconciliation
- **WMS Pack Station** - Scan-to-verify packing station with barcode support

## Installation

```bash
bench get-app https://github.com/gopal-kolli/solara_wms.git
bench --site your-site install-app solara_wms
bench --site your-site migrate
```

## License

MIT

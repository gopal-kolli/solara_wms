# SOLARA WMS

Warehouse Management System (WMS) for ERPNext/Frappe.

> **Safety status:** the future-state WMS is under a HYD-first TEST rollout.
> Installation defaults `WMS Settings` to **Disabled**. Warehouse-floor actions
> never submit ERPNext stock/accounting documents; controlled TEST handoffs may
> prepare drafts for independent review.

## Features

- **Warehouse Bin** - Named bin locations (A-01-01) with zone, aisle, rack, level tracking
- **WMS Item Location** - Warehouse-scoped Home/Reserve/Overflow SKU placement and replenishment policy
- **WMS Task** - Directed warehouse tasks (Putaway, Pick, Transfer, Count, Adjust)
- **WMS ASN** - Advanced Shipping Notice for inbound receiving with variance tracking
- **WMS Wave Pick** - Batch picking across multiple Sales Orders
- **WMS Dispatch** - Legacy draft-only outbound reference; the active Shopify D2C flow is separate
- **WMS Stock Freeze** - Freeze/unfreeze inventory for audits or quality holds
- **WMS Cycle Count** - Scheduled cycle counting; variances can prepare a draft Stock Reconciliation
- **D2C Pack Verify** - Active parcel verification, piece check and photo evidence

The target architecture, multi-warehouse model, 4x capacity constraints and
TEST gates are documented in [docs/future_sentry_architecture.md](docs/future_sentry_architecture.md).

## Installation

```bash
bench get-app https://github.com/gopal-kolli/solara_wms.git
bench --site your-site install-app solara_wms
bench --site your-site migrate
```

## License

MIT

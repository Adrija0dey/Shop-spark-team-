# Team Spark: Shop Stock Tracker

Hack Day Haldia x GRID community, 6-hour build challenge. Python / Flask / SQLite, single-page frontend.

Small shops track stock in notebooks. This keeps stock accurate, records sales and warns before items run out.

## Features

**Core**
- Product CRUD: name, SKU, category, cost, price, stock, reorder level
- Multi-line sales that reduce stock; a sale is rejected (and nothing is changed) if any line has insufficient stock
- Restock entries plus a per-product stock-movement log (opening, sale, restock, adjustment) with running balance
- Low-stock badge in the header and a Low stock list
- Sales report (daily / weekly): revenue, profit, orders, units, top 5 items, per-day chart
- Search, sort (click column headers) and filter (category, low stock only) on the product table
- Everything stored in SQLite

**Bonus**
- CSV import / export (import updates existing SKUs, reports bad rows)
- Moving-average demand forecast ("Reorder in N days")
- Printable invoice with invoice numbers (`INV-2026-00001`)
- Barcode scanning with the phone camera (Chrome, HTTPS)

## Run locally

```bash
pip install -r requirements.txt
python app.py        # http://127.0.0.1:5000
```

Click **Load demo data** on an empty Products page to see a week of sample sales.

## Deploy

Start command: `gunicorn app:app`. Step-by-step instructions are in [DEPLOY.md](DEPLOY.md).

Optional environment variables: `SHOP_NAME`, `TZ_OFFSET_MINUTES` (default 330, IST), `DB_PATH`.

## How it works

- **Stock safety:** a sale runs in one SQLite transaction. All lines are checked first (same product on two lines is combined), and only then are stock, sale and movement rows written. The database also has `CHECK (stock >= 0)` as a second guard.
- **Profit:** each sale line stores the price and cost at the time of sale, so reports stay correct if prices change or a product is deleted later.
- **Forecast:** average units sold per day over the last 14 days (counted from the product's first sale in that window). `Reorder in` = (stock - reorder level) / average, rounded down.
- **Timezone:** "today" and report days use the shop clock set by `TZ_OFFSET_MINUTES`, not the server's UTC clock.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Health check |
| GET / POST | `/api/products` | List (`q`, `category`, `low`, `sort`, `dir`) / create |
| GET / PUT / DELETE | `/api/products/<id>` | Read / update / delete |
| POST | `/api/products/<id>/restock` | `{qty, cost?, note?}` |
| GET | `/api/products/<id>/movements` | Stock-movement log |
| POST | `/api/sales` | `{items: [{product_id, qty}]}`; 409 if stock is short |
| GET | `/api/sales`, `/api/sales/<id>` | Sales history / one sale |
| GET | `/invoice/<id>` | Printable invoice |
| GET | `/api/low-stock` | Items at or below reorder level |
| GET | `/api/forecast?within=7` | Items expected to hit reorder level soon |
| GET | `/api/reports/sales?period=daily\|weekly&date=YYYY-MM-DD` | Sales report |
| GET / POST | `/api/products/export.csv`, `/api/products/import` | CSV export / import |
| POST | `/api/seed` | Demo data (only when the shop is empty) |

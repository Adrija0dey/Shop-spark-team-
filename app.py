"""Team Spark - small-shop stock tracker (Flask + SQLite).

Run locally:  python app.py   ->  http://127.0.0.1:5000
Deploy:       gunicorn app:app
"""
import csv
import io
import math
import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from html import escape

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "stock.db"))
TZ_MIN = int(os.environ.get("TZ_OFFSET_MINUTES", "330"))  # shop clock; 330 = India (IST)
SHOP_NAME = os.environ.get("SHOP_NAME", "Team Spark Store")
FORECAST_DAYS = 14  # moving-average window for the demand forecast

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB uploads (CSV import)
app.json.sort_keys = False


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


@app.errorhandler(ApiError)
def on_api_error(e):
    return jsonify(error=e.message, **e.extra), e.status


@app.errorhandler(HTTPException)
def on_http_error(e):
    if request.path.startswith("/api/"):
        return jsonify(error=e.description or e.name), e.code
    return e


@app.errorhandler(Exception)
def on_error(e):
    app.logger.exception(e)
    if request.path.startswith("/api/"):
        return jsonify(error="Server error"), 500
    return "Server error", 500


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  sku TEXT COLLATE NOCASE NOT NULL UNIQUE,
  category TEXT NOT NULL DEFAULT '',
  cost REAL NOT NULL DEFAULT 0 CHECK (cost >= 0),
  price REAL NOT NULL DEFAULT 0 CHECK (price >= 0),
  stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
  reorder_level INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_no TEXT UNIQUE,
  created_at TEXT NOT NULL,
  total REAL NOT NULL,
  profit REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sale_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
  product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
  sku TEXT NOT NULL,
  name TEXT NOT NULL,
  qty INTEGER NOT NULL CHECK (qty > 0),
  unit_price REAL NOT NULL,
  unit_cost REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  qty_change INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at);
CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_mov_product ON movements(product_id, id);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)  # autocommit; we use tx()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def tx():
    """One write transaction: commits on success, rolls back on any error."""
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def rows(sql, args=(), conn=None):
    c = conn or db()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        if conn is None:
            c.close()


def now():
    return (datetime.now(timezone.utc) + timedelta(minutes=TZ_MIN)).strftime("%Y-%m-%d %H:%M:%S")


def today():
    return date.fromisoformat(now()[:10])


init_db()


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Send a JSON object in the request body")
    return data


def number(data, key, integer=False):
    value = data.get(key)
    if value is None or value == "":
        return 0
    label = key.replace("_", " ")
    try:
        f = float(value)
        if not math.isfinite(f):
            raise ValueError
        if integer:
            if f != int(f):
                raise ValueError
            f = int(f)
        else:
            f = round(f, 2)
    except (TypeError, ValueError):
        raise ApiError(f"{label} must be {'a whole number' if integer else 'a number'}")
    if f < 0:
        raise ApiError(f"{label} cannot be negative")
    return f


def parse_product(data):
    name = str(data.get("name") or "").strip()
    sku = str(data.get("sku") or "").strip()
    if not name:
        raise ApiError("Name is required")
    if not sku:
        raise ApiError("SKU is required")
    return {
        "name": name[:120],
        "sku": sku[:60],
        "category": str(data.get("category") or "").strip()[:60],
        "cost": number(data, "cost"),
        "price": number(data, "price"),
        "stock": number(data, "stock", True),
        "reorder_level": number(data, "reorder_level", True),
    }


def get_product(conn, pid):
    r = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if r is None:
        raise ApiError("Product not found", 404)
    return dict(r)


def log_move(conn, pid, kind, change, balance, note="", when=None):
    conn.execute(
        "INSERT INTO movements (product_id, kind, qty_change, balance_after, note, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (pid, kind, change, balance, note, when or now()),
    )


# --------------------------------------------------------------------------
# Demand forecast (moving average) + low-stock flags
# --------------------------------------------------------------------------
def _avg_daily_sales(conn):
    """Average units/day per product over the last FORECAST_DAYS days.

    The window is the number of days since the product's first sale inside
    the period (so a new item isn't diluted by days before it existed).
    """
    t = today()
    start = t - timedelta(days=FORECAST_DAYS - 1)
    out = {}
    for r in conn.execute(
        """SELECT si.product_id AS pid, SUM(si.qty) AS qty, MIN(substr(s.created_at, 1, 10)) AS first
           FROM sale_items si JOIN sales s ON s.id = si.sale_id
           WHERE si.product_id IS NOT NULL AND s.created_at >= ?
           GROUP BY si.product_id""",
        (start.isoformat() + " 00:00:00",),
    ):
        window = max(1, (t - date.fromisoformat(r["first"])).days + 1)
        out[r["pid"]] = r["qty"] / window
    return out


def decorate(products, conn):
    avg = _avg_daily_sales(conn)
    for p in products:
        a = avg.get(p["id"], 0)
        p["low"] = p["stock"] <= p["reorder_level"]
        p["avg_daily"] = round(a, 2)
        p["days_left"] = math.floor(p["stock"] / a) if a else None
        p["reorder_in_days"] = max(0, math.floor((p["stock"] - p["reorder_level"]) / a)) if a else None
    return products


# --------------------------------------------------------------------------
# Pages + health
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/api/health")
def health():
    return jsonify(status="ok", time=now())


# --------------------------------------------------------------------------
# Products (CRUD, search / sort / filter)
# --------------------------------------------------------------------------
SORTS = {
    "name": "name COLLATE NOCASE",
    "sku": "sku COLLATE NOCASE",
    "category": "category COLLATE NOCASE",
    "cost": "cost",
    "price": "price",
    "stock": "stock",
    "reorder_level": "reorder_level",
    "margin": "(price - cost)",
}


@app.get("/api/products")
def list_products():
    where, args = [], []
    q = request.args.get("q", "").strip()
    if q:
        like = f"%{q}%"
        where.append("(name LIKE ? OR sku LIKE ? OR category LIKE ?)")
        args += [like, like, like]
    category = request.args.get("category", "").strip()
    if category:
        where.append("category = ? COLLATE NOCASE")
        args.append(category)
    if request.args.get("low") in ("1", "true"):
        where.append("stock <= reorder_level")
    order = SORTS.get(request.args.get("sort", "name"), SORTS["name"])
    direction = "DESC" if request.args.get("dir") == "desc" else "ASC"
    sql = "SELECT * FROM products"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} {direction}, id"
    conn = db()
    try:
        return jsonify(decorate(rows(sql, args, conn), conn))
    finally:
        conn.close()


@app.get("/api/categories")
def categories():
    return jsonify([r["category"] for r in rows(
        "SELECT DISTINCT category FROM products WHERE category != '' ORDER BY category COLLATE NOCASE")])


@app.get("/api/products/<int:pid>")
def read_product(pid):
    conn = db()
    try:
        return jsonify(decorate([get_product(conn, pid)], conn)[0])
    finally:
        conn.close()


@app.post("/api/products")
def create_product():
    data = parse_product(body())
    with tx() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO products (name, sku, category, cost, price, stock, reorder_level, created_at) "
                "VALUES (:name, :sku, :category, :cost, :price, :stock, :reorder_level, :created)",
                {**data, "created": now()},
            )
        except sqlite3.IntegrityError:
            raise ApiError(f"SKU '{data['sku']}' already exists", 409)
        pid = cur.lastrowid
        if data["stock"]:
            log_move(conn, pid, "opening", data["stock"], data["stock"], "Opening stock")
        product = decorate([get_product(conn, pid)], conn)[0]
    return jsonify(product), 201


@app.put("/api/products/<int:pid>")
def update_product(pid):
    payload = body()
    with tx() as conn:
        old = get_product(conn, pid)
        data = parse_product({**old, **payload})
        try:
            conn.execute(
                "UPDATE products SET name=:name, sku=:sku, category=:category, cost=:cost, price=:price, "
                "stock=:stock, reorder_level=:reorder_level WHERE id=:id",
                {**data, "id": pid},
            )
        except sqlite3.IntegrityError:
            raise ApiError(f"SKU '{data['sku']}' already exists", 409)
        if data["stock"] != old["stock"]:
            log_move(conn, pid, "adjustment", data["stock"] - old["stock"], data["stock"], "Manual stock edit")
        product = decorate([get_product(conn, pid)], conn)[0]
    return jsonify(product)


@app.delete("/api/products/<int:pid>")
def delete_product(pid):
    with tx() as conn:
        get_product(conn, pid)
        conn.execute("DELETE FROM products WHERE id = ?", (pid,))  # past sales keep their own name/SKU copy
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# Restock + stock-movement log
# --------------------------------------------------------------------------
@app.post("/api/products/<int:pid>/restock")
def restock(pid):
    d = body()
    qty = number(d, "qty", True)
    if qty < 1:
        raise ApiError("Quantity must be at least 1")
    note = str(d.get("note") or "").strip()[:200]
    with tx() as conn:
        p = get_product(conn, pid)
        new_stock = p["stock"] + qty
        cost = number(d, "cost") if d.get("cost") not in (None, "") else p["cost"]
        conn.execute("UPDATE products SET stock = ?, cost = ? WHERE id = ?", (new_stock, cost, pid))
        log_move(conn, pid, "restock", qty, new_stock, note)
        product = decorate([get_product(conn, pid)], conn)[0]
    return jsonify(product)


@app.get("/api/products/<int:pid>/movements")
def movements(pid):
    conn = db()
    try:
        get_product(conn, pid)
        return jsonify(rows(
            "SELECT * FROM movements WHERE product_id = ? ORDER BY id DESC LIMIT 500", (pid,), conn))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Sales
# --------------------------------------------------------------------------
def record_sale(conn, wanted, when=None):
    """wanted = {product_id: qty}. Checks ALL stock first, then writes. Raises 409 if short."""
    when = when or now()
    lines, short = [], []
    for pid, qty in wanted.items():
        p = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        if p is None:
            raise ApiError(f"Product {pid} not found", 404)
        if p["stock"] < qty:
            short.append({"product_id": pid, "name": p["name"], "sku": p["sku"],
                          "requested": qty, "available": p["stock"]})
        lines.append((p, qty))
    if short:
        names = ", ".join(f"{s['name']} (have {s['available']}, need {s['requested']})" for s in short)
        raise ApiError("Not enough stock: " + names, 409, shortages=short)

    total = round(sum(p["price"] * q for p, q in lines), 2)
    profit = round(sum((p["price"] - p["cost"]) * q for p, q in lines), 2)
    cur = conn.execute("INSERT INTO sales (created_at, total, profit) VALUES (?,?,?)", (when, total, profit))
    sid = cur.lastrowid
    invoice = f"INV-{when[:4]}-{sid:05d}"
    conn.execute("UPDATE sales SET invoice_no = ? WHERE id = ?", (invoice, sid))
    for p, q in lines:
        conn.execute(
            "INSERT INTO sale_items (sale_id, product_id, sku, name, qty, unit_price, unit_cost) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, p["id"], p["sku"], p["name"], q, p["price"], p["cost"]),
        )
        new_stock = p["stock"] - q
        conn.execute("UPDATE products SET stock = ? WHERE id = ?", (new_stock, p["id"]))
        log_move(conn, p["id"], "sale", -q, new_stock, invoice, when)
    return sid


def sale_detail(conn, sid):
    s = conn.execute("SELECT * FROM sales WHERE id = ?", (sid,)).fetchone()
    if s is None:
        raise ApiError("Sale not found", 404)
    sale = dict(s)
    sale["items"] = rows(
        "SELECT sku, name, qty, unit_price, ROUND(qty * unit_price, 2) AS line_total "
        "FROM sale_items WHERE sale_id = ? ORDER BY id", (sid,), conn)
    return sale


@app.post("/api/sales")
def create_sale():
    items = body().get("items")
    if not isinstance(items, list) or not items:
        raise ApiError("Add at least one item to the sale")
    wanted = {}
    for it in items:
        if not isinstance(it, dict):
            raise ApiError("Invalid item in sale")
        try:
            pid = int(it.get("product_id"))
        except (TypeError, ValueError):
            raise ApiError("Each item needs a product_id")
        qty = number(it, "qty", True)
        if qty < 1:
            raise ApiError("Quantity must be at least 1")
        wanted[pid] = wanted.get(pid, 0) + qty  # same product on two lines = one combined check
    with tx() as conn:
        sid = record_sale(conn, wanted)
        sale = sale_detail(conn, sid)
    return jsonify(sale), 201


@app.get("/api/sales")
def list_sales():
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except ValueError:
        limit = 100
    return jsonify(rows(
        "SELECT s.*, (SELECT COALESCE(SUM(qty), 0) FROM sale_items WHERE sale_id = s.id) AS units "
        "FROM sales s ORDER BY s.id DESC LIMIT ?", (limit,)))


@app.get("/api/sales/<int:sid>")
def read_sale(sid):
    conn = db()
    try:
        return jsonify(sale_detail(conn, sid))
    finally:
        conn.close()


INVOICE_CSS = """
body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#111;margin:0;background:#f3f4f6}
.sheet{max-width:720px;margin:24px auto;background:#fff;padding:36px;border:1px solid #ddd}
h1{margin:0;font-size:22px} .muted{color:#666}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:24px}
table{width:100%;border-collapse:collapse} th,td{padding:8px 6px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#555} .r{text-align:right}
.total td{border-bottom:0;font-weight:700;font-size:16px;padding-top:14px}
button{margin:0 auto 24px;display:block;padding:8px 18px;font:inherit;cursor:pointer}
@media print{body{background:#fff}.sheet{border:0;margin:0;padding:0}button{display:none}}
"""


@app.get("/invoice/<int:sid>")
def invoice(sid):
    conn = db()
    try:
        sale = sale_detail(conn, sid)
    finally:
        conn.close()
    lines = "".join(
        f"<tr><td>{i}</td><td>{escape(it['name'])}<br><span class='muted'>{escape(it['sku'])}</span></td>"
        f"<td class='r'>{it['qty']}</td><td class='r'>{it['unit_price']:.2f}</td>"
        f"<td class='r'>{it['line_total']:.2f}</td></tr>"
        for i, it in enumerate(sale["items"], 1)
    )
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Invoice {escape(sale['invoice_no'])}</title><style>{INVOICE_CSS}</style></head><body>"
        "<div class='sheet'>"
        f"<div class='head'><div><h1>{escape(SHOP_NAME)}</h1><div class='muted'>Tax invoice / cash memo</div></div>"
        f"<div style='text-align:right'><strong>{escape(sale['invoice_no'])}</strong><br>"
        f"<span class='muted'>{escape(sale['created_at'])}</span></div></div>"
        "<table><thead><tr><th>#</th><th>Item</th><th class='r'>Qty</th>"
        "<th class='r'>Price (&#8377;)</th><th class='r'>Amount (&#8377;)</th></tr></thead>"
        f"<tbody>{lines}"
        f"<tr class='total'><td colspan='4' class='r'>Total</td><td class='r'>&#8377;{sale['total']:.2f}</td></tr>"
        "</tbody></table>"
        "<p class='muted' style='margin-top:28px'>Thank you for shopping with us.</p></div>"
        "<button onclick='window.print()'>Print invoice</button></body></html>"
    )
    return Response(page, mimetype="text/html")


# --------------------------------------------------------------------------
# Reports + alerts
# --------------------------------------------------------------------------
def parse_day(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise ApiError("date must look like YYYY-MM-DD")


@app.get("/api/reports/sales")
def sales_report():
    period = request.args.get("period", "daily")
    if period not in ("daily", "weekly"):
        raise ApiError("period must be 'daily' or 'weekly'")
    end = parse_day(request.args["date"]) if request.args.get("date") else today()
    start = end if period == "daily" else end - timedelta(days=6)  # weekly = 7 days ending on `end`
    lo = start.isoformat() + " 00:00:00"
    hi = (end + timedelta(days=1)).isoformat() + " 00:00:00"
    conn = db()
    try:
        s = conn.execute(
            "SELECT COUNT(*) AS orders, COALESCE(SUM(total), 0) AS revenue, COALESCE(SUM(profit), 0) AS profit "
            "FROM sales WHERE created_at >= ? AND created_at < ?", (lo, hi)).fetchone()
        units = conn.execute(
            "SELECT COALESCE(SUM(si.qty), 0) FROM sale_items si JOIN sales s ON s.id = si.sale_id "
            "WHERE s.created_at >= ? AND s.created_at < ?", (lo, hi)).fetchone()[0]
        top = rows(
            """SELECT si.sku, MAX(si.name) AS name, SUM(si.qty) AS qty,
                      ROUND(SUM(si.qty * si.unit_price), 2) AS revenue,
                      ROUND(SUM(si.qty * (si.unit_price - si.unit_cost)), 2) AS profit
               FROM sale_items si JOIN sales s ON s.id = si.sale_id
               WHERE s.created_at >= ? AND s.created_at < ?
               GROUP BY si.sku ORDER BY qty DESC, revenue DESC LIMIT 5""", (lo, hi), conn)
        per_day = {r["day"]: r for r in rows(
            "SELECT substr(created_at, 1, 10) AS day, ROUND(SUM(total), 2) AS revenue, "
            "ROUND(SUM(profit), 2) AS profit, COUNT(*) AS orders FROM sales "
            "WHERE created_at >= ? AND created_at < ? GROUP BY day", (lo, hi), conn)}
    finally:
        conn.close()
    by_day, d = [], start
    while d <= end:
        r = per_day.get(d.isoformat(), {})
        by_day.append({"day": d.isoformat(), "revenue": r.get("revenue", 0),
                       "profit": r.get("profit", 0), "orders": r.get("orders", 0)})
        d += timedelta(days=1)
    return jsonify({
        "period": period, "from": start.isoformat(), "to": end.isoformat(),
        "orders": s["orders"], "units": units,
        "revenue": round(s["revenue"], 2), "profit": round(s["profit"], 2),
        "top_items": top, "by_day": by_day,
    })


@app.get("/api/low-stock")
def low_stock():
    conn = db()
    try:
        items = decorate(rows(
            "SELECT * FROM products WHERE stock <= reorder_level "
            "ORDER BY (stock - reorder_level), name COLLATE NOCASE", (), conn), conn)
    finally:
        conn.close()
    return jsonify(count=len(items), items=items)


@app.get("/api/forecast")
def forecast():
    """Products expected to hit their reorder level within `within` days (default 7)."""
    try:
        within = max(0, int(request.args.get("within", 7)))
    except ValueError:
        within = 7
    conn = db()
    try:
        items = decorate(rows("SELECT * FROM products", (), conn), conn)
    finally:
        conn.close()
    items = [p for p in items if p["reorder_in_days"] is not None and p["reorder_in_days"] <= within]
    items.sort(key=lambda p: (p["reorder_in_days"], p["name"].lower()))
    return jsonify(within_days=within, items=items)


# --------------------------------------------------------------------------
# CSV import / export
# --------------------------------------------------------------------------
CSV_COLS = ["name", "sku", "category", "cost", "price", "stock", "reorder_level"]


def _csv_safe(v):
    # stop spreadsheet apps treating a cell as a formula
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _csv_unsafe(v):
    v = (v or "").strip()
    if len(v) > 1 and v[0] == "'" and v[1] in "=+-@\t\r":
        return v[1:]
    return v


@app.get("/api/products/export.csv")
def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLS)
    for p in rows("SELECT * FROM products ORDER BY name COLLATE NOCASE"):
        w.writerow([_csv_safe(p[c]) for c in CSV_COLS])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=products.csv"})


@app.post("/api/products/import")
def import_csv():
    raw = request.files["file"].read() if "file" in request.files else request.get_data()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    if not reader.fieldnames:
        raise ApiError("The CSV file is empty")
    reader.fieldnames = [(h or "").strip().lower().replace(" ", "_") for h in reader.fieldnames]
    if "name" not in reader.fieldnames or "sku" not in reader.fieldnames:
        raise ApiError("CSV needs 'name' and 'sku' columns (optional: category, cost, price, stock, reorder_level)")
    created = updated = 0
    errors = []
    with tx() as conn:
        for i, row in enumerate(reader, start=2):  # row 1 is the header
            if i > 5001:
                errors.append({"row": i, "error": "Stopped: more than 5000 rows"})
                break
            clean = {k: _csv_unsafe(v) for k, v in row.items() if k}
            if not any(clean.values()):
                continue
            try:
                data = parse_product(clean)
                old = conn.execute("SELECT * FROM products WHERE sku = ?", (data["sku"],)).fetchone()
                if old:
                    # columns missing/blank in the file keep their current value
                    given = {k: v for k, v in clean.items() if k in CSV_COLS and v != ""}
                    data = parse_product({**dict(old), **given})
                    conn.execute(
                        "UPDATE products SET name=:name, category=:category, cost=:cost, price=:price, "
                        "stock=:stock, reorder_level=:reorder_level WHERE id=:id", {**data, "id": old["id"]})
                    if data["stock"] != old["stock"]:
                        log_move(conn, old["id"], "adjustment", data["stock"] - old["stock"],
                                 data["stock"], "CSV import")
                    updated += 1
                else:
                    cur = conn.execute(
                        "INSERT INTO products (name, sku, category, cost, price, stock, reorder_level, created_at) "
                        "VALUES (:name, :sku, :category, :cost, :price, :stock, :reorder_level, :created)",
                        {**data, "created": now()})
                    if data["stock"]:
                        log_move(conn, cur.lastrowid, "opening", data["stock"], data["stock"], "CSV import")
                    created += 1
            except ApiError as e:
                errors.append({"row": i, "error": e.message})
    return jsonify(created=created, updated=updated, errors=errors)


# --------------------------------------------------------------------------
# Demo data (empty shop only) - handy because free hosts can wipe the DB
# --------------------------------------------------------------------------
DEMO_PRODUCTS = [
    # name, sku, category, cost, price, stock, reorder level
    ("Basmati Rice 1kg", "RICE-1KG", "Grocery", 78, 95, 60, 20),
    ("Sugar 1kg", "SUGAR-1KG", "Grocery", 40, 46, 45, 15),
    ("Toor Dal 1kg", "DAL-1KG", "Grocery", 120, 142, 30, 12),
    ("Mustard Oil 1L", "OIL-1L", "Grocery", 135, 158, 22, 10),
    ("Tea Powder 250g", "TEA-250", "Grocery", 85, 105, 25, 10),
    ("Glucose Biscuits", "BISC-01", "Snacks", 8, 10, 120, 40),
    ("Potato Chips 50g", "CHIPS-50", "Snacks", 14, 20, 70, 25),
    ("Notebook A5", "NOTE-A5", "Stationery", 22, 35, 40, 15),
    ("Ball Pen (Blue)", "PEN-BLU", "Stationery", 4, 10, 100, 30),
    ("Hand Soap", "SOAP-01", "Household", 28, 38, 35, 12),
    ("Detergent Powder 1kg", "DET-1KG", "Household", 82, 102, 20, 8),
    ("LED Bulb 9W", "LED-9W", "Electrical", 55, 85, 6, 8),
]


@app.post("/api/seed")
def seed_demo():
    rng = random.Random(7)
    with tx() as conn:
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
            raise ApiError("Demo data can only be loaded into an empty shop", 409)
        opened = (today() - timedelta(days=7)).isoformat() + " 08:00:00"
        ids = []
        for name, sku, cat, cost, price, stock, reorder in DEMO_PRODUCTS:
            cur = conn.execute(
                "INSERT INTO products (name, sku, category, cost, price, stock, reorder_level, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (name, sku, cat, cost, price, stock, reorder, opened))
            ids.append(cur.lastrowid)
            log_move(conn, cur.lastrowid, "opening", stock, stock, "Opening stock", opened)
        for offset in range(6, -1, -1):
            day = today() - timedelta(days=offset)
            for _ in range(rng.randint(2, 4)):
                wanted = {pid: rng.randint(1, 4) for pid in rng.sample(ids, rng.randint(1, 3))}
                when = now() if offset == 0 else \
                    f"{day.isoformat()} {rng.randint(9, 20):02d}:{rng.randint(0, 59):02d}:00"
                try:
                    record_sale(conn, wanted, when)
                except ApiError:
                    pass  # not enough stock for this random sale - skip it
    return jsonify(ok=True, products=len(DEMO_PRODUCTS)), 201


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")

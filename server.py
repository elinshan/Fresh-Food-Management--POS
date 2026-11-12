#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
丰食生鲜 · 蔬菜店铺销售管理系统 —— 本地服务端
================================================
只使用 Python 3 标准库（http.server / sqlite3 / json ...），不需要 pip 安装任何包。

数据保存在 data/fengshi.db（SQLite 数据库文件），
每次改动都会立即写入磁盘，关闭窗口 / 重启电脑数据都不会丢失。

启动方式：
    python server.py          （或直接双击「启动系统.bat」）
"""

import hashlib
import json
import mimetypes
import os
import random
import secrets
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

# 打包成 exe 运行时：web 资源在 PyInstaller 的临时解包目录里，
# 而用户数据（数据库）必须放在 exe 所在目录，关闭程序 / 重启电脑后才不会丢。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    WEB_DIR = os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "web")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    WEB_DIR = os.path.join(BASE_DIR, "web")

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "fengshi.db")

HOST = "127.0.0.1"
PORTS = [8765, 8766, 8767, 8768, 8769, 8770]

DEFAULT_SETTINGS = {
    "shopName": "鲜蔬源蔬菜店",
    "phone": "400-888-8888",
    "address": "幸福路 66 号蔬菜市场 A12 档",
    "orderPrefix": "XS",
    "receiptNote": "感谢惠顾，欢迎再次光临！",
    "printNote": "1",
    "printPhone": "1",
    "printAddress": "1",
    "logo": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    salt          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    note  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS products (
    id          TEXT PRIMARY KEY,
    category_id TEXT NOT NULL,
    name        TEXT NOT NULL,
    unit        TEXT DEFAULT '斤',
    price       REAL DEFAULT 0,
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_products_cat ON products(category_id);

CREATE TABLE IF NOT EXISTS suppliers (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    contact TEXT DEFAULT '',
    phone   TEXT DEFAULT '',
    address TEXT DEFAULT '',
    note    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS customers (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    contact TEXT DEFAULT '',
    phone   TEXT DEFAULT '',
    address TEXT DEFAULT '',
    type    TEXT DEFAULT '客户',
    note    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS prices (
    customer_id TEXT NOT NULL,
    product_id  TEXT NOT NULL,
    price       REAL NOT NULL,
    PRIMARY KEY (customer_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    no            TEXT,
    date          TEXT,
    customer_id   TEXT,
    customer_name TEXT,
    subtotal      REAL DEFAULT 0,
    discount      REAL DEFAULT 0,
    total         REAL DEFAULT 0,
    remark        TEXT DEFAULT '',
    created_at    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(date);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);

CREATE TABLE IF NOT EXISTS order_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    seq        INTEGER DEFAULT 0,
    product_id TEXT,
    name       TEXT,
    unit       TEXT,
    price      REAL DEFAULT 0,
    qty        REAL DEFAULT 0,
    amount     REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
"""

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


class ApiError(Exception):
    """业务异常：直接转成 {"error": message} 返回给前端"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
#  基础工具
# --------------------------------------------------------------------------- #

def new_id(prefix):
    return prefix + secrets.token_hex(4)


def hash_pw(password, salt):
    return hashlib.sha256((salt + "|" + password).encode("utf-8")).hexdigest()


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def txt(value, limit=200):
    if value is None:
        return ""
    return str(value).strip()[:limit]


def num(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def money(value):
    return round(num(value), 2)


# --------------------------------------------------------------------------- #
#  建库与初始数据
# --------------------------------------------------------------------------- #

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.executescript(SCHEMA)
        conn.commit()

        if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
            salt = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO users (username, salt, password_hash) VALUES (?,?,?)",
                ("admin", salt, hash_pw("123456", salt)),
            )
            conn.commit()

        if conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"] == 0:
            seed_demo(conn)
            conn.commit()
    finally:
        conn.close()


def seed_demo(conn):
    """写入一套与原始页面一致的演示数据（类目、商品、商户、一户一价、近 60 天订单）"""
    for table in ("order_items", "orders", "prices", "products",
                  "categories", "suppliers", "customers", "settings"):
        conn.execute("DELETE FROM %s" % table)

    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?)", (key, str(value)))

    cat_defs = [
        ("叶菜类", ["大白菜", "小白菜", "菠菜", "生菜", "芹菜", "韭菜"]),
        ("根茎类", ["土豆", "胡萝卜", "洋葱", "白萝卜", "山药"]),
        ("茄果类", ["西红柿", "黄瓜", "青椒", "茄子", "丝瓜", "南瓜", "冬瓜"]),
        ("豆菌类", ["豆角", "香菇", "金针菇", "平菇"]),
    ]
    price_map = {
        "大白菜": 1.5, "小白菜": 2, "菠菜": 3.5, "生菜": 3, "芹菜": 2.6, "韭菜": 3,
        "土豆": 2, "胡萝卜": 2.2, "洋葱": 1.8, "白萝卜": 1.2, "山药": 6.5,
        "西红柿": 3.8, "黄瓜": 2.8, "青椒": 4, "茄子": 3.2, "丝瓜": 3.4,
        "南瓜": 2.4, "冬瓜": 1.6,
        "豆角": 5, "香菇": 7, "金针菇": 5.5, "平菇": 4.5,
    }

    products = {}
    for cat_name, items in cat_defs:
        cat_id = new_id("cg")
        conn.execute("INSERT INTO categories (id, name, note) VALUES (?,?,?)",
                     (cat_id, cat_name, ""))
        for product_name in items:
            pid = new_id("p")
            unit = "袋" if product_name == "金针菇" else "斤"
            price = float(price_map.get(product_name, 2))
            conn.execute(
                "INSERT INTO products (id, category_id, name, unit, price, note) VALUES (?,?,?,?,?,?)",
                (pid, cat_id, product_name, unit, price, ""),
            )
            products[product_name] = {"id": pid, "unit": unit, "price": price}

    suppliers = [
        ("城东农产品批发市场", "王建国", "13900001111", "城东区兴农路88号", "每日凌晨4点配送"),
        ("绿源蔬菜种植基地", "李秀兰", "13800002222", "郊区绿源村", "有机蔬菜直供"),
        ("本地农户合作社", "赵大山", "13700003333", "城南镇合兴村", "时令蔬菜"),
    ]
    for name, contact, phone, address, note in suppliers:
        conn.execute(
            "INSERT INTO suppliers (id, name, contact, phone, address, note) VALUES (?,?,?,?,?,?)",
            (new_id("s"), name, contact, phone, address, note),
        )

    customers = [
        ("阳光小区食堂", "刘经理", "13600004444", "阳光小区3号楼", "食堂", ""),
        ("老王家常菜", "王老板", "13500005555", "建设路12号", "餐饮", "每日配送"),
        ("张记牛肉面", "张老板", "13400006666", "人民路45号", "餐饮", ""),
        ("川味小厨", "陈师傅", "13300007777", "解放路8号", "餐饮", "周末休息"),
        ("零售顾客", "—", "—", "—", "零售", "散客"),
    ]
    customer_rows = []
    for name, contact, phone, address, ctype, note in customers:
        cid = new_id("m")
        conn.execute(
            "INSERT INTO customers (id, name, contact, phone, address, type, note) VALUES (?,?,?,?,?,?,?)",
            (cid, name, contact, phone, address, ctype, note),
        )
        customer_rows.append({"id": cid, "name": name})

    custom_prices = [
        (0, "大白菜", 1.2), (0, "土豆", 1.7), (0, "西红柿", 3.4), (0, "黄瓜", 2.5),
        (1, "西红柿", 3.6), (1, "豆角", 4.6), (1, "香菇", 6.5),
        (3, "青椒", 3.7),
    ]
    for index, product_name, price in custom_prices:
        conn.execute(
            "INSERT OR REPLACE INTO prices (customer_id, product_id, price) VALUES (?,?,?)",
            (customer_rows[index]["id"], products[product_name]["id"], price),
        )

    # 近 60 天演示订单
    today = datetime.now().date()
    prefix = DEFAULT_SETTINGS["orderPrefix"]
    for day_offset in range(59, -1, -1):
        day = today - timedelta(days=day_offset)
        date_str = day.strftime("%Y-%m-%d")
        order_count = random.randint(1, 4)
        for seq in range(order_count):
            index = random.randrange(len(customer_rows))
            customer = customer_rows[index]
            item_count = random.randint(1, 4)
            chosen = random.sample(list(products.keys()), min(item_count, len(products)))
            items = []
            subtotal = 0.0
            for product_name in chosen:
                product = products[product_name]
                items.append({
                    "productId": product["id"],
                    "name": product_name,
                    "unit": product["unit"],
                    "price": product["price"],
                    "qty": round(1 + random.random() * 18, 1),
                })
            for item in items:
                custom = conn.execute(
                    "SELECT price FROM prices WHERE customer_id=? AND product_id=?",
                    (customer["id"], item["productId"]),
                ).fetchone()
                if custom:
                    item["price"] = float(custom["price"])
                item["amount"] = round(item["price"] * item["qty"], 2)
                subtotal += item["amount"]
            subtotal = round(subtotal, 2)
            discount = round(subtotal * 0.05, 2) if random.random() < 0.18 else 0.0
            total = round(subtotal - discount, 2)
            order_no = "%s%s%03d" % (prefix, date_str.replace("-", ""), seq + 1)
            created_at = int(
                (datetime.combine(day, datetime.min.time()) + timedelta(minutes=seq * 3)).timestamp() * 1000
            )
            order_id = new_id("o")
            conn.execute(
                """INSERT INTO orders
                   (id, no, date, customer_id, customer_name, subtotal, discount, total, remark, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (order_id, order_no, date_str, customer["id"], customer["name"],
                 subtotal, discount, total, "", created_at),
            )
            for sequence, item in enumerate(items):
                conn.execute(
                    """INSERT INTO order_items
                       (order_id, seq, product_id, name, unit, price, qty, amount)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (order_id, sequence, item["productId"], item["name"], item["unit"],
                     item["price"], item["qty"], item["amount"]),
                )


# --------------------------------------------------------------------------- #
#  读取 / 保存整份业务数据
# --------------------------------------------------------------------------- #

def load_state(username):
    conn = connect()
    try:
        categories = [
            {"id": r["id"], "name": r["name"], "note": r["note"] or ""}
            for r in conn.execute("SELECT * FROM categories ORDER BY rowid")
        ]
        products = [
            {"id": r["id"], "categoryId": r["category_id"], "name": r["name"],
             "unit": r["unit"] or "斤", "price": float(r["price"] or 0), "note": r["note"] or ""}
            for r in conn.execute("SELECT * FROM products ORDER BY rowid")
        ]
        suppliers = [
            {"id": r["id"], "name": r["name"], "contact": r["contact"] or "",
             "phone": r["phone"] or "", "address": r["address"] or "", "note": r["note"] or ""}
            for r in conn.execute("SELECT * FROM suppliers ORDER BY rowid")
        ]
        customers = [
            {"id": r["id"], "name": r["name"], "contact": r["contact"] or "",
             "phone": r["phone"] or "", "address": r["address"] or "",
             "type": r["type"] or "客户", "note": r["note"] or ""}
            for r in conn.execute("SELECT * FROM customers ORDER BY rowid")
        ]
        prices = [
            {"customerId": r["customer_id"], "productId": r["product_id"], "price": float(r["price"] or 0)}
            for r in conn.execute("SELECT * FROM prices")
        ]

        items_by_order = {}
        for r in conn.execute("SELECT * FROM order_items ORDER BY order_id, seq"):
            items_by_order.setdefault(r["order_id"], []).append({
                "productId": r["product_id"] or "",
                "name": r["name"] or "",
                "unit": r["unit"] or "斤",
                "price": float(r["price"] or 0),
                "qty": float(r["qty"] or 0),
                "amount": float(r["amount"] or 0),
            })

        orders = []
        for r in conn.execute("SELECT * FROM orders ORDER BY created_at, no"):
            orders.append({
                "id": r["id"],
                "no": r["no"] or "",
                "date": r["date"] or "",
                "customerId": r["customer_id"] or "",
                "customerName": r["customer_name"] or "",
                "items": items_by_order.get(r["id"], []),
                "subtotal": float(r["subtotal"] or 0),
                "discount": float(r["discount"] or 0),
                "total": float(r["total"] or 0),
                "remark": r["remark"] or "",
                "createdAt": int(r["created_at"] or 0),
            })

        settings = dict(DEFAULT_SETTINGS)
        for r in conn.execute("SELECT key, value FROM settings"):
            settings[r["key"]] = r["value"]

        return {
            "auth": {"username": username},
            "categories": categories,
            "products": products,
            "suppliers": suppliers,
            "customers": customers,
            "prices": prices,
            "orders": orders,
            "settings": settings,
        }
    finally:
        conn.close()


def save_state(data):
    """把前端提交的整份业务数据写进 SQLite（单事务，原子替换）"""
    if not isinstance(data, dict):
        raise ApiError("数据格式不正确")
    for key in ("categories", "products", "suppliers", "customers", "prices", "orders", "settings"):
        if key not in data:
            raise ApiError("缺少字段：%s" % key)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("order_items", "orders", "prices", "products",
                      "categories", "suppliers", "customers", "settings"):
            conn.execute("DELETE FROM %s" % table)

        for key in DEFAULT_SETTINGS:
            value = (data.get("settings") or {}).get(key, DEFAULT_SETTINGS[key])
            # LOGO 存的是图片 dataURL，需要放宽长度；其余设置保持短文本
            limit = 400000 if key == "logo" else 120
            conn.execute("INSERT INTO settings (key, value) VALUES (?,?)",
                         (key, txt(value, limit) or DEFAULT_SETTINGS[key]))

        cat_ids = set()
        for row in data.get("categories") or []:
            cid = txt(row.get("id"), 64) or new_id("cg")
            if cid in cat_ids:
                continue
            cat_ids.add(cid)
            conn.execute("INSERT INTO categories (id, name, note) VALUES (?,?,?)",
                         (cid, txt(row.get("name"), 60) or "未命名类目", txt(row.get("note"), 200)))

        product_ids = set()
        for row in data.get("products") or []:
            pid = txt(row.get("id"), 64) or new_id("p")
            if pid in product_ids:
                continue
            category_id = txt(row.get("categoryId"), 64)
            if category_id not in cat_ids:
                continue
            product_ids.add(pid)
            conn.execute(
                "INSERT INTO products (id, category_id, name, unit, price, note) VALUES (?,?,?,?,?,?)",
                (pid, category_id, txt(row.get("name"), 60) or "未命名商品",
                 txt(row.get("unit"), 16) or "斤",
                 round(max(0.0, num(row.get("price"))), 2), txt(row.get("note"), 200)),
            )

        for row in data.get("suppliers") or []:
            conn.execute(
                "INSERT INTO suppliers (id, name, contact, phone, address, note) VALUES (?,?,?,?,?,?)",
                (txt(row.get("id"), 64) or new_id("s"), txt(row.get("name"), 60) or "未命名供应商",
                 txt(row.get("contact"), 40), txt(row.get("phone"), 40),
                 txt(row.get("address"), 120), txt(row.get("note"), 200)),
            )

        for row in data.get("customers") or []:
            conn.execute(
                "INSERT INTO customers (id, name, contact, phone, address, type, note) VALUES (?,?,?,?,?,?,?)",
                (txt(row.get("id"), 64) or new_id("m"), txt(row.get("name"), 60) or "未命名客户",
                 txt(row.get("contact"), 40), txt(row.get("phone"), 40),
                 txt(row.get("address"), 120), txt(row.get("type"), 16) or "客户",
                 txt(row.get("note"), 200)),
            )

        for row in data.get("prices") or []:
            cid = txt(row.get("customerId"), 64)
            pid = txt(row.get("productId"), 64)
            if not cid or pid not in product_ids:
                continue
            conn.execute("INSERT OR REPLACE INTO prices (customer_id, product_id, price) VALUES (?,?,?)",
                         (cid, pid, round(max(0.0, num(row.get("price"))), 2)))

        order_ids = set()
        for row in data.get("orders") or []:
            oid = txt(row.get("id"), 64) or new_id("o")
            if oid in order_ids:
                continue
            order_ids.add(oid)
            subtotal = 0.0
            items = []
            for sequence, item in enumerate(row.get("items") or []):
                price = round(max(0.0, num(item.get("price"))), 2)
                qty = round(max(0.0, num(item.get("qty"))), 3)
                amount = round(price * qty, 2)
                subtotal += amount
                items.append((oid, sequence, txt(item.get("productId"), 64),
                              txt(item.get("name"), 60) or "商品",
                              txt(item.get("unit"), 16) or "斤", price, qty, amount))
            subtotal = round(subtotal, 2)
            discount = min(subtotal, round(max(0.0, num(row.get("discount"))), 2))
            total = round(subtotal - discount, 2)
            conn.execute(
                """INSERT INTO orders
                   (id, no, date, customer_id, customer_name, subtotal, discount, total, remark, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (oid, txt(row.get("no"), 40), txt(row.get("date"), 10),
                 txt(row.get("customerId"), 64), txt(row.get("customerName"), 60),
                 subtotal, discount, total, txt(row.get("remark"), 200),
                 int(num(row.get("createdAt")) or 0)),
            )
            for item in items:
                conn.execute(
                    """INSERT INTO order_items
                       (order_id, seq, product_id, name, unit, price, qty, amount)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    item,
                )

        conn.execute("COMMIT")
        return {"ok": True, "orders": len(order_ids), "products": len(product_ids)}
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def checkpoint():
    """程序退出前把 WAL 日志合并回主数据库文件"""
    try:
        conn = connect()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  HTTP 服务
# --------------------------------------------------------------------------- #
EXTRA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "FengshiPOS/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础输出 ---------------- #
    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=400):
        self.send_json({"error": message}, status)

    def log_request(self, code="-", size="-"):
        if urlparse(self.path).path.startswith("/api/"):
            sys.stdout.write("  [%s] %s %s -> %s\n" % (
                time.strftime("%H:%M:%S"), self.command, urlparse(self.path).path, code))

    def log_message(self, fmt, *args):
        pass  # 静态文件不刷屏

    # ---------------- 请求体 / 鉴权 ---------------- #
    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 64 * 1024 * 1024:
            raise ApiError("提交的数据过大", 413)
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError("请求数据不是合法的 JSON")

    def require_user(self):
        token = (self.headers.get("X-Auth-Token") or "").strip()
        with SESSIONS_LOCK:
            user = SESSIONS.get(token)
        if not user:
            raise ApiError("登录状态已失效，请重新登录", 401)
        return user

    def current_password_hash(self, username):
        conn = connect()
        try:
            row = conn.execute("SELECT salt, password_hash FROM users WHERE username=?",
                               (username,)).fetchone()
            if not row:
                raise ApiError("账号不存在", 401)
            return row["salt"], row["password_hash"]
        finally:
            conn.close()

    # ---------------- 路由 ---------------- #
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/ping":
                return self.send_json({"ok": True, "app": "fengshi-pos",
                                       "time": time.strftime("%Y-%m-%d %H:%M:%S")})
            if path == "/api/state":
                user = self.require_user()
                return self.send_json(load_state(user))
            if path == "/api/backup":
                self.require_user()
                return self.send_backup()
            if path.startswith("/api/"):
                return self.send_error_json("接口不存在", 404)
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self.serve_static(path)
        except ApiError as e:
            self.send_error_json(e.message, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.send_error_json("服务器内部错误：%s" % e, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()

            if path == "/api/login":
                return self.handle_login(body)
            if path == "/api/logout":
                token = (self.headers.get("X-Auth-Token") or "").strip()
                with SESSIONS_LOCK:
                    SESSIONS.pop(token, None)
                return self.send_json({"ok": True})
            if path == "/api/password":
                return self.handle_password(body)
            if path == "/api/shutdown":
                self.require_user()
                self.send_json({"ok": True})
                threading.Timer(0.5, request_exit).start()
                return
            if path == "/api/reset":
                user = self.require_user()
                conn = connect()
                try:
                    seed_demo(conn)
                    salt = secrets.token_hex(16)
                    conn.execute("DELETE FROM users")
                    conn.execute(
                        "INSERT INTO users (username, salt, password_hash) VALUES (?,?,?)",
                        ("admin", salt, hash_pw("123456", salt)),
                    )
                    conn.commit()
                finally:
                    conn.close()
                return self.send_json(load_state("admin"))
            if path.startswith("/api/"):
                return self.send_error_json("接口不存在", 404)
            return self.send_error_json("接口不存在", 404)
        except ApiError as e:
            self.send_error_json(e.message, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.send_error_json("服务器内部错误：%s" % e, 500)

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/state":
                self.require_user()
                result = save_state(body)
                return self.send_json(result)
            return self.send_error_json("接口不存在", 404)
        except ApiError as e:
            self.send_error_json(e.message, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.send_error_json("服务器内部错误：%s" % e, 500)

    # ---------------- 业务处理 ---------------- #
    def handle_login(self, body):
        username = txt(body.get("username"), 40)
        password = str(body.get("password") or "")
        if not username or not password:
            raise ApiError("请输入账号和密码")
        salt, password_hash = self.current_password_hash(username)
        if hash_pw(password, salt) != password_hash:
            raise ApiError("账号或密码不正确，请重新输入", 401)
        token = secrets.token_hex(24)
        with SESSIONS_LOCK:
            SESSIONS[token] = username
        self.send_json({"ok": True, "token": token, "username": username})

    def handle_password(self, body):
        user = self.require_user()
        old_password = str(body.get("oldPassword") or "")
        new_password = str(body.get("newPassword") or "")
        if not old_password or not new_password:
            raise ApiError("请填写完整的密码信息")
        if len(new_password) < 4:
            raise ApiError("新密码至少 4 位")
        salt, password_hash = self.current_password_hash(user)
        if hash_pw(old_password, salt) != password_hash:
            raise ApiError("原密码不正确")
        new_salt = secrets.token_hex(16)
        conn = connect()
        try:
            conn.execute("UPDATE users SET salt=?, password_hash=? WHERE username=?",
                         (new_salt, hash_pw(new_password, new_salt), user))
            conn.commit()
        finally:
            conn.close()
        self.send_json({"ok": True})

    def send_backup(self):
        checkpoint()
        with open(DB_PATH, "rb") as fh:
            data = fh.read()
        filename = "fengshi-%s.db" % time.strftime("%Y%m%d-%H%M%S")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------------- 静态文件 ---------------- #
    def serve_static(self, path):
        rel = unquote(path).lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        full = os.path.abspath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.abspath(WEB_DIR) + os.sep):
            return self.send_static_error(403, "禁止访问该路径")
        if not os.path.isfile(full):
            return self.send_static_error(404, "文件不存在：%s" % rel)
        ext = os.path.splitext(full)[1].lower()
        ctype = EXTRA_TYPES.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_static_error(self, status, message):
        body = ("<meta charset='utf-8'><div style='font-family:sans-serif;padding:40px;"
                "color:#444'><h2>%s</h2><p>%s</p>"
                "<p>请确认已通过「启动系统.bat」启动，或访问 http://127.0.0.1:%d/</p></div>"
                % (status, message, self.server.server_port)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


SERVER_REF = {"httpd": None}


def request_exit():
    """在独立线程里通知 serve_forever 退出（httpd.shutdown 不能在本线程调用）"""
    httpd = SERVER_REF.get("httpd")
    if httpd is not None:
        try:
            httpd.shutdown()
        except Exception:
            pass


def existing_instance_url():
    """检查是否已有本程序实例在运行（用于重复双击时直接打开已有页面，不再起第二个）"""
    for port in PORTS:
        # 先看端口是否空闲：空闲说明本程序不可能正占着它
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((HOST, port))
            probe.close()
            return None
        except OSError:
            probe.close()

        # 端口被占用：发一个 ping，看是不是本程序
        try:
            with socket.create_connection((HOST, port), timeout=0.5) as sock:
                sock.sendall(b"GET /api/ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                data = sock.recv(512)
            # 响应头里有 Server: FengshiPOS 标记，比读 JSON 正文更可靠
            if b"FengshiPOS" in data or b"fengshi-pos" in data:
                return "http://%s:%d/" % (HOST, port)
        except OSError:
            continue
    return None


def pick_port():
    for port in PORTS:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((HOST, port))
            probe.close()
            return port
        except OSError:
            probe.close()
            continue
    return None


def banner(url, port):
    print("=" * 62)
    print("  鲜蔬源 · 蔬菜店铺销售管理系统  已启动")
    print("-" * 62)
    print("  访问地址 ： %s" % url)
    print("  数据文件 ： %s" % DB_PATH)
    print("  默认账号 ： admin")
    print("  默认密码 ： 123456")
    print("-" * 62)
    print("  所有修改会即时写入 SQLite 数据库")
    print("  退出方式：关闭窗口 / Ctrl+C，或点「系统设置 → 关闭系统」")
    print("=" * 62)
    print("")


def setup_frozen_logging():
    """无控制台打包（--windowed）时 stdout/stderr 为空，把输出重定向到 data\\app.log"""
    if not getattr(sys, "frozen", False):
        return
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        log_file = open(os.path.join(DATA_DIR, "app.log"), "a", encoding="utf-8", buffering=1)
    except Exception:
        return
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file


def main():
    setup_frozen_logging()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    init_db()

    port = 0
    try:
        port = int(os.environ.get("FENGSHI_PORT") or 0)
    except ValueError:
        port = 0
    if not port:
        # 已经有一个实例在跑（例如重复双击了图标）：直接打开它的页面，不再启动第二个
        url = existing_instance_url()
        if url:
            print("检测到系统已在运行：%s" % url)
            if os.environ.get("FENGSHI_NO_BROWSER") != "1":
                webbrowser.open(url)
            return 0
        port = pick_port()
    if not port:
        print("端口 %s 均被占用，请关闭占用程序后重新启动。" % PORTS)
        return 1

    httpd = Server((HOST, port), ApiHandler)
    SERVER_REF["httpd"] = httpd
    url = "http://%s:%d/" % (HOST, port)
    banner(url, port)

    if os.environ.get("FENGSHI_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到退出指令……")
    finally:
        httpd.server_close()
        checkpoint()
        print("数据已保存到：%s" % DB_PATH)
        print("系统已安全退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

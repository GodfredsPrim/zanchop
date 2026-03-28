import os
import json
import logging
import sqlite3
import sys
import threading
import time
import uuid
from html import escape
from flask import Flask, request, send_from_directory, jsonify
from datetime import datetime
from dotenv import load_dotenv
import whatsapp_cloud_helper as cloud  # New helper

# Fix Windows console encoding for Unicode characters (emojis)
if sys.platform == 'win32':
    try:
        # Try to reconfigure stdout/stderr to use UTF-8
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        # Python < 3.7 fallback: wrap stdout/stderr with UTF-8 encoding
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

load_dotenv()

# =========================
# CONFIG
# =========================
ADMIN_PHONE = os.getenv("ADMIN_PHONE")
ADMIN_ACCESS_CODE = os.getenv("ADMIN_ACCESS_CODE") or os.getenv("VERIFY_TOKEN", "prim_store_verify")
MOMO_RECEIVER_NUMBER = os.getenv("MOMO_RECEIVER_NUMBER", "233599966902")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://your-ngrok-or-domain.com")

# Paystack Configuration
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_CALLBACK_URL = os.getenv("PAYSTACK_CALLBACK_URL", f"{PUBLIC_URL}/payment/callback")
PAYSTACK_INIT_URL = "https://api.paystack.co/transaction/initialize"
PAYSTACK_VERIFY_URL = "https://api.paystack.co/transaction/verify"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "prim_store.db")
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")
STATIC_FOLDER = os.path.join(BASE_DIR, 'static')
IMAGES_FOLDER = os.path.join(STATIC_FOLDER, 'images')
UPLOADS_FOLDER = os.path.join(STATIC_FOLDER, 'uploads')

os.makedirs(IMAGES_FOLDER, exist_ok=True)
os.makedirs(UPLOADS_FOLDER, exist_ok=True)
app.static_folder = STATIC_FOLDER

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(STATIC_FOLDER, filename)

# =========================
# DATABASE
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Users: phone, name, role (buyer/seller/admin), zone, created_at
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        phone TEXT PRIMARY KEY,
        name TEXT,
        shop_name TEXT,
        shop_description TEXT,
        shop_image_url TEXT,
        role TEXT DEFAULT 'buyer',
        zone TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Products: associated with a seller
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_phone TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        price REAL NOT NULL,
        stock INTEGER DEFAULT 1,
        image_url TEXT,
        category TEXT,
        FOREIGN KEY (seller_phone) REFERENCES users(phone)
    )''')
    # Orders
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_phone TEXT NOT NULL,
        seller_phone TEXT NOT NULL,
        total_price REAL NOT NULL,
        delivery_fee REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        delivery_zone TEXT,
        delivery_address TEXT,
        pickup_or_delivery TEXT DEFAULT 'delivery',
        payment_ref TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (buyer_phone) REFERENCES users(phone),
        FOREIGN KEY (seller_phone) REFERENCES users(phone)
    )''')
    # Order Items
    c.execute('''CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        price_at_purchase REAL NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS seller_invites (
        code TEXT PRIMARY KEY,
        seller_phone TEXT NOT NULL,
        seller_name TEXT,
        shop_name TEXT NOT NULL,
        shop_description TEXT,
        shop_image_url TEXT,
        zone TEXT,
        created_by TEXT,
        status TEXT DEFAULT 'active',
        claimed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS seller_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_phone TEXT NOT NULL UNIQUE,
        seller_name TEXT,
        shop_name TEXT,
        shop_description TEXT,
        shop_image_url TEXT,
        zone TEXT,
        landmark TEXT,
        status TEXT DEFAULT 'pending',
        reviewed_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        reviewed_at TIMESTAMP
    )''')

    def existing_columns(table_name):
        c.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in c.fetchall()}

    def add_column_if_missing(table_name, column_name, definition):
        if column_name not in existing_columns(table_name):
            try:
                c.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    add_column_if_missing("users", "shop_name", "TEXT")
    add_column_if_missing("users", "shop_description", "TEXT")
    add_column_if_missing("users", "shop_image_url", "TEXT")
    add_column_if_missing("users", "landmark", "TEXT")
    add_column_if_missing("seller_invites", "shop_image_url", "TEXT")
    add_column_if_missing("seller_invites", "landmark", "TEXT")
    add_column_if_missing("seller_requests", "shop_image_url", "TEXT")
    add_column_if_missing("seller_requests", "landmark", "TEXT")
    add_column_if_missing("seller_requests", "status", "TEXT DEFAULT 'pending'")
    add_column_if_missing("seller_requests", "reviewed_by", "TEXT")
    add_column_if_missing("seller_requests", "reviewed_at", "TIMESTAMP")

    add_column_if_missing("orders", "buyer_phone", "TEXT")
    add_column_if_missing("orders", "seller_phone", "TEXT")
    add_column_if_missing("orders", "total_price", "REAL")
    add_column_if_missing("orders", "delivery_fee", "REAL DEFAULT 0")
    add_column_if_missing("orders", "delivery_zone", "TEXT")
    add_column_if_missing("orders", "delivery_landmark", "TEXT")
    add_column_if_missing("orders", "delivery_address", "TEXT")
    add_column_if_missing("orders", "pickup_or_delivery", "TEXT DEFAULT 'delivery'")
    add_column_if_missing("orders", "payment_ref", "TEXT")
    add_column_if_missing("orders", "confirmation_code", "TEXT")
    add_column_if_missing("order_items", "item_name", "TEXT")
    add_column_if_missing("order_items", "addon_text", "TEXT")
    add_column_if_missing("order_items", "special_instructions", "TEXT")

    order_columns = existing_columns("orders")
    if "phone" in order_columns and "buyer_phone" in order_columns:
        c.execute("UPDATE orders SET buyer_phone = COALESCE(buyer_phone, phone) WHERE buyer_phone IS NULL OR buyer_phone = ''")
    if "total" in order_columns and "total_price" in order_columns:
        c.execute("UPDATE orders SET total_price = COALESCE(total_price, total) WHERE total_price IS NULL")
    if "seller_phone" in order_columns:
        c.execute("""
            UPDATE orders
            SET seller_phone = COALESCE(
                seller_phone,
                (
                    SELECT p.seller_phone
                    FROM order_items oi
                    JOIN products p ON p.id = oi.product_id
                    WHERE oi.order_id = orders.id
                    LIMIT 1
                )
            )
            WHERE seller_phone IS NULL OR seller_phone = ''
        """)
    if "delivery_fee" in order_columns:
        c.execute("UPDATE orders SET delivery_fee = COALESCE(delivery_fee, 0)")

    c.execute("CREATE INDEX IF NOT EXISTS idx_users_role_shop ON users(role, shop_name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_products_seller_stock ON products(seller_phone, stock)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_buyer_created ON orders(buyer_phone, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_seller_created ON orders(seller_phone, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_seller_requests_status ON seller_requests(status, created_at)")

    conn.commit()
    conn.close()

init_db()

# Cape Coast delivery zones and landmark suggestions
DELIVERY_ZONES = {
    "UCC Science / Main Campus": {
        "base_fee": 0.0,
        "rank": 0,
        "landmarks": [
            "School Bus Road",
            "Medical Village",
            "SRC Hall",
            "Sam Jonah Library",
            "Science Market",
            "UCC Hospital",
            "CALC",
            "School Junction"
        ]
    },
    "UCC North / Ayensu / Casford": {
        "base_fee": 0.0,
        "rank": 1,
        "landmarks": [
            "Ayensu",
            "Casford",
            "KNH",
            "Valco",
            "ATL",
            "Atlantic Hall",
            "Kakumdo",
            "SRC Junction"
        ]
    },
    "UCC South / Oguaa / Adehye": {
        "base_fee": 0.0,
        "rank": 1,
        "landmarks": [
            "Oguaa",
            "Adehye",
            "PSI",
            "SSNIT",
            "Superannuation",
            "Old Site",
            "UCC Taxi Rank",
            "West Gate"
        ]
    },
    "Amamoma / Apewosika": {
        "base_fee": 0.0,
        "rank": 2,
        "landmarks": [
            "Amamoma",
            "Apewosika",
            "University Hall",
            "Amamoma Junction",
            "UCC West Gate"
        ]
    },
    "OLA / Bakano / Town Centre": {
        "base_fee": 0.0,
        "rank": 3,
        "landmarks": [
            "OLA College of Education",
            "Bakano",
            "Kotokuraba",
            "Cape Coast Castle",
            "Victoria Park"
        ]
    },
    "Pedu / CCTU / Abura": {
        "base_fee": 0.0,
        "rank": 3,
        "landmarks": [
            "Cape Coast Sports Stadium",
            "Cape Coast Technical University",
            "Pedu Junction",
            "Abura",
            "Adisadel"
        ]
    },
    "Kwaprow / Duakor / Ntsin": {
        "base_fee": 0.0,
        "rank": 4,
        "landmarks": [
            "Kwaprow",
            "Duakor",
            "Ntsin",
            "Mpeasem",
            "Kwaprow Market"
        ]
    }
}
UCC_ZONES = {zone: meta["base_fee"] for zone, meta in DELIVERY_ZONES.items()}
# =========================
# HELPERS
# =========================
JSON_CACHE = {}
JSON_LOCK = threading.Lock()
MARKET_CACHE = {
    "shops": {"expires_at": 0, "value": None},
    "catalogs": {}
}
MARKET_CACHE_TTL = 8

def load_json(path, default=None):
    if default is None:
        default = {}
    with JSON_LOCK:
        if path in JSON_CACHE:
            return JSON_CACHE[path]
    if not os.path.exists(path):
        with JSON_LOCK:
            JSON_CACHE[path] = default
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            data = json.loads(content) if content else default
            with JSON_LOCK:
                JSON_CACHE[path] = data
            return data
    except Exception:
        return default

def save_json(path, data):
    try:
        with JSON_LOCK:
            JSON_CACHE[path] = data
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, path)
    except Exception as e:
        logging.error(f"Save error: {e}")

def reset_user_session(phone, state="idle", data=None, keep_cart=False):
    normalized_phone = normalize_phone(phone)
    sessions = load_json(SESSIONS_FILE, {})
    session_record = {"state": state, "data": data or {}}
    if keep_cart and normalized_phone in sessions and isinstance(sessions[normalized_phone].get("cart"), list):
        session_record["cart"] = sessions[normalized_phone]["cart"]
    sessions[normalized_phone] = session_record
    save_json(SESSIONS_FILE, sessions)

def show_onboarding_seller_image_choice(phone):
    buttons = [
        {"id": "seller_onboard_image_device", "title": "📷 Upload Photo"},
        {"id": "seller_onboard_image_link", "title": "🔗 Add Link"},
        {"id": "seller_onboard_image_skip", "title": "➡️ Skip"}
    ]
    success = cloud.send_interactive_buttons(
        phone,
        "🖼️ *Shop Image*\n\nAdd a shop image now, or skip and let admin approve the request without one.",
        buttons,
        header_text="Shop Image"
    )
    if not success:
        cloud.send_whatsapp_message(phone, "1. Upload Photo\n2. Add Link\n3. Skip")

def invalidate_market_cache(seller_phone=None):
    MARKET_CACHE["shops"] = {"expires_at": 0, "value": None}
    if seller_phone:
        MARKET_CACHE["catalogs"].pop(normalize_phone(seller_phone), None)
    else:
        MARKET_CACHE["catalogs"].clear()

def build_public_asset_url(relative_path):
    cleaned = relative_path.replace("\\", "/").lstrip("/")
    return f"{PUBLIC_URL.rstrip('/')}/{cleaned}"

def save_incoming_whatsapp_image(media_id):
    content, mime_type = cloud.fetch_media_bytes(media_id)
    if not content:
        return None

    ext_map = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    extension = ext_map.get((mime_type or "").lower(), ".jpg")
    filename = f"{uuid.uuid4().hex}{extension}"
    file_path = os.path.join(UPLOADS_FOLDER, filename)

    with open(file_path, "wb") as f:
        f.write(content)

    return build_public_asset_url(f"static/uploads/{filename}")

SCHEMA_CACHE = {}

def get_table_columns(table_name):
    if table_name not in SCHEMA_CACHE:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(f"PRAGMA table_info({table_name})")
        SCHEMA_CACHE[table_name] = {row[1] for row in c.fetchall()}
        conn.close()
    return SCHEMA_CACHE[table_name]

def order_column_expr(preferred, fallback=None, default="NULL"):
    columns = get_table_columns("orders")
    if preferred in columns and fallback and fallback in columns:
        return f"COALESCE({preferred}, {fallback})"
    if preferred in columns:
        return preferred
    if fallback and fallback in columns:
        return fallback
    return default

def get_orders_view_sql():
    return f"""
        SELECT
            id,
            {order_column_expr('buyer_phone', 'phone', 'NULL')} AS buyer_phone,
            {order_column_expr('seller_phone', None, 'NULL')} AS seller_phone,
            {order_column_expr('total_price', 'total', '0')} AS total_price,
            {order_column_expr('delivery_fee', None, '0')} AS delivery_fee,
            {order_column_expr('delivery_zone', None, 'NULL')} AS delivery_zone,
            {order_column_expr('delivery_landmark', None, 'NULL')} AS delivery_landmark,
            {order_column_expr('delivery_address', None, 'NULL')} AS delivery_address,
            {order_column_expr('pickup_or_delivery', None, "'delivery'")} AS pickup_or_delivery,
            {order_column_expr('status', None, "'pending'")} AS status,
            {order_column_expr('payment_ref', None, 'NULL')} AS payment_ref,
            {order_column_expr('confirmation_code', None, 'NULL')} AS confirmation_code,
            created_at
        FROM orders
    """

def resolve_zone_choice(text):
    cleaned = (text or "").strip()
    if not cleaned:
        return None

    if cleaned.lower().startswith("zone_"):
        try:
            idx = int(cleaned.split("_")[1]) - 1
            zones = list(UCC_ZONES.keys())
            if 0 <= idx < len(zones):
                return zones[idx]
        except (IndexError, ValueError):
            pass

    try:
        idx = int(cleaned) - 1
        zones = list(UCC_ZONES.keys())
        if 0 <= idx < len(zones):
            return zones[idx]
    except ValueError:
        pass

    lowered = cleaned.lower()
    for zone in UCC_ZONES.keys():
        if lowered == zone.lower() or lowered in zone.lower() or zone.lower() in lowered:
            return zone
    return None

def get_landmarks_for_zone(zone):
    return DELIVERY_ZONES.get(zone, {}).get("landmarks", [])

def resolve_landmark_choice(zone, text):
    cleaned = (text or "").strip()
    landmarks = get_landmarks_for_zone(zone)
    if not cleaned or not landmarks:
        return None

    prefix = "landmark_"
    if cleaned.lower().startswith(prefix):
        try:
            idx = int(cleaned.split("_")[-1]) - 1
            if 0 <= idx < len(landmarks):
                return landmarks[idx]
        except ValueError:
            pass

    try:
        idx = int(cleaned) - 1
        if 0 <= idx < len(landmarks):
            return landmarks[idx]
    except ValueError:
        pass

    lowered = cleaned.lower()
    for landmark in landmarks:
        if lowered == landmark.lower() or lowered in landmark.lower() or landmark.lower() in lowered:
            return landmark
    return None

def calculate_delivery_fee(seller_zone, seller_landmark, buyer_zone, buyer_landmark):
    return 0.0

def set_reply_map(session, key, items):
    session.setdefault("data", {})
    session["data"][key] = {str(index): item for index, item in enumerate(items, 1)}

def get_reply_map_value(session, key, text):
    reply_map = session.get("data", {}).get(key, {})
    return reply_map.get((text or "").strip())

def clear_reply_map(session, key):
    session.setdefault("data", {})
    session["data"].pop(key, None)

def truncate_text(value, limit):
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"

def fetch_available_shops():
    cached = MARKET_CACHE["shops"]
    if cached["value"] is not None and cached["expires_at"] > time.time():
        return cached["value"]

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT u.phone, u.shop_name, u.shop_description, u.zone, u.landmark, u.shop_image_url
        FROM users u
        WHERE u.role = 'seller'
          AND u.shop_name IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM products p
              WHERE p.seller_phone = u.phone AND p.stock > 0
          )
        ORDER BY u.shop_name
    """)
    shops = c.fetchall()
    conn.close()
    MARKET_CACHE["shops"] = {"expires_at": time.time() + MARKET_CACHE_TTL, "value": shops}
    return shops

def fetch_shop_catalog(seller_phone):
    normalized_seller_phone = normalize_phone(seller_phone)
    cached = MARKET_CACHE["catalogs"].get(normalized_seller_phone)
    if cached and cached["expires_at"] > time.time():
        return cached["value"]

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT id, name, price, description, image_url, stock
        FROM products
        WHERE seller_phone = ? AND stock > 0
        ORDER BY id DESC
    """, (normalized_seller_phone,))
    products = c.fetchall()
    c.execute("""
        SELECT shop_name, shop_description, zone, landmark, shop_image_url
        FROM users
        WHERE phone = ?
    """, (normalized_seller_phone,))
    shop = c.fetchone()
    conn.close()
    value = (products, shop)
    MARKET_CACHE["catalogs"][normalized_seller_phone] = {
        "expires_at": time.time() + MARKET_CACHE_TTL,
        "value": value
    }
    return value

def format_order_status(status):
    mapping = {
        "awaiting_payment": ("Awaiting Payment", "⏳"),
        "pending": ("Pending Review", "🧾"),
        "paid": ("Paid", "✅"),
        "accepted": ("Accepted", "✅"),
        "preparing": ("Preparing", "🍳"),
        "on_the_way": ("On The Way", "🛵"),
        "delivered": ("Delivered", "🎉"),
        "completed": ("Delivered", "🎉"),
        "cancelled": ("Cancelled", "❌"),
    }
    label, icon = mapping.get(status, (status.replace("_", " ").title(), "ℹ️"))
    return f"{icon} {label}"

def get_order_record_by_reference(reference):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT id, buyer_phone, seller_phone, total_price, pickup_or_delivery, delivery_zone,
               delivery_landmark, delivery_address, status, confirmation_code
        FROM ({get_orders_view_sql()}) AS orders_view
        WHERE payment_ref = ?
        LIMIT 1
    """, (reference,))
    order = c.fetchone()
    conn.close()
    return order

def finalize_paid_order(reference):
    order = get_order_record_by_reference(reference)
    if not order:
        return None, "missing"

    order_id, buyer_phone, seller_phone, total, pickup_or_delivery, delivery_zone, delivery_landmark, delivery_address, status, confirmation_code = order
    already_paid = status in {"paid", "accepted", "preparing", "on_the_way", "delivered", "completed"} and confirmation_code

    if already_paid:
        return {
            "order_id": order_id,
            "buyer_phone": buyer_phone,
            "seller_phone": seller_phone,
            "total": total,
            "pickup_or_delivery": pickup_or_delivery,
            "delivery_zone": delivery_zone,
            "delivery_landmark": delivery_landmark,
            "delivery_address": delivery_address,
            "confirmation_code": confirmation_code,
            "already_paid": True,
        }, None

    confirmation_code = confirmation_code or generate_order_code()
    update_order_status(order_id, "paid", confirmation_code)

    buyer_msg = f"✅ *Payment Successful!*\n\nOrder #{order_id}\nAmount Paid: GHS {total:.2f}\n\n🎫 *Confirmation Code: {confirmation_code}*\n\n"
    if pickup_or_delivery == "pickup":
        buyer_msg += "Show this code when you collect your order from the restaurant."
    else:
        buyer_msg += "Show this code to the delivery person when your food arrives."
    cloud.send_whatsapp_message(buyer_phone, buyer_msg)

    notify_seller(order_id, buyer_phone, total, seller_phone, {
        "pickup_or_delivery": pickup_or_delivery,
        "delivery_zone": delivery_zone,
        "delivery_landmark": delivery_landmark,
        "delivery_address": delivery_address,
    })

    return {
        "order_id": order_id,
        "buyer_phone": buyer_phone,
        "seller_phone": seller_phone,
        "total": total,
        "pickup_or_delivery": pickup_or_delivery,
        "delivery_zone": delivery_zone,
        "delivery_landmark": delivery_landmark,
        "delivery_address": delivery_address,
        "confirmation_code": confirmation_code,
        "already_paid": False,
    }, None

def generate_seller_code():
    import random
    import string

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    while True:
        code = "SELL-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        c.execute("SELECT 1 FROM seller_invites WHERE code = ?", (code,))
        if not c.fetchone():
            conn.close()
            return code

def create_seller_invite(seller_phone, seller_name, shop_name, shop_description, shop_image_url, zone, landmark, created_by):
    code = generate_seller_code()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO seller_invites
        (code, seller_phone, seller_name, shop_name, shop_description, shop_image_url, zone, landmark, created_by, status, claimed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL)
    """, (
        code,
        normalize_phone(seller_phone),
        seller_name,
        shop_name,
        shop_description,
        shop_image_url,
        zone,
        landmark,
        normalize_phone(created_by)
    ))
    conn.commit()
    conn.close()
    return code

def get_seller_invite(code):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT code, seller_phone, seller_name, shop_name, shop_description, shop_image_url, zone, landmark, status, claimed_at
        FROM seller_invites
        WHERE UPPER(code) = UPPER(?)
    """, (code.strip(),))
    invite = c.fetchone()
    conn.close()
    return invite

def claim_seller_invite(code, claimant_phone):
    normalized_phone = normalize_phone(claimant_phone)
    invite = get_seller_invite(code)
    if not invite:
        return None, "That seller access code was not found."
    if invite[8] != "active":
        return None, "That seller access code has already been used."
    if normalize_phone(invite[1]) != normalized_phone:
        return None, "That code is assigned to a different phone number."

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        UPDATE seller_invites
        SET status = 'claimed', claimed_at = CURRENT_TIMESTAMP
        WHERE code = ?
    """, (invite[0],))
    conn.commit()
    conn.close()
    return invite, None

def create_seller_request(seller_phone, seller_name, zone, landmark="", shop_name=None, shop_description="", shop_image_url=""):
    normalized_phone = normalize_phone(seller_phone)
    seller_name = (seller_name or "").strip()
    fallback_shop_name = shop_name or (f"{seller_name.split()[0]}'s Shop" if seller_name else "New Seller Shop")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO seller_requests
        (seller_phone, seller_name, shop_name, shop_description, shop_image_url, zone, landmark, status, reviewed_by, reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL)
        ON CONFLICT(seller_phone) DO UPDATE SET
            seller_name = excluded.seller_name,
            shop_name = excluded.shop_name,
            shop_description = excluded.shop_description,
            shop_image_url = excluded.shop_image_url,
            zone = excluded.zone,
            landmark = excluded.landmark,
            status = 'pending',
            reviewed_by = NULL,
            reviewed_at = NULL
    """, (
        normalized_phone,
        seller_name,
        fallback_shop_name,
        shop_description or "Awaiting admin review",
        shop_image_url or "",
        zone or "",
        landmark or "",
    ))
    c.execute("SELECT id FROM seller_requests WHERE seller_phone = ?", (normalized_phone,))
    request_id = c.fetchone()[0]
    conn.commit()
    conn.close()
    return request_id

def get_pending_seller_requests(limit=20):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT id, seller_phone, seller_name, shop_name, zone, landmark, created_at
        FROM seller_requests
        WHERE status = 'pending'
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))
    requests = c.fetchall()
    conn.close()
    return requests

def get_seller_request(request_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT id, seller_phone, seller_name, shop_name, shop_description, shop_image_url,
               zone, landmark, status, reviewed_by, created_at, reviewed_at
        FROM seller_requests
        WHERE id = ?
        LIMIT 1
    """, (request_id,))
    request_row = c.fetchone()
    conn.close()
    return request_row

def update_seller_request_status(request_id, status, reviewed_by=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        UPDATE seller_requests
        SET status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (status, normalize_phone(reviewed_by) if reviewed_by else None, request_id))
    conn.commit()
    conn.close()

def submit_seller_request(phone, session):
    data = session.get("data", {})
    request_id = create_seller_request(
        phone,
        data.get("name", ""),
        data.get("zone", ""),
        landmark=data.get("landmark", ""),
        shop_name=data.get("shop_name"),
        shop_description=data.get("shop_desc", ""),
        shop_image_url=data.get("shop_image_url", ""),
    )
    create_user(phone, data.get("name"), "buyer", data.get("zone"), data.get("landmark"))

    cloud.send_whatsapp_message(
        phone,
        "📝 *Seller Request Sent*\n\n"
        "Your phone number has been saved as your seller number.\n"
        "An admin will review your shop details and confirm the seller profile."
    )
    if ADMIN_PHONE:
        cloud.send_whatsapp_message(
            ADMIN_PHONE,
            f"📨 *New Seller Request*\n\n"
            f"Request #{request_id}\n"
            f"Name: {data.get('name', 'Not set')}\n"
            f"Phone: {normalize_phone(phone)}\n"
            f"Shop: {data.get('shop_name', 'Not set')}\n"
            f"Zone: {data.get('zone', 'Not set')}\n"
            f"Landmark: {data.get('landmark', 'Not set')}\n\n"
            "Open *Seller Requests* in the admin dashboard to review it."
        )

    reset_user_session(phone, state="buyer_menu", data={}, keep_cart=True)
    buyer_user = get_user(phone)
    session["data"] = {}
    session["state"] = "buyer_menu"
    if buyer_user:
        show_buyer_home(phone, buyer_user, session)
    return request_id

def activate_seller_profile(form, reviewed_by):
    seller_phone = form["seller_phone"]
    create_user(seller_phone, form["seller_name"], "seller", form["zone"], form["landmark"])
    update_user(seller_phone, name=form["seller_name"], role="seller", zone=form["zone"], landmark=form["landmark"])
    update_user_shop(
        seller_phone,
        form["shop_name"],
        form["shop_description"],
        form["shop_image_url"],
        form["landmark"]
    )
    if form.get("request_id"):
        update_seller_request_status(form["request_id"], "approved", reviewed_by)
    reset_user_session(seller_phone, state="idle", data={})
    buttons = [
        {"id": "seller_open_dashboard", "title": "🍽️ Open Dashboard"}
    ]
    success = cloud.send_interactive_buttons(
        seller_phone,
        "✅ *Seller Approved*\n\nYour shop profile has been confirmed by ZanChop admin. Your number is now active as a seller account.",
        buttons,
        header_text="Seller Approved"
    )
    if not success:
        cloud.send_whatsapp_message(
            seller_phone,
            "✅ *Seller Approved*\n\nYour shop profile has been confirmed by ZanChop admin. Open the dashboard with the button or send *menu*."
        )

# =========================
# PAYSTACK PAYMENT FUNCTIONS
# =========================
def generate_order_code():
    """Generate a unique order confirmation code"""
    import random
    import string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def initiate_paystack_payment(buyer_phone, amount, order_id, reference):
    """Initialize Paystack payment and return authorization URL"""
    import requests
    
    if not PAYSTACK_SECRET_KEY or PAYSTACK_SECRET_KEY == "PASTE_YOUR_PAYSTACK_SECRET_KEY_HERE":
        logging.warning("Paystack secret key not configured")
        return None
    
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    
    data = {
        "amount": int(amount * 100),  # Paystack uses kobo
        "email": f"buyer_{buyer_phone}@zanchop.com",
        "reference": reference,
        "callback_url": PAYSTACK_CALLBACK_URL,
        "metadata": {
            "order_id": order_id,
            "buyer_phone": buyer_phone
        }
    }
    
    try:
        response = requests.post(PAYSTACK_INIT_URL, headers=headers, json=data)
        response.raise_for_status()
        result = response.json()
        if result.get("status"):
            return result["data"]["authorization_url"]
        return None
    except Exception as e:
        logging.error(f"Paystack init error: {e}")
        return None

def verify_paystack_payment(reference):
    """Verify Paystack payment status"""
    import requests
    
    if not PAYSTACK_SECRET_KEY:
        return None
    
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"
    }
    
    try:
        response = requests.get(f"{PAYSTACK_VERIFY_URL}/{reference}", headers=headers)
        response.raise_for_status()
        result = response.json()
        return result.get("data", {}).get("status") == "success"
    except Exception as e:
        logging.error(f"Paystack verify error: {e}")
        return False

# =========================
# DB FUNCTIONS
# =========================
def get_products():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, description, price, stock, image_url, seller_phone FROM products WHERE stock > 0")
    prods = c.fetchall()
    conn.close()
    return prods

def get_product_by_id(pid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT seller_phone, id, name, price, stock, image_url FROM products WHERE id = ?", (pid,))
    prod = c.fetchone()
    conn.close()
    return prod

def get_seller_product(pid, seller_phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT id, seller_phone, name, description, price, stock, image_url
        FROM products
        WHERE id = ? AND seller_phone = ?
    """, (pid, normalize_phone(seller_phone)))
    prod = c.fetchone()
    conn.close()
    return prod

def update_product_details(pid, seller_phone, **fields):
    allowed = {"name", "description", "price", "image_url", "stock"}
    updates = []
    values = []
    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key} = ?")
            values.append(value)
    if not updates:
        return False

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    values.extend([pid, normalize_phone(seller_phone)])
    c.execute(f"UPDATE products SET {', '.join(updates)} WHERE id = ? AND seller_phone = ?", values)
    conn.commit()
    updated = c.rowcount > 0
    conn.close()
    if updated:
        invalidate_market_cache(seller_phone)
    return updated

def delete_product(pid, seller_phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id = ? AND seller_phone = ?", (pid, normalize_phone(seller_phone)))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()
    if deleted:
        invalidate_market_cache(seller_phone)
    return deleted

def get_seller_orders(seller_phone, limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT id, buyer_phone, total_price, status, pickup_or_delivery, delivery_zone, delivery_landmark, delivery_address, confirmation_code, payment_ref, created_at
        FROM ({get_orders_view_sql()}) AS orders_view
        WHERE seller_phone = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (normalize_phone(seller_phone), limit))
    orders = c.fetchall()
    conn.close()
    return orders

def get_seller_order(order_id, seller_phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT id, buyer_phone, seller_phone, total_price, delivery_fee, delivery_zone, delivery_landmark, delivery_address, pickup_or_delivery, status, payment_ref, confirmation_code, created_at
        FROM ({get_orders_view_sql()}) AS orders_view
        WHERE id = ? AND seller_phone = ?
        LIMIT 1
    """, (order_id, normalize_phone(seller_phone)))
    order = c.fetchone()
    conn.close()
    return order

# =========================
# USER HELPERS
# =========================
def normalize_phone(phone):
    """Normalize phone number format - remove + prefix for consistency"""
    if phone:
        return phone.lstrip('+')
    return phone

def get_user(phone):
    # Normalize phone for database lookup - try both with and without +
    normalized = normalize_phone(phone)
    with_plus = f"+{normalized}"
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT phone, name, shop_name, shop_description, role, zone, shop_image_url, landmark FROM users WHERE phone = ? OR phone = ? OR phone = ?", 
              (normalized, with_plus, phone))
    user = c.fetchone()
    conn.close()
    return user

USER_PHONE = 0
USER_NAME = 1
USER_SHOP_NAME = 2
USER_SHOP_DESCRIPTION = 3
USER_ROLE = 4
USER_ZONE = 5
USER_SHOP_IMAGE = 6
USER_LANDMARK = 7

def create_user(phone, name=None, role='buyer', zone=None, landmark=None):
    normalized_phone = normalize_phone(phone)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (phone, name, role, zone, landmark) VALUES (?, ?, ?, ?, ?)",
              (normalized_phone, name, role, zone, landmark))
    c.execute("""
        UPDATE users
        SET name = COALESCE(?, name),
            role = COALESCE(?, role),
            zone = COALESCE(?, zone),
            landmark = COALESCE(?, landmark)
        WHERE phone = ?
    """, (name, role, zone, landmark, normalized_phone))
    conn.commit()
    conn.close()

def update_user(phone, name=None, role=None, zone=None, landmark=None):
    normalized_phone = normalize_phone(phone)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if name: c.execute("UPDATE users SET name = ? WHERE phone = ?", (name, normalized_phone))
    if role: c.execute("UPDATE users SET role = ? WHERE phone = ?", (role, normalized_phone))
    if zone: c.execute("UPDATE users SET zone = ? WHERE phone = ?", (zone, normalized_phone))
    if landmark: c.execute("UPDATE users SET landmark = ? WHERE phone = ?", (landmark, normalized_phone))
    conn.commit()
    conn.close()

def get_product_details(pid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT p.seller_phone, p.id, p.name, p.description, p.price, p.stock, p.image_url,
               u.shop_name, u.shop_description, u.zone, u.landmark
        FROM products p
        LEFT JOIN users u ON u.phone = p.seller_phone
        WHERE p.id = ?
        LIMIT 1
    """, (pid,))
    product = c.fetchone()
    conn.close()
    return product

def search_market_catalog(query, limit=10):
    cleaned = (query or "").strip().lower()
    if not cleaned:
        return []
    like_query = f"%{cleaned}%"
    prefix_query = f"{cleaned}%"
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT p.id, p.name, p.price, p.description, p.stock, p.image_url, p.seller_phone,
               u.shop_name, u.zone
        FROM products p
        JOIN users u ON u.phone = p.seller_phone
        WHERE p.stock > 0
          AND (
              LOWER(p.name) LIKE ?
              OR LOWER(COALESCE(p.description, '')) LIKE ?
              OR LOWER(COALESCE(u.shop_name, '')) LIKE ?
          )
        ORDER BY
            CASE WHEN LOWER(p.name) LIKE ? THEN 0 ELSE 1 END,
            p.id DESC
        LIMIT ?
    """, (like_query, like_query, like_query, prefix_query, limit))
    results = c.fetchall()
    conn.close()
    return results

def get_buyer_orders(buyer_phone, limit=8):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT o.id, o.total_price, o.status, o.pickup_or_delivery, o.delivery_zone,
               o.delivery_landmark, o.delivery_address, o.confirmation_code, o.created_at,
               u.shop_name
        FROM ({get_orders_view_sql()}) AS o
        LEFT JOIN users u ON u.phone = o.seller_phone
        WHERE o.buyer_phone = ?
        ORDER BY o.created_at DESC
        LIMIT ?
    """, (normalize_phone(buyer_phone), limit))
    orders = c.fetchall()
    conn.close()
    return orders

def get_buyer_order(order_id, buyer_phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT o.id, o.buyer_phone, o.seller_phone, o.total_price, o.delivery_fee, o.delivery_zone,
               o.delivery_landmark, o.delivery_address, o.pickup_or_delivery, o.status,
               o.payment_ref, o.confirmation_code, o.created_at, u.shop_name
        FROM ({get_orders_view_sql()}) AS o
        LEFT JOIN users u ON u.phone = o.seller_phone
        WHERE o.id = ? AND o.buyer_phone = ?
        LIMIT 1
    """, (order_id, normalize_phone(buyer_phone)))
    order = c.fetchone()
    conn.close()
    return order

def get_order_items(order_id):
    item_columns = get_table_columns("order_items")
    item_name_expr = "COALESCE(oi.item_name, p.name)" if "item_name" in item_columns else "p.name"
    addon_expr = "oi.addon_text" if "addon_text" in item_columns else "NULL"
    instructions_expr = "oi.special_instructions" if "special_instructions" in item_columns else "NULL"

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT oi.product_id, {item_name_expr} AS item_name, oi.quantity, oi.price_at_purchase,
               {addon_expr} AS addon_text, {instructions_expr} AS special_instructions
        FROM order_items oi
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = ?
        ORDER BY oi.id ASC
    """, (order_id,))
    items = c.fetchall()
    conn.close()
    return items

# =========================
# WEBHOOKS (Meta WhatsApp Cloud API)
# =========================
def process_message(from_phone, msg_body, is_interactive=False, metadata=None):
    """Central processing for all incoming messages."""
    # Normalize phone for consistency
    normalized_phone = normalize_phone(from_phone)
    logging.info(f"Incoming from {normalized_phone}: {msg_body}")

    # Session management - use normalized phone
    sessions = load_json(SESSIONS_FILE, {})
    session_key = normalized_phone
    if session_key not in sessions:
        sessions[session_key] = {"state": "start", "data": {}}
    session = sessions[session_key]

    user = get_user(normalized_phone)
    
    try:
        admin_phone = os.getenv("ADMIN_PHONE", "").lstrip('+')
        is_admin = normalized_phone == admin_phone
        admin_verified = session.get("data", {}).get("admin_verified")
        
        # Admin always uses the admin flow first
        if is_admin:
            handle_admin_flow(from_phone, msg_body, session)
        # New user OR in onboarding flow
        elif not user or session["state"].startswith("onboarding_"):
            handle_onboarding(from_phone, msg_body, session)
        # Existing user - route based on role
        elif user[USER_ROLE] == 'seller':
            handle_seller_flow(from_phone, msg_body, session, user)
        elif user[USER_ROLE] == 'buyer':
            handle_buyer_flow(from_phone, msg_body, session, user)
        else:
            send_text(from_phone, "Welcome! Type 'menu' to see your options.")

        save_json(SESSIONS_FILE, sessions)
        return True
    except Exception as e:
        logging.exception(f"Error in process_message: {e}")
        return False

@app.route("/twilio", methods=["POST"])
def twilio_webhook():
    """
    ⚠️ DEPRECATED: This endpoint is for legacy Twilio integration.
    Please use the /webhook endpoint for Meta WhatsApp Cloud API.
    """
    logging.warning("Legacy Twilio webhook called - please update to use /webhook")
    data = request.form
    from_phone = data.get("From", "").replace("whatsapp:", "")
    normalized_phone = normalize_phone(from_phone)
    msg_body = data.get("Body", "").strip()
    media_url = data.get("MediaUrl0") 

    if media_url:
        sessions = load_json(SESSIONS_FILE, {})
        if normalized_phone in sessions:
            sessions[normalized_phone]["pending_image"] = media_url
            save_json(SESSIONS_FILE, sessions)

    process_message(from_phone, msg_body)
    return "OK", 200

@app.route("/webhook", methods=["GET", "POST"])
def whatsapp_webhook():
    """Meta WhatsApp Cloud API Webhook."""
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode == "subscribe" and token == os.getenv("VERIFY_TOKEN", "prim_store_verify"):
            print("✅ Meta Cloud API Webhook Verified!")
            return challenge, 200
        return "Forbidden", 403

    if request.method == "POST":
        data = request.json
        try:
            # Extract messages from Meta Cloud API format
            entries = data.get("entry", [])
            if not entries:
                return "OK", 200
                
            changes = entries[0].get("changes", [])
            if not changes:
                return "OK", 200
                
            value = changes[0].get("value", {})
            messages = value.get("messages", [])
            
            if not messages:
                return "OK", 200
                
            msg = messages[0]
            from_phone = msg["from"]
            normalized_phone = normalize_phone(from_phone)
            msg_type = msg.get("type", "text")
        
            # Session management for media
            sessions = load_json(SESSIONS_FILE, {})
            if normalized_phone not in sessions:
                sessions[normalized_phone] = {"state": "start", "data": {}}
            session = sessions[normalized_phone]
            
            # Handle different message types
            msg_body = ""
            is_interactive = False
            
            if msg_type == "text":
                msg_body = msg["text"]["body"].strip()
            elif msg_type == "interactive":
                is_interactive = True
                interactive = msg["interactive"]
                if interactive["type"] == "button_reply":
                    msg_body = interactive["button_reply"]["id"]
                elif interactive["type"] == "list_reply":
                    msg_body = interactive["list_reply"]["id"]
            elif msg_type == "image":
                # Handle incoming images
                image = msg.get("image", {})
                image_url = image.get("id", "")  # Media ID
                # Store media ID in session for processing
                if image_url:
                    session["pending_image_id"] = image_url
                    session["pending_image_url"] = image.get("link", "")
                    if handle_onboarding_seller_image_upload(from_phone, session, image_url):
                        save_json(SESSIONS_FILE, sessions)
                        return "OK", 200
                    if handle_admin_seller_image_upload(from_phone, session, image_url):
                        save_json(SESSIONS_FILE, sessions)
                        return "OK", 200
                    if handle_seller_image_upload(from_phone, session, image_url):
                        save_json(SESSIONS_FILE, sessions)
                        return "OK", 200
                # Send acknowledgment
                cloud.send_whatsapp_message(from_phone, "📷 Image received. When you're in an image upload step, I'll attach it automatically.")
                save_json(SESSIONS_FILE, sessions)
                return "OK", 200
            elif msg_type == "audio":
                # Handle audio messages
                audio = msg.get("audio", {})
                session["pending_audio_id"] = audio.get("id", "")
                save_json(SESSIONS_FILE, sessions)
                return "OK", 200
            
            if msg_body:
                process_message(from_phone, msg_body, is_interactive=is_interactive)
            
            return "OK", 200
        except Exception as e:
            logging.error(f"Error processing Meta Cloud API webhook: {e}")
            return "OK", 200

def send_text(to, text):
    """Send a text message via Meta WhatsApp Cloud API."""
    cloud.send_whatsapp_message(to, text)

# =========================
# ONBOARDING FLOW
# =========================
def handle_onboarding(phone, text, session):
    state = session["state"]
    text = (text or "").strip()

    if state == "start":
        msg = (
            "🍽️ *Welcome to ZanChop UCC*\n\n"
            "Campus cravings, handled beautifully.\n"
            "Browse trusted restaurants, order in minutes, and pay securely.\n\n"
            "Let's get your profile ready. What's your *full name*?"
        )
        cloud.send_whatsapp_message(phone, msg)
        session["state"] = "onboarding_name"
    
    elif state == "onboarding_name":
        session["data"]["name"] = text
        zones = []
        for i, zone in enumerate(UCC_ZONES.keys(), 1):
            preview = ", ".join(DELIVERY_ZONES.get(zone, {}).get("landmarks", [])[:2]) or "Popular campus area"
            zones.append({"id": f"zone_{i}", "title": zone, "description": truncate_text(preview, 72)})
        
        sections = [{"title": "Select Your Zone", "rows": zones}]
        success = cloud.send_interactive_list(
            phone, 
            f"✅ Great, {text}!\n\n📍 Which campus zone are you in?", 
            "Select Zone", 
            sections
        )
        if not success:
            msg = f"✅ Great, {text}!\n\nChoose your campus zone:\n"
            for i, zone in enumerate(UCC_ZONES.keys(), 1):
                preview = ", ".join(DELIVERY_ZONES.get(zone, {}).get("landmarks", [])[:2])
                msg += f"{i}. {zone}"
                if preview:
                    msg += f" - {preview}"
                msg += "\n"
            cloud.send_whatsapp_message(phone, msg)
        session["state"] = "onboarding_zone"
        
    elif state == "onboarding_zone":
        zone_name = resolve_zone_choice(text)
        if zone_name:
            session["data"]["zone"] = zone_name
            
            buttons = [
                {"id": "role_buyer", "title": "🛒 I want to BUY"},
                {"id": "role_seller", "title": "🍔 I want to SELL"}
            ]
            success = cloud.send_interactive_buttons(
                phone,
                f"📍 *Zone: {zone_name}*\n\nFinal step! How do you want to use ZanChop?",
                buttons
            )
            if not success:
                cloud.send_whatsapp_message(phone, f"📍 Zone: {zone_name}\n\n1. I want to BUY\n2. I want to SELL")
            session["state"] = "onboarding_role"
        else:
            cloud.send_whatsapp_message(phone, "❌ Invalid selection. Please select your zone from the menu.")
            
    elif state == "onboarding_role":
        role = ""
        if "buyer" in text.lower(): role = "buyer"
        elif "seller" in text.lower(): role = "seller"
        
        if not role:
            cloud.send_whatsapp_message(phone, "❌ Please tap one of the buttons.")
            return

        session["data"]["role"] = role
        if role == "seller":
            cloud.send_whatsapp_message(
                phone,
                "🏪 *Seller Setup*\n\nLet's build your shop profile for admin approval.\n\nWhat is your *shop name*?"
            )
            session["state"] = "onboarding_seller_shop_name"
        else:
            finalize_onboarding(phone, session)

    elif state == "onboarding_seller_shop_name":
        session["data"]["shop_name"] = text
        cloud.send_whatsapp_message(
            phone,
            "📝 *Shop Description*\n\nGive a short description of what you sell.\nExample: Home-style jollof, fried rice, and drinks"
        )
        session["state"] = "onboarding_seller_shop_desc"

    elif state == "onboarding_seller_shop_desc":
        session["data"]["shop_desc"] = text
        show_landmark_picker(phone, session["data"].get("zone", ""), "onboarding_seller_landmark", header_text="Shop Landmark")
        session["state"] = "onboarding_seller_landmark"

    elif state == "onboarding_seller_landmark":
        zone = session["data"].get("zone")
        landmark = resolve_landmark_choice(zone, text.replace("onboarding_seller_landmark_", "landmark_"))
        if not landmark:
            cloud.send_whatsapp_message(phone, "❌ Landmark not found. Please tap a valid landmark.")
            return
        session["data"]["landmark"] = landmark
        show_onboarding_seller_image_choice(phone)
        session["state"] = "onboarding_seller_image_choice"

    elif state == "onboarding_seller_image_choice":
        if text in {"seller_onboard_image_device", "1"}:
            cloud.send_whatsapp_message(phone, "📷 Send your shop image from your device now, or tap Skip to continue without one.")
            session["state"] = "onboarding_seller_image_upload"
            pending_image_id = session.get("pending_image_id")
            if pending_image_id and handle_onboarding_seller_image_upload(phone, session, pending_image_id):
                return
        elif text in {"seller_onboard_image_link", "2"}:
            cloud.send_whatsapp_message(phone, "🔗 Send a public image URL for your shop, or type *skip* to continue without one.")
            session["state"] = "onboarding_seller_image_input"
        elif text in {"seller_onboard_image_skip", "3", "skip"}:
            session["data"]["shop_image_url"] = ""
            submit_seller_request(phone, session)
        else:
            cloud.send_whatsapp_message(phone, "❌ Choose Upload Photo, Add Link, or Skip.")

    elif state == "onboarding_seller_image_input":
        session["data"]["shop_image_url"] = "" if text.lower() == "skip" else text
        submit_seller_request(phone, session)

    elif state == "onboarding_seller_image_upload":
        cloud.send_whatsapp_message(phone, "📷 Send the shop image from your device, or use the Skip button.")

def finalize_onboarding(phone, session, seller_code=None):
    data = session["data"]
    create_user(phone, data["name"], data["role"], data["zone"], data.get("landmark"))
    
    if data["role"] == "seller":
        update_user_shop(phone, data.get("shop_name"), data.get("shop_desc"), data.get("shop_image_url"), data.get("landmark"))
    
    msg = (
        f"🎉 *Welcome to ZanChop, {data['name']}!*\n\n"
        f"📍 Zone: {data['zone']}\n"
        f"🎭 Role: {data['role'].capitalize()}"
    )
    if data.get("shop_name"):
        msg += f"\n🏪 Shop: {data['shop_name']}"
    if data.get("landmark"):
        msg += f"\n📌 Landmark: {data['landmark']}"
    if seller_code:
        msg += f"\n🔐 Access Code: {seller_code}"

    if data["role"] == "buyer":
        msg += "\n\nOpening your buyer menu now."
    else:
        msg += "\n\nOpening your seller dashboard now."
    cloud.send_whatsapp_message(phone, msg)
    session["data"] = {}
    refreshed_user = get_user(phone)
    if data["role"] == "buyer" and refreshed_user:
        session["state"] = "buyer_menu"
        show_buyer_home(phone, refreshed_user, session)
    elif refreshed_user:
        session["state"] = "seller_menu"
        show_seller_dashboard(phone, refreshed_user)
    else:
        session["state"] = "idle"

def show_admin_panel(phone):
    rows = [
        {"id": "admin_users", "title": "Users", "description": "See all buyers and sellers"},
        {"id": "admin_seller_requests", "title": "Seller Requests", "description": "Review pending seller applications"},
        {"id": "admin_register_seller", "title": "Register Seller", "description": "Create a seller profile manually"},
        {"id": "admin_prods", "title": "Products", "description": "Review listed dishes"},
        {"id": "admin_orders", "title": "Orders", "description": "Review platform orders"},
        {"id": "admin_stats", "title": "Stats", "description": "View marketplace totals"}
    ]
    success = cloud.send_interactive_list(
        phone,
        "👑 *ZanChop Admin Panel*\n\nManage restaurants, buyers, products, and orders from one place.",
        "Open Menu",
        [{"title": "Admin Actions", "rows": rows}],
        header_text="ZanChop Admin"
    )
    if not success:
        cloud.send_whatsapp_message(
            phone,
            "👑 *ZanChop Admin Panel*\n\nChoose an option:\n1. Users\n2. Seller Requests\n3. Register Seller\n4. Products\n5. Orders\n6. Stats"
        )

def new_admin_seller_form():
    return {
        "seller_phone": "",
        "seller_name": "",
        "shop_name": "",
        "shop_description": "",
        "shop_image_url": "",
        "zone": ""
        ,
        "landmark": "",
        "request_id": None
    }

def show_admin_seller_form(phone, form):
    rows = [
        {"id": "admin_form_phone", "title": "Seller Phone", "description": truncate_text(form["seller_phone"] or "Tap to add seller WhatsApp number", 72)},
        {"id": "admin_form_name", "title": "Seller Name", "description": truncate_text(form["seller_name"] or "Tap to add seller name", 72)},
        {"id": "admin_form_shop", "title": "Shop Name", "description": truncate_text(form["shop_name"] or "Tap to add restaurant name", 72)},
        {"id": "admin_form_desc", "title": "Shop Description", "description": truncate_text(form["shop_description"] or "Tap to add short description", 72)},
        {"id": "admin_form_image", "title": "Shop Image", "description": truncate_text(form["shop_image_url"] or "Tap to add image link or upload", 72)},
        {"id": "admin_form_zone", "title": "Zone", "description": truncate_text(form["zone"] or "Tap to choose seller zone", 72)},
        {"id": "admin_form_landmark", "title": "Landmark", "description": truncate_text(form["landmark"] or "Tap to choose seller landmark", 72)},
        {"id": "admin_form_review", "title": "Review & Approve", "description": "Approve this seller profile"}
    ]
    success = cloud.send_interactive_list(
        phone,
        "🧾 *Register Seller Form*\n\nBuild the seller profile step by step. Tap any field to fill or update it.",
        "Edit Form",
        [{"title": "Seller Setup", "rows": rows}],
        header_text="New Seller"
    )
    if not success:
        summary = (
            "🧾 *Register Seller Form*\n\n"
            f"1. Phone: {form['seller_phone'] or 'Not set'}\n"
            f"2. Name: {form['seller_name'] or 'Not set'}\n"
            f"3. Shop: {form['shop_name'] or 'Not set'}\n"
            f"4. Description: {form['shop_description'] or 'Not set'}\n"
            f"5. Image URL: {form['shop_image_url'] or 'Not set'}\n"
            f"6. Zone: {form['zone'] or 'Not set'}\n"
            f"7. Landmark: {form['landmark'] or 'Not set'}\n"
            "8. Review & Approve"
        )
        cloud.send_whatsapp_message(phone, summary)

def show_admin_seller_requests(phone):
    requests = get_pending_seller_requests()
    if not requests:
        cloud.send_whatsapp_message(phone, "📭 There are no pending seller requests right now.")
        return False

    rows = []
    for request_row in requests:
        rows.append({
            "id": f"admin_request_{request_row[0]}",
            "title": truncate_text(request_row[2] or request_row[1], 24),
            "description": truncate_text(f"{request_row[1]} | {request_row[4] or 'Zone pending'} | {request_row[3] or 'Shop pending'}", 72)
        })
    success = cloud.send_interactive_list(
        phone,
        "📨 *Pending Seller Requests*\n\nTap a request to review and approve the seller.",
        "Review Requests",
        [{"title": "Seller Applications", "rows": rows}],
        header_text="Seller Requests"
    )
    if not success:
        msg = "📨 *Pending Seller Requests*\n\n"
        for index, request_row in enumerate(requests, 1):
            msg += f"{index}. {request_row[2] or request_row[1]} | {request_row[1]} | {request_row[4] or 'Zone pending'}\n"
        msg += f"\nReply with the request number (1-{len(requests)})."
        cloud.send_whatsapp_message(phone, msg)
    return True

def seller_request_to_form(request_row):
    return {
        "seller_phone": request_row[1] or "",
        "seller_name": request_row[2] or "",
        "shop_name": request_row[3] or "",
        "shop_description": request_row[4] or "",
        "shop_image_url": request_row[5] or "",
        "zone": request_row[6] or "",
        "landmark": request_row[7] or "",
        "request_id": request_row[0],
    }

def show_zone_picker(phone, state_label, header_text="Choose Zone"):
    rows = []
    for index, zone in enumerate(UCC_ZONES.keys(), 1):
        preview = ", ".join(DELIVERY_ZONES.get(zone, {}).get("landmarks", [])[:2]) or "Popular campus area"
        rows.append({
            "id": f"{state_label}_{index}",
            "title": zone[:24],
            "description": truncate_text(preview, 72)
        })
    success = cloud.send_interactive_list(
        phone,
        "📍 Tap the zone that matches this seller.",
        "Select Zone",
        [{"title": "Available Zones", "rows": rows}],
        header_text=header_text
    )
    if not success:
        msg = "📍 Choose a zone:\n\n"
        for index, zone in enumerate(UCC_ZONES.keys(), 1):
            preview = ", ".join(DELIVERY_ZONES.get(zone, {}).get("landmarks", [])[:2])
            msg += f"{index}. {zone}"
            if preview:
                msg += f" - {preview}"
            msg += "\n"
        cloud.send_whatsapp_message(phone, msg)

def show_landmark_picker(phone, zone, state_label, header_text="Choose Landmark"):
    landmarks = get_landmarks_for_zone(zone)
    rows = []
    for index, landmark in enumerate(landmarks, 1):
        rows.append({
            "id": f"{state_label}_{index}",
            "title": landmark[:24],
            "description": f"Suggested under {zone}"
        })
    success = cloud.send_interactive_list(
        phone,
        f"📌 Tap the landmark that best describes the location in *{zone}*.",
        "Select Landmark",
        [{"title": "Suggested Landmarks", "rows": rows}],
        header_text=header_text
    )
    if not success:
        msg = f"📌 Choose a landmark in {zone}:\n\n"
        for index, landmark in enumerate(landmarks, 1):
            msg += f"{index}. {landmark}\n"
        cloud.send_whatsapp_message(phone, msg)

def show_buyer_zone_picker(phone, seller_zone, seller_landmark):
    rows = []
    for index, (zone, meta) in enumerate(DELIVERY_ZONES.items(), 1):
        preview_landmarks = ", ".join(meta["landmarks"][:2])
        rows.append({
            "id": f"buyer_zone_{index}",
            "title": zone[:24],
            "description": f"{preview_landmarks}..."
        })
    success = cloud.send_interactive_list(
        phone,
        f"📍 *Choose Delivery Zone*\n\nSeller area: {seller_zone or 'Not set'}\nSeller landmark: {seller_landmark or 'Not set'}\n\nTap the zone closest to your delivery point.",
        "Select Zone",
        [{"title": "Cape Coast Delivery Zones", "rows": rows}],
        header_text="Delivery Zone"
    )
    if not success:
        msg = "🚚 Choose your delivery zone:\n\n"
        for index, (zone, _) in enumerate(DELIVERY_ZONES.items(), 1):
            msg += f"{index}. {zone}\n"
        cloud.send_whatsapp_message(phone, msg)

def show_buyer_landmark_picker(phone, zone):
    show_landmark_picker(phone, zone, "buyer_landmark", header_text="Delivery Landmark")

def show_admin_seller_review(phone, form):
    is_request = bool(form.get("request_id"))
    msg = (
        "🧾 *Review Seller Profile*\n\n"
        f"Seller Phone: {form['seller_phone']}\n"
        f"Seller Name: {form['seller_name']}\n"
        f"Shop Name: {form['shop_name']}\n"
        f"Description: {form['shop_description']}\n"
        f"Image URL: {form['shop_image_url'] or 'Not set'}\n"
        f"Zone: {form['zone']}\n\n"
        f"Landmark: {form['landmark']}\n\n"
        "Everything looks ready. Approve this seller account?"
    )
    buttons = [
        {"id": "admin_approve_seller_request" if is_request else "admin_activate_seller_profile", "title": "✅ Approve Seller"},
        {"id": "admin_edit_seller_form", "title": "✏️ Edit Form"},
        {"id": "admin_cancel_seller_form", "title": "❌ Cancel"}
    ]
    cloud.send_interactive_buttons(phone, msg, buttons, header_text="Review Seller")

def show_seller_dashboard(phone, user):
    rows = [
        {"id": "seller_add", "title": "Add Food", "description": "Create a new menu item"},
        {"id": "seller_products", "title": "Manage Products", "description": "Edit or delete menu items"},
        {"id": "seller_orders", "title": "Manage Orders", "description": "Track accepted, preparing, and delivered orders"}
    ]
    success = cloud.send_interactive_list(
        phone,
        f"Hello {user[USER_NAME]}! 👋\n\nWelcome to *{user[USER_SHOP_NAME] or 'your shop'}*.\nChoose what you want to manage.",
        "Open Dashboard",
        [{"title": "Seller Actions", "rows": rows}],
        header_text="Seller Dashboard"
    )
    if not success:
        cloud.send_whatsapp_message(
            phone,
            "📊 *Seller Dashboard*\n\n1. Add Food\n2. Manage Products\n3. Manage Orders"
        )

def show_seller_products_menu(phone, seller_phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT id, name, price, description
        FROM products
        WHERE seller_phone = ?
        ORDER BY id DESC
        LIMIT 10
    """, (normalize_phone(seller_phone),))
    products = c.fetchall()
    conn.close()

    if not products:
        cloud.send_whatsapp_message(phone, "📭 Your menu is empty. Tap Add Food to create your first item.")
        return False

    rows = []
    for product in products:
        rows.append({
            "id": f"seller_prod_{product[0]}",
            "title": product[1][:24],
            "description": f"GHS {product[2]:.2f} | {(product[3] or 'No description')[:32]}"
        })
    success = cloud.send_interactive_list(
        phone,
        "📋 *Manage Products*\n\nTap a product to edit or delete it.",
        "Select Product",
        [{"title": "Your Products", "rows": rows}],
        header_text="Product Manager"
    )
    if not success:
        msg = "📋 *Manage Products*\n\nReply with a number:\n"
        for index, product in enumerate(products, 1):
            msg += f"{index}. *{product[1]}* | GHS {product[2]:.2f}\n"
        cloud.send_whatsapp_message(phone, msg)
    return True

def show_seller_product_actions(phone, product):
    rows = [
        {"id": "seller_edit_name", "title": "Edit Name", "description": f"Current: {product[2][:45]}"},
        {"id": "seller_edit_desc", "title": "Edit Description", "description": (product[3] or "No description")[:55]},
        {"id": "seller_edit_price", "title": "Edit Price", "description": f"Current: GHS {product[4]:.2f}"},
        {"id": "seller_edit_stock", "title": "Edit Stock", "description": f"Available: {product[5]}"},
        {"id": "seller_edit_image", "title": "Edit Image", "description": (product[6] or "No image set")[:55]},
        {"id": "seller_delete_product", "title": "Delete Product", "description": "Remove this product from your menu"},
        {"id": "seller_back_products", "title": "Back to Products", "description": "Return to your product list"}
    ]
    success = cloud.send_interactive_list(
        phone,
        f"🧾 *{product[2]}*\n\nPrice: GHS {product[4]:.2f}\nStock: {product[5]}\nDescription: {product[3] or 'No description'}\nImage: {product[6] or 'Not set'}",
        "Choose Action",
        [{"title": "Product Actions", "rows": rows}],
        header_text="Edit Product"
    )
    if not success:
        msg = (
            f"🧾 *{product[2]}*\n\n"
            "1. Edit Name\n"
            "2. Edit Description\n"
            "3. Edit Price\n"
            "4. Edit Stock\n"
            "5. Edit Image\n"
            "6. Delete Product\n"
            "7. Back to Products"
        )
        cloud.send_whatsapp_message(phone, msg)

def show_seller_orders_menu(phone, seller_phone):
    orders = get_seller_orders(seller_phone)
    if not orders:
        cloud.send_whatsapp_message(phone, "📭 No orders yet.")
        return False

    rows = []
    for order in orders:
        rows.append({
            "id": f"seller_order_{order[0]}",
            "title": f"Order #{order[0]}",
            "description": truncate_text(f"{order[1]} | GHS {order[2]:.2f} | {format_order_status(order[3])} | {order[6] or order[5] or order[4]}", 72)
        })
    success = cloud.send_interactive_list(
        phone,
        "📦 *Manage Orders*\n\nTap an order to update its status.",
        "Select Order",
        [{"title": "Recent Orders", "rows": rows}],
        header_text="Order Manager"
    )
    if not success:
        msg = "📦 *Manage Orders*\n\nReply with an order number:\n"
        for index, order in enumerate(orders, 1):
            msg += f"{index}. Order #{order[0]} | GHS {order[2]:.2f} | {format_order_status(order[3])}\n"
        cloud.send_whatsapp_message(phone, msg)
    return True

def show_seller_order_actions(phone, order):
    order_id, buyer_phone, _, total_price, delivery_fee, delivery_zone, delivery_landmark, delivery_address, pickup_or_delivery, status, _, confirmation_code, created_at = order
    buttons = [{"id": "seller_orders_back", "title": "⬅️ Back"}]
    next_action = None
    if status in {"paid", "pending"}:
        next_action = {"id": "seller_accept_order", "title": "✅ Accept"}
    elif status == "accepted":
        next_action = {"id": "seller_mark_preparing", "title": "🍳 Preparing"}
    elif status == "preparing":
        next_action = {"id": "seller_mark_dispatch", "title": "🛵 On The Way"}
    if next_action:
        buttons.insert(0, next_action)
    if status not in {"delivered", "completed", "cancelled"}:
        buttons.insert(1 if next_action else 0, {"id": "seller_cancel_order", "title": "❌ Cancel"})

    summary = (
        f"📦 *Order #{order_id}*\n\n"
        f"Buyer: {buyer_phone}\n"
        f"Status: {format_order_status(status)}\n"
        f"Method: {pickup_or_delivery}\n"
        f"Total: GHS {total_price:.2f}\n"
        "Delivery: Free for now\n"
        f"Zone: {delivery_zone or 'N/A'}\n"
        f"Landmark: {delivery_landmark or 'N/A'}\n"
        f"Address: {delivery_address or 'N/A'}\n"
        f"Code: {confirmation_code or 'Pending payment'}"
    )
    success = cloud.send_interactive_buttons(phone, summary, buttons[:3], header_text="Order Actions")
    if not success:
        if status in {"paid", "pending"}:
            msg = summary + "\n\n1. Accept Order\n2. Cancel Order\n3. Back"
        elif status == "accepted":
            msg = summary + "\n\n1. Mark Preparing\n2. Cancel Order\n3. Back"
        elif status == "preparing":
            msg = summary + "\n\n1. Mark On The Way\n2. Cancel Order\n3. Back"
        elif status == "on_the_way":
            msg = summary + "\n\n1. Cancel Order\n2. Back"
        else:
            msg = summary + "\n\n1. Back"
        cloud.send_whatsapp_message(phone, msg)

def update_user_shop(phone, shop_name=None, shop_desc=None, shop_image_url=None, landmark=None):
    normalized_phone = normalize_phone(phone)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    updates = []
    values = []
    if shop_name is not None:
        updates.append("shop_name = ?")
        values.append(shop_name)
    if shop_desc is not None:
        updates.append("shop_description = ?")
        values.append(shop_desc)
    if shop_image_url is not None:
        updates.append("shop_image_url = ?")
        values.append(shop_image_url)
    if landmark is not None:
        updates.append("landmark = ?")
        values.append(landmark)
    if updates:
        values.append(normalized_phone)
        c.execute(f"UPDATE users SET {', '.join(updates)} WHERE phone = ?", values)
    conn.commit()
    conn.close()
    if updates:
        invalidate_market_cache(phone)

def clear_pending_media(session):
    session.pop("pending_image_id", None)
    session.pop("pending_image_url", None)
    session.pop("pending_image", None)

def handle_seller_image_upload(phone, session, media_id):
    state = session.get("state")
    image_url = save_incoming_whatsapp_image(media_id)
    if not image_url:
        cloud.send_whatsapp_message(phone, "❌ I couldn't process that image. Please try another photo or use an image link.")
        return True

    if state == "seller_add_image_upload":
        add_product_db(
            phone,
            session["data"]["p_name"],
            session["data"].get("p_desc", ""),
            session["data"]["p_price"],
            session["data"].get("p_stock", 1),
            image_url
        )
        clear_pending_media(session)
        cloud.send_whatsapp_message(phone, f"✅ *{session['data']['p_name']}* added to your menu with your uploaded image.")
        session["state"] = "idle"
        session["data"] = {}
        return True

    if state == "seller_edit_image_upload":
        pid = session.get("data", {}).get("selected_product_id")
        if not pid:
            clear_pending_media(session)
            cloud.send_whatsapp_message(phone, "❌ Product context was lost. Open the product again and try uploading the image.")
            session["state"] = "seller_menu"
            return True

        update_product_details(pid, phone, image_url=image_url)
        clear_pending_media(session)
        product = get_seller_product(pid, phone)
        cloud.send_whatsapp_message(phone, "✅ Product image updated from your device upload.")
        if product:
            show_seller_product_actions(phone, product)
            session["state"] = "seller_product_actions"
        else:
            session["state"] = "seller_menu"
        return True

    return False

def handle_admin_seller_image_upload(phone, session, media_id):
    if session.get("state") != "admin_seller_shop_image_upload":
        return False

    image_url = save_incoming_whatsapp_image(media_id)
    if not image_url:
        cloud.send_whatsapp_message(phone, "❌ I couldn't process that image. Please try another photo or use a public image link.")
        return True

    session.setdefault("data", {}).setdefault("seller_form", new_admin_seller_form())
    session["data"]["seller_form"]["shop_image_url"] = image_url
    clear_pending_media(session)
    cloud.send_whatsapp_message(phone, "✅ Shop image uploaded from device.")
    show_admin_seller_form(phone, session["data"]["seller_form"])
    session["state"] = "admin_seller_form"
    return True

def handle_onboarding_seller_image_upload(phone, session, media_id):
    if session.get("state") != "onboarding_seller_image_upload":
        return False

    image_url = save_incoming_whatsapp_image(media_id)
    if not image_url:
        cloud.send_whatsapp_message(phone, "❌ I couldn't process that image. Please try another photo or choose Add Link.")
        return True

    session.setdefault("data", {})["shop_image_url"] = image_url
    clear_pending_media(session)
    submit_seller_request(phone, session)
    return True

# =========================
# SELLER FLOW
# =========================
def handle_seller_flow(phone, text, session, user):
    state = session.get("state", "idle")
    text_lower = text.lower().strip()
    session.setdefault("data", {})

    if text_lower == "add":
        cloud.send_whatsapp_message(phone, "🍔 *New Menu Item*\n\nEnter the food name.\nExample: Jollof Rice with Chicken")
        session["state"] = "seller_add_name"
        return

    if text_lower in {"menu", "home"} or state in {"idle", "start"}:
        show_seller_dashboard(phone, user)
        session["state"] = "seller_menu"
        return

    if state == "seller_menu":
        if text in {"seller_add", "add"}:
            cloud.send_whatsapp_message(phone, "🍔 *New Menu Item*\n\nEnter the food name.\nExample: Jollof Rice with Chicken")
            session["state"] = "seller_add_name"
        elif text in {"seller_products", "seller_menu"}:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""
                SELECT id
                FROM products
                WHERE seller_phone = ?
                ORDER BY id DESC
                LIMIT 10
            """, (normalize_phone(phone),))
            set_reply_map(session, "seller_products_map", [row[0] for row in c.fetchall()])
            conn.close()
            if show_seller_products_menu(phone, phone):
                session["state"] = "seller_products_list"
            else:
                session["state"] = "seller_menu"
        elif text == "seller_orders":
            orders = get_seller_orders(phone)
            set_reply_map(session, "seller_orders_map", [order[0] for order in orders])
            if show_seller_orders_menu(phone, phone):
                session["state"] = "seller_orders_list"
            else:
                session["state"] = "seller_menu"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please tap one of the seller actions.")

    elif state == "seller_add_name":
        session["data"]["p_name"] = text
        cloud.send_whatsapp_message(phone, "📝 Enter a short product description.\nExample: Smoky jollof with grilled chicken\n\nType *skip* if you want to leave it blank.")
        session["state"] = "seller_add_desc"

    elif state == "seller_add_desc":
        session["data"]["p_desc"] = "" if text_lower == "skip" else text
        cloud.send_whatsapp_message(phone, f"💵 Enter the price for *{session['data']['p_name']}*.\nExample: 25")
        session["state"] = "seller_add_price"

    elif state == "seller_add_price":
        try:
            price = float(text)
            if price <= 0:
                raise ValueError
            session["data"]["p_price"] = price
            cloud.send_whatsapp_message(phone, "📦 Enter how many portions are available right now.\nExample: 12")
            session["state"] = "seller_add_stock"
            return
            buttons = [
                {"id": "seller_add_image_url", "title": "🔗 Add Image"},
                {"id": "seller_skip_image", "title": "➡️ Skip Image"},
                {"id": "seller_cancel_add", "title": "❌ Cancel"}
            ]
            success = cloud.send_interactive_buttons(
                phone,
                f"🖼️ *Image Setup*\n\nName: {session['data']['p_name']}\nPrice: GHS {price:.2f}\n\nWould you like to attach an image URL?",
                buttons,
                header_text="Product Image"
            )
            session["state"] = "seller_add_image_choice"
        except ValueError:
            send_text(phone, "Invalid price. Please enter a valid number greater than 0.")

    elif state == "seller_add_stock":
        try:
            stock = int(text)
            if stock < 1:
                raise ValueError
            session["data"]["p_stock"] = stock
            buttons = [
                {"id": "seller_add_image_device", "title": "📷 Upload Photo"},
                {"id": "seller_add_image_url", "title": "🔗 Add Link"},
                {"id": "seller_skip_image", "title": "➡️ Skip Image"}
            ]
            success = cloud.send_interactive_buttons(
                phone,
                f"🖼️ *Image Setup*\n\nName: {session['data']['p_name']}\nPrice: GHS {session['data']['p_price']:.2f}\nStock: {stock}\n\nChoose how to add the menu photo. You can also type *cancel* to stop.",
                buttons,
                header_text="Product Image"
            )
            if not success:
                cloud.send_whatsapp_message(phone, "1. Upload Photo from device\n2. Add Image Link\n3. Skip Image\n\nType *cancel* to stop.")
            session["state"] = "seller_add_image_choice"
        except ValueError:
            send_text(phone, "Invalid stock. Please enter a whole number greater than 0.")

    elif state == "seller_add_image_choice":
        if text in {"seller_add_image_device", "1"}:
            cloud.send_whatsapp_message(phone, "📷 Send the menu photo from your device now.\n\nYou can also type *skip* to continue without an image or *cancel* to stop.")
            session["state"] = "seller_add_image_upload"
            pending_image_id = session.get("pending_image_id")
            if pending_image_id and handle_seller_image_upload(phone, session, pending_image_id):
                return
        elif text in {"seller_add_image_url", "2"}:
            cloud.send_whatsapp_message(phone, "🔗 Send the public image URL.\nExample: https://example.com/jollof.jpg")
            session["state"] = "seller_add_image_url"
        elif text in {"seller_skip_image", "3", "skip"}:
            add_product_db(phone, session["data"]["p_name"], session["data"].get("p_desc", ""), session["data"]["p_price"], session["data"].get("p_stock", 1), "")
            send_text(phone, f"✅ *{session['data']['p_name']}* added to your menu.")
            session["state"] = "idle"
            session["data"] = {}
        elif text in {"seller_cancel_add", "cancel"}:
            send_text(phone, "❌ Product creation cancelled.")
            session["state"] = "idle"
            session["data"] = {}
        else:
            cloud.send_whatsapp_message(phone, "❌ Choose Upload Photo, Add Link, Skip Image, or type cancel.")

    elif state == "seller_add_image_upload":
        if text_lower == "skip":
            add_product_db(phone, session["data"]["p_name"], session["data"].get("p_desc", ""), session["data"]["p_price"], session["data"].get("p_stock", 1), "")
            clear_pending_media(session)
            send_text(phone, f"✅ *{session['data']['p_name']}* added to your menu.")
            session["state"] = "idle"
            session["data"] = {}
        elif text_lower == "cancel":
            clear_pending_media(session)
            send_text(phone, "❌ Product creation cancelled.")
            session["state"] = "idle"
            session["data"] = {}
        else:
            cloud.send_whatsapp_message(phone, "📷 Send the image from your device, or type *skip* or *cancel*.")

    elif state == "seller_add_image_url":
        add_product_db(phone, session["data"]["p_name"], session["data"].get("p_desc", ""), session["data"]["p_price"], session["data"].get("p_stock", 1), text)
        send_text(phone, f"✅ *{session['data']['p_name']}* added to your menu with an image.")
        session["state"] = "idle"
        session["data"] = {}

    elif state == "seller_products_list":
        pid = None
        if text.startswith("seller_prod_"):
            pid = int(text.split("_")[-1])
        else:
            pid = get_reply_map_value(session, "seller_products_map", text)
        if pid:
            product = get_seller_product(pid, phone)
            if not product:
                cloud.send_whatsapp_message(phone, "❌ Product not found.")
                return
            session["data"]["selected_product_id"] = pid
            show_seller_product_actions(phone, product)
            session["state"] = "seller_product_actions"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please tap a product from the list.")

    elif state == "seller_product_actions":
        pid = session["data"].get("selected_product_id")
        product = get_seller_product(pid, phone) if pid else None
        if not product:
            cloud.send_whatsapp_message(phone, "❌ Product not found.")
            session["state"] = "seller_menu"
            return

        normalized_choice = {
            "1": "seller_edit_name",
            "2": "seller_edit_desc",
            "3": "seller_edit_price",
            "4": "seller_edit_stock",
            "5": "seller_edit_image",
            "6": "seller_delete_product",
            "7": "seller_back_products",
        }.get(text, text)

        if normalized_choice == "seller_edit_name":
            cloud.send_whatsapp_message(phone, f"✏️ Enter the new product name.\nCurrent: {product[2]}")
            session["state"] = "seller_edit_name"
        elif normalized_choice == "seller_edit_desc":
            cloud.send_whatsapp_message(phone, f"📝 Enter the new description.\nCurrent: {product[3] or 'No description'}\n\nType *skip* to clear it.")
            session["state"] = "seller_edit_desc"
        elif normalized_choice == "seller_edit_price":
            cloud.send_whatsapp_message(phone, f"💵 Enter the new price.\nCurrent: GHS {product[4]:.2f}")
            session["state"] = "seller_edit_price"
        elif normalized_choice == "seller_edit_stock":
            cloud.send_whatsapp_message(phone, f"📦 Enter the new stock quantity.\nCurrent: {product[5]}")
            session["state"] = "seller_edit_stock"
        elif normalized_choice == "seller_edit_image":
            buttons = [
                {"id": "seller_edit_image_device", "title": "📷 Upload Photo"},
                {"id": "seller_edit_image_url", "title": "🔗 Add Link"},
                {"id": "seller_edit_image_remove", "title": "🗑️ Remove"}
            ]
            success = cloud.send_interactive_buttons(
                phone,
                f"🖼️ Update the image for *{product[2]}*.\nCurrent: {product[6] or 'Not set'}\n\nChoose device upload, image link, or remove the current image.",
                buttons,
                header_text="Update Product Image"
            )
            if not success:
                cloud.send_whatsapp_message(phone, "1. Upload Photo from device\n2. Add Image Link\n3. Remove Image\n\nType *back* to return.")
            session["state"] = "seller_edit_image_choice"
        elif normalized_choice == "seller_delete_product":
            buttons = [
                {"id": "seller_confirm_delete", "title": "🗑️ Delete"},
                {"id": "seller_cancel_delete", "title": "⬅️ Keep Item"}
            ]
            success = cloud.send_interactive_buttons(
                phone,
                f"Delete *{product[2]}* from your menu?",
                buttons,
                header_text="Confirm Delete"
            )
            session["state"] = "seller_delete_confirm"
        elif normalized_choice == "seller_back_products":
            if show_seller_products_menu(phone, phone):
                session["state"] = "seller_products_list"
            else:
                session["state"] = "seller_menu"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please tap one of the product actions.")

    elif state == "seller_edit_name":
        update_product_details(session["data"]["selected_product_id"], phone, name=text)
        product = get_seller_product(session["data"]["selected_product_id"], phone)
        send_text(phone, "✅ Product name updated.")
        show_seller_product_actions(phone, product)
        session["state"] = "seller_product_actions"

    elif state == "seller_edit_desc":
        update_product_details(session["data"]["selected_product_id"], phone, description="" if text_lower == "skip" else text)
        product = get_seller_product(session["data"]["selected_product_id"], phone)
        send_text(phone, "✅ Product description updated.")
        show_seller_product_actions(phone, product)
        session["state"] = "seller_product_actions"

    elif state == "seller_edit_price":
        try:
            price = float(text)
            if price <= 0:
                raise ValueError
            update_product_details(session["data"]["selected_product_id"], phone, price=price)
            product = get_seller_product(session["data"]["selected_product_id"], phone)
            send_text(phone, "✅ Product price updated.")
            show_seller_product_actions(phone, product)
            session["state"] = "seller_product_actions"
        except ValueError:
            send_text(phone, "❌ Invalid price. Enter a valid number greater than 0.")

    elif state == "seller_edit_stock":
        try:
            stock = int(text)
            if stock < 0:
                raise ValueError
            update_product_details(session["data"]["selected_product_id"], phone, stock=stock)
            product = get_seller_product(session["data"]["selected_product_id"], phone)
            send_text(phone, "✅ Product stock updated.")
            show_seller_product_actions(phone, product)
            session["state"] = "seller_product_actions"
        except ValueError:
            send_text(phone, "❌ Invalid stock. Enter a whole number 0 or greater.")

    elif state == "seller_edit_image_choice":
        if text in {"seller_edit_image_device", "1"}:
            cloud.send_whatsapp_message(phone, "📷 Send the new image from your device now.\n\nType *back* to return without changing it.")
            session["state"] = "seller_edit_image_upload"
            pending_image_id = session.get("pending_image_id")
            if pending_image_id and handle_seller_image_upload(phone, session, pending_image_id):
                return
        elif text in {"seller_edit_image_url", "2"}:
            cloud.send_whatsapp_message(phone, "🔗 Enter the new image URL.\nExample: https://example.com/jollof.jpg\n\nType *back* to return.")
            session["state"] = "seller_edit_image"
        elif text in {"seller_edit_image_remove", "3", "skip"}:
            update_product_details(session["data"]["selected_product_id"], phone, image_url="")
            product = get_seller_product(session["data"]["selected_product_id"], phone)
            send_text(phone, "✅ Product image removed.")
            show_seller_product_actions(phone, product)
            session["state"] = "seller_product_actions"
        elif text_lower == "back":
            product = get_seller_product(session["data"]["selected_product_id"], phone)
            show_seller_product_actions(phone, product)
            session["state"] = "seller_product_actions"
        else:
            cloud.send_whatsapp_message(phone, "❌ Choose Upload Photo, Add Link, Remove Image, or type back.")

    elif state == "seller_edit_image_upload":
        if text_lower == "back":
            product = get_seller_product(session["data"]["selected_product_id"], phone)
            clear_pending_media(session)
            show_seller_product_actions(phone, product)
            session["state"] = "seller_product_actions"
        else:
            cloud.send_whatsapp_message(phone, "📷 Send the image from your device, or type *back*.")

    elif state == "seller_edit_image":
        if text_lower == "back":
            product = get_seller_product(session["data"]["selected_product_id"], phone)
            show_seller_product_actions(phone, product)
            session["state"] = "seller_product_actions"
            return
        update_product_details(session["data"]["selected_product_id"], phone, image_url="" if text_lower == "skip" else text)
        product = get_seller_product(session["data"]["selected_product_id"], phone)
        send_text(phone, "✅ Product image updated.")
        show_seller_product_actions(phone, product)
        session["state"] = "seller_product_actions"

    elif state == "seller_delete_confirm":
        if text == "seller_confirm_delete":
            deleted = delete_product(session["data"]["selected_product_id"], phone)
            session["data"].pop("selected_product_id", None)
            if deleted:
                send_text(phone, "✅ Product deleted from your menu.")
            else:
                send_text(phone, "❌ Product could not be deleted.")
            if show_seller_products_menu(phone, phone):
                session["state"] = "seller_products_list"
            else:
                session["state"] = "seller_menu"
        elif text == "seller_cancel_delete":
            product = get_seller_product(session["data"]["selected_product_id"], phone)
            show_seller_product_actions(phone, product)
            session["state"] = "seller_product_actions"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please tap Delete or Keep Item.")

    elif state == "seller_orders_list":
        order_id = None
        if text.startswith("seller_order_"):
            order_id = int(text.split("_")[-1])
        else:
            order_id = get_reply_map_value(session, "seller_orders_map", text)
        if order_id:
            order = get_seller_order(order_id, phone)
            if not order:
                cloud.send_whatsapp_message(phone, "❌ Order not found.")
                return
            session["data"]["selected_order_id"] = order_id
            show_seller_order_actions(phone, order)
            session["state"] = "seller_order_actions"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please tap an order from the list.")

    elif state == "seller_order_actions":
        order_id = session["data"].get("selected_order_id")
        order = get_seller_order(order_id, phone) if order_id else None
        if not order:
            cloud.send_whatsapp_message(phone, "❌ Order not found.")
            session["state"] = "seller_menu"
            return

        normalized_choice = text
        if text == "1":
            if order[9] in {"paid", "pending"}:
                normalized_choice = "seller_accept_order"
            elif order[9] == "accepted":
                normalized_choice = "seller_mark_preparing"
            elif order[9] == "preparing":
                normalized_choice = "seller_mark_dispatch"
            elif order[9] == "on_the_way":
                normalized_choice = "seller_cancel_order"
            else:
                normalized_choice = "seller_orders_back"
        elif text == "2":
            if order[9] in {"paid", "pending", "accepted", "preparing", "on_the_way", "awaiting_payment"}:
                normalized_choice = "seller_cancel_order"
            else:
                normalized_choice = "seller_orders_back"
        elif text == "3":
            normalized_choice = "seller_orders_back"

        if normalized_choice == "seller_accept_order":
            if order[9] == "awaiting_payment":
                cloud.send_whatsapp_message(phone, "⏳ This order is still waiting for payment. Accept it after payment is confirmed.")
                return
            update_order_status(order_id, "accepted")
            buyer_msg = f"✅ *Order #{order_id} Accepted*\n\nThe restaurant has accepted your order and will begin shortly."
            cloud.send_whatsapp_message(order[1], buyer_msg)
            send_text(phone, f"✅ Order #{order_id} marked as accepted.")
            show_seller_order_actions(phone, get_seller_order(order_id, phone))
        elif normalized_choice == "seller_mark_preparing":
            update_order_status(order_id, "preparing")
            buyer_msg = f"🍳 *Order #{order_id} Preparing*\n\nYour food is now being prepared."
            cloud.send_whatsapp_message(order[1], buyer_msg)
            send_text(phone, f"✅ Order #{order_id} marked as preparing.")
            show_seller_order_actions(phone, get_seller_order(order_id, phone))
        elif normalized_choice == "seller_mark_dispatch":
            update_order_status(order_id, "on_the_way")
            code = order[11] or "N/A"
            buyer_msg = (
                f"🛵 *Order #{order_id} On The Way*\n\n"
                "Your food is on the way. When it arrives, open My Orders and confirm with your OTP.\n"
                f"OTP: *{code}*"
            )
            cloud.send_whatsapp_message(order[1], buyer_msg)
            send_text(phone, f"✅ Order #{order_id} marked as on the way.")
            show_seller_order_actions(phone, get_seller_order(order_id, phone))
        elif normalized_choice == "seller_cancel_order":
            update_order_status(order_id, "cancelled")
            cloud.send_whatsapp_message(order[1], f"❌ *Order #{order_id} Cancelled*\n\nThe seller marked this order as cancelled. Please contact support if needed.")
            send_text(phone, f"✅ Order #{order_id} marked as cancelled.")
            show_seller_order_actions(phone, get_seller_order(order_id, phone))
        elif normalized_choice == "seller_orders_back":
            if show_seller_orders_menu(phone, phone):
                session["state"] = "seller_orders_list"
            else:
                session["state"] = "seller_menu"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please tap one of the order actions.")

def list_seller_products(phone):
    if not show_seller_products_menu(phone, phone):
        send_text(phone, "Your menu is empty.")

def list_seller_orders(phone):
    if not show_seller_orders_menu(phone, phone):
        send_text(phone, "No orders yet.")

# =========================
# BUYER FLOW
# =========================
def handle_buyer_flow(phone, text, session, user):
    state = session.get("state", "idle")
    text_lower = text.lower()

    # Handle start state or menu command - show main menu with buttons
    # Also show menu when user types "menu" from any state
    if text_lower == "hi" or text_lower == "menu" or text_lower == "home" or state in ["idle", "start"]:
        buttons = [
            {"id": "browse", "title": "🍔 Browse Shops"},
            {"id": "orders", "title": "📦 My Orders"},
            {"id": "profile", "title": "👤 My Profile"}
        ]
        success = cloud.send_interactive_buttons(
            phone,
            f"Hello {user[USER_NAME]}! 👋\n\nWelcome to *ZanChop UCC*.\nFresh campus meals, simple pickup or delivery, and secure Paystack checkout.\n\nWhat would you like to do?",
            buttons,
            header_text="ZanChop | Main Menu"
        )
        if not success:
            # Fallback to text if buttons fail
            cloud.send_whatsapp_message(
                phone,
                f"👋 Hello {user[USER_NAME]}!\n\nWelcome to *ZanChop UCC*\nFresh campus meals, simple pickup or delivery, and secure Paystack checkout.\n\nChoose an option:\n1. 🍔 Browse Shops\n2. 📦 My Orders\n3. 👤 My Profile\n\n*Reply with 1, 2, or 3*"
            )
        session["state"] = "buyer_menu"
        return

    if state == "buyer_menu":
        if text == "browse" or text == "1":
            shops = fetch_available_shops()
            set_reply_map(session, "buyer_shops_map", [shop[0] for shop in shops])
            show_shops_list(phone)
            session["state"] = "buyer_choosing_shop"
        elif text == "orders" or text == "2":
            list_buyer_orders(phone)
            session["state"] = "idle"
        elif text == "profile" or text == "3":
            show_buyer_profile(phone, user)
            session["state"] = "buyer_profile"
        else:
            cloud.send_whatsapp_message(phone, "❌ Invalid option. Please tap a button.")

    elif state == "buyer_profile":
        if text in {"profile_change_zone", "1"}:
            show_zone_picker(phone, "profile_zone", header_text="Change Zone")
            session["state"] = "buyer_profile_zone"
        elif text in {"profile_back", "2", "menu"}:
            session["state"] = "idle"
            handle_buyer_flow(phone, "menu", session, get_user(phone))
        else:
            cloud.send_whatsapp_message(phone, "❌ Please choose Change Zone or Back.")

    elif state == "buyer_profile_zone":
        zone_name = resolve_zone_choice(text.replace("profile_zone_", "zone_"))
        if not zone_name:
            cloud.send_whatsapp_message(phone, "❌ Zone not found. Please tap a valid zone.")
            return
        update_user(phone, zone=zone_name)
        refreshed_user = get_user(phone)
        cloud.send_whatsapp_message(phone, f"✅ Your zone has been updated to *{zone_name}*.")
        show_buyer_profile(phone, refreshed_user)
        session["state"] = "buyer_profile"

    elif state == "buyer_choosing_shop":
        # Handle shop selection - try phone number or numeric index
        seller_phone = get_reply_map_value(session, "buyer_shops_map", text) or text
        
        # Try as numeric index first
        try:
            idx = int(text) - 1
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""
                SELECT u.phone, u.shop_name
                FROM users u
                WHERE u.role = 'seller'
                  AND u.shop_name IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM products p
                      WHERE p.seller_phone = u.phone AND p.stock > 0
                  )
                ORDER BY u.shop_name
            """)
            shops = c.fetchall()
            conn.close()
            if 0 <= idx < len(shops):
                seller_phone = shops[idx][0]
        except ValueError:
            pass
        
        seller = get_user(seller_phone)
        if seller and seller[USER_ROLE] == 'seller':
            session["data"]["selected_shop"] = seller_phone
            products, _ = fetch_shop_catalog(seller_phone)
            set_reply_map(session, "buyer_products_map", [product[0] for product in products])
            show_catalog_buyer(phone, seller_phone)
            session["state"] = "buyer_browsing"
        else:
            cloud.send_whatsapp_message(phone, "❌ Shop not found. Please try again or type 'menu'.")
            
    elif state == "buyer_browsing":
        try:
            # Handle list ID like "prod_5" or numeric input
            pid_str = str(get_reply_map_value(session, "buyer_products_map", text) or text).replace("prod_", "")
            pid = int(pid_str)
            prod = get_product_by_id(pid)
            if prod:
                session["data"]["selected_prod"] = pid
                session["data"]["seller_phone"] = prod[0]
                if prod[5]:
                    cloud.send_whatsapp_image(
                        phone,
                        prod[5],
                        caption=f"{prod[2]}\nGHS {prod[3]:.2f} each"
                    )
                cloud.send_whatsapp_message(phone, f"How many *{prod[2]}* would you like to order?\n*Price: GHS {prod[3]:.2f} each*\nAvailable now: {prod[4]}")
                session["state"] = "buyer_order_qty"
            else:
                cloud.send_whatsapp_message(phone, "Product not found. Enter another ID or 'menu'.")
        except ValueError:
            cloud.send_whatsapp_message(phone, "Please select a product from the menu.")

    elif state == "buyer_order_qty":
        try:
            qty = int(text)
            if qty < 1:
                raise ValueError
            pid = session["data"]["selected_prod"]
            prod = get_product_by_id(pid)
            if not prod:
                cloud.send_whatsapp_message(phone, "❌ That item is no longer available. Type 'menu' to continue.")
                session["state"] = "idle"
                session["data"] = {}
                return
            if qty > prod[4]:
                cloud.send_whatsapp_message(phone, f"❌ Only {prod[4]} portion(s) of *{prod[2]}* are available right now. Please enter a smaller quantity.")
                return
            
            food_total = prod[3] * qty
            buyer_zone = user[USER_ZONE]
            seller_user = get_user(prod[0])
            seller_zone = seller_user[USER_ZONE] if seller_user else ""
            seller_landmark = seller_user[USER_LANDMARK] if seller_user else ""
            
            session["data"]["qty"] = qty
            session["data"]["food_total"] = food_total
            session["data"]["buyer_zone"] = buyer_zone
            session["data"]["seller_zone"] = seller_zone
            session["data"]["seller_landmark"] = seller_landmark
            session["data"]["delivery_fee"] = 0
            session["data"]["delivery_zone"] = ""
            session["data"]["delivery_landmark"] = ""
            session["data"]["delivery_address"] = ""

            buttons = [
                {"id": "fulfillment_delivery", "title": "🚚 Delivery"},
                {"id": "fulfillment_pickup", "title": "🏪 Pickup"},
                {"id": "cancel_order", "title": "❌ Cancel"}
            ]
            success = cloud.send_interactive_buttons(
                phone,
                f"Nice choice.\n\nItem total: GHS {food_total:.2f}\nRestaurant area: {seller_zone or 'Not set'}\nRestaurant landmark: {seller_landmark or 'Not set'}\n\nHow would you like to receive your order?",
                buttons,
                header_text="Choose Fulfilment"
            )
            if not success:
                cloud.send_whatsapp_message(phone, "1. Delivery\n2. Pickup\n3. Cancel")
            session["state"] = "buyer_fulfillment_method"
        except ValueError:
            cloud.send_whatsapp_message(phone, "Invalid quantity. Please enter a number.")

    elif state == "buyer_fulfillment_method":
        if text in {"fulfillment_pickup", "2"}:
            session["data"]["pickup_or_delivery"] = "pickup"
            session["data"]["delivery_fee"] = 0
            session["data"]["delivery_zone"] = "Pickup at restaurant"
            session["data"]["delivery_address"] = "Pickup at restaurant"
            send_checkout_summary(phone, session)
            session["state"] = "buyer_confirm_order"
        elif text in {"fulfillment_delivery", "1"}:
            session["data"]["pickup_or_delivery"] = "delivery"
            show_buyer_zone_picker(phone, session["data"].get("seller_zone"), session["data"].get("seller_landmark"))
            session["state"] = "buyer_delivery_zone"
        elif text in {"cancel_order", "3"}:
            cloud.send_whatsapp_message(phone, "❌ Order cancelled. Type 'menu' to start again.")
            session["state"] = "idle"
            session["data"] = {}
        else:
            cloud.send_whatsapp_message(phone, "❌ Please choose Delivery or Pickup.")

    elif state == "buyer_delivery_zone":
        delivery_zone = resolve_zone_choice(text.replace("buyer_zone_", "zone_"))
        if not delivery_zone:
            cloud.send_whatsapp_message(phone, "❌ Zone not found. Please tap a valid delivery zone.")
            return

        session["data"]["delivery_zone"] = delivery_zone
        show_buyer_landmark_picker(phone, delivery_zone)
        session["state"] = "buyer_delivery_landmark"

    elif state == "buyer_delivery_landmark":
        delivery_zone = session["data"].get("delivery_zone")
        delivery_landmark = resolve_landmark_choice(delivery_zone, text.replace("buyer_landmark_", "landmark_"))
        if not delivery_landmark:
            cloud.send_whatsapp_message(phone, "❌ Landmark not found. Please tap a valid delivery landmark.")
            return

        session["data"]["delivery_landmark"] = delivery_landmark
        session["data"]["delivery_fee"] = calculate_delivery_fee(
            session["data"].get("seller_zone"),
            session["data"].get("seller_landmark"),
            delivery_zone,
            delivery_landmark
        )
        cloud.send_whatsapp_message(phone, f"🏠 *Delivery Address*\n\nZone: {delivery_zone}\nLandmark: {delivery_landmark}\nDelivery is free for now.\n\nPlease provide your specific location details:\n*Example: Martina Hostel, Room 12 near the gate*")
        session["state"] = "buyer_delivery_address"
    
    elif state == "buyer_delivery_address":
        if len(text) < 5:
            cloud.send_whatsapp_message(phone, "❌ Please provide a more detailed address.")
            return
        
        session["data"]["delivery_address"] = text
        send_checkout_summary(phone, session)
        session["state"] = "buyer_confirm_order"

    elif state == "buyer_confirm_order":
        if text in {"proceed_payment", "1"}:
            try:
                validate_order_request(session["data"])
            except ValueError as exc:
                cloud.send_whatsapp_message(phone, f"❌ {exc} Please review your order and try again.")
                session["state"] = "buyer_browsing"
                return
            import uuid
            payment_ref = f"ZC_{uuid.uuid4().hex[:8]}"
            session["data"]["payment_ref"] = payment_ref
            
            order_id, seller_phone, total = place_order_market(phone, session["data"], 'awaiting_payment')
            
            payment_url = initiate_paystack_payment(phone, total, order_id, payment_ref)
            
            if payment_url:
                fulfilment = session["data"].get("pickup_or_delivery", "delivery").capitalize()
                msg = f"💳 *Payment Required*\n\n"
                msg += f"Order #{order_id}\n"
                msg += f"Total: GHS {total:.2f}\n"
                msg += f"Method: {fulfilment}\n"
                if fulfilment.lower() == "delivery":
                    msg += f"Zone: {session['data'].get('delivery_zone', 'N/A')}\n"
                    msg += f"Landmark: {session['data'].get('delivery_landmark', 'N/A')}\n"
                    msg += f"Address: {session['data'].get('delivery_address', 'N/A')}\n"
                msg += "\n"
                msg += f"Click to pay:\n{payment_url}\n\n"
                msg += "After payment, you'll receive a confirmation code."
                cloud.send_whatsapp_message(phone, msg)
            else:
                order_code = generate_order_code()
                update_order_status(order_id, 'paid', order_code)
                notify_seller(order_id, phone, total, seller_phone, session["data"])
                cloud.send_whatsapp_message(phone, f"✅ *Order Confirmed!*\n\nOrder Code: *{order_code}*\n\nShare this code when your order is handed over. The seller has been notified.")
            
            session["state"] = "idle"
            session["data"] = {}
        elif text in {"cancel_order", "2"}:
            cloud.send_whatsapp_message(phone, "❌ Order cancelled. Type 'menu' to start over.")
            session["state"] = "idle"
            session["data"] = {}
        else:
            cloud.send_whatsapp_message(phone, "❌ Please choose Pay Now or Cancel.")

def send_checkout_summary(phone, session):
    pid = session["data"]["selected_prod"]
    prod = get_product_by_id(pid)
    prod_name = prod[2] if prod else "Item"
    fulfilment = session["data"].get("pickup_or_delivery", "delivery")
    delivery_fee = float(session["data"].get("delivery_fee", 0) or 0)
    total = session["data"]["food_total"] + delivery_fee

    summary = f"📑 *Order Summary*\n\n"
    summary += f"Item: {prod_name}\n"
    summary += f"Qty: {session['data']['qty']}\n"
    summary += f"Subtotal: GHS {session['data']['food_total']:.2f}\n"
    if fulfilment == "delivery":
        summary += f"Delivery ({session['data']['delivery_zone']}): Included for now\n"
        summary += f"Landmark: {session['data'].get('delivery_landmark', 'N/A')}\n"
        summary += f"Address: {session['data']['delivery_address']}\n"
    else:
        summary += "Pickup: Collect directly from the restaurant\n"
        summary += "Delivery: Included for now\n"
    summary += "-----------\n"
    summary += f"*Total: GHS {total:.2f}*\n\n"
    summary += "Proceed to payment:"

    buttons = [
        {"id": "proceed_payment", "title": "💳 Pay Now"},
        {"id": "cancel_order", "title": "❌ Cancel"}
    ]
    success = cloud.send_interactive_buttons(phone, summary, buttons, header_text="Ready to Pay")
    if not success:
        cloud.send_whatsapp_message(phone, summary + "\n\n1. Pay Now\n2. Cancel")

def show_shops_list(phone):
    shops = fetch_available_shops()
    
    if not shops:
        cloud.send_whatsapp_message(phone, "🍴 *No Shops Available*\n\nSorry, there are no food vendors listed yet. Type 'menu' to go back.")
    else:
        sections = []
        rows = []
        for s in shops:
            rows.append({
                "id": s[0], # phone number is the ID
                "title": s[1], # Shop Name
                "description": (s[2] or s[4] or s[3] or "")[:72] # Shop Description
            })
        sections.append({"title": "Choose a Restaurant", "rows": rows})
        
        success = cloud.send_interactive_list(
            phone,
            "🍴 *Browse Shops*\n\nChoose a restaurant to view their menu:",
            "View Menu",
            sections
        )
        
        if not success:
            # Fallback to text message
            msg = "🍴 *Available Shops:*\n\n"
            for i, s in enumerate(shops, 1):
                msg += f"{i}. *{s[1]}*\n"
                if s[2]:
                    msg += f"   {s[2][:50]}\n"
            msg += f"\n*Reply with the shop number (1-{len(shops)})*"
            cloud.send_whatsapp_message(phone, msg)

def show_catalog_buyer(phone, seller_phone):
    normalized_seller_phone = normalize_phone(seller_phone)
    prods, shop = fetch_shop_catalog(normalized_seller_phone)
    shop_name = shop[0] if shop and shop[0] else "Shop"
    shop_description = shop[1] if shop else ""
    shop_zone = shop[2] if shop else ""
    shop_landmark = shop[3] if shop else ""
    shop_image = shop[4] if shop else ""

    if not prods:
        cloud.send_whatsapp_message(phone, f"🍴 *{shop_name}* has no items available right now. Type 'menu' to go back.")
    else:
        if shop_image:
            caption = f"{shop_name}\n{shop_description or 'Fresh food, fast pickup and delivery.'}"
            cloud.send_whatsapp_image(phone, shop_image, caption=caption[:1024])
        sections = []
        rows = []
        for p in prods:
            rows.append({
                "id": f"prod_{p[0]}",
                "title": p[1],
                "description": f"GHS {p[2]:.2f} | {p[5]} left | {p[3][:28] if p[3] else ''}"
            })
        sections.append({"title": "Available Items", "rows": rows})
        
        success = cloud.send_interactive_list(
            phone,
            f"🍴 *{shop_name} Menu*\n\n{shop_description or 'Fresh campus meals.'}\nArea: {shop_zone or 'Not set'}\nLandmark: {shop_landmark or 'Not set'}\n\nSelect an item to order:",
            "Select Item",
            sections
        )
        
        if not success:
            # Fallback to text message
            msg = f"🍴 *{shop_name} Menu:*\n\n"
            for i, p in enumerate(prods, 1):
                msg += f"{i}. *{p[1]}* - GHS {p[2]:.2f} ({p[5]} left)\n"
                if p[3]:
                    msg += f"   {p[3][:50]}\n"
            msg += f"\n*Reply with item number (1-{len(prods)})*"
            cloud.send_whatsapp_message(phone, msg)

def validate_order_request(order_data):
    pid = order_data["selected_prod"]
    qty = int(order_data["qty"])
    product = get_product_by_id(pid)
    if not product:
        raise ValueError("Selected product no longer exists.")
    if qty < 1 or qty > product[4]:
        raise ValueError("Selected quantity exceeds available stock.")
    return product

def place_order_market(buyer_phone, order_data, status='pending'):
    pid = order_data["selected_prod"]
    seller_phone = normalize_phone(order_data["seller_phone"])
    qty = order_data["qty"]
    validate_order_request(order_data)
    food_total = order_data["food_total"]
    delivery_fee = float(order_data.get("delivery_fee", 0) or 0)
    payment_ref = order_data.get("payment_ref", "")
    total_amount = food_total + delivery_fee
    fulfillment_method = order_data.get("pickup_or_delivery", "delivery")
    delivery_zone = order_data.get("delivery_zone") or order_data.get("buyer_zone", "")
    delivery_landmark = order_data.get("delivery_landmark", "")
    delivery_address = order_data.get("delivery_address", "")
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    order_columns = get_table_columns("orders")
    values = {
        "buyer_phone": normalize_phone(buyer_phone),
        "phone": normalize_phone(buyer_phone),
        "seller_phone": seller_phone,
        "total_price": total_amount,
        "total": total_amount,
        "delivery_fee": delivery_fee,
        "delivery_zone": delivery_zone,
        "delivery_landmark": delivery_landmark,
        "delivery_address": delivery_address,
        "pickup_or_delivery": fulfillment_method,
        "status": status,
        "payment_ref": payment_ref,
    }
    insert_columns = [column for column in values if column in order_columns]
    placeholders = ", ".join(["?"] * len(insert_columns))
    c.execute(
        f"INSERT INTO orders ({', '.join(insert_columns)}) VALUES ({placeholders})",
        tuple(values[column] for column in insert_columns)
    )
    order_id = c.lastrowid
    
    c.execute("INSERT INTO order_items (order_id, product_id, quantity, price_at_purchase) VALUES (?, ?, ?, ?)",
              (order_id, pid, qty, food_total/qty))
    conn.commit()
    conn.close()
    
    return order_id, seller_phone, total_amount

def notify_seller(order_id, buyer_phone, total, seller_phone, order_data=None):
    """Notify seller of new order"""
    order_data = order_data or {}
    fulfilment = order_data.get("pickup_or_delivery", "delivery").capitalize()
    seller_msg = f"🔔 *NEW ORDER!*\n\nOrder #{order_id}\nBuyer: {buyer_phone}\nTotal: GHS {total:.2f}\nMethod: {fulfilment}"
    if fulfilment.lower() == "delivery":
        seller_msg += f"\nZone: {order_data.get('delivery_zone', 'N/A')}"
        seller_msg += f"\nLandmark: {order_data.get('delivery_landmark', 'N/A')}"
        seller_msg += f"\nAddress: {order_data.get('delivery_address', 'N/A')}"
    seller_msg += "\n\nGo to 'View Orders' to see details."
    cloud.send_whatsapp_message(seller_phone, seller_msg)

def update_order_status(order_id, status, confirmation_code=None):
    """Update order status and optionally add confirmation code"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if confirmation_code:
        c.execute("UPDATE orders SET status = ?, confirmation_code = ? WHERE id = ?", 
                  (status, confirmation_code, order_id))
    else:
        c.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()

# =========================
# PAYSTACK WEBHOOK
# =========================
@app.route("/payment/callback", methods=["GET"])
def paystack_callback():
    """Handle Paystack payment callback"""
    reference = request.args.get("reference")
    
    if not reference:
        return render_payment_status_page(
            "Reference Missing",
            "We could not find a payment reference in this callback request.",
            tone="error"
        ), 400
    
    # Verify payment
    payment_success = verify_paystack_payment(reference)
    
    if payment_success:
        order_info, error = finalize_paid_order(reference)
        if error == "missing":
            return render_payment_status_page(
                "Order Not Found",
                "Payment was confirmed, but we could not find the matching order record.",
                tone="warning"
            ), 404

        subtitle = "Your payment went through and the seller has been updated on WhatsApp."
        if order_info.get("already_paid"):
            subtitle = "This payment was already confirmed earlier. Your order remains active."
        return render_payment_status_page(
            "Payment Successful",
            subtitle,
            tone="success",
            confirmation_code=order_info.get("confirmation_code"),
            order_id=order_info.get("order_id")
        ), 200
    return render_payment_status_page(
        "Payment Verification Failed",
        "We could not confirm this transaction yet. If you were charged, contact support with your reference.",
        tone="error"
    ), 400

@app.route("/payment/webhook", methods=["POST"])
def paystack_webhook():
    """Handle Paystack webhook for payment notifications"""
    data = request.get_json() or {}
    
    if data.get("event") == "charge.success":
        reference = data.get("data", {}).get("reference")
        if reference:
            if verify_paystack_payment(reference):
                finalize_paid_order(reference)
    
    return "OK", 200

def list_buyer_orders(phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT id, total_price, status, pickup_or_delivery
        FROM ({get_orders_view_sql()}) AS orders_view
        WHERE buyer_phone = ?
        ORDER BY created_at DESC
        LIMIT 5
    """, (normalize_phone(phone),))
    orders = c.fetchall()
    conn.close()
    if not orders:
        send_text(phone, "📭 You haven't placed any orders yet.\n\nType 'menu' to browse food!")
    else:
        msg = "📦 *Your Recent Orders:*\n\n"
        for o in orders:
            status_emoji = "✅" if o[2] in ("completed", "paid") else "🍳" if o[2] == "accepted" else "⏳" if o[2] in ("pending", "awaiting_payment") else "❌"
            msg += f"{status_emoji} Order #{o[0]}: GHS {o[1]:.2f} [{o[3]}]\n"
        msg += "\nType 'menu' to order more food!"
        send_text(phone, msg)

def show_buyer_profile(phone, user):
    """Show buyer profile with options to update."""
    msg = f"""👤 *Your Profile*

📛 Name: {user[USER_NAME]}
📱 Phone: {user[USER_PHONE]}
📍 Zone: {user[USER_ZONE]}
🎭 Role: {user[USER_ROLE].capitalize()}

Select:

[1] 🔄 Change Zone
[2] ◀️ Back to Menu

*Tap a number*"""
    buttons = [
        {"id": "profile_change_zone", "title": "🔄 Change Zone"},
        {"id": "profile_back", "title": "◀️ Back"}
    ]
    success = cloud.send_interactive_buttons(phone, msg, buttons, header_text="Your Profile")
    if not success:
        send_text(phone, msg)

def show_cart(phone, session):
    """Show current cart contents."""
    cart = session.get("cart", [])
    
    if not cart:
        msg = """🛒 *Your Cart is Empty*

Browse food and add items to your cart!

Type 'menu' to start shopping."""
        send_text(phone, msg)
    else:
        total = sum(item['price'] * item['qty'] for item in cart)
        msg = "🛒 *Your Cart*\n\n"
        for i, item in enumerate(cart, 1):
            msg += f"• {item['name']} x{item['qty']} = GHS {item['price'] * item['qty']:.2f}\n"
        msg += f"\n💰 *Total: GHS {total:.2f}*"
        
        buttons = [
            {"id": "cart_clear", "title": "🗑️ Clear Cart"},
            {"id": "cart_checkout", "title": "✅ Place Order"},
            {"id": "cart_continue", "title": "◀️ Shop More"}
        ]
        cloud.send_interactive_buttons(phone, msg, buttons)

def add_product_db(seller_phone, name, desc, price, stock, image_url):
    normalized_phone = normalize_phone(seller_phone)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO products (seller_phone, name, description, price, stock, image_url) VALUES (?, ?, ?, ?, ?, ?)",
              (normalized_phone, name, desc, price, stock, image_url))
    conn.commit()
    conn.close()
    invalidate_market_cache(normalized_phone)

def render_payment_status_page(title, subtitle, tone="success", confirmation_code=None, order_id=None):
    tone_map = {
        "success": {
            "accent": "#0f9d58",
            "glow": "rgba(15, 157, 88, 0.22)",
            "badge": "Payment Confirmed",
        },
        "warning": {
            "accent": "#c77d00",
            "glow": "rgba(199, 125, 0, 0.22)",
            "badge": "Action Needed",
        },
        "error": {
            "accent": "#d93025",
            "glow": "rgba(217, 48, 37, 0.22)",
            "badge": "Payment Issue",
        },
    }
    palette = tone_map.get(tone, tone_map["success"])
    code_html = ""
    if confirmation_code:
        code_html = f"""
        <div class="code-card">
          <span class="label">Confirmation code</span>
          <strong>{escape(confirmation_code)}</strong>
        </div>
        """
    order_html = ""
    if order_id:
        order_html = f'<p class="meta">Order #{int(order_id)}</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} | ZanChop</title>
  <style>
    :root {{
      --bg: #f4efe6;
      --ink: #132218;
      --muted: #5b6a60;
      --accent: {palette["accent"]};
      --glow: {palette["glow"]};
      --card: rgba(255,255,255,0.86);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Segoe UI", Arial, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, var(--glow), transparent 32%),
        radial-gradient(circle at bottom right, rgba(255, 180, 80, 0.18), transparent 28%),
        linear-gradient(135deg, #f7f2ea, #eef7f1);
      display: grid;
      place-items: center;
      overflow: hidden;
    }}
    .orb {{
      position: fixed;
      width: 18rem;
      height: 18rem;
      border-radius: 999px;
      filter: blur(16px);
      opacity: .45;
      animation: drift 14s ease-in-out infinite alternate;
    }}
    .orb.one {{ top: -4rem; left: -3rem; background: var(--glow); }}
    .orb.two {{ bottom: -5rem; right: -2rem; background: rgba(20, 88, 54, 0.14); animation-delay: -4s; }}
    .panel {{
      width: min(92vw, 42rem);
      padding: 2rem;
      border-radius: 28px;
      background: var(--card);
      border: 1px solid rgba(19, 34, 24, 0.08);
      box-shadow: 0 28px 80px rgba(19, 34, 24, 0.12);
      backdrop-filter: blur(10px);
      position: relative;
      z-index: 1;
      animation: rise .7s ease-out both;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: .5rem;
      padding: .55rem .9rem;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(19, 34, 24, 0.08);
      color: var(--accent);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-size: .72rem;
    }}
    h1 {{
      margin: 1rem 0 .5rem;
      font-size: clamp(2rem, 4vw, 3.2rem);
      line-height: 1;
    }}
    .meta, p {{
      margin: .4rem 0;
      color: var(--muted);
      line-height: 1.6;
      font-size: 1rem;
    }}
    .code-card {{
      margin: 1.5rem 0 1rem;
      padding: 1rem 1.15rem;
      border-radius: 20px;
      background: rgba(255,255,255,0.74);
      border: 1px solid rgba(19, 34, 24, 0.08);
    }}
    .label {{
      display: block;
      font-size: .72rem;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--muted);
      margin-bottom: .45rem;
    }}
    strong {{
      font-size: clamp(1.6rem, 5vw, 2.4rem);
      letter-spacing: .18em;
      color: var(--accent);
    }}
    .foot {{
      margin-top: 1.25rem;
      font-size: .92rem;
    }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(24px) scale(.98); }}
      to {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}
    @keyframes drift {{
      from {{ transform: translate3d(0, 0, 0) scale(1); }}
      to {{ transform: translate3d(18px, 32px, 0) scale(1.14); }}
    }}
  </style>
</head>
<body>
  <div class="orb one"></div>
  <div class="orb two"></div>
  <main class="panel">
    <span class="badge">{escape(palette["badge"])}</span>
    <h1>{escape(title)}</h1>
    {order_html}
    <p>{escape(subtitle)}</p>
    {code_html}
    <p class="foot">Return to WhatsApp to continue with your order updates, seller messages, and delivery handoff.</p>
  </main>
</body>
</html>"""

# =========================
# BUYER EXPERIENCE OVERRIDES
# =========================
def ensure_cart(session):
    cart = session.get("cart")
    if not isinstance(cart, list):
        session["cart"] = []
    return session["cart"]

def get_cart_subtotal(cart):
    return round(sum(float(item["price"]) * int(item["qty"]) for item in cart), 2)

def get_cart_item_count(cart):
    return sum(int(item["qty"]) for item in cart)

def clear_buyer_draft(session):
    session["data"] = {}

def add_item_to_cart(session, product, qty, addon_text="", instructions=""):
    cart = ensure_cart(session)
    seller_phone = normalize_phone(product[0])
    if cart and normalize_phone(cart[0]["seller_phone"]) != seller_phone:
        return False, "Your cart already has items from another restaurant. Clear it first or finish checkout."

    existing_qty = sum(int(item["qty"]) for item in cart if int(item["product_id"]) == int(product[1]))
    if existing_qty + qty > int(product[5]):
        return False, f"Only {product[5]} portion(s) of *{product[2]}* are available right now."

    normalized_addons = "" if addon_text.lower() == "skip" else addon_text.strip()
    normalized_instructions = "" if instructions.lower() == "skip" else instructions.strip()
    for item in cart:
        if (
            int(item["product_id"]) == int(product[1]) and
            item.get("addon_text", "") == normalized_addons and
            item.get("instructions", "") == normalized_instructions
        ):
            item["qty"] = int(item["qty"]) + qty
            return True, f"Updated *{product[2]}* in your cart."

    cart.append({
        "product_id": int(product[1]),
        "seller_phone": seller_phone,
        "shop_name": product[7] or "Restaurant",
        "name": product[2],
        "description": product[3] or "",
        "price": float(product[4]),
        "qty": int(qty),
        "image_url": product[6] or "",
        "addon_text": normalized_addons,
        "instructions": normalized_instructions,
    })
    return True, f"Added *{product[2]}* to your cart."

def prepare_checkout_data(session, user):
    cart = ensure_cart(session)
    if not cart:
        raise ValueError("Your cart is empty.")

    seller_phone = normalize_phone(cart[0]["seller_phone"])
    seller = get_user(seller_phone)
    for item in cart:
        product = get_product_details(item["product_id"])
        if not product:
            raise ValueError(f"{item['name']} is no longer available.")
        if normalize_phone(product[0]) != seller_phone:
            raise ValueError("Your cart must contain items from one restaurant only.")
        if int(item["qty"]) > int(product[5]):
            raise ValueError(f"{product[2]} only has {product[5]} portion(s) left.")

    session["data"] = {
        "seller_phone": seller_phone,
        "cart_items": [dict(item) for item in cart],
        "food_total": get_cart_subtotal(cart),
        "buyer_zone": user[USER_ZONE] or "",
        "seller_zone": seller[USER_ZONE] if seller else "",
        "seller_landmark": seller[USER_LANDMARK] if seller else "",
        "delivery_fee": 0,
        "delivery_zone": "",
        "delivery_landmark": "",
        "delivery_address": "",
        "pickup_or_delivery": "delivery",
        "checkout_shop_name": cart[0].get("shop_name", "Restaurant"),
    }
    return session["data"]

def show_buyer_home(phone, user, session):
    cart = ensure_cart(session)
    cart_count = get_cart_item_count(cart)
    rows = [
        {"id": "home_browse", "title": "Browse Food", "description": "Restaurants, menus, full product view"},
        {"id": "home_search", "title": "Search", "description": "Find food or vendors by keyword"},
        {"id": "home_orders", "title": "My Orders", "description": "Track status and confirm with OTP"},
        {"id": "home_cart", "title": "View Cart", "description": f"{cart_count} item(s) ready for checkout"},
        {"id": "home_profile", "title": "Buyer Details", "description": "Name, number, and zone"},
        {"id": "home_help", "title": "Help", "description": "Call or message support directly"},
    ]
    body = (
        f"Hello, welcome to *Zan Chop*, your Campus food platform.\n\n"
        f"Buyer: {user[USER_NAME]}\n"
        f"Number: {user[USER_PHONE]}\n"
        f"Zone: {user[USER_ZONE] or 'Not set'}\n\n"
        "Choose what you want to do."
    )
    success = cloud.send_interactive_list(
        phone,
        body,
        "Open Home",
        [{"title": "Buyer Home", "rows": rows}],
        header_text="Zan Chop"
    )
    if not success:
        msg = (
            f"Hello, welcome to *Zan Chop*, your Campus food platform.\n\n"
            f"Buyer: {user[USER_NAME]}\n"
            f"Number: {user[USER_PHONE]}\n"
            f"Zone: {user[USER_ZONE] or 'Not set'}\n\n"
            "1. Browse Food\n"
            "2. Search\n"
            "3. My Orders\n"
            "4. View Cart\n"
            "5. Buyer Details\n"
            "6. Help"
        )
        cloud.send_whatsapp_message(phone, msg)

def show_buyer_help(phone):
    support_number = normalize_phone(ADMIN_PHONE or MOMO_RECEIVER_NUMBER or "")
    wa_link = f"https://wa.me/{support_number}" if support_number else "Not set"
    call_hint = f"+{support_number}" if support_number else "Not set"
    msg = (
        "🆘 *Zan Chop Help*\n\n"
        "Need help with payment, delivery, or a seller?\n\n"
        f"Call Support: {call_hint}\n"
        f"WhatsApp Support: {wa_link}\n\n"
        "Type *menu* anytime to go back home."
    )
    buttons = [
        {"id": "help_back", "title": "⬅️ Back Home"}
    ]
    success = cloud.send_interactive_buttons(phone, msg, buttons, header_text="Support")
    if not success:
        cloud.send_whatsapp_message(phone, msg + "\n\n1. Back Home")

def show_buyer_profile(phone, user):
    msg = (
        "👤 *Buyer Details*\n\n"
        f"Name: {user[USER_NAME]}\n"
        f"Phone: {user[USER_PHONE]}\n"
        f"Zone: {user[USER_ZONE] or 'Not set'}\n"
        f"Role: {user[USER_ROLE].capitalize()}\n\n"
        "You can update your zone or return home."
    )
    buttons = [
        {"id": "profile_change_zone", "title": "🔄 Change Zone"},
        {"id": "profile_back", "title": "⬅️ Back Home"}
    ]
    success = cloud.send_interactive_buttons(phone, msg, buttons, header_text="Buyer Details")
    if not success:
        cloud.send_whatsapp_message(phone, msg + "\n\n1. Change Zone\n2. Back Home")

def show_shops_list(phone):
    shops = fetch_available_shops()
    if not shops:
        cloud.send_whatsapp_message(phone, "🍽️ *No Shops Available*\n\nSorry, there are no restaurants ready yet. Type *menu* to go back.")
        return

    rows = []
    for shop in shops[:10]:
        rows.append({
            "id": shop[0],
            "title": truncate_text(shop[1] or "Restaurant", 24),
            "description": truncate_text(shop[2] or shop[4] or shop[3] or "Fresh food available", 72)
        })
    success = cloud.send_interactive_list(
        phone,
        "🍽️ *Browse Food*\n\nChoose a restaurant to open its full menu.",
        "View Restaurants",
        [{"title": "Restaurants", "rows": rows}],
        header_text="Browse Food"
    )
    if not success:
        msg = "🍽️ *Restaurants*\n\n"
        for index, shop in enumerate(shops, 1):
            msg += f"{index}. *{shop[1]}*\n"
            if shop[2]:
                msg += f"   {truncate_text(shop[2], 48)}\n"
        msg += f"\nReply with the restaurant number (1-{len(shops)})."
        cloud.send_whatsapp_message(phone, msg)

def show_catalog_buyer(phone, seller_phone):
    products, shop = fetch_shop_catalog(seller_phone)
    shop_name = shop[0] if shop and shop[0] else "Restaurant"
    shop_description = shop[1] if shop else ""
    shop_zone = shop[2] if shop else ""
    shop_landmark = shop[3] if shop else ""
    shop_image = shop[4] if shop else ""

    if not products:
        cloud.send_whatsapp_message(phone, f"🍽️ *{shop_name}* has no items available right now. Type *menu* to go back.")
        return

    if shop_image:
        caption = f"{shop_name}\n{shop_description or 'Fresh campus meals, fast pickup and delivery.'}"
        cloud.send_whatsapp_image(phone, shop_image, caption=caption[:1024])

    rows = []
    for product in products[:10]:
        rows.append({
            "id": f"prod_{product[0]}",
            "title": truncate_text(product[1], 24),
            "description": truncate_text(f"GHS {product[2]:.2f} | {product[5]} left | {product[3] or 'Tap to view details'}", 72)
        })
    success = cloud.send_interactive_list(
        phone,
        (
            f"🍽️ *{shop_name}*\n\n"
            f"{shop_description or 'Fresh campus meals.'}\n"
            f"Zone: {shop_zone or 'Not set'}\n"
            f"Landmark: {shop_landmark or 'Not set'}\n\n"
            "Select any food item to open its full product page."
        ),
        "View Menu",
        [{"title": "Available Food", "rows": rows}],
        header_text=truncate_text(shop_name, 20)
    )
    if not success:
        msg = f"🍽️ *{shop_name} Menu*\n\n"
        for index, product in enumerate(products, 1):
            msg += f"{index}. *{product[1]}* - GHS {product[2]:.2f} ({product[5]} left)\n"
            if product[3]:
                msg += f"   {truncate_text(product[3], 48)}\n"
        msg += f"\nReply with the item number (1-{len(products)})."
        cloud.send_whatsapp_message(phone, msg)

def show_search_results(phone, results, query):
    if not results:
        cloud.send_whatsapp_message(phone, f"🔎 No food or restaurant matched *{query}*. Try another keyword.")
        return False

    rows = []
    for result in results[:10]:
        rows.append({
            "id": f"search_prod_{result[0]}",
            "title": truncate_text(result[1], 24),
            "description": truncate_text(f"{result[7] or 'Restaurant'} | GHS {result[2]:.2f} | {result[4]} left", 72)
        })
    success = cloud.send_interactive_list(
        phone,
        f"🔎 *Search Results*\n\nKeyword: {query}\nSelect a food item to view details.",
        "Open Results",
        [{"title": "Matching Food", "rows": rows}],
        header_text="Search"
    )
    if not success:
        msg = f"🔎 *Results for '{query}'*\n\n"
        for index, result in enumerate(results, 1):
            msg += f"{index}. *{result[1]}* | {result[7] or 'Restaurant'} | GHS {result[2]:.2f}\n"
        msg += f"\nReply with the result number (1-{len(results)})."
        cloud.send_whatsapp_message(phone, msg)
    return True

def show_product_detail(phone, product):
    _, _, name, description, price, stock, image_url, shop_name, _, zone, landmark = product
    if image_url:
        cloud.send_whatsapp_image(phone, image_url, caption=f"{name}\nGHS {price:.2f} each")
    body = (
        f"🍱 *{name}*\n\n"
        f"Restaurant: {shop_name or 'Restaurant'}\n"
        f"Price: GHS {price:.2f}\n"
        f"Available: {stock}\n"
        f"Zone: {zone or 'Not set'}\n"
        f"Landmark: {landmark or 'Not set'}\n\n"
        f"{description or 'No extra description yet.'}\n\n"
        "You can add extras and special instructions in the next steps."
    )
    buttons = [
        {"id": "buyer_add_to_cart", "title": "🛒 Add to Cart"},
        {"id": "buyer_buy_now", "title": "⚡ Buy Now"},
        {"id": "buyer_product_back", "title": "⬅️ Back"}
    ]
    success = cloud.send_interactive_buttons(phone, body, buttons, header_text="Product Page")
    if not success:
        cloud.send_whatsapp_message(phone, body + "\n\n1. Add to Cart\n2. Buy Now\n3. Back")

def show_cart(phone, session):
    cart = ensure_cart(session)
    if not cart:
        cloud.send_whatsapp_message(
            phone,
            "🛒 *Your Cart is Empty*\n\nBrowse restaurants, open a product page, and add food to your cart."
        )
        return

    total = get_cart_subtotal(cart)
    msg = f"🛒 *Your Cart*\n\nRestaurant: {cart[0].get('shop_name', 'Restaurant')}\n\n"
    for index, item in enumerate(cart, 1):
        line_total = float(item["price"]) * int(item["qty"])
        msg += f"{index}. *{item['name']}* x{item['qty']} = GHS {line_total:.2f}\n"
        if item.get("addon_text"):
            msg += f"   Add-ons: {truncate_text(item['addon_text'], 55)}\n"
        if item.get("instructions"):
            msg += f"   Note: {truncate_text(item['instructions'], 55)}\n"
    msg += f"\n💰 *Subtotal: GHS {total:.2f}*"
    buttons = [
        {"id": "cart_checkout", "title": "✅ Checkout"},
        {"id": "cart_clear", "title": "🗑️ Clear Cart"},
        {"id": "cart_continue", "title": "◀️ Shop More"}
    ]
    success = cloud.send_interactive_buttons(phone, msg, buttons, header_text="Cart")
    if not success:
        cloud.send_whatsapp_message(phone, msg + "\n\n1. Checkout\n2. Clear Cart\n3. Shop More")

def show_buyer_orders_menu(phone, orders):
    if not orders:
        cloud.send_whatsapp_message(phone, "📭 You haven't placed any orders yet.\n\nType *menu* to browse food.")
        return False

    rows = []
    for order in orders[:10]:
        rows.append({
            "id": f"buyer_order_{order[0]}",
            "title": f"Order #{order[0]}",
            "description": truncate_text(f"{order[9] or 'Restaurant'} | {format_order_status(order[2])} | GHS {order[1]:.2f}", 72)
        })
    success = cloud.send_interactive_list(
        phone,
        "📦 *My Orders*\n\nSelect an order to view status, OTP, and delivery details.",
        "Open Orders",
        [{"title": "Recent Orders", "rows": rows}],
        header_text="My Orders"
    )
    if not success:
        msg = "📦 *My Orders*\n\n"
        for index, order in enumerate(orders, 1):
            msg += f"{index}. Order #{order[0]} | {order[9] or 'Restaurant'} | {format_order_status(order[2])}\n"
        msg += f"\nReply with the order number (1-{len(orders)})."
        cloud.send_whatsapp_message(phone, msg)
    return True

def show_buyer_order_detail(phone, order):
    items = get_order_items(order[0])
    item_lines = []
    for item in items:
        line = f"• {item[1]} x{item[2]} = GHS {float(item[3]) * int(item[2]):.2f}"
        if item[4]:
            line += f"\n  Add-ons: {truncate_text(item[4], 50)}"
        if item[5]:
            line += f"\n  Note: {truncate_text(item[5], 50)}"
        item_lines.append(line)

    msg = (
        f"📦 *Order #{order[0]}*\n\n"
        f"Restaurant: {order[13] or order[2]}\n"
        f"Status: {format_order_status(order[9])}\n"
        f"Method: {order[8].capitalize()}\n"
        f"Total: GHS {order[3]:.2f}\n"
        "Delivery: Free for now\n"
        f"Zone: {order[5] or 'N/A'}\n"
        f"Landmark: {order[6] or 'N/A'}\n"
        f"Address: {order[7] or 'N/A'}\n"
        f"OTP: {order[11] or 'Available after payment'}\n\n"
        f"{chr(10).join(item_lines) if item_lines else 'No order items found.'}"
    )
    buttons = [{"id": "buyer_orders_back", "title": "⬅️ Back"}]
    if order[9] == "on_the_way" and order[11]:
        buttons.insert(0, {"id": "buyer_order_confirm_otp", "title": "🔐 Confirm OTP"})
    success = cloud.send_interactive_buttons(phone, msg, buttons[:3], header_text="Order Detail")
    if not success:
        if order[9] == "on_the_way" and order[11]:
            cloud.send_whatsapp_message(phone, msg + "\n\n1. Confirm OTP\n2. Back")
        else:
            cloud.send_whatsapp_message(phone, msg + "\n\n1. Back")

def begin_cart_checkout(phone, session, user):
    try:
        checkout = prepare_checkout_data(session, user)
    except ValueError as exc:
        cloud.send_whatsapp_message(phone, f"❌ {exc}")
        return False

    buttons = [
        {"id": "fulfillment_delivery", "title": "🚚 Delivery"},
        {"id": "fulfillment_pickup", "title": "🏪 Pickup"},
        {"id": "fulfillment_back_cart", "title": "🛒 Back to Cart"}
    ]
    success = cloud.send_interactive_buttons(
        phone,
        (
            f"Checkout for *{checkout['checkout_shop_name']}*\n\n"
            f"Items: {get_cart_item_count(checkout['cart_items'])}\n"
            f"Food Total: GHS {checkout['food_total']:.2f}\n"
            f"Restaurant Zone: {checkout['seller_zone'] or 'Not set'}\n"
            f"Restaurant Landmark: {checkout['seller_landmark'] or 'Not set'}\n\n"
            "How should we fulfil this order?"
        ),
        buttons,
        header_text="Checkout"
    )
    if not success:
        cloud.send_whatsapp_message(phone, "1. Delivery\n2. Pickup\n3. Back to Cart")
    return True

def send_checkout_summary(phone, session):
    cart_items = session["data"].get("cart_items", [])
    fulfilment = session["data"].get("pickup_or_delivery", "delivery")
    delivery_fee = float(session["data"].get("delivery_fee", 0) or 0)
    total = float(session["data"].get("food_total", 0) or 0) + delivery_fee

    summary = "🧾 *Checkout Summary*\n\n"
    for item in cart_items:
        line_total = float(item["price"]) * int(item["qty"])
        summary += f"• {item['name']} x{item['qty']} = GHS {line_total:.2f}\n"
        if item.get("addon_text"):
            summary += f"  Add-ons: {truncate_text(item['addon_text'], 55)}\n"
        if item.get("instructions"):
            summary += f"  Note: {truncate_text(item['instructions'], 55)}\n"
    summary += f"\nSubtotal: GHS {session['data'].get('food_total', 0):.2f}\n"
    if fulfilment == "delivery":
        summary += f"Delivery ({session['data']['delivery_zone']}): Included for now\n"
        summary += f"Landmark: {session['data'].get('delivery_landmark', 'N/A')}\n"
        summary += f"Address: {session['data']['delivery_address']}\n"
    else:
        summary += "Pickup: Collect directly from the restaurant\n"
        summary += "Delivery: Included for now\n"
    summary += "-----------\n"
    summary += f"*Total: GHS {total:.2f}*\n\n"
    summary += "After payment, your OTP will be used to confirm handoff."

    buttons = [
        {"id": "proceed_payment", "title": "💳 Pay Now"},
        {"id": "checkout_back", "title": "🛒 Back to Cart"},
        {"id": "cancel_order", "title": "❌ Cancel"}
    ]
    success = cloud.send_interactive_buttons(phone, summary, buttons, header_text="Ready to Pay")
    if not success:
        cloud.send_whatsapp_message(phone, summary + "\n\n1. Pay Now\n2. Back to Cart\n3. Cancel")

def validate_order_request(order_data):
    cart_items = order_data.get("cart_items") or []
    if cart_items:
        seller_phone = normalize_phone(order_data["seller_phone"])
        running_total = 0
        for item in cart_items:
            product = get_product_details(item["product_id"])
            if not product:
                raise ValueError(f"{item.get('name', 'An item')} is no longer available.")
            if normalize_phone(product[0]) != seller_phone:
                raise ValueError("Cart items must come from one restaurant.")
            qty = int(item["qty"])
            if qty < 1 or qty > int(product[5]):
                raise ValueError(f"{product[2]} only has {product[5]} portion(s) left.")
            running_total += float(product[4]) * qty
        order_data["food_total"] = round(running_total, 2)
        return cart_items

    pid = order_data["selected_prod"]
    qty = int(order_data["qty"])
    product = get_product_by_id(pid)
    if not product:
        raise ValueError("Selected product no longer exists.")
    if qty < 1 or qty > product[4]:
        raise ValueError("Selected quantity exceeds available stock.")
    return [product]

def place_order_market(buyer_phone, order_data, status='pending'):
    validate_order_request(order_data)
    cart_items = order_data.get("cart_items") or []
    if not cart_items:
        product = get_product_details(order_data["selected_prod"])
        cart_items = [{
            "product_id": int(product[1]),
            "seller_phone": normalize_phone(product[0]),
            "shop_name": product[7] or "Restaurant",
            "name": product[2],
            "price": float(product[4]),
            "qty": int(order_data["qty"]),
            "addon_text": order_data.get("addon_text", ""),
            "instructions": order_data.get("instructions", ""),
        }]
        order_data["cart_items"] = cart_items

    seller_phone = normalize_phone(order_data["seller_phone"])
    delivery_fee = float(order_data.get("delivery_fee", 0) or 0)
    payment_ref = order_data.get("payment_ref", "")
    total_amount = float(order_data["food_total"]) + delivery_fee
    fulfillment_method = order_data.get("pickup_or_delivery", "delivery")
    delivery_zone = order_data.get("delivery_zone") or order_data.get("buyer_zone", "")
    delivery_landmark = order_data.get("delivery_landmark", "")
    delivery_address = order_data.get("delivery_address", "")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    order_columns = get_table_columns("orders")
    values = {
        "buyer_phone": normalize_phone(buyer_phone),
        "phone": normalize_phone(buyer_phone),
        "seller_phone": seller_phone,
        "total_price": total_amount,
        "total": total_amount,
        "delivery_fee": delivery_fee,
        "delivery_zone": delivery_zone,
        "delivery_landmark": delivery_landmark,
        "delivery_address": delivery_address,
        "pickup_or_delivery": fulfillment_method,
        "status": status,
        "payment_ref": payment_ref,
    }
    insert_columns = [column for column in values if column in order_columns]
    placeholders = ", ".join(["?"] * len(insert_columns))
    c.execute(
        f"INSERT INTO orders ({', '.join(insert_columns)}) VALUES ({placeholders})",
        tuple(values[column] for column in insert_columns)
    )
    order_id = c.lastrowid

    item_columns = get_table_columns("order_items")
    for item in cart_items:
        item_values = {
            "order_id": order_id,
            "product_id": int(item["product_id"]),
            "quantity": int(item["qty"]),
            "price_at_purchase": float(item["price"]),
            "item_name": item.get("name", ""),
            "addon_text": item.get("addon_text", ""),
            "special_instructions": item.get("instructions", ""),
        }
        item_insert_columns = [column for column in item_values if column in item_columns]
        item_placeholders = ", ".join(["?"] * len(item_insert_columns))
        c.execute(
            f"INSERT INTO order_items ({', '.join(item_insert_columns)}) VALUES ({item_placeholders})",
            tuple(item_values[column] for column in item_insert_columns)
        )
    conn.commit()
    conn.close()

    return order_id, seller_phone, total_amount

def notify_seller(order_id, buyer_phone, total, seller_phone, order_data=None):
    order_data = order_data or {}
    fulfilment = order_data.get("pickup_or_delivery", "delivery").capitalize()
    item_lines = []
    for item in order_data.get("cart_items", []):
        item_lines.append(f"• {item['name']} x{item['qty']}")
    seller_msg = f"🔔 *NEW ORDER!*\n\nOrder #{order_id}\nBuyer: {buyer_phone}\nTotal: GHS {total:.2f}\nMethod: {fulfilment}"
    if item_lines:
        seller_msg += f"\nItems:\n{chr(10).join(item_lines)}"
    if fulfilment.lower() == "delivery":
        seller_msg += f"\nZone: {order_data.get('delivery_zone', 'N/A')}"
        seller_msg += f"\nLandmark: {order_data.get('delivery_landmark', 'N/A')}"
        seller_msg += f"\nAddress: {order_data.get('delivery_address', 'N/A')}"
    seller_msg += "\n\nGo to Manage Orders to update the status."
    cloud.send_whatsapp_message(seller_phone, seller_msg)

def list_buyer_orders(phone, session=None):
    orders = get_buyer_orders(phone)
    if session is not None:
        set_reply_map(session, "buyer_orders_map", [order[0] for order in orders])
    return show_buyer_orders_menu(phone, orders)

def show_previous_buyer_listing(phone, session, user):
    previous_state = session["data"].get("product_return_state")
    if previous_state == "buyer_search_results":
        query = session["data"].get("search_query", "")
        results = search_market_catalog(query)
        set_reply_map(session, "buyer_search_map", [result[0] for result in results])
        if show_search_results(phone, results, query):
            return "buyer_search_results"
    selected_shop = session["data"].get("selected_shop")
    if selected_shop:
        products, _ = fetch_shop_catalog(selected_shop)
        set_reply_map(session, "buyer_products_map", [product[0] for product in products])
        show_catalog_buyer(phone, selected_shop)
        return "buyer_browsing"
    show_buyer_home(phone, user, session)
    return "buyer_menu"

def handle_buyer_flow(phone, text, session, user):
    state = session.get("state", "idle")
    text = (text or "").strip()
    text_lower = text.lower()
    session.setdefault("data", {})
    ensure_cart(session)

    if text_lower in {"hi", "menu", "home"} or state in {"idle", "start"}:
        show_buyer_home(phone, user, session)
        session["state"] = "buyer_menu"
        return

    if state == "buyer_menu":
        normalized_choice = {
            "1": "home_browse",
            "2": "home_search",
            "3": "home_orders",
            "4": "home_cart",
            "5": "home_profile",
            "6": "home_help",
            "browse": "home_browse",
            "buy": "home_browse",
            "search": "home_search",
            "orders": "home_orders",
            "cart": "home_cart",
            "profile": "home_profile",
            "help": "home_help",
        }.get(text_lower, text)

        if normalized_choice == "home_browse":
            shops = fetch_available_shops()
            set_reply_map(session, "buyer_shops_map", [shop[0] for shop in shops])
            show_shops_list(phone)
            session["state"] = "buyer_choosing_shop"
        elif normalized_choice == "home_search":
            cloud.send_whatsapp_message(phone, "🔎 Send a food name or restaurant keyword.\n\nExamples: jollof, pizza, waakye, campus grill")
            session["state"] = "buyer_search_query"
        elif normalized_choice == "home_orders":
            if list_buyer_orders(phone, session):
                session["state"] = "buyer_orders_list"
            else:
                session["state"] = "buyer_menu"
        elif normalized_choice == "home_cart":
            show_cart(phone, session)
            session["state"] = "buyer_cart"
        elif normalized_choice == "home_profile":
            show_buyer_profile(phone, user)
            session["state"] = "buyer_profile"
        elif normalized_choice == "home_help":
            show_buyer_help(phone)
            session["state"] = "buyer_help"
        else:
            cloud.send_whatsapp_message(phone, "❌ Choose Browse Food, Search, My Orders, View Cart, Buyer Details, or Help.")

    elif state == "buyer_help":
        if text in {"help_back", "1", "back"}:
            show_buyer_home(phone, user, session)
            session["state"] = "buyer_menu"
        else:
            cloud.send_whatsapp_message(phone, "❌ Type 1 or tap Back Home.")

    elif state == "buyer_profile":
        if text in {"profile_change_zone", "1"}:
            show_zone_picker(phone, "profile_zone", header_text="Change Zone")
            session["state"] = "buyer_profile_zone"
        elif text in {"profile_back", "2", "back"}:
            show_buyer_home(phone, user, session)
            session["state"] = "buyer_menu"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please choose Change Zone or Back Home.")

    elif state == "buyer_profile_zone":
        zone_name = resolve_zone_choice(text.replace("profile_zone_", "zone_"))
        if not zone_name:
            cloud.send_whatsapp_message(phone, "❌ Zone not found. Please tap a valid zone.")
            return
        update_user(phone, zone=zone_name)
        refreshed_user = get_user(phone)
        cloud.send_whatsapp_message(phone, f"✅ Your zone has been updated to *{zone_name}*.")
        show_buyer_profile(phone, refreshed_user)
        session["state"] = "buyer_profile"

    elif state == "buyer_choosing_shop":
        shops = fetch_available_shops()
        seller_phone = get_reply_map_value(session, "buyer_shops_map", text) or text
        try:
            idx = int(text) - 1
            if 0 <= idx < len(shops):
                seller_phone = shops[idx][0]
        except ValueError:
            pass

        seller = get_user(seller_phone)
        if seller and seller[USER_ROLE] == "seller":
            session["data"]["selected_shop"] = normalize_phone(seller_phone)
            products, _ = fetch_shop_catalog(seller_phone)
            set_reply_map(session, "buyer_products_map", [product[0] for product in products])
            show_catalog_buyer(phone, seller_phone)
            session["state"] = "buyer_browsing"
        else:
            cloud.send_whatsapp_message(phone, "❌ Restaurant not found. Choose a valid shop or type *menu*.")

    elif state == "buyer_search_query":
        if len(text) < 2:
            cloud.send_whatsapp_message(phone, "❌ Please send at least 2 letters to search.")
            return
        results = search_market_catalog(text)
        session["data"]["search_query"] = text
        set_reply_map(session, "buyer_search_map", [result[0] for result in results])
        if show_search_results(phone, results, text):
            session["state"] = "buyer_search_results"

    elif state == "buyer_search_results":
        raw_value = get_reply_map_value(session, "buyer_search_map", text) or text
        try:
            pid = int(str(raw_value).replace("search_prod_", "").replace("prod_", ""))
        except ValueError:
            cloud.send_whatsapp_message(phone, "❌ Please choose one of the search results.")
            return
        product = get_product_details(pid)
        if not product:
            cloud.send_whatsapp_message(phone, "❌ That item is no longer available.")
            return
        session["data"]["selected_prod"] = pid
        session["data"]["seller_phone"] = normalize_phone(product[0])
        session["data"]["product_return_state"] = "buyer_search_results"
        show_product_detail(phone, product)
        session["state"] = "buyer_product_detail"

    elif state == "buyer_browsing":
        raw_value = get_reply_map_value(session, "buyer_products_map", text) or text
        try:
            pid = int(str(raw_value).replace("prod_", ""))
        except ValueError:
            cloud.send_whatsapp_message(phone, "❌ Please select a food item from the menu.")
            return
        product = get_product_details(pid)
        if not product:
            cloud.send_whatsapp_message(phone, "❌ That item is no longer available.")
            return
        session["data"]["selected_prod"] = pid
        session["data"]["seller_phone"] = normalize_phone(product[0])
        session["data"]["product_return_state"] = "buyer_browsing"
        show_product_detail(phone, product)
        session["state"] = "buyer_product_detail"

    elif state == "buyer_product_detail":
        normalized_choice = {
            "1": "buyer_add_to_cart",
            "2": "buyer_buy_now",
            "3": "buyer_product_back",
            "back": "buyer_product_back",
        }.get(text_lower, text)

        if normalized_choice == "buyer_product_back":
            session["state"] = show_previous_buyer_listing(phone, session, user)
            return
        if normalized_choice not in {"buyer_add_to_cart", "buyer_buy_now"}:
            cloud.send_whatsapp_message(phone, "❌ Choose Add to Cart, Buy Now, or Back.")
            return

        product = get_product_details(session["data"].get("selected_prod"))
        if not product:
            cloud.send_whatsapp_message(phone, "❌ This product is no longer available.")
            session["state"] = show_previous_buyer_listing(phone, session, user)
            return

        session["data"]["post_qty_action"] = "checkout" if normalized_choice == "buyer_buy_now" else "cart"
        cloud.send_whatsapp_message(
            phone,
            f"How many *{product[2]}* would you like?\nPrice: GHS {product[4]:.2f} each\nAvailable now: {product[5]}"
        )
        session["state"] = "buyer_product_qty"

    elif state == "buyer_product_qty":
        try:
            qty = int(text)
        except ValueError:
            cloud.send_whatsapp_message(phone, "❌ Enter a whole number quantity.")
            return
        product = get_product_details(session["data"].get("selected_prod"))
        if not product:
            cloud.send_whatsapp_message(phone, "❌ This product is no longer available.")
            session["state"] = show_previous_buyer_listing(phone, session, user)
            return
        if qty < 1 or qty > int(product[5]):
            cloud.send_whatsapp_message(phone, f"❌ Only {product[5]} portion(s) of *{product[2]}* are available right now.")
            return
        session["data"]["pending_qty"] = qty
        cloud.send_whatsapp_message(phone, "➕ Add-ons or preferences?\n\nExample: extra shito, boiled egg, salad\nType *skip* if none.")
        session["state"] = "buyer_product_addons"

    elif state == "buyer_product_addons":
        session["data"]["addon_text"] = "" if text_lower == "skip" else truncate_text(text, 120)
        cloud.send_whatsapp_message(phone, "📝 Any special instructions?\n\nExample: less pepper, call on arrival\nType *skip* if none.")
        session["state"] = "buyer_product_instructions"

    elif state == "buyer_product_instructions":
        session["data"]["instructions"] = "" if text_lower == "skip" else truncate_text(text, 160)
        product = get_product_details(session["data"].get("selected_prod"))
        if not product:
            cloud.send_whatsapp_message(phone, "❌ This product is no longer available.")
            session["state"] = show_previous_buyer_listing(phone, session, user)
            return
        added, message = add_item_to_cart(
            session,
            product,
            int(session["data"].get("pending_qty", 1)),
            session["data"].get("addon_text", ""),
            session["data"].get("instructions", "")
        )
        cloud.send_whatsapp_message(phone, f"{'✅' if added else '❌'} {message}")
        if not added:
            session["state"] = "buyer_cart"
            show_cart(phone, session)
            return
        if session["data"].get("post_qty_action") == "checkout":
            if begin_cart_checkout(phone, session, user):
                session["state"] = "buyer_fulfillment_method"
            else:
                session["state"] = "buyer_cart"
        else:
            show_cart(phone, session)
            session["state"] = "buyer_cart"

    elif state == "buyer_cart":
        normalized_choice = {
            "1": "cart_checkout",
            "2": "cart_clear",
            "3": "cart_continue",
        }.get(text_lower, text)
        if normalized_choice == "cart_checkout":
            if begin_cart_checkout(phone, session, user):
                session["state"] = "buyer_fulfillment_method"
        elif normalized_choice == "cart_clear":
            session["cart"] = []
            clear_buyer_draft(session)
            cloud.send_whatsapp_message(phone, "✅ Your cart has been cleared.")
            show_buyer_home(phone, user, session)
            session["state"] = "buyer_menu"
        elif normalized_choice == "cart_continue":
            selected_shop = session["data"].get("selected_shop")
            if selected_shop:
                products, _ = fetch_shop_catalog(selected_shop)
                set_reply_map(session, "buyer_products_map", [product[0] for product in products])
                show_catalog_buyer(phone, selected_shop)
                session["state"] = "buyer_browsing"
            else:
                show_buyer_home(phone, user, session)
                session["state"] = "buyer_menu"
        else:
            cloud.send_whatsapp_message(phone, "❌ Choose Checkout, Clear Cart, or Shop More.")

    elif state == "buyer_orders_list":
        raw_value = get_reply_map_value(session, "buyer_orders_map", text) or text
        try:
            order_id = int(str(raw_value).replace("buyer_order_", ""))
        except ValueError:
            cloud.send_whatsapp_message(phone, "❌ Please choose one of your orders.")
            return
        order = get_buyer_order(order_id, phone)
        if not order:
            cloud.send_whatsapp_message(phone, "❌ Order not found.")
            return
        session["data"]["selected_order_id"] = order_id
        show_buyer_order_detail(phone, order)
        session["state"] = "buyer_order_actions"

    elif state == "buyer_order_actions":
        order_id = session["data"].get("selected_order_id")
        order = get_buyer_order(order_id, phone) if order_id else None
        if not order:
            cloud.send_whatsapp_message(phone, "❌ Order not found.")
            session["state"] = "buyer_menu"
            return
        normalized_choice = {
            "1": "buyer_order_confirm_otp" if order[9] == "on_the_way" and order[11] else "buyer_orders_back",
            "2": "buyer_orders_back",
            "back": "buyer_orders_back",
        }.get(text_lower, text)
        if normalized_choice == "buyer_order_confirm_otp":
            cloud.send_whatsapp_message(phone, "🔐 Enter the OTP for this order to confirm delivery.")
            session["state"] = "buyer_order_confirm_otp"
        elif normalized_choice == "buyer_orders_back":
            if list_buyer_orders(phone, session):
                session["state"] = "buyer_orders_list"
            else:
                session["state"] = "buyer_menu"
        else:
            cloud.send_whatsapp_message(phone, "❌ Choose Confirm OTP or Back.")

    elif state == "buyer_order_confirm_otp":
        order_id = session["data"].get("selected_order_id")
        order = get_buyer_order(order_id, phone) if order_id else None
        if not order:
            cloud.send_whatsapp_message(phone, "❌ Order not found.")
            session["state"] = "buyer_menu"
            return
        if text.strip().upper() != (order[11] or "").upper():
            cloud.send_whatsapp_message(phone, "❌ OTP does not match this order. Please try again.")
            return
        update_order_status(order_id, "delivered")
        cloud.send_whatsapp_message(phone, f"🎉 *Order #{order_id} Delivered*\n\nThanks for confirming delivery.")
        cloud.send_whatsapp_message(order[2], f"🎉 Buyer confirmed delivery for Order #{order_id}.")
        show_buyer_order_detail(phone, get_buyer_order(order_id, phone))
        session["state"] = "buyer_order_actions"

    elif state == "buyer_fulfillment_method":
        normalized_choice = {
            "1": "fulfillment_delivery",
            "2": "fulfillment_pickup",
            "3": "fulfillment_back_cart",
        }.get(text_lower, text)
        if normalized_choice == "fulfillment_pickup":
            session["data"]["pickup_or_delivery"] = "pickup"
            session["data"]["delivery_fee"] = 0
            session["data"]["delivery_zone"] = "Pickup at restaurant"
            session["data"]["delivery_landmark"] = session["data"].get("seller_landmark", "")
            session["data"]["delivery_address"] = "Pickup at restaurant"
            send_checkout_summary(phone, session)
            session["state"] = "buyer_confirm_order"
        elif normalized_choice == "fulfillment_delivery":
            session["data"]["pickup_or_delivery"] = "delivery"
            show_buyer_zone_picker(phone, session["data"].get("seller_zone"), session["data"].get("seller_landmark"))
            session["state"] = "buyer_delivery_zone"
        elif normalized_choice == "fulfillment_back_cart":
            show_cart(phone, session)
            session["state"] = "buyer_cart"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please choose Delivery, Pickup, or Back to Cart.")

    elif state == "buyer_delivery_zone":
        delivery_zone = resolve_zone_choice(text.replace("buyer_zone_", "zone_"))
        if not delivery_zone:
            cloud.send_whatsapp_message(phone, "❌ Zone not found. Please tap a valid delivery zone.")
            return
        session["data"]["delivery_zone"] = delivery_zone
        show_buyer_landmark_picker(phone, delivery_zone)
        session["state"] = "buyer_delivery_landmark"

    elif state == "buyer_delivery_landmark":
        delivery_zone = session["data"].get("delivery_zone")
        delivery_landmark = resolve_landmark_choice(delivery_zone, text.replace("buyer_landmark_", "landmark_"))
        if not delivery_landmark:
            cloud.send_whatsapp_message(phone, "❌ Landmark not found. Please tap a valid delivery landmark.")
            return
        session["data"]["delivery_landmark"] = delivery_landmark
        session["data"]["delivery_fee"] = calculate_delivery_fee(
            session["data"].get("seller_zone"),
            session["data"].get("seller_landmark"),
            delivery_zone,
            delivery_landmark
        )
        cloud.send_whatsapp_message(
            phone,
            f"🏠 *Delivery Address*\n\nZone: {delivery_zone}\nLandmark: {delivery_landmark}\nDelivery is free for now.\n\nSend your exact address.\nExample: Martina Hostel, Room 12 near the gate"
        )
        session["state"] = "buyer_delivery_address"

    elif state == "buyer_delivery_address":
        if len(text) < 5:
            cloud.send_whatsapp_message(phone, "❌ Please provide a more detailed address.")
            return
        session["data"]["delivery_address"] = text
        send_checkout_summary(phone, session)
        session["state"] = "buyer_confirm_order"

    elif state == "buyer_confirm_order":
        normalized_choice = {
            "1": "proceed_payment",
            "2": "checkout_back",
            "3": "cancel_order",
        }.get(text_lower, text)
        if normalized_choice == "checkout_back":
            show_cart(phone, session)
            session["state"] = "buyer_cart"
            return
        if normalized_choice == "cancel_order":
            clear_buyer_draft(session)
            cloud.send_whatsapp_message(phone, "❌ Checkout cancelled. Your cart is still saved.")
            show_cart(phone, session)
            session["state"] = "buyer_cart"
            return
        if normalized_choice != "proceed_payment":
            cloud.send_whatsapp_message(phone, "❌ Please choose Pay Now, Back to Cart, or Cancel.")
            return

        try:
            validate_order_request(session["data"])
        except ValueError as exc:
            cloud.send_whatsapp_message(phone, f"❌ {exc} Please review your cart and try again.")
            show_cart(phone, session)
            session["state"] = "buyer_cart"
            return

        payment_ref = f"ZC_{uuid.uuid4().hex[:8]}"
        session["data"]["payment_ref"] = payment_ref
        order_id, seller_phone, total = place_order_market(phone, session["data"], "awaiting_payment")
        payment_url = initiate_paystack_payment(phone, total, order_id, payment_ref)

        if payment_url:
            fulfilment = session["data"].get("pickup_or_delivery", "delivery").capitalize()
            msg = (
                f"💳 *Payment Required*\n\n"
                f"Order #{order_id}\n"
                f"Total: GHS {total:.2f}\n"
                f"Method: {fulfilment}\n"
            )
            if fulfilment.lower() == "delivery":
                msg += f"Zone: {session['data'].get('delivery_zone', 'N/A')}\n"
                msg += f"Landmark: {session['data'].get('delivery_landmark', 'N/A')}\n"
                msg += f"Address: {session['data'].get('delivery_address', 'N/A')}\n"
            msg += f"\nClick to pay:\n{payment_url}\n\n"
            msg += "After payment, your OTP will appear in My Orders."
            cloud.send_whatsapp_message(phone, msg)
        else:
            order_code = generate_order_code()
            update_order_status(order_id, "paid", order_code)
            notify_seller(order_id, phone, total, seller_phone, session["data"])
            cloud.send_whatsapp_message(
                phone,
                f"✅ *Order Confirmed!*\n\nOrder #{order_id}\nOTP: *{order_code}*\n\nOpen *My Orders* to track accepted, preparing, on the way, and delivered updates."
            )

        session["cart"] = []
        clear_buyer_draft(session)
        show_buyer_home(phone, user, session)
        session["state"] = "buyer_menu"
# =========================
# ADMIN FLOW
# =========================
def handle_admin_flow(phone, text, session):
    state = session.get("state", "idle")
    text_lower = text.lower().strip()
    admin_verified = session.get("data", {}).get("admin_verified")

    if state == "admin_auth_code":
        if text == ADMIN_ACCESS_CODE:
            session.setdefault("data", {})["admin_verified"] = True
            show_admin_panel(phone)
            session["state"] = "admin_menu"
        else:
            cloud.send_whatsapp_message(phone, "❌ Invalid admin access code. Please try again.")
        return

    if not admin_verified:
        session.setdefault("data", {})
        session["data"]["admin_verified"] = False
        cloud.send_whatsapp_message(phone, "🔐 Enter your admin access code.")
        session["state"] = "admin_auth_code"
        return

    if text_lower in {"admin", "menu", "home", "hi", "hello"} or state in {"idle", "start"}:
        show_admin_panel(phone)
        session["state"] = "admin_menu"
        return

    if state == "admin_menu":
        if text == "admin_users" or text == "1":
            list_all_users(phone)
        elif text == "admin_seller_requests" or text == "2":
            requests = get_pending_seller_requests()
            set_reply_map(session, "admin_seller_requests_map", [request_row[0] for request_row in requests])
            if show_admin_seller_requests(phone):
                session["state"] = "admin_seller_requests"
            else:
                session["state"] = "admin_menu"
            return
        elif text == "admin_register_seller" or text == "3":
            session.setdefault("data", {})["seller_form"] = new_admin_seller_form()
            show_admin_seller_form(phone, session["data"]["seller_form"])
            session["state"] = "admin_seller_form"
            return
        elif text == "admin_prods" or text == "4":
            list_all_products_admin(phone)
        elif text == "admin_orders" or text == "5":
            list_all_orders_admin(phone)
        elif text == "admin_stats" or text == "6":
            show_marketplace_stats(phone)
        else:
            cloud.send_whatsapp_message(phone, "❌ Invalid option.")
        session["state"] = "admin_menu"
        return

    if state == "admin_seller_requests":
        request_id = get_reply_map_value(session, "admin_seller_requests_map", text)
        if not request_id:
            try:
                request_id = int(str(text).replace("admin_request_", ""))
            except ValueError:
                request_id = None
        request_row = get_seller_request(request_id) if request_id else None
        if not request_row or request_row[8] != "pending":
            cloud.send_whatsapp_message(phone, "❌ Seller request not found.")
            return
        session.setdefault("data", {})["seller_form"] = seller_request_to_form(request_row)
        show_admin_seller_form(phone, session["data"]["seller_form"])
        session["state"] = "admin_seller_form"
        return

    if state == "admin_seller_form":
        form = session.setdefault("data", {}).setdefault("seller_form", new_admin_seller_form())
        normalized_choice = {
            "1": "admin_form_phone",
            "2": "admin_form_name",
            "3": "admin_form_shop",
            "4": "admin_form_desc",
            "5": "admin_form_image",
            "6": "admin_form_zone",
            "7": "admin_form_landmark",
            "8": "admin_form_review",
        }.get(text, text)
        if normalized_choice == "admin_form_phone":
            cloud.send_whatsapp_message(phone, "📱 *Seller Phone*\n\nEnter the seller's WhatsApp number.\nExample: 233599966902")
            session["state"] = "admin_seller_phone_input"
        elif normalized_choice == "admin_form_name":
            cloud.send_whatsapp_message(phone, "👤 *Seller Name*\n\nEnter the seller's full name.\nExample: Mary Mensah")
            session["state"] = "admin_seller_name_input"
        elif normalized_choice == "admin_form_shop":
            cloud.send_whatsapp_message(phone, "🏪 *Shop Name*\n\nEnter the restaurant or shop name.\nExample: Mary Kitchen")
            session["state"] = "admin_seller_shop_name_input"
        elif normalized_choice == "admin_form_desc":
            cloud.send_whatsapp_message(phone, "📝 *Shop Description*\n\nEnter a short description.\nExample: Home-style jollof, fried rice, and chicken")
            session["state"] = "admin_seller_shop_desc_input"
        elif normalized_choice == "admin_form_image":
            buttons = [
                {"id": "admin_image_device", "title": "📷 Upload Photo"},
                {"id": "admin_image_link", "title": "🔗 Add Link"},
                {"id": "admin_image_skip", "title": "➡️ Skip"}
            ]
            success = cloud.send_interactive_buttons(
                phone,
                "🖼️ *Shop Image*\n\nChoose how to add the shop image for this seller.",
                buttons,
                header_text="Seller Image"
            )
            if not success:
                cloud.send_whatsapp_message(phone, "1. Upload Photo from device\n2. Add Image Link\n3. Skip")
            session["state"] = "admin_seller_shop_image_choice"
        elif normalized_choice == "admin_form_zone":
            show_zone_picker(phone, "admin_zone", header_text="Seller Zone")
            session["state"] = "admin_seller_zone_select"
        elif normalized_choice == "admin_form_landmark":
            if not form.get("zone"):
                cloud.send_whatsapp_message(phone, "❌ Choose the seller zone first, then select a landmark.")
                show_admin_seller_form(phone, form)
            else:
                show_landmark_picker(phone, form["zone"], "admin_landmark", header_text="Seller Landmark")
                session["state"] = "admin_seller_landmark_select"
        elif normalized_choice == "admin_form_review":
            required = ["seller_phone", "seller_name", "shop_name", "shop_description", "zone", "landmark"]
            missing = [field for field in required if not form.get(field)]
            if missing:
                cloud.send_whatsapp_message(phone, "❌ Complete seller phone, seller name, shop name, description, zone, and landmark before review.")
                show_admin_seller_form(phone, form)
            else:
                show_admin_seller_review(phone, form)
                session["state"] = "admin_seller_review"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please tap one of the form fields.")
        return

    if state == "admin_seller_phone_input":
        seller_phone = normalize_phone(text)
        if not seller_phone.isdigit():
            cloud.send_whatsapp_message(phone, "❌ Please enter a valid phone number using digits only.")
            return
        session["data"]["seller_form"]["seller_phone"] = seller_phone
        show_admin_seller_form(phone, session["data"]["seller_form"])
        session["state"] = "admin_seller_form"
        return

    if state == "admin_seller_name_input":
        session["data"]["seller_form"]["seller_name"] = text
        show_admin_seller_form(phone, session["data"]["seller_form"])
        session["state"] = "admin_seller_form"
        return

    if state == "admin_seller_shop_name_input":
        session["data"]["seller_form"]["shop_name"] = text
        show_admin_seller_form(phone, session["data"]["seller_form"])
        session["state"] = "admin_seller_form"
        return

    if state == "admin_seller_shop_desc_input":
        session["data"]["seller_form"]["shop_description"] = text
        show_admin_seller_form(phone, session["data"]["seller_form"])
        session["state"] = "admin_seller_form"
        return

    if state == "admin_seller_shop_image_choice":
        if text in {"admin_image_device", "1"}:
            cloud.send_whatsapp_message(phone, "📷 Send the shop image from your device now.\n\nType *skip* to leave it blank.")
            session["state"] = "admin_seller_shop_image_upload"
            pending_image_id = session.get("pending_image_id")
            if pending_image_id and handle_admin_seller_image_upload(phone, session, pending_image_id):
                return
        elif text in {"admin_image_link", "2"}:
            cloud.send_whatsapp_message(phone, "🔗 Enter a public image URL.\nExample: https://example.com/shop.jpg\n\nType *skip* if you want to leave it blank.")
            session["state"] = "admin_seller_shop_image_input"
        elif text in {"admin_image_skip", "3", "skip"}:
            session["data"]["seller_form"]["shop_image_url"] = ""
            show_admin_seller_form(phone, session["data"]["seller_form"])
            session["state"] = "admin_seller_form"
        else:
            cloud.send_whatsapp_message(phone, "❌ Choose Upload Photo, Add Link, or Skip.")
        return

    if state == "admin_seller_shop_image_input":
        session["data"]["seller_form"]["shop_image_url"] = "" if text_lower == "skip" else text
        show_admin_seller_form(phone, session["data"]["seller_form"])
        session["state"] = "admin_seller_form"
        return

    if state == "admin_seller_shop_image_upload":
        if text_lower == "skip":
            clear_pending_media(session)
            session["data"]["seller_form"]["shop_image_url"] = ""
            show_admin_seller_form(phone, session["data"]["seller_form"])
            session["state"] = "admin_seller_form"
        else:
            cloud.send_whatsapp_message(phone, "📷 Send the shop image from your device, or type *skip*.")
        return

    if state == "admin_seller_zone_select":
        zone = resolve_zone_choice(text.replace("admin_zone_", "zone_"))
        if not zone:
            cloud.send_whatsapp_message(phone, "❌ Zone not found. Please tap a valid zone.")
            return
        session["data"]["seller_form"]["zone"] = zone
        session["data"]["seller_form"]["landmark"] = ""
        show_landmark_picker(phone, zone, "admin_landmark", header_text="Seller Landmark")
        session["state"] = "admin_seller_landmark_select"
        return

    if state == "admin_seller_landmark_select":
        zone = session["data"]["seller_form"].get("zone")
        landmark = resolve_landmark_choice(zone, text.replace("admin_landmark_", "landmark_"))
        if not landmark:
            cloud.send_whatsapp_message(phone, "❌ Landmark not found. Please tap a valid landmark.")
            return
        session["data"]["seller_form"]["landmark"] = landmark
        show_admin_seller_form(phone, session["data"]["seller_form"])
        session["state"] = "admin_seller_form"
        return

    if state == "admin_seller_review":
        if text in {"admin_activate_seller_profile", "admin_approve_seller_request"}:
            form = session["data"]["seller_form"]
            activate_seller_profile(form, phone)
            cloud.send_whatsapp_message(
                phone,
                f"✅ *Seller Registered*\n\n"
                f"Seller: {form['seller_name']}\n"
                f"Phone: {form['seller_phone']}\n"
                f"Shop: {form['shop_name']}\n"
                f"Zone: {form['zone']}\n"
                f"Landmark: {form['landmark']}\n"
                f"Image URL: {form['shop_image_url'] or 'Not set'}\n"
                "Seller account is now active."
            )
            session["data"]["seller_form"] = new_admin_seller_form()
            show_admin_panel(phone)
            session["state"] = "admin_menu"
        elif text == "admin_approve_seller_request":
            form = session["data"]["seller_form"]
            create_user(form["seller_phone"], form["seller_name"], "seller", form["zone"], form["landmark"])
            update_user(form["seller_phone"], role="seller", zone=form["zone"], landmark=form["landmark"])
            update_user_shop(
                form["seller_phone"],
                form["shop_name"],
                form["shop_description"],
                form["shop_image_url"],
                form["landmark"]
            )
            if form.get("request_id"):
                update_seller_request_status(form["request_id"], "approved", phone)
            cloud.send_whatsapp_message(
                form["seller_phone"],
                "✅ *Seller Approved*\n\nYour seller profile has been confirmed by ZanChop admin.\nType *menu* to open your seller dashboard."
            )
            cloud.send_whatsapp_message(
                phone,
                f"✅ *Seller Approved*\n\nSeller: {form['seller_name']}\nPhone: {form['seller_phone']}\nShop: {form['shop_name']}\nZone: {form['zone']}\nLandmark: {form['landmark']}"
            )
            session["data"]["seller_form"] = new_admin_seller_form()
            show_admin_panel(phone)
            session["state"] = "admin_menu"
        elif text == "admin_edit_seller_form":
            show_admin_seller_form(phone, session["data"]["seller_form"])
            session["state"] = "admin_seller_form"
        elif text == "admin_cancel_seller_form":
            session["data"]["seller_form"] = new_admin_seller_form()
            show_admin_panel(phone)
            session["state"] = "admin_menu"
        else:
            cloud.send_whatsapp_message(phone, "❌ Please tap Approve/Create, Edit Form, or Cancel.")
        return

def list_all_users(phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT name, phone, role FROM users ORDER BY created_at DESC")
    users = c.fetchall()
    conn.close()
    msg = "*Marketplace Users:*\n"
    for u in users:
        msg += f"• {u[0]} ({u[1]}) [{u[2]}]\n"
    send_text(phone, msg)
    show_admin_panel(phone)

def list_all_products_admin(phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, seller_phone FROM products")
    prods = c.fetchall()
    conn.close()
    msg = "*All Food Items:*\n"
    for p in prods:
        msg += f"• ID: {p[0]} | {p[1]} (Seller: {p[2]})\n"
    send_text(phone, msg)
    show_admin_panel(phone)

def list_all_orders_admin(phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT id, buyer_phone, seller_phone, total_price, status, pickup_or_delivery
        FROM ({get_orders_view_sql()}) AS orders_view
        ORDER BY created_at DESC
        LIMIT 20
    """)
    orders = c.fetchall()
    conn.close()
    if not orders:
        send_text(phone, "📭 No orders yet.")
        show_admin_panel(phone)
        return
    msg = "📦 *All Platform Orders:*\n\n"
    for o in orders:
        msg += f"#{o[0]}: {o[1]} -> {o[2]}\n   GHS {o[3]:.2f} [{o[4]} | {o[5]}]\n\n"
    send_text(phone, msg)
    show_admin_panel(phone)

def show_marketplace_stats(phone):
    """Show marketplace statistics."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Count users
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM users WHERE role = 'buyer'")
    buyers = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM users WHERE role = 'seller'")
    sellers = c.fetchone()[0]
    
    # Count products
    c.execute("SELECT COUNT(*) FROM products")
    products = c.fetchone()[0]
    
    # Count orders
    c.execute("SELECT COUNT(*) FROM orders")
    orders = c.fetchone()[0]
    
    c.execute(f"""
        SELECT COALESCE(SUM(total_price), 0)
        FROM ({get_orders_view_sql()}) AS orders_view
        WHERE status IN ('paid', 'accepted', 'preparing', 'on_the_way', 'delivered', 'completed')
    """)
    revenue = c.fetchone()[0] or 0
    
    conn.close()
    
    msg = f"""📊 *ZanChop UCC Stats*

👥 *Users:* {total_users}
   🛒 Buyers: {buyers}
   🍔 Sellers: {sellers}

🍔 *Products:* {products}
📦 *Orders:* {orders}

💰 *Paid Revenue:* GHS {revenue:.2f}"""
    send_text(phone, msg)
    show_admin_panel(phone)

def get_platform_snapshot():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE role = 'buyer'")
    buyers = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE role = 'seller'")
    sellers = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM products WHERE stock > 0")
    live_products = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders")
    orders = c.fetchone()[0]
    c.execute("""
        SELECT p.name, u.shop_name, p.price, p.stock
        FROM products p
        JOIN users u ON u.phone = p.seller_phone
        WHERE p.stock > 0
        ORDER BY p.stock DESC, p.id DESC
        LIMIT 4
    """)
    featured = c.fetchall()
    conn.close()
    return {
        "buyers": buyers,
        "sellers": sellers,
        "live_products": live_products,
        "orders": orders,
        "featured": featured,
    }

@app.route("/", methods=["GET"])
def landing_page():
    snapshot = get_platform_snapshot()
    cards = ""
    for item_name, shop_name, price, stock in snapshot["featured"]:
        cards += f"""
        <article class="card">
          <span class="pill">{escape(shop_name or 'Campus Kitchen')}</span>
          <h3>{escape(item_name)}</h3>
          <p>Fast ordering, pickup or delivery, and live stock visibility.</p>
          <div class="meta">GHS {price:.2f} · {int(stock)} left</div>
        </article>
        """
    if not cards:
        cards = """
        <article class="card">
          <span class="pill">ZanChop</span>
          <h3>Fresh orders, cleaner flow</h3>
          <p>Buyers can browse shops, confirm delivery, and pay securely from WhatsApp.</p>
          <div class="meta">Sellers manage menus, pricing, stock, and orders.</div>
        </article>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ZanChop UCC</title>
  <style>
    :root {{
      --bg: #f7f1e7;
      --ink: #152417;
      --muted: #536257;
      --accent: #d36a1c;
      --accent-2: #0f8f5f;
      --card: rgba(255,255,255,0.76);
      --line: rgba(21, 36, 23, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Trebuchet MS", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(211,106,28,0.16), transparent 28%),
        radial-gradient(circle at bottom right, rgba(15,143,95,0.18), transparent 26%),
        linear-gradient(135deg, #fbf6ee, #eef7f1);
    }}
    .hero {{
      position: relative;
      overflow: hidden;
      padding: 4.5rem 1.25rem 2.5rem;
    }}
    .shell {{
      width: min(1100px, 100%);
      margin: 0 auto;
    }}
    .brand {{
      display: inline-flex;
      align-items: center;
      gap: .7rem;
      padding: .55rem .9rem;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid var(--line);
      font-weight: 700;
      letter-spacing: .04em;
    }}
    .hero-grid {{
      display: grid;
      grid-template-columns: 1.2fr .9fr;
      gap: 1.5rem;
      align-items: center;
      margin-top: 1.5rem;
    }}
    h1 {{
      font-size: clamp(2.4rem, 6vw, 5.3rem);
      line-height: .96;
      margin: 0 0 1rem;
    }}
    .lead {{
      max-width: 42rem;
      color: var(--muted);
      font-size: 1.08rem;
      line-height: 1.7;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: .9rem;
      margin-top: 1.4rem;
    }}
    .btn {{
      text-decoration: none;
      color: white;
      background: linear-gradient(135deg, var(--accent), #ef8c3b);
      padding: .95rem 1.2rem;
      border-radius: 18px;
      box-shadow: 0 18px 40px rgba(211,106,28,0.18);
      font-weight: 700;
    }}
    .btn.secondary {{
      color: var(--ink);
      background: rgba(255,255,255,0.72);
      border: 1px solid var(--line);
      box-shadow: none;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 1rem;
    }}
    .stat, .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 26px;
      padding: 1.15rem;
      box-shadow: 0 18px 50px rgba(21, 36, 23, 0.08);
      backdrop-filter: blur(10px);
    }}
    .stat strong {{
      display: block;
      font-size: 2rem;
      margin-bottom: .25rem;
    }}
    .section {{
      padding: 0 1.25rem 4rem;
    }}
    .section h2 {{
      font-size: clamp(1.6rem, 3vw, 2.4rem);
      margin-bottom: 1rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 1rem;
    }}
    .pill {{
      display: inline-block;
      padding: .35rem .65rem;
      border-radius: 999px;
      background: rgba(15,143,95,0.12);
      color: var(--accent-2);
      font-size: .78rem;
      font-weight: 700;
      letter-spacing: .05em;
      text-transform: uppercase;
    }}
    .card h3 {{
      margin: .9rem 0 .4rem;
      font-size: 1.2rem;
    }}
    .card p, .stat span {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }}
    .meta {{
      margin-top: .9rem;
      font-weight: 700;
      color: var(--ink);
    }}
    .float {{
      position: absolute;
      border-radius: 999px;
      filter: blur(10px);
      opacity: .5;
      animation: drift 16s ease-in-out infinite alternate;
    }}
    .float.one {{ width: 14rem; height: 14rem; top: -2rem; right: 8%; background: rgba(211,106,28,0.14); }}
    .float.two {{ width: 10rem; height: 10rem; bottom: 10%; right: 24%; background: rgba(15,143,95,0.16); animation-delay: -5s; }}
    @keyframes drift {{
      from {{ transform: translate3d(0, 0, 0) scale(1); }}
      to {{ transform: translate3d(20px, 24px, 0) scale(1.16); }}
    }}
    @media (max-width: 900px) {{
      .hero-grid, .grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 560px) {{
      .stats {{ grid-template-columns: 1fr; }}
      .actions {{ flex-direction: column; }}
      .btn {{ text-align: center; }}
    }}
  </style>
</head>
<body>
  <section class="hero">
    <div class="float one"></div>
    <div class="float two"></div>
    <div class="shell">
      <span class="brand">ZanChop UCC</span>
      <div class="hero-grid">
        <div>
          <h1>Food ordering that actually feels fast.</h1>
          <p class="lead">ZanChop brings campus restaurants, pickup, delivery, Paystack checkout, and seller stock management into one cleaner WhatsApp-first system.</p>
          <div class="actions">
            <a class="btn" href="https://wa.me/{escape((ADMIN_PHONE or '').lstrip('+')) if ADMIN_PHONE else '#'}">Open WhatsApp</a>
            <a class="btn secondary" href="/payment/callback">Payment Flow</a>
          </div>
        </div>
        <div class="stats">
          <div class="stat"><strong>{snapshot["buyers"]}</strong><span>Active buyers</span></div>
          <div class="stat"><strong>{snapshot["sellers"]}</strong><span>Seller dashboards</span></div>
          <div class="stat"><strong>{snapshot["live_products"]}</strong><span>Live menu items</span></div>
          <div class="stat"><strong>{snapshot["orders"]}</strong><span>Total orders tracked</span></div>
        </div>
      </div>
    </div>
  </section>
  <section class="section">
    <div class="shell">
      <h2>Marketplace Highlights</h2>
      <div class="grid">
        {cards}
      </div>
    </div>
  </section>
</body>
</html>"""
    return html

# =========================
# RUN
# =========================
if __name__ == "__main__":
    print("=" * 50)
    print("🎓 ZanChop UCC - WhatsApp Marketplace")
    print("=" * 50)
    print(f"✅ Bot Name: ZanChop UCC")
    print(f"✅ Meta Cloud API: Enabled")
    print(f"✅ Webhook URL: /webhook")
    print(f"✅ Backend Port: 5000")
    print(f"✅ Database: prim_store.db")
    print("=" * 50)
    print("\n📱 Send a WhatsApp message to start!")
    print("🛒 Buyers: Type 'menu' to browse food")
    print("🍔 Sellers: Type 'menu' to manage your menu")
    print("\n" + "=" * 50)
    
    app.run(host="0.0.0.0", port=5000)

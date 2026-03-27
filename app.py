import os
import json
import logging
import sqlite3
import sys
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

os.makedirs(IMAGES_FOLDER, exist_ok=True)
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

    conn.commit()
    conn.close()

init_db()

# Cape Coast delivery zones and landmark suggestions
DELIVERY_ZONES = {
    "UCC Science / Main Campus": {
        "base_fee": 4.0,
        "rank": 0,
        "landmarks": [
            "Sam Jonah Library",
            "Science Market",
            "Sasakawa",
            "UCC Hospital",
            "CALC",
            "LLT",
            "School Junction"
        ]
    },
    "UCC North / Ayensu / Casford": {
        "base_fee": 4.5,
        "rank": 1,
        "landmarks": [
            "Ayensu",
            "Casford",
            "KNH",
            "Valco Hall",
            "Atlantic Hall",
            "Kakumdo",
            "SRC Junction"
        ]
    },
    "UCC South / Oguaa / Adehye": {
        "base_fee": 5.0,
        "rank": 1,
        "landmarks": [
            "Oguaa Hall",
            "Adehye Hall",
            "Old Site",
            "UCC Taxi Rank",
            "West Gate"
        ]
    },
    "Amamoma / Apewosika": {
        "base_fee": 5.5,
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
        "base_fee": 6.0,
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
        "base_fee": 6.5,
        "rank": 3,
        "landmarks": [
            "Cape Coast Technical University",
            "Pedu Junction",
            "Cape Coast Stadium",
            "Abura",
            "Adisadel"
        ]
    },
    "Kwaprow / Duakor / Ntsin": {
        "base_fee": 7.5,
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
def load_json(path, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path): return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return json.loads(content) if content else default
    except: return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Save error: {e}")

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
    if not seller_zone or not buyer_zone:
        return 6.0

    seller_meta = DELIVERY_ZONES.get(seller_zone)
    buyer_meta = DELIVERY_ZONES.get(buyer_zone)
    if not seller_meta or not buyer_meta:
        return 6.0

    average_base = (seller_meta["base_fee"] + buyer_meta["base_fee"]) / 2
    rank_gap = abs(seller_meta["rank"] - buyer_meta["rank"])
    fee = average_base + (rank_gap * 1.25)
    if seller_zone == buyer_zone:
        fee = max(3.5, average_base - 0.5)
    if seller_landmark and buyer_landmark and seller_landmark == buyer_landmark:
        fee = max(3.5, fee - 0.5)
    return round(fee, 2)

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
    return updated

def delete_product(pid, seller_phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id = ? AND seller_phone = ?", (pid, normalize_phone(seller_phone)))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()
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
                # Send acknowledgment
                cloud.send_whatsapp_message(from_phone, "📷 Image received! Processing...")
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
        for i, (zone, fee) in enumerate(UCC_ZONES.items(), 1):
            zones.append({"id": f"zone_{i}", "title": zone, "description": f"Delivery: GHS {fee}"})
        
        sections = [{"title": "Select Your Zone", "rows": zones}]
        cloud.send_interactive_list(
            phone, 
            f"✅ Great, {text}!\n\n📍 Which campus zone are you in?", 
            "Select Zone", 
            sections
        )
        session["state"] = "onboarding_zone"
        
    elif state == "onboarding_zone":
        zone_name = resolve_zone_choice(text)
        if zone_name:
            session["data"]["zone"] = zone_name
            
            buttons = [
                {"id": "role_buyer", "title": "🛒 I want to BUY"},
                {"id": "role_seller", "title": "🍔 I want to SELL"}
            ]
            cloud.send_interactive_buttons(
                phone, 
                f"📍 *Zone: {zone_name}*\n\nFinal step! How do you want to use ZanChop?", 
                buttons
            )
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
                "🔐 *Seller Access*\n\n"
                "Restaurant dashboards are activated with an admin-issued seller code.\n"
                "Please enter your *seller access code* to continue."
            )
            session["state"] = "onboarding_seller_code"
        else:
            finalize_onboarding(phone, session)

    elif state == "onboarding_seller_code":
        invite, error = claim_seller_invite(text.upper(), phone)
        if error:
            cloud.send_whatsapp_message(phone, f"❌ {error}\n\nPlease check the code and try again.")
            return

        session["data"]["role"] = "seller"
        session["data"]["name"] = invite[2] or session["data"].get("name")
        session["data"]["shop_name"] = invite[3]
        session["data"]["shop_desc"] = invite[4] or ""
        session["data"]["shop_image_url"] = invite[5] or ""
        session["data"]["zone"] = invite[6] or session["data"].get("zone")
        session["data"]["landmark"] = invite[7] or ""
        finalize_onboarding(phone, session, seller_code=invite[0])

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
        msg += "\n\nType *menu* to browse restaurants, choose pickup or delivery, and pay with Paystack."
    else:
        msg += "\n\nType *menu* to open your restaurant dashboard, add dishes, and manage orders."
    cloud.send_whatsapp_message(phone, msg)
    session["state"] = "idle"
    session["data"] = {}

def show_admin_panel(phone):
    rows = [
        {"id": "admin_users", "title": "Users", "description": "See all buyers and sellers"},
        {"id": "admin_register_seller", "title": "Register Seller", "description": "Create a restaurant access code"},
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
            "👑 *ZanChop Admin Panel*\n\nChoose an option:\n1. Users\n2. Register Seller\n3. Products\n4. Orders\n5. Stats"
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
        "landmark": ""
    }

def show_admin_seller_form(phone, form):
    rows = [
        {"id": "admin_form_phone", "title": "Seller Phone", "description": form["seller_phone"] or "Tap to add seller WhatsApp number"},
        {"id": "admin_form_name", "title": "Seller Name", "description": form["seller_name"] or "Tap to add seller name"},
        {"id": "admin_form_shop", "title": "Shop Name", "description": form["shop_name"] or "Tap to add restaurant name"},
        {"id": "admin_form_desc", "title": "Shop Description", "description": (form["shop_description"][:60] if form["shop_description"] else "Tap to add short description")},
        {"id": "admin_form_image", "title": "Image URL", "description": form["shop_image_url"] or "Tap to add shop image URL"},
        {"id": "admin_form_zone", "title": "Zone", "description": form["zone"] or "Tap to choose seller zone"},
        {"id": "admin_form_landmark", "title": "Landmark", "description": form["landmark"] or "Tap to choose seller landmark"},
        {"id": "admin_form_review", "title": "Review & Create", "description": "Generate seller access code"}
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
            "8. Review & Create"
        )
        cloud.send_whatsapp_message(phone, summary)

def show_zone_picker(phone, state_label, header_text="Choose Zone"):
    rows = []
    for index, (zone, fee) in enumerate(UCC_ZONES.items(), 1):
        rows.append({
            "id": f"{state_label}_{index}",
            "title": zone[:24],
            "description": f"Delivery fee GHS {fee:.2f}"
        })
    cloud.send_interactive_list(
        phone,
        "📍 Tap the zone that matches this seller.",
        "Select Zone",
        [{"title": "Available Zones", "rows": rows}],
        header_text=header_text
    )

def show_landmark_picker(phone, zone, state_label, header_text="Choose Landmark"):
    landmarks = get_landmarks_for_zone(zone)
    rows = []
    for index, landmark in enumerate(landmarks, 1):
        rows.append({
            "id": f"{state_label}_{index}",
            "title": landmark[:24],
            "description": f"Suggested under {zone}"
        })
    cloud.send_interactive_list(
        phone,
        f"📌 Tap the landmark that best describes the location in *{zone}*.",
        "Select Landmark",
        [{"title": "Suggested Landmarks", "rows": rows}],
        header_text=header_text
    )

def show_buyer_zone_picker(phone, seller_zone, seller_landmark):
    rows = []
    for index, (zone, meta) in enumerate(DELIVERY_ZONES.items(), 1):
        preview_landmarks = ", ".join(meta["landmarks"][:2])
        rows.append({
            "id": f"buyer_zone_{index}",
            "title": zone[:24],
            "description": f"{preview_landmarks}..."
        })
    cloud.send_interactive_list(
        phone,
        f"📍 *Choose Delivery Zone*\n\nSeller area: {seller_zone or 'Not set'}\nSeller landmark: {seller_landmark or 'Not set'}\n\nTap the zone closest to your delivery point.",
        "Select Zone",
        [{"title": "Cape Coast Delivery Zones", "rows": rows}],
        header_text="Delivery Zone"
    )

def show_buyer_landmark_picker(phone, zone):
    show_landmark_picker(phone, zone, "buyer_landmark", header_text="Delivery Landmark")

def show_admin_seller_review(phone, form):
    msg = (
        "🧾 *Review Seller Profile*\n\n"
        f"Seller Phone: {form['seller_phone']}\n"
        f"Seller Name: {form['seller_name']}\n"
        f"Shop Name: {form['shop_name']}\n"
        f"Description: {form['shop_description']}\n"
        f"Image URL: {form['shop_image_url'] or 'Not set'}\n"
        f"Zone: {form['zone']}\n\n"
        f"Landmark: {form['landmark']}\n\n"
        "Everything looks ready. Create the access code?"
    )
    buttons = [
        {"id": "admin_create_seller_code", "title": "✅ Create Code"},
        {"id": "admin_edit_seller_form", "title": "✏️ Edit Form"},
        {"id": "admin_cancel_seller_form", "title": "❌ Cancel"}
    ]
    cloud.send_interactive_buttons(phone, msg, buttons, header_text="Review Seller")

def show_seller_dashboard(phone, user):
    rows = [
        {"id": "seller_add", "title": "Add Food", "description": "Create a new menu item"},
        {"id": "seller_products", "title": "Manage Products", "description": "Edit or delete menu items"},
        {"id": "seller_orders", "title": "Manage Orders", "description": "Accept and complete customer orders"}
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
            "description": f"GHS {product[2]:.2f} | {(product[3] or 'No description')[:45]}"
        })
    return cloud.send_interactive_list(
        phone,
        "📋 *Manage Products*\n\nTap a product to edit or delete it.",
        "Select Product",
        [{"title": "Your Products", "rows": rows}],
        header_text="Product Manager"
    )

def show_seller_product_actions(phone, product):
    rows = [
        {"id": "seller_edit_name", "title": "Edit Name", "description": f"Current: {product[2][:45]}"},
        {"id": "seller_edit_desc", "title": "Edit Description", "description": (product[3] or "No description")[:55]},
        {"id": "seller_edit_price", "title": "Edit Price", "description": f"Current: GHS {product[4]:.2f}"},
        {"id": "seller_edit_image", "title": "Edit Image URL", "description": (product[6] or "No image set")[:55]},
        {"id": "seller_delete_product", "title": "Delete Product", "description": "Remove this product from your menu"},
        {"id": "seller_back_products", "title": "Back to Products", "description": "Return to your product list"}
    ]
    cloud.send_interactive_list(
        phone,
        f"🧾 *{product[2]}*\n\nPrice: GHS {product[4]:.2f}\nDescription: {product[3] or 'No description'}\nImage: {product[6] or 'Not set'}",
        "Choose Action",
        [{"title": "Product Actions", "rows": rows}],
        header_text="Edit Product"
    )

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
            "description": f"{order[1]} | GHS {order[2]:.2f} | {order[3]} | {order[6] or order[5] or order[4]}"
        })
    return cloud.send_interactive_list(
        phone,
        "📦 *Manage Orders*\n\nTap an order to update its status.",
        "Select Order",
        [{"title": "Recent Orders", "rows": rows}],
        header_text="Order Manager"
    )

def show_seller_order_actions(phone, order):
    order_id, buyer_phone, _, total_price, delivery_fee, delivery_zone, delivery_landmark, delivery_address, pickup_or_delivery, status, _, confirmation_code, created_at = order
    buttons = [{"id": "seller_orders_back", "title": "⬅️ Back"}]
    if status in {"paid", "pending"}:
        buttons.insert(0, {"id": "seller_accept_order", "title": "✅ Accept"})
    elif status == "accepted":
        buttons.insert(0, {"id": "seller_complete_order", "title": "📦 Complete"})

    summary = (
        f"📦 *Order #{order_id}*\n\n"
        f"Buyer: {buyer_phone}\n"
        f"Status: {status}\n"
        f"Method: {pickup_or_delivery}\n"
        f"Total: GHS {total_price:.2f}\n"
        f"Delivery Fee: GHS {delivery_fee:.2f}\n"
        f"Zone: {delivery_zone or 'N/A'}\n"
        f"Landmark: {delivery_landmark or 'N/A'}\n"
        f"Address: {delivery_address or 'N/A'}\n"
        f"Code: {confirmation_code or 'Pending payment'}"
    )
    cloud.send_interactive_buttons(phone, summary, buttons[:3], header_text="Order Actions")

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
            if show_seller_products_menu(phone, phone):
                session["state"] = "seller_products_list"
            else:
                session["state"] = "seller_menu"
        elif text == "seller_orders":
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
            buttons = [
                {"id": "seller_add_image_url", "title": "🔗 Add Image"},
                {"id": "seller_skip_image", "title": "➡️ Skip Image"},
                {"id": "seller_cancel_add", "title": "❌ Cancel"}
            ]
            cloud.send_interactive_buttons(
                phone,
                f"🖼️ *Image Setup*\n\nName: {session['data']['p_name']}\nPrice: GHS {price:.2f}\n\nWould you like to attach an image URL?",
                buttons,
                header_text="Product Image"
            )
            session["state"] = "seller_add_image_choice"
        except ValueError:
            send_text(phone, "Invalid price. Please enter a valid number greater than 0.")

    elif state == "seller_add_image_choice":
        if text == "seller_add_image_url":
            cloud.send_whatsapp_message(phone, "🔗 Send the public image URL.\nExample: https://example.com/jollof.jpg")
            session["state"] = "seller_add_image_url"
        elif text == "seller_skip_image":
            add_product_db(phone, session["data"]["p_name"], session["data"].get("p_desc", ""), session["data"]["p_price"], 1, "")
            send_text(phone, f"✅ *{session['data']['p_name']}* added to your menu.")
            session["state"] = "idle"
            session["data"] = {}
        elif text == "seller_cancel_add":
            send_text(phone, "❌ Product creation cancelled.")
            session["state"] = "idle"
            session["data"] = {}
        else:
            cloud.send_whatsapp_message(phone, "❌ Please tap Add Image, Skip Image, or Cancel.")

    elif state == "seller_add_image_url":
        add_product_db(phone, session["data"]["p_name"], session["data"].get("p_desc", ""), session["data"]["p_price"], 1, text)
        send_text(phone, f"✅ *{session['data']['p_name']}* added to your menu with an image.")
        session["state"] = "idle"
        session["data"] = {}

    elif state == "seller_products_list":
        if text.startswith("seller_prod_"):
            pid = int(text.split("_")[-1])
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

        if text == "seller_edit_name":
            cloud.send_whatsapp_message(phone, f"✏️ Enter the new product name.\nCurrent: {product[2]}")
            session["state"] = "seller_edit_name"
        elif text == "seller_edit_desc":
            cloud.send_whatsapp_message(phone, f"📝 Enter the new description.\nCurrent: {product[3] or 'No description'}\n\nType *skip* to clear it.")
            session["state"] = "seller_edit_desc"
        elif text == "seller_edit_price":
            cloud.send_whatsapp_message(phone, f"💵 Enter the new price.\nCurrent: GHS {product[4]:.2f}")
            session["state"] = "seller_edit_price"
        elif text == "seller_edit_image":
            cloud.send_whatsapp_message(phone, f"🔗 Enter the new image URL.\nCurrent: {product[6] or 'Not set'}\n\nType *skip* to remove it.")
            session["state"] = "seller_edit_image"
        elif text == "seller_delete_product":
            buttons = [
                {"id": "seller_confirm_delete", "title": "🗑️ Delete"},
                {"id": "seller_cancel_delete", "title": "⬅️ Keep Item"}
            ]
            cloud.send_interactive_buttons(
                phone,
                f"Delete *{product[2]}* from your menu?",
                buttons,
                header_text="Confirm Delete"
            )
            session["state"] = "seller_delete_confirm"
        elif text == "seller_back_products":
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

    elif state == "seller_edit_image":
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
        if text.startswith("seller_order_"):
            order_id = int(text.split("_")[-1])
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

        if text == "seller_accept_order":
            update_order_status(order_id, "accepted")
            buyer_msg = f"✅ *Order #{order_id} Accepted*\n\nYour restaurant has started preparing your order."
            cloud.send_whatsapp_message(order[1], buyer_msg)
            send_text(phone, f"✅ Order #{order_id} marked as accepted.")
            show_seller_order_actions(phone, get_seller_order(order_id, phone))
        elif text == "seller_complete_order":
            update_order_status(order_id, "completed")
            buyer_msg = f"🎉 *Order #{order_id} Completed*\n\nThanks for ordering with ZanChop. Confirmation code: {order[11] or 'N/A'}"
            cloud.send_whatsapp_message(order[1], buyer_msg)
            send_text(phone, f"✅ Order #{order_id} marked as completed.")
            show_seller_order_actions(phone, get_seller_order(order_id, phone))
        elif text == "seller_orders_back":
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
            show_shops_list(phone)
            session["state"] = "buyer_choosing_shop"
        elif text == "orders" or text == "2":
            list_buyer_orders(phone)
            session["state"] = "idle"
        elif text == "profile" or text == "3":
            show_buyer_profile(phone, user)
            session["state"] = "idle"
        else:
            cloud.send_whatsapp_message(phone, "❌ Invalid option. Please tap a button.")

    elif state == "buyer_choosing_shop":
        # Handle shop selection - try phone number or numeric index
        seller_phone = text
        
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
            show_catalog_buyer(phone, seller_phone)
            session["state"] = "buyer_browsing"
        else:
            cloud.send_whatsapp_message(phone, "❌ Shop not found. Please try again or type 'menu'.")
            
    elif state == "buyer_browsing":
        try:
            # Handle list ID like "prod_5" or numeric input
            pid_str = text.replace("prod_", "")
            pid = int(pid_str)
            prod = get_product_by_id(pid)
            if prod:
                session["data"]["selected_prod"] = pid
                session["data"]["seller_phone"] = prod[0]
                cloud.send_whatsapp_message(phone, f"How many *{prod[2]}* would you like to order?\n*Price: GHS {prod[3]:.2f} each*")
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
            cloud.send_interactive_buttons(
                phone,
                f"Nice choice.\n\nItem total: GHS {food_total:.2f}\nRestaurant area: {seller_zone or 'Not set'}\nRestaurant landmark: {seller_landmark or 'Not set'}\n\nHow would you like to receive your order?",
                buttons,
                header_text="Choose Fulfilment"
            )
            session["state"] = "buyer_fulfillment_method"
        except ValueError:
            cloud.send_whatsapp_message(phone, "Invalid quantity. Please enter a number.")

    elif state == "buyer_fulfillment_method":
        if text == "fulfillment_pickup":
            session["data"]["pickup_or_delivery"] = "pickup"
            session["data"]["delivery_fee"] = 0
            session["data"]["delivery_zone"] = "Pickup at restaurant"
            session["data"]["delivery_address"] = "Pickup at restaurant"
            send_checkout_summary(phone, session)
            session["state"] = "buyer_confirm_order"
        elif text == "fulfillment_delivery":
            session["data"]["pickup_or_delivery"] = "delivery"
            show_buyer_zone_picker(phone, session["data"].get("seller_zone"), session["data"].get("seller_landmark"))
            session["state"] = "buyer_delivery_zone"
        elif text == "cancel_order":
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
        cloud.send_whatsapp_message(phone, f"🏠 *Delivery Address*\n\nZone: {delivery_zone}\nLandmark: {delivery_landmark}\nEstimated delivery fee: GHS {session['data']['delivery_fee']:.2f}\n\nPlease provide your specific location details:\n*Example: Martina Hostel, Room 12 near the gate*")
        session["state"] = "buyer_delivery_address"
    
    elif state == "buyer_delivery_address":
        if len(text) < 5:
            cloud.send_whatsapp_message(phone, "❌ Please provide a more detailed address.")
            return
        
        session["data"]["delivery_address"] = text
        send_checkout_summary(phone, session)
        session["state"] = "buyer_confirm_order"

    elif state == "buyer_confirm_order":
        if text == "proceed_payment":
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
        elif text == "cancel_order":
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
        summary += f"Delivery ({session['data']['delivery_zone']}): GHS {delivery_fee:.2f}\n"
        summary += f"Landmark: {session['data'].get('delivery_landmark', 'N/A')}\n"
        summary += f"Address: {session['data']['delivery_address']}\n"
    else:
        summary += "Pickup: Collect directly from the restaurant\n"
        summary += "Delivery Fee: GHS 0.00\n"
    summary += "-----------\n"
    summary += f"*Total: GHS {total:.2f}*\n\n"
    summary += "Proceed to payment:"

    buttons = [
        {"id": "proceed_payment", "title": "💳 Pay Now"},
        {"id": "cancel_order", "title": "❌ Cancel"}
    ]
    cloud.send_interactive_buttons(phone, summary, buttons, header_text="Ready to Pay")

def show_shops_list(phone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT u.phone, u.shop_name, u.shop_description
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
    
    if not shops:
        cloud.send_whatsapp_message(phone, "🍴 *No Shops Available*\n\nSorry, there are no food vendors listed yet. Type 'menu' to go back.")
    else:
        sections = []
        rows = []
        for s in shops:
            rows.append({
                "id": s[0], # phone number is the ID
                "title": s[1], # Shop Name
                "description": s[2][:72] if s[2] else "" # Shop Description
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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, price, description, image_url FROM products WHERE seller_phone = ? AND stock > 0", (normalized_seller_phone,))
    prods = c.fetchall()
    
    c.execute("SELECT shop_name FROM users WHERE phone = ?", (normalized_seller_phone,))
    shop = c.fetchone()
    shop_name = shop[0] if shop and shop[0] else "Shop"
    conn.close()

    if not prods:
        cloud.send_whatsapp_message(phone, f"🍴 *{shop_name}* has no items available right now. Type 'menu' to go back.")
    else:
        sections = []
        rows = []
        for p in prods:
            rows.append({
                "id": f"prod_{p[0]}",
                "title": p[1],
                "description": f"GHS {p[2]:.2f} - {p[3][:50] if p[3] else ''}"
            })
        sections.append({"title": "Available Items", "rows": rows})
        
        success = cloud.send_interactive_list(
            phone,
            f"🍴 *{shop_name} Menu*\n\nSelect an item to order:",
            "Select Item",
            sections
        )
        
        if not success:
            # Fallback to text message
            msg = f"🍴 *{shop_name} Menu:*\n\n"
            for i, p in enumerate(prods, 1):
                msg += f"{i}. *{p[1]}* - GHS {p[2]:.2f}\n"
                if p[3]:
                    msg += f"   {p[3][:50]}\n"
            msg += f"\n*Reply with item number (1-{len(prods)})*"
            cloud.send_whatsapp_message(phone, msg)

def place_order_market(buyer_phone, order_data, status='pending'):
    pid = order_data["selected_prod"]
    seller_phone = normalize_phone(order_data["seller_phone"])
    qty = order_data["qty"]
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
        return "No reference provided", 400
    
    # Verify payment
    payment_success = verify_paystack_payment(reference)
    
    if payment_success:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(f"""
            SELECT id, buyer_phone, seller_phone, total_price, pickup_or_delivery, delivery_zone, delivery_landmark, delivery_address
            FROM ({get_orders_view_sql()}) AS orders_view
            WHERE payment_ref = ?
            LIMIT 1
        """, (reference,))
        order = c.fetchone()
        conn.close()
        
        if order:
            order_id, buyer_phone, seller_phone, total, pickup_or_delivery, delivery_zone, delivery_landmark, delivery_address = order
            
            confirmation_code = generate_order_code()
            update_order_status(order_id, 'paid', confirmation_code)
            
            buyer_msg = f"✅ *Payment Successful!*\n\n"
            buyer_msg += f"Order #{order_id}\n"
            buyer_msg += f"Amount Paid: GHS {total:.2f}\n\n"
            buyer_msg += f"🎫 *Confirmation Code: {confirmation_code}*\n\n"
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
            
            return f"Payment successful! Your order code is: {confirmation_code}", 200
        else:
            return "Order not found", 404
    else:
        return "Payment verification failed", 400

@app.route("/payment/webhook", methods=["POST"])
def paystack_webhook():
    """Handle Paystack webhook for payment notifications"""
    data = request.get_json()
    
    if data.get("event") == "charge.success":
        reference = data.get("data", {}).get("reference")
        if reference:
            verify_paystack_payment(reference)
    
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
        elif text == "admin_register_seller" or text == "2":
            session.setdefault("data", {})["seller_form"] = new_admin_seller_form()
            show_admin_seller_form(phone, session["data"]["seller_form"])
            session["state"] = "admin_seller_form"
            return
        elif text == "admin_prods" or text == "3":
            list_all_products_admin(phone)
        elif text == "admin_orders" or text == "4":
            list_all_orders_admin(phone)
        elif text == "admin_stats" or text == "5":
            show_marketplace_stats(phone)
        else:
            cloud.send_whatsapp_message(phone, "❌ Invalid option.")
        session["state"] = "admin_menu"
        return

    if state == "admin_seller_form":
        form = session.setdefault("data", {}).setdefault("seller_form", new_admin_seller_form())
        if text == "admin_form_phone":
            cloud.send_whatsapp_message(phone, "📱 *Seller Phone*\n\nEnter the seller's WhatsApp number.\nExample: 233599966902")
            session["state"] = "admin_seller_phone_input"
        elif text == "admin_form_name":
            cloud.send_whatsapp_message(phone, "👤 *Seller Name*\n\nEnter the seller's full name.\nExample: Mary Mensah")
            session["state"] = "admin_seller_name_input"
        elif text == "admin_form_shop":
            cloud.send_whatsapp_message(phone, "🏪 *Shop Name*\n\nEnter the restaurant or shop name.\nExample: Mary Kitchen")
            session["state"] = "admin_seller_shop_name_input"
        elif text == "admin_form_desc":
            cloud.send_whatsapp_message(phone, "📝 *Shop Description*\n\nEnter a short description.\nExample: Home-style jollof, fried rice, and chicken")
            session["state"] = "admin_seller_shop_desc_input"
        elif text == "admin_form_image":
            cloud.send_whatsapp_message(phone, "🖼️ *Shop Image URL*\n\nEnter a public image URL.\nExample: https://example.com/shop.jpg\n\nType *skip* if you want to leave it blank.")
            session["state"] = "admin_seller_shop_image_input"
        elif text == "admin_form_zone":
            show_zone_picker(phone, "admin_zone", header_text="Seller Zone")
            session["state"] = "admin_seller_zone_select"
        elif text == "admin_form_landmark":
            if not form.get("zone"):
                cloud.send_whatsapp_message(phone, "❌ Choose the seller zone first, then select a landmark.")
                show_admin_seller_form(phone, form)
            else:
                show_landmark_picker(phone, form["zone"], "admin_landmark", header_text="Seller Landmark")
                session["state"] = "admin_seller_landmark_select"
        elif text == "admin_form_review":
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

    if state == "admin_seller_shop_image_input":
        session["data"]["seller_form"]["shop_image_url"] = "" if text_lower == "skip" else text
        show_admin_seller_form(phone, session["data"]["seller_form"])
        session["state"] = "admin_seller_form"
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
        if text == "admin_create_seller_code":
            form = session["data"]["seller_form"]
            code = create_seller_invite(
                form["seller_phone"],
                form["seller_name"],
                form["shop_name"],
                form["shop_description"],
                form["shop_image_url"],
                form["zone"],
                form["landmark"],
                phone
            )
            cloud.send_whatsapp_message(
                phone,
                f"✅ *Seller Registered*\n\n"
                f"Seller: {form['seller_name']}\n"
                f"Phone: {form['seller_phone']}\n"
                f"Shop: {form['shop_name']}\n"
                f"Zone: {form['zone']}\n"
                f"Landmark: {form['landmark']}\n"
                f"Image URL: {form['shop_image_url'] or 'Not set'}\n"
                f"Access Code: *{code}*\n\n"
                "Send this code to the seller. They can choose seller in the bot and activate their dashboard with it."
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
            cloud.send_whatsapp_message(phone, "❌ Please tap Create Code, Edit Form, or Cancel.")
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
        WHERE status IN ('paid', 'completed')
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

import json
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "prim_store.db"
SESSIONS_FILE = BASE_DIR / "sessions.json"

TABLES = [
    "order_items",
    "orders",
    "products",
    "seller_invites",
    "seller_requests",
    "users",
]


def main():
    if DB_FILE.exists():
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        for table in TABLES:
            cur.execute(f"DELETE FROM {table}")
        cur.execute("DELETE FROM sqlite_sequence")
        conn.commit()
        conn.close()

    SESSIONS_FILE.write_text(json.dumps({}, indent=2), encoding="utf-8")
    print("Development data cleared.")
    print(f"Database: {DB_FILE}")
    print(f"Sessions: {SESSIONS_FILE}")


if __name__ == "__main__":
    main()

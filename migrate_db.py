import sqlite3
import os

DB_FILE = "prim_store.db"

def migrate():
    if not os.path.exists(DB_FILE):
        print(f"❌ Error: {DB_FILE} does not exist.")
        return

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    print("🔄 Starting Migration: Adding shop_name and shop_description to users table...")
    
    # Check if shop_name column exists
    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    
    if "shop_name" not in columns:
        try:
            c.execute("ALTER TABLE users ADD COLUMN shop_name TEXT")
            print("✅ Column 'shop_name' added.")
        except sqlite3.OperationalError as e:
            print(f"⚠️ Error adding shop_name: {e}")
    else:
        print("ℹ️ Column 'shop_name' already exists.")

    if "shop_description" not in columns:
        try:
            c.execute("ALTER TABLE users ADD COLUMN shop_description TEXT")
            print("✅ Column 'shop_description' added.")
        except sqlite3.OperationalError as e:
            print(f"⚠️ Error adding shop_description: {e}")
    else:
        print("ℹ️ Column 'shop_description' already exists.")

    conn.commit()
    conn.close()
    print("🎊 Migration Complete!")

if __name__ == "__main__":
    migrate()

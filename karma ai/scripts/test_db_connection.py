"""Self-service script to verify the Supabase POSTGRES_URL and catalog data."""
import sys
from urllib.parse import urlparse

import psycopg2
from dotenv import load_dotenv
import os

load_dotenv()

url = os.environ.get("POSTGRES_URL")
if not url:
    print("[Karma DB] ERROR: POSTGRES_URL is not set in .env")
    sys.exit(1)

host = urlparse(url).hostname or "<unknown>"
print(f"[Karma DB] Connecting to host: {host}")

try:
    conn = psycopg2.connect(dsn=url)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    print(f"[Karma DB] Connection OK — host: {host}")

    cur.execute(
        "SELECT category, COUNT(*), MIN(price_inr) FROM catalog GROUP BY category ORDER BY category"
    )
    rows = cur.fetchall()
    if rows:
        print(f"\n{'Category':<30} {'Count':>6} {'Min price (INR)':>16}")
        print("-" * 56)
        for category, count, min_price in rows:
            print(f"{category:<30} {count:>6} {min_price:>16}")
    else:
        print("[Karma DB] WARNING: catalog table is empty — run the seed script.")

    cur.close()
    conn.close()
except Exception as e:
    print(
        f"\n[Karma DB] Cannot connect to Postgres. Your POSTGRES_URL may be using the "
        f"legacy Supabase direct host (db.<ref>.supabase.co) which has been retired.\n"
        f"Update it to the Session Pooler URL from: "
        f"Supabase Dashboard → Connect → Session pooler.\n"
        f"Current host: {host}\n"
        f"Raw error: {e}"
    )
    sys.exit(1)

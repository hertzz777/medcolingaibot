import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")

UNLIMITED_PRODUCTS = ("full_30d", "lifetime", "cardio_module")


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS entitlements (
                user_id BIGINT,
                product TEXT,
                charge_id TEXT UNIQUE,
                granted_at TIMESTAMPTZ DEFAULT now(),
                expires_at TIMESTAMPTZ,
                PRIMARY KEY (user_id, product)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id BIGINT,
                usage_date DATE,
                count INT NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            )
        """)


def grant_entitlement(user_id: int, product: str, charge_id: str):
    expires_at = "now() + interval '30 days'" if product == "full_30d" else "NULL"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO entitlements (user_id, product, charge_id, expires_at)
            VALUES (%s, %s, %s, {expires_at})
            ON CONFLICT (charge_id) DO NOTHING
        """, (user_id, product, charge_id))


def has_access(user_id: int, product: str) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS(
                SELECT 1 FROM entitlements
                WHERE user_id = %s AND product = %s
                  AND (expires_at IS NULL OR expires_at > now())
            )
        """, (user_id, product))
        return cur.fetchone()[0]


def has_unlimited_access(user_id: int) -> bool:
    return any(has_access(user_id, product) for product in UNLIMITED_PRODUCTS)


def try_consume_daily_quiz(user_id: int, limit: int = 1) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO daily_usage (user_id, usage_date, count)
            VALUES (%s, CURRENT_DATE, 1)
            ON CONFLICT (user_id, usage_date)
            DO UPDATE SET count = daily_usage.count + 1
            RETURNING count
        """, (user_id,))
        count = cur.fetchone()[0]
        return count <= limit

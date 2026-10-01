import psycopg2
import psycopg2.extras
from flask import g
import config


def get_db():
    """Return a request-scoped PostgreSQL connection with dict-like rows."""
    if "db" not in g:
        g.db = psycopg2.connect(
            host=config.DB_HOST,
            port=config.DB_PORT,
            dbname=config.DB_NAME,
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=None, fetch="all"):
    """Run a SELECT and return rows (list of dicts, one dict, or None)."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        if fetch == "all":
            return cur.fetchall()
        if fetch == "one":
            return cur.fetchone()
        return None


def execute(sql, params=None, returning=None):
    """Run an INSERT/UPDATE/DELETE, commit, and optionally return a value."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        result = cur.fetchone() if returning else None
    conn.commit()
    return result[returning] if result else None
import os
import psycopg2
from flask import g

def get_db():
    if 'db' not in g:
        # 1. Try DATABASE_URL first (Render's internal connection string)
        db_url = os.environ.get('DATABASE_URL')
        
        if db_url:
            # Fix for SQLAlchemy/psycopg2 URL format if it starts with postgres://
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)
            g.db = psycopg2.connect(db_url)
        else:
            # 2. Fall back to individual variables
            g.db = psycopg2.connect(
                host=os.environ.get('DB_HOST'),
                database=os.environ.get('DB_NAME'),
                user=os.environ.get('DB_USER'),
                password=os.environ.get('DB_PASSWORD'),
                port=os.environ.get('DB_PORT', '5432')
            )
    return g.db
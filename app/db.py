import os
import secrets
import sqlite3
import hashlib
import hmac
from datetime import date, datetime, timezone

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "paypilot").strip()
DB_PATH = os.getenv("DB_PATH", "paypilot.db")
PAID = {"paid", "marked_as_paid"}


def _mongo_database():
    from pymongo import MongoClient

    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")
    return client[MONGODB_DB]


def _mongo_id(collection, sequence_name):
    from pymongo import ReturnDocument

    result = collection.find_one_and_update(
        {"_id": sequence_name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result["value"]


def conn():
    if MONGODB_URI:
        raise RuntimeError("MongoDB uses its document adapter, not SQL connections")
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    if MONGODB_URI:
        database = _mongo_database()
        database.users.create_index("email", unique=True)
        database.sessions.create_index("token_hash", unique=True)
        database.invoices.create_index([("user_id", 1), ("id", -1)])
        database.invoices.create_index("paypal_id")
        database.webhook_events.create_index("event_id", unique=True)
        return
    with conn() as c:
        if DATABASE_URL:
            c.execute(
                """CREATE TABLE IF NOT EXISTS invoices(
            id BIGSERIAL PRIMARY KEY,
            paypal_id TEXT, number TEXT,
            client_name TEXT, client_email TEXT, description TEXT,
            amount NUMERIC(12, 2), currency TEXT, due_date DATE,
            status TEXT DEFAULT 'sent', reminders INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)"""
            )
        else:
            c.execute(
                """CREATE TABLE IF NOT EXISTS invoices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paypal_id TEXT, number TEXT,
            client_name TEXT, client_email TEXT, description TEXT,
            amount REAL, currency TEXT, due_date TEXT,
            status TEXT DEFAULT 'sent', reminders INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
            )
        if DATABASE_URL:
            c.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS user_id BIGINT")
        else:
            columns = {row[1] for row in c.execute("PRAGMA table_info(invoices)")}
            if "user_id" not in columns:
                c.execute("ALTER TABLE invoices ADD COLUMN user_id INTEGER")
        c.execute(
            """CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
            if not DATABASE_URL
            else """CREATE TABLE IF NOT EXISTS users(
            id BIGSERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions(
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL)"""
            if DATABASE_URL
            else """CREATE TABLE IF NOT EXISTS sessions(
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL)"""
        )
        if DATABASE_URL:
            c.execute(
                """ALTER TABLE sessions
                ALTER COLUMN expires_at TYPE TIMESTAMPTZ
                USING expires_at::timestamptz"""
            )
        c.execute(
            """CREATE TABLE IF NOT EXISTS webhook_events(
            event_id TEXT PRIMARY KEY,
            received_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
        )


def _view(row):
    d = dict(row)
    d.pop("_id", None)
    if hasattr(d.get("due_date"), "isoformat"):
        d["due_date"] = d["due_date"].isoformat()
    if hasattr(d.get("created_at"), "isoformat"):
        d["created_at"] = d["created_at"].isoformat(sep=" ")
    s = (d["status"] or "").lower()
    if s in PAID:
        d["display_status"] = "paid"
    elif s in {"cancelled", "refunded"}:
        d["display_status"] = s
    elif d["due_date"] and d["due_date"] < date.today().isoformat():
        d["display_status"] = "overdue"
    else:
        d["display_status"] = "sent"
    return d


def add(**f):
    if MONGODB_URI:
        database = _mongo_database()
        invoice = {
            "id": _mongo_id(database.counters, "invoice_id"),
            **f,
            "status": "sent",
            "reminders": 0,
            "created_at": datetime.now(timezone.utc),
        }
        database.invoices.insert_one(invoice)
        return invoice["id"]
    with conn() as c:
        if DATABASE_URL:
            cur = c.execute(
                """INSERT INTO invoices(paypal_id, number, client_name, client_email,
            description, amount, currency, due_date, status, user_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'sent',%s)
            RETURNING id""",
                (
                    f["paypal_id"],
                    f["number"],
                    f["client_name"],
                    f["client_email"],
                    f["description"],
                    f["amount"],
                    f["currency"],
                    f["due_date"],
                    f["user_id"],
                ),
            )
            return cur.fetchone()["id"]
        cur = c.execute(
            """INSERT INTO invoices(paypal_id, number, client_name, client_email,
            description, amount, currency, due_date, status, user_id)
            VALUES(:paypal_id,:number,:client_name,:client_email,
            :description,:amount,:currency,:due_date,'sent',:user_id)""",
            f,
        )
        return cur.lastrowid


def _password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 310_000
    ).hex()
    return f"pbkdf2_sha256$310000${salt}${digest}"


def _password_valid(password, encoded):
    try:
        _, rounds, salt, expected = encoded.split("$")
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(rounds)
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_user(email, password):
    if MONGODB_URI:
        database = _mongo_database()
        try:
            user = {
                "id": _mongo_id(database.counters, "user_id"),
                "email": email,
                "password_hash": _password_hash(password),
                "created_at": datetime.now(timezone.utc),
            }
            database.users.insert_one(user)
            return {"id": user["id"], "email": user["email"]}
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                return None
            raise
    with conn() as c:
        placeholder = "%s" if DATABASE_URL else "?"
        try:
            if DATABASE_URL:
                row = c.execute(
                    f"INSERT INTO users(email,password_hash) VALUES({placeholder},{placeholder}) RETURNING id,email",
                    (email, _password_hash(password)),
                ).fetchone()
            else:
                cur = c.execute(
                    "INSERT INTO users(email,password_hash) VALUES(?,?)",
                    (email, _password_hash(password)),
                )
                row = {"id": cur.lastrowid, "email": email}
            return dict(row)
        except Exception as exc:
            if "unique" in str(exc).lower():
                return None
            raise


def authenticate(email, password):
    if MONGODB_URI:
        user = _mongo_database().users.find_one({"email": email})
        if not user or not _password_valid(password, user["password_hash"]):
            return None
        return {"id": user["id"], "email": user["email"]}
    with conn() as c:
        placeholder = "%s" if DATABASE_URL else "?"
        row = c.execute(
            f"SELECT id,email,password_hash FROM users WHERE email={placeholder}",
            (email,),
        ).fetchone()
    if not row or not _password_valid(password, row["password_hash"]):
        return None
    return {"id": row["id"], "email": row["email"]}


def create_session(user_id, expires_at):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if MONGODB_URI:
        _mongo_database().sessions.insert_one(
            {
                "token_hash": token_hash,
                "user_id": user_id,
                "expires_at": datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                ),
            }
        )
        return token
    with conn() as c:
        placeholder = "%s" if DATABASE_URL else "?"
        if DATABASE_URL:
            expires_at = expires_at.replace("Z", "+00:00")
        c.execute(
            f"INSERT INTO sessions(token_hash,user_id,expires_at) VALUES({placeholder},{placeholder},{placeholder})",
            (token_hash, user_id, expires_at),
        )
    return token


def session_user(token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if MONGODB_URI:
        database = _mongo_database()
        session = database.sessions.find_one(
            {
                "token_hash": token_hash,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            }
        )
        user = (
            database.users.find_one({"id": session["user_id"]})
            if session
            else None
        )
        return {"id": user["id"], "email": user["email"]} if user else None
    with conn() as c:
        placeholder = "%s" if DATABASE_URL else "?"
        row = c.execute(
            f"""SELECT u.id,u.email FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash={placeholder} AND s.expires_at > CURRENT_TIMESTAMP""",
            (token_hash,),
        ).fetchone()
    return dict(row) if row else None


def all_invoices(user_id=None):
    if MONGODB_URI:
        query = {"user_id": user_id} if user_id is not None else {}
        rows = _mongo_database().invoices.find(query).sort("id", -1)
        return [_view(r) for r in rows]
    with conn() as c:
        if user_id is None:
            rows = c.execute("SELECT * FROM invoices ORDER BY id DESC")
        else:
            placeholder = "%s" if DATABASE_URL else "?"
            rows = c.execute(
                f"SELECT * FROM invoices WHERE user_id={placeholder} ORDER BY id DESC",
                (user_id,),
            )
        return [_view(r) for r in rows]


def claim_unowned_invoices(user_id):
    if MONGODB_URI:
        result = _mongo_database().invoices.update_many(
            {"user_id": None}, {"$set": {"user_id": user_id}}
        )
        return result.modified_count
    with conn() as c:
        if DATABASE_URL:
            cur = c.execute(
                "UPDATE invoices SET user_id=%s WHERE user_id IS NULL",
                (user_id,),
            )
        else:
            cur = c.execute(
                "UPDATE invoices SET user_id=? WHERE user_id IS NULL",
                (user_id,),
            )
        return cur.rowcount


def get(inv_id, user_id=None):
    if MONGODB_URI:
        query = {"id": inv_id}
        if user_id is not None:
            query["user_id"] = user_id
        row = _mongo_database().invoices.find_one(query)
        return _view(row) if row else None
    with conn() as c:
        placeholder = "%s" if DATABASE_URL else "?"
        query = f"SELECT * FROM invoices WHERE id={placeholder}"
        params = [inv_id]
        if user_id is not None:
            query += f" AND user_id={placeholder}"
            params.append(user_id)
        r = c.execute(query, params).fetchone()
        return _view(r) if r else None


def by_paypal_id(pid):
    if MONGODB_URI:
        row = _mongo_database().invoices.find_one({"paypal_id": pid})
        return _view(row) if row else None
    with conn() as c:
        placeholder = "%s" if DATABASE_URL else "?"
        r = c.execute(
            f"SELECT * FROM invoices WHERE paypal_id={placeholder}", (pid,)
        ).fetchone()
        return _view(r) if r else None


def set_status(inv_id, status):
    if MONGODB_URI:
        _mongo_database().invoices.update_one(
            {"id": inv_id}, {"$set": {"status": status.lower()}}
        )
        return
    with conn() as c:
        placeholder = "%s" if DATABASE_URL else "?"
        c.execute(
            f"UPDATE invoices SET status={placeholder} WHERE id={placeholder}",
            (status.lower(), inv_id),
        )


def bump_reminder(inv_id):
    if MONGODB_URI:
        _mongo_database().invoices.update_one(
            {"id": inv_id}, {"$inc": {"reminders": 1}}
        )
        return
    with conn() as c:
        placeholder = "%s" if DATABASE_URL else "?"
        c.execute(
            f"UPDATE invoices SET reminders=reminders+1 WHERE id={placeholder}",
            (inv_id,),
        )


def record_webhook_event(event_id):
    if not event_id:
        return False
    if MONGODB_URI:
        try:
            _mongo_database().webhook_events.insert_one(
                {"event_id": event_id, "received_at": datetime.now(timezone.utc)}
            )
            return True
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                return False
            raise
    with conn() as c:
        if DATABASE_URL:
            cur = c.execute(
                "INSERT INTO webhook_events(event_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (event_id,),
            )
        else:
            cur = c.execute(
                "INSERT OR IGNORE INTO webhook_events(event_id) VALUES(?)",
                (event_id,),
            )
        return cur.rowcount == 1

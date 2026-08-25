"""SQLite 数据存储：账号、会话与参数预设。"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid

import config

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
DB_FILE = os.path.join(DATA_DIR, "app.db")


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def _connect():
    _ensure_dirs()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                model TEXT,
                params TEXT,
                messages TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
                ON sessions(username, updated_at DESC);
        """)


_init_db()


def _new_id():
    return uuid.uuid4().hex


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000)
    return salt, digest.hex()


def _migrate_legacy_sessions(username):
    if not os.path.isdir(SESSIONS_DIR):
        return
    with _connect() as conn:
        for name in os.listdir(SESSIONS_DIR):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, name), "r", encoding="utf-8") as f:
                    session = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, username, title, created_at, updated_at, model, params, messages)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session["id"], username, session.get("title", "未命名"),
                 session.get("created_at", time.time()),
                 session.get("updated_at", session.get("created_at", time.time())),
                 session.get("model"), json.dumps(session.get("params"), ensure_ascii=False),
                 json.dumps(session.get("messages", []), ensure_ascii=False)),
            )


def create_user(username, password):
    salt, password_hash = _hash_password(password)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users(username, salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, salt, password_hash, time.time()),
            )
    except sqlite3.IntegrityError:
        return False
    _migrate_legacy_sessions(username)
    return True


def verify_user(username, password):
    with _connect() as conn:
        user = conn.execute("SELECT salt, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        return False
    _, password_hash = _hash_password(password, user["salt"])
    return hmac.compare_digest(password_hash, user["password_hash"])


def change_password(username, current_password, new_password):
    with _connect() as conn:
        user = conn.execute("SELECT salt, password_hash FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            return False
        _, current_hash = _hash_password(current_password, user["salt"])
        if not hmac.compare_digest(current_hash, user["password_hash"]):
            return False
        salt, password_hash = _hash_password(new_password)
        conn.execute("UPDATE users SET salt = ?, password_hash = ? WHERE username = ?", (salt, password_hash, username))
    return True


def _row_to_session(row):
    return {"id": row["id"], "title": row["title"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "model": row["model"],
            "params": json.loads(row["params"]) if row["params"] else None,
            "messages": json.loads(row["messages"])}


def create_session(username, title=None, model=None, params=None):
    now = time.time()
    session = {"id": _new_id(), "title": title or "会话 " + time.strftime("%m-%d %H:%M"),
               "created_at": now, "updated_at": now, "model": model, "params": params, "messages": []}
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, username, title, created_at, updated_at, model, params, messages) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], username, session["title"], now, now, model,
             json.dumps(params, ensure_ascii=False), json.dumps([])),
        )
    return session


def get_session(username, sid):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ? AND username = ?", (sid, username)).fetchone()
    return _row_to_session(row) if row else None


def _save_session(username, session):
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ?, model = ?, params = ?, messages = ? WHERE id = ? AND username = ?",
            (session["title"], session.get("updated_at", time.time()), session.get("model"),
             json.dumps(session.get("params"), ensure_ascii=False),
             json.dumps(session.get("messages", []), ensure_ascii=False), session["id"], username),
        )


def update_session_config(username, sid, model=None, params=None):
    session = get_session(username, sid)
    if session is None:
        return None
    if model is not None:
        session["model"] = model
    if params is not None:
        session["params"] = params
    _save_session(username, session)
    return session


def list_sessions(username):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, model, created_at, updated_at, messages FROM sessions WHERE username = ? ORDER BY updated_at DESC",
            (username,),
        ).fetchall()
    return [{"id": row["id"], "title": row["title"], "model": row["model"],
             "created_at": row["created_at"], "updated_at": row["updated_at"],
             "message_count": len(json.loads(row["messages"]))} for row in rows]


def delete_session(username, sid):
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ? AND username = ?", (sid, username))


def rename_session(username, sid, title):
    session = get_session(username, sid)
    if session is None:
        return None
    session["title"] = title
    _save_session(username, session)
    return session


def _is_user_configured(session):
    params = session.get("params") or {}
    return any(params.get(key) != default for key, default in config.DEFAULT_PARAMS.items())


def cleanup_empty_sessions(username, exclude_id=None):
    removed = []
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE username = ?", (username,)).fetchall()
        for row in rows:
            session = _row_to_session(row)
            if session["id"] != exclude_id and not session["messages"] and not _is_user_configured(session):
                conn.execute("DELETE FROM sessions WHERE id = ? AND username = ?", (session["id"], username))
                removed.append(session["id"])
    return removed


def delete_message(username, sid, index):
    session = get_session(username, sid)
    if session is None:
        return None
    indices = [i for i, message in enumerate(session["messages"]) if message.get("role") != "system"]
    if index < 0 or index >= len(indices):
        return None
    del session["messages"][indices[index]]
    session["updated_at"] = time.time()
    _save_session(username, session)
    return session


def append_message(username, sid, role, content, reasoning=None, files=None):
    session = get_session(username, sid) or create_session(username)
    message = {"role": role, "content": content, "ts": time.time()}
    if reasoning is not None:
        message["reasoning"] = reasoning
    if files is not None:
        message["files"] = [{"name": f.get("name", ""), "content": f.get("content", "")} for f in files]
    session["messages"].append(message)
    session["updated_at"] = time.time()
    _save_session(username, session)
    return session


def _read_presets():
    if not os.path.exists(PRESETS_FILE):
        return []
    with open(PRESETS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_presets(presets):
    _ensure_dirs()
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


def list_presets():
    return _read_presets()


def save_preset(name, params):
    presets = _read_presets()
    for preset in presets:
        if preset["name"] == name:
            preset["params"] = params
            _write_presets(presets)
            return preset
    preset = {"name": name, "params": params}
    presets.append(preset)
    _write_presets(presets)
    return preset


def delete_preset(name):
    _write_presets([preset for preset in _read_presets() if preset["name"] != name])
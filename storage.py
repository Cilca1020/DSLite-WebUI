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
                messages TEXT NOT NULL,
                auto_title_generated INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
                ON sessions(username, updated_at DESC);
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "auto_title_generated" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN auto_title_generated INTEGER NOT NULL DEFAULT 0")
        if "vm" not in columns:
            # 向量记忆设置（每会话独立，JSON: {"enabled": bool, "model": str, "recent_n": int}）
            conn.execute("ALTER TABLE sessions ADD COLUMN vm TEXT")


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


def _parse_vm(raw):
    """规范化向量记忆设置：{"enabled": bool, "model": str|None, "recent_n": int}。
    接收 dict 或 JSON 字符串或 None；缺省为新会话默认（关闭、无模型、N=默认）。"""
    default = {
        "enabled": False,
        "model": None,
        "recent_n": config.VECTOR_MEMORY_RECENT_N,
    }
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return default
    if not isinstance(raw, dict):
        return default
    try:
        recent_n = max(1, min(int(raw.get("recent_n") or default["recent_n"]), 1000))
    except (TypeError, ValueError):
        recent_n = default["recent_n"]
    return {
        "enabled": bool(raw.get("enabled", False)),
        "model": raw.get("model") or None,
        "recent_n": recent_n,
    }


def _row_to_session(row):
    return {"id": row["id"], "title": row["title"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "model": row["model"],
            "params": json.loads(row["params"]) if row["params"] else None,
            "vm": _parse_vm(row["vm"]),
            "messages": json.loads(row["messages"])}


def create_session(username, title=None, model=None, params=None, vm=None):
    now = time.time()
    session = {"id": _new_id(), "title": title or "会话 " + time.strftime("%m-%d %H:%M"),
               "created_at": now, "updated_at": now, "model": model, "params": params,
               "vm": _parse_vm(vm), "messages": []}
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, username, title, created_at, updated_at, model, params, messages, vm) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], username, session["title"], now, now, model,
             json.dumps(params, ensure_ascii=False), json.dumps([]),
             json.dumps(session["vm"], ensure_ascii=False)),
        )
    return session


def get_session(username, sid):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ? AND username = ?", (sid, username)).fetchone()
    return _row_to_session(row) if row else None


def get_session_messages(username, sid, limit=30, before=None):
    """分页读取会话消息（长对话按需加载更早的消息）。

    before 为全局消息下标锚点：返回下标 < before 的最早 limit 条；
    不传时返回最新的 limit 条。消息列表按时间升序（最早在前）。
    返回 dict（含分页信息）或 None（会话不存在）。
    返回的 messages 每项带全局 index（会话内 user/assistant 消息从 0 起的下标，
    与 delete_message 的删除下标一致），供前端渲染与后续加载定位。
    """
    session = get_session(username, sid)
    if session is None:
        return None
    chat = [m for m in session["messages"] if m.get("role") != "system"]
    total = len(chat)
    if before is None:
        end = total
    else:
        try:
            end = max(0, min(int(before), total))
        except (TypeError, ValueError):
            end = total
    start = max(0, end - limit)
    page = chat[start:end]
    return {
        "id": session["id"],
        "title": session["title"],
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "model": session["model"],
        "params": session["params"],
        "vm": session.get("vm", _parse_vm(None)),
        "messages": [{"index": start + i, **m} for i, m in enumerate(page)],
        "total": total,
        "has_more": start > 0,
    }


def _save_session(username, session):
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ?, model = ?, params = ?, messages = ?, vm = ? WHERE id = ? AND username = ?",
            (session["title"], session.get("updated_at", time.time()), session.get("model"),
             json.dumps(session.get("params"), ensure_ascii=False),
             json.dumps(session.get("messages", []), ensure_ascii=False),
             json.dumps(session.get("vm", _parse_vm(None)), ensure_ascii=False),
             session["id"], username),
        )


def set_auto_title(username, sid, title):
    title = title.strip()[:10]
    if not title:
        return None
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE sessions SET title = ?, auto_title_generated = 1 WHERE id = ? AND username = ? AND auto_title_generated = 0",
            (title, sid, username),
        )
        if cursor.rowcount != 1:
            return None
    return get_session(username, sid)


def auto_title_status(username, sid):
    with _connect() as conn:
        row = conn.execute(
            "SELECT title, auto_title_generated FROM sessions WHERE id = ? AND username = ?",
            (sid, username),
        ).fetchone()
    if not row:
        return None
    return {"title": row["title"], "generated": bool(row["auto_title_generated"])}


def update_session_config(username, sid, model=None, params=None, vm=None):
    session = get_session(username, sid)
    if session is None:
        return None
    if model is not None:
        session["model"] = model
    if params is not None:
        session["params"] = params
    if vm is not None:
        session["vm"] = _parse_vm(vm)
    _save_session(username, session)
    return session


def import_session(username, data):
    """按导出 JSON 的结构新建一个完整会话（标题、模型、参数、时间、消息）。
    返回新会话；vm 缺省为关闭，与前端导入按钮对应导出文件结构。
    """
    now = time.time()
    try:
        created_at = float(data.get("created_at") or now)
    except (TypeError, ValueError):
        created_at = now
    try:
        updated_at = float(data.get("updated_at") or created_at or now)
    except (TypeError, ValueError):
        updated_at = created_at
    title = data.get("title") or "会话 " + time.strftime("%m-%d %H:%M")
    messages = [m for m in (data.get("messages") or []) if m.get("role") in ("user", "assistant")]
    session = {
        "id": _new_id(),
        "title": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "model": data.get("model"),
        "params": data.get("params"),
        "vm": _parse_vm(data.get("vm")),
        "messages": messages,
    }
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, username, title, created_at, updated_at, model, params, messages, vm) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], username, title, created_at, updated_at, session["model"],
             json.dumps(session["params"], ensure_ascii=False),
             json.dumps(messages, ensure_ascii=False),
             json.dumps(session["vm"], ensure_ascii=False)),
        )
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
    # 仅按推理参数判断是否「已配置」；向量记忆设置（vm）不参与，
    # 因此只有向量设置、无消息的空会话仍会被 cleanup_empty_sessions 清理。
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


def append_message(username, sid, role, content, reasoning=None, files=None, interrupted=None):
    session = get_session(username, sid) or create_session(username)
    message = {"role": role, "content": content, "ts": time.time()}
    if reasoning is not None:
        message["reasoning"] = reasoning
    if files is not None:
        message["files"] = [{"name": f.get("name", ""), "content": f.get("content", "")} for f in files]
    if interrupted is not None:
        message["interrupted"] = bool(interrupted)
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
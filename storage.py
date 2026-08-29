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
    # timeout 让写操作在锁冲突时等待（而非立刻抛 database is locked），
    # 缓解记忆维护后台线程与请求读库/写库的并发冲突。
    conn = sqlite3.connect(DB_FILE, timeout=10)
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
            CREATE TABLE IF NOT EXISTS character_cards (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "auto_title_generated" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN auto_title_generated INTEGER NOT NULL DEFAULT 0")
        if "vm" not in columns:
            # 向量记忆设置（每会话独立，JSON: {"enabled": bool, "model": str, "recent_n": int}）
            conn.execute("ALTER TABLE sessions ADD COLUMN vm TEXT")
        if "memory" not in columns:
            # 四层记忆（每会话独立，JSON）：card(核心设定卡) / facts(动态关键事实) /
            # summary(剧情摘要) / vector(向量记忆设置，迁移自旧 vm 字段)
            conn.execute("ALTER TABLE sessions ADD COLUMN memory TEXT")
            # 老会话：把已有的 vm 迁移进 memory.vector，避免新面板丢配置
            _migrate_vm_to_memory(conn)


def _migrate_vm_to_memory(conn):
    """把已有的 vm 字段迁移进 memory.vector（老会话数据兼容，不丢配置）。"""
    rows = conn.execute(
        "SELECT id, vm FROM sessions WHERE vm IS NOT NULL AND vm != '' AND "
        "(memory IS NULL OR memory = '')"
    ).fetchall()
    for row in rows:
        try:
            vm = json.loads(row["vm"])
        except (TypeError, ValueError):
            continue
        if not isinstance(vm, dict):
            continue
        memory = {"card": None, "facts": [], "summary": None, "vector": _parse_vm(vm)}
        conn.execute(
            "UPDATE sessions SET memory = ? WHERE id = ?",
            (json.dumps(memory, ensure_ascii=False), row["id"]),
        )


def _default_memory():
    """会话四层记忆的默认结构。"""
    return {
        "card": None,      # 核心设定卡 {"content","source","updated_at"}
        "facts": [],       # 动态关键事实 [{"text","ts"}]
        "summary": None,   # 剧情摘要 {"text","summarized_ts","last_round"}
        "vector": _parse_vm(None),  # 向量记忆设置（迁移自旧 vm）
    }


def _parse_memory(raw):
    """规范化四层记忆结构。接收 dict 或 JSON 字符串或 None；缺省返回默认结构。"""
    mem = _default_memory()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return mem
    if not isinstance(raw, dict):
        return mem
    mem["card"] = raw.get("card") if isinstance(raw.get("card"), dict) else None
    if not isinstance(raw.get("facts"), list):
        mem["facts"] = []
    else:
        mem["facts"] = [
            {"text": str(f.get("text", "")).strip(), "ts": float(f.get("ts", 0))}
            for f in raw["facts"] if isinstance(f, dict) and str(f.get("text", "")).strip()
        ]
    s = raw.get("summary")
    mem["summary"] = _parse_summary(s) if s is not None else None
    mem["vector"] = _parse_vm(raw.get("vector")) if isinstance(raw.get("vector"), dict) else _parse_vm(raw.get("vm"))
    return mem


def _parse_summary(s):
    """规范化剧情摘要结构。接收 dict 或 None；缺省返回带默认配置的结构。

    结构：{text, summarized_ts, last_round, slice_rounds, auto_rounds}
    slice_rounds / auto_rounds 为用户可设置的触发参数，缺省回退到 config 默认。
    """
    default = {
        "text": "",
        "summarized_ts": None,
        "last_round": 0,
        "slice_rounds": config.SUMMARY_SLICE_ROUNDS,
        "auto_rounds": config.SUMMARY_AUTO_ROUNDS,
    }
    if not isinstance(s, dict):
        return default
    out = dict(default)
    out["text"] = str(s.get("text") or "").strip()
    try:
        out["summarized_ts"] = float(s["summarized_ts"]) if s.get("summarized_ts") is not None else None
    except (TypeError, ValueError):
        out["summarized_ts"] = None
    try:
        out["last_round"] = int(s.get("last_round") or 0)
    except (TypeError, ValueError):
        out["last_round"] = 0
    # 切片宽度：>=1，上限给个安全值
    try:
        v = int(s.get("slice_rounds"))
        out["slice_rounds"] = max(1, min(v, 200))
    except (TypeError, ValueError):
        pass  # 保留默认
    # 自动触发阈值：>=0（0 表示仅手动）
    try:
        v = int(s.get("auto_rounds"))
        out["auto_rounds"] = max(0, min(v, 1000))
    except (TypeError, ValueError):
        pass  # 保留默认
    return out


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
    """规范化向量记忆设置。

    结构：{"enabled": bool, "model": str|None, "recent_n": int, "top_k": int|None}。
    top_k 语义：None=未设置（用默认 config.VECTOR_MEMORY_TOP_K）；0=不限制（自动召回）；>0=固定 N 条。
    接收 dict 或 JSON 字符串或 None；缺省为新会话默认（关闭、无模型、N=默认）。
    """
    default = {
        "enabled": False,
        "model": None,
        "recent_n": config.VECTOR_MEMORY_RECENT_N,
        "top_k": None,
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
    # top_k：必须区分「未提供(None)」与「设置为0(不限制)」。
    # 只有当键存在且值非空时才算显式设置。
    top_k = default["top_k"]
    if "top_k" in raw and raw.get("top_k") is not None and raw.get("top_k") != "":
        try:
            top_k = max(0, min(int(raw["top_k"]), 500))
        except (TypeError, ValueError):
            top_k = default["top_k"]
    return {
        "enabled": bool(raw.get("enabled", False)),
        "model": raw.get("model") or None,
        "recent_n": recent_n,
        "top_k": top_k,
    }


def _row_to_session(row):
    memory = _parse_memory(row["memory"] if "memory" in row.keys() else None)
    return {"id": row["id"], "title": row["title"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "model": row["model"],
            "params": json.loads(row["params"]) if row["params"] else None,
            "vm": memory["vector"],   # 兼容旧前端：vm 从 memory.vector 权威读取
            "memory": memory,
            "messages": json.loads(row["messages"])}


def create_session(username, title=None, model=None, params=None, vm=None, memory=None):
    now = time.time()
    mem = _parse_memory(memory) if memory is not None else _default_memory()
    if vm is not None:
        mem["vector"] = _parse_vm(vm)
    session = {"id": _new_id(), "title": title or "会话 " + time.strftime("%m-%d %H:%M"),
               "created_at": now, "updated_at": now, "model": model, "params": params,
               "vm": mem["vector"], "memory": mem, "messages": []}
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, username, title, created_at, updated_at, model, params, messages, vm, memory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], username, session["title"], now, now, model,
             json.dumps(params, ensure_ascii=False), json.dumps([]),
             json.dumps(session["vm"], ensure_ascii=False),
             json.dumps(mem, ensure_ascii=False)),
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
    memory = _parse_memory(session.get("memory"))
    vm = memory["vector"]
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ?, model = ?, params = ?, messages = ?, vm = ?, memory = ? WHERE id = ? AND username = ?",
            (session["title"], session.get("updated_at", time.time()), session.get("model"),
             json.dumps(session.get("params"), ensure_ascii=False),
             json.dumps(session.get("messages", []), ensure_ascii=False),
             json.dumps(vm, ensure_ascii=False),
             json.dumps(memory, ensure_ascii=False),
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


def update_session_config(username, sid, model=None, params=None, vm=None, memory=None):
    session = get_session(username, sid)
    if session is None:
        return None
    if model is not None:
        session["model"] = model
    if params is not None:
        session["params"] = params
    if vm is not None:
        session["memory"]["vector"] = _parse_vm(vm)
    if memory is not None:
        session["memory"] = _parse_memory(memory)
    _save_session(username, session)
    return session


# ------------------------- 四层记忆读写（memory 列） -------------------------

def get_session_memory(username, sid):
    """读取会话的四层记忆结构；会话不存在返回 None。"""
    session = get_session(username, sid)
    return session["memory"] if session else None


def save_session_memory(username, sid, memory):
    """整体覆盖会话的四层记忆（规范化后落库），返回新会话或 None。"""
    session = get_session(username, sid)
    if session is None:
        return None
    session["memory"] = _parse_memory(memory)
    _save_session(username, session)
    return session


def set_session_card(username, sid, content, source="paste"):
    """写入核心设定卡。content 为空则清除该层。返回新 memory 结构或 None。"""
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    content = (content or "").strip()
    memory["card"] = {"content": content, "source": source, "updated_at": time.time()} if content else None
    save_session_memory(username, sid, memory)
    return memory


def set_session_facts(username, sid, facts):
    """整体覆盖动态关键事实列表。facts 为 [{"text", ...}]，规范化后落库。返回新 memory 或 None。"""
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    memory["facts"] = _parse_memory({"facts": facts, "card": None, "summary": None, "vector": memory["vector"]})["facts"]
    save_session_memory(username, sid, memory)
    return memory


def set_session_summary(username, sid, text, last_round=None):
    """写入剧情摘要。text 为空则清除。last_round 为已总结到的对话轮次（计数）。
    保留已有的用户配置（slice_rounds / auto_rounds）。返回新 memory 或 None。"""
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    text = (text or "").strip()
    old = memory.get("summary") or {}
    if text:
        memory["summary"] = {
            "text": text,
            "summarized_ts": time.time(),
            "last_round": last_round if last_round is not None else old.get("last_round", 0),
            "slice_rounds": old.get("slice_rounds"),
            "auto_rounds": old.get("auto_rounds"),
        }
    else:
        memory["summary"] = None
    save_session_memory(username, sid, memory)
    return memory


def set_session_summary_config(username, sid, slice_rounds=None, auto_rounds=None):
    """设置会话剧情摘要的触发参数（slice_rounds 切片宽度 / auto_rounds 自动触发阈值）。

    只更新传入的参数，其余保留。返回新 memory 或 None（会话不存在）。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    s = memory.get("summary") or {}
    changed = False
    if slice_rounds is not None:
        try:
            s = dict(s)
            s["slice_rounds"] = max(1, min(int(slice_rounds), 200))
            changed = True
        except (TypeError, ValueError):
            pass
    if auto_rounds is not None:
        try:
            s = dict(s)
            s["auto_rounds"] = max(0, min(int(auto_rounds), 1000))
            changed = True
        except (TypeError, ValueError):
            pass
    if changed:
        memory["summary"] = _parse_summary(s)
        save_session_memory(username, sid, memory)
    return memory


# 哨兵：区分「参数未提供（不更新）」与「显式传入 None（清除/恢复默认）」
_UNSET = object()


def set_session_vector_config(username, sid, top_k=_UNSET, recent_n=None, enabled=None, model=None):
    """设置会话的向量记忆配置。

    只更新传入的参数，其余保留。返回新 memory 或 None（会话不存在）。
    top_k 语义：_UNSET=不更新；None=清除（恢复默认，存储为 null）；0=不限制（自动召回）；>0=固定 N 条。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    vec = dict(memory.get("vector") or _parse_vm(None))
    changed = False
    if top_k is not _UNSET:
        if top_k is None:
            vec["top_k"] = None
        else:
            try:
                vec["top_k"] = max(0, min(int(top_k), 500))
            except (TypeError, ValueError):
                pass
        changed = True
    if recent_n is not None:
        try:
            vec["recent_n"] = max(1, min(int(recent_n), 1000))
            changed = True
        except (TypeError, ValueError):
            pass
    if enabled is not None:
        vec["enabled"] = bool(enabled)
        changed = True
    if model is not None:
        vec["model"] = str(model).strip() or None
        changed = True
    if changed:
        memory["vector"] = _parse_vm(vec)
        save_session_memory(username, sid, memory)
    return memory


def clear_session_memory_layer(username, sid, layer):
    """清空某一层记忆（card / facts / summary），vector 为配置不清。返回新 memory 或 None。"""
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    if layer in ("card", "facts", "summary"):
        memory[layer] = [] if layer == "facts" else None
        save_session_memory(username, sid, memory)
    return memory


# ------------------------- 角色卡库（跨会话复用） -------------------------

def list_character_cards(username):
    """列出用户可用的角色卡（按更新时间倒序）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, content, created_at, updated_at FROM character_cards ORDER BY updated_at DESC"
        ).fetchall()
    return [{"id": r["id"], "name": r["name"], "content": r["content"],
             "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


def get_character_card(card_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, content, created_at, updated_at FROM character_cards WHERE id = ?",
            (card_id,),
        ).fetchone()
    return {"id": row["id"], "name": row["name"], "content": row["content"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]} if row else None


def save_character_card(name, content, card_id=None):
    """保存/更新角色卡。card_id 为空则新建。返回角色卡 dict。"""
    now = time.time()
    name = (name or "").strip()[:50] or "未命名角色卡"
    content = (content or "").strip()
    with _connect() as conn:
        if card_id:
            conn.execute(
                "UPDATE character_cards SET name = ?, content = ?, updated_at = ? WHERE id = ?",
                (name, content, now, card_id),
            )
        else:
            cid = _new_id()
            conn.execute(
                "INSERT INTO character_cards(id, name, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (cid, name, content, now, now),
            )
            return {"id": cid, "name": name, "content": content, "created_at": now, "updated_at": now}
    # 退出 with 后 UPDATE 已 commit，再查询确保读到新值
    return get_character_card(card_id) if card_id else None


def delete_character_card(card_id):
    with _connect() as conn:
        conn.execute("DELETE FROM character_cards WHERE id = ?", (card_id,))


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
    mem = _parse_memory(data.get("memory"))
    if data.get("vm") is not None:
        mem["vector"] = _parse_vm(data.get("vm"))
    session = {
        "id": _new_id(),
        "title": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "model": data.get("model"),
        "params": data.get("params"),
        "vm": mem["vector"],
        "memory": mem,
        "messages": messages,
    }
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, username, title, created_at, updated_at, model, params, messages, vm, memory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], username, title, created_at, updated_at, session["model"],
             json.dumps(session["params"], ensure_ascii=False),
             json.dumps(messages, ensure_ascii=False),
             json.dumps(session["vm"], ensure_ascii=False),
             json.dumps(mem, ensure_ascii=False)),
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
        # 保存 binary 标志：前端据此区分「文本文件(拼内容)」与「二进制附件(base64)」，
        # 否则另一设备/重试时从后端恢复会把二进制文件误当作文本拼接。
        message["files"] = [{"name": f.get("name", ""), "content": f.get("content", ""),
                             "binary": bool(f.get("binary", False))} for f in files]
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


# 所有辅助函数定义完毕后再初始化数据库（含旧 vm -> memory.vector 迁移）
_init_db()
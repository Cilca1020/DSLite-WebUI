"""JSON 文件存储：管理会话历史与参数预设。

所有数据存于 data/ 目录，零数据库依赖。
- 会话：data/sessions/<session_id>.json
- 预设：data/presets.json（一个文件存所有预设）
"""

import json
import os
import time
import uuid

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")


def _ensure_dirs():
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def _new_id():
    return uuid.uuid4().hex


# ------------------------- 会话管理 -------------------------

def create_session(title=None, model=None, params=None):
    """新建一个空会话，返回会话元信息。

    model:  该会话使用的模型 id（每会话独立）
    params: 该会话的推理参数（每会话独立）
    """
    _ensure_dirs()
    sid = _new_id()
    session = {
        "id": sid,
        "title": title or "会话 " + time.strftime("%m-%d %H:%M"),
        "created_at": time.time(),
        "model": model,
        "params": params,
        "messages": [],  # 每条: {role, content, ts}
    }
    save_session(session)
    return session


def update_session_config(sid, model=None, params=None):
    """更新会话的模型与推理参数（每个会话一套独立配置）。"""
    session = get_session(sid)
    if session is None:
        return None
    if model is not None:
        session["model"] = model
    if params is not None:
        session["params"] = params
    save_session(session)
    return session


def save_session(session):
    """覆盖保存一个会话对象。"""
    _ensure_dirs()
    path = os.path.join(SESSIONS_DIR, session["id"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)


def get_session(sid):
    """读取单个会话，不存在返回 None。"""
    path = os.path.join(SESSIONS_DIR, sid + ".json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_sessions():
    """列出所有会话元信息（不含完整消息），按最近互动时间倒序。"""
    _ensure_dirs()
    sessions = []
    for name in os.listdir(SESSIONS_DIR):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(SESSIONS_DIR, name), "r", encoding="utf-8") as f:
            s = json.load(f)
        created = s.get("created_at", 0)
        # 最近互动时间：有更新则用 updated_at，否则用创建时间
        updated = s.get("updated_at", created)
        sessions.append({
            "id": s["id"],
            "title": s.get("title", "未命名"),
            "model": s.get("model"),
            "created_at": created,
            "updated_at": updated,
            "message_count": len(s.get("messages", [])),
        })
    sessions.sort(key=lambda x: x["updated_at"], reverse=True)
    return sessions


def delete_session(sid):
    """删除会话文件。"""
    path = os.path.join(SESSIONS_DIR, sid + ".json")
    if os.path.exists(path):
        os.remove(path)


def rename_session(sid, title):
    """修改会话标题。"""
    session = get_session(sid)
    if session is None:
        return None
    session["title"] = title
    save_session(session)
    return session


def cleanup_empty_sessions(exclude_id=None):
    """删除所有空会话（无消息），可排除某个 id（如当前正在查看的）。

    返回被删除的会话 id 列表。
    """
    _ensure_dirs()
    removed = []
    for name in os.listdir(SESSIONS_DIR):
        if not name.endswith(".json"):
            continue
        sid = name[: -len(".json")]
        if sid == exclude_id:
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if len(s.get("messages", [])) == 0:
            os.remove(path)
            removed.append(sid)
    return removed


def append_message(sid, role, content, reasoning=None):
    """向指定会话追加一条消息。

    reasoning: 仅用于渲染（如思考过程），不参与模型上下文；
               content 始终为纯回答文本，同时用于渲染与上传。
    每次追加会刷新 updated_at（用于按最近互动排序）。
    """
    session = get_session(sid)
    if session is None:
        session = create_session()
    msg = {
        "role": role,
        "content": content,
        "ts": time.time(),
    }
    if reasoning is not None:
        msg["reasoning"] = reasoning
    session.setdefault("messages", []).append(msg)
    session["updated_at"] = time.time()
    save_session(session)
    return session


# ------------------------- 预设管理 -------------------------

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
    """新增或覆盖一个参数预设。"""
    presets = _read_presets()
    for p in presets:
        if p["name"] == name:
            p["params"] = params
            _write_presets(presets)
            return p
    preset = {"name": name, "params": params}
    presets.append(preset)
    _write_presets(presets)
    return preset


def delete_preset(name):
    presets = _read_presets()
    presets = [p for p in presets if p["name"] != name]
    _write_presets(presets)

"""Flask 后端入口。

接口一览：
  GET  /                       -> 返回前端页面 (static/index.html)
  GET  /api/models             -> 支持的模型列表
  GET  /api/params/default     -> 默认推理参数
  POST /api/chat               -> 对话（支持流式，前端用 fetch + ReadableStream 接收）
  GET  /api/sessions           -> 会话列表
  POST /api/sessions           -> 新建会话
  GET  /api/sessions/<id>      -> 单个会话内容
  DELETE /api/sessions/<id>    -> 删除会话
  POST /api/sessions/<id>/rename -> 重命名会话 ({"title": "..."})
  POST /api/sessions/cleanup   -> 删除空会话 ({"exclude": 当前会话id})
  POST /api/sessions/<id>/msg  -> 向会话追加一条消息（非流式，存历史用）
  DELETE /api/sessions/<id>/msg/<index> -> 删除会话中第 index 条消息（不含 system）
  GET  /api/presets            -> 参数预设列表
  POST /api/presets            -> 保存预设
  DELETE /api/presets/<name>   -> 删除预设
"""

import json
import hmac
import os
import random
import re
import secrets
import string

from flask import Flask, Response, jsonify, request, send_from_directory, session

import config
import llm_client
import storage

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("DEEPSEEK_WEBUI_SECRET_KEY", config.SECRET_KEY)


@app.before_request
def protect_api():
    if request.path.startswith("/api/") and not request.path.startswith("/api/auth/") and "username" not in session:
        return jsonify({"error": "请先登录"}), 401


# ------------------------- 账号与验证码 -------------------------

@app.route("/api/auth/me")
def auth_me():
    return jsonify({"authenticated": "username" in session, "username": session.get("username")})


@app.route("/api/auth/captcha")
def auth_captcha():
    code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5))
    session["captcha"] = code
    random.seed(code)
    colors = ["#2563eb", "#dc2626", "#059669", "#7c3aed", "#d97706"]
    lines = "".join(f'<path d="M{random.randint(0,170)} {random.randint(8,52)} L{random.randint(20,190)} {random.randint(8,52)}" stroke="{random.choice(colors)}"/>' for _ in range(5))
    chars = "".join(f'<text x="{18 + i * 34}" y="39" transform="rotate({random.randint(-18,18)} {18 + i * 34} 30)" fill="{random.choice(colors)}">{char}</text>' for i, char in enumerate(code))
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="190" height="58"><rect width="190" height="58" rx="8" fill="#f3f4f6"/>{lines}<g font-family="Arial,sans-serif" font-size="27" font-weight="700">{chars}</g></svg>'
    return Response(svg, mimetype="image/svg+xml")


def _auth_form():
    data = request.get_json(force=True, silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    captcha = str(data.get("captcha", "")).strip().upper()
    if not username or not password or not captcha:
        return None, (jsonify({"error": "请填写完整信息"}), 400)
    if len(username) < 3 or len(username) > 32 or not username.replace("_", "").isalnum():
        return None, (jsonify({"error": "账号需为 3-32 位字母、数字或下划线"}), 400)
    if not hmac.compare_digest(captcha, session.pop("captcha", "")):
        return None, (jsonify({"error": "验证码错误或已过期"}), 400)
    return (username, password), None


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    values, error = _auth_form()
    if error:
        return error
    username, password = values
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    if not storage.create_user(username, password):
        return jsonify({"error": "账号已存在"}), 409
    session["username"] = username
    return jsonify({"authenticated": True, "username": username})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    values, error = _auth_form()
    if error:
        return error
    username, password = values
    if not storage.verify_user(username, password):
        return jsonify({"error": "账号或密码错误"}), 401
    session["username"] = username
    return jsonify({"authenticated": True, "username": username})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/password", methods=["POST"])
def auth_password():
    if "username" not in session:
        return jsonify({"error": "请先登录"}), 401
    data = request.get_json(force=True, silent=True) or {}
    current_password = str(data.get("current_password", ""))
    new_password = str(data.get("new_password", ""))
    if len(new_password) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400
    if current_password == new_password:
        return jsonify({"error": "新密码不能与当前密码相同"}), 400
    if not storage.change_password(session["username"], current_password, new_password):
        return jsonify({"error": "当前密码错误"}), 400
    return jsonify({"ok": True})


# ------------------------- 静态页面 -------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ------------------------- 模型与参数 -------------------------

@app.route("/api/models")
def api_models():
    models = [
        {"id": m["id"], "label": m["label"], "api_key_hint": m.get("api_key_hint", "")}
        for m in config.SUPPORTED_MODELS
    ]
    return jsonify(models)


@app.route("/api/params/default")
def api_default_params():
    return jsonify({
        "defaults": config.DEFAULT_PARAMS,
        "ranges": {k: list(v) for k, v in config.PARAM_RANGES.items()},
        "meta": config.PARAM_META,
        "stop_max_items": config.STOP_MAX_ITEMS,
        "stop_max_len": config.STOP_MAX_LEN,
    })


# ------------------------- 对话 -------------------------

def _validate_params(data):
    """校验并钳制参数到合法范围，返回清洗后的参数字典。"""
    out = {}
    for key, (lo, hi) in config.PARAM_RANGES.items():
        val = data.get(key, config.DEFAULT_PARAMS.get(key))
        try:
            val = float(val) if key != "max_tokens" else int(val)
        except (TypeError, ValueError):
            val = config.DEFAULT_PARAMS.get(key)
        out[key] = max(lo, min(hi, val))
    out["system_prompt"] = data.get("system_prompt", config.DEFAULT_PARAMS["system_prompt"])

    # stop 序列：逗号分隔，限制数量与单项长度
    raw_stop = data.get("stop", config.DEFAULT_PARAMS["stop"])
    if isinstance(raw_stop, str) and raw_stop.strip():
        items = [s.strip() for s in raw_stop.split(",") if s.strip()]
        items = [s[: config.STOP_MAX_LEN] for s in items[: config.STOP_MAX_ITEMS]]
        out["stop"] = items
    else:
        out["stop"] = []
    return out


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    api_key = data.get("api_key")
    model = data.get("model")
    user_messages = data.get("messages", [])  # 前端传来的对话上下文（不含 system）

    if not api_key:
        return jsonify({"error": "缺少 api_key"}), 400
    if not model:
        return jsonify({"error": "缺少 model"}), 400
    if not user_messages:
        return jsonify({"error": "消息为空"}), 400

    params = _validate_params(data)

    # 组装完整消息：system prompt 在前
    messages = [{"role": "system", "content": params["system_prompt"]}]
    messages += [{"role": m["role"], "content": m["content"]} for m in user_messages]

    # 流式响应：用 text/event-stream 把每个片段推给前端
    def generate():
        try:
            for piece in llm_client.chat(
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=params["temperature"],
                top_p=params["top_p"],
                max_tokens=params["max_tokens"],
                frequency_penalty=params["frequency_penalty"],
                presence_penalty=params["presence_penalty"],
                stop=params["stop"],
                stream=True,
            ):
                yield piece
        except RuntimeError as e:
            yield f"\n[错误] {e}"

    return Response(generate(), mimetype="text/plain; charset=utf-8")


# ------------------------- 会话管理 -------------------------

@app.route("/api/sessions")
def api_list_sessions():
    return jsonify(storage.list_sessions(session["username"]))


@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    data = request.get_json(force=True, silent=True) or {}
    title = data.get("title")
    model = data.get("model")
    params = data.get("params")
    return jsonify(storage.create_session(session["username"], title, model=model, params=params))


@app.route("/api/sessions/<sid>/config", methods=["POST"])
def api_update_session_config(sid):
    data = request.get_json(force=True, silent=True) or {}
    saved_session = storage.update_session_config(
        session["username"],
        sid,
        model=data.get("model"),
        params=data.get("params"),
    )
    if saved_session is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(saved_session)


@app.route("/api/sessions/<sid>")
def api_get_session(sid):
    s = storage.get_session(session["username"], sid)
    if s is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(s)


@app.route("/api/sessions/<sid>", methods=["DELETE"])
def api_delete_session(sid):
    storage.delete_session(session["username"], sid)
    return jsonify({"ok": True})


@app.route("/api/sessions/<sid>/rename", methods=["POST"])
def api_rename_session(sid):
    data = request.get_json(force=True, silent=True) or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "缺少 title"}), 400
    saved_session = storage.rename_session(session["username"], sid, title)
    if saved_session is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(saved_session)


@app.route("/api/sessions/cleanup", methods=["POST"])
def api_cleanup_sessions():
    # 删除空会话，可排除当前正在查看的那个
    exclude = (request.get_json(force=True, silent=True) or {}).get("exclude")
    removed = storage.cleanup_empty_sessions(session["username"], exclude_id=exclude)
    return jsonify({"removed": removed})


@app.route("/api/sessions/<sid>/msg/<int:index>", methods=["DELETE"])
def api_delete_msg(sid, index):
    """删除会话中指定下标的消息（从 0 开始，仅统计 user/assistant 消息，不含 system）。"""
    saved_session = storage.delete_message(session["username"], sid, index)
    if saved_session is None:
        return jsonify({"error": "会话不存在或下标越界"}), 404
    return jsonify(saved_session)


@app.route("/api/sessions/<sid>/msg", methods=["POST"])
def api_append_msg(sid):
    data = request.get_json(force=True, silent=True) or {}
    role = data.get("role")
    content = data.get("content", "")
    reasoning = data.get("reasoning")
    files = data.get("files")
    interrupted = data.get("interrupted")
    if role not in ("user", "assistant"):
        return jsonify({"error": "role 必须为 user 或 assistant"}), 400
    saved_session = storage.append_message(
        session["username"], sid, role, content, reasoning=reasoning, files=files, interrupted=interrupted
    )
    return jsonify(saved_session)


# 默认标题格式：create_session 生成的 "会话 MM-DD HH:MM"
_DEFAULT_TITLE_RE = re.compile(r"^会话 \d{2}-\d{2} \d{2}:\d{2}$")


def _is_default_title(title):
    return bool(_DEFAULT_TITLE_RE.match(title or ""))


@app.route("/api/sessions/<sid>/auto-title", methods=["POST"])
def api_auto_title(sid):
    data = request.get_json(force=True, silent=True) or {}
    api_key = str(data.get("api_key", "")).strip()
    system_prompt = data.get("system_prompt", config.DEFAULT_PARAMS["system_prompt"])
    current_user = session["username"]
    title_status = storage.auto_title_status(current_user, sid)
    if title_status is None:
        return jsonify({"error": "会话不存在"}), 404
    # 以标题是否仍为默认格式作为唯一判据：非默认标题（用户已手动命名或已自动生成过）
    # 一律直接返回，不重新生成，防止覆盖自定义标题（不依赖 auto_title_generated 标志，
    # 避免旧数据中标志为 0 的手动命名会话被误覆盖）。
    # generated=1 但标题仍是默认格式（历史脏数据）时继续生成实现自愈。
    if not _is_default_title(title_status["title"]):
        return jsonify({"title": title_status["title"]})
    saved_session = storage.get_session(current_user, sid)
    if not api_key or not saved_session:
        return jsonify({"error": "参数不完整或会话不存在"}), 400
    # 标题生成固定使用轻量 chat 模型，不跟随 UI 选中模型，
    # 避免 reasoner 因 token 预算输不出正文、OpenAI 模型等导致的生成失败
    model = config.AUTO_TITLE_MODEL
    messages = [m for m in saved_session["messages"] if m.get("role") in ("user", "assistant")]
    if len(messages) < 2 or messages[0]["role"] != "user" or messages[1]["role"] != "assistant":
        return jsonify({"error": "首轮对话尚未完成"}), 400
    prompt = (
        "请根据系统提示词和下面的第一轮问答，为这个会话生成一个简洁标题。"
        "只返回标题文字，不要引号、标点或解释，最多10个字。\n"
        f"用户：{messages[0].get('content', '')}\n助手：{messages[1].get('content', '')}"
    )
    try:
        title = llm_client.chat(
            api_key=api_key,
            model=model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            temperature=0.2,
            top_p=1.0,
            # 推理模型（reasoner）的思考过程也计入 max_tokens，32 容易输不出正文
            max_tokens=1024,
            stream=False,
        )
    except (RuntimeError, ValueError, KeyError, IndexError):
        return jsonify({"error": "标题生成失败"}), 502
    title = "".join(str(title or "").replace("\n", " ").split()).strip(" \"'`。，、：:；;！!？?《》[]()（）")[:10]
    if not title:
        return jsonify({"error": "标题为空"}), 502
    updated = storage.set_auto_title(current_user, sid, title)
    if updated is None:
        # 并发下已被其他请求生成：返回最新标题
        latest = storage.auto_title_status(current_user, sid)
        return jsonify({"title": latest["title"] if latest else title})
    return jsonify(updated)


# ------------------------- 预设管理 -------------------------

@app.route("/api/presets")
def api_list_presets():
    return jsonify(storage.list_presets())


@app.route("/api/presets", methods=["POST"])
def api_save_preset():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    params = data.get("params")
    if not name:
        return jsonify({"error": "缺少 name"}), 400
    if not params:
        return jsonify({"error": "缺少 params"}), 400
    preset = storage.save_preset(name, params)
    return jsonify(preset)


@app.route("/api/presets/<name>", methods=["DELETE"])
def api_delete_preset(name):
    storage.delete_preset(name)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=True)

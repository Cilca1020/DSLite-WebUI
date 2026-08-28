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

import colorsys
import io
import json
import hmac
import os
import random
import re
import secrets
import string
from datetime import timedelta

from flask import Flask, Response, jsonify, request, send_from_directory, session
from PIL import Image, ImageDraw, ImageFont
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import llm_client
import storage
import vector_memory

app = Flask(__name__, static_folder="static")
# 部署在 nginx 反向代理之后：信任其转发的 X-Forwarded-Proto / For / Host，
# 使 request.is_secure 与 Cookie 安全标记等能正确感知 HTTPS 请求。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("DSLITE_WEBUI_SECRET_KEY", config.SECRET_KEY)
app.config["SESSION_COOKIE_SECURE"] = config.SESSION_COOKIE_SECURE
# 持久化 session cookie：关闭浏览器 / 切换网络后仍保持登录 30 天
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


@app.before_request
def make_session_persistent():
    # 让登录态跨浏览器会话 / 网络保持：session cookie 带 Max-Age（30 天），
    # 关闭浏览器或切换网络时不会丢失登录状态。
    session.permanent = True


@app.before_request
def protect_api():
    if request.path.startswith("/api/") and not request.path.startswith("/api/auth/") and "username" not in session:
        return jsonify({"error": "请先登录"}), 401


# ------------------------- 账号与验证码 -------------------------

@app.route("/api/auth/me")
def auth_me():
    return jsonify({"authenticated": "username" in session, "username": session.get("username")})


# 验证码字符集：剔除 0/O、1/I/L、2/Z、5/S、8/B 等易混淆字符
CAPTCHA_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CAPTCHA_LENGTH = 4
_CAPTCHA_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _captcha_font(size):
    """加载粗体字体，找不到则回退到 Pillow 内置字体。"""
    for path in _CAPTCHA_FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _rand_color(rng, lo=30, hi=160):
    return (rng.randint(lo, hi), rng.randint(lo, hi), rng.randint(lo, hi))


def _char_color(rng):
    """深色调、低饱和、色相随机的字符色。

    用 HSV 控制：色相全色环随机（各字符颜色不同），但饱和度偏低、
    明度统一偏深。与浅色背景形成明度差，色盲用户也能区分字符与背景。
    """
    h = rng.random()
    s = rng.uniform(0.45, 0.7)
    v = rng.uniform(0.38, 0.55)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def _render_captcha(code, width=220, height=64):
    """生成带彩色斑块背景、噪点、干扰曲线、字符旋转/抖动/随机大小的验证码图片。"""
    rng = random.Random()
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    # 彩色马赛克斑块背景：打散底色，压低字符与背景的对比度
    block = 8
    for y in range(0, height, block):
        for x in range(0, width, block):
            t = x / width
            base = (240 - int(10 * t), 243 - int(8 * t), 247 - int(14 * t))
            offset = rng.randint(-26, 26)
            fill = tuple(max(0, min(255, c + offset)) for c in base)
            draw.rectangle((x, y, x + block - 1, y + block - 1), fill=fill)
    # 随机噪点（颜色与字符相近，制造颗粒干扰）
    for _ in range(int(width * height * 0.12)):
        draw.point((rng.randint(0, width - 1), rng.randint(0, height - 1)),
                   fill=_rand_color(rng, 90, 200))
    # 贝塞尔干扰曲线
    for _ in range(5):
        color = _rand_color(rng, 100, 190)
        pts = [
            (rng.randint(-10, width // 4), rng.randint(0, height)),
            (rng.randint(width // 4, width // 2), rng.randint(0, height)),
            (rng.randint(width // 2, 3 * width // 4), rng.randint(0, height)),
            (rng.randint(3 * width // 4, width + 10), rng.randint(0, height)),
        ]
        draw.line(pts, fill=color, width=rng.randint(1, 2), joint="curve")
    # 随机小圆圈
    for _ in range(8):
        x, y = rng.randint(0, width), rng.randint(0, height)
        r = rng.randint(2, 5)
        draw.ellipse((x - r, y - r, x + r, y + r),
                     outline=_rand_color(rng, 110, 180), width=1)
    # 逐字符绘制：随机大小 / 旋转 / 上下抖动 / 深色调低饱和随机色
    step = width / (len(code) + 1)
    for i, ch in enumerate(code):
        size = rng.randint(27, 33)
        font = _captcha_font(size)
        bbox = draw.textbbox((0, 0), ch, font=font)
        mask = Image.new("L", (bbox[2] - bbox[0] + 10, bbox[3] - bbox[1] + 10), 0)
        ImageDraw.Draw(mask).text((5 - bbox[0], 5 - bbox[1]), ch, font=font, fill=255)
        mask = mask.rotate(rng.randint(-28, 28), expand=1, resample=Image.BICUBIC)
        cx = int(step * (i + 1) - mask.width / 2)
        cy = int(height / 2 - mask.height / 2 + rng.randint(-6, 6))
        img.paste(Image.new("RGB", mask.size, _char_color(rng)), (cx, cy), mask)
    return img


@app.route("/api/auth/captcha")
def auth_captcha():
    code = "".join(secrets.choice(CAPTCHA_CHARS) for _ in range(CAPTCHA_LENGTH))
    session["captcha"] = code
    buf = io.BytesIO()
    _render_captcha(code).save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


def _auth_form():
    data = request.get_json(force=True, silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    captcha = str(data.get("captcha", "")).strip().upper()
    if not username or not password or not captcha:
        return None, (jsonify({"error": "请填写完整信息"}), 400)
    if len(captcha) != CAPTCHA_LENGTH:
        return None, (jsonify({"error": f"验证码需为 {CAPTCHA_LENGTH} 位"}), 400)
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


@app.route("/api/vector-memory/models")
def api_vector_memory_models():
    """列出后端 models/ 目录下可用的向量化模型（用户选取后前端缓存）。"""
    return jsonify(vector_memory.list_available_models())


def _no_vm_char_budget(model, params):
    """估算「关闭向量记忆」时历史最多可占用的字符数。

    按所选模型 API 上下文窗口动态计算：可用 token = 上下文窗口 - 回复 max_tokens - system prompt，
    再换算为字符数并留出安全余量；未配置窗口的模型回退到默认值。
    """
    window = config.MODEL_CONTEXT_WINDOW.get(model or "", config.NO_VM_DEFAULT_WINDOW)
    max_tokens = int(params.get("max_tokens") or config.DEFAULT_PARAMS.get("max_tokens", 2048))
    sys_tokens = len(params.get("system_prompt") or "") / config.CHARS_PER_TOKEN
    avail_tokens = max(0, window - max_tokens - sys_tokens)
    return int(avail_tokens * config.CHARS_PER_TOKEN * config.NO_VM_CONTEXT_RATIO)


def _truncate_long(user_messages, model=None, params=None):
    """向量记忆关闭/不可用时：按所选模型上下文窗口动态保留最近对话（只留最近部分）。

    system prompt 单独组装，不在此列表中，因此绝不会被砍掉。
    """
    chat = [
        {"role": m["role"], "content": m["content"]}
        for m in user_messages
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    budget = _no_vm_char_budget(model, params) if params else config.NO_VM_MAX_CHARS
    while (sum(len(m["content"]) for m in chat) > budget
           or len(chat) > config.NO_VM_MAX_MESSAGES) and len(chat) > 1:
        chat.pop(0)
    return chat


def _build_chat_context(user_messages, sid, data, username=None, params=None):
    """拼接发给模型的对话上下文。

    - 向量记忆开启（请求参数 vector_memory=true，需带 session_id 与已选向量化模型）：
      先把会话消息增量索引到向量库，再返回「最近 N 条 + 向量检索 Top-K」。
      模型加载失败抛 VectorMemoryError，由调用方转成错误响应（不静默忽略）。
    - 关闭 / 无 session_id：原样透传，但对话过长时砍掉过早的消息。
    """
    enabled = config.VECTOR_MEMORY_ENABLED
    if "vector_memory" in data:
        enabled = bool(data.get("vector_memory"))

    if not enabled or not sid:
        return _truncate_long(user_messages, data.get("model"), params)

    model_dir = None
    vm_model = str(data.get("vector_memory_model") or "").strip()
    if vm_model:
        model_dir = os.path.join("models", vm_model)

    vm = vector_memory.get_instance(
        db_path=config.VECTOR_MEMORY_DB,
        model_dir=model_dir or vector_memory.MODEL_DIR,
        device=config.VECTOR_MEMORY_DEVICE,
        recent_n=config.VECTOR_MEMORY_RECENT_N,
        top_k=config.VECTOR_MEMORY_TOP_K,
        min_score=config.VECTOR_MEMORY_MIN_SCORE,
    )
    # 双向同步（补新 + 删旧）：以「存储中的完整会话 + 本次请求消息」为准，
    # 确保 ①前端只发送分页时早期消息也能被索引；②被编辑/重试/删除的消息
    # 不再残留在向量库中被检索进新上下文。
    stored_msgs = None
    if username and sid:
        stored = storage.get_session(username, sid)
        if stored:
            stored_msgs = stored["messages"]
            vm.reconcile_session(sid, list(stored_msgs) + list(user_messages))
        else:
            vm.sync_session(sid, user_messages)
    else:
        vm.sync_session(sid, user_messages)
    # 检索查询取最后一条 user 消息（当前提问）
    query = next(
        (
            m["content"]
            for m in reversed(user_messages)
            if m.get("role") == "user" and (m.get("content") or "").strip()
        ),
        "",
    )
    # N 为用户自定义的「最近 N 轮对话」（一轮 = 一次提问 + 一次回答），
    # 不得超过当前会话总轮数；由 build_history 内部换算成消息条数。
    n_rounds = config.VECTOR_MEMORY_RECENT_N
    try:
        n_rounds = int(data.get("vector_memory_recent_n") or n_rounds)
    except (TypeError, ValueError):
        pass
    return vm.build_history(
        user_messages,
        session_id=sid,
        query=query,
        recent_rounds=n_rounds,
        full_messages=stored_msgs,
        max_chars=config.NO_VM_MAX_CHARS,
    )


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    api_key = data.get("api_key")
    model = data.get("model")
    user_messages = data.get("messages", [])  # 前端传来的对话上下文（不含 system）
    sid = data.get("session_id") or data.get("sessionId") or data.get("sid")

    if not api_key:
        return jsonify({"error": "缺少 api_key"}), 400
    if not model:
        return jsonify({"error": "缺少 model"}), 400
    if not user_messages:
        return jsonify({"error": "消息为空"}), 400

    params = _validate_params(data)

    # 组装完整消息：system prompt 在前
    try:
        context = _build_chat_context(user_messages, sid, data, session["username"], params)
    except vector_memory.VectorMemoryError as e:
        app.logger.error("向量记忆不可用：%s", e)
        return jsonify({"error": f"向量记忆不可用：{e}", "vector_memory_error": True}), 502
    messages = [{"role": "system", "content": params["system_prompt"]}] + context

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
    vm = data.get("vm")
    return jsonify(storage.create_session(session["username"], title, model=model, params=params, vm=vm))


@app.route("/api/sessions/<sid>/config", methods=["POST"])
def api_update_session_config(sid):
    data = request.get_json(force=True, silent=True) or {}
    saved_session = storage.update_session_config(
        session["username"],
        sid,
        model=data.get("model"),
        params=data.get("params"),
        vm=data.get("vm"),
    )
    if saved_session is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(saved_session)


@app.route("/api/sessions/import", methods=["POST"])
def api_import_session():
    """按导出的 JSON 结构新建一个完整会话（与导出功能对应）。"""
    data = request.get_json(force=True, silent=True) or {}
    s = storage.import_session(session["username"], data)
    return jsonify(s)


@app.route("/api/sessions/<sid>")
def api_get_session(sid):
    s = storage.get_session(session["username"], sid)
    if s is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(s)


@app.route("/api/sessions/<sid>/messages")
def api_get_session_messages(sid):
    """分页读取会话消息（长对话按需加载）。limit 为单次条数；before 为消息下标锚点，
    返回该锚点之前最近的 limit 条；不传 before 时返回最新的 limit 条。"""
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 200))
    except (TypeError, ValueError):
        limit = 30
    before = request.args.get("before")
    if before is not None:
        try:
            before = max(0, int(before))
        except (TypeError, ValueError):
            before = None
    data = storage.get_session_messages(session["username"], sid, limit=limit, before=before)
    if data is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(data)


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
    # 生产环境使用 waitress（支持 Windows 的多线程 WSGI 服务器）。
    # 关闭 debug 重载器：waitress 不支持 werkzeug 的 reloader。
    from waitress import serve
    # _quiet=False 让 waitress 实时打印访问日志（IP、方法、路径、状态码）。
    # threads 提高并发上限，缓解流式对话占用线程导致的请求排队（Task queue depth 告警）。
    serve(app, host=config.HOST, port=config.PORT, _quiet=False, threads=16)

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
  POST /api/sessions/<id>/msg  -> 向会话追加一条消息（非流式，存历史用）
  GET  /api/presets            -> 参数预设列表
  POST /api/presets            -> 保存预设
  DELETE /api/presets/<name>   -> 删除预设
"""

import json

from flask import Flask, Response, jsonify, request, send_from_directory

import config
import llm_client
import storage

app = Flask(__name__, static_folder="static")


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
    return jsonify(config.DEFAULT_PARAMS)


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
                stream=True,
            ):
                yield piece
        except RuntimeError as e:
            yield f"\n[错误] {e}"

    return Response(generate(), mimetype="text/plain; charset=utf-8")


# ------------------------- 会话管理 -------------------------

@app.route("/api/sessions")
def api_list_sessions():
    return jsonify(storage.list_sessions())


@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    title = (request.get_json(force=True, silent=True) or {}).get("title")
    return jsonify(storage.create_session(title))


@app.route("/api/sessions/<sid>")
def api_get_session(sid):
    s = storage.get_session(sid)
    if s is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(s)


@app.route("/api/sessions/<sid>", methods=["DELETE"])
def api_delete_session(sid):
    storage.delete_session(sid)
    return jsonify({"ok": True})


@app.route("/api/sessions/<sid>/rename", methods=["POST"])
def api_rename_session(sid):
    data = request.get_json(force=True, silent=True) or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "缺少 title"}), 400
    session = storage.rename_session(sid, title)
    if session is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(session)


@app.route("/api/sessions/<sid>/msg", methods=["POST"])
def api_append_msg(sid):
    data = request.get_json(force=True, silent=True) or {}
    role = data.get("role")
    content = data.get("content", "")
    reasoning = data.get("reasoning")
    if role not in ("user", "assistant"):
        return jsonify({"error": "role 必须为 user 或 assistant"}), 400
    session = storage.append_message(sid, role, content, reasoning=reasoning)
    return jsonify(session)


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

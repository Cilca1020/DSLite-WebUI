"""大模型调用封装：兼容 OpenAI 协议（DeepSeek / OpenAI 等）。

设计要点：
- 所有厂商走同一套 /chat/completions 接口，仅 base_url 与 model 不同。
- 支持流式（stream=True）与非流式两种返回，前端用流式做打字机效果。
- 不持久化 API Key，Key 由调用方（路由层）传入。
- 网络层自动重试：应对 DNS 抖动、连接超时、瞬时 5xx / 限流（见 config.REQUEST_*）。
"""

import json

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    REQUEST_MAX_RETRIES,
    REQUEST_RETRY_BACKOFF,
    REQUEST_RETRY_STATUS,
    REQUEST_TIMEOUT,
)


def _make_session():
    """构建带网络层重试的 requests.Session。

    - 对连接错误 / 连接超时 / DNS 解析失败 / 读取超时总是重试（Retry 默认覆盖这些）。
    - 对状态码 429、500、502、503、504 也重试（服务器瞬时错误 / 限流）。
    - 指数退避：backoff_factor 1.0 -> 1s, 2s, 4s。
    """
    retry = Retry(
        total=REQUEST_MAX_RETRIES,
        connect=REQUEST_MAX_RETRIES,
        read=REQUEST_MAX_RETRIES,
        status=REQUEST_MAX_RETRIES,
        backoff_factor=REQUEST_RETRY_BACKOFF,
        status_forcelist=list(REQUEST_RETRY_STATUS),
        allowed_methods=["POST"],
        raise_on_status=False,  # 不自动抛 HTTPError，交由调用方处理状态码
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_session = None


def _get_session():
    global _session
    if _session is None:
        _session = _make_session()
    return _session


def _find_model(model_id):
    """根据模型 id 找到配置项。"""
    from config import SUPPORTED_MODELS
    for m in SUPPORTED_MODELS:
        if m["id"] == model_id:
            return m
    return None


def chat(
    api_key,
    model,
    messages,
    temperature=0.7,
    top_p=1.0,
    max_tokens=2048,
    frequency_penalty=0.0,
    presence_penalty=0.0,
    stop=None,
    stream=False,
):
    """发起一次对话。

    参数:
        api_key:    模型服务 API Key
        model:      模型 id（对应 config.SUPPORTED_MODELS 的 id）
        messages:   [{"role": "system"/"user"/"assistant", "content": "..."}]
        stream:     True 时返回生成器（逐块 yield 文本），False 时返回完整文本

    返回:
        stream=False -> str（完整回复）
        stream=True  -> generator，逐块 yield 文本片段
    """
    cfg = _find_model(model)
    if cfg is None:
        raise ValueError(f"不支持的模型: {model}")

    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "frequency_penalty": frequency_penalty,
        "presence_penalty": presence_penalty,
        "stream": stream,
    }
    if stop:
        payload["stop"] = stop

    session = _get_session()
    if not stream:
        try:
            resp = session.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            raise RuntimeError(f"网络错误: {e}") from e
        if resp.status_code != 200:
            raise RuntimeError(f"API 错误 {resp.status_code}: {resp.text}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    return _stream_response(session, url, headers, payload)


def _stream_response(session, url, headers, payload):
    """流式读取 SSE 响应，避免影响非流式调用的返回类型。

    用带重试的 session 发起流式请求；网络错误会在重试耗尽后抛 RuntimeError。
    """
    try:
        resp = session.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT, stream=True)
    except requests.RequestException as e:
        raise RuntimeError(f"网络错误: {e}") from e
    if resp.status_code != 200:
        # 非流式返回时直接读取错误正文，便于报错信息完整
        raise RuntimeError(f"API 错误 {resp.status_code}: {resp.text}")

    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            # 推理过程（DeepSeek Reasoner 等通过 reasoning_content 提供）
            reasoning = delta.get("reasoning_content")
            if reasoning:
                yield "<<REASONING>>" + reasoning
            # 正式回答内容
            piece = delta.get("content")
            if piece:
                yield "<<ANSWER>>" + piece
    except requests.RequestException as e:
        raise RuntimeError(f"网络错误: {e}") from e

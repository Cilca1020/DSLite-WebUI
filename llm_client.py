"""大模型调用封装：兼容 OpenAI 协议（DeepSeek / OpenAI 等）。

设计要点：
- 所有厂商走同一套 /chat/completions 接口，仅 base_url 与 model 不同。
- 支持流式（stream=True）与非流式两种返回，前端用流式做打字机效果。
- 不持久化 API Key，Key 由调用方（路由层）传入。
"""

import json

import requests

from config import REQUEST_TIMEOUT


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
        "stream": stream,
    }

    if not stream:
        resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"API 错误 {resp.status_code}: {resp.text}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # 流式：用流式读取，逐行解析 SSE
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT, stream=True)
    if resp.status_code != 200:
        raise RuntimeError(f"API 错误 {resp.status_code}: {resp.text}")

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

"""全局配置：支持的模型、默认推理参数、服务端口。

新增模型只需在 SUPPORTED_MODELS 里加一项，前端会自动出现可选项。
"""

# Flask 服务配置
HOST = "127.0.0.1"
PORT = 5000

# 支持的模型列表。每个模型包含：
#   id        -> 实际传给 API 的 model 名
#   label     -> 前端展示名
#   base_url  -> API 基址（兼容 OpenAI 协议）
#   default_model -> 该厂商默认 model 名（label 等于它时直接用它）
SUPPORTED_MODELS = [
    {
        "id": "deepseek-chat",
        "label": "DeepSeek Chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_hint": "sk-...（DeepSeek 控制台获取）",
    },
    {
        "id": "deepseek-reasoner",
        "label": "DeepSeek Reasoner",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_hint": "sk-...（DeepSeek 控制台获取）",
    },
    {
        "id": "gpt-4o-mini",
        "label": "OpenAI GPT-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "api_key_hint": "sk-...（OpenAI 平台获取）",
    },
    {
        "id": "gpt-4o",
        "label": "OpenAI GPT-4o",
        "base_url": "https://api.openai.com/v1",
        "api_key_hint": "sk-...（OpenAI 平台获取）",
    },
]

# 默认推理参数（前端初始值也来自这里）
DEFAULT_PARAMS = {
    "temperature": 0.7,
    "top_p": 1.0,
    "max_tokens": 2048,
    "system_prompt": "你是一个乐于助人的助手。",
}

# 参数的合法范围，用于后端校验
PARAM_RANGES = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "max_tokens": (1, 8192),
}

# 请求模型 API 的超时时间（秒）
REQUEST_TIMEOUT = 60

"""全局配置：支持的模型、默认推理参数、服务端口。

新增模型只需在 SUPPORTED_MODELS 里加一项，前端会自动出现可选项。
"""

# Flask 服务配置
# 0.0.0.0 允许局域网内其他设备（如手机）访问；仅本机访问可改回 127.0.0.1
HOST = "0.0.0.0"
PORT = 5000
SECRET_KEY = "dslite-webui-change-this-secret"

# 自动生成会话标题使用的固定模型（不跟随 UI 选择，保证标题生成轻量稳定）
AUTO_TITLE_MODEL = "deepseek-chat"

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
]

# 默认推理参数（前端初始值也来自这里）
DEFAULT_PARAMS = {
    "temperature": 0.7,
    "top_p": 1.0,
    "max_tokens": 2048,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
    "stop": "",
    "system_prompt": "你是一个乐于助人的助手。",
}

# 数值型参数的合法范围，用于后端校验（前端也会据此外做回退）
PARAM_RANGES = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "max_tokens": (1, 8192),
    "frequency_penalty": (-2.0, 2.0),
    "presence_penalty": (-2.0, 2.0),
}

# 每个参数的中文说明（前端展示用）
PARAM_META = {
    "temperature": "采样随机性。越高越发散有创意，越低越确定保守。",
    "top_p": "核采样阈值。只从累计概率达此值的词集中采样。",
    "max_tokens": "单次回复最大 token 数（含推理与回答）。",
    "frequency_penalty": "按词频惩罚重复词，降低重复。",
    "presence_penalty": "按是否出现过惩罚，鼓励引入新话题。",
    "stop": "停止词，最多 4 个，用逗号分隔；命中即停止生成。",
}

# stop 序列限制（非数值型，单独约束）
STOP_MAX_ITEMS = 4
STOP_MAX_LEN = 32

# 请求模型 API 的超时时间（秒）
REQUEST_TIMEOUT = 60

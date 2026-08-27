# DSLite-WebUI

一个网页端程序，用于与 DeepSeek 等兼容 OpenAI 协议的大模型对话，支持便捷的参数调整。

## 功能

- 与 DeepSeek / OpenAI 等模型对话（支持流式输出，生成过程中可随时「停止生成」，已生成内容保留）
- 实时调整推理参数：temperature、top_p、max_tokens、system prompt
- 多模型切换（通过配置）
- 多会话保存与历史记录
- 会话导出：支持 JSON（完整结构化数据）、Markdown（可读对话记录）、纯文本（txt，无标记符号）三种格式下载
- 参数预设模板
- 用户输入 API Key 后，首轮问答完成时自动总结会话标题（每个会话仅一次，最多 10 字）
- API Key 在网页端输入，存于浏览器 localStorage（后端不持久化 Key）
- 账号登录与注册，登录时使用图形验证码
- 向量记忆（长对话）：超长会话下用本地嵌入模型召回早期相关内容拼入上下文

## 向量记忆（长对话）

用于超长会话的历史召回：把消息用**本地嵌入模型**向量化后存入 SQLite，每次提问时在本地检索最相关的早期内容拼入上下文，避免上下文一长就「忘掉开头」。

对向量化模型的要求：

- 必须是 **sentence-transformers 兼容的本地嵌入模型**（不依赖任何远程 API / Ollama）。
- 模型目录内需包含 `config_sentence_transformers.json`（程序以此识别可用模型）。
- 模型文件统一放在 `models/` 目录下，支持两种目录结构：
  - HF Hub 缓存结构：`models/models--组织--模型名/snapshots/<commit>/`
  - 直接平铺目录：`models/模型名/`
- 默认模型为 **Qwen3-Embedding-0.6B**（目录 `models/models--Qwen--Qwen3-Embedding-0.6B`，**1024 维**，float32）。检索时使用 `prompt_name="query"` 套用模型的 query 指令，建议选用支持 query prompt 的 Qwen3-Embedding 系列。
- 对向量**维数无固定要求**：向量一律按 `float32` 存储与读取，入库时记录各自的实际维度即可。但同一向量库（`data/vector_memory.db`）内所有向量维度必须**一致**，因为检索用点积要求维度匹配；更换成不同维度的模型前需先清空旧向量库，否则会维度不匹配报错。

安装向量记忆所需的依赖（普通对话不装也可用，仅开启向量记忆时才需要）：

```bash
pip install torch sentence-transformers
```

如需 CUDA 加速，请按 <https://pytorch.org/get-started/locally/> 安装与显卡/驱动匹配的版本。

下载默认模型（二选一）：

```bash
# 方式一：huggingface-cli
huggingface-cli download Qwen/Qwen3-Embedding-0.6B --local-dir models/models--Qwen--Qwen3-Embedding-0.6B

# 方式二：git clone
git clone https://huggingface.co/Qwen/Qwen3-Embedding-0.6B models/models--Qwen--Qwen3-Embedding-0.6B
```

运行说明：

- 设备自动回退：CUDA → MPS → CPU；CUDA/MPS 使用 `bfloat16`，CPU 使用 `float32`。
- 模型为**懒加载**：未安装 torch 或未下载模型时不影响普通对话，仅当开启向量记忆并实际使用时才加载；若 torch 或模型不可用，程序会明确报错并提示，不会静默降级。
- 更换模型：把任意符合上述要求的 sentence-transformers 模型放入 `models/` 目录，即可在设置面板中选择。**维度相同的模型**可直接替换、复用旧向量；**维度不同**则需先清空向量库再换。

## 目录结构

```
DSLite-WebUI/
├── app.py            # Flask 后端入口，注册所有接口
├── llm_client.py     # 调用大模型的封装（兼容 OpenAI 协议）
├── storage.py        # SQLite 账号与会话存储，JSON 参数预设存储
├── config.py         # 配置（端口、默认模型、支持的模型列表）
├── vector_memory.py  # 向量记忆：本地嵌入模型向量化 + SQLite 存储 + 本地检索
├── models/           # 本地向量化模型（sentence-transformers，默认 Qwen3-Embedding-0.6B，运行时下载）
├── static/           # 前端
│   ├── index.html
│   ├── style-light.css / style-dark.css
│   ├── icons.js      # 基础工具与全局状态
│   ├── api.js        # 网络请求封装
│   ├── render.js     # 渲染逻辑（markdown / 消息气泡）
│   ├── sessions.js   # 参数读写与会话管理
│   ├── export.js     # 会话导出（JSON / Markdown / 纯文本）
│   ├── chat.js       # 流式输出与消息操作
│   ├── main.js       # 初始化入口
│   ├── file_reader.js  # 文件读取模块
│   └── vendor/       # 第三方库（marked 等）
├── data/             # 运行时生成（app.db、旧版会话 JSON、预设，已被 .gitignore 忽略）
└── requirements.txt
```

## 快速开始

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 启动：

```bash
python app.py
```

3. 浏览器打开 http://127.0.0.1:5000

4. 首次打开页面先注册账号，之后使用账号、密码和图形验证码登录。
5. 登录后在页面右上角填入你的 API Key（存于浏览器本地，不会发给除模型服务外的任何地方）。

## 说明

- 后端仅作为代理转发，每次请求由前端携带 Key，后端不落盘保存 Key。
- 账号和会话数据保存在 `data/app.db`，会话通过 `username` 外键绑定账号，密码以 PBKDF2 哈希保存；部署到生产环境时请设置 `DSLITE_WEBUI_SECRET_KEY` 环境变量覆盖默认 Flask session 密钥。
- 支持的模型在 `config.py` 中配置，默认包含 DeepSeek 与 OpenAI 兼容端点。

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

## 目录结构

```
DSLite-WebUI/
├── app.py            # Flask 后端入口，注册所有接口
├── llm_client.py     # 调用大模型的封装（兼容 OpenAI 协议）
├── storage.py        # SQLite 账号与会话存储，JSON 参数预设存储
├── config.py         # 配置（端口、默认模型、支持的模型列表）
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

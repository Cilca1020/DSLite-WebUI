# DeepSeek WebUI

一个网页端程序，用于与 DeepSeek 等兼容 OpenAI 协议的大模型对话，支持便捷的参数调整。

## 功能

- 与 DeepSeek / OpenAI 等模型对话（支持流式输出）
- 实时调整推理参数：temperature、top_p、max_tokens、system prompt
- 多模型切换（通过配置）
- 多会话保存与历史记录
- 参数预设模板
- API Key 在网页端输入，存于浏览器 localStorage（后端不持久化 Key）

## 目录结构

```
DeepSeek_WebUI/
├── app.py            # Flask 后端入口，注册所有接口
├── llm_client.py     # 调用大模型的封装（兼容 OpenAI 协议）
├── storage.py        # 读写 data/ 下的 JSON 文件（会话、预设）
├── config.py         # 配置（端口、默认模型、支持的模型列表）
├── static/           # 前端
│   ├── index.html
│   ├── style.css
│   └── app.js
├── data/             # 运行时生成（会话/预设 JSON，已被 .gitignore 忽略）
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

4. 在页面右上角填入你的 API Key（存于浏览器本地，不会发给除模型服务外的任何地方）。

## 说明

- 后端仅作为代理转发，每次请求由前端携带 Key，后端不落盘保存 Key。
- 支持的模型在 `config.py` 中配置，默认包含 DeepSeek 与 OpenAI 兼容端点。

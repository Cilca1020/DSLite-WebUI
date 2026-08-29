# 四层记忆模块（Memory Engine）交接文档

> 交接时间：2026-08-29
> 模块状态：**后端核心已完成，前端（阶段二）未做**
> 下一步接手的 Agent 请从「# 待办事项」开始。

---

## 1. 模块目标与架构

解决「角色扮演/长对话中，关键设定随剧情推进被遗忘」的问题。
用户导入静态设定 + LLM 动态维护，构建四层记忆，按优先级注入上下文：

```
[system prompt]
[① 核心设定卡]   用户导入（粘贴/文件），静态常驻，可跨会话复用
[② 动态关键事实] LLM 自动抽取 + 手动增删，增量更新（改名/恋爱等）
[③ 剧情摘要]     LLM 切片分段滚动总结，n（切片宽度）可调，自动+手动触发
[④ 向量检索片段] 按需召回细节（复用 vector_memory 机制）
[最近 N 轮对话]
```

核心设计原则：
- **记忆维护是后台"尽力而为"的任务**，绝不阻塞/拖慢/搞崩主对话。
- 记忆维护失败需**自愈**（下次对话结束会重试），不永久丢失。
- 所有 LLM 维护调用统一用固定轻量模型 `config.MEMORY_MAINTENANCE_MODEL`（deepseek-chat）。

## 2. 数据模型

四层记忆存于 `sessions.memory` 列（JSON，`storage.py` 负责规范化）：

```python
memory = {
  "card":    {"content": "...", "source": "paste|file|card_lib", "updated_at": ...} | None,
  "facts":   [{"text": "...", "ts": ...}, ...],
  "summary": {"text": "...", "summarized_ts": ..., "last_round": int,
              "slice_rounds": int, "auto_rounds": int} | None,
  "vector":  {"enabled": bool, "model": str|None, "recent_n": int, "top_k": int|None},
}
```

- `last_round`：已总结到的对话轮数（增量总结的依据，一轮 = 一次 user 提问）
- `slice_rounds`：切片宽度（每 N 轮切一块喂 LLM），用户可设，默认 `config.SUMMARY_SLICE_ROUNDS`=8
- `auto_rounds`：自动触发阈值（新增 N 轮才自动总结），0=仅手动，默认 `config.SUMMARY_AUTO_ROUNDS`=10
- `vector.top_k`：None=默认；0=自动召回（不限制，按相似度阈值）；>0=固定 N 条

**角色卡库**（跨会话复用）：独立表 `character_cards(id, name, content, created_at, updated_at)`。

**旧数据迁移**：老会话的 `vm` 字段自动迁移到 `memory.vector`（`_migrate_vm_to_memory`），不丢配置。
注意 `storage.py` 的 `_init_db()` 在**文件末尾**调用（因为 `_migrate_vm_to_memory`/`_parse_vm` 定义在其后，模块级调用必须在所有函数定义之后）。

## 3. 文件结构与职责

| 文件 | 职责 |
|------|------|
| `memory_engine.py` | **四层记忆核心引擎**（新建）：`build_context` 拼接注入、`extract_facts` 事实抽取、`summarize_slice`/`merge_summary`/`run_summary` 剧情总结、`should_auto_summary`/`extract_facts_for_session` 自动维护 |
| `storage.py` | 数据层：`sessions.memory` 列读写、`character_cards` 表、各层读写函数（`set_session_card/facts/summary/summary_config/vector_config`） |
| `vector_memory.py` | 第④层：向量检索，`search`/`build_history` 支持 `top_k=0`（自动召回），返回消息带 `_src` 标记 |
| `app.py` | API 路由 + `_build_chat_context` 接入 + 流式收尾后台记忆维护（`_spawn_memory_maintenance`） |
| `config.py` | 记忆相关配置常量 |

## 4. 已完成后端功能

### 4.1 上下文注入（`memory_engine.build_context`）
按 ①→④ 顺序拼接，返回 `[{role, content}, ...]`。①②③ 无条件注入（system role），④ 按需 + 最近窗口。

### 4.2 动态关键事实（`extract_facts` / `extract_facts_for_session`）
- LLM 抽取「必须长期记住的关键事实」，增量合并进旧事实。
- 已加**模糊去重**（`_dedup_facts` + `_fact_similar`，difflib，阈值 0.62），解决"同义事实重复"。
- `config.FACT_MAX`（上限 50）、`FACT_EXTRACT_EVERY`（触发频率）。

### 4.3 剧情摘要（`summarize_slice` / `merge_summary` / `run_summary`）
- 切片分段总结：从 `last_round` 之后增量切片，每块 `slice_rounds` 轮，逐片总结再合并。
- 增量幂等：无新内容时 `run_summary` 返回旧摘要，不重复总结。
- prompt 已强化：**按时间顺序组织**、**忽略低价值机械性操作**（翻页/寒暄/流水账）。

### 4.4 流式收尾后台维护（`app.py`）
`generate()` 流式结束后，`_spawn_memory_maintenance` 在**后台线程**执行事实抽取 + 自动总结，不阻塞响应（曾因同步执行导致"卡在输出结尾"，已修复）。
- `_memory_maintenance_active` set + 锁做**并发去重**，同会话不并发重复总结。
- 记忆维护用**闭包捕获的 `current_username`**（流式迭代时请求上下文已销毁，不能访问 `flask.session`——曾踩过 `Working outside of request context` 的坑）。

### 4.5 网络层重试（`llm_client.py`）
用 urllib3 `Retry` + `HTTPAdapter`：对 DNS 抖动/连接超时/429/5xx 自动重试（指数退避），应对 `getaddrinfo failed`。

### 4.6 控制台上下文日志（`app.py _log_context`）
每次 `/api/chat` 打印实际上下文，**分段标记**：①核心设定卡/②动态关键事实/③剧情摘要/④向量检索/最近对话，向量检索范围清晰可辨。显示完整文本（无截断）。

## 5. 已提供的后端接口

| 接口 | 方法 | 作用 |
|------|------|------|
| `/api/sessions/<sid>/memory` | GET/POST | 读取/整体覆盖四层记忆 |
| `/api/sessions/<sid>/card` | POST | 写入核心设定卡 |
| `/api/sessions/<sid>/card-lib` | POST | 从角色卡库应用一张卡 |
| `/api/sessions/<sid>/facts` | POST | 整体覆盖动态关键事实 |
| `/api/sessions/<sid>/summary` | POST | 手动触发剧情总结（`slice_rounds` 可传） |
| `/api/sessions/<sid>/summary-config` | GET/POST | 读/设置 `slice_rounds`/`auto_rounds` |
| `/api/sessions/<sid>/vector-config` | GET/POST | 读/设置向量配置（`top_k`/`recent_n`/`enabled`/`model`/`auto`） |
| `/api/sessions/<sid>/facts-refresh` | POST | 手动刷新动态关键事实 |
| `/api/sessions/<sid>/refresh` | POST | **一键刷新全部记忆**（事实+摘要，强制） |
| `/api/cards` | GET/POST | 角色卡库列出/保存 |
| `/api/cards/<card_id>` | DELETE | 删除角色卡 |

## 6. 配置项（`config.py`）

```python
MEMORY_MAINTENANCE_MODEL = "deepseek-chat"   # 记忆维护统一用轻量模型
FACT_MAX = 50                                # 动态事实上限
FACT_EXTRACT_EVERY = 2                       # 事实抽取触发频率
SUMMARY_SLICE_ROUNDS = 8                     # 默认切片宽度（轮）
SUMMARY_AUTO_ROUNDS = 10                     # 默认自动总结阈值（0=仅手动）
SUMMARY_MAX_CHARS = 2000                     # 摘要字符上限
MEMORY_CONTEXT_MAX_CHARS = 12000             # 四层注入总字符硬上限
REQUEST_MAX_RETRIES = 3                      # LLM 网络重试
REQUEST_RETRY_BACKOFF = 1.0
REQUEST_RETRY_STATUS = (429, 500, 502, 503, 504)
```

## 7. 已修复的坑（重要经验）

1. **流式收尾访问 `flask.session` 会崩** → 闭包捕获 `username`。
2. **记忆维护同步执行阻塞流式响应** → 改后台线程 + 并发去重。
3. **SQLite 并发写锁冲突** → `_connect` 加 `timeout=10`。
4. **`_init_db()` 模块级调用顺序** → 移到文件末尾。
5. **同义事实重复** → difflib 模糊去重。
6. **剧情摘要时序混乱/记录流水账** → prompt 强化时间顺序 + 忽略低价值。
7. **`top_k or self.top_k` 吞掉 0** → 改为 `None` 才回退，`0` 保留表示自动召回。

## 8. 测试情况

- 后端已通过多轮模拟测试：四层注入、事实抽取去重、剧情总结、增量幂等、并发安全、`top_k` 语义、手动刷新接口。
- 测试均为 mock LLM（不真实调用），验证**机制正确性**；**真实 LLM 的抽取/总结质量尚未用真实 API 验证**。
- 测试账号：`test_user` / `test123456`，有一超长会话（208 条消息，原用于智能回忆验证）。

---

## 9. 待办事项（下一步 Agent）

### 阶段二：前端记忆设置面板（尚未做，主要工作）
在**现有设置弹窗内新增「记忆」tab 分页**，承载：
- [ ] **①核心设定卡**：文本框粘贴 + 文件导入 + 「保存到卡库/从卡库加载」下拉复用
- [ ] **②动态关键事实**：列表展示 + 手动增删（`facts` 接口）
- [ ] **③剧情摘要**：显示当前摘要、「立即总结」按钮（`refresh`/`summary` 接口）、切片宽度 `slice_rounds` 设置、自动阈值 `auto_rounds` 设置
- [ ] **④向量记忆**：开关、模型选择、`recent_n`、`top_k`（固定 N / 自动召回 `auto`）设置
- [ ] 接入 `vector-config` / `summary-config` / `card` / `facts` / `refresh` / `cards` 接口
- [ ] 旧 `vm` 设置迁移入口展示

### 后续优化（可选）
- [ ] 用真实 API Key 验证一次真实角色扮演对话的抽取/总结质量
- [ ] `MEMORY_CONTEXT_MAX_CHARS` 的容量防护是否生效验证
- [ ] 考虑 SQLite WAL 模式进一步降低锁冲突

## 10. 关键文件速查

- 上下文注入核心：`memory_engine.py::build_context`
- 事实去重：`memory_engine.py::_dedup_facts/_fact_similar`
- 摘要入口：`memory_engine.py::run_summary`
- 自动维护触发：`app.py::_spawn_memory_maintenance`
- 数据规范化：`storage.py::_parse_memory/_parse_vm/_parse_summary`
- 向量 top_k：`vector_memory.py::search/build_history`

/* 前端主逻辑：对话、参数面板、会话、预设、localStorage 管理 */

// ------------------------- 工具 -------------------------
const $ = (sel) => document.querySelector(sel);
const LS_KEY = "dsw_api_key";
const LS_PARAMS = "dsw_params";
const LS_MODEL = "dsw_model";

// 内联 SVG 图标（用 currentColor 描边，自动跟随主题文字色）。
// 内联可避免 Flask 对 .svg 的 MIME 类型不完整（image/svg 而非 image/svg+xml）
// 导致 CSS mask 不生效的问题。
const ICONS = {
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>',
  save: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>',
  theme: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>',
};

// 生成图标按钮内部的 svg 元素（统一尺寸类）
function svgIcon(name) {
  return ICONS[name] || "";
}

// 当前会话状态（前端内存态，持久化交给后端 storage）
let currentSessionId = null;
// 当前对话上下文：[{role, content}]，不含 system（system 由参数面板提供）
let conversation = [];

// ------------------------- 状态持久化 -------------------------
function saveApiKey(k) {
  if (k) localStorage.setItem(LS_KEY, k);
  else localStorage.removeItem(LS_KEY);
}
function loadApiKey() { return localStorage.getItem(LS_KEY) || ""; }

function saveParamsUI() {
  const p = readParamsFromUI();
  localStorage.setItem(LS_PARAMS, JSON.stringify(p));
}
function loadParamsUI() {
  const s = localStorage.getItem(LS_PARAMS);
  return s ? JSON.parse(s) : null;
}

// ------------------------- 参数读写 -------------------------
// 默认/范围/说明信息由后端 /api/params/default 提供，前端据此做范围回退
let PARAM_DEFAULTS = {};
let PARAM_RANGES = {};
let PARAM_META = {};
let STOP_MAX_ITEMS = 4;
let STOP_MAX_LEN = 32;

// 数值参数：超范围时回退到默认值；非数字也回退到默认
function clampParam(key, raw) {
  const def = PARAM_DEFAULTS[key];
  if (key === "stop") return def; // stop 单独处理
  const [lo, hi] = PARAM_RANGES[key] || [null, null];
  let v = parseFloat(raw);
  if (isNaN(v)) return def; // 非数字 -> 回退默认
  if (lo !== null && v < lo) v = lo; // 低于下限 -> 钳到下限
  if (hi !== null && v > hi) v = hi; // 高于上限 -> 钳到上限
  return key === "max_tokens" ? Math.round(v) : v;
}

// 处理 stop：逗号分隔，限制数量与单项长度，超长回退为截断（不丢弃）
function sanitizeStop(raw) {
  const s = (raw || "").toString();
  if (!s.trim()) return "";
  const items = s.split(",").map((x) => x.trim()).filter(Boolean);
  return items
    .slice(0, STOP_MAX_ITEMS)
    .map((x) => x.slice(0, STOP_MAX_LEN))
    .join(",");
}

function readParamsFromUI() {
  const p = { system_prompt: $("#system_prompt").value };
  document.querySelectorAll(".param-field input[data-key]").forEach((el) => {
    const key = el.dataset.key;
    p[key] = key === "stop" ? sanitizeStop(el.value) : clampParam(key, el.value);
  });
  return p;
}

function writeParamsToUI(p) {
  $("#system_prompt").value = p.system_prompt ?? PARAM_DEFAULTS.system_prompt ?? "";
  document.querySelectorAll(".param-field input[data-key]").forEach((el) => {
    const key = el.dataset.key;
    if (key in p && p[key] !== undefined && p[key] !== null) {
      el.value = p[key];
    } else if (key in PARAM_DEFAULTS) {
      el.value = PARAM_DEFAULTS[key];
    }
  });
}

// ------------------------- API 封装 -------------------------
async function apiGet(url) {
  const r = await fetch(url);
  return r.json();
}
async function apiPost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}
async function apiDelete(url) {
  const r = await fetch(url, { method: "DELETE" });
  return r.json();
}

// 流式对话：返回后端逐块文本，通过 onChunk 回调渲染
async function streamChat(payload, onChunk) {
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || "请求失败");
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder("utf-8");
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    onChunk(decoder.decode(value, { stream: true }));
  }
}

// ------------------------- 渲染 -------------------------
// 将文本以 Markdown 渲染进元素（思考与正文通用）
function renderMarkdown(el, text) {
  const src = (text || "").trim();
  el.innerHTML = (window.marked ? marked.parse(src) : src);
  if (!src) el.textContent = "";
}

function addMsgEl(role, text, markdown = true) {
  const box = $("#chatBox");
  const div = document.createElement("div");
  div.className = "msg " + role;
  if (markdown && window.marked) {
    renderMarkdown(div, text);
  } else {
    div.textContent = text || "";
  }
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

async function refreshSessions() {
  const list = await apiGet("/api/sessions");
  const ul = $("#sessionList");
  ul.innerHTML = "";
  list.forEach((s) => {
    const li = document.createElement("li");
    if (s.id === currentSessionId) li.className = "active";
    const title = document.createElement("span");
    title.textContent = s.title;
    title.style.flex = "1";
    title.onclick = () => openSession(s.id);
    const renameBtn = document.createElement("button");
    renameBtn.className = "item-btn";
    renameBtn.title = "重命名";
    renameBtn.innerHTML = svgIcon("edit");
    renameBtn.onclick = async (e) => {
      e.stopPropagation();
      const name = prompt("重命名会话：", s.title);
      if (name === null) return;
      const t = name.trim();
      if (!t) return;
      await apiPost("/api/sessions/" + s.id + "/rename", { title: t });
      refreshSessions();
    };
    const del = document.createElement("button");
    del.className = "item-btn danger";
    del.title = "删除";
    del.innerHTML = svgIcon("trash");
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("确定删除会话「" + s.title + "」？")) return;
      await apiDelete("/api/sessions/" + s.id);
      if (currentSessionId === s.id) {
        // 删的是当前会话：优先切到最新会话，无其他会话才新建
        const list = await apiGet("/api/sessions");
        if (list.length > 0) {
          await openSession(list[0].id);
        } else {
          await newSession();
        }
      } else {
        refreshSessions();
      }
    };
    li.appendChild(title);
    li.appendChild(renameBtn);
    li.appendChild(del);
    ul.appendChild(li);
  });
}

async function refreshPresets() {
  const list = await apiGet("/api/presets");
  const ul = $("#presetList");
  ul.innerHTML = "";
  list.forEach((p) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = p.name;
    name.style.flex = "1";
    name.onclick = () => applyPreset(p.params);
    const del = document.createElement("button");
    del.className = "item-btn danger";
    del.title = "删除";
    del.innerHTML = svgIcon("trash");
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("确定删除预设「" + p.name + "」？")) return;
      await apiDelete("/api/presets/" + encodeURIComponent(p.name));
      refreshPresets();
    };
    li.appendChild(name);
    li.appendChild(del);
    ul.appendChild(li);
  });
}

function applyPreset(params) {
  writeParamsToUI(params);
  saveParamsUI();
}

// ------------------------- 会话操作 -------------------------
async function newSession() {
  const s = await apiPost("/api/sessions", {});
  currentSessionId = s.id;
  conversation = [];
  $("#chatBox").innerHTML = "";
  refreshSessions();
}

// 渲染一条助手消息（支持思考折叠块），返回元素
function renderAssistant(text, reasoning) {
  const el = addMsgEl("assistant", text); // 正文已按 markdown 渲染
  if (reasoning) {
    const wrap = document.createElement("details");
    wrap.className = "reasoning";
    const summary = document.createElement("summary");
    summary.textContent = "思考过程";
    const body = document.createElement("div");
    body.className = "reasoning-body";
    renderMarkdown(body, reasoning); // 思考过程也渲染 markdown
    wrap.appendChild(summary);
    wrap.appendChild(body);
    el.insertBefore(wrap, el.firstChild);
  }
  return el;
}

async function openSession(id) {
  const s = await apiGet("/api/sessions/" + id);
  currentSessionId = id;
  // content 始终是纯回答，直接用于模型上下文；reasoning 仅渲染
  conversation = s.messages
    .filter((m) => m.role !== "system")
    .map((m) => ({ role: m.role, content: m.content }));
  const box = $("#chatBox");
  box.innerHTML = "";
  s.messages
    .filter((m) => m.role !== "system")
    .forEach((m) => {
      if (m.role === "assistant") {
        renderAssistant(m.content, m.reasoning || null);
      } else {
        addMsgEl(m.role, m.content);
      }
    });
  refreshSessions();
  // 切换后清理其他空会话（保留当前正在查看的）
  await cleanupEmptySessions(currentSessionId);
}

// 清理空会话（无消息），排除 excludeId 指向的当前会话
async function cleanupEmptySessions(excludeId) {
  try {
    await apiPost("/api/sessions/cleanup", { exclude: excludeId });
    refreshSessions();
  } catch (_) {
    /* 清理失败不影响主流程 */
  }
}

// ------------------------- 发送消息 -------------------------
async function sendMessage() {
  const input = $("#userInput");
  const text = input.value.trim();
  if (!text) return;

  const apiKey = loadApiKey();
  if (!apiKey) {
    alert("请先点击右上角设置按钮，在设置面板中输入 API Key");
    return;
  }
  const model = $("#modelSelect").value;

  // 渲染用户消息
  addMsgEl("user", text);
  input.value = "";
  conversation.push({ role: "user", content: text });

  // 渲染助手占位
  const assistantEl = addMsgEl("assistant", "");
  // 推理过程折叠块：默认不创建，仅当真正收到 reasoning 内容时才按需创建
  let reasoningWrap = null;
  let reasoningBody = null;
  const ensureReasoning = () => {
    if (reasoningWrap) return reasoningWrap;
    reasoningWrap = document.createElement("details");
    reasoningWrap.className = "reasoning";
    reasoningWrap.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "思考过程";
    reasoningBody = document.createElement("div");
    reasoningBody.className = "reasoning-body";
    reasoningWrap.appendChild(summary);
    reasoningWrap.appendChild(reasoningBody);
    assistantEl.insertBefore(reasoningWrap, assistantEl.firstChild);
    return reasoningWrap;
  };

  const sendBtn = $("#sendBtn");
  sendBtn.disabled = true;
  try {
    let full = "";
    let reasoning = "";
    let mode = "reasoning"; // 当前渲染模式：reasoning / answer
    // 累积缓冲：网络分包可能把标记切开，或把多个标记拼在一起，
    // 必须按标记边界切分，只处理完整片段，尾部不完整部分留到下次。
    let buffer = "";
    const MARK = "<<REASONING>>";
    const MARK_A = "<<ANSWER>>";
    const MAX_PREFIX = Math.max(MARK.length, MARK_A.length) - 1; // 可能形成标记的最大残留长度
    const flush = () => {
      // 在 buffer 中找最先出现的完整标记
      const idxR = buffer.indexOf(MARK);
      const idxA = buffer.indexOf(MARK_A);
      let next = -1;
      let nextMode = null;
      if (idxR !== -1 && (idxA === -1 || idxR < idxA)) {
        next = idxR; nextMode = "reasoning";
      } else if (idxA !== -1) {
        next = idxA; nextMode = "answer";
      }
      if (next === -1) {
        // buffer 中没有完整标记：把不可能再拼接成标记的安全前缀先渲染，
        // 保留末尾最多 MAX_PREFIX 个字符（可能是被切开的标记的一部分）。
        if (buffer.length > MAX_PREFIX) {
          const safeLen = buffer.length - MAX_PREFIX;
          applyChunk(buffer.slice(0, safeLen));
          buffer = buffer.slice(safeLen);
        }
        return;
      }
      // next 之前是上一段的延续文本（无新标记前缀），按当前 mode 处理
      const before = buffer.slice(0, next);
      if (before) applyChunk(before);
      // 切换到新标记模式
      mode = nextMode;
      buffer = buffer.slice(next + (nextMode === "reasoning" ? MARK : MARK_A).length);
      // 递归处理剩余 buffer（可能还含下一个标记）
      flush();
    };
    // 正文容器：markdown 渲染只作用于此，不影响思考折叠块
    const answerDiv = document.createElement("div");
    answerDiv.className = "msg-content";
    assistantEl.appendChild(answerDiv);
    const applyChunk = (text) => {
      if (!text) return;
      if (mode === "reasoning") {
        reasoning += text;
        ensureReasoning();
        reasoningBody.textContent = reasoning; // 流式实时显示纯文本
      } else {
        if (reasoning && reasoningWrap && reasoningWrap.open) reasoningWrap.open = false;
        answerDiv.appendChild(document.createTextNode(text)); // 流式实时显示纯文本
        full += text;
      }
      $("#chatBox").scrollTop = $("#chatBox").scrollHeight;
    };
    await streamChat(
      {
        api_key: apiKey,
        model,
        messages: conversation,
        ...readParamsFromUI(),
      },
      (chunk) => {
        buffer += chunk;
        flush();
      }
    );
    // 流结束后处理残留缓冲
    if (buffer) {
      applyChunk(buffer);
      buffer = "";
    }
    // 流式结束后再做一次 Markdown 渲染（思考与正文都生效）
    if (reasoning && reasoningBody) renderMarkdown(reasoningBody, reasoning);
    renderMarkdown(answerDiv, full);
    // 存历史：content 存纯回答（用于渲染+上传），reasoning 单独存（仅渲染）
    if (currentSessionId) {
      await apiPost("/api/sessions/" + currentSessionId + "/msg", { role: "user", content: text });
      await apiPost("/api/sessions/" + currentSessionId + "/msg", {
        role: "assistant",
        content: full,
        reasoning: reasoning || undefined,
      });
      refreshSessions();
    }
    conversation.push({ role: "assistant", content: full });
  } catch (e) {
    assistantEl.textContent = "[错误] " + e.message;
  } finally {
    sendBtn.disabled = false;
  }
}

// ------------------------- 初始化 -------------------------
async function init() {
  // 模型列表
  const models = await apiGet("/api/models");
  const sel = $("#modelSelect");
  models.forEach((m) => {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.label;
    sel.appendChild(o);
  });
  const savedModel = localStorage.getItem(LS_MODEL);
  if (savedModel) sel.value = savedModel;
  sel.onchange = () => localStorage.setItem(LS_MODEL, sel.value);

  // 默认参数 / 范围 / 说明
  const def = await apiGet("/api/params/default");
  PARAM_DEFAULTS = def.defaults || {};
  PARAM_RANGES = def.ranges || {};
  PARAM_META = def.meta || {};
  STOP_MAX_ITEMS = def.stop_max_items || 4;
  STOP_MAX_LEN = def.stop_max_len || 32;

  // 渲染每个参数的说明文字（含取值范围）
  document.querySelectorAll(".param-hint[data-hint]").forEach((el) => {
    const key = el.dataset.hint;
    const meta = PARAM_META[key] || "";
    const rng = PARAM_RANGES[key];
    let tip = meta;
    if (rng) tip += `（范围 ${rng[0]}~${rng[1]}）`;
    el.textContent = tip;
  });

  const saved = loadParamsUI();
  writeParamsToUI(saved || PARAM_DEFAULTS);

  // 参数面板改动即存（失焦时回退到合法范围）
  document.querySelectorAll(".param-field input[data-key], #system_prompt").forEach((el) => {
    el.addEventListener("change", () => {
      const p = readParamsFromUI();
      writeParamsToUI(p); // 把回退后的值写回输入框
      saveParamsUI();
    });
  });

  // API Key
  $("#apiKeyInput").value = loadApiKey();
  $("#apiKeyInput").addEventListener("change", (e) => saveApiKey(e.target.value));
  $("#clearKeyBtn").onclick = () => {
    saveApiKey("");
    $("#apiKeyInput").value = "";
  };

  // 设置面板（右侧抽屉）
  const openSettings = () => {
    $("#settingsPanel").classList.remove("hidden");
    $("#settingsOverlay").classList.remove("hidden");
    $("#settingsPanel").setAttribute("aria-hidden", "false");
  };
  const closeSettings = () => {
    writeParamsToUI(readParamsFromUI()); // 关闭前先把越界值回退
    $("#settingsPanel").classList.add("hidden");
    $("#settingsOverlay").classList.add("hidden");
    $("#settingsPanel").setAttribute("aria-hidden", "true");
    saveParamsUI();
  };
  $("#openSettingsBtn").onclick = openSettings;
  $("#closeSettingsBtn").onclick = closeSettings;
  $("#settingsOverlay").onclick = closeSettings;

  // 深色/浅色切换
  const themeBtn = $("#themeBtn");
  const applyTheme = (t) => {
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem("dsw_theme", t);
    themeBtn.title = t === "dark" ? "切换到浅色" : "切换到深色";
  };
  applyTheme(localStorage.getItem("dsw_theme") || "light");
  themeBtn.onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme");
    applyTheme(cur === "dark" ? "light" : "dark");
  };

  // 按钮
  $("#sendBtn").onclick = sendMessage;
  $("#userInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  $("#newSessionBtn").onclick = newSession;
  $("#savePresetBtn").onclick = async () => {
    const name = prompt("预设名称：");
    if (!name) return;
    await apiPost("/api/presets", { name, params: readParamsFromUI() });
    refreshPresets();
  };

  // 刷新侧栏
  refreshSessions();
  refreshPresets();
  // 默认新建一个会话
  if (!currentSessionId) await newSession();
  // 启动时清理其他空会话（保留当前会话）
  await cleanupEmptySessions(currentSessionId);
}

init();

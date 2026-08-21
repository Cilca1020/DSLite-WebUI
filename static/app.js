/* 前端主逻辑：对话、参数面板、会话、预设、localStorage 管理 */

// ------------------------- 工具 -------------------------
const $ = (sel) => document.querySelector(sel);
const LS_KEY = "dsw_api_key";
const LS_PARAMS = "dsw_params";
const LS_MODEL = "dsw_model";

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
function readParamsFromUI() {
  return {
    temperature: parseFloat($("#temperature").value),
    top_p: parseFloat($("#top_p").value),
    max_tokens: parseInt($("#max_tokens").value, 10),
    system_prompt: $("#system_prompt").value,
  };
}
function writeParamsToUI(p) {
  $("#temperature").value = p.temperature;
  $("#top_p").value = p.top_p;
  $("#max_tokens").value = p.max_tokens;
  $("#system_prompt").value = p.system_prompt;
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
function addMsgEl(role, text) {
  const box = $("#chatBox");
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
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
    const del = document.createElement("span");
    del.className = "item-del";
    del.textContent = "✕";
    del.onclick = async (e) => {
      e.stopPropagation();
      await apiDelete("/api/sessions/" + s.id);
      if (currentSessionId === s.id) newSession();
      refreshSessions();
    };
    li.appendChild(title);
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
    const del = document.createElement("span");
    del.className = "item-del";
    del.textContent = "✕";
    del.onclick = async (e) => {
      e.stopPropagation();
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

async function openSession(id) {
  const s = await apiGet("/api/sessions/" + id);
  currentSessionId = id;
  conversation = s.messages
    .filter((m) => m.role !== "system")
    .map((m) => ({ role: m.role, content: m.content }));
  const box = $("#chatBox");
  box.innerHTML = "";
  conversation.forEach((m) => addMsgEl(m.role, m.content));
  refreshSessions();
}

// ------------------------- 发送消息 -------------------------
async function sendMessage() {
  const input = $("#userInput");
  const text = input.value.trim();
  if (!text) return;

  const apiKey = loadApiKey();
  if (!apiKey) {
    alert("请先在右上角输入 API Key");
    return;
  }
  const model = $("#modelSelect").value;

  // 渲染用户消息
  addMsgEl("user", text);
  input.value = "";
  conversation.push({ role: "user", content: text });

  // 渲染助手占位
  const assistantEl = addMsgEl("assistant", "");

  const sendBtn = $("#sendBtn");
  sendBtn.disabled = true;
  try {
    let full = "";
    await streamChat(
      {
        api_key: apiKey,
        model,
        messages: conversation,
        ...readParamsFromUI(),
      },
      (chunk) => {
        full += chunk;
        assistantEl.textContent = full;
        $("#chatBox").scrollTop = $("#chatBox").scrollHeight;
      }
    );
    // 存历史
    if (currentSessionId) {
      await apiPost("/api/sessions/" + currentSessionId + "/msg", { role: "user", content: text });
      await apiPost("/api/sessions/" + currentSessionId + "/msg", { role: "assistant", content: full });
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

  // 默认参数
  const def = await apiGet("/api/params/default");
  const saved = loadParamsUI();
  writeParamsToUI(saved || def);

  // 参数面板改动即存
  ["#temperature", "#top_p", "#max_tokens", "#system_prompt"].forEach((id) => {
    $(id).addEventListener("change", saveParamsUI);
  });

  // API Key
  $("#apiKeyInput").value = loadApiKey();
  $("#apiKeyInput").addEventListener("change", (e) => saveApiKey(e.target.value));
  $("#clearKeyBtn").onclick = () => {
    saveApiKey("");
    $("#apiKeyInput").value = "";
  };

  // 折叠参数
  $("#toggleParamsBtn").onclick = () => {
    $("#paramsPanel").classList.toggle("hidden");
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
}

init();

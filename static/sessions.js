/* 参数读写 + 会话管理（新建/打开/清理/列表） */

// 默认/范围/说明信息由后端 /api/params/default 提供，前端据此做范围回退
let PARAM_DEFAULTS = {};
let PARAM_RANGES = {};
let PARAM_META = {};
let STOP_MAX_ITEMS = 4;
let STOP_MAX_LEN = 32;

function saveApiKey(k) {
  if (k) localStorage.setItem(LS_KEY, k);
  else localStorage.removeItem(LS_KEY);
}

function loadApiKey() { return localStorage.getItem(LS_KEY) || ""; }

// 确保存在当前会话：未选中任何对话时，用当前 UI 的 model + 参数创建新会话。
// 创建后立即出现在侧栏，可在左侧跳转。已选中会话时直接返回。
async function ensureSession() {
  if (currentSessionId) return currentSessionId;
  const model = $("#modelSelect").value || ($("#modelSelect").options[0] || {}).value || "";
  const s = await apiPost("/api/sessions", {
    model: model,
    params: readParamsFromUI(),
  });
  currentSessionId = s.id;
  refreshSessions();
  return currentSessionId;
}

// 把当前 UI 上的 model + 推理参数保存到当前会话（每个对话一套独立配置）。
// 未选中对话时先创建会话再保存——修改配置即正式创建对话。
async function saveSessionConfig() {
  if (!currentSessionId) await ensureSession();
  if (!currentSessionId) return; // 创建失败则放弃保存
  const cfg = {
    model: $("#modelSelect").value,
    params: readParamsFromUI(),
  };
  try {
    await apiPost("/api/sessions/" + currentSessionId + "/config", cfg);
  } catch (_) {
    /* 保存失败不影响对话 */
  }
}

// 把会话配置（model + params）应用到 UI
function applyConfigToUI(model, params) {
  if (model) $("#modelSelect").value = model;
  if (params) {
    writeParamsToUI(params);
  } else {
    writeParamsToUI(PARAM_DEFAULTS);
  }
}

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

// 渲染侧边栏会话列表
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
    li.style.cursor = "pointer";
    li.onclick = () => openSession(s.id);
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

// 把界面重置为「未选中对话」的空白态：清空对话区、显示「你好」欢迎提示、
// 参数重置为默认值。不创建会话。模型沿用当前 UI 选中值（上一个浏览对话使用的模型）。
function resetChatUI() {
  currentSessionId = null;
  conversation = [];
  const box = $("#chatBox");
  box.innerHTML = "";
  showEmptyHint(box); // 未选中对话时居中显示「你好」欢迎提示
  const model = $("#modelSelect").value || ($("#modelSelect").options[0] || {}).value || "";
  applyConfigToUI(model, PARAM_DEFAULTS);
  refreshSessions(); // 清除侧栏激活态
}

// 新建会话：已选中对话时重置为空白态（不创建会话）；
// 未选中对话时提示已是最新。只有修改配置参数、prompt 或发送消息才正式创建会话。
async function newSession() {
  if (!currentSessionId) {
    showToast("已经是最新对话");
    return;
  }
  resetChatUI();
}

// 打开会话：载入会话配置与历史消息并渲染
async function openSession(id) {
  const s = await apiGet("/api/sessions/" + id);
  currentSessionId = id;
  // 载入该会话独立的 model + 推理参数
  applyConfigToUI(s.model, s.params);
  // content 始终是纯回答，直接用于模型上下文；reasoning 仅渲染；files 仅 user 消息携带
  conversation = s.messages
    .filter((m) => m.role !== "system")
    .map((m) => ({ role: m.role, content: m.content, files: m.files || null }));
  const box = $("#chatBox");
  box.innerHTML = "";
  // 先确定最新一轮 user 消息下标（仅这一轮显示编辑按钮），再渲染
  lastUserMsgIndex = null;
  let mIdxPre = 0;
  s.messages
    .filter((m) => m.role !== "system")
    .forEach((m) => {
      if (m.role === "user") lastUserMsgIndex = mIdxPre;
      mIdxPre++;
    });
  let mIdx = 0; // 仅统计 user/assistant 的下标，与后端 delete_message 对齐
  s.messages
    .filter((m) => m.role !== "system")
    .forEach((m) => {
      if (m.role === "assistant") {
        renderAssistant(m.content, m.reasoning || null, mIdx);
      } else {
        // 用户消息强制纯文本：Markdown 语法按原样显示，不渲染
        addMsgEl(m.role, m.content, false, mIdx, m.files || null);
      }
      mIdx++;
    });
  // 渲染完成后滚动到最底部
  box.scrollTop = box.scrollHeight;
  // 空会话（无消息）时居中显示欢迎提示，否则移除
  if (!box.querySelector(".msg-row")) showEmptyHint(box);
  else hideEmptyHint(box);
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

/* 记忆分页：设置面板「记忆」页的四层记忆管理
 *
 * 层级顺序（与后端注入顺序一致）：
 *   ⓪ 系统提示词        —— 无条件注入，作为 system 消息最先发送
 *   ① 人物卡（card）    —— 无条件注入，随每次请求作为 system 前缀
 *   ② 动态关键事实      —— 无条件注入，自动抽取 + 可手动触发
 *   ③ 剧情摘要          —— 无条件注入，增量总结 + 可手动触发
 *   ④ 向量记忆          —— 按需召回，检索相似历史片段
 *
 * 记忆按会话单独存储；未选中会话时显示占位提示。
 */

// 当前记忆数据缓存（openSession / resetChatUI 时刷新）
let MEMORY_STATE = null;

// 读取记忆面板上的元素（可能尚未渲染，统一用安全取值）
const memEl = (id) => document.getElementById(id);

// 把「记忆」页各卡片恢复为未选中会话的占位态（顶部批量按钮一并隐藏）
function resetMemoryPanel() {
  MEMORY_STATE = null;
  const empty = memEl("memoryEmptyHint");
  if (empty) empty.classList.remove("hidden");
  const bar = memEl("memoryMasterBar");
  if (bar) bar.classList.add("hidden");
  ["memorySystemGroup", "memoryCardGroup", "memoryFactsGroup", "memorySummaryGroup", "memoryVectorGroup"].forEach((id) => {
    const el = memEl(id);
    if (el) el.classList.add("hidden");
  });
}

// 异步加载当前会话的四层记忆并渲染到「记忆」页。
// 打开设置面板 / 切换会话时调用；无会话时显示占位提示。
async function loadMemoryPanel() {
  const empty = memEl("memoryEmptyHint");
  const groups = ["memorySystemGroup", "memoryCardGroup", "memoryFactsGroup", "memorySummaryGroup", "memoryVectorGroup"].map(memEl);

  if (!currentSessionId) {
    resetMemoryPanel();
    return;
  }
  if (empty) empty.classList.add("hidden");
  const bar = memEl("memoryMasterBar");
  if (bar) bar.classList.remove("hidden");
  groups.forEach((el) => el && el.classList.remove("hidden"));

  try {
    const r = await apiGet("/api/sessions/" + currentSessionId + "/memory");
    if (!r || r.error) {
      showToast(r && r.error ? r.error : "读取记忆失败");
      return;
    }
    MEMORY_STATE = r.memory || {};
    renderMemoryPanel();
  } catch (_) {
    showToast("读取记忆失败");
  }

  // 角色卡库下拉框（独立于记忆加载，失败不阻塞）
  refreshCardLibSelect();
}

// 把记忆数据渲染到各卡片
function renderMemoryPanel() {
  const mem = MEMORY_STATE || {};

  // ① 人物卡
  const card = mem.card || {};
  const cardInput = memEl("memoryCardInput");
  if (cardInput && !cardInput.dataset.dirty) cardInput.value = card.content || "";

  // ② 动态关键事实
  renderFacts();

  // 最近 N 轮上下文策略
  const recentN = (mem.recent_n !== undefined && mem.recent_n !== null)
    ? mem.recent_n
    : ((mem.vector || {}).recent_n !== undefined ? (mem.vector || {}).recent_n : 10);
  const recentField = memEl("memoryContextN") || memEl("vmRecentN");
  if (recentField) recentField.value = recentN;
  if (window.vmSaveN) window.vmSaveN(recentN);

  // ③ 剧情摘要
  const summary = mem.summary || {};
  const summaryText = memEl("memorySummaryText");
  if (summaryText) summaryText.value = summary.text || "";
  const meta = memEl("memorySummaryMeta");
  if (meta) {
    const last = summary.last_round;
    meta.textContent = (last
      ? "已总结至第 " + last + " 轮" + (summary.slice_rounds ? "（切片 " + summary.slice_rounds + " 轮）" : "")
      : "尚未总结");
  }
  const sliceInput = memEl("memorySummarySlice");
  if (sliceInput && summary.slice_rounds !== undefined && summary.slice_rounds !== null) {
    sliceInput.value = summary.slice_rounds;
  }
  const autoInput = memEl("memorySummaryAuto");
  if (autoInput && summary.auto_rounds !== undefined && summary.auto_rounds !== null) {
    autoInput.value = summary.auto_rounds;
  }

  // ④ 向量记忆：模型/开关/N 由 applyVmToUI 管理，这里同步 top_k 输入框
  const vec = mem.vector || {};
  const topK = memEl("vmTopK");
  if (topK) {
    // 0 表示自动召回，作为合法值保留；null/undefined 显示为空（后端用默认）
    topK.value = (vec.top_k === null || vec.top_k === undefined) ? "" : vec.top_k;
    topK.disabled = !vec.enabled;
  }
  refreshMemoryCardStates();
}

// 向量记忆层：模型选择/开关/N 的可见性与禁用由 main.js（syncVmUi）管理，
// 这里只保证 top_k 输入框跟随启用状态
function syncMemoryVectorField() {
  const en = memEl("vmEnabled");
  const topK = memEl("vmTopK");
  const topKField = memEl("vmTopKField");
  const hasModel = !!(window.vmLoadModel && window.vmLoadModel());
  if (topKField) topKField.classList.toggle("vm-hidden", !hasModel);
  if (topK) topK.disabled = !hasModel || !(en && en.checked);
}

// 同步卡片 2/3/4 的开关状态与置灰（内容保留但不可编辑）。
// 在渲染完成、开关变化、一键配置/关闭智能总结后即时调用；
// 通过操作 checkbox 的 checked 触发 CSS 过渡，让开关动画即时生效（无需刷新）。
function refreshMemoryCardStates() {
  const mem = MEMORY_STATE || {};

  // ② 动态关键事实
  const factsEnabled = mem.facts_enabled !== false;
  const factsSwitch = memEl("memoryFactsEnabled");
  if (factsSwitch) factsSwitch.checked = factsEnabled;
  const factsGroup = memEl("memoryFactsGroup");
  if (factsGroup) factsGroup.classList.toggle("memory-off", !factsEnabled);
  // 自动总结开关（总开关下一级）：默认开启
  const factsAuto = memEl("memoryFactsAuto");
  if (factsAuto) factsAuto.checked = mem.facts_auto !== false;

  // ③ 剧情摘要
  const summary = mem.summary || {};
  const summaryEnabled = summary.enabled !== false;
  const summarySwitch = memEl("memorySummaryEnabled");
  if (summarySwitch) summarySwitch.checked = summaryEnabled;
  const summaryGroup = memEl("memorySummaryGroup");
  if (summaryGroup) summaryGroup.classList.toggle("memory-off", !summaryEnabled);

  // ④ 向量记忆：开关由 main.js / chat.js（vmEnabled）管理，这里按可见开关态同步置灰与 top_k 禁用态
  const vectorSwitch = memEl("vmEnabled");
  const vecEnabled = vectorSwitch ? !!vectorSwitch.checked : !!(mem.vector || {}).enabled;
  const vectorGroup = memEl("memoryVectorGroup");
  if (vectorGroup) vectorGroup.classList.toggle("memory-off", !vecEnabled);
  syncMemoryVectorField();
}

// 刷新角色卡库下拉框（跨会话复用的人物卡）
async function refreshCardLibSelect() {
  const sel = memEl("memoryCardLibSelect");
  if (!sel) return;
  try {
    const r = await apiGet("/api/cards");
    const cards = (r && r.cards) || [];
    sel.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = cards.length ? "选择角色卡…" : "卡库为空";
    sel.appendChild(placeholder);
    cards.forEach((c) => {
      const o = document.createElement("option");
      o.value = c.id;
      o.textContent = c.name || c.id;
      sel.appendChild(o);
    });
    sel.disabled = !cards.length;
  } catch (_) {
    sel.innerHTML = '<option value="">卡库读取失败</option>';
  }
}

/* ---------------- 事件绑定 ---------------- */

// ① 人物卡：保存设定
function bindMemoryCard() {
  const btn = memEl("memoryCardSaveBtn");
  if (!btn) return;
  btn.onclick = async () => {
    if (!currentSessionId) return showToast("请先选择对话");
    const input = memEl("memoryCardInput");
    const content = (input && input.value || "").trim();
    if (!content) return showToast("设定内容为空");
    btn.disabled = true;
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/card", {
        content: content,
        source: "paste",
      });
      if (r && r.error) return showToast(r.error);
      input.dataset.dirty = "";
      showToast("人物卡已保存");
    } catch (_) {
      showToast("保存失败");
    } finally {
      btn.disabled = false;
    }
  };
  // 内容被用户编辑后标记 dirty，避免渲染时被后端数据覆盖
  const input = memEl("memoryCardInput");
  if (input) input.addEventListener("input", () => { input.dataset.dirty = "1"; });
}

// ① 人物卡：从卡库应用
function bindMemoryCardLib() {
  const btn = memEl("memoryCardLibApplyBtn");
  if (!btn) return;
  btn.onclick = async () => {
    if (!currentSessionId) return showToast("请先选择对话");
    const sel = memEl("memoryCardLibSelect");
    const cardId = sel && sel.value;
    if (!cardId) return showToast("请先选择一张角色卡");
    btn.disabled = true;
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/card-lib", { card_id: cardId });
      if (r && r.error) return showToast(r.error);
      const input = memEl("memoryCardInput");
      if (input) {
        input.value = (r.memory && r.memory.card && r.memory.card.content) || "";
        input.dataset.dirty = "";
      }
      showToast("已应用角色卡");
    } catch (_) {
      showToast("应用失败");
    } finally {
      btn.disabled = false;
    }
  };
}

// ② 动态关键事实：条目级操作 API（增删改 / 上锁解锁）
async function factsItemOp(op, index, extra = {}) {
  if (!currentSessionId) { showToast("请先选择对话"); return null; }
  try {
    const r = await apiPost("/api/sessions/" + currentSessionId + "/facts-item", Object.assign({ op, index }, extra));
    if (r && r.error) { showToast(r.error); return null; }
    if (r && r.memory) {
      MEMORY_STATE = r.memory;
      renderFacts();
    }
    return r;
  } catch (_) {
    showToast("操作失败");
    return null;
  }
}

// 小图标按钮（锁 / 编辑 / 删除）
function factIconBtn(title, svgPath, onClick) {
  const btn = document.createElement("button");
  btn.className = "fact-icon-btn";
  btn.type = "button";
  btn.title = title;
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + svgPath + "</svg>";
  btn.addEventListener("click", onClick);
  return btn;
}

// ② 渲染事实条目列表：每条带 上锁/编辑/删除 操作
function renderFacts() {
  const mem = MEMORY_STATE || {};
  const facts = Array.isArray(mem.facts) ? mem.facts : [];
  const factsList = memEl("memoryFactsList");
  if (!factsList) return;
  factsList.innerHTML = "";
  if (!facts.length) {
    const p = document.createElement("p");
    p.className = "memory-empty-text";
    p.textContent = "暂无事实。对话中会自动抽取，也可手动添加或点击「重新总结」。";
    factsList.appendChild(p);
    return;
  }
  const LOCK_SVG = '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>';               // 闭锁
  const UNLOCK_SVG = '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.9-.9"/>';            // 开锁
  const EDIT_SVG = '<path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>';
  const DEL_SVG = '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>';
  facts.forEach((f, i) => {
    const locked = !!(f && f.locked);
    const row = document.createElement("div");
    row.className = "memory-fact-row" + (locked ? " fact-locked" : "");

    const idx = document.createElement("span");
    idx.className = "memory-fact-index";
    idx.textContent = String(i + 1).padStart(2, "0");

    const text = document.createElement("span");
    text.className = "memory-fact-text";
    text.textContent = (f && f.text) ? String(f.text) : "";

    const actions = document.createElement("span");
    actions.className = "memory-fact-actions";

    // 上锁/解锁：即时生效
    actions.appendChild(factIconBtn(locked ? "解锁（重新总结时可被更新）" : "上锁（不被重新总结覆盖）", locked ? UNLOCK_SVG : LOCK_SVG, async (e) => {
      const r = await factsItemOp("lock", i, { locked: !locked });
      if (r) showToast(!locked ? "已上锁" : "已解锁");
    }));

    // 编辑：文本变输入框，Enter/失焦保存，Esc 取消
    actions.appendChild(factIconBtn("编辑", EDIT_SVG, () => {
      if (row.querySelector("input")) return;
      const input = document.createElement("input");
      input.className = "memory-fact-edit";
      input.type = "text";
      input.maxLength = 200;
      input.value = text.textContent;
      row.replaceChild(input, text);
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      let done = false;
      const finish = (save) => {
        if (done) return;
        done = true;
        const v = input.value.trim();
        row.replaceChild(text, input);
        if (save && v && v !== text.textContent) factsItemOp("update", i, { text: v });
      };
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") finish(true);
        else if (ev.key === "Escape") finish(false);
      });
      input.addEventListener("blur", () => finish(true));
    }));

    // 删除
    actions.appendChild(factIconBtn("删除", DEL_SVG, async () => {
      const r = await factsItemOp("delete", i);
      if (r) showToast("已删除");
    }));

    row.appendChild(idx);
    row.appendChild(text);
    row.appendChild(actions);
    factsList.appendChild(row);
  });
}

// ② 绑定手动添加条目
function bindFactAdd() {
  const input = memEl("memoryFactAddInput");
  const btn = memEl("memoryFactAddBtn");
  if (!input || !btn) return;
  const add = async () => {
    const v = input.value.trim();
    if (!v) return showToast("内容不能为空");
    const r = await factsItemOp("add", -1, { text: v });
    if (r) { input.value = ""; showToast("已添加"); }
  };
  btn.addEventListener("click", add);
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") add(); });
}

// ② 动态关键事实：手动重新总结（从全部历史重新抽取并合并覆盖）
function bindMemoryFacts() {
  const btn = memEl("memoryFactsRefreshBtn");
  if (!btn) return;
  btn.onclick = async () => {
    if (!currentSessionId) return showToast("请先选择对话");
    const apiKey = loadApiKey();
    if (!apiKey) return showToast("请先在「参数」页填写 API Key");
    btn.disabled = true;
    btn.textContent = "总结中…";
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/facts-refresh", { api_key: apiKey, full: true });
      if (r && r.error) return showToast(r.error || "没有新增事实");
      showToast("事实已更新");
      await loadMemoryPanel();
    } catch (_) {
      showToast("总结失败");
    } finally {
      btn.disabled = false;
      btn.textContent = "重新总结";
    }
  };
}

// 最近 N 轮：独立上下文保留策略
function bindMemoryContext() {
  const field = memEl("memoryContextN");
  const saveBtn = memEl("memoryContextSaveBtn");
  if (!field) return;
  const saveCurrent = async () => {
    if (!currentSessionId) return;
    const raw = field.value;
    let recent_n = null;
    if (raw !== "" && raw !== null && raw !== undefined) {
      recent_n = Math.max(0, Math.min(1000, parseInt(raw, 10) || 0));
      field.value = recent_n;
    }
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/context-config", { recent_n: recent_n });
      if (r && r.error) return showToast(r.error);
      if (window.vmSaveN) window.vmSaveN(r.recent_n);
      showToast("上下文策略已保存");
      await loadMemoryPanel();
    } catch (_) {
      showToast("保存失败");
    }
  };
  field.addEventListener("change", saveCurrent);
  if (saveBtn) saveBtn.addEventListener("click", saveCurrent);
}

// ③ 剧情摘要：保存配置 + 重新总结
function bindMemorySummary() {
  const saveBtn = memEl("memorySummarySaveConfigBtn");
  if (saveBtn) {
    saveBtn.onclick = async () => {
      if (!currentSessionId) return showToast("请先选择对话");
      const slice = parseInt(memEl("memorySummarySlice").value, 10);
      const auto = parseInt(memEl("memorySummaryAuto").value, 10);
      const body = {};
      if (!isNaN(slice)) body.slice_rounds = Math.max(1, Math.min(200, slice));
      if (!isNaN(auto)) body.auto_rounds = Math.max(0, Math.min(1000, auto));
      if (!Object.keys(body).length) return showToast("请填写配置");
      saveBtn.disabled = true;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/summary-config", body);
        if (r && r.error) return showToast(r.error);
        showToast("摘要配置已保存");
        await loadMemoryPanel();
      } catch (_) {
        showToast("保存失败");
      } finally {
        saveBtn.disabled = false;
      }
    };
  }

  const runBtn = memEl("memorySummaryRunBtn");
  if (runBtn) {
    runBtn.onclick = async () => {
      if (!currentSessionId) return showToast("请先选择对话");
      const apiKey = loadApiKey();
      if (!apiKey) return showToast("请先在「参数」页填写 API Key");
      runBtn.disabled = true;
      runBtn.textContent = "总结中…";
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/summary", { api_key: apiKey, full: true });
        if (r && r.error) return showToast(r.error || "总结失败");
        showToast("摘要已重新生成");
        await loadMemoryPanel();
      } catch (_) {
        showToast("总结失败");
      } finally {
        runBtn.disabled = false;
        runBtn.textContent = "重新总结";
      }
    };
  }
}

// ④ 向量记忆：top_k 输入保存（模型/开关/N 的保存逻辑在 main.js / sessions.js）
function bindMemoryVectorTopK() {
  const topK = memEl("vmTopK");
  if (!topK) return;
  topK.addEventListener("change", async () => {
    if (!currentSessionId) return;
    let v = parseInt(topK.value, 10);
    // 空值 -> 不传（后端用默认）；否则钳制到 0~500
    const body = {};
    if (topK.value === "" || isNaN(v)) {
      body.top_k = null;
    } else {
      v = Math.max(0, Math.min(500, v));
      topK.value = v;
      body.top_k = v;
    }
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/vector-config", body);
      if (r && r.error) return showToast(r.error);
      // 同步到内存态，保证后续请求/新建会话使用最新值
      if (window.vmSaveTopK) window.vmSaveTopK(body.top_k);
      showToast("召回数量已保存");
    } catch (_) {
      showToast("保存失败");
    }
  });
}

// 顶部批量操作：一键配置（打开 2/3/4 开关 + 恢复默认值）/ 关闭智能总结（关闭 2/3/4，0/1 继续生效）
function bindMemoryMasterButtons() {
  const oneClick = memEl("memoryOneClickBtn");
  if (oneClick) {
    oneClick.onclick = async () => {
      if (!currentSessionId) return showToast("请先选择对话");
      oneClick.disabled = true;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", {
          facts_enabled: true,
          summary_enabled: true,
          vector_enabled: true,
          reset_values: true,
        });
        if (r && r.error) return showToast(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
        // 程序化改开关不触发 change 事件，手动同步内部状态与开关动画
        if (window.vmSetEnabled) window.vmSetEnabled(true);
        if (window.vmSaveN) window.vmSaveN((r.memory || {}).recent_n);
        const en = memEl("vmEnabled");
        if (en) en.checked = true;
        refreshMemoryCardStates();
        showToast("一键配置已应用");
      } catch (_) {
        showToast("配置失败");
      } finally {
        oneClick.disabled = false;
      }
    };
  }

  const disableBtn = memEl("memoryDisableSmartBtn");
  if (disableBtn) {
    disableBtn.onclick = async () => {
      if (!currentSessionId) return showToast("请先选择对话");
      disableBtn.disabled = true;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", {
          facts_enabled: false,
          summary_enabled: false,
          vector_enabled: false,
        });
        if (r && r.error) return showToast(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
        if (window.vmSetEnabled) window.vmSetEnabled(false);
        const en = memEl("vmEnabled");
        if (en) en.checked = false;
        refreshMemoryCardStates();
        showToast("已关闭智能总结");
      } catch (_) {
        showToast("操作失败");
      } finally {
        disableBtn.disabled = false;
      }
    };
  }
}

// 卡片 ②③ 右上角开关：点击即时带动画并持久化到后端（④ 由 main.js 处理）
function bindMemoryCardSwitches() {
  // ② 自动总结开关（总开关下一级）：关闭时后台不自动抽取，手动仍可用
  const factsAuto = memEl("memoryFactsAuto");
  if (factsAuto) {
    factsAuto.addEventListener("change", async () => {
      if (!currentSessionId) { refreshMemoryCardStates(); return showToast("请先选择对话"); }
      const on = factsAuto.checked;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", { facts_auto: on });
        if (r && r.error) throw new Error(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
        showToast(on ? "已开启自动总结" : "已关闭自动总结");
      } catch (e) {
        factsAuto.checked = !on; // 失败回滚
        showToast(e && e.message ? e.message : "设置失败");
      }
    });
  }

  // ② 动态关键事实
  const factsSwitch = memEl("memoryFactsEnabled");
  if (factsSwitch) {
    factsSwitch.addEventListener("change", async () => {
      if (!currentSessionId) { refreshMemoryCardStates(); return showToast("请先选择对话"); }
      const on = factsSwitch.checked; // CSS 过渡即时生效
      const group = memEl("memoryFactsGroup");
      if (group) group.classList.toggle("memory-off", !on);
      MEMORY_STATE.facts_enabled = on;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", { facts_enabled: on });
        if (r && r.error) throw new Error(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
      } catch (e) {
        factsSwitch.checked = !on;
        if (group) group.classList.toggle("memory-off", on);
        showToast(e && e.message ? e.message : "设置失败");
      }
    });
  }

  // ③ 剧情摘要
  const summarySwitch = memEl("memorySummaryEnabled");
  if (summarySwitch) {
    summarySwitch.addEventListener("change", async () => {
      if (!currentSessionId) { refreshMemoryCardStates(); return showToast("请先选择对话"); }
      const on = summarySwitch.checked;
      const group = memEl("memorySummaryGroup");
      if (group) group.classList.toggle("memory-off", !on);
      MEMORY_STATE = MEMORY_STATE || {};
      MEMORY_STATE.summary = Object.assign({}, MEMORY_STATE.summary || {}, { enabled: on });
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", { summary_enabled: on });
        if (r && r.error) throw new Error(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
      } catch (e) {
        summarySwitch.checked = !on;
        if (group) group.classList.toggle("memory-off", on);
        showToast(e && e.message ? e.message : "设置失败");
      }
    });
  }
}

/* ---------------- 初始化 ---------------- */

function initMemoryPanel() {
  bindMemoryCard();
  bindMemoryCardLib();
  bindFactAdd();
  bindMemoryFacts();
  bindMemoryContext();
  bindMemorySummary();
  bindMemoryVectorTopK();
  bindMemoryMasterButtons();
  bindMemoryCardSwitches();
  // 向量开关变化时同步 top_k 禁用态（main.js 的 syncVmUi 也会调用）
  const en = memEl("vmEnabled");
  if (en) en.addEventListener("change", syncMemoryVectorField);
  resetMemoryPanel();
}

// 暴露给 main.js（打开设置）、sessions.js（切换会话）与 chat.js（applyVmToUI）调用
window.loadMemoryPanel = loadMemoryPanel;
window.resetMemoryPanel = resetMemoryPanel;
window.syncMemoryVectorField = syncMemoryVectorField;
window.refreshMemoryCardStates = refreshMemoryCardStates;

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initMemoryPanel);
} else {
  initMemoryPanel();
}

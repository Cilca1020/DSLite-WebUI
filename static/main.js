/* 初始化与启动入口 */

// 移动端布局判断：以设备横竖屏为准（竖屏 = 移动端布局）。
// 桌面显示器恒为横屏，不会误判；iPad 竖屏也会按移动端处理。
const portraitMQ = window.matchMedia("(orientation: portrait)");
const isMobileLayout = () => portraitMQ.matches;

async function init() {
  if (!(await initAuth())) return;
  // 竖屏（手机 / 竖持平板）：默认收起侧边栏，避免首次进入即遮挡对话区
  if (isMobileLayout()) {
    document.querySelector(".app").setAttribute("data-sidebar", "collapsed");
  }
  // 模型列表
  const models = await apiGet("/api/models");
  const sel = $("#modelSelect");
  models.forEach((m) => {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.label;
    sel.appendChild(o);
  });
  // 默认选第一个模型；打开会话时会被会话自身的 model 覆盖
  if (models.length) sel.value = models[0].id;
  sel.onchange = () => {
    // 已选中会话：切换模型立即保存到该会话；
    // 未选中对话：仅更新 UI，不创建会话（待改参数/发消息时一并落库）
    if (currentSessionId) saveSessionConfig();
  };

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

  // 无会话时先用默认参数初始化 UI；打开会话后会按会话配置覆盖
  writeParamsToUI(PARAM_DEFAULTS);

  // 参数面板改动即存（失焦时回退到合法范围，并保存到当前会话）
  document.querySelectorAll(".param-field input[data-key], #system_prompt").forEach((el) => {
    el.addEventListener("change", () => {
      const p = readParamsFromUI();
      writeParamsToUI(p); // 把回退后的值写回输入框
      saveSessionConfig();
    });
  });

  // API Key
  $("#apiKeyInput").value = loadApiKey();
  $("#apiKeyInput").addEventListener("change", (e) => saveApiKey(e.target.value));

  // 消息字号设置（localStorage 持久化，通过 CSS 变量 --msg-font-size 全局生效）
  const MSG_FONT_KEY = "dsw_msg_font_size";
  const applyMsgFont = (v) => {
    const px = Math.max(12, Math.min(20, parseInt(v, 10) || 14));
    document.documentElement.style.setProperty("--msg-font-size", px + "px");
    return px;
  };
  const msgFontInput = $("#msgFontSize");
  msgFontInput.value = localStorage.getItem(MSG_FONT_KEY) || 14;
  applyMsgFont(msgFontInput.value);
  msgFontInput.addEventListener("change", () => {
    const px = applyMsgFont(msgFontInput.value);
    msgFontInput.value = px; // 写回钳制后的合法值
    localStorage.setItem(MSG_FONT_KEY, px);
  });

  // 思考过程是否自动收起（localStorage 持久化；默认开启，保持原行为）
  const AUTO_COLLAPSE_KEY = "dsw_auto_collapse_reasoning";
  const getAutoCollapse = () => localStorage.getItem(AUTO_COLLAPSE_KEY) !== "0";
  window.getAutoCollapseReasoning = getAutoCollapse; // 供 chat.js 流式输出时判断
  const autoCollapseInput = $("#autoCollapseReasoning");
  autoCollapseInput.checked = getAutoCollapse();
  autoCollapseInput.addEventListener("change", () => {
    localStorage.setItem(AUTO_COLLAPSE_KEY, autoCollapseInput.checked ? "1" : "0");
  });

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
    // 未选中对话时参数修改已由 change 事件创建并保存会话，这里仅在已选中时兜底保存
    if (currentSessionId) saveSessionConfig();
  };
  $("#openSettingsBtn").onclick = openSettings;
  $("#closeSettingsBtn").onclick = closeSettings;
  $("#settingsOverlay").onclick = closeSettings;

  // 主题色预设（浅/深各一套；选用常见、不猎奇的色系）
  const THEME_COLORS = {
    blue:   { label: "蓝",   light: { accent: "#4f7cff", hover: "#3f6cf0", disabled: "#a9c0ff", soft: "#eaf0ff" }, dark: { accent: "#4f7cff", hover: "#3f6cf0", disabled: "#3a4a7a", soft: "#232b40" } },
    teal:   { label: "青",   light: { accent: "#14b8a6", hover: "#0d9488", disabled: "#8ce0d6", soft: "#e4f7f4" }, dark: { accent: "#14b8a6", hover: "#0d9488", disabled: "#155e56", soft: "#123a35" } },
    green:  { label: "绿",   light: { accent: "#22c55e", hover: "#16a34a", disabled: "#a6e8bd", soft: "#e8f8ef" }, dark: { accent: "#22c55e", hover: "#16a34a", disabled: "#1b5e38", soft: "#123b22" } },
    orange: { label: "橙",   light: { accent: "#f59e0b", hover: "#d97706", disabled: "#fbd38d", soft: "#fef3e0" }, dark: { accent: "#f59e0b", hover: "#d97706", disabled: "#8a5b18", soft: "#4a3a15" } },
    purple: { label: "紫",   light: { accent: "#8b5cf6", hover: "#7c3aed", disabled: "#c4b5fd", soft: "#f1ebfe" }, dark: { accent: "#8b5cf6", hover: "#7c3aed", disabled: "#54408f", soft: "#33295c" } },
    rose:   { label: "玫红", light: { accent: "#ec4899", hover: "#db2777", disabled: "#f9a8d4", soft: "#fdeaf5" }, dark: { accent: "#ec4899", hover: "#db2777", disabled: "#8f3d66", soft: "#4a2440" } },
  };
  const ACCENT_KEY = "dsw_accent_color";
  let currentAccent = localStorage.getItem(ACCENT_KEY) || "blue";
  if (!THEME_COLORS[currentAccent]) currentAccent = "blue";
  // 把主题色写入 CSS 变量（发送按钮、用户气泡、链接、hover 等自动跟随）
  const applyAccent = () => {
    const theme = document.documentElement.getAttribute("data-theme") || "light";
    const vars = THEME_COLORS[currentAccent][theme] || THEME_COLORS[currentAccent].light;
    const s = document.documentElement.style;
    s.setProperty("--accent", vars.accent);
    s.setProperty("--accent-hover", vars.hover);
    s.setProperty("--accent-disabled", vars.disabled);
    s.setProperty("--accent-soft", vars.soft);
    s.setProperty("--msg-user-bg", vars.accent);
    // 同步色块选中态
    document.querySelectorAll(".swatch").forEach((el) =>
      el.classList.toggle("active", el.dataset.key === currentAccent)
    );
  };

  // 深色/浅色切换
  const themeBtn = $("#themeBtn");
  const applyTheme = (t) => {
    const html = document.documentElement;
    html.classList.add("theme-transition"); // 启用渐变动画，切换完成后移除
    html.setAttribute("data-theme", t);
    localStorage.setItem("dsw_theme", t);
    // 太阳/月亮切换：浅色下显示月亮（可切入夜间），深色下显示太阳（可切回白天）
    themeBtn.title = t === "dark" ? "切换到浅色" : "切换到深色";
    themeBtn.innerHTML = t === "dark" ? svgIcon("sun") : svgIcon("moon");
    // 同步切换代码高亮配色
    const light = $("#hljsLight");
    const dark = $("#hljsDark");
    if (light) light.disabled = t === "dark";
    if (dark) dark.disabled = t !== "dark";
    // 主题色按当前深浅主题取对应配色
    applyAccent();
    // 渐变结束后移除过渡类，恢复各元素的常规交互过渡
    setTimeout(() => html.classList.remove("theme-transition"), 320);
  };
  // 生成主题色色块并初始化选中态
  const swatchesEl = $("#accentSwatches");
  Object.keys(THEME_COLORS).forEach((key) => {
    const c = THEME_COLORS[key];
    const sw = document.createElement("button");
    sw.type = "button";
    sw.className = "swatch";
    sw.dataset.key = key;
    sw.title = c.label;
    sw.style.background = c.light.accent;
    sw.onclick = () => {
      currentAccent = key;
      localStorage.setItem(ACCENT_KEY, key);
      applyAccent();
    };
    swatchesEl.appendChild(sw);
  });
  applyAccent();
  applyTheme(localStorage.getItem("dsw_theme") || "light");
  themeBtn.onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme");
    applyTheme(cur === "dark" ? "light" : "dark");
  };

  // 置底按钮：滚动离开最底部时浮现，点击回到底部；最底部或无法滚动时隐藏
  const chatBoxEl = $("#chatBox");
  const scrollDownBtn = $("#scrollDownBtn");
  const updateScrollDown = () => {
    const box = chatBoxEl;
    const canScroll = box.scrollHeight > box.clientHeight + 1;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    scrollDownBtn.classList.toggle("hidden", !canScroll || atBottom);
  };
  chatBoxEl.addEventListener("scroll", () => {
    updateScrollDown();
    // 上滑接近顶部时按需加载更早的消息（长对话分段渲染）
    if (chatBoxEl.scrollTop <= 40) loadOlderMessages();
  }, { passive: true });
  scrollDownBtn.addEventListener("click", () => {
    chatBoxEl.scrollTop = chatBoxEl.scrollHeight; // 瞬时回底，避免长对话滚动动画过慢
  });
  updateScrollDown();

  // 按钮
  $("#sendBtn").onclick = sendMessage;
  $("#userInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  // 移动端键盘无 Shift 键，「换行」按钮在光标处插入换行，替代 Shift+Enter
  $("#newlineBtn").onclick = () => {
    const input = $("#userInput");
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? start;
    input.value = input.value.slice(0, start) + "\n" + input.value.slice(end);
    const next = start + 1;
    input.setSelectionRange(next, next);
    input.focus();
  };
  // 移动端 placeholder 提示改用「换行」按钮
  if (isMobileLayout()) {
    $("#userInput").placeholder = "输入消息，Enter 发送，「换行」按钮换行";
  }
  // 粘贴降级为纯文本：丢弃 HTML 富文本格式，仅保留纯文本
  $("#userInput").addEventListener("paste", (e) => {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData("text/plain");
    const input = e.target;
    const start = input.selectionStart;
    const end = input.selectionEnd;
    input.value = input.value.slice(0, start) + text + input.value.slice(end);
    input.selectionStart = input.selectionEnd = start + text.length;
  });
  $("#newSessionBtn").onclick = newSession;
  // 侧边栏收起/展开
  $("#toggleSidebarBtn").onclick = () => {
    const app = document.querySelector(".app");
    const collapsed = app.getAttribute("data-sidebar") === "collapsed";
    app.setAttribute("data-sidebar", collapsed ? "open" : "collapsed");
  };
  // 移动端：点击侧栏中的会话/预设/新建按钮后自动收起抽屉
  document.querySelector(".sidebar").addEventListener("click", (e) => {
    if (!isMobileLayout()) return;
    if (e.target.closest("li, .sidebar-head button")) {
      document.querySelector(".app").setAttribute("data-sidebar", "collapsed");
    }
  });
  // 移动端：侧栏打开时，点击侧栏之外的区域（对话区）也可关闭抽屉
  document.querySelector(".main").addEventListener("click", (e) => {
    if (!isMobileLayout()) return;
    if (e.target.closest("#toggleSidebarBtn")) return; // 开关按钮交给它自己处理
    const app = document.querySelector(".app");
    if (app.getAttribute("data-sidebar") === "open") {
      app.setAttribute("data-sidebar", "collapsed");
    }
  });
  // 旋转屏幕时同步侧栏状态：竖屏收起（抽屉式）、横屏展开，保证与 CSS 布局一致
  window.addEventListener("orientationchange", () => {
    document.querySelector(".app").setAttribute(
      "data-sidebar",
      isMobileLayout() ? "collapsed" : "open"
    );
  });

  // 刷新侧栏
  const sessions = await apiGet("/api/sessions");
  await refreshSessions();
  // 标题总结放到后台执行，避免模型调用期间阻塞会话面板。
  Promise.all(
    sessions
      .filter((item) => item.title.startsWith("会话 ") && item.message_count >= 2)
      .map((item) => autoTitleSession(item.id).catch(() => false))
  ).then((results) => {
    if (results.some(Boolean)) refreshSessions();
  });
  // 启动不自动创建会话：直接进入「未选中对话」空白态显示「你好」提示，
  // 等用户改参数/prompt 或发消息再创建
  resetChatUI();
  // 启动时清理遗留的空会话（此时未选中对话，等价于清理全部空会话）
  await cleanupEmptySessions(currentSessionId);
}

init();

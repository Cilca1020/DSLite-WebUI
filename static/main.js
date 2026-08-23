/* 初始化与启动入口 */

// 移动端布局判断：以设备横竖屏为准（竖屏 = 移动端布局）。
// 桌面显示器恒为横屏，不会误判；iPad 竖屏也会按移动端处理。
const portraitMQ = window.matchMedia("(orientation: portrait)");
const isMobileLayout = () => portraitMQ.matches;

async function init() {
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
  sel.onchange = () => saveSessionConfig();

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
    saveSessionConfig();
  };
  $("#openSettingsBtn").onclick = openSettings;
  $("#closeSettingsBtn").onclick = closeSettings;
  $("#settingsOverlay").onclick = closeSettings;

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
    // 渐变结束后移除过渡类，恢复各元素的常规交互过渡
    setTimeout(() => html.classList.remove("theme-transition"), 320);
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
  refreshSessions();
  // 默认新建一个会话
  if (!currentSessionId) await newSession();
  // 启动时清理其他空会话（保留当前会话）
  await cleanupEmptySessions(currentSessionId);
}

init();

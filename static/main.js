/* 初始化与启动入口 */

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
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem("dsw_theme", t);
    themeBtn.title = t === "dark" ? "切换到浅色" : "切换到深色";
    // 同步切换代码高亮配色
    const light = $("#hljsLight");
    const dark = $("#hljsDark");
    if (light) light.disabled = t === "dark";
    if (dark) dark.disabled = t !== "dark";
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

  // 刷新侧栏
  refreshSessions();
  // 默认新建一个会话
  if (!currentSessionId) await newSession();
  // 启动时清理其他空会话（保留当前会话）
  await cleanupEmptySessions(currentSessionId);
}

init();

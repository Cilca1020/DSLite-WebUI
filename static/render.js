/* 渲染逻辑：markdown、消息气泡、文件卡片、助手消息 */

// 将文本以 Markdown 渲染进元素（思考与正文通用）
function renderMarkdown(el, text) {
  const src = (text || "").trim();
  // marked 输出末尾带一个格式化 \n，在 white-space:pre-wrap 容器（如思考块）中会被渲染成多余空行，故 trim
  el.innerHTML = (window.marked ? marked.parse(src) : src).trim();
  if (!src) el.textContent = "";
  attachCodeCopy(el); // 渲染完成后为每个代码块附加"单独复制"按钮
}

// 为容器内所有代码块（pre code）附加独立的复制按钮。
// 已有按钮的代码块跳过（幂等）：流式增量渲染时会对新增块重复调用。
function attachCodeCopy(container) {
  if (!container || !container.querySelectorAll) return;
  container.querySelectorAll("pre").forEach((pre) => {
    if (pre.querySelector(":scope > .code-copy-btn")) return;
    const code = pre.querySelector("code");
    if (!code) return;
    // 语法高亮：仅对带语言标签的代码块高亮（无语言时不自动检测，避免误判）
    if (window.hljs) {
      const langMatch = (code.className || "").match(/language-([\w+#.-]+)/);
      if (langMatch) {
        try {
          hljs.highlightElement(code);
        } catch (_) {
          /* 未知语言等异常忽略，代码保持原样 */
        }
      }
    }
    // 语言标签：从 code 类名提取（如 language-python），无则显示"plain text"
    const langEl = document.createElement("span");
    langEl.className = "code-lang";
    const langMatch = (code.className || "").match(/language-([\w+#.-]+)/);
    langEl.textContent = langMatch ? langMatch[1] : "plain text";
    pre.appendChild(langEl);
    const btn = document.createElement("button");
    btn.className = "code-copy-btn";
    btn.type = "button";
    btn.title = "复制代码";
    btn.innerHTML = svgIcon("copy");
    btn.onclick = (e) => {
      e.stopPropagation();
      const txt = code.innerText;
      const done = () => {
        btn.innerHTML = svgIcon("check");
        setTimeout(() => (btn.innerHTML = svgIcon("copy")), 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done).catch(() => fallbackCopy(txt, done));
      } else {
        fallbackCopy(txt, done);
      }
    };
    pre.appendChild(btn);
  });
}

// 渲染一条消息气泡。返回气泡元素（div），供上层插入 reasoning 折叠块。
// role: "user" | "assistant"；markdown 控制是否渲染 markdown（用户消息强制纯文本）；
// msgIndex 非 null 且当前会话存在时，渲染气泡外的操作条；
// files：用户消息携带的文件（渲染为气泡上方的文件卡片）。
function addMsgEl(role, text, markdown = true, msgIndex = null, files = null) {
  const box = $("#chatBox");
  // 每行容器：包裹气泡 + 气泡外的操作条
  const row = document.createElement("div");
  row.className = "msg-row " + role;
  const div = document.createElement("div");
  div.className = "msg " + role;
  if (markdown && window.marked) {
    renderMarkdown(div, text);
  } else {
    div.textContent = text || "";
  }
  // 保存原始 markdown 源，供"复制为 Markdown"使用
  div._rawText = text || "";
  // 文件卡片（仅用户消息携带文件时存在）：渲染在气泡上方
  if (role === "user" && files && files.length) {
    row.appendChild(buildFileCardsEl(files));
  }
  // 消息工具条：常驻显示在气泡外右下方（不依赖 hover，便于移动端）
  let bar = null;
  if (msgIndex !== null && currentSessionId) {
    bar = document.createElement("div");
    bar.className = "msg-actions";
    // 复制纯文本按钮：复制气泡纯文本（排除思考过程折叠块与文件卡片）
    const copyBtn = document.createElement("button");
    copyBtn.className = "msg-action";
    copyBtn.title = "复制为纯文本";
    copyBtn.innerHTML = svgIcon("copy");
    copyBtn.onclick = (e) => {
      e.stopPropagation();
      const clone = div.cloneNode(true);
      const r = clone.querySelector(".reasoning");
      if (r) r.remove();
      // 复制只包含用户输入文本，不含文件内容
      const txt = clone.innerText.trim();
      const done = () => {
        copyBtn.innerHTML = svgIcon("check");
        setTimeout(() => (copyBtn.innerHTML = svgIcon("copy")), 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done).catch(() => fallbackCopy(txt, done));
      } else {
        fallbackCopy(txt, done);
      }
    };
    bar.appendChild(copyBtn);
    // 复制为 Markdown 按钮：使用消息原始 markdown 源（排除思考过程折叠块）
    // 仅助手消息提供（用户提问通常无需 Markdown 源码）
    if (role === "assistant") {
      const copyMdBtn = document.createElement("button");
      copyMdBtn.className = "msg-action md-btn";
      copyMdBtn.title = "复制为 Markdown";
      copyMdBtn.textContent = "MD";
      copyMdBtn.onclick = (e) => {
        e.stopPropagation();
        const md = (div._rawText || div.innerText || "").trim();
        const done = () => {
          copyMdBtn.innerHTML = svgIcon("check");
          setTimeout(() => (copyMdBtn.textContent = "MD"), 1200);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(md).then(done).catch(() => fallbackCopy(md, done));
        } else {
          fallbackCopy(md, done);
        }
      };
      bar.appendChild(copyMdBtn);
    }
    // 编辑按钮：仅最新一轮的 user 消息显示
    if (role === "user" && msgIndex === lastUserMsgIndex) {
      const editBtn = document.createElement("button");
      editBtn.className = "msg-action";
      editBtn.title = "编辑这条提问";
      editBtn.innerHTML = svgIcon("edit");
      editBtn.onclick = (e) => {
        e.stopPropagation();
        editMessage(msgIndex);
      };
      bar.appendChild(editBtn);
    }
    const delBtn = document.createElement("button");
    delBtn.className = "msg-action";
    delBtn.title = "删除这条消息（连同同轮另一条）";
    delBtn.innerHTML = svgIcon("trash");
    delBtn.onclick = (e) => {
      e.stopPropagation();
      deleteMessage(msgIndex);
    };
    // 重试仅对助手消息有意义：重新请求其对应上文
    if (role === "assistant") {
      const retryBtn = document.createElement("button");
      retryBtn.className = "msg-action";
      retryBtn.title = "重试这条回答";
      retryBtn.innerHTML = svgIcon("retry");
      retryBtn.onclick = (e) => {
        e.stopPropagation();
        retryFrom(msgIndex);
      };
      bar.appendChild(retryBtn);
    }
    bar.appendChild(delBtn);
  }
  row.appendChild(div);
  if (msgIndex !== null && currentSessionId) row.appendChild(bar);
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
  return div; // 返回气泡，供上层插入 reasoning 折叠块
}

// 构建"文件卡片"元素（代表文件的气泡），供用户消息渲染使用
function buildFileCardsEl(files) {
  const wrap = document.createElement("div");
  wrap.className = "file-cards";
  files.forEach((f) => {
    const card = document.createElement("div");
    card.className = "file-card";
    const icon = document.createElement("span");
    icon.className = "file-card-icon";
    icon.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
    const name = document.createElement("span");
    name.className = "file-card-name";
    name.textContent = f.name;
    name.title = f.name;
    card.appendChild(icon);
    card.appendChild(name);
    wrap.appendChild(card);
  });
  return wrap;
}

// 渲染一条助手消息（支持思考折叠块），返回元素
function renderAssistant(text, reasoning, msgIndex = null) {
  const el = addMsgEl("assistant", text, true, msgIndex); // 正文已按 markdown 渲染
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

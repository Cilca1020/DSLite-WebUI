/* 发送/重试核心与消息操作（编辑/删除/重试） */

// 流式请求助手回复并渲染到 assistantEl。userText 仅用于首次写入历史；
// saveHistory=true 时会把 user+assistant 两条消息写入后端会话历史。
// 返回 { full, reasoning } 供调用方更新内存 conversation。
// writeUser=true 时额外把 user 消息写入历史（仅首次发送需要；重试时 user 已在历史中）。
// files：该轮用户消息携带的文本文件（{name, content}），随 user 消息一起存入历史。
async function streamAssistant(userText, assistantEl, saveHistory, writeUser = true, files = null) {
  const apiKey = loadApiKey();
  if (!apiKey) {
    assistantEl.textContent = "[错误] 请先点击右上角设置按钮，在设置面板中输入 API Key";
    return null;
  }
  const model = $("#modelSelect").value;

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

  // ---- 企业级增量渲染：已定型块永久固定，仅末尾"未闭合草稿"以纯文本显示 ----
  // 设计：answerDiv 内由「已渲染的块元素们」+「一个草稿 span 节点」组成。
  // 每当累积文本越过一个"块级安全断点"，就把 [renderedLen, 断点) 这一段
  // 增量渲染并 append（旧块不重绘），其余末尾不稳定的内容放进草稿节点。
  let renderedLen = 0; // 已经被定型渲染到 answerDiv 的稳定前缀长度
  const draftNode = document.createElement("span"); // 末尾未成型草稿（纯文本，非等宽/非框）
  draftNode.className = "md-draft";
  draftNode.style.whiteSpace = "pre-wrap";
  draftNode.style.wordBreak = "break-word";
  let draftAttached = false;

  // 寻找 src[0, end) 中最后一个"块级安全断点"下标（不含则回退到 0）。
  // 安全断点定义：其后内容即便孤立即使 markdown 重解析也不会影响前面已定型块。
  function lastSafeBreak(src, end) {
    const seg = src.slice(0, end);
    // 代码围栏：未闭合的 ``` 之后不能断（避免把未闭合块提前定死）
    const fenceRe = /```/g;
    let fenceCount = 0, m;
    while ((m = fenceRe.exec(seg)) !== null) fenceCount++;
    if (fenceCount % 2 !== 0) {
      // 存在未闭合代码块：断点必须在开场 ``` 之前
      const open = seg.lastIndexOf("```");
      return Math.max(0, open);
    }
    // 优先在「空行（段落边界）」处断开
    let brk = seg.lastIndexOf("\n\n");
    if (brk !== -1) return brk + 2; // 包含空行，下一段为草稿
    // 其次在「行尾」断开（单行块如标题/列表项，整行写完整再定型）
    const nl = seg.lastIndexOf("\n");
    if (nl !== -1) return nl + 1;
    return 0; // 仅一行且未结束：整段作为草稿
  }

  // 把 [renderedLen, upto) 这一段增量渲染并追加为块（不触碰已渲染部分）
  function flushRendered(upto) {
    if (upto <= renderedLen) return;
    const seg = full.slice(renderedLen, upto);
    const html = (window.marked ? marked.parse(seg) : seg);
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    attachCodeCopy(tmp); // 新定型的代码块立即附加"单独复制"按钮（幂等）
    // 草稿节点若已挂到 answerDiv 则插到其前，否则直接追加（避免 insertBefore 目标不在树中报错）
    while (tmp.firstChild) {
      if (draftAttached) answerDiv.insertBefore(tmp.firstChild, draftNode);
      else answerDiv.appendChild(tmp.firstChild);
    }
    renderedLen = upto;
  }

  function renderAnswer() {
    if (!full) return;
    const brk = lastSafeBreak(full, full.length);
    flushRendered(brk); // 把稳定前缀内的新完整块增量渲染
    // 剩余草稿（可能为空）放进纯文本节点
    const draft = full.slice(renderedLen);
    if (draft) {
      if (!draftAttached) {
        answerDiv.appendChild(draftNode);
        draftAttached = true;
      }
      draftNode.textContent = draft;
    } else if (draftAttached) {
      draftNode.remove();
      draftAttached = false;
    }
  }

  const applyChunk = (text) => {
    if (!text) return;
    if (mode === "reasoning") {
      reasoning += text;
      ensureReasoning();
      reasoningBody.textContent = reasoning; // 流式实时显示纯文本
    } else {
      if (reasoning && reasoningWrap && reasoningWrap.open) reasoningWrap.open = false;
      full += text;
      renderAnswer(); // 增量渲染：实时成型，已定型块不重绘
    }
    $("#chatBox").scrollTop = $("#chatBox").scrollHeight;
  };
  await streamChat(
    {
      api_key: apiKey,
      model,
      // 把用户消息携带的文本文件内容拼接到对应 user 的 content 中，
      // 使模型能看到文件内容（files 本身不作为独立消息）
      messages: conversation.map((m) => {
        if (m.role === "user" && m.files && m.files.length) {
          const appended = m.files
            .map((f) => {
              if (f.binary) {
                // 二进制办公文档：以 base64 附件形式标注（文本模型仅能识别为附件）
                return `\n\n--- 附件（二进制，${f.name}，${f.content.length} 字节 base64）：请知悉该文件为二进制文档，无法在此直接解析内容 ---`;
              }
              return `\n\n--- 文件内容：${f.name} ---\n${f.content}`;
            })
            .join("");
          return { role: "user", content: (m.content || "") + appended };
        }
        return { role: m.role, content: m.content };
      }),
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
  // 收尾：把剩余草稿也定型渲染，移除纯文本草稿节点（不整段重渲染，已定型块不动）
  flushRendered(full.length);
  if (draftAttached) {
    draftNode.remove();
    draftAttached = false;
  }
  // 思考过程仍为纯文本（模型以纯文本输出），流结束定型一次
  if (reasoning && reasoningBody) renderMarkdown(reasoningBody, reasoning);
  // 流式结束后保存原始 markdown 源，供"复制为 Markdown"使用
  assistantEl._rawText = full;
  // 存历史：content 存纯回答（用于渲染+上传），reasoning 单独存（仅渲染）
  if (saveHistory && currentSessionId) {
    if (writeUser)
      await apiPost("/api/sessions/" + currentSessionId + "/msg", {
        role: "user",
        content: userText,
        files: files && files.length ? files : undefined,
      });
    await apiPost("/api/sessions/" + currentSessionId + "/msg", {
      role: "assistant",
      content: full,
      reasoning: reasoning || undefined,
    });
    refreshSessions();
  }
  return { full, reasoning };
}

// 编辑最新一轮的提问：取出原文本填入输入框，并删除该轮（user+assistant）。
// 用户修改后发送，新的一轮即取代原位置。仅最新一轮可触发。
async function editMessage(index) {
  if (!currentSessionId || index !== lastUserMsgIndex) return;
  const s = await apiGet("/api/sessions/" + currentSessionId);
  const chat = (s.messages || []).filter((m) => m.role !== "system");
  if (index < 0 || index >= chat.length || chat[index].role !== "user") return;
  const original = chat[index].content;
  // 成对删除该轮（user + 紧跟的 assistant），不弹确认
  let second = -1;
  if (chat[index + 1] && chat[index + 1].role === "assistant") second = index + 1;
  const hi = Math.max(index, second);
  const lo = Math.min(index, second);
  await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + hi);
  if (second !== -1) await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + lo);
  lastUserMsgIndex = null; // 该轮即将被删除，临时清空
  await openSession(currentSessionId);
  // 预填输入框，并把原文件恢复回待发送队列（用户可保留或重新选择）
  const input = $("#userInput");
  input.value = original;
  if (window.FileReaderModule) {
    FileReaderModule.clearPendingFiles();
    const originalFiles = chat[index].files || [];
    if (originalFiles.length) FileReaderModule.addFilesFromData(originalFiles);
  }
  input.focus();
}

// 删除第 index 条消息（不含 system），并重新渲染会话。
// 按「轮次」成对删除：删除 user 时连同其后紧跟的 assistant 一起删，
// 删除 assistant 时连同其前紧邻的 user 一起删；不成对则单独删。
async function deleteMessage(index) {
  if (!currentSessionId) return;
  if (!confirm("确定删除这条消息（及其同轮消息）？")) return;
  // 读取当前会话，判断是否存在成对消息
  const s = await apiGet("/api/sessions/" + currentSessionId);
  const chat = (s.messages || []).filter((m) => m.role !== "system");
  if (index < 0 || index >= chat.length) return;
  const role = chat[index].role;
  let second = -1; // 成对消息的下标（-1 表示无配对）
  if (role === "user" && chat[index + 1] && chat[index + 1].role === "assistant") {
    second = index + 1;
  } else if (role === "assistant" && chat[index - 1] && chat[index - 1].role === "user") {
    second = index - 1;
  }
  // 先删较大的下标，避免删除前一条导致后一条下标前移而误删
  if (second !== -1) {
    const hi = Math.max(index, second);
    const lo = Math.min(index, second);
    await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + hi);
    await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + lo);
  } else {
    await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + index);
  }
  // 用后端数据重新渲染，保证内存与磁盘一致
  await openSession(currentSessionId);
}

// 重试：删除第 msgIndex（助手消息）及其之后的所有消息，
// 以该助手对应的上文重新请求一次
async function retryFrom(msgIndex) {
  if (!currentSessionId) return;
  const sendBtn = $("#sendBtn");
  if (sendBtn.disabled) return; // 避免与正在进行的请求冲突
  // 删除该助手消息及其后所有消息：反复删除同一个下标直到该下标越界
  while (true) {
    const s = await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + msgIndex);
    if (s && s.error) break; // 下标已越界或无会话
    // 判断是否还有消息，防止无限循环
    const list = await apiGet("/api/sessions/" + currentSessionId);
    const chatCount = (list.messages || []).filter((m) => m.role !== "system").length;
    if (chatCount <= msgIndex) break;
  }
  // 依据最新后端数据重建内存态与 UI（保留文件信息）
  const s = await apiGet("/api/sessions/" + currentSessionId);
  conversation = s.messages
    .filter((m) => m.role !== "system")
    .map((m) => ({ role: m.role, content: m.content, files: m.files || null }));
  // 该助手消息对应的用户提问在 conversation 中位于 msgIndex-1
  const userMsg = conversation[msgIndex - 1] || { content: "", files: null };
  const userText = userMsg.content || "";
  const userFiles = userMsg.files || null;
  // 重新渲染会话（此时尾部已被删掉）
  await openSession(currentSessionId);
  // 在会话末尾追加一个新的助手占位并流式请求（user 提问仍在 conversation 中，作为上下文）
  const assistantEl = addMsgEl("assistant", "", true, msgIndex);
  try {
    sendBtn.disabled = true;
    const res = await streamAssistant(userText, assistantEl, true, false, userFiles);
    if (res) conversation.push({ role: "assistant", content: res.full });
  } catch (e) {
    assistantEl.textContent = "[错误] " + e.message;
  } finally {
    sendBtn.disabled = false;
  }
}

// 发送消息：渲染用户消息与助手占位，触发流式请求
async function sendMessage() {
  const input = $("#userInput");
  const text = input.value.trim();
  // 文本与文件至少有一种；都为空则忽略
  const files = window.FileReaderModule ? FileReaderModule.getPendingFiles() : [];
  if (!text && files.length === 0) return;

  const apiKey = loadApiKey();
  if (!apiKey) {
    alert("请先点击右上角设置按钮，在设置面板中输入 API Key");
    return;
  }

  // 渲染用户消息（传入下标，使操作条可见）；携带文件时在气泡上方渲染文件卡片
  // 用户消息强制纯文本：输入中若含 Markdown 语法则按原样显示，不渲染
  const userIdx = conversation.length;
  lastUserMsgIndex = userIdx; // 最新一轮，显示编辑按钮（渲染前先设置）
  addMsgEl("user", text || "（已发送文件）", false, userIdx, files);
  input.value = "";
  conversation.push({ role: "user", content: text, files: files.length ? files : null });

  // 发送后立即清空输入框上方的待发送文件卡片（不等输出完成）
  if (window.FileReaderModule) FileReaderModule.clearPendingFiles();

  // 渲染助手占位（下标紧随用户消息之后）
  const assistantEl = addMsgEl("assistant", "", true, userIdx + 1);

  const sendBtn = $("#sendBtn");
  sendBtn.disabled = true;
  try {
    const res = await streamAssistant(text, assistantEl, true, true, files);
    if (res) conversation.push({ role: "assistant", content: res.full });
  } catch (e) {
    assistantEl.textContent = "[错误] " + e.message;
  } finally {
    sendBtn.disabled = false;
  }
}

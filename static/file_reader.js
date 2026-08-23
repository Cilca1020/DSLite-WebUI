/* 文件读取模块：本地常用文本 / 办公文档读取、浏览、拖放、待发送队列与 UI。
 * 全局暴露 window.FileReaderModule，供前端主逻辑模块调用。
 * - 文本类文件：直接读为文本（content 为文本）。
 * - 二进制办公文档（pdf/docx/xlsx/pptx/rtf/odt 等）：读为 base64（content 为 data URL），
 *   发送时以附件形式标注，便于模型识别为二进制资料。
 */

(function () {
  "use strict";

  // 待发送文件队列：[{ name, content, size, binary }]
  const pendingFiles = [];

  // 单个文件大小上限（4MB），超出则跳过并提示
  const MAX_FILE_BYTES = 4 * 1024 * 1024;

  // 文本类扩展名白名单（MIME 非 text/* 时按扩展名兜底）
  const TEXT_EXT = new Set([
    "txt", "md", "markdown", "json", "csv", "tsv", "log", "yaml", "yml", "xml",
    "ini", "toml", "conf", "cfg", "env", "sh", "bat", "ps1", "py", "js",
    "mjs", "cjs", "ts", "jsx", "tsx", "html", "htm", "css", "scss", "less",
    "c", "h", "cpp", "hpp", "cc", "java", "go", "rs", "rb", "php", "sql",
    "r", "kt", "swift", "lua", "pl", "tex", "gitignore", "dockerfile", "rtf",
  ]);

  // 二进制办公文档扩展名（以 base64 形式携带）
  const BINARY_DOC_EXT = new Set([
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "odt", "ods", "odp", "rtf",
  ]);

  function extOf(name) {
    const i = name.lastIndexOf(".");
    return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
  }

  function isTextFile(file) {
    if (file.type && file.type.startsWith("text/")) return true;
    if (file.type === "application/json") return true;
    return TEXT_EXT.has(extOf(file.name));
  }

  function isBinaryDoc(file) {
    return BINARY_DOC_EXT.has(extOf(file.name));
  }

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  // 读取单个文件；文本类读为文本，二进制办公文档读为 base64；其余跳过并提示。
  // 返回 { name, content, size, binary }，失败/不支持返回 null。
  function readOne(file) {
    return new Promise((resolve) => {
      if (!isTextFile(file) && !isBinaryDoc(file)) {
        alert('已跳过不支持的文件：「' + file.name + '」（仅支持常用文本与办公文档）');
        resolve(null);
        return;
      }
      if (file.size > MAX_FILE_BYTES) {
        alert('已跳过过大文件：「' + file.name + '」（超过 ' + formatSize(MAX_FILE_BYTES) + ' 上限）');
        resolve(null);
        return;
      }
      const reader = new FileReader();
      reader.onerror = () => {
        alert('读取文件失败：「' + file.name + '」');
        resolve(null);
      };
      if (isTextFile(file)) {
        reader.onload = () => resolve({ name: file.name, content: String(reader.result || ""), size: file.size, binary: false });
        reader.readAsText(file);
      } else {
        // 二进制文档：抽取为纯文本后再发送（失败则回退为 base64 附件）
        extractBinaryText(file)
          .then((text) => resolve({ name: file.name, content: text, size: file.size, binary: false }))
          .catch((err) => {
            console.warn("文档抽取失败，回退为 base64 附件：", file.name, err);
            const r = new FileReader();
            r.onload = () => resolve({ name: file.name, content: String(r.result || ""), size: file.size, binary: true });
            r.onerror = () => { alert("读取文件失败：「" + file.name + "」"); resolve(null); };
            r.readAsDataURL(file);
          });
      }
    });
  }

  // 将二进制办公文档抽取为纯文本。
  // 成功返回文本字符串；失败/不支持抛出异常。
  async function extractBinaryText(file) {
    const ext = extOf(file.name);

    // PDF
    if (ext === "pdf") {
      if (!window.pdfjsLib) throw new Error("pdf.js 未加载");
      window.pdfjsLib.GlobalWorkerOptions.workerSrc =
        "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js";
      const pdf = await window.pdfjsLib.getDocument({ data: await file.arrayBuffer() }).promise;
      let out = "";
      for (let i = 1; i <= pdf.numPages; i++) {
        const page = await pdf.getPage(i);
        const content = await page.getTextContent();
        // 按阅读顺序排序：先按 y 从上到下，再按 x 从左到右
        const items = content.items
          .filter((it) => it.str != null && it.str !== "")
          .map((it) => ({ str: it.str, x: it.transform[4], y: it.transform[5] }))
          .sort((a, b) => b.y - a.y || a.x - b.x);
        // 同行（y 相近）用空格连，换行用 \n
        let line = "";
        let lastY = null;
        for (const it of items) {
          if (lastY !== null && Math.abs(it.y - lastY) > 2) {
            out += line + "\n";
            line = "";
          }
          line += (line ? " " : "") + it.str;
          lastY = it.y;
        }
        if (line) out += line + "\n";
        out += "\n"; // 页间空行
      }
      out = out.trim();
      if (!out) throw new Error("PDF 无可抽取文字（可能是扫描图片版）");
      return out;
    }

    // DOCX（mammoth，全局 UMD）
    if (ext === "docx") {
      const res = await mammoth.extractRawText({ arrayBuffer: await file.arrayBuffer() });
      return res.value || "";
    }

    // XLSX / XLS（SheetJS，全局 UMD）
    if (ext === "xlsx" || ext === "xls") {
      const wb = XLSX.read(await file.arrayBuffer(), { type: "array" });
      let out = "";
      wb.SheetNames.forEach((name) => {
        out += "【工作表：" + name + "】\n";
        out += XLSX.utils.sheet_to_csv(wb.Sheets[name]) + "\n";
      });
      return out.trim();
    }

    // PPTX / ODP / ODT / ODS（OOXML/ODF 本质是 zip，用 JSZip 解压取文本）
    if (["pptx", "odp", "odt", "ods"].includes(ext)) {
      const zip = await JSZip.loadAsync(await file.arrayBuffer());
      let texts = [];
      if (ext === "pptx") {
        // ppt/slides/slideN.xml 中的 <a:t> 文本
        const slides = Object.keys(zip.files).filter((p) => /^ppt\/slides\/slide\d+\.xml$/.test(p));
        for (const s of slides.sort()) {
          const xml = await zip.files[s].async("string");
          texts.push(extractXmlText(xml));
        }
      } else if (ext === "odp" || ext === "ods" || ext === "odt") {
        // content.xml 中的 <text:p> 段落
        const xml = await zip.files["content.xml"].async("string");
        texts = xml.split(/<\/?text:p>/).map((s) => stripHtml(s)).filter(Boolean);
      }
      return texts.join("\n").trim();
    }

    // RTF：去控制字后当文本
    if (ext === "rtf") {
      const buf = await file.text();
      return rtfToText(buf);
    }

    throw new Error("不支持的二进制文档类型：" + ext);
  }

  // 从 OOXML 的 xml 中抽取 <a:t> 文本
  function extractXmlText(xml) {
    const re = /<a:t[^>]*>([\s\S]*?)<\/a:t>/g;
    let m, out = [];
    while ((m = re.exec(xml))) out.push(stripHtml(m[1]));
    return out.join(" ");
  }

  function stripHtml(s) {
    return String(s).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
  }

  // 极简 RTF -> 文本：去掉 {\*\...} 控制块与 \xxx 控制字
  function rtfToText(rtf) {
    return rtf
      .replace(/\{\\\*[\s\S]*?\}/g, " ")      // 删除 {\*\...} 信息块
      .replace(/[{}]/g, " ")                   // 花括号
      .replace(/\\[a-z]+\-?\d*\s?/g, " ")      // 控制字
      .replace(/\\\S/g, " ")                   // 转义符号
      .replace(/\s+/g, " ")
      .trim();
  }

  // 添加文件（FileList / File[]），去重同名同大小
  async function addFiles(fileLike) {
    const files = Array.from(fileLike || []);
    for (const f of files) {
      const item = await readOne(f);
      if (!item) continue;
      const dup = pendingFiles.some((p) => p.name === item.name && p.size === item.size);
      if (!dup) pendingFiles.push(item);
    }
    renderChips();
  }

  function removeAt(idx) {
    pendingFiles.splice(idx, 1);
    renderChips();
  }

  function clearPendingFiles() {
    pendingFiles.length = 0;
    renderChips();
  }

  // 返回给发送逻辑的数据（含 binary 标志）
  function getPendingFiles() {
    return pendingFiles.map((p) => ({ name: p.name, content: p.content, binary: !!p.binary }));
  }

  // 从已有数据恢复文件队列（编辑历史消息时复用，不经过 FileReader 读取）
  function addFilesFromData(files) {
    (files || []).forEach((f) => {
      const content = f.content || "";
      const item = { name: f.name || "未命名", content: content, size: content.length, binary: !!f.binary };
      const dup = pendingFiles.some((p) => p.name === item.name && p.size === item.size);
      if (!dup) pendingFiles.push(item);
    });
    renderChips();
  }

  function hasPendingFiles() {
    return pendingFiles.length > 0;
  }

  // 渲染文件卡片（在输入框上方的 #fileChips 容器）
  function renderChips() {
    const box = document.getElementById("fileChips");
    if (!box) return;
    box.innerHTML = "";
    pendingFiles.forEach((p, idx) => {
      const chip = document.createElement("div");
      chip.className = "file-chip";

      const icon = document.createElement("span");
      icon.className = "file-chip-icon";
      icon.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';

      const meta = document.createElement("span");
      meta.className = "file-chip-meta";
      const name = document.createElement("span");
      name.className = "file-chip-name";
      name.textContent = p.name;
      name.title = p.name;
      const size = document.createElement("span");
      size.className = "file-chip-size";
      size.textContent = formatSize(p.size);
      meta.appendChild(name);
      meta.appendChild(size);

      const del = document.createElement("button");
      del.className = "file-chip-del";
      del.type = "button";
      del.title = "移除该文件";
      del.innerHTML = "&times;";
      del.onclick = (e) => {
        e.stopPropagation();
        removeAt(idx);
      };

      chip.appendChild(icon);
      chip.appendChild(meta);
      chip.appendChild(del);
      box.appendChild(chip);
    });
    // 无文件时隐藏容器（保留布局不跳动）
    box.style.display = pendingFiles.length ? "flex" : "none";
  }

  // 初始化：浏览按钮 + 拖放
  function init() {
    const btn = document.getElementById("attachBtn");
    const hidden = document.getElementById("fileInput");
    if (btn && hidden) {
      btn.onclick = () => hidden.click();
      hidden.onchange = (e) => {
        addFiles(e.target.files);
        e.target.value = ""; // 允许重复选择同一文件
      };
    }

    // 拖放区域：整个输入栏
    const dropZone = document.querySelector(".input-bar");
    if (dropZone) {
      const setActive = (on) => dropZone.classList.toggle("drag-over", on);
      ["dragenter", "dragover"].forEach((ev) =>
        dropZone.addEventListener(ev, (e) => {
          e.preventDefault();
          setActive(true);
        })
      );
      ["dragleave", "dragend"].forEach((ev) =>
        dropZone.addEventListener(ev, (e) => {
          // 仅当离开输入栏区域时取消高亮
          if (e.target === dropZone || !dropZone.contains(e.relatedTarget)) setActive(false);
        })
      );
      dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        setActive(false);
        if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
      });
    }

    renderChips();
  }

  // 暴露 API
  window.FileReaderModule = {
    init,
    addFiles,
    addFilesFromData,
    getPendingFiles,
    clearPendingFiles,
    hasPendingFiles,
    renderChips,
  };

  // 自动初始化（脚本位于 body 末尾，DOM 已就绪）
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

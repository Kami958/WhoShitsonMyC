/* AI 侧栏：自由聊天 / 右键问 AI / 设置草稿联动 */
"use strict";

const _ai = {
  open: false,
  requestId: "",
  streaming: false,
  context: null,
  // 工具上下文详情是否展开
  contextDetailOpen: false,
  // 对比树清理多切片：后端 job_id；无则非清理模式
  cleanupJobId: "",
  cleanupHasMore: false,
  cleanupSlice: 0,
  cleanupDeferredN: 0,
  // 本批未纳入的大项摘要（名称/大小），供继续条与详情展示
  cleanupDeferredTop: [],
  // 多轮对话历史（不含 system；后端 prompts 再拼）
  history: [],
  // rAF 合并 chunk
  pendingText: "",
  raf: 0,
  // 当前助手气泡节点
  bubbleEl: null,
  // 本轮助手回复累积（写入 history）
  replyBuf: "",
  // 流式期间缓存的 tool 提议：{ tool_call_id, items }[]
  pendingProposes: [],
  // 等人审：首轮 done 后仍保留 requestId，卡片未决时禁用发送
  awaitingTools: false,
  // 续写阶段标记（第二轮 stream）
  continuePhase: false,
  // 设置草稿（不进通用 apply_settings）
  draft: null,
  savedHasKey: false,
  // 最近拉取/缓存的模型 id 列表
  modelOptions: [],
  // 运行时：是否已启用（决定左侧入口是否强调可用）
  enabled: false,
};

/** marked + DOMPurify：仅 AI 模块可用时懒加载，失败则回退纯文本。 */
const _aiMd = {
  ready: false,
  failed: false,
  loading: null,
};

function isAiAvailable() {
  return typeof hasModule === "function" && hasModule("ai");
}

function _aiLoadScript(src) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector('script[data-ai-vendor="' + src + '"]');
    if (existing) {
      if (existing.getAttribute("data-loaded") === "1") {
        resolve();
        return;
      }
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener(
        "error",
        () => reject(new Error("load failed: " + src)),
        { once: true }
      );
      return;
    }
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.setAttribute("data-ai-vendor", src);
    s.onload = () => {
      s.setAttribute("data-loaded", "1");
      resolve();
    };
    s.onerror = () => reject(new Error("load failed: " + src));
    document.head.appendChild(s);
  });
}

/** 仅在 AI 模块存在时加载 MD 渲染库；lite / 无 AI 不请求这些脚本。 */
function _aiEnsureMarkdown() {
  if (_aiMd.ready) return Promise.resolve(true);
  if (_aiMd.failed) return Promise.resolve(false);
  if (!isAiAvailable()) return Promise.resolve(false);
  if (_aiMd.loading) return _aiMd.loading;
  _aiMd.loading = _aiLoadScript("js/ai-vendor/marked.min.js")
    .then(() => _aiLoadScript("js/ai-vendor/purify.min.js"))
    .then(() => {
      if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
        throw new Error("markdown globals missing");
      }
      try {
        // UMD: marked.setOptions / marked.use
        if (typeof marked.setOptions === "function") {
          marked.setOptions({ gfm: true, breaks: true });
        } else if (typeof marked.use === "function") {
          marked.use({ breaks: true, gfm: true });
        }
      } catch (e) {}
      _aiMd.ready = true;
      return true;
    })
    .catch((e) => {
      console.warn("[ai] markdown libs unavailable", e);
      _aiMd.failed = true;
      return false;
    })
    .finally(() => {
      _aiMd.loading = null;
    });
  return _aiMd.loading;
}

function _aiRenderMarkdownHtml(text) {
  if (!_aiMd.ready) return null;
  if (typeof marked === "undefined" || typeof DOMPurify === "undefined") return null;
  const raw = String(text || "");
  if (!raw) return "";
  try {
    const parse = marked.parse || marked;
    const html = typeof parse === "function" ? parse(raw) : "";
    return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
  } catch (e) {
    return null;
  }
}

/**
 * 写入气泡内容。
 * @param {HTMLElement|null} el
 * @param {string} text 源文本（助手侧可含 Markdown）
 * @param {{ asMarkdown?: boolean }} [opts]
 */
function _aiSetBodyContent(el, text, opts) {
  if (!el) return;
  const options = opts || {};
  const src = text == null ? "" : String(text);
  if (!options.asMarkdown) {
    el.classList.remove("ai-md");
    el.textContent = src;
    return;
  }
  const html = _aiRenderMarkdownHtml(src);
  if (html == null) {
    el.classList.remove("ai-md");
    el.textContent = src;
    return;
  }
  el.classList.add("ai-md");
  el.innerHTML = html;
}

/* openAiPanel / closeAiPanel / toggleAiPanel / syncAiRailState / refreshAiSideEntry
 * 定义在 pending.js（工具侧栏页签）。 */

function clearAiChat() {
  if (_ai.streaming && _ai.requestId) stopAiRequest();
  else if (_ai.awaitingTools && _ai.requestId) {
    callModule("ai", "cancel", { id: _ai.requestId });
  }
  _ai.history = [];
  _ai.context = null;
  _ai.contextDetailOpen = false;
  _aiClearCleanupJob({ silent: true });
  _ai.replyBuf = "";
  _ai.pendingText = "";
  _ai.pendingProposes = [];
  _ai.awaitingTools = false;
  _ai.continuePhase = false;
  _ai.bubbleEl = null;
  _ai.requestId = "";
  const list = $("#aiMessages");
  if (list) list.innerHTML = "";
  updateAiContextBar();
  updateAiCleanupContinueBar();
  const input = $("#aiInput");
  if (input) {
    input.value = "";
    try {
      input.focus();
    } catch (e) {}
  }
}

/** 序列化当前对话（history + 工具上下文），供导出。 */
function _aiBuildChatExportPayload() {
  const history = [];
  (_ai.history || []).forEach((item) => {
    if (!item || typeof item !== "object") return;
    const role = item.role;
    const content = item.content;
    if ((role === "user" || role === "assistant") && typeof content === "string" && content) {
      history.push({ role: role, content: content });
    }
  });
  let context = null;
  if (_ai.context && typeof _ai.context === "object") {
    try {
      context = JSON.parse(JSON.stringify(_ai.context));
    } catch (e) {
      context = null;
    }
  }
  return {
    format: "WhoShitsOnMyC-ai-chat",
    version: 1,
    exported_at: new Date().toISOString(),
    history: history,
    context: context,
  };
}

function _aiParseChatImport(text) {
  let data;
  try {
    data = JSON.parse(String(text || ""));
  } catch (e) {
    return { error: "invalid" };
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return { error: "invalid" };
  }
  // 兼容：顶层是 history 数组，或标准 {format, history}
  let rawHist = data.history;
  if (!Array.isArray(rawHist) && Array.isArray(data.messages)) {
    rawHist = data.messages;
  }
  if (!Array.isArray(rawHist)) {
    return { error: "invalid" };
  }
  const history = [];
  rawHist.forEach((item) => {
    if (!item || typeof item !== "object") return;
    const role = item.role;
    const content = item.content;
    if ((role === "user" || role === "assistant") && typeof content === "string" && content.trim()) {
      history.push({ role: role, content: content });
    }
  });
  if (!history.length) {
    return { error: "invalid" };
  }
  let context = null;
  if (data.context && typeof data.context === "object" && !Array.isArray(data.context)) {
    const path = data.context.path || data.context.rel_path || "";
    if (path || data.context.kind || "old_size" in data.context || "new_size" in data.context) {
      context = data.context;
    }
  }
  return { history: history, context: context };
}

function _aiRenderHistoryFromState() {
  const list = $("#aiMessages");
  if (list) list.innerHTML = "";
  (_ai.history || []).forEach((item) => {
    if (!item) return;
    if (item.role === "user") {
      _aiAppendMessage("user", item.content, { plain: true });
    } else if (item.role === "assistant") {
      _aiAppendMessage("assistant", item.content);
    }
  });
  updateAiContextBar();
}

async function exportAiChat() {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  if (_ai.streaming) return;
  if (!(_ai.history && _ai.history.length)) {
    toast(t("aiExportEmpty"), true);
    return;
  }
  if (!state.api || !state.api.export_ai_chat) {
    toast(t("aiExportFail"), true);
    return;
  }
  const payload = _aiBuildChatExportPayload();
  let content;
  try {
    content = JSON.stringify(payload, null, 2);
  } catch (e) {
    toast(t("aiExportFail"), true);
    return;
  }
  const btn = $("#aiExportChatBtn");
  if (btn) btn.disabled = true;
  try {
    const res = await state.api.export_ai_chat(content);
    if (res && res.cancelled) return;
    if (res && res.error) {
      toast(res.error || t("aiExportFail"), true);
      return;
    }
    toast(t("aiExportOk"));
  } catch (e) {
    toast(String(e && e.message ? e.message : e) || t("aiExportFail"), true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function importAiChat() {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  if (_ai.streaming) return;
  if (!state.api || !state.api.import_ai_chat) {
    toast(t("aiImportFail"), true);
    return;
  }
  if (_ai.history && _ai.history.length) {
    const ok = await showConfirmDialog({
      title: t("aiImportChat"),
      message: t("aiImportConfirm"),
      okText: t("confirmOk"),
      danger: false,
    });
    if (!ok) return;
  }
  const btn = $("#aiImportChatBtn");
  if (btn) btn.disabled = true;
  try {
    const res = await state.api.import_ai_chat();
    if (res && res.cancelled) return;
    if (res && res.error) {
      toast(res.error || t("aiImportFail"), true);
      return;
    }
    const parsed = _aiParseChatImport(res && res.text);
    if (parsed.error || !parsed.history) {
      toast(t("aiImportInvalid"), true);
      return;
    }
    // 停掉进行中的请求后替换状态
    if (_ai.streaming && _ai.requestId) stopAiRequest();
    _ai.history = parsed.history;
    _ai.context = parsed.context || null;
    _ai.contextDetailOpen = false;
    _aiClearCleanupJob({ silent: true });
    _ai.replyBuf = "";
    _ai.pendingText = "";
    _ai.bubbleEl = null;
    openAiPanel();
    _aiRenderHistoryFromState();
    updateAiCleanupContinueBar();
    toast(t("aiImportOk", parsed.history.length));
  } catch (e) {
    toast(String(e && e.message ? e.message : e) || t("aiImportFail"), true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function clearAiContextOnly() {
  _ai.context = null;
  _ai.contextDetailOpen = false;
  _aiClearCleanupJob({ silent: false });
  updateAiContextBar();
  updateAiCleanupContinueBar();
}

/** 丢弃对比树清理 job（可选通知后端）。 */
function _aiClearCleanupJob(opts) {
  const options = opts || {};
  const jid = _ai.cleanupJobId;
  _ai.cleanupJobId = "";
  _ai.cleanupHasMore = false;
  _ai.cleanupSlice = 0;
  _ai.cleanupDeferredN = 0;
  _ai.cleanupDeferredTop = [];
  if (!options.silent && jid && isAiAvailable()) {
    callModule("ai", "cancel_compare_cleanup", { job_id: jid });
  }
}

function _aiApplyCleanupMeta(res) {
  if (!res || typeof res !== "object") return;
  _ai.cleanupJobId = String(res.job_id || _ai.cleanupJobId || "").trim();
  _ai.cleanupHasMore = !!res.has_more;
  try {
    _ai.cleanupSlice = Number(res.slice);
    if (Number.isNaN(_ai.cleanupSlice)) _ai.cleanupSlice = 0;
  } catch (e) {
    _ai.cleanupSlice = 0;
  }
  const deferredList =
    (Array.isArray(res.deferred_top) && res.deferred_top) ||
    (res.context &&
      Array.isArray(res.context.deferred_top) &&
      res.context.deferred_top) ||
    [];
  _ai.cleanupDeferredTop = deferredList
    .filter((d) => d && typeof d === "object")
    .map((d) => ({
      name: String(d.name || d.rel || d.path || "").trim(),
      metric:
        d.metric != null
          ? d.metric
          : d.new_size != null
            ? d.new_size
            : d.delta != null
              ? Math.abs(d.delta)
              : 0,
      rel: String(d.rel || d.rel_path || "").trim(),
    }))
    .filter((d) => d.name);
  const deferred =
    _ai.cleanupDeferredTop.length ||
    (res.deferred_top && res.deferred_top.length) ||
    (res.context &&
      res.context.deferred_top &&
      res.context.deferred_top.length) ||
    0;
  _ai.cleanupDeferredN = deferred || 0;
  if (!_ai.cleanupHasMore) {
    // 本批已尽，保留 job_id 无意义
    _ai.cleanupJobId = "";
    _ai.cleanupDeferredTop = [];
    _ai.cleanupDeferredN = 0;
  }
}

function updateAiCleanupContinueBar() {
  const bar = $("#aiCleanupContinueBar");
  const hint = $("#aiCleanupContinueHint");
  const btn = $("#aiCleanupContinueBtn");
  if (!bar) return;
  const show =
    !!_ai.cleanupJobId &&
    !!_ai.cleanupHasMore &&
    !_ai.streaming &&
    !_ai.awaitingTools;
  if (!show) {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  if (hint) {
    const names = (_ai.cleanupDeferredTop || [])
      .slice(0, 6)
      .map((d) => d.name)
      .filter(Boolean);
    const more = Math.max(
      0,
      (_ai.cleanupDeferredN || 0) - names.length
    );
    let line = t(
      "aiCleanupContinueHint",
      _ai.cleanupSlice,
      _ai.cleanupDeferredN
    );
    if (names.length) {
      const sep = typeof LANG !== "undefined" && LANG === "zh" ? "、" : ", ";
      line +=
        " · " +
        t("aiCleanupContinueDeferred") +
        ": " +
        names.join(sep);
      if (more > 0) line += "; " + t("aiCleanupContinueMore", more);
    }
    hint.textContent = line;
    hint.title = line;
  }
  if (btn) {
    btn.disabled = !!_ai.streaming || !!_ai.awaitingTools;
  }
}

function _aiHideContextDetail() {
  const detail = $("#aiContextDetail");
  if (detail) {
    detail.classList.add("hidden");
    detail.innerHTML = "";
  }
  _ai.contextDetailOpen = false;
}

function _aiRenderContextDetail() {
  const detail = $("#aiContextDetail");
  if (!detail) return;
  const ctx = _ai.context;
  if (!ctx) {
    detail.classList.add("hidden");
    detail.innerHTML = "";
    return;
  }
  const rows = [];
  const path = ctx.path || ctx.rel_path || ctx.rel || "";
  if (path) {
    rows.push([t("aiContextDetailPath"), path]);
  }
  if (ctx.scenario) {
    rows.push([
      t("aiContextDetailScenario"),
      String(ctx.scenario) +
        (ctx.slice != null ? " · #" + String(Number(ctx.slice) + 1) : ""),
    ]);
  }
  if ("is_dir" in ctx) {
    rows.push([
      t("aiContextDetailType"),
      ctx.is_dir ? t("aiContextDetailTypeDir") : t("aiContextDetailTypeFile"),
    ]);
  }
  if (ctx.kind) {
    rows.push([t("aiContextDetailKind"), String(ctx.kind)]);
  }
  if ("old_size" in ctx || "new_size" in ctx) {
    const sizeText =
      t("aiContextDetailOld") +
      " " +
      fmtBytes(ctx.old_size || 0) +
      " · " +
      t("aiContextDetailNew") +
      " " +
      fmtBytes(ctx.new_size || 0) +
      " · " +
      t("aiContextDetailDelta") +
      " " +
      fmtDelta(ctx.delta || 0);
    rows.push([t("aiContextDetailSizes"), sizeText]);
  }
  if (ctx.mtime) {
    try {
      rows.push([t("aiContextDetailMtime"), fmtTime(ctx.mtime)]);
    } catch (e) {}
  }
  // cleanup 用 items；右键用 children
  const listItems = Array.isArray(ctx.items)
    ? ctx.items
    : Array.isArray(ctx.children)
      ? ctx.children
      : [];
  let html = "";
  if (!rows.length && !listItems.length) {
    html =
      '<div class="ai-context-detail-v">' +
      escapeHtml(t("aiContextDetailEmpty")) +
      "</div>";
  } else {
    rows.forEach((pair) => {
      html +=
        '<div class="ai-context-detail-row">' +
        '<span class="ai-context-detail-k">' +
        escapeHtml(pair[0]) +
        "</span>" +
        '<span class="ai-context-detail-v">' +
        escapeHtml(pair[1]) +
        "</span></div>";
    });
    if (listItems.length) {
      const label =
        ctx.scenario === "cleanup"
          ? t("aiContextDetailItems")
          : t("aiContextDetailChildren");
      const cap = ctx.scenario === "cleanup" ? 40 : 10;
      html +=
        '<div class="ai-context-detail-row">' +
        '<span class="ai-context-detail-k">' +
        escapeHtml(label) +
        "</span>" +
        '<span class="ai-context-detail-v"><ul class="ai-context-detail-children">';
      listItems.slice(0, cap).forEach((ch) => {
        if (!ch || typeof ch !== "object") return;
        const name = ch.name || ch.rel || ch.path || "?";
        const rel = ch.rel || ch.rel_path || "";
        const line =
          name +
          (rel && rel !== name ? " (" + rel + ")" : "") +
          (ch.is_dir ? " /" : "") +
          " · " +
          (ch.kind || "-") +
          " · " +
          fmtDelta(ch.delta || 0) +
          " · " +
          fmtBytes(ch.new_size || 0);
        html += "<li>" + escapeHtml(line) + "</li>";
      });
      html += "</ul></span></div>";
    }
    // 清理多切片：展示本批未纳入的大项（名称+体量）
    const deferredSrc =
      (Array.isArray(ctx.deferred_top) && ctx.deferred_top) ||
      _ai.cleanupDeferredTop ||
      [];
    if (ctx.scenario === "cleanup" && deferredSrc.length) {
      html +=
        '<div class="ai-context-detail-row">' +
        '<span class="ai-context-detail-k">' +
        escapeHtml(t("aiContextDetailDeferred")) +
        "</span>" +
        '<span class="ai-context-detail-v"><ul class="ai-context-detail-children">';
      const defCap = 20;
      deferredSrc.slice(0, defCap).forEach((d) => {
        if (!d || typeof d !== "object") return;
        const name = d.name || d.rel || d.path || "?";
        const metric =
          d.metric != null
            ? d.metric
            : d.new_size != null
              ? d.new_size
              : 0;
        const line = name + " · ≈" + fmtBytes(metric || 0);
        html += "<li>" + escapeHtml(line) + "</li>";
      });
      const more = deferredSrc.length - defCap;
      if (more > 0) {
        html +=
          "<li>" +
          escapeHtml(t("aiContextDetailDeferredMore", more)) +
          "</li>";
      }
      html += "</ul></span></div>";
    }
  }
  detail.innerHTML = html;
  detail.classList.remove("hidden");
  _ai.contextDetailOpen = true;
}

function toggleAiContextDetail() {
  if (!_ai.context) return;
  if (_ai.contextDetailOpen) {
    _aiHideContextDetail();
    return;
  }
  _aiRenderContextDetail();
}

function updateAiContextBar() {
  const bar = $("#aiContextBar");
  const text = $("#aiContextText");
  if (!bar) return;
  const ctx = _ai.context;
  const path =
    (ctx && (ctx.path || ctx.rel_path || ctx.rel || ctx.name)) || "";
  const hasCtx =
    !!ctx &&
    !!(
      path ||
      ctx.scenario ||
      (Array.isArray(ctx.items) && ctx.items.length) ||
      (Array.isArray(ctx.children) && ctx.children.length)
    );
  if (!hasCtx) {
    bar.classList.add("hidden");
    if (text) text.textContent = "";
    _aiHideContextDetail();
    updateAiCleanupContinueBar();
    return;
  }
  bar.classList.remove("hidden");
  if (text) {
    let label = path || String(ctx.name || ctx.scenario || "");
    if (ctx.scenario === "cleanup") {
      const n =
        (Array.isArray(ctx.items) && ctx.items.length) ||
        ctx.paths_in_slice ||
        0;
      label =
        (path || t("aiContextChipLabel")) +
        " · " +
        t("aiCleanupSliceLabel", (Number(ctx.slice) || 0) + 1, n);
    }
    text.textContent = t("aiContextLabel", label);
    text.title = label;
  }
  if (_ai.contextDetailOpen) {
    _aiRenderContextDetail();
  } else {
    _aiHideContextDetail();
  }
  updateAiCleanupContinueBar();
}

function _aiAppendMessage(role, text, opts) {
  const list = $("#aiMessages");
  if (!list) return null;
  const options = opts || {};
  const isUser = role === "user";
  const isLocal = !!options.local;
  const row = document.createElement("div");
  row.className = "ai-msg ai-msg-" + (isUser ? "user" : "assistant");
  if (options.pending) row.classList.add("ai-msg-pending");
  if (isLocal) row.classList.add("ai-msg-local");
  // 楼层式：每条消息带角色标注，不用气泡
  const head = document.createElement("div");
  head.className = "ai-msg-role";
  head.textContent = isUser ? t("aiRoleUser") : t("aiRoleAssistant");
  const body = document.createElement("div");
  body.className = "ai-msg-body";
  const src = text || "";
  // 用户输入 / 错误 / 本地提示：纯文本。助手回复：Markdown（消毒后渲染）
  const asMarkdown =
    !isUser && !options.plain && !options.error && !options.pending && !isLocal;
  if (asMarkdown && src) {
    _aiSetBodyContent(body, src, { asMarkdown: true });
    if (!_aiMd.ready) {
      _aiEnsureMarkdown().then((ok) => {
        if (ok && body.isConnected) {
          _aiSetBodyContent(body, src, { asMarkdown: true });
        }
      });
    }
  } else {
    body.textContent = src;
  }
  row.appendChild(head);
  row.appendChild(body);
  list.appendChild(row);
  list.scrollTop = list.scrollHeight;
  return body;
}

/**
 * AI 提议加入待删除：走页面级勾选弹窗（与手动提议同一套），再 continue_tools。
 * @param {Array<{tool_call_id?: string, items: Array}>} batches
 */
async function _aiShowProposeDialog(batches) {
  const mergedItems = [];
  const toolCallIds = [];
  (batches || []).forEach((batch) => {
    if (!batch) return;
    const tid = String(batch.tool_call_id || "").trim();
    if (tid && toolCallIds.indexOf(tid) < 0) toolCallIds.push(tid);
    const items = Array.isArray(batch.items)
      ? batch.items
      : Array.isArray(batch)
        ? batch
        : [];
    items.forEach((it) => {
      if (it && typeof it === "object") mergedItems.push(it);
    });
  });
  if (!mergedItems.length) {
    _ai.awaitingTools = false;
    if (_ai.requestId) {
      _aiContinueTools(toolCallIds, {
        status: "cancelled",
        accepted: 0,
        rejected: 0,
        message: t("aiProposeCardResultCancelled"),
      });
    }
    return;
  }

  const total = mergedItems.length;
  if (typeof showPendingChecklistDialog !== "function") {
    toast(t("aiProposeCardResultCancelled"), true);
    _aiContinueTools(toolCallIds, {
      status: "cancelled",
      accepted: 0,
      rejected: total,
      message: t("aiProposeCardResultCancelled"),
    });
    return;
  }

  const selected = await showPendingChecklistDialog(mergedItems);
  if (!selected) {
    const msg = t("aiProposeCardResultCancelled");
    _aiContinueTools(toolCallIds, {
      status: "cancelled",
      accepted: 0,
      rejected: total,
      message: msg,
    });
    return;
  }
  if (!selected.length) {
    toast(t("aiProposeCardEmpty"), true);
    const msg = t("aiProposeCardResultCancelled");
    _aiContinueTools(toolCallIds, {
      status: "cancelled",
      accepted: 0,
      rejected: total,
      message: msg,
    });
    return;
  }

  let added = 0;
  if (typeof enqueuePendingFromAi === "function") {
    const enq = enqueuePendingFromAi(selected);
    added = (enq && enq.added) || 0;
  }
  const accepted = selected.length;
  const rejected = Math.max(0, total - accepted);
  const msg =
    added > 0
      ? t("aiProposeCardResultAdded", added)
      : t("aiProposeCardResultNone");
  if (added > 0) toast(msg);
  _aiContinueTools(toolCallIds, {
    status: "approved",
    accepted,
    rejected,
    message: msg,
  });
}

/** 首轮结束后弹出页面勾选框；需仍保留 requestId。有节点上下文才接受 propose。 */
function _aiFlushPendingProposes(_assistantRow) {
  const batches = _ai.pendingProposes || [];
  _ai.pendingProposes = [];
  if (!batches.length) return;
  const ctx = _ai.context;
  // 自由聊无节点：丢弃误触发的 propose
  if (!ctx) {
    if (_ai.requestId && _ai.awaitingTools) {
      _aiContinueTools([], {
        status: "cancelled",
        accepted: 0,
        rejected: 0,
        message: t("aiProposeCardResultCancelled"),
      });
    }
    return;
  }
  // 不阻塞事件循环；弹窗内 await 后 continue_tools
  Promise.resolve()
    .then(() => _aiShowProposeDialog(batches))
    .catch(() => {
      _aiContinueTools([], {
        status: "cancelled",
        accepted: 0,
        rejected: 0,
        message: t("aiProposeCardResultCancelled"),
      });
    });
}

/**
 * 用户确认/取消后回传模型并续写。
 * @param {string|string[]} toolCallIds
 * @param {{status:string, accepted:number, rejected:number, message:string}} result
 */
async function _aiContinueTools(toolCallIds, result) {
  const reqId = _ai.requestId;
  if (!reqId) {
    _ai.awaitingTools = false;
    _aiSetStreaming(false);
    return;
  }
  _ai.awaitingTools = false;
  _ai.continuePhase = true;
  _ai.replyBuf = "";
  _ai.pendingText = "";
  _ai.bubbleEl = _aiAppendMessage("assistant", "", { pending: true });
  _aiSetStreaming(true);

  const ids = Array.isArray(toolCallIds)
    ? toolCallIds.filter(Boolean)
    : [toolCallIds].filter(Boolean);
  if (!ids.length) ids.push(`call_${reqId}_0`);
  const status = (result && result.status) || "cancelled";
  const accepted = (result && result.accepted) || 0;
  const rejected = (result && result.rejected) || 0;
  const message = (result && result.message) || "";
  const payload = {
    id: reqId,
    results: ids.map((tid, i) => ({
      tool_call_id: tid,
      status,
      // 计数只挂在第一条，避免模型误读成倍数
      accepted: i === 0 ? accepted : 0,
      rejected: i === 0 ? rejected : 0,
      message: i === 0 ? message : status,
    })),
  };
  const res = await callModule("ai", "continue_tools", payload);
  if (res && res.error) {
    _aiSetStreaming(false);
    _ai.continuePhase = false;
    if (_ai.bubbleEl) {
      _aiSetBodyContent(_ai.bubbleEl, res.error, { asMarkdown: false });
      const row = _ai.bubbleEl.parentElement;
      if (row) row.classList.remove("ai-msg-pending");
    }
    toast(res.error, true);
    _ai.requestId = "";
    _ai.bubbleEl = null;
    _ai.replyBuf = "";
    return;
  }
  if (res && res.id) _ai.requestId = res.id;
}

function _aiFlushChunk() {
  _ai.raf = 0;
  if (!_ai.bubbleEl || !_ai.pendingText) return;
  _ai.replyBuf += _ai.pendingText;
  _ai.pendingText = "";
  // 流式过程按完整缓冲重渲 MD（未加载库时回退纯文本）
  _aiSetBodyContent(_ai.bubbleEl, _ai.replyBuf, { asMarkdown: true });
  const list = $("#aiMessages");
  if (list) list.scrollTop = list.scrollHeight;
}

function _aiQueueChunk(text) {
  if (!text) return;
  _ai.pendingText += text;
  if (!_ai.raf) {
    _ai.raf = requestAnimationFrame(_aiFlushChunk);
  }
}

function _aiSetStreaming(on) {
  _ai.streaming = !!on;
  // 等人审时也禁止发送，但停止按钮仍可用
  const busy = !!on || !!_ai.awaitingTools;
  const stopBtn = $("#aiStopBtn");
  const sendBtn = $("#aiSendBtn");
  if (stopBtn) stopBtn.classList.toggle("hidden", !busy);
  if (sendBtn) sendBtn.disabled = busy;
  const input = $("#aiInput");
  if (input) input.disabled = busy;
  const newBtn = $("#aiNewChatBtn");
  if (newBtn) newBtn.disabled = busy;
  const exportBtn = $("#aiExportChatBtn");
  if (exportBtn) exportBtn.disabled = busy;
  const importBtn = $("#aiImportChatBtn");
  if (importBtn) importBtn.disabled = busy;
  const panelModel = $("#aiPanelModelSelect");
  if (panelModel) panelModel.disabled = busy;
  const panelFetch = $("#aiPanelFetchModelsBtn");
  if (panelFetch) panelFetch.disabled = busy;
  updateAiCleanupContinueBar();
}

async function ensureAiConsent() {
  let cfg;
  try {
    cfg = await callModule("ai", "get_config");
  } catch (e) {
    return false;
  }
  if (cfg && cfg.error) {
    toast(cfg.error, true);
    return false;
  }
  if (cfg && typeof cfg.enabled === "boolean") {
    _ai.enabled = !!cfg.enabled;
    refreshAiSideEntry();
  }
  if (cfg && cfg.consented) return true;
  const ok = await showConfirmDialog({
    title: t("aiPrivacyTitle"),
    message: t("aiPrivacyMessage"),
    okText: t("aiPrivacyAccept"),
    danger: false,
  });
  if (!ok) return false;
  const res = await callModule("ai", "set_config", { consented: true });
  if (res && res.error) {
    toast(res.error, true);
    return false;
  }
  return true;
}

/** 右键问 AI：一层子项上限（仅 right_click；清理场景不用此数）。 */
const RIGHT_CLICK_MAX_CHILDREN = 10;
const _AI_MAX_CHILDREN = RIGHT_CLICK_MAX_CHILDREN;

/**
 * 把 get_children / 缓存节点列表收成 SoftwareContext.children 形状。
 * 右键：只一层；条数截断为 RIGHT_CLICK_MAX_CHILDREN。
 */
function _aiMapChildrenPreview(nodes, root) {
  const list = Array.isArray(nodes) ? nodes : [];
  const base = root != null ? root : state.compareRoot || "";
  const out = [];
  for (let i = 0; i < list.length && out.length < RIGHT_CLICK_MAX_CHILDREN; i++) {
    const ch = list[i];
    if (!ch || typeof ch !== "object") continue;
    const rel = String(ch.path || ch.rel || ch.rel_path || "").trim();
    const name = String(ch.name || rel || "").trim() || "?";
    const abs =
      (ch.full && String(ch.full)) ||
      (typeof fullPath === "function" ? fullPath(base, rel) : rel);
    const row = {
      name,
      kind: ch.kind || "",
      delta: ch.delta || 0,
      new_size: ch.new_size || 0,
      old_size: ch.old_size || 0,
      rel,
      path: abs || "",
      is_dir: !!ch.is_dir,
    };
    if (ch.mtime) row.mtime = ch.mtime;
    out.push(row);
  }
  return out;
}

/**
 * 目录只展开一层：async 拉取直接子项（|delta| 已由后端排序）。
 * 失败返回 []，不阻断问 AI。
 */
async function loadAiChildrenPreview(node) {
  if (!node || !node.is_dir) return [];
  if (Array.isArray(node._childrenPreview) && node._childrenPreview.length) {
    return _aiMapChildrenPreview(node._childrenPreview, state.compareRoot || "");
  }
  // 顶层：已有 compare 结果时可免请求
  const relParent = String(node.path || "").trim();
  if (
    !relParent &&
    Array.isArray(state._topNodes) &&
    state._topNodes.length
  ) {
    const mapped = _aiMapChildrenPreview(state._topNodes, state.compareRoot || "");
    node._childrenPreview = mapped;
    return mapped;
  }
  if (
    !state.api ||
    typeof state.api.get_children !== "function" ||
    !state.oldPath ||
    !state.newPath
  ) {
    return [];
  }
  try {
    const res = await state.api.get_children(
      state.oldPath,
      state.newPath,
      relParent
    );
    if (!res || res.error || !Array.isArray(res.nodes)) return [];
    const mapped = _aiMapChildrenPreview(res.nodes, state.compareRoot || "");
    node._childrenPreview = mapped;
    return mapped;
  } catch (e) {
    return [];
  }
}

/**
 * 从对比树节点组装 AI 上下文。
 * @param {object} node
 * @param {Array|null} childrenOverride 已加载的一层子项（可选）
 */
function buildAiContextFromNode(node, childrenOverride) {
  if (!node) return null;
  const path = fullPath(state.compareRoot || "", node.path || "");
  let children = [];
  if (Array.isArray(childrenOverride)) {
    children = _aiMapChildrenPreview(childrenOverride, state.compareRoot || "");
  } else if (Array.isArray(node._childrenPreview)) {
    children = _aiMapChildrenPreview(node._childrenPreview, state.compareRoot || "");
  }
  // 不传 scan_root：完整 path 已含扫描根信息
  return {
    scenario: "right_click",
    path: path,
    rel_path: node.path || "",
    rel: node.path || "",
    name: node.name || "",
    is_dir: !!node.is_dir,
    kind: node.kind || "",
    old_size: node.old_size || 0,
    new_size: node.new_size || 0,
    delta: node.delta || 0,
    mtime: node.mtime || 0,
    children: children,
  };
}

/**
 * 用指定快照对启动清理 packing（单快照时 old=new 即可）。
 * @returns {Promise<boolean>}
 */
async function _aiStartCleanupWithPaths(opts) {
  const options = opts || {};
  const oldPath = String(options.old_path || "").trim();
  const newPath = String(options.new_path || oldPath || "").trim();
  const root = String(options.root || "").trim();
  const seedIn = options.seed;
  if (!oldPath || !newPath) {
    toast(t("diskCleanupStartFail"), true);
    return false;
  }
  if (_ai.streaming || _ai.awaitingTools) {
    toast(t("aiCleanupBusy"), true);
    return false;
  }

  const seed = seedIn && typeof seedIn === "object"
    ? seedIn
    : {
        path: "",
        name: root || "root",
        is_dir: true,
        kind: "",
        old_size: 0,
        new_size: Number(options.total_size) || 0,
        delta: 0,
      };

  const res = await callModule("ai", "start_compare_cleanup", {
    old_path: oldPath,
    new_path: newPath,
    seed: seed,
    root: root,
  });
  if (!res || res.error) {
    toast((res && res.error) || t("aiCleanupPackFail"), true);
    return false;
  }
  const ctx = res.context && typeof res.context === "object" ? res.context : null;
  if (!ctx) {
    toast(t("aiCleanupPackFail"), true);
    return false;
  }
  if (!ctx.root && root) ctx.root = root;

  _ai.context = ctx;
  _ai.contextDetailOpen = false;
  _aiApplyCleanupMeta(res);
  openAiPanel();
  updateAiContextBar();
  updateAiCleanupContinueBar();

  const q = t("aiCleanupDefaultQuestion");
  const input = $("#aiInput");
  if (input) {
    input.value = q;
    try {
      input.focus();
      input.select();
    } catch (e) {}
  }
  return true;
}

/**
 * 对比树右键「AI 清理分析」：服务端 packing 第一批 → 挂 context → 打开侧栏。
 * 真删仍只走用户同意后的 pending。
 */
async function startCompareCleanupFromNode(node) {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  if (!state.oldPath || !state.newPath) {
    toast(t("aiCleanupNeedCompare"), true);
    return;
  }

  const seed = node
    ? {
        path: node.path || "",
        name: node.name || "",
        is_dir: node.is_dir !== false,
        kind: node.kind || "",
        old_size: node.old_size || 0,
        new_size: node.new_size || 0,
        delta: node.delta || 0,
        mtime: node.mtime || 0,
      }
    : { path: "", name: state.compareRoot || "root", is_dir: true };

  await _aiStartCleanupWithPaths({
    old_path: state.oldPath,
    new_path: state.newPath,
    root: state.compareRoot || "",
    seed: seed,
  });
}

function _aiNormRootKey(p) {
  let s = String(p || "").replace(/\//g, "\\").trim();
  if (!s) return "";
  // 盘符根统一成 C:\
  if (/^[a-zA-Z]:$/.test(s)) s += "\\";
  if (/^[a-zA-Z]:\\$/.test(s)) return s.toUpperCase();
  return s.replace(/[\\/]+$/, "").toLowerCase();
}

function _aiSnapshotsMatchingRoot(folderPath) {
  const key = _aiNormRootKey(folderPath);
  if (!key) return [];
  const list = Array.isArray(state.snapshots) ? state.snapshots : [];
  return list.filter((s) => s && _aiNormRootKey(s.root) === key);
}

function closeDiskCleanupSourceDialog() {
  const ov = $("#diskCleanupSourceOverlay");
  if (ov) ov.classList.add("hidden");
}

function closeDiskCleanupSnapDialog() {
  const ov = $("#diskCleanupSnapOverlay");
  if (ov) ov.classList.add("hidden");
  const list = $("#diskCleanupSnapList");
  if (list) list.innerHTML = "";
}

function openDiskCleanupSourceDialog() {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  closeDiskCleanupSnapDialog();
  const ov = $("#diskCleanupSourceOverlay");
  if (ov) ov.classList.remove("hidden");
}

/**
 * 侧栏「AI 清理」：选快照或选目录（目录须已有快照）。
 * 暂不接入产品入口；函数保留，勿从 UI 调用。
 */
function openDiskCleanupEntry() {
  // 产品暂隐藏：需要恢复时取消 wireDiskCleanupUi 内绑定即可
  return;
}

function _aiRenderSnapPickList(snaps, listEl) {
  if (!listEl) return;
  listEl.innerHTML = "";
  const rows = Array.isArray(snaps) ? snaps.slice() : [];
  rows.sort((a, b) => (Number(b.scanned_at) || 0) - (Number(a.scanned_at) || 0));
  rows.forEach((s) => {
    if (!s || !s.path) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "disk-cleanup-snap-item";
    btn.setAttribute("role", "option");
    const root = document.createElement("div");
    root.className = "disk-cleanup-snap-root";
    root.textContent = s.root || s.path;
    const meta = document.createElement("div");
    meta.className = "disk-cleanup-snap-meta";
    const parts = [];
    try {
      if (s.scanned_at) parts.push(fmtTime(s.scanned_at));
    } catch (e) {}
    if (s.total_size != null) parts.push(fmtBytes(s.total_size || 0));
    if (s.note) parts.push(String(s.note));
    meta.textContent = parts.join(" · ");
    btn.appendChild(root);
    btn.appendChild(meta);
    btn.onclick = () => {
      closeDiskCleanupSnapDialog();
      closeDiskCleanupSourceDialog();
      startDiskCleanupFromSnapshot(s);
    };
    listEl.appendChild(btn);
  });
}

function openDiskCleanupSnapPicker(snaps, opts) {
  const options = opts || {};
  const list = Array.isArray(snaps) ? snaps : [];
  if (!list.length) {
    toast(t("diskCleanupNoSnaps"), true);
    return;
  }
  if (list.length === 1 && !options.forceList) {
    closeDiskCleanupSourceDialog();
    startDiskCleanupFromSnapshot(list[0]);
    return;
  }
  closeDiskCleanupSourceDialog();
  const title = $("#diskCleanupSnapTitle");
  const hint = $("#diskCleanupSnapHint");
  if (title) title.textContent = options.title || t("diskCleanupSnapTitle");
  if (hint) hint.textContent = options.hint || t("diskCleanupSnapHint");
  const listEl = $("#diskCleanupSnapList");
  _aiRenderSnapPickList(list, listEl);
  const ov = $("#diskCleanupSnapOverlay");
  if (ov) ov.classList.remove("hidden");
}

async function startDiskCleanupFromSnapshot(snap) {
  if (!snap || !snap.path) {
    toast(t("diskCleanupStartFail"), true);
    return;
  }
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  // 单快照：同一路径作 old/new，metric 走 new_size
  await _aiStartCleanupWithPaths({
    old_path: snap.path,
    new_path: snap.path,
    root: snap.root || "",
    total_size: snap.total_size || 0,
    seed: {
      path: "",
      rel: "",
      name: snap.root || "root",
      is_dir: true,
      kind: "",
      old_size: 0,
      new_size: Number(snap.total_size) || 0,
      delta: Number(snap.total_size) || 0,
    },
  });
}

async function pickDiskCleanupSnapshotFromList() {
  const snaps = Array.isArray(state.snapshots) ? state.snapshots.slice() : [];
  if (!snaps.length) {
    toast(t("diskCleanupNoSnaps"), true);
    return;
  }
  openDiskCleanupSnapPicker(snaps, { forceList: true });
}

async function pickDiskCleanupFolder() {
  if (!state.api || typeof state.api.choose_folder !== "function") {
    toast(t("diskCleanupStartFail"), true);
    return;
  }
  let res;
  try {
    res = await state.api.choose_folder();
  } catch (e) {
    toast(String(e && e.message ? e.message : e) || t("diskCleanupStartFail"), true);
    return;
  }
  if (!res || !res.path) return;
  const matches = _aiSnapshotsMatchingRoot(res.path);
  if (!matches.length) {
    toast(t("diskCleanupNoSnapForFolder"), true);
    return;
  }
  if (matches.length === 1) {
    closeDiskCleanupSourceDialog();
    await startDiskCleanupFromSnapshot(matches[0]);
    return;
  }
  openDiskCleanupSnapPicker(matches, {
    title: t("diskCleanupMultiSnapTitle"),
    hint: t("diskCleanupMultiSnapHint"),
    forceList: true,
  });
}

/**
 * 侧栏「AI 清理」入口与对话框。
 * 暂不接入产品：不绑定入口按钮，避免出现在界面；实现保留便于后续打开。
 */
function wireDiskCleanupUi() {
  // 入口已隐藏且不绑定 onclick。若以后恢复：
  // const entry = $("#diskCleanupBtn");
  // if (entry) entry.onclick = () => openDiskCleanupEntry();
  return;
}

/** 同一清理 job 的下一批 context（不自动发问，预填默认问句）。 */
async function continueCompareCleanup() {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  if (!_ai.cleanupJobId || !_ai.cleanupHasMore) {
    updateAiCleanupContinueBar();
    return;
  }
  if (_ai.streaming || _ai.awaitingTools) {
    toast(t("aiCleanupBusy"), true);
    return;
  }
  const res = await callModule("ai", "next_compare_cleanup", {
    job_id: _ai.cleanupJobId,
  });
  if (!res || res.error) {
    toast((res && res.error) || t("aiCleanupPackFail"), true);
    if (res && res.error) {
      _aiClearCleanupJob({ silent: true });
      updateAiCleanupContinueBar();
    }
    return;
  }
  const ctx = res.context && typeof res.context === "object" ? res.context : null;
  if (!ctx) {
    toast(t("aiCleanupPackFail"), true);
    return;
  }
  if (!ctx.root && state.compareRoot) ctx.root = state.compareRoot;
  _ai.context = ctx;
  _ai.contextDetailOpen = false;
  _aiApplyCleanupMeta(res);
  openAiPanel();
  updateAiContextBar();
  updateAiCleanupContinueBar();

  const input = $("#aiInput");
  if (input) {
    input.value = t("aiCleanupDefaultQuestion");
    try {
      input.focus();
      input.select();
    } catch (e) {}
  }
}

/** 截断 history，避免请求体过大。 */
function _aiHistoryForRequest() {
  const max = 12; // 6 轮
  if (_ai.history.length <= max) return _ai.history.slice();
  return _ai.history.slice(_ai.history.length - max);
}

async function _aiStartAsk(question, opts) {
  const options = opts || {};
  const q = String(question || "").trim();
  if (!q) return;

  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  if (_ai.streaming || _ai.awaitingTools) return;

  const consented = await ensureAiConsent();
  if (!consented) return;

  // 再读一次启用状态（设置页可能刚改过）
  try {
    const cfg = await callModule("ai", "get_config");
    if (cfg && !cfg.error) {
      _ai.enabled = !!cfg.enabled;
      refreshAiSideEntry();
      if (!cfg.enabled) {
        toast(t("aiNeedEnable"), true);
        return;
      }
    }
  } catch (e) {}

  openAiPanel();

  // 去掉欢迎气泡占位（若仍是唯一一条助手欢迎语）
  const list = $("#aiMessages");
  if (list && list.children.length === 1) {
    const only = list.children[0];
    if (only && only.classList.contains("ai-msg-assistant") && !_ai.history.length) {
      // 保留也无妨；不强制删
    }
  }

  _aiAppendMessage("user", q);
  _ai.history.push({ role: "user", content: q });
  _ai.bubbleEl = _aiAppendMessage("assistant", "", { pending: true });
  _ai.pendingText = "";
  _ai.replyBuf = "";
  _ai.pendingProposes = [];
  _ai.awaitingTools = false;
  _ai.continuePhase = false;
  _aiSetStreaming(true);

  const payload = {
    question: q,
    history: _aiHistoryForRequest().slice(0, -1), // 当前问题已在 messages 末尾拼，历史不含本轮 user
  };
  // 有路径上下文则带上；自由聊天传空对象
  if (_ai.context && !options.forceFree) {
    payload.context = _ai.context;
  } else {
    payload.context = {};
  }

  const res = await callModule("ai", "ask", payload);
  if (res && res.error) {
    _aiSetStreaming(false);
    // 回滚本轮 user history
    if (_ai.history.length && _ai.history[_ai.history.length - 1].role === "user") {
      _ai.history.pop();
    }
    if (_ai.bubbleEl) {
      _aiSetBodyContent(_ai.bubbleEl, res.error, { asMarkdown: false });
      const row = _ai.bubbleEl.parentElement;
      if (row) row.classList.remove("ai-msg-pending");
    }
    if (res.need_enable) toast(t("aiNeedEnable"), true);
    else if (res.need_consent) toast(t("aiNeedConsent"), true);
    else toast(res.error, true);
    _ai.requestId = "";
    _ai.bubbleEl = null;
    _ai.replyBuf = "";
    return;
  }
  _ai.requestId = (res && res.id) || "";
  if (!_ai.requestId) {
    _aiSetStreaming(false);
    if (_ai.bubbleEl) {
      _aiSetBodyContent(_ai.bubbleEl, t("aiRequestFailed"), { asMarkdown: false });
    }
    _ai.bubbleEl = null;
  }
}

async function askAiAboutNode(node, question) {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  // 右键单项分析：退出清理多切片
  _aiClearCleanupJob({ silent: false });
  if (node) {
    // 目录：只展开一层子项再组 context（失败则仅主项）
    let preview = null;
    if (node.is_dir) {
      preview = await loadAiChildrenPreview(node);
    }
    _ai.context = buildAiContextFromNode(node, preview);
    _ai.contextDetailOpen = false;
  }
  openAiPanel();
  updateAiContextBar();
  updateAiCleanupContinueBar();
  const q =
    (question && String(question).trim()) || t("aiDefaultQuestion");
  const input = $("#aiInput");
  if (input) {
    input.value = q;
    try {
      input.focus();
      // 选中默认文案，方便直接改或回车发送
      input.select();
    } catch (e) {}
  }
}

async function sendAiFollowup() {
  if (!isAiAvailable() || _ai.streaming || _ai.awaitingTools) return;
  const input = $("#aiInput");
  const q = input ? String(input.value || "").trim() : "";
  if (!q) return;
  if (input) input.value = "";
  await _aiStartAsk(q);
}

function stopAiRequest() {
  const id = _ai.requestId;
  const wasAwaiting = !!_ai.awaitingTools;
  _ai.awaitingTools = false;
  _ai.continuePhase = false;
  _ai.pendingProposes = [];
  // 关闭未完成的页面勾选弹窗
  if (typeof closePendingChecklistDialog === "function") {
    try {
      closePendingChecklistDialog(null);
    } catch (e) {}
  }
  if (id) {
    callModule("ai", "cancel", { id: id });
  }
  _aiFlushChunk();
  if (!wasAwaiting) {
    if (_ai.bubbleEl && !_ai.replyBuf) {
      _aiSetBodyContent(_ai.bubbleEl, t("aiStopped"), { asMarkdown: false });
    } else if (_ai.bubbleEl && _ai.replyBuf) {
      _aiSetBodyContent(_ai.bubbleEl, _ai.replyBuf, { asMarkdown: true });
    }
    if (_ai.bubbleEl && _ai.bubbleEl.parentElement) {
      _ai.bubbleEl.parentElement.classList.remove("ai-msg-pending");
    }
    // 已产生的部分回复仍记入 history，便于继续聊
    if (_ai.replyBuf) {
      _ai.history.push({ role: "assistant", content: _ai.replyBuf });
    }
  }
  _ai.requestId = "";
  _ai.bubbleEl = null;
  _ai.replyBuf = "";
  _aiSetStreaming(false);
}

/** 处理 Python 推送的 AI 事件。 */
function onAiPyEvent(event, payload) {
  const p = payload || {};
  if (p.id && _ai.requestId && p.id !== _ai.requestId) {
    return;
  }
  if (event === "ai-chunk") {
    if (!_ai.bubbleEl) {
      _ai.bubbleEl = _aiAppendMessage("assistant", "", { pending: true });
    }
    _aiQueueChunk(p.text || "");
    return;
  }
  if (event === "ai-tool-propose") {
    // 流式未结束则缓存；无节点上下文丢弃
    if (!_ai.context) return;
    const items = Array.isArray(p.items) ? p.items : [];
    if (!items.length) return;
    if (!_ai.pendingProposes) _ai.pendingProposes = [];
    _ai.pendingProposes.push({
      tool_call_id: String(p.tool_call_id || ""),
      items: items,
    });
    return;
  }
  if (event === "ai-done") {
    _aiFlushChunk();
    const full = _ai.replyBuf || "";
    const row = _ai.bubbleEl && _ai.bubbleEl.parentElement;
    if (_ai.bubbleEl && row) {
      row.classList.remove("ai-msg-pending");
    }
    if (_ai.bubbleEl && !full) {
      // 仅 tool 无正文时允许空；续写阶段仍提示
      if (_ai.continuePhase || !p.awaiting_tools) {
        _aiSetBodyContent(_ai.bubbleEl, t("aiEmptyReply"), { asMarkdown: false });
      }
    } else if (_ai.bubbleEl && full) {
      const el = _ai.bubbleEl;
      _aiSetBodyContent(el, full, { asMarkdown: true });
      if (!_aiMd.ready) {
        _aiEnsureMarkdown().then((ok) => {
          if (ok && el.isConnected) {
            _aiSetBodyContent(el, full, { asMarkdown: true });
          }
        });
      }
    }
    if (full) {
      _ai.history.push({ role: "assistant", content: full });
    }

    const awaiting = !!p.awaiting_tools && !_ai.continuePhase;
    if (awaiting) {
      // 保留 requestId，页面弹窗勾选，等人审后 continue_tools
      _ai.awaitingTools = true;
      _ai.streaming = false;
      _ai.bubbleEl = null;
      _ai.replyBuf = "";
      _aiFlushPendingProposes(row);
      _aiSetStreaming(false);
      return;
    }

    _ai.awaitingTools = false;
    _ai.continuePhase = false;
    _ai.pendingProposes = [];
    _ai.requestId = "";
    _ai.bubbleEl = null;
    _ai.replyBuf = "";
    _aiSetStreaming(false);
    return;
  }
  if (event === "ai-error") {
    _aiFlushChunk();
    _ai.awaitingTools = false;
    _ai.continuePhase = false;
    // 出错/停止时丢弃未确认的提议
    _ai.pendingProposes = [];
    if (typeof closePendingChecklistDialog === "function") {
      try {
        closePendingChecklistDialog(null);
      } catch (e) {}
    }
    const msg = p.message || t("aiRequestFailed");
    if (_ai.bubbleEl) {
      if (!_ai.replyBuf) {
        _aiSetBodyContent(_ai.bubbleEl, msg, { asMarkdown: false });
      } else if (msg && !p.cancelled) {
        const combined = _ai.replyBuf + "\n\n" + msg;
        _aiSetBodyContent(_ai.bubbleEl, combined, { asMarkdown: true });
      } else if (_ai.replyBuf) {
        _aiSetBodyContent(_ai.bubbleEl, _ai.replyBuf, { asMarkdown: true });
      }
      if (_ai.bubbleEl.parentElement) {
        _ai.bubbleEl.parentElement.classList.remove("ai-msg-pending");
      }
    } else if (!p.cancelled) {
      _aiAppendMessage("assistant", msg, { error: true });
    }
    const partial = _ai.replyBuf;
    if (partial) {
      _ai.history.push({ role: "assistant", content: partial });
    }
    if (!p.cancelled) toast(msg, true);
    _ai.requestId = "";
    _ai.bubbleEl = null;
    _ai.replyBuf = "";
    _aiSetStreaming(false);
  }
}

// ---- 设置页 AI 草稿 -------------------------------------------------------

async function loadAiSettingsDraft() {
  if (!isAiAvailable()) {
    _ai.draft = null;
    _ai.enabled = false;
    _ai.modelOptions = [];
    refreshAiSideEntry();
    return;
  }
  const cfg = await callModule("ai", "get_config");
  if (cfg && cfg.error) {
    _ai.draft = {
      enabled: false,
      base_url: "https://api.openai.com/v1",
      model: "",
      api_key: "",
      extra_prompt: "",
      consented: false,
      has_key: false,
      key_dirty: false,
      model_options: [],
      cleanup_max_depth: 3,
      enabled_tools: _aiDefaultEnabledTools(),
      tool_catalog: _aiDefaultToolCatalog(),
    };
    _ai.modelOptions = [];
    _ai.enabled = false;
    fillAiSettingsForm();
    refreshAiSideEntry();
    return;
  }
  const opts = Array.isArray(cfg && cfg.model_options)
    ? cfg.model_options.map((x) => String(x || "").trim()).filter(Boolean)
    : [];
  let depth = 3;
  if (cfg && cfg.cleanup_max_depth != null) {
    const n = parseInt(cfg.cleanup_max_depth, 10);
    if (!isNaN(n)) depth = Math.max(1, Math.min(8, n));
  }
  _ai.draft = {
    enabled: !!(cfg && cfg.enabled),
    base_url: (cfg && cfg.base_url) || "https://api.openai.com/v1",
    model: (cfg && cfg.model) || "",
    api_key: "",
    extra_prompt: (cfg && cfg.extra_prompt) || "",
    consented: !!(cfg && cfg.consented),
    has_key: !!(cfg && cfg.has_key),
    key_dirty: false,
    model_options: opts.slice(),
    cleanup_max_depth: depth,
    enabled_tools: _aiNormalizeEnabledTools(
      cfg && cfg.enabled_tools,
      cfg && cfg.tools_enabled
    ),
    tool_catalog: _aiNormalizeToolCatalog(cfg && cfg.tool_catalog),
  };
  _ai.modelOptions = opts.slice();
  _ai.savedHasKey = !!(cfg && cfg.has_key);
  _ai.enabled = !!(cfg && cfg.enabled);
  fillAiSettingsForm();
  refreshAiSideEntry();
}

function _aiNormalizeModelOptions(list) {
  const out = [];
  const seen = Object.create(null);
  (Array.isArray(list) ? list : []).forEach((item) => {
    const s = String(item || "").trim();
    if (!s || seen[s]) return;
    seen[s] = true;
    out.push(s);
  });
  return out;
}

/** 默认 tool 目录（与后端 CATALOG_TOOLS 对齐；后端 public_view 会覆盖）。 */
function _aiDefaultToolCatalog() {
  return [
    {
      name: "propose_pending_delete",
      label_zh: "申请加入待删除",
      label_en: "Propose pending delete",
      desc_zh: "向软件申请把路径加入待删除列表，须你勾选确认后才会入队",
      desc_en: "Ask the app to add paths to pending delete; you still confirm first",
    },
  ];
}

function _aiDefaultEnabledTools() {
  return _aiDefaultToolCatalog().map((t) => t.name);
}

function _aiNormalizeToolCatalog(raw) {
  const fallback = _aiDefaultToolCatalog();
  if (!Array.isArray(raw) || !raw.length) return fallback;
  const out = [];
  const seen = Object.create(null);
  raw.forEach((item) => {
    if (!item || typeof item !== "object") return;
    const name = String(item.name || "").trim();
    if (!name || seen[name]) return;
    seen[name] = true;
    out.push({
      name,
      label_zh: String(item.label_zh || name),
      label_en: String(item.label_en || name),
      desc_zh: String(item.desc_zh || ""),
      desc_en: String(item.desc_en || ""),
    });
  });
  return out.length ? out : fallback;
}

/** 收成合法已启用 tool 名；兼容旧 tools_enabled 布尔。 */
function _aiNormalizeEnabledTools(raw, legacyToolsEnabled) {
  const allowed = Object.create(null);
  _aiNormalizeToolCatalog(
    (_ai.draft && _ai.draft.tool_catalog) || _aiDefaultToolCatalog()
  ).forEach((t) => {
    allowed[t.name] = true;
  });
  if (raw == null) {
    if (legacyToolsEnabled === false) return [];
    return Object.keys(allowed);
  }
  let items = [];
  if (typeof raw === "string") {
    items = raw.replace(/;/g, ",").split(",");
  } else if (Array.isArray(raw)) {
    items = raw;
  } else {
    return [];
  }
  const out = [];
  const seen = Object.create(null);
  items.forEach((item) => {
    const name = String(item || "").trim();
    if (!name || !allowed[name] || seen[name]) return;
    seen[name] = true;
    out.push(name);
  });
  return out;
}

function _aiToolLabel(tool) {
  if (!tool) return "";
  if (tool.name === "propose_pending_delete") {
    const s = t("aiToolProposePending");
    if (s && s !== "aiToolProposePending") return s;
  }
  const zh = typeof LANG !== "undefined" && LANG === "zh";
  return zh ? tool.label_zh || tool.name : tool.label_en || tool.name;
}

function _aiToolDesc(tool) {
  if (!tool) return "";
  if (tool.name === "propose_pending_delete") {
    const s = t("aiToolProposePendingDesc");
    if (s && s !== "aiToolProposePendingDesc") return s;
  }
  const zh = typeof LANG !== "undefined" && LANG === "zh";
  return zh ? tool.desc_zh || "" : tool.desc_en || "";
}

function updateAiToolsSettingsSummary() {
  const el = $("#aiToolsSettingsSummary");
  if (!el) return;
  const n = (
    (_ai.draft && Array.isArray(_ai.draft.enabled_tools)
      ? _ai.draft.enabled_tools
      : []) || []
  ).length;
  if (!n) {
    el.textContent = t("aiToolsSettingsNone");
    return;
  }
  el.textContent = t("aiToolsSettingsSummary", n);
}

function closeAiToolsSettingsDialog() {
  const ov = $("#aiToolsSettingsOverlay");
  if (ov) ov.classList.add("hidden");
}

function openAiToolsSettingsDialog() {
  if (!_ai.draft) return;
  const catalog = _aiNormalizeToolCatalog(_ai.draft.tool_catalog);
  _ai.draft.tool_catalog = catalog;
  const enabled = new Set(
    _aiNormalizeEnabledTools(_ai.draft.enabled_tools)
  );
  const list = $("#aiToolsSettingsList");
  if (!list) return;
  list.innerHTML = "";
  catalog.forEach((tool) => {
    const lab = document.createElement("label");
    lab.className = "ai-tools-settings-item";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.value = tool.name;
    chk.checked = enabled.has(tool.name);
    chk.setAttribute("data-tool-name", tool.name);
    const body = document.createElement("span");
    body.className = "ai-tools-settings-body";
    const title = document.createElement("span");
    title.className = "ai-tools-settings-title";
    title.textContent = _aiToolLabel(tool);
    const desc = document.createElement("span");
    desc.className = "ai-tools-settings-desc";
    desc.textContent = _aiToolDesc(tool);
    body.appendChild(title);
    if (desc.textContent) body.appendChild(desc);
    lab.appendChild(chk);
    lab.appendChild(body);
    list.appendChild(lab);
  });
  const ov = $("#aiToolsSettingsOverlay");
  if (ov) ov.classList.remove("hidden");
}

function applyAiToolsSettingsDialog() {
  if (!_ai.draft) {
    closeAiToolsSettingsDialog();
    return;
  }
  const list = $("#aiToolsSettingsList");
  const names = [];
  if (list) {
    list.querySelectorAll('input[type="checkbox"][data-tool-name]').forEach((chk) => {
      if (chk.checked) names.push(String(chk.getAttribute("data-tool-name") || "").trim());
    });
  }
  _ai.draft.enabled_tools = _aiNormalizeEnabledTools(names);
  updateAiToolsSettingsSummary();
  closeAiToolsSettingsDialog();
}

/** 填充单个 select 的模型选项（空选项 = 未选）。 */
function _fillOneModelSelect(sel, opts, selectedModel, emptyLabel) {
  if (!sel) return;
  const model = String(selectedModel || "").trim();
  sel.innerHTML = "";
  const first = document.createElement("option");
  first.value = "";
  first.textContent = emptyLabel;
  sel.appendChild(first);
  opts.forEach((id) => {
    const op = document.createElement("option");
    op.value = id;
    op.textContent = id;
    sel.appendChild(op);
  });
  // 当前模型不在列表里时临时追加，避免侧栏/设置显示成空
  if (model && opts.indexOf(model) < 0) {
    const extra = document.createElement("option");
    extra.value = model;
    extra.textContent = model;
    sel.appendChild(extra);
    sel.value = model;
  } else if (model && opts.indexOf(model) >= 0) {
    sel.value = model;
  } else {
    sel.value = "";
  }
}

/** 填充设置页下拉 + 侧栏模型选择。输入框仅用于「填入」，不回填当前模型。 */
function fillAiModelOptions(options, selectedModel) {
  const opts = _aiNormalizeModelOptions(options);
  _ai.modelOptions = opts;
  if (_ai.draft) _ai.draft.model_options = opts.slice();

  const model = String(selectedModel || "").trim();
  if (_ai.draft && model) _ai.draft.model = model;
  const emptyLabel = t("aiModelNone");
  _fillOneModelSelect($("#aiModelSelect"), opts, model, emptyLabel);
  _fillOneModelSelect($("#aiPanelModelSelect"), opts, model, emptyLabel);
}

function _aiSettingsOpen() {
  const ov = $("#settingsOverlay");
  return !!(ov && !ov.classList.contains("hidden"));
}

/** 切换当前模型：设置页只改草稿；侧栏对已保存配置即时写 model。 */
async function setAiModelQuick(modelId) {
  const model = String(modelId || "").trim();
  if (!model) return { ok: false };
  if (_ai.draft) _ai.draft.model = model;
  fillAiModelOptions(_ai.modelOptions || [], model);
  // 设置页打开时不落盘，等「完成」
  if (_aiSettingsOpen()) return { ok: true };
  if (!isAiAvailable()) return { ok: false };
  const res = await callModule("ai", "set_config", { model: model });
  if (res && res.error) {
    toast(res.error, true);
    return res;
  }
  if (res && typeof res.model === "string" && _ai.draft) {
    _ai.draft.model = res.model;
  }
  return res || { ok: true };
}

/** 把输入框中的模型名加入草稿列表并设为当前模型（不写盘）。 */
function addAiModelFromInput() {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  const input = $("#aiModel");
  const name = String((input && input.value) || "").trim();
  if (!name) {
    toast(t("aiAddModelEmpty"), true);
    return;
  }
  const opts = _aiNormalizeModelOptions(
    (_ai.draft && _ai.draft.model_options) || _ai.modelOptions || []
  );
  if (opts.indexOf(name) < 0) opts.unshift(name);
  if (_ai.draft) {
    _ai.draft.model = name;
    _ai.draft.model_options = opts.slice();
  }
  _ai.modelOptions = opts.slice();
  fillAiModelOptions(opts, name);
  if (input) input.value = "";
}

/** 从草稿列表删除当前选中模型（不写盘）。 */
function removeAiModelSelected() {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  const sel = $("#aiModelSelect");
  const name = String((sel && sel.value) || "").trim();
  if (!name) {
    toast(t("aiRemoveModelEmpty"), true);
    return;
  }
  const opts = _aiNormalizeModelOptions(
    (_ai.draft && _ai.draft.model_options) || _ai.modelOptions || []
  ).filter((x) => x !== name);
  let nextModel = (_ai.draft && _ai.draft.model) || "";
  if (nextModel === name) nextModel = opts[0] || "";
  if (_ai.draft) {
    _ai.draft.model = nextModel;
    _ai.draft.model_options = opts.slice();
  }
  _ai.modelOptions = opts.slice();
  fillAiModelOptions(opts, nextModel);
}

/** 从侧栏打开设置并切到 AI 页。 */
async function openAiSettingsFromPanel() {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  if (typeof openSettings === "function") {
    await openSettings("ai");
  }
}

function aiBaseUrlNeedsV1Hint(baseUrl) {
  // OpenAI 兼容：base 通常以 /v1 结尾；/models 等是路径不是 base
  const url = String(baseUrl || "").trim().replace(/\/+$/, "");
  if (!url) return false;
  return !url.toLowerCase().endsWith("/v1");
}

function updateAiBaseUrlHint(value) {
  const hint = $("#aiBaseUrlHint");
  if (!hint) return;
  const show = aiBaseUrlNeedsV1Hint(
    value != null ? value : ($("#aiBaseUrl") && $("#aiBaseUrl").value) || ""
  );
  hint.classList.toggle("hidden", !show);
  if (show) hint.removeAttribute("hidden");
  else hint.setAttribute("hidden", "");
}

function fillAiSettingsForm() {
  const d = _ai.draft;
  if (!d) return;
  const en = $("#aiEnabledChk");
  if (en) en.checked = !!d.enabled;
  const url = $("#aiBaseUrl");
  if (url) url.value = d.base_url || "";
  updateAiBaseUrlHint(d.base_url || "");
  // 输入框仅用于新增，不回填当前模型名
  const modelInput = $("#aiModel");
  if (modelInput) modelInput.value = "";
  fillAiModelOptions(d.model_options || _ai.modelOptions || [], d.model || "");
  const key = $("#aiApiKey");
  if (key) {
    key.value = "";
    key.placeholder = d.has_key ? t("aiKeySaved") : t("aiKeyPlaceholder");
  }
  const extra = $("#aiExtraPrompt");
  if (extra) extra.value = d.extra_prompt || "";
  d.tool_catalog = _aiNormalizeToolCatalog(d.tool_catalog);
  d.enabled_tools = _aiNormalizeEnabledTools(d.enabled_tools);
  updateAiToolsSettingsSummary();
  const depthEl = $("#aiCleanupMaxDepth");
  if (depthEl) {
    const n = parseInt(d.cleanup_max_depth, 10);
    depthEl.value = String(isNaN(n) ? 3 : Math.max(1, Math.min(8, n)));
  }
}

function readAiSettingsFormToDraft() {
  if (!_ai.draft) return;
  const en = $("#aiEnabledChk");
  if (en) _ai.draft.enabled = !!en.checked;
  const url = $("#aiBaseUrl");
  if (url) _ai.draft.base_url = String(url.value || "").trim();
  // 当前模型以选择框为准，输入框只在点「填入」时生效
  const modelSel = $("#aiModelSelect");
  if (modelSel) _ai.draft.model = String(modelSel.value || "").trim();
  const key = $("#aiApiKey");
  if (key) {
    const v = String(key.value || "");
    if (v.trim()) {
      _ai.draft.api_key = v.trim();
      _ai.draft.key_dirty = true;
    }
  }
  const extra = $("#aiExtraPrompt");
  if (extra) _ai.draft.extra_prompt = String(extra.value || "");
  // enabled_tools 由二级对话框写入草稿，此处只做归一
  _ai.draft.enabled_tools = _aiNormalizeEnabledTools(_ai.draft.enabled_tools);
  const depthEl = $("#aiCleanupMaxDepth");
  if (depthEl) {
    let n = parseInt(depthEl.value, 10);
    if (isNaN(n)) n = 3;
    n = Math.max(1, Math.min(8, n));
    _ai.draft.cleanup_max_depth = n;
    depthEl.value = String(n);
  }
  _ai.draft.model_options = _aiNormalizeModelOptions(
    _ai.draft.model_options || _ai.modelOptions || []
  );
}

async function applyAiSettingsDraft() {
  if (!isAiAvailable() || !_ai.draft) return { ok: true };
  readAiSettingsFormToDraft();
  const d = _ai.draft;
  let depth = parseInt(d.cleanup_max_depth, 10);
  if (isNaN(depth)) depth = 3;
  depth = Math.max(1, Math.min(8, depth));
  const payload = {
    enabled: !!d.enabled,
    base_url: d.base_url || "",
    model: d.model || "",
    extra_prompt: d.extra_prompt || "",
    model_options: _aiNormalizeModelOptions(d.model_options || _ai.modelOptions || []),
    cleanup_max_depth: depth,
    enabled_tools: _aiNormalizeEnabledTools(d.enabled_tools),
  };
  if (d.key_dirty && d.api_key) {
    payload.api_key = d.api_key;
  }
  const res = await callModule("ai", "set_config", payload);
  if (res && res.error) return res;
  _ai.draft.key_dirty = false;
  _ai.draft.api_key = "";
  _ai.draft.has_key = !!(res && res.has_key) || _ai.draft.has_key;
  if (res && Array.isArray(res.model_options)) {
    _ai.draft.model_options = _aiNormalizeModelOptions(res.model_options);
    _ai.modelOptions = _ai.draft.model_options.slice();
  }
  // 保存后清掉输入框里的明文 key，placeholder 改为已保存
  const keyEl = $("#aiApiKey");
  if (keyEl) {
    keyEl.value = "";
    keyEl.placeholder = _ai.draft.has_key ? t("aiKeySaved") : t("aiKeyPlaceholder");
  }
  _ai.enabled = !!d.enabled;
  if (res && typeof res.enabled === "boolean") _ai.enabled = !!res.enabled;
  if (res && typeof res.model === "string") {
    _ai.draft.model = res.model;
  }
  if (res && res.cleanup_max_depth != null) {
    const n = parseInt(res.cleanup_max_depth, 10);
    if (!isNaN(n)) _ai.draft.cleanup_max_depth = Math.max(1, Math.min(8, n));
  }
  if (res && Array.isArray(res.enabled_tools)) {
    _ai.draft.enabled_tools = _aiNormalizeEnabledTools(res.enabled_tools);
  }
  if (res && Array.isArray(res.tool_catalog)) {
    _ai.draft.tool_catalog = _aiNormalizeToolCatalog(res.tool_catalog);
  }
  updateAiToolsSettingsSummary();
  fillAiModelOptions(_ai.modelOptions, (_ai.draft && _ai.draft.model) || "");
  refreshAiSideEntry();
  return res || { ok: true };
}

function _aiToastError(resOrMsg) {
  let msg = "";
  if (resOrMsg == null) msg = t("aiRequestFailed");
  else if (typeof resOrMsg === "string") msg = resOrMsg;
  else msg = resOrMsg.error || resOrMsg.message || t("aiRequestFailed");
  toast(msg, true);
}

/** 重建模型下拉并失焦，避免仍展开旧列表。 */
function _aiRefreshModelSelects(models, current) {
  fillAiModelOptions(models, current);
  ["#aiModelSelect", "#aiPanelModelSelect"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    try {
      el.blur();
    } catch (e) {}
  });
}

async function fetchAiModels() {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  // 设置页打开时读表单；侧栏直接用当前草稿/已存配置
  const settingsOpen =
    $("#settingsOverlay") &&
    !$("#settingsOverlay").classList.contains("hidden");
  if (settingsOpen) readAiSettingsFormToDraft();
  const d = _ai.draft || {};
  const payload = {
    base_url: d.base_url || "",
  };
  if (d.key_dirty && d.api_key) payload.api_key = d.api_key;
  const btns = [$("#aiFetchModelsBtn"), $("#aiPanelFetchModelsBtn")].filter(
    Boolean
  );
  btns.forEach((b) => {
    b.disabled = true;
  });
  try {
    const res = await callModule("ai", "list_models", payload);
    if (res && res.error) {
      _aiToastError(res);
      return;
    }
    const models = _aiNormalizeModelOptions(
      (res && (res.models || res.model_options)) || []
    );
    let current = (d.model || "").trim();
    // 当前模型为空时自动选第一项
    if (!current && models[0]) current = models[0];
    // 只更新草稿；点「完成」再写 settings.yaml
    if (_ai.draft) {
      _ai.draft.model = current;
      _ai.draft.model_options = models.slice();
    }
    _ai.modelOptions = models.slice();
    _aiRefreshModelSelects(models, current);
    if (!models.length) {
      toast(t("aiFetchModelsEmpty"));
      return;
    }
    toast(t("aiFetchModelsOk", models.length));
  } catch (e) {
    _aiToastError(String(e && e.message ? e.message : e));
  } finally {
    btns.forEach((b) => {
      b.disabled = false;
    });
  }
}

async function testAiConnection() {
  if (!isAiAvailable()) {
    toast(t("aiModuleMissing"), true);
    return;
  }
  readAiSettingsFormToDraft();
  const d = _ai.draft || {};
  // 用草稿测连，不写盘；点「完成」才持久化
  const payload = {
    base_url: d.base_url || "",
    model: d.model || "",
  };
  if (d.key_dirty && d.api_key) payload.api_key = d.api_key;
  const btn = $("#aiTestBtn");
  if (btn) btn.disabled = true;
  try {
    const res = await callModule("ai", "test_connection", payload);
    if (res && res.error) {
      _aiToastError(res);
      return;
    }
    const reply = String((res && res.preview) || "").trim();
    if (reply) toast(t("aiTestOkWithReply", reply));
    else toast(t("aiTestEmptyReply"));
  } catch (e) {
    _aiToastError(String(e && e.message ? e.message : e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

function wireAiUi() {
  const newChatBtn = $("#aiNewChatBtn");
  if (newChatBtn) newChatBtn.onclick = () => clearAiChat();
  const exportChatBtn = $("#aiExportChatBtn");
  if (exportChatBtn) exportChatBtn.onclick = () => exportAiChat();
  const importChatBtn = $("#aiImportChatBtn");
  if (importChatBtn) importChatBtn.onclick = () => importAiChat();
  const settingsBtn = $("#aiPanelSettingsBtn");
  if (settingsBtn) settingsBtn.onclick = () => openAiSettingsFromPanel();
  const clearCtxBtn = $("#aiClearCtxBtn");
  if (clearCtxBtn) clearCtxBtn.onclick = () => clearAiContextOnly();
  const ctxChip = $("#aiContextChip");
  if (ctxChip) ctxChip.onclick = () => toggleAiContextDetail();
  const cleanupContinueBtn = $("#aiCleanupContinueBtn");
  if (cleanupContinueBtn) cleanupContinueBtn.onclick = () => continueCompareCleanup();
  wireDiskCleanupUi();
  const sendBtn = $("#aiSendBtn");
  if (sendBtn) sendBtn.onclick = () => sendAiFollowup();
  const stopBtn = $("#aiStopBtn");
  if (stopBtn) stopBtn.onclick = () => stopAiRequest();
  const input = $("#aiInput");
  if (input) {
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendAiFollowup();
      }
    });
  }
  const testBtn = $("#aiTestBtn");
  if (testBtn) testBtn.onclick = () => testAiConnection();
  const baseUrlInput = $("#aiBaseUrl");
  if (baseUrlInput) {
    const onBaseUrlChange = () => {
      const v = String(baseUrlInput.value || "").trim();
      if (_ai.draft) _ai.draft.base_url = v;
      updateAiBaseUrlHint(v);
    };
    baseUrlInput.addEventListener("input", onBaseUrlChange);
    baseUrlInput.addEventListener("change", onBaseUrlChange);
    baseUrlInput.addEventListener("blur", onBaseUrlChange);
  }
  const fetchBtn = $("#aiFetchModelsBtn");
  if (fetchBtn) fetchBtn.onclick = () => fetchAiModels();
  const panelFetchBtn = $("#aiPanelFetchModelsBtn");
  if (panelFetchBtn) panelFetchBtn.onclick = () => fetchAiModels();
  const addModelBtn = $("#aiAddModelBtn");
  if (addModelBtn) addModelBtn.onclick = () => addAiModelFromInput();
  const removeModelBtn = $("#aiRemoveModelBtn");
  if (removeModelBtn) removeModelBtn.onclick = () => removeAiModelSelected();
  const modelSel = $("#aiModelSelect");
  if (modelSel) {
    modelSel.addEventListener("change", () => {
      const v = String(modelSel.value || "").trim();
      if (_ai.draft) _ai.draft.model = v;
      // 设置页只改草稿，不写盘
      fillAiModelOptions(_ai.modelOptions || [], v);
    });
  }
  const panelModelSel = $("#aiPanelModelSelect");
  if (panelModelSel) {
    panelModelSel.addEventListener("change", () => {
      const v = String(panelModelSel.value || "").trim();
      if (!v) {
        if (_ai.draft) _ai.draft.model = "";
        fillAiModelOptions(_ai.modelOptions || [], "");
        // 侧栏清空当前模型：仅当设置未打开时写盘
        if (!_aiSettingsOpen() && isAiAvailable()) {
          callModule("ai", "set_config", { model: "" }).catch(() => {});
        }
        return;
      }
      setAiModelQuick(v);
    });
  }
  const modelInput = $("#aiModel");
  if (modelInput) {
    modelInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        addAiModelFromInput();
      }
    });
  }
  const toolsSettingsBtn = $("#aiToolsSettingsBtn");
  if (toolsSettingsBtn) toolsSettingsBtn.onclick = () => openAiToolsSettingsDialog();
  const toolsOkBtn = $("#aiToolsSettingsOkBtn");
  if (toolsOkBtn) toolsOkBtn.onclick = () => applyAiToolsSettingsDialog();
  const toolsCancelBtn = $("#aiToolsSettingsCancelBtn");
  if (toolsCancelBtn) toolsCancelBtn.onclick = () => closeAiToolsSettingsDialog();
  const toolsCloseBtn = $("#aiToolsSettingsCloseBtn");
  if (toolsCloseBtn) toolsCloseBtn.onclick = () => closeAiToolsSettingsDialog();
  const toolsOv = $("#aiToolsSettingsOverlay");
  if (toolsOv) {
    toolsOv.addEventListener("click", (e) => {
      if (e.target === toolsOv) closeAiToolsSettingsDialog();
    });
  }
  syncAiRailState();
}

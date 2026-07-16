/* 右侧工具侧栏 + 待删除列表（会话内，不写 yaml） */
"use strict";

/**
 * items[].result: null | { status: 'ok'|'missing'|'blacklist'|'blocked'|'fail', message?: string }
 * @type {{ open: boolean, tab: string, items: Array, permanent: boolean, executing: boolean }}
 */
const _tool = {
  open: false,
  tab: "pending",
  items: [],
  permanent: false,
  executing: false,
};

let _pendingIdSeq = 1;

function isToolOpen() {
  return !!_tool.open;
}

/** 侧栏面板目标宽度（与 CSS .tool-panel 一致），用于扩展主窗口 */
const _TOOL_PANEL_W_MIN = 240;
const _TOOL_PANEL_W_MAX = 640;
const _TOOL_PANEL_W_DEFAULT = 340;
const _TOOL_PANEL_W_KEY = "wsmc.toolPanelWidth";

function toolPanelWidthPx() {
  const panel = $("#toolPanel");
  if (panel) {
    const applied = parseInt(
      getComputedStyle(document.documentElement).getPropertyValue(
        "--tool-panel-w"
      ),
      10
    );
    if (Number.isFinite(applied) && applied > 0) return applied;
    // 隐藏时 offsetWidth 为 0，用计算样式
    const cs = window.getComputedStyle(panel);
    const w = parseFloat(cs.width);
    if (Number.isFinite(w) && w > 0) return Math.round(w);
  }
  return _TOOL_PANEL_W_DEFAULT;
}

function applyToolPanelWidth(px) {
  const panel = $("#toolPanel");
  let w = Math.round(Number(px));
  if (!Number.isFinite(w)) w = _TOOL_PANEL_W_DEFAULT;
  w = Math.max(_TOOL_PANEL_W_MIN, Math.min(_TOOL_PANEL_W_MAX, w));
  document.documentElement.style.setProperty("--tool-panel-w", w + "px");
  if (panel) panel.style.width = w + "px";
  return w;
}

function restoreToolPanelWidth() {
  try {
    const raw = localStorage.getItem(_TOOL_PANEL_W_KEY);
    if (raw != null && raw !== "") {
      applyToolPanelWidth(raw);
      return;
    }
  } catch (e) {}
  applyToolPanelWidth(_TOOL_PANEL_W_DEFAULT);
}

function syncToolPanelResizerVisibility() {
  const handle = $("#toolPanelResizer");
  if (!handle) return;
  // 仅侧栏展开时显示分界拖条
  handle.classList.toggle("hidden", !_tool.open);
}

/** 右侧工具栏左缘拖拽：变宽挤压中间对比区，不改窗口尺寸 */
function wireToolPanelResizer() {
  const handle = $("#toolPanelResizer");
  const panel = $("#toolPanel");
  if (!handle || !panel) return;

  restoreToolPanelWidth();
  syncToolPanelResizerVisibility();

  let dragging = false;
  let startX = 0;
  let startW = 0;
  let raf = 0;
  let pendingW = 0;

  const flush = () => {
    raf = 0;
    if (!dragging) return;
    applyToolPanelWidth(pendingW);
  };

  const onMove = (e) => {
    if (!dragging) return;
    // 手柄在面板左侧：向左拖 = 变宽
    pendingW = startW + (startX - e.clientX);
    if (!raf) raf = requestAnimationFrame(flush);
  };

  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("is-tool-panel-resizing");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
    const w = applyToolPanelWidth(pendingW || startW);
    try {
      localStorage.setItem(_TOOL_PANEL_W_KEY, String(w));
    } catch (e) {}
  };

  handle.addEventListener("pointerdown", (e) => {
    if (!_tool.open) return;
    if (e.button != null && e.button !== 0) return;
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    startW = toolPanelWidthPx();
    pendingW = startW;
    document.body.classList.add("is-tool-panel-resizing");
    try {
      handle.setPointerCapture(e.pointerId);
    } catch (err) {}
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  });
}

/** 串行化窗口尺寸变更，避免快切时 open/close 交叉导致布局闪烁 */
let _toolWinChain = Promise.resolve();
let _toolWinToken = 0;

function syncWindowForToolPanel(open) {
  const api = state && state.api;
  if (!api || typeof api.set_tool_panel_open !== "function") {
    return Promise.resolve(null);
  }
  const w = toolPanelWidthPx();
  return Promise.resolve(api.set_tool_panel_open(!!open, w)).catch(() => null);
}

/**
 * 过渡期间把主区钉在屏幕原位（position:fixed + 流内占位），
 * 避免窗口/侧栏几何变化时 flex 主区瞬宽，空态「磁盘空间对比」重居中闪一下。
 */
let _mainLockCount = 0;
let _mainLockPlaceholder = null;
let _mainLockPrevStyle = "";
let _mainLockSize = null; // { w, h }

function lockMainLayout() {
  const main = document.querySelector(".main");
  if (!main) return false;
  if (_mainLockCount === 0) {
    const r = main.getBoundingClientRect();
    const w = Math.round(r.width);
    const h = Math.round(r.height);
    if (!(w > 0) || !(h > 0)) return false;
    _mainLockPrevStyle = main.getAttribute("style") || "";
    _mainLockSize = { w, h };
    const ph = document.createElement("div");
    ph.id = "mainLayoutPlaceholder";
    ph.setAttribute("aria-hidden", "true");
    ph.style.cssText = [
      `flex:0 0 ${w}px`,
      `width:${w}px`,
      `min-width:${w}px`,
      `max-width:${w}px`,
      `height:${h}px`,
      "align-self:stretch",
      "visibility:hidden",
      "pointer-events:none",
      "overflow:hidden",
    ].join(";");
    main.parentNode.insertBefore(ph, main);
    _mainLockPlaceholder = ph;
    // fixed 钉在视口原位；窗口/侧栏变化时主区内容不再跟着重算
    main.style.setProperty("position", "fixed", "important");
    main.style.setProperty("left", `${Math.round(r.left)}px`, "important");
    main.style.setProperty("top", `${Math.round(r.top)}px`, "important");
    main.style.setProperty("width", `${w}px`, "important");
    main.style.setProperty("height", `${h}px`, "important");
    main.style.setProperty("right", "auto", "important");
    main.style.setProperty("bottom", "auto", "important");
    main.style.setProperty("margin", "0", "important");
    main.style.setProperty("z-index", "4", "important");
    main.style.setProperty("flex", "none", "important");
    main.style.setProperty("max-width", "none", "important");
    main.style.setProperty("min-width", "0", "important");
    main.style.setProperty("box-sizing", "border-box", "important");
  }
  _mainLockCount += 1;
  return true;
}

function unlockMainLayout() {
  if (_mainLockCount <= 0) {
    _mainLockCount = 0;
    return;
  }
  _mainLockCount -= 1;
  if (_mainLockCount > 0) return;
  const main = document.querySelector(".main");
  const size = _mainLockSize;
  // 先按占位尺寸回到文档流（仍固定宽），再卸占位，最后一帧清回 flex:1，
  // 避免 removeAttribute 瞬间主区被撑满再压回
  if (main && size) {
    main.style.cssText = [
      `flex: 0 0 ${size.w}px`,
      `width: ${size.w}px`,
      `min-width: ${size.w}px`,
      `max-width: ${size.w}px`,
      "position: relative",
      "left: auto",
      "top: auto",
      "right: auto",
      "bottom: auto",
      "z-index: auto",
      "margin: 0",
      "box-sizing: border-box",
    ].join("; ");
  } else if (main) {
    if (_mainLockPrevStyle) main.setAttribute("style", _mainLockPrevStyle);
    else main.removeAttribute("style");
  }
  if (_mainLockPlaceholder && _mainLockPlaceholder.parentNode) {
    _mainLockPlaceholder.parentNode.removeChild(_mainLockPlaceholder);
  }
  _mainLockPlaceholder = null;
  _mainLockPrevStyle = "";
  _mainLockSize = null;
  // 下一帧再恢复默认 flex，几何应已与收起后一致
  requestAnimationFrame(() => {
    if (_mainLockCount > 0) return;
    const el = document.querySelector(".main");
    if (!el) return;
    el.style.flex = "";
    el.style.width = "";
    el.style.minWidth = "";
    el.style.maxWidth = "";
    el.style.position = "";
    el.style.left = "";
    el.style.top = "";
    el.style.right = "";
    el.style.bottom = "";
    el.style.zIndex = "";
    el.style.margin = "";
    el.style.boxSizing = "";
  });
}

function afterPaint() {
  return new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  });
}

/** 等 WebView 内宽稳定，避免 resize 回包后内容区尚未跟上就显隐侧栏 */
function waitInnerWidthStable(timeoutMs) {
  const limit = typeof timeoutMs === "number" ? timeoutMs : 280;
  return new Promise((resolve) => {
    let last = window.innerWidth;
    let same = 0;
    const t0 = performance.now();
    const tick = () => {
      const w = window.innerWidth;
      if (w === last) same += 1;
      else {
        same = 0;
        last = w;
      }
      if (same >= 2 || performance.now() - t0 > limit) {
        resolve(w);
        return;
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}

/**
 * 打开侧栏：钉主区 → 加宽窗口 → 显示面板 → 解锁。
 * （若不先钉住，窗口先变宽时主区被 flex 撑开，空态会闪到更宽区域的中心）
 * 快切时以最新目标为准（token 丢弃过期结果）。
 */
function openToolPanel(tab) {
  const panel = $("#toolPanel");
  if (!panel) return;
  const wantTab =
    typeof tab === "string" && tab ? tab : (_tool.tab || "pending");
  // 仅切页签、侧栏已开：不改窗口
  if (_tool.open && !panel.classList.contains("hidden")) {
    switchToolTab(wantTab);
    syncToolRailState();
    return;
  }
  _tool.open = true;
  _tool.tab = wantTab;
  // 先更新 rail 状态（箭头/badge），面板仍 hidden 直到窗口加宽完成
  syncToolRailState();
  const token = ++_toolWinToken;
  _toolWinChain = _toolWinChain
    .then(async () => {
      if (token !== _toolWinToken || !_tool.open) return;
      const locked = lockMainLayout();
      try {
        await syncWindowForToolPanel(true);
        await waitInnerWidthStable(240);
        if (token !== _toolWinToken) return;
        if (!_tool.open) {
          panel.classList.add("hidden");
          return;
        }
        panel.classList.remove("hidden");
        switchToolTab(_tool.tab || wantTab);
        syncToolRailState();
        await afterPaint();
      } finally {
        if (locked) unlockMainLayout();
      }
    })
    .catch(() => {
      if (!_tool.open) panel.classList.add("hidden");
    });
}

function closeToolPanel() {
  const panel = $("#toolPanel");
  // 已关且面板已藏：无需再动
  if (!_tool.open && (!panel || panel.classList.contains("hidden"))) {
    return;
  }
  // 逻辑上先标关闭；.open 只影响箭头样式，不再改 dock 占宽
  _tool.open = false;
  syncToolRailState();
  // 关闭侧栏时若 AI 在流式回复，停止（与原先 closeAiPanel 一致）
  if (typeof stopAiRequest === "function" && typeof _ai !== "undefined" && _ai.streaming && _ai.requestId) {
    stopAiRequest();
  }
  if (typeof _ai !== "undefined") _ai.open = false;
  const token = ++_toolWinToken;
  _toolWinChain = _toolWinChain
    .then(async () => {
      if (token !== _toolWinToken) return;
      // 等待期间又被打开：不缩窗、不藏面板（open 会接管）
      if (_tool.open) return;
      // 收起：先钉主区 → 再藏面板 → 再缩窗 → 解锁
      // （若先藏面板/先去占宽，主区会瞬间变宽，空态居中闪一下）
      const locked = lockMainLayout();
      try {
        if (panel) panel.classList.add("hidden");
        await afterPaint();
        if (token !== _toolWinToken || _tool.open) return;
        await syncWindowForToolPanel(false);
        await waitInnerWidthStable(240);
        await afterPaint();
      } finally {
        if (locked) unlockMainLayout();
      }
    })
    .catch(() => {
      if (panel && !_tool.open) panel.classList.add("hidden");
    });
}

function toggleToolPanel() {
  if (_tool.open) closeToolPanel();
  else openToolPanel(_tool.tab || "pending");
}

function syncToolRailState() {
  const dock = $("#toolDock");
  const rail = $("#toolRailToggle");
  if (dock) dock.classList.toggle("open", !!_tool.open);
  if (rail) {
    rail.setAttribute("aria-expanded", _tool.open ? "true" : "false");
    // 不设 title，避免展开三角悬停冒泡提示
    rail.removeAttribute("title");
    rail.removeAttribute("data-i18n-title");
  }
  syncToolPanelResizerVisibility();
  updatePendingBadge();
}

/** 有 AI 模块时显示 AI 页签；无则只保留待删除。 */
function refreshToolTabsVisibility() {
  if (typeof applyModuleVisibility === "function") applyModuleVisibility();
  const aiOn = typeof hasModule === "function" && hasModule("ai");
  if (!aiOn && _tool.tab === "ai") {
    switchToolTab("pending");
  }
  // 无 AI 时隐藏页签栏的 AI 按钮已由 module-off 处理；仅一项时弱化 tab 外观
  const tabs = document.querySelector(".tool-tabs");
  if (tabs) tabs.classList.toggle("tool-tabs-single", !aiOn);
  syncToolRailState();
}

function switchToolTab(tabId) {
  let tab = (tabId || "pending").trim();
  if (tab === "ai" && typeof hasModule === "function" && !hasModule("ai")) {
    tab = "pending";
  }
  _tool.tab = tab;
  document.querySelectorAll(".tool-tab").forEach((btn) => {
    const on = btn.getAttribute("data-tool-tab") === tab;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".tool-pane").forEach((pane) => {
    const on = pane.getAttribute("data-tool-pane") === tab;
    pane.classList.toggle("active", on);
    pane.classList.toggle("hidden", !on);
  });
  if (typeof _ai !== "undefined") {
    _ai.open = !!(tab === "ai" && _tool.open);
  }
  if (tab === "ai" && typeof _aiEnsureMarkdown === "function") {
    _aiEnsureMarkdown();
  }
  if (tab === "ai" && typeof updateAiContextBar === "function") {
    updateAiContextBar();
  }
  if (tab === "pending") {
    renderPendingList();
  }
}

/** 供 AI 模块复用：打开侧栏并切到 AI。 */
function openAiPanel() {
  if (typeof isAiAvailable === "function" && !isAiAvailable()) return;
  openToolPanel("ai");
  const input = $("#aiInput");
  if (input) {
    try {
      input.focus();
    } catch (e) {}
  }
}

function closeAiPanel() {
  // 仅切回待删除，不强制关整个侧栏（用户可能还要看队列）
  if (_tool.open) switchToolTab("pending");
  else closeToolPanel();
  if (typeof _ai !== "undefined" && _ai.streaming && _ai.requestId && typeof stopAiRequest === "function") {
    // 若仍在 AI 流且用户切走，不自动 stop；仅关闭整栏时 stop
  }
}

function toggleAiPanel() {
  if (_tool.open && _tool.tab === "ai") {
    switchToolTab("pending");
  } else {
    openAiPanel();
  }
}

function syncAiRailState() {
  // 兼容旧调用：统一同步工具栏
  syncToolRailState();
  if (typeof _ai !== "undefined") {
    const rail = $("#toolRailToggle");
    if (rail) {
      rail.classList.toggle(
        "ai-side-disabled",
        typeof isAiAvailable === "function" && isAiAvailable() && !_ai.enabled && _tool.tab === "ai"
      );
    }
  }
}

function refreshAiSideEntry() {
  refreshToolTabsVisibility();
}

function pendingItemKey(root, rel) {
  return `${String(root || "").toLowerCase()}\0${String(rel || "").toLowerCase()}`;
}

/**
 * 右键「加入待删除」：入队并打开待删除页签。
 */
function addCompareNodeToPending(node) {
  if (!state.compareRoot) {
    toast(t("deleteFail"), true);
    return;
  }
  const rel = (node && node.path) || "";
  if (!rel) {
    toast(t("deleteBlockedRoot"), true);
    return;
  }
  const root = state.compareRoot;
  const full = fullPath(root, rel);
  const key = pendingItemKey(root, rel);
  if (_tool.items.some((it) => pendingItemKey(it.root, it.rel) === key)) {
    toast(t("pendingExists"));
    openToolPanel("pending");
    return;
  }
  _tool.items.push({
    id: `p${_pendingIdSeq++}`,
    root,
    rel,
    name: (node && (node.name || node.path)) || rel,
    isDir: !!(node && node.is_dir),
    full,
    result: null,
  });
  renderPendingList();
  openToolPanel("pending");
  toast(t("pendingAdded"));
}

/** 可勾选清单对话框 resolver；同时只允许一个 */
let _pendingChecklistResolver = null;

function closePendingChecklistDialog(result) {
  const ov = $("#pendingChecklistOverlay");
  if (ov) ov.classList.add("hidden");
  const list = $("#pendingChecklistList");
  if (list) list.innerHTML = "";
  const resolve = _pendingChecklistResolver;
  _pendingChecklistResolver = null;
  if (resolve) resolve(result);
}

/**
 * 可勾选路径清单。默认全选；确认返回勾选项数组，取消返回 null。
 * @param {Array<object>} items
 * @returns {Promise<Array<object>|null>}
 */
function showPendingChecklistDialog(items) {
  const list = Array.isArray(items) ? items : [];
  const ov = $("#pendingChecklistOverlay");
  const listEl = $("#pendingChecklistList");
  const titleEl = $("#pendingChecklistTitle");
  const hintEl = $("#pendingChecklistHint");
  if (!ov || !listEl) {
    return Promise.resolve(null);
  }
  if (_pendingChecklistResolver) closePendingChecklistDialog(null);

  if (titleEl) titleEl.textContent = t("pendingChecklistTitle");
  if (hintEl) hintEl.textContent = t("pendingChecklistHint");

  const defaultRoot = (typeof state !== "undefined" && state.compareRoot) || "";
  listEl.innerHTML = "";
  list.forEach((raw, idx) => {
    if (!raw || typeof raw !== "object") return;
    const root = String(raw.root || defaultRoot || "").trim();
    const rel = String(raw.rel || raw.rel_path || "").trim();
    const path = raw.path
      ? String(raw.path)
      : typeof fullPath === "function"
        ? fullPath(root, rel)
        : root && rel
          ? root + "\\" + rel
          : root || rel;
    const name = String(raw.name || rel || path || "").trim() || path;
    const reason = raw.reason != null ? String(raw.reason) : "";
    const row = document.createElement("label");
    row.className = "pending-checklist-item";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = true;
    chk.setAttribute("data-checklist-idx", String(idx));
    const body = document.createElement("div");
    body.className = "pending-checklist-item-body";
    const nameEl = document.createElement("div");
    nameEl.className = "pending-checklist-item-name";
    nameEl.textContent = name;
    const pathEl = document.createElement("div");
    pathEl.className = "pending-checklist-item-path";
    pathEl.textContent = path;
    pathEl.title = path;
    body.appendChild(nameEl);
    body.appendChild(pathEl);
    if (reason) {
      const reasonEl = document.createElement("div");
      reasonEl.className = "pending-checklist-item-reason";
      reasonEl.textContent = reason;
      body.appendChild(reasonEl);
    }
    row.appendChild(chk);
    row.appendChild(body);
    // 把原始项挂在节点上，确认时按勾选收集
    row._pendingItem = {
      root,
      rel,
      rel_path: rel,
      name,
      is_dir: !!raw.is_dir,
      reason,
      path: path || "",
    };
    listEl.appendChild(row);
  });

  ov.classList.remove("hidden");
  return new Promise((resolve) => {
    _pendingChecklistResolver = resolve;
  });
}

function _pendingChecklistCollectChecked() {
  const listEl = $("#pendingChecklistList");
  if (!listEl) return [];
  const out = [];
  listEl.querySelectorAll(".pending-checklist-item").forEach((row) => {
    const chk = row.querySelector('input[type="checkbox"]');
    if (chk && chk.checked && row._pendingItem) {
      out.push(row._pendingItem);
    }
  });
  return out;
}

function _pendingChecklistSetAll(checked) {
  const listEl = $("#pendingChecklistList");
  if (!listEl) return;
  listEl.querySelectorAll('input[type="checkbox"]').forEach((chk) => {
    chk.checked = !!checked;
  });
}

/**
 * AI / 外部提议加入待删除：**必须**用户确认后才入队。
 *
 * 流程：可勾选清单 → check_pending_paths（白名单等）→ 入队。
 * 不执行真删；不静默写入。
 *
 * @param {Array<{root?: string, rel?: string, rel_path?: string, path?: string, name?: string, is_dir?: boolean, reason?: string}>} items
 * @param {{ skipConfirm?: boolean, quiet?: boolean }} [opts]
 *   skipConfirm 仅测试用；quiet 时不 toast（由调用方写聊天提示）
 * @returns {Promise<{ok: boolean, added: number, rejected: number, cancelled?: boolean, skippedDup?: number}>}
 */
async function proposePendingItems(items, opts) {
  const options = opts || {};
  const quiet = !!options.quiet;
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    if (!quiet) toast(t("pendingProposeNone"), true);
    return { ok: false, added: 0, rejected: 0 };
  }
  const defaultRoot = state.compareRoot || "";
  const normalized = [];
  list.forEach((raw) => {
    if (!raw || typeof raw !== "object") return;
    const root = String(raw.root || defaultRoot || "").trim();
    const rel = String(raw.rel || raw.rel_path || "").trim();
    const name = String(raw.name || rel || raw.path || "").trim();
    if (!root && !rel && !raw.path) return;
    normalized.push({
      root,
      rel,
      rel_path: rel,
      name: name || rel || root,
      is_dir: !!raw.is_dir,
      reason: raw.reason != null ? String(raw.reason) : "",
      path: raw.path ? String(raw.path) : "",
    });
  });
  if (!normalized.length) {
    if (!quiet) toast(t("pendingProposeNone"), true);
    return { ok: false, added: 0, rejected: 0 };
  }

  let selected = normalized;
  if (!options.skipConfirm) {
    selected = await showPendingChecklistDialog(normalized);
    if (!selected) {
      return { ok: false, added: 0, rejected: 0, cancelled: true };
    }
    if (!selected.length) {
      if (!quiet) toast(t("pendingChecklistEmpty"), true);
      return { ok: false, added: 0, rejected: 0, cancelled: true };
    }
  }

  let allowed = selected;
  let rejectedCount = 0;
  if (state.api && typeof state.api.check_pending_paths === "function") {
    try {
      const res = await state.api.check_pending_paths(
        selected.map((it) => ({
          root: it.root,
          rel: it.rel,
          name: it.name,
          is_dir: it.is_dir,
          reason: it.reason,
        }))
      );
      if (res && res.error) {
        if (!quiet) toast(res.error, true);
        return { ok: false, added: 0, rejected: selected.length };
      }
      allowed = Array.isArray(res && res.allowed) ? res.allowed : [];
      rejectedCount = Array.isArray(res && res.rejected) ? res.rejected.length : 0;
    } catch (e) {
      if (!quiet) {
        toast(String(e && e.message ? e.message : e) || t("deleteFail"), true);
      }
      return { ok: false, added: 0, rejected: selected.length };
    }
  }

  let added = 0;
  let skippedDup = 0;
  allowed.forEach((row) => {
    const root = String((row && row.root) || defaultRoot || "").trim();
    const rel = String((row && (row.rel || row.rel_path)) || "").trim();
    if (!root && !rel) return;
    const key = pendingItemKey(root, rel);
    if (_tool.items.some((it) => pendingItemKey(it.root, it.rel) === key)) {
      skippedDup += 1;
      return;
    }
    const full =
      (row && row.path) ||
      (typeof fullPath === "function" ? fullPath(root, rel) : root + "\\" + rel);
    _tool.items.push({
      id: `p${_pendingIdSeq++}`,
      root,
      rel,
      name: String((row && row.name) || rel || full),
      isDir: !!(row && row.is_dir),
      full,
      result: null,
    });
    added += 1;
  });

  if (added > 0) {
    renderPendingList();
    openToolPanel("pending");
    if (!quiet) toast(t("pendingProposeAdded", added));
  } else if (rejectedCount > 0) {
    if (!quiet) toast(t("pendingProposeFiltered", rejectedCount), true);
  } else if (skippedDup > 0) {
    if (!quiet) toast(t("pendingExists"));
    openToolPanel("pending");
  } else {
    if (!quiet) toast(t("pendingProposeNone"), true);
  }
  if (added > 0 && rejectedCount > 0 && !quiet) {
    toast(t("pendingProposeFiltered", rejectedCount), true);
  }
  return {
    ok: added > 0,
    added,
    rejected: rejectedCount,
    skippedDup,
  };
}

function removePendingItem(id) {
  _tool.items = _tool.items.filter((it) => it.id !== id);
  renderPendingList();
}

function clearPendingList() {
  if (!_tool.items.length) return;
  _tool.items = [];
  renderPendingList();
}

/** 未执行完、仍可再删的条目数（用于徽章与执行按钮）。 */
function pendingActiveCount() {
  return _tool.items.filter((it) => !it.result || it.result.status !== "ok").length;
}

/**
 * 把后端 code / 错误文案归成列表状态。
 * @returns {{ status: string, message: string }}
 */
function classifyPendingResult(res, errText) {
  const code = (res && res.code) || "";
  const msg = (res && res.error) || errText || t("deleteFail");
  if (code === "missing") {
    return { status: "missing", message: msg };
  }
  if (code === "blacklist") {
    return { status: "blacklist", message: msg };
  }
  if (
    code === "root" ||
    code === "drive_root" ||
    code === "outside" ||
    code === "invalid" ||
    code === "recycle_unsupported"
  ) {
    return { status: "blocked", message: msg };
  }
  return { status: "fail", message: msg };
}

function pendingStatusLabel(status) {
  if (status === "ok") return t("pendingStatusOk");
  if (status === "missing") return t("pendingStatusMissing");
  if (status === "blacklist") return t("pendingStatusBlacklist");
  if (status === "blocked") return t("pendingStatusBlocked");
  if (status === "fail") return t("pendingStatusFail");
  return "";
}

/** 同步执行/清空按钮禁用态（侧栏三角不再显示数量角标）。 */
function updatePendingBadge() {
  const n = _tool.items.length;
  const active = pendingActiveCount();
  const execBtn = $("#pendingExecuteBtn");
  if (execBtn) execBtn.disabled = active === 0 || _tool.executing;
  const clearBtn = $("#pendingClearBtn");
  if (clearBtn) clearBtn.disabled = n === 0 || _tool.executing;
}

/**
 * AI 审批后直接入队：规范化 + 去重，不调用 check_pending_paths。
 * 白名单在真正删除时由 delete_path 处理。
 * @param {Array<object>} items
 * @returns {{added: number, skippedDup: number}}
 */
function enqueuePendingFromAi(items) {
  const list = Array.isArray(items) ? items : [];
  const defaultRoot = (typeof state !== "undefined" && state.compareRoot) || "";
  let added = 0;
  let skippedDup = 0;
  list.forEach((raw) => {
    if (!raw || typeof raw !== "object") return;
    const root = String(raw.root || defaultRoot || "").trim();
    const rel = String(raw.rel || raw.rel_path || "").trim();
    if (!root && !rel && !raw.path) return;
    const key = pendingItemKey(root, rel);
    if (_tool.items.some((it) => pendingItemKey(it.root, it.rel) === key)) {
      skippedDup += 1;
      return;
    }
    const full =
      (raw.path && String(raw.path)) ||
      (typeof fullPath === "function" ? fullPath(root, rel) : root + "\\" + rel);
    _tool.items.push({
      id: `p${_pendingIdSeq++}`,
      root,
      rel,
      name: String(raw.name || rel || full),
      isDir: !!raw.is_dir,
      full,
      result: null,
    });
    added += 1;
  });
  if (added > 0) {
    renderPendingList();
  } else {
    updatePendingBadge();
  }
  return { added, skippedDup };
}

function renderPendingList() {
  const list = $("#pendingList");
  const empty = $("#pendingEmpty");
  if (!list) return;
  const items = _tool.items;
  if (empty) empty.classList.toggle("hidden", items.length > 0);
  list.innerHTML = items
    .map((it) => {
      const icon = it.isDir ? "📁" : "📄";
      const st = it.result && it.result.status;
      const stClass = st ? ` is-${escapeHtml(st)}` : "";
      const stLabel = st ? pendingStatusLabel(st) : "";
      const stTitle = (it.result && it.result.message) || stLabel;
      const badge = stLabel
        ? `<span class="pending-item-status" title="${escapeHtml(stTitle)}">${escapeHtml(stLabel)}</span>`
        : "";
      return (
        `<div class="pending-item${stClass}" data-id="${escapeHtml(it.id)}">` +
        `<div class="pending-item-main">` +
        `<span class="pending-item-icon" aria-hidden="true">${icon}</span>` +
        `<div class="pending-item-text">` +
        `<div class="pending-item-name" title="${escapeHtml(it.full)}">${escapeHtml(it.name)}</div>` +
        `<div class="pending-item-path" title="${escapeHtml(it.full)}">${escapeHtml(it.full)}</div>` +
        `</div></div>` +
        badge +
        `<button type="button" class="btn-plain compact pending-item-remove" data-remove-id="${escapeHtml(it.id)}" data-i18n-title="pendingRemove" title="${escapeHtml(t("pendingRemove"))}">✕</button>` +
        `</div>`
      );
    })
    .join("");
  list.querySelectorAll("[data-remove-id]").forEach((btn) => {
    btn.onclick = () => {
      if (_tool.executing) return;
      removePendingItem(btn.getAttribute("data-remove-id"));
    };
  });
  updatePendingBadge();
  const chk = $("#pendingPermanentChk");
  if (chk) chk.checked = !!_tool.permanent;
}

async function executePendingDeletes() {
  if (_tool.executing) return;
  if (!state.api || !state.api.delete_path) {
    toast(t("deleteFail"), true);
    return;
  }
  // 已成功的不再重试；失败/缺失等可再执行
  const items = _tool.items.filter((it) => !it.result || it.result.status !== "ok");
  if (!items.length) {
    toast(t("pendingEmpty"), true);
    return;
  }
  const permanent = !!_tool.permanent;
  const n = items.length;
  if (permanent) {
    const ok1 = await showConfirmDialog({
      title: t("deletePermanentTitle"),
      message: t("pendingExecutePermanentConfirm", n),
      okText: t("deletePermanent"),
      danger: true,
    });
    if (!ok1) return;
    const ok2 = await showConfirmDialog({
      title: t("deletePermanentTitle"),
      message: t("pendingExecutePermanentAgain", n),
      okText: t("deletePermanent"),
      danger: true,
    });
    if (!ok2) return;
  } else {
    const ok = await showConfirmDialog({
      title: t("deleteTitle"),
      message: t("pendingExecuteConfirm", n),
      okText: t("deleteToRecycle"),
      danger: true,
    });
    if (!ok) return;
  }

  _tool.executing = true;
  updatePendingBadge();
  let okCount = 0;
  let failCount = 0;
  try {
    for (const it of items) {
      try {
        const res = await state.api.delete_path(it.root, it.rel, permanent);
        if (res && res.error) {
          failCount += 1;
          it.result = classifyPendingResult(res);
        } else {
          okCount += 1;
          it.result = {
            status: "ok",
            message: permanent ? t("deletedPermanent") : t("deletedRecycle"),
          };
        }
      } catch (e) {
        failCount += 1;
        it.result = classifyPendingResult(null, String(e) || t("deleteFail"));
      }
      renderPendingList();
    }
  } finally {
    _tool.executing = false;
    renderPendingList();
  }
  if (okCount > 0 && failCount === 0) {
    toast(
      permanent
        ? `${t("deletedPermanent")} · ${t("deleteRefreshHint")}`
        : `${t("deletedRecycle")} · ${t("deleteRefreshHint")}`
    );
  } else if (okCount > 0 || failCount > 0) {
    toast(t("pendingPartial", okCount, failCount), failCount > 0);
  }
}

function wirePendingUi() {
  wireToolPanelResizer();
  const rail = $("#toolRailToggle");
  if (rail) {
    rail.onclick = () => toggleToolPanel();
  }
  const closeBtn = $("#toolCloseBtn");
  if (closeBtn) closeBtn.onclick = () => closeToolPanel();
  document.querySelectorAll(".tool-tab").forEach((btn) => {
    btn.onclick = () => {
      const tab = btn.getAttribute("data-tool-tab") || "pending";
      if (tab === "ai" && typeof isAiAvailable === "function" && !isAiAvailable()) {
        toast(t("aiModuleMissing"), true);
        return;
      }
      if (!_tool.open) openToolPanel(tab);
      else switchToolTab(tab);
    };
  });
  const clearBtn = $("#pendingClearBtn");
  if (clearBtn) clearBtn.onclick = () => clearPendingList();
  const execBtn = $("#pendingExecuteBtn");
  if (execBtn) execBtn.onclick = () => executePendingDeletes();
  const permChk = $("#pendingPermanentChk");
  if (permChk) {
    permChk.onchange = async (e) => {
      const on = !!e.target.checked;
      if (!on) {
        _tool.permanent = false;
        return;
      }
      // 先保持未勾选，确认后再打开，避免取消时闪一下勾选态
      e.target.checked = false;
      _tool.permanent = false;
      const ok = await showConfirmDialog({
        title: t("pendingPermanentWarnTitle"),
        message: t("pendingPermanentWarn"),
        okText: t("confirmOk"),
        danger: true,
      });
      if (!ok) return;
      e.target.checked = true;
      _tool.permanent = true;
    };
  }

  // 可勾选清单：全选 / 全不选 / 确认 / 取消
  const clAll = $("#pendingChecklistSelectAllBtn");
  if (clAll) clAll.onclick = () => _pendingChecklistSetAll(true);
  const clNone = $("#pendingChecklistSelectNoneBtn");
  if (clNone) clNone.onclick = () => _pendingChecklistSetAll(false);
  const clOk = $("#pendingChecklistOkBtn");
  if (clOk) {
    clOk.onclick = () => {
      const selected = _pendingChecklistCollectChecked();
      if (!selected.length) {
        toast(t("pendingChecklistEmpty"), true);
        return;
      }
      closePendingChecklistDialog(selected);
    };
  }
  const clCancel = $("#pendingChecklistCancelBtn");
  if (clCancel) clCancel.onclick = () => closePendingChecklistDialog(null);
  const clClose = $("#pendingChecklistCloseBtn");
  if (clClose) clClose.onclick = () => closePendingChecklistDialog(null);

  renderPendingList();
  refreshToolTabsVisibility();
}

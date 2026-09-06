/* 右侧工具侧栏 + 待删除列表（会话内，不写 yaml） */
"use strict";

/**
 * items[].result: null | { status: 'ok'|'missing'|'blacklist'|'blocked'|'fail', message?: string }
 * @type {{ open: boolean, tab: string, items: Array, permanent: boolean, executing: boolean }}
 */
const _tool = {
  open: false,
  tab: "link",
  items: [],
  permanent: false,
  executing: false,
  // 待删除列表排序：added-desc|added-asc|name-asc|name-desc|size-desc|path-asc
  sort: "added-desc",
};

let _pendingIdSeq = 1;

function isToolOpen() {
  return !!_tool.open;
}

/** 侧栏面板目标宽度（与 CSS .tool-panel 一致），用于扩展主窗口 */
const _TOOL_PANEL_W_MIN = 240;
const _TOOL_PANEL_W_MAX = 640;
const _TOOL_PANEL_W_DEFAULT = 340;
// 旧版 localStorage 键，仅用于一次性迁移到 settings.yaml，之后不再读写
const _TOOL_PANEL_W_LEGACY_KEY = "wsmc.toolPanelWidth";
const _TOOL_OPEN_LEGACY_KEY = "wsmc.toolOpen";
const _TOOL_TAB_KEY = "wsmc.toolTab";
const _TOOL_ANIM_MS = 250;

// 侧栏开合编排（关键约束：原生窗口 resize 是异步的、多次调用会合并，
// 永远不要试图让它逐帧跟随 CSS 动画）：
//   展开 = 钉死主区当前宽度 → 一次性加宽窗口 → 侧栏在新增的右侧区域里
//          用 CSS 过渡滑入 → 结束后解除钉宽。
//   收起 = 钉死主区宽度 → 侧栏 CSS 过渡收回 → 一次性收窄窗口 →
//          等 viewport 跟上后解除钉宽。
// 主区宽度全程不变，窗口右边缘与侧栏各动各的。
let _toolAnimGen = 0; // 动画代际：反向打断时作废旧收尾回调
let _toolPanelW = _TOOL_PANEL_W_DEFAULT; // 侧栏「应有」宽度（拖拽/设置同步时更新）
let _toolWinSeq = Promise.resolve(); // 串行化窗口伸缩调用，防止快速开合乱序

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
  _toolPanelW = w;
  document.documentElement.style.setProperty("--tool-panel-w", w + "px");
  if (panel) panel.style.width = w + "px";
  return w;
}

/** 动画专用：允许把侧栏宽度设到 0（收起终点），不走最小宽度夹取。 */
function _setToolPanelRawWidth(px) {
  const panel = $("#toolPanel");
  let w = Math.round(Number(px));
  if (!Number.isFinite(w)) w = 0;
  w = Math.max(0, Math.min(_TOOL_PANEL_W_MAX, w));
  document.documentElement.style.setProperty("--tool-panel-w", w + "px");
  if (panel) panel.style.width = w + "px";
  return w;
}

/** 启动时把「默认页签」从 localStorage 恢复；展开状态由 settings.yaml 决定。 */
function restoreToolPanelState() {
  // 无上次记录时默认停在「目录链接」；有记录则恢复上次页签。
  // 业务跳转（如加入待删除后自动切到该页）不受影响，见 openToolPanel("pending") 调用点。
  let tab = "link";
  try {
    const savedTab = localStorage.getItem(_TOOL_TAB_KEY);
    if (savedTab && (savedTab === "pending" || savedTab === "link" ||
                     (typeof hasModule === "function" && hasModule("ai") && savedTab === "ai"))) {
      tab = savedTab;
    }
  } catch (e) {}
  const panel = $("#toolPanel");
  if (!panel) return;
  // 收起态：直接把 panel 折成 0 宽，避免过渡期间闪烁；
  // 若 YAML 记住的是展开态，boot 后段 syncToolPanelFromSettings 再展开
  panel.classList.add("collapsed");
  panel.classList.add("hidden");
  _tool.open = false;
  _tool.tab = tab;
}

/**
 * 一次性把旧版 localStorage 里的侧栏状态搬进 settings.yaml。
 * 后端 migrate 只在 YAML 没写过该键时执行，重复调用无害。
 */
async function migrateToolPanelStateLegacy() {
  let open = false;
  let width = _TOOL_PANEL_W_DEFAULT;
  let hasLegacy = false;
  try {
    const rawOpen = localStorage.getItem(_TOOL_OPEN_LEGACY_KEY);
    const rawW = localStorage.getItem(_TOOL_PANEL_W_LEGACY_KEY);
    if (rawOpen != null || rawW != null) {
      hasLegacy = true;
      open = rawOpen === "1";
      const w = parseInt(rawW, 10);
      if (Number.isFinite(w)) width = w;
    }
  } catch (e) {}
  if (!hasLegacy) return;
  try {
    await state.api.sync_tool_panel_state(open, width);
  } catch (e) {}
  try {
    localStorage.removeItem(_TOOL_OPEN_LEGACY_KEY);
    localStorage.removeItem(_TOOL_PANEL_W_LEGACY_KEY);
  } catch (e) {}
}

/**
 * 按 settings.yaml 渲染侧栏宽度与展开状态（boot 后段调用）。
 * 展开不回调 set_tool_panel_open：启动宽度已由 Python 侧算好。
 */
function syncToolPanelFromSettings() {
  const s = state._settings;
  if (!s) return;
  if (s.ui_tool_panel_width) applyToolPanelWidth(s.ui_tool_panel_width);
  if (s.ui_tool_panel_open === true) {
    openToolPanel(_tool.tab || "pending", { silent: true });
  }
}

function persistToolTab() {
  try { localStorage.setItem(_TOOL_TAB_KEY, String(_tool.tab || "pending")); } catch (e) {}
}

function syncToolPanelResizerVisibility() {
  const handle = $("#toolPanelResizer");
  const panel = $("#toolPanel");
  if (!handle) return;
  // 侧栏仍在布局中（收起动画未走完、还没 hidden）就保持显示，
  // 让分界拖条跟着侧栏左缘一起收回，动画结束后才消失
  const visible = _tool.open || (panel && !panel.classList.contains("hidden"));
  handle.classList.toggle("hidden", !visible);
}

/** 右侧工具栏左缘拖拽：变宽挤压中间对比区，不改窗口尺寸 */
function wireToolPanelResizer() {
  const handle = $("#toolPanelResizer");
  const panel = $("#toolPanel");
  if (!handle || !panel) return;

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
    // 宽度只记进 YAML，不改窗口尺寸（拖拽挤压的是主区）
    try {
      state.api.set_tool_panel_width(w);
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

/** 钉死主区当前宽度：flex 不再重新分配，窗口伸缩产生的空区全部落在主区右侧。 */
function _pinMainWidth() {
  const main = document.querySelector("main.main");
  if (!main || main.dataset.pinned === "1") return;
  const w = Math.round(main.getBoundingClientRect().width);
  if (!(w > 0)) return;
  main.style.width = w + "px";
  main.style.flex = "none";
  main.style.marginRight = "auto"; // 空区留在主区与侧栏之间，侧栏贴右缘
  main.dataset.pinned = "1";
}

/** 解除钉宽：窗口尺寸已与内容一致，恢复 flex:1 不产生视觉变化。 */
function _unpinMainWidth() {
  const main = document.querySelector("main.main");
  if (!main || main.dataset.pinned !== "1") return;
  main.style.width = "";
  main.style.flex = "";
  main.style.marginRight = "";
  delete main.dataset.pinned;
}

/** 串行调用后端的一次性窗口伸缩；调用顺序即生效顺序。 */
function _resizeWindowForPanel(open, panelW) {
  const call = _toolWinSeq.then(() =>
    state.api.resize_window_for_panel(open, panelW)
  );
  _toolWinSeq = call.then(() => {}, () => {});
  return call;
}

/** 等 viewport 真正变到新尺寸（resize 事件）或超时兜底后再回调。 */
function _waitViewportSettle(cb) {
  let done = false;
  const fire = () => {
    if (done) return;
    done = true;
    window.removeEventListener("resize", fire);
    cb();
  };
  window.addEventListener("resize", fire);
  setTimeout(fire, 200);
}

/**
 * 打开侧栏：先钉死主区当前宽度、一次性加宽窗口，侧栏再用 CSS 过渡
 * 滑入新增的右侧区域，结束后解除钉宽。主区宽度全程不变。
 *
 * ``opts.silent`` 为真时（启动恢复）窗口宽度已按记住的值算好，
 * 只展开侧栏，不伸缩窗口、不动画。
 */
function openToolPanel(tab, opts) {
  const panel = $("#toolPanel");
  if (!panel) return;
  const silent = !!(opts && opts.silent);
  const wantTab =
    typeof tab === "string" && tab ? tab : (_tool.tab || "pending");
  if (_tool.open && !panel.classList.contains("hidden")) {
    switchToolTab(wantTab);
    syncToolRailState();
    return;
  }
  _tool.open = true;
  _tool.tab = wantTab;
  persistToolTab();
  switchToolTab(wantTab);
  syncToolRailState();

  const targetW = _toolPanelW;

  if (silent) {
    panel.classList.remove("hidden");
    panel.classList.remove("collapsed");
    _setToolPanelRawWidth(targetW);
    syncToolPanelResizerVisibility();
    return;
  }

  const gen = ++_toolAnimGen;
  _pinMainWidth();
  if (panel.classList.contains("hidden")) {
    // 从收起态展开：先瞬时归零，再交给 CSS 过渡滑入
    panel.style.transition = "none";
    panel.classList.remove("hidden");
    panel.classList.add("collapsed");
    _setToolPanelRawWidth(0);
    panel.getBoundingClientRect(); // 强制回流，让 0 宽先落地
    panel.style.transition = "";
  } else {
    // 打断收起中的反向动画：从当前宽度直接过渡回目标宽
    panel.classList.remove("collapsed");
  }

  const reveal = () => {
    if (gen !== _toolAnimGen) return;
    syncToolPanelResizerVisibility(); // resizer 跟着侧栏左缘滑入
    panel.classList.remove("collapsed");
    _setToolPanelRawWidth(targetW); // CSS 过渡 0 → targetW
    setTimeout(() => {
      if (gen !== _toolAnimGen) return;
      _unpinMainWidth(); // 窗口已加宽到位，恢复 flex 布局无视觉变化
      try {
        state.api.set_tool_panel_open(true, targetW);
      } catch (e) {}
    }, _TOOL_ANIM_MS + 60);
  };

  // 先一次性加宽窗口；失败（最大化等）则退化为挤压主区的行为
  _resizeWindowForPanel(true, targetW)
    .then((r) => {
      if (gen !== _toolAnimGen) return;
      if (!r || !r.ok) _unpinMainWidth();
      reveal();
    })
    .catch(() => {
      if (gen !== _toolAnimGen) return;
      _unpinMainWidth();
      reveal();
    });
}

function closeToolPanel() {
  const panel = $("#toolPanel");
  // 已关且面板已藏：无需再动
  if (!_tool.open && (!panel || panel.classList.contains("hidden"))) {
    return;
  }
  _tool.open = false;
  syncToolRailState();
  // 关闭侧栏时若 AI 在流式回复，停止（与原先 closeAiPanel 一致）
  if (typeof stopAiRequest === "function" && typeof _ai !== "undefined" && _ai.streaming && _ai.requestId) {
    stopAiRequest();
  }
  if (typeof _ai !== "undefined") _ai.open = false;
  persistToolTab();

  const gen = ++_toolAnimGen;
  const keepW = _toolPanelW;
  _pinMainWidth(); // 主区钉在当前宽，侧栏收走的区域先露背景
  panel.classList.remove("hidden");
  panel.classList.add("collapsed"); // CSS 过渡 width → 0
  setTimeout(() => {
    if (gen !== _toolAnimGen) return; // 被再次展开打断
    panel.classList.add("hidden");
    syncToolPanelResizerVisibility(); // resizer 随侧栏收完后消失
    const finish = () => {
      if (gen !== _toolAnimGen) return;
      _unpinMainWidth();
      try {
        state.api.set_tool_panel_open(false, keepW);
      } catch (e) {}
    };
    // 先收窄窗口（原生异步），等 viewport 跟上再解钉，避免主区闪宽
    _resizeWindowForPanel(false, keepW)
      .then(() => _waitViewportSettle(finish))
      .catch(() => finish());
  }, _TOOL_ANIM_MS + 60);
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
    // rail 已搬到 comparebar 内，与 #toolDock 不再是祖先关系，CSS 选择器失效；
    // 在按钮自身上同步 .open，让 CSS 可以直接匹配
    rail.classList.toggle("open", !!_tool.open);
    rail.setAttribute("aria-expanded", _tool.open ? "true" : "false");
    // 不设 title，避免展开三角悬停冒泡提示
    rail.removeAttribute("title");
    rail.removeAttribute("data-i18n-title");
  }
  syncToolPanelResizerVisibility();
  updatePendingBadge();
}

/** 有 AI 模块时显示 AI 页签；无则隐藏。待删除与目录迁移常驻。 */
function refreshToolTabsVisibility() {
  if (typeof applyModuleVisibility === "function") applyModuleVisibility();
  const aiOn = typeof hasModule === "function" && hasModule("ai");
  if (!aiOn && _tool.tab === "ai") {
    switchToolTab("pending");
  }
  // 待删除 + 目录迁移至少两项常驻，单页签弱化样式不再适用
  const tabs = document.querySelector(".tool-tabs");
  if (tabs) tabs.classList.remove("tool-tabs-single");
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
  if (tab === "link" && typeof refreshLinkList === "function") {
    refreshLinkList(false);
  }
  if (_tool.open) persistToolTab();
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
    size: Number(node && (node.new_size != null ? node.new_size : node.old_size)) || 0,
    oldSize: Number(node && node.old_size) || 0,
    newSize: Number(node && node.new_size) || 0,
    delta: Number(node && node.delta) || (
      (Number(node && node.new_size) || 0) - (Number(node && node.old_size) || 0)
    ),
    addedAt: Date.now(),
    result: null,
  });
  renderPendingList();
  openToolPanel("pending");
  toast(t("pendingAdded"));
}

/** 批量加入待删除；nodes 为对比树节点摘要列表。 */
function addCompareNodesToPending(nodes) {
  const list = Array.isArray(nodes) ? nodes : [];
  if (!list.length) {
    toast(t("treeSelectNeed"), true);
    return { added: 0, skippedDup: 0 };
  }
  if (!state.compareRoot) {
    toast(t("deleteFail"), true);
    return { added: 0, skippedDup: 0 };
  }
  const root = state.compareRoot;
  let added = 0;
  let skippedDup = 0;
  for (const node of list) {
    const rel = (node && node.path) || "";
    if (!rel) continue;
    const key = pendingItemKey(root, rel);
    if (_tool.items.some((it) => pendingItemKey(it.root, it.rel) === key)) {
      skippedDup += 1;
      continue;
    }
    const full = fullPath(root, rel);
    _tool.items.push({
      id: `p${_pendingIdSeq++}`,
      root,
      rel,
      name: (node && (node.name || node.path)) || rel,
      isDir: !!(node && node.is_dir),
      full,
      size: Number(node && (node.new_size != null ? node.new_size : node.old_size)) || 0,
      oldSize: Number(node && node.old_size) || 0,
      newSize: Number(node && node.new_size) || 0,
      delta: Number(node && node.delta) || (
        (Number(node && node.new_size) || 0) - (Number(node && node.old_size) || 0)
      ),
      addedAt: Date.now(),
      result: null,
    });
    added += 1;
  }
  if (added > 0) {
    renderPendingList();
    openToolPanel("pending");
    toast(t("pendingAddedN", added));
  } else if (skippedDup > 0) {
    toast(t("pendingExists"));
    openToolPanel("pending");
  } else {
    toast(t("treeSelectNeed"), true);
  }
  return { added, skippedDup };
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
      size: Number(row && row.size) || 0,
      addedAt: Date.now(),
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

// 排序项：key + 默认方向。选中后再次点击同一项切换正倒序。
const PENDING_SORT_OPTIONS = [
  { key: "added", dir: "desc", i18nKey: "pendingSortAdded" }, // 默认：加入时间
  { key: "delta", dir: "desc", i18nKey: "pendingSortDelta" }, // 变化大小
  { key: "pct", dir: "desc", i18nKey: "pendingSortPct" },     // 变化比例
  { key: "size", dir: "desc", i18nKey: "pendingSortSize" },   // 占用大小
  { key: "name", dir: "asc", i18nKey: "pendingSortName" },    // 名称
];

/** 把 "added-desc" 拆成 { key, dir }。 */
function _splitPendingSort(v) {
  const s = String(v || "added-desc");
  const i = s.lastIndexOf("-");
  return i > 0 ? { key: s.slice(0, i), dir: s.slice(i + 1) } : { key: s, dir: "desc" };
}

/** 把 { key, dir } 拼成 "added-desc"。 */
function _joinPendingSort(key, dir) {
  return `${key}-${dir}`;
}

function _pendingDelta(it) {
  if (!it) return 0;
  if (it.delta != null && Number.isFinite(Number(it.delta))) return Number(it.delta);
  return (Number(it.newSize) || 0) - (Number(it.oldSize) || 0);
}

function _pendingPct(it) {
  const oldSize = Number(it && it.oldSize) || 0;
  const delta = Math.abs(_pendingDelta(it));
  if (oldSize > 0) return delta / oldSize;
  return delta !== 0 ? Number.POSITIVE_INFINITY : 0;
}

function sortedPendingItems(items) {
  const list = Array.isArray(items) ? items.slice() : [];
  const { key, dir } = _splitPendingSort((_tool && _tool.sort) || "added-desc");
  const loc = typeof cmpLocale === "function" ? cmpLocale() : undefined;
  const byPath = (a, b) =>
    String(a.full || a.rel || "").localeCompare(String(b.full || b.rel || ""), loc);
  // 先定义升序比较器，dir=desc 时整体取反。
  let cmp;
  if (key === "delta") {
    cmp = (a, b) =>
      Math.abs(_pendingDelta(a)) - Math.abs(_pendingDelta(b)) ||
      (Number(a.size) || 0) - (Number(b.size) || 0) ||
      byPath(a, b);
  } else if (key === "pct") {
    cmp = (a, b) =>
      _pendingPct(a) - _pendingPct(b) ||
      Math.abs(_pendingDelta(a)) - Math.abs(_pendingDelta(b)) ||
      byPath(a, b);
  } else if (key === "size") {
    cmp = (a, b) =>
      (Number(a.size) || 0) - (Number(b.size) || 0) ||
      byPath(a, b);
  } else if (key === "name") {
    cmp = (a, b) =>
      String(a.name || a.rel || "").localeCompare(String(b.name || b.rel || ""), loc) ||
      byPath(a, b);
  } else {
    // added：先加入的在前
    cmp = (a, b) => (a.addedAt || 0) - (b.addedAt || 0) || byPath(a, b);
  }
  if (dir === "desc") {
    const asc = cmp;
    cmp = (a, b) => -asc(a, b);
  }
  list.sort(cmp);
  return list;
}

function syncPendingSortChrome() {
  const { key, dir } = _splitPendingSort((_tool && _tool.sort) || "added-desc");
  const btn = $("#pendingSortBtn");
  const opt = PENDING_SORT_OPTIONS.find((o) => o.key === key) || PENDING_SORT_OPTIONS[0];
  if (btn) {
    btn.classList.toggle("is-active", (_tool && _tool.sort) !== "added-desc");
    btn.setAttribute("aria-expanded", "false");
    // 复用 summary-icon-btn：只改 title，不改内部 SVG
    const title = opt ? `${t(opt.i18nKey)} · ${t(dir === "asc" ? "pendingSortAsc" : "pendingSortDesc")}` : t("pendingSortTitle");
    btn.title = title;
    btn.setAttribute("data-i18n-title", "pendingSortTitle");
  }
}

function closePendingSortMenu() {
  const menu = $("#pendingSortMenu");
  const btn = $("#pendingSortBtn");
  if (menu) menu.classList.add("hidden");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function openPendingSortMenu(anchor) {
  const menu = $("#pendingSortMenu");
  const btn = $("#pendingSortBtn");
  if (!menu || !anchor) return;
  if (!menu.classList.contains("hidden")) {
    closePendingSortMenu();
    return;
  }
  if (typeof closeSummaryMenus === "function") closeSummaryMenus();
  // tool-panel 有 contain: layout style，fixed 菜单会在侧栏内被裁切/定位错
  // 打开时挂到 body，与主界面排序菜单一致
  if (menu.parentElement !== document.body) {
    document.body.appendChild(menu);
  }
  menu.innerHTML = "";
  const current = _splitPendingSort((_tool && _tool.sort) || "added-desc");
  for (const opt of PENDING_SORT_OPTIONS) {
    const isSelected = opt.key === current.key;
    const dir = isSelected ? current.dir : opt.dir;
    const item = document.createElement("button");
    item.type = "button";
    item.className = "icon-menu-item" + (isSelected ? " is-selected" : "");
    item.setAttribute("role", "menuitemradio");
    item.setAttribute("aria-checked", isSelected ? "true" : "false");
    // 文案 + 方向箭头（未选中的项显示其默认方向）
    const label = document.createElement("span");
    label.className = "sort-menu-label";
    label.textContent = t(opt.i18nKey);
    const arrow = document.createElement("span");
    arrow.className = "sort-menu-arrow";
    arrow.textContent = t(dir === "asc" ? "pendingSortAsc" : "pendingSortDesc");
    item.appendChild(label);
    item.appendChild(arrow);
    item.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      // 再次点击同一项：切换正倒序；否则用该项默认方向
      const nextDir = isSelected ? (current.dir === "asc" ? "desc" : "asc") : opt.dir;
      _tool.sort = _joinPendingSort(opt.key, nextDir);
      syncPendingSortChrome();
      renderPendingList();
      closePendingSortMenu();
    };
    menu.appendChild(item);
  }
  const r = anchor.getBoundingClientRect();
  menu.classList.remove("hidden");
  // 先显示再量宽
  const mw = menu.offsetWidth || 180;
  let left = r.right - mw;
  if (left < 8) left = 8;
  if (left + mw > window.innerWidth - 8) {
    left = Math.max(8, window.innerWidth - mw - 8);
  }
  let top = r.bottom + 4;
  const mh = menu.offsetHeight || 160;
  if (top + mh > window.innerHeight - 8) {
    top = Math.max(8, r.top - mh - 4);
  }
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  if (btn) btn.setAttribute("aria-expanded", "true");
}

function pendingMetaLine(it) {
  const size = Number(it && it.size) || 0;
  const delta = _pendingDelta(it);
  const parts = [];
  if (size > 0) parts.push(fmtBytes(size));
  parts.push(delta !== 0 ? fmtDelta(delta) : "±0");
  return parts.join(" · ");
}

function renderPendingList() {
  const list = $("#pendingList");
  const empty = $("#pendingEmpty");
  if (!list) return;
  syncPendingSortChrome();
  const items = sortedPendingItems(_tool.items);
  if (empty) empty.classList.toggle("hidden", (_tool.items || []).length > 0);
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
        `<div class="pending-item-meta" title="${escapeHtml(it.full)}">${escapeHtml(pendingMetaLine(it))}</div>` +
        `</div></div>` +
        badge +
        `<button type="button" class="btn-plain compact pending-item-locate" data-locate-id="${escapeHtml(it.id)}" data-i18n-title="pendingLocate" title="${escapeHtml(t("pendingLocate"))}">${escapeHtml(t("pendingLocate"))}</button>` +
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
  list.querySelectorAll("[data-locate-id]").forEach((btn) => {
    btn.onclick = () => {
      const id = btn.getAttribute("data-locate-id");
      const it = _tool.items.find((x) => x.id === id);
      if (it) locatePendingItem(it);
    };
  });
  updatePendingBadge();
  const chk = $("#pendingPermanentChk");
  if (chk) chk.checked = !!_tool.permanent;
}

/** 待删除项定位回对比树：逐段展开路径并高亮目标行。 */
function locatePendingItem(it) {
  if (!it || !it.rel) return;
  if (!state.compared) {
    toast(t("pendingLocateNoTree"), true);
    return;
  }
  if (
    it.root &&
    state.compareRoot &&
    _treePathKey(String(it.root).replace(/[\\/]+$/, "")) !==
      _treePathKey(String(state.compareRoot).replace(/[\\/]+$/, ""))
  ) {
    toast(t("pendingLocateOtherRoot"), true);
    return;
  }
  if (typeof locateTreePath === "function") {
    locateTreePath(it.rel);
  } else {
    toast(t("pendingLocateNoTree"), true);
  }
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
  restoreToolPanelState();
  const sortBtn = $("#pendingSortBtn");
  if (sortBtn) {
    sortBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openPendingSortMenu(sortBtn);
    });
    syncPendingSortChrome();
  }
  document.addEventListener("click", (e) => {
    const menu = $("#pendingSortMenu");
    if (!menu || menu.classList.contains("hidden")) return;
    if (e.target.closest("#pendingSortMenu") || e.target.closest("#pendingSortBtn")) return;
    closePendingSortMenu();
  });
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

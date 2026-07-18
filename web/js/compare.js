/* 对比结果树 / 排序 / 右键菜单 */
"use strict";

// ---- 对比与变化树 ----

function _searchPreheatKeyFor(oldPath, newPath) {
  return `${oldPath || ""}\n${newPath || ""}`;
}

function _currentSearchPreheatKey() {
  return _searchPreheatKeyFor(state.oldPath, state.newPath);
}

function setCompareBusy(text) {
  const el = $("#compareBusy");
  if (!el) return;
  const msg = String(text || "").trim();
  if (!msg) {
    el.textContent = "";
    el.classList.add("hidden");
    el.classList.remove("is-busy");
    return;
  }
  el.textContent = msg;
  el.classList.remove("hidden");
  el.classList.add("is-busy");
}

async function doCompare() {
  if (state.comparing) return;
  state.comparing = true;
  // 新一轮对比：立刻收起搜索栏；并强制作废上一对的索引就绪状态
  if (typeof collapseTreeSearch === "function") collapseTreeSearch({ clear: true });
  if (typeof resetSearchPreheatUi === "function") resetSearchPreheatUi();
  else {
    state.searchPreheat = "idle";
    state.searchPreheatKey = "";
  }
  const btn = $("#compareBtn");
  btn.disabled = true;
  // 按钮保持短文案「对比」，长状态放到下方独立行，避免顶栏被「正在解压…」撑乱
  btn.textContent = t("compare");
  // 是否真要解压以后端会话缓存为准，避免同快照再点对比误提示「正在解压」
  let needDecompress = false;
  try {
    if (state.api && state.api.compare_cache_status) {
      const st = await state.api.compare_cache_status(state.oldPath, state.newPath);
      if (st && st.ok !== false && !st.error) {
        needDecompress = !!st.need_decompress;
      } else {
        // 接口异常时：仅两侧都是未缓存的压缩包才猜解压
        needDecompress = [state.oldPath, state.newPath].some((p) => {
          const s = snapByPath(p);
          return s && s.compressed;
        });
      }
    } else {
      needDecompress = [state.oldPath, state.newPath].some((p) => {
        const s = snapByPath(p);
        return s && s.compressed;
      });
    }
  } catch (_) {
    needDecompress = [state.oldPath, state.newPath].some((p) => {
      const s = snapByPath(p);
      return s && s.compressed;
    });
  }
  const busyText = needDecompress ? t("decompressing") : t("comparing");
  setCompareBusy(busyText);
  // 首次对比时，空态标题也同步提示（主内容区更显眼）
  const empty = $("#emptyState");
  const emptyTitle = empty && empty.querySelector(".empty-title");
  const prevEmptyTitle = emptyTitle ? emptyTitle.textContent : "";
  if (empty && !empty.classList.contains("hidden") && emptyTitle) {
    emptyTitle.textContent = busyText;
  }
  try {
    const res = await state.api.compare(state.oldPath, state.newPath);
    if (res.error) {
      toast(res.error, true);
      return;
    }
    state.compared = true;
    state.compareRoot = res.summary.new.root;
    state._lastSummary = res.summary;
    state._lastCompareKey = `${state.oldPath}\n${state.newPath}`;
    state._lastComparePaths = [state.oldPath, state.newPath]
      .filter(Boolean)
      .slice()
      .sort()
      .join("\n");
    // 对比开始时已收起搜索；成功后再确保一次（防异步预热回调又撑开）
    if (typeof collapseTreeSearch === "function") collapseTreeSearch({ clear: true });
    renderSummary(res.summary);
    renderTopLevel(res.nodes);
    // 搜索仅回车触发；内存索引在打开搜索框时再预热
  } catch (err) {
    toast(t("compareFailed", err), true);
  } finally {
    state.comparing = false;
    setCompareBusy("");
    if (emptyTitle && empty && !empty.classList.contains("hidden")) {
      emptyTitle.textContent = prevEmptyTitle || t("emptyTitle");
    }
    btn.textContent = t("compare");
    updatePickers();
  }
}

/** 设置过滤并同步图标按钮状态（不触发重渲染，渲染由调用方负责）。 */
function setFilter(f) {
  state.filter = f;
  syncSummaryToolButtons();
}

const SORT_OPTIONS = [
  { value: "delta-desc", key: "sortDeltaDesc" },
  { value: "pct-desc", key: "sortPctDesc" },
  { value: "name-asc", key: "sortNameAsc" },
  { value: "name-desc", key: "sortNameDesc" },
  { value: "mtime-desc", key: "sortMtimeDesc" },
];

const FILTER_OPTIONS = [
  { value: "all", key: "filterAll" },
  { value: "grew", key: "filterGrew" },
  { value: "shrank", key: "filterShrank" },
];

const SNAP_SORT_OPTIONS = [
  { value: "time-desc", key: "snapSortTimeDesc" },
  { value: "time-asc", key: "snapSortTimeAsc" },
  { value: "name-asc", key: "snapSortNameAsc" },
  { value: "name-desc", key: "snapSortNameDesc" },
];

function syncSummaryToolButtons() {
  const sortBtn = $("#sortMenuBtn");
  if (sortBtn) {
    sortBtn.classList.toggle("is-active", state.sort !== "delta-desc");
    sortBtn.setAttribute("aria-expanded", "false");
  }
  const filterBtn = $("#filterMenuBtn");
  if (filterBtn) {
    filterBtn.classList.toggle("is-active", state.filter !== "all");
    filterBtn.setAttribute("aria-expanded", "false");
  }
  const snapSortBtn = $("#snapSortMenuBtn");
  if (snapSortBtn) {
    snapSortBtn.classList.toggle("is-active", state.snapSort !== "time-desc");
    snapSortBtn.setAttribute("aria-expanded", "false");
  }
  const searchSortBtn = $("#searchSortBtn");
  if (searchSortBtn) {
    searchSortBtn.classList.toggle(
      "is-active",
      (state.searchSort || "delta-desc") !== "delta-desc"
    );
    searchSortBtn.setAttribute("aria-expanded", "false");
  }
}

function closeSummaryMenus() {
  for (const id of ["sortMenu", "filterMenu", "snapSortMenu", "searchSortMenu"]) {
    const menu = $(`#${id}`);
    if (menu) menu.classList.add("hidden");
  }
  const sortBtn = $("#sortMenuBtn");
  if (sortBtn) sortBtn.setAttribute("aria-expanded", "false");
  const filterBtn = $("#filterMenuBtn");
  if (filterBtn) filterBtn.setAttribute("aria-expanded", "false");
  const snapSortBtn = $("#snapSortMenuBtn");
  if (snapSortBtn) snapSortBtn.setAttribute("aria-expanded", "false");
  const searchSortBtn = $("#searchSortBtn");
  if (searchSortBtn) searchSortBtn.setAttribute("aria-expanded", "false");
}

function _fillIconMenu(menu, options, current, onPick) {
  menu.innerHTML = "";
  for (const opt of options) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "icon-menu-item" + (opt.value === current ? " is-selected" : "");
    item.setAttribute("role", "menuitemradio");
    item.setAttribute("aria-checked", opt.value === current ? "true" : "false");
    item.textContent = t(opt.key);
    item.onclick = (e) => {
      e.stopPropagation();
      onPick(opt.value);
      closeSummaryMenus();
    };
    menu.appendChild(item);
  }
}

function openSummaryMenu(kind, anchor) {
  const cfg = {
    sort: {
      menu: "#sortMenu",
      btn: "#sortMenuBtn",
      options: SORT_OPTIONS,
      current: () => state.sort,
      onPick: (value) => {
        if (state.sort === value) return;
        state.sort = value;
        syncSummaryToolButtons();
        if (state.compared && state._topNodes) renderTopLevel(state._topNodes);
      },
    },
    filter: {
      menu: "#filterMenu",
      btn: "#filterMenuBtn",
      options: FILTER_OPTIONS,
      current: () => state.filter,
      onPick: (value) => {
        if (state.filter === value) return;
        setFilter(value);
        if (state.compared && state._topNodes) renderTopLevel(state._topNodes);
      },
    },
    snapSort: {
      menu: "#snapSortMenu",
      btn: "#snapSortMenuBtn",
      options: SNAP_SORT_OPTIONS,
      current: () => state.snapSort,
      onPick: (value) => {
        if (state.snapSort === value) return;
        state.snapSort = value;
        syncSummaryToolButtons();
        renderSnapshotList();
      },
    },
    searchSort: {
      menu: "#searchSortMenu",
      btn: "#searchSortBtn",
      options: SORT_OPTIONS,
      current: () => state.searchSort || "delta-desc",
      onPick: (value) => {
        if ((state.searchSort || "delta-desc") === value) return;
        state.searchSort = value;
        syncSummaryToolButtons();
        // 仅重排搜索结果，不影响变化树
        if (_searchQuery) runTreeSearch(_searchQuery);
      },
    },
  }[kind];
  if (!cfg || !anchor) return;

  const menu = $(cfg.menu);
  const btn = $(cfg.btn);
  if (!menu) return;

  if (!menu.classList.contains("hidden")) {
    closeSummaryMenus();
    return;
  }
  // 关掉其它图标菜单
  closeSummaryMenus();

  _fillIconMenu(menu, cfg.options, cfg.current(), cfg.onPick);

  const r = anchor.getBoundingClientRect();
  menu.classList.remove("hidden");
  // 先显示再量宽，贴按钮右对齐，避免溢出视口
  const mw = menu.offsetWidth || 180;
  let left = r.right - mw;
  if (left < 8) left = 8;
  if (left + mw > window.innerWidth - 8) left = Math.max(8, window.innerWidth - mw - 8);
  menu.style.left = `${left}px`;
  menu.style.top = `${r.bottom + 4}px`;
  if (btn) btn.setAttribute("aria-expanded", "true");
}

function renderSummary(summary) {
  $("#emptyState").classList.add("hidden");
  $("#summaryBar").classList.remove("hidden");

  const delta = summary.total_delta;
  const dEl = $("#summaryDelta");
  dEl.textContent = fmtDelta(delta);
  dEl.className = "summary-delta " + (delta >= 0 ? "grow" : "shrink");

  const skipped = summary.old.skipped_count + summary.new.skipped_count;
  const warn = $("#skipWarn");
  if (skipped > 0) {
    warn.textContent = t("skipWarn", skipped);
    warn.classList.remove("hidden");
  } else {
    warn.classList.add("hidden");
  }
}

let _preheatReadyTimer = 0;
let _searchInputSaved = "";
let _preheatWaiters = [];

function _resolvePreheatWaiters(status) {
  const list = _preheatWaiters.splice(0, _preheatWaiters.length);
  for (const fn of list) {
    try { fn(status); } catch (_) { /* ignore */ }
  }
}

/** 处理后端搜索内存索引预热事件。 */
function onSearchPreheatEvent(payload) {
  const st = payload && payload.status;
  if (st !== "started" && st !== "ready" && st !== "failed" && st !== "aborted") {
    return;
  }
  // 已清空对比：丢弃迟到推送
  if (!state.compared) {
    return;
  }
  if (st === "started" && state.searchPreheat === "ready") return;
  if (st === "aborted") {
    state.searchPreheat = "idle";
    state.searchPreheatKey = "";
    renderSearchPreheatStatus();
    _resolvePreheatWaiters("aborted");
    return;
  }
  state.searchPreheat = st;
  if (st === "ready" || st === "started" || st === "failed") {
    state.searchPreheatKey = _currentSearchPreheatKey();
  }
  renderSearchPreheatStatus();
  if (st === "ready" || st === "failed") {
    _resolvePreheatWaiters(st);
  }
}

/**
 * 状态写在搜索输入框 placeholder 上：
 * 准备中加宽高亮；就绪短暂提示后恢复。
 */
function renderSearchPreheatStatus() {
  const wrap = $("#treeSearchWrap");
  const input = $("#treeSearchInput");
  if (!input) return;
  if (_preheatReadyTimer) {
    clearTimeout(_preheatReadyTimer);
    _preheatReadyTimer = 0;
  }
  const st = state.searchPreheat || "idle";
  const open = !!(wrap && wrap.classList.contains("is-open"));
  const busy = open && st === "started" && !!state.compared && !!state.searchMemoryIndex;

  if (wrap) wrap.classList.toggle("is-preheating", busy);
  input.readOnly = busy;
  input.classList.toggle("is-preheating", busy);

  if (busy) {
    // 首次进入准备中时暂存用户已输入内容，准备完再写回
    if (_searchInputSaved === "" && input.value && input.value !== t("searchPreheatStarted")) {
      _searchInputSaved = input.value;
    }
    input.value = "";
    input.placeholder = t("searchPreheatStarted");
    // 不设 title，避免鼠标悬停再弹一层怪提示
    input.removeAttribute("title");
    return;
  }

  // 恢复 placeholder / 输入；就绪不再提示，直接回到正常搜索框
  const normalPh = t("treeSearchPlaceholder");
  if (_searchInputSaved) {
    input.value = _searchInputSaved;
    _searchInputSaved = "";
  }
  if (st === "failed" && open && state.compared) {
    input.placeholder = t("searchPreheatFailed");
    input.removeAttribute("title");
    _preheatReadyTimer = setTimeout(() => {
      _preheatReadyTimer = 0;
      input.placeholder = normalPh;
    }, 2600);
  } else {
    if (!input.value || input.placeholder === t("searchPreheatStarted")
        || input.placeholder === t("searchPreheatReady")
        || input.placeholder === t("searchPreheatFailed")) {
      input.placeholder = normalPh;
    }
    input.removeAttribute("title");
  }
}

/** 清空对比 / 收起时复位预热 UI。 */
function resetSearchPreheatUi() {
  if (_preheatReadyTimer) {
    clearTimeout(_preheatReadyTimer);
    _preheatReadyTimer = 0;
  }
  _searchInputSaved = "";
  state.searchPreheat = "idle";
  state.searchPreheatKey = "";
  _resolvePreheatWaiters("idle");
  const wrap = $("#treeSearchWrap");
  const input = $("#treeSearchInput");
  if (wrap) wrap.classList.remove("is-preheating");
  if (input) {
    input.readOnly = false;
    input.classList.remove("is-preheating");
    input.placeholder = t("treeSearchPlaceholder");
    input.removeAttribute("title");
  }
}

/**
 * 打开搜索时按设置触发内存索引预热；开启时需等到 ready/failed。
 * @returns {Promise<"ready"|"failed"|"skipped"|"idle">}
 */
async function ensureSearchPreheatForOpen() {
  if (!state.compared || !state.oldPath || !state.newPath) {
    return "idle";
  }
  const key = _currentSearchPreheatKey();
  // 换过快照对：上一对的 ready 一律作废
  if (state.searchPreheatKey && state.searchPreheatKey !== key) {
    state.searchPreheat = "idle";
    state.searchPreheatKey = "";
  }
  // 设置关闭：不预热
  if (!state.searchMemoryIndex) {
    state.searchPreheat = "skipped";
    state.searchPreheatKey = key;
    renderSearchPreheatStatus();
    return "skipped";
  }
  if (state.searchPreheat === "ready" && state.searchPreheatKey === key) {
    renderSearchPreheatStatus();
    return "ready";
  }
  // failed 不在这里直接返回：允许再次打开搜索时重试预热

  // 已在进行中且仍是当前快照对：等事件（带轮询兜底）
  if (state.searchPreheat === "started" && state.searchPreheatKey === key) {
    renderSearchPreheatStatus();
    return await _waitPreheatTerminal();
  }

  state.searchPreheat = "started";
  state.searchPreheatKey = key;
  renderSearchPreheatStatus();

  try {
    const res = await state.api.start_search_preheat(state.oldPath, state.newPath);
    // 请求返回期间用户可能又换了对
    if (_currentSearchPreheatKey() !== key) {
      return "idle";
    }
    if (res && res.error) {
      state.searchPreheat = "failed";
      state.searchPreheatKey = key;
      renderSearchPreheatStatus();
      return "failed";
    }
    const st = (res && res.status) || "started";
    if (st === "skipped") {
      state.searchPreheat = "skipped";
      state.searchMemoryIndex = false;
      state.searchPreheatKey = key;
      renderSearchPreheatStatus();
      return "skipped";
    }
    if (st === "ready") {
      state.searchPreheat = "ready";
      state.searchPreheatKey = key;
      renderSearchPreheatStatus();
      return "ready";
    }
    // started：等事件（带轮询兜底）
    state.searchPreheat = "started";
    state.searchPreheatKey = key;
    renderSearchPreheatStatus();
    return await _waitPreheatTerminal();
  } catch (_) {
    if (_currentSearchPreheatKey() !== key) return "idle";
    state.searchPreheat = "failed";
    state.searchPreheatKey = key;
    renderSearchPreheatStatus();
    return "failed";
  }
}

/** 等待预热结束：事件优先，定时查后端状态兜底。 */
function _waitPreheatTerminal() {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (st) => {
      if (settled) return;
      settled = true;
      if (pollTimer) clearInterval(pollTimer);
      resolve(st);
    };
    _preheatWaiters.push(finish);
    const pollTimer = setInterval(async () => {
      if (settled) return;
      if (!state.compared) {
        finish("idle");
        return;
      }
      // 事件已先更新 state
      if (state.searchPreheat === "ready" || state.searchPreheat === "failed"
          || state.searchPreheat === "idle" || state.searchPreheat === "skipped") {
        finish(state.searchPreheat);
        return;
      }
      try {
        const res = await state.api.search_preheat_status(state.oldPath, state.newPath);
        const st = res && res.status;
        if (st === "ready" || st === "failed" || st === "skipped") {
          state.searchPreheat = st;
          renderSearchPreheatStatus();
          _resolvePreheatWaiters(st);
        }
      } catch (_) { /* 忽略轮询失败 */ }
    }, 400);
  });
}

/** 过滤判定：某节点在当前过滤下是否显示。 */
function matchFilter(node) {
  // 搜索定位时临时展示全部节点（含大小未变），否则中间路径会被藏掉
  if (state._showAllForLocate) return true;
  if (node.kind === "incomparable") return state.filter !== "shrank";
  if (state.filter === "grew") return node.delta > 0;
  if (state.filter === "shrank") return node.delta < 0;
  return node.delta !== 0; // all：隐藏「大小未变」的噪声
}

// ---- 排序 ----

/** 变化百分比（|delta| / 旧大小）；旧不存在视为无穷大（新增即 +100%+）。 */
function deltaPct(n) {
  if (n.old_size > 0) return Math.abs(n.delta) / n.old_size;
  return n.delta !== 0 ? Infinity : 0;
}

const SORTERS = {
  "delta-desc": (a, b) => Math.abs(b.delta) - Math.abs(a.delta),
  "pct-desc": (a, b) =>
    (deltaPct(b) - deltaPct(a)) || (Math.abs(b.delta) - Math.abs(a.delta)),
  "name-asc": (a, b) =>
    (a.name || a.path).localeCompare(b.name || b.path, cmpLocale()),
  "name-desc": (a, b) =>
    (b.name || b.path).localeCompare(a.name || a.path, cmpLocale()),
  // v1 旧快照没有 mtime（为 0），自然沉底。
  "mtime-desc": (a, b) => (b.mtime || 0) - (a.mtime || 0),
};

function renderTopLevel(nodes) {
  state._topNodes = nodes;
  // 顶层最大变化量：作为「顶层基准」模式下整棵树统一的条长标尺，
  // 取全部顶层节点（不受筛选影响），保证切换筛选时条长不跳变。
  state._barRef = nodes.reduce((m, n) => Math.max(m, Math.abs(n.delta)), 1);
  const tree = $("#tree");
  tree.innerHTML = "";
  const frag = buildLevel(nodes, 0);
  tree.appendChild(frag);
  if (!tree.querySelector(".node")) {
    tree.innerHTML = `<div class="child-loading">${t("noMatchTop")}</div>`;
  }
}

/** 由一层节点数据构建 DOM 片段（含展开/懒加载逻辑）。 */
function buildLevel(nodes, depth) {
  const frag = document.createDocumentFragment();
  const visible = nodes.filter(matchFilter);
  visible.sort(SORTERS[state.sort] || SORTERS["delta-desc"]);
  // 条长统一按顶层最大变化量，保证切换筛选时比例不跳变
  const ref = state._barRef || 1;

  const renderCount = Math.min(visible.length, PER_LEVEL_CAP);
  for (let i = 0; i < renderCount; i++) {
    frag.appendChild(buildNode(visible[i], depth, ref));
  }
  if (visible.length > PER_LEVEL_CAP) {
    const more = document.createElement("div");
    more.className = "show-more";
    let shown = renderCount;
    more.textContent = t("showMore", visible.length - shown);
    more.onclick = () => {
      const next = Math.min(visible.length, shown + PER_LEVEL_CAP);
      const f = document.createDocumentFragment();
      for (let i = shown; i < next; i++) f.appendChild(buildNode(visible[i], depth, ref));
      more.parentNode.insertBefore(f, more);
      shown = next;
      if (shown >= visible.length) more.remove();
      else more.textContent = t("showMore", visible.length - shown);
    };
    frag.appendChild(more);
  }
  return frag;
}

/** 构建单个节点（行 + 可能的子容器）。 */
function buildNode(node, depth, ref) {
  const group = document.createElement("div");
  group.className = "node-group";
  group.dataset.path = node.path || "";

  const kindClass =
    node.kind === "incomparable" ? "incomparable"
    : node.delta > 0 ? "grow"
    : node.delta < 0 ? "shrink"
    : "unchanged";

  const row = document.createElement("div");
  row.className = `node ${kindClass}${node.is_dir ? " dir" : ""}`;
  row.dataset.path = node.path || "";
  row.style.paddingLeft = `${14 + depth * 20}px`;

  const canExpand = node.is_dir && node.has_children;
  const barPct = Math.max(2, Math.min(100, Math.round((Math.abs(node.delta) / ref) * 100)));
  const deltaText =
    node.kind === "incomparable" ? t("incomparable") : fmtDelta(node.delta);
  // 只存在于一侧的内容单独打标，回答「多/少了什么」。
  const tag =
    node.kind === "added" ? `<span class="node-tag added">${t("tagAdded")}</span>`
    : node.kind === "removed" ? `<span class="node-tag removed">${t("tagRemoved")}</span>`
    : "";

  row.innerHTML = `
    <span class="twisty">${canExpand ? "▸" : ""}</span>
    <span class="node-icon">${node.is_dir ? "📁" : "📄"}</span>
    <span class="node-name">${escapeHtml(node.name || node.path)}</span>
    ${tag}
    <span class="node-fill"></span>
    <span class="node-bar"><i style="width:${barPct}%"></i></span>
    <span class="node-delta">${deltaText}</span>`;

  const children = document.createElement("div");
  children.className = "children hidden";

  if (canExpand) {
    row.onclick = () => toggleDir(node, row, children, depth);
  }
  // 悬停显示明细；右键出菜单（定位/复制路径）。
  row.title =
    `${fmtBytes(node.old_size)} → ${fmtBytes(node.new_size)}` +
    (node.mtime ? t("mtimeLine", fmtTime(node.mtime)) : "");
  row.oncontextmenu = (e) => {
    e.preventDefault();
    openCtxMenu(e, node);
  };

  group.appendChild(row);
  group.appendChild(children);
  return group;
}

/** 将整棵对比树收起到顶层（仅隐藏展开态，不卸载已加载子节点）。 */
function collapseAllTree() {
  const tree = $("#tree");
  if (!tree) return;
  for (const ch of tree.querySelectorAll(".children")) {
    ch.classList.add("hidden");
  }
  for (const tw of tree.querySelectorAll(".twisty.open")) {
    tw.classList.remove("open");
  }
}

// ---- 对比树搜索 ----

const SEARCH_PAGE_SIZE = 50;

let _searchTimer = 0;
let _searchSeq = 0;
let _searchQuery = "";
let _searchOffset = 0;
let _searchTotal = 0;
let _searchLoadingMore = false;
let _searchInFlight = false;

function _normTreePath(p) {
  return String(p || "").replace(/\//g, "\\");
}

function _setSearchMoreVisible(show) {
  const wrap = $("#searchMoreWrap");
  if (wrap) wrap.classList.toggle("hidden", !show);
  const btn = $("#searchMoreBtn");
  if (btn) btn.disabled = false;
}

/** 同步搜索选项勾选与 title（不触发搜索）。 */
function syncSearchOptionsChrome() {
  const caseChk = $("#searchCaseChk");
  if (caseChk) {
    caseChk.checked = !!state.searchCaseSensitive;
    const lab = caseChk.closest("label");
    if (lab) lab.title = t("treeSearchCaseTitle");
  }
  const exactChk = $("#searchExactChk");
  if (exactChk) {
    exactChk.checked = !!state.searchExact;
    const lab = exactChk.closest("label");
    if (lab) lab.title = t("treeSearchExactTitle");
  }
}

/**
 * 切换搜索选项。
 * 区分大小写/严格匹配是最宽结果的子集，后端走同一关键词缓存后内存过滤，
 * 这里仍调一次接口以刷新分页与 total，不会重新扫库。
 */
function setSearchOption(key, value) {
  if (key === "case") state.searchCaseSensitive = !!value;
  else if (key === "exact") state.searchExact = !!value;
  else return;
  syncSearchOptionsChrome();
  if (_searchQuery) {
    const input = $("#treeSearchInput");
    runTreeSearch(input ? input.value : _searchQuery);
  }
}

function syncTreeSearchChrome() {
  const wrap = $("#treeSearchWrap");
  const input = $("#treeSearchInput");
  const clearBtn = $("#treeSearchClear");
  const toggle = $("#treeSearchToggle");
  const hasText = !!(input && String(input.value || "").trim());
  const hasResult = !!_searchQuery;
  const open = !!(wrap && wrap.classList.contains("is-open"));
  if (clearBtn) clearBtn.classList.toggle("hidden", !hasText);
  if (wrap) wrap.classList.toggle("is-active", hasResult || hasText);
  if (toggle) {
    toggle.classList.toggle("is-active", hasResult || hasText);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (input) input.tabIndex = open ? 0 : -1;
  syncSearchOptionsChrome();
}

async function openTreeSearch({ focus = true } = {}) {
  const wrap = $("#treeSearchWrap");
  if (!wrap) return;
  wrap.classList.add("is-open");
  syncTreeSearchChrome();
  // 打开搜索框才触发内存索引预热；开启时需等准备完成才能输入搜索
  const preheatSt = await ensureSearchPreheatForOpen();
  // 若期间又被收起，不再抢焦点
  if (!wrap.classList.contains("is-open")) return;
  syncTreeSearchChrome();
  renderSearchPreheatStatus();
  if (focus) {
    const input = $("#treeSearchInput");
    if (input && preheatSt !== "started") {
      requestAnimationFrame(() => {
        if (!wrap.classList.contains("is-open")) return;
        // 准备中 readOnly，仍可聚焦看状态；完成后可输入
        input.focus();
        if (!input.readOnly) input.select();
      });
    }
  }
}

function collapseTreeSearch({ clear = false } = {}) {
  if (clear) clearTreeSearch({ keepInput: false });
  const wrap = $("#treeSearchWrap");
  if (wrap) wrap.classList.remove("is-open");
  const input = $("#treeSearchInput");
  if (input) input.blur();
  syncTreeSearchChrome();
}

/** 有搜索请求在后端进行中时，通知其强行中断（空闲时是无害空调用）。 */
function cancelTreeSearchBackend() {
  if (!_searchInFlight) return;
  try {
    const p = state.api.cancel_search && state.api.cancel_search();
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch (_) { /* 后端不可用时忽略 */ }
}

function clearTreeSearch({ keepInput = false } = {}) {
  _searchSeq += 1;
  cancelTreeSearchBackend();
  if (_searchTimer) {
    clearTimeout(_searchTimer);
    _searchTimer = 0;
  }
  _searchQuery = "";
  _searchOffset = 0;
  _searchTotal = 0;
  _searchLoadingMore = false;
  const panel = $("#searchPanel");
  const list = $("#searchList");
  const meta = $("#searchMeta");
  if (panel) panel.classList.add("hidden");
  if (list) list.innerHTML = "";
  if (meta) meta.textContent = "";
  _setSearchMoreVisible(false);
  if (!keepInput) {
    const input = $("#treeSearchInput");
    if (input) input.value = "";
  }
  syncTreeSearchChrome();
}

/** 仅同步清除钮显示，不触发搜索（搜索需回车确认）。 */
function onTreeSearchInput() {
  syncTreeSearchChrome();
}

async function runTreeSearch(raw, { append = false } = {}) {
  const q = String(raw || "").trim();
  const panel = $("#searchPanel");
  const list = $("#searchList");
  const meta = $("#searchMeta");
  if (!panel || !list || !meta) return;

  if (!q) {
    clearTreeSearch({ keepInput: true });
    return;
  }
  // 内存索引开启且仍在准备：必须等完成（打开搜索框时已触发）
  if (state.searchMemoryIndex && state.searchPreheat === "started") {
    panel.classList.remove("hidden");
    list.innerHTML = "";
    meta.textContent = t("searchPreheatStarted");
    _setSearchMoreVisible(false);
    return;
  }
  // 单个 ASCII 字符（字母/数字/符号）匹配面过大，要求再补充；单个汉字等宽字符放行
  if (q.length === 1 && q.charCodeAt(0) < 128) {
    panel.classList.remove("hidden");
    list.innerHTML = "";
    meta.textContent = t("treeSearchTooBroad");
    _setSearchMoreVisible(false);
    return;
  }
  if (!state.compared || !state.oldPath || !state.newPath) {
    panel.classList.remove("hidden");
    list.innerHTML = "";
    meta.textContent = t("treeSearchNeedCompare");
    _setSearchMoreVisible(false);
    return;
  }

  if (append && _searchLoadingMore) return;

  const offset = append ? _searchOffset : 0;
  if (!append) {
    _searchQuery = q;
    _searchOffset = 0;
    _searchTotal = 0;
  } else if (q !== _searchQuery) {
    // 关键词已变：走首搜
    return runTreeSearch(q, { append: false });
  }

  const seq = ++_searchSeq;
  // 上一个搜索还在后端跑：先强行中断，避免新旧搜索排队且旧的白耗 CPU
  if (!append) cancelTreeSearchBackend();
  panel.classList.remove("hidden");
  if (!append) {
    // 状态只写在 meta，避免 list 里再塞一份「正在搜索」
    meta.textContent = t("treeSearchSearching");
    list.innerHTML = "";
    _setSearchMoreVisible(false);
  } else {
    _searchLoadingMore = true;
    const moreBtn = $("#searchMoreBtn");
    if (moreBtn) {
      moreBtn.disabled = true;
      moreBtn.textContent = t("treeSearchSearching");
    }
  }

  const t0 = (typeof performance !== "undefined" && performance.now)
    ? performance.now()
    : Date.now();

  let res;
  _searchInFlight = true;
  try {
    res = await state.api.search_diff(
      state.oldPath, state.newPath, q, SEARCH_PAGE_SIZE, offset,
      state.searchSort || "delta-desc",
      !!state.searchCaseSensitive,
      !!state.searchExact
    );
  } catch (err) {
    if (seq !== _searchSeq) return;
    _searchLoadingMore = false;
    if (!append) list.innerHTML = "";
    meta.textContent = t("treeSearchFailed", String(err));
    _setSearchMoreVisible(false);
    return;
  } finally {
    _searchInFlight = false;
  }
  if (seq !== _searchSeq) return;
  // 被取消的搜索：结果已无人需要，静默丢弃
  if (res && res.cancelled) return;

  const clientMs = Math.max(0, Math.round(
    ((typeof performance !== "undefined" && performance.now)
      ? performance.now()
      : Date.now()) - t0
  ));

  if (res && res.error) {
    _searchLoadingMore = false;
    if (!append) list.innerHTML = "";
    meta.textContent = t("treeSearchFailed", res.error);
    _setSearchMoreVisible(false);
    return;
  }

  const nodes = (res && res.nodes) || [];
  const total = (res && typeof res.total === "number") ? res.total : nodes.length;
  const elapsedMs = (res && typeof res.elapsed_ms === "number")
    ? res.elapsed_ms
    : clientMs;

  if (!append) list.innerHTML = "";

  if (!append && !nodes.length) {
    _searchTotal = 0;
    _searchOffset = 0;
    meta.textContent = t("treeSearchEmpty");
    _setSearchMoreVisible(false);
    _searchLoadingMore = false;
    return;
  }

  const frag = document.createDocumentFragment();
  for (const node of nodes) {
    frag.appendChild(buildSearchItem(node, q));
  }
  list.appendChild(frag);

  _searchTotal = total;
  _searchOffset = offset + nodes.length;
  _searchLoadingMore = false;

  const shown = _searchOffset;
  meta.textContent = t("treeSearchMeta", shown, total, q, elapsedMs);
  _setSearchMoreVisible(shown < total);
  const moreBtn = $("#searchMoreBtn");
  if (moreBtn) {
    moreBtn.disabled = false;
    moreBtn.textContent = t("treeSearchMore");
  }
}

function loadMoreTreeSearch() {
  if (!_searchQuery || _searchOffset >= _searchTotal) return;
  runTreeSearch(_searchQuery, { append: true });
}

/**
 * 高亮关键词，返回安全 HTML。
 * 跟随搜索选项：严格匹配只整串相等时高亮；区分大小写则按原样比对。
 */
function highlightQueryHtml(text, query) {
  const src = String(text || "");
  const q = String(query || "").trim();
  if (!src) return "";
  if (!q) return escapeHtml(src);
  const cs = !!state.searchCaseSensitive;
  const exact = !!state.searchExact;
  if (exact) {
    const ok = cs ? src === q : src.toLowerCase() === q.toLowerCase();
    return ok
      ? `<mark class="si-hit">${escapeHtml(src)}</mark>`
      : escapeHtml(src);
  }
  const hay = cs ? src : src.toLowerCase();
  const needle = cs ? q : q.toLowerCase();
  let out = "";
  let i = 0;
  while (i < src.length) {
    const hit = hay.indexOf(needle, i);
    if (hit < 0) {
      out += escapeHtml(src.slice(i));
      break;
    }
    if (hit > i) out += escapeHtml(src.slice(i, hit));
    out += `<mark class="si-hit">${escapeHtml(src.slice(hit, hit + q.length))}</mark>`;
    i = hit + Math.max(1, q.length);
  }
  return out;
}

/**
 * 路径展示：命中段完整显示并高亮；前后过长用 … 省略。
 * 例：…\\xxxx\\[aaaa命中bbbb]\\cccc
 */
function formatSearchPathHtml(path, query) {
  const norm = _normTreePath(path);
  const parts = norm.split("\\").filter(Boolean);
  if (!parts.length) return "";

  const q = String(query || "").trim();
  const cs = !!state.searchCaseSensitive;
  const exact = !!state.searchExact;
  const sepInQuery = /[\\/]/.test(q);

  const nameMatches = (p) => {
    if (!q) return false;
    if (exact) return cs ? p === q : p.toLowerCase() === q.toLowerCase();
    return cs ? p.includes(q) : p.toLowerCase().includes(q.toLowerCase());
  };

  // 优先：路径子串命中所在的段；否则名字段；否则第一段包含关键词
  let hitIdx = parts.length - 1;
  if (sepInQuery && q) {
    const qPath = q.replace(/\//g, "\\");
    const full = parts.join("\\");
    const hay = cs ? full : full.toLowerCase();
    const needle = cs ? qPath : qPath.toLowerCase();
    let qi = -1;
    if (exact) {
      qi = hay === needle ? 0 : -1;
    } else {
      qi = hay.indexOf(needle);
    }
    if (qi >= 0) {
      let acc = 0;
      for (let i = 0; i < parts.length; i++) {
        const end = acc + parts[i].length;
        if (qi < end) {
          hitIdx = i;
          break;
        }
        acc = end + 1; // + sep
      }
    }
  } else if (q) {
    const nameHit = parts.findIndex((p) => nameMatches(p));
    if (nameHit >= 0) hitIdx = nameHit;
  }

  const MAX_HEAD = 18;
  const MAX_TAIL = 14;
  const segs = [];
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if (i === hitIdx) {
      segs.push(highlightQueryHtml(part, q));
      continue;
    }
    // 命中段前后各保留有限长度；更远的段折叠
    if (i < hitIdx - 1) {
      if (i === 0) segs.push("…");
      continue;
    }
    if (i > hitIdx + 1) {
      if (i === parts.length - 1) segs.push(escapeHtml(
        part.length > MAX_TAIL ? `…${part.slice(-MAX_TAIL)}` : part
      ));
      else if (i === hitIdx + 2) segs.push("…");
      continue;
    }
    // 紧邻命中段：可截断显示
    if (i === hitIdx - 1) {
      const shown = part.length > MAX_HEAD ? `…${part.slice(-MAX_HEAD)}` : part;
      segs.push(escapeHtml(shown));
    } else if (i === hitIdx + 1) {
      const shown = part.length > MAX_TAIL ? `${part.slice(0, MAX_TAIL)}…` : part;
      segs.push(escapeHtml(shown));
    }
  }
  return segs.join("\\");
}

function buildSearchItem(node, query) {
  const el = document.createElement("div");
  el.className = "search-item";
  const kindClass =
    node.kind === "incomparable" ? "muted"
    : node.delta > 0 ? "grow"
    : node.delta < 0 ? "shrink"
    : "muted";
  const deltaText =
    node.kind === "incomparable" ? t("incomparable") : fmtDelta(node.delta);
  const name = node.name || node.path || "";
  const path = node.path || "";
  el.innerHTML =
    `<span class="si-name" title="${escapeHtml(name)}">${highlightQueryHtml(name, query)}</span>` +
    `<span class="si-path" title="${escapeHtml(path)}">${formatSearchPathHtml(path, query)}</span>` +
    `<span class="si-delta ${kindClass}">${deltaText}</span>`;
  el.onclick = () => locateTreePath(node.path);
  el.oncontextmenu = (e) => {
    e.preventDefault();
    openCtxMenu(e, node);
  };
  return el;
}

/**
 * 沿路径逐段展开对比树并滚动到目标节点。
 * 临时展示「含未变节点」，否则中间路径在默认筛选下会被隐藏。
 */
async function locateTreePath(targetPath) {
  if (!targetPath || !state.compared) return;
  const normTarget = _normTreePath(targetPath);
  const parts = normTarget.split("\\").filter(Boolean);
  if (!parts.length) return;

  const tree = $("#tree");
  if (!tree) return;

  const prevShowAll = !!state._showAllForLocate;
  state._showAllForLocate = true;
  try {
    // 重新渲染顶层，带上未变节点
    if (state._topNodes) renderTopLevel(state._topNodes);

    let prefix = "";
    for (let i = 0; i < parts.length; i++) {
      prefix = prefix ? `${prefix}\\${parts[i]}` : parts[i];
      const isLast = i === parts.length - 1;
      let row = tree.querySelector(`.node[data-path="${cssEscapeAttr(prefix)}"]`);

      // 本层可能被 PER_LEVEL_CAP 截断：点「显示更多」直到出现或耗尽
      if (!row) {
        row = await revealCappedNode(tree, prefix);
      }
      if (!row) {
        toast(t("treeSearchLocateFailed", prefix), true);
        return;
      }
      if (!isLast) {
        const group = row.parentElement;
        const children = group && group.querySelector(":scope > .children");
        if (children) {
          const needOpen =
            children.classList.contains("hidden") ||
            children.dataset.loaded !== "1";
          if (needOpen) {
            row.click();
            await waitForChildrenLoaded(children);
          }
        }
      } else {
        row.classList.remove("flash-hit");
        void row.offsetWidth;
        row.classList.add("flash-hit");
        row.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }
  } finally {
    state._showAllForLocate = prevShowAll;
    // 定位结束后恢复当前筛选视图，但保留已展开的 DOM 以免整树折叠
    // （仅顶层若需严格一致可再 renderTopLevel；这里不重绘以免丢掉展开状态）
  }
}

/** 在当前树中点击「显示更多」直到出现指定 path 的节点，或没有更多。 */
async function revealCappedNode(tree, path) {
  const sel = `.node[data-path="${cssEscapeAttr(path)}"]`;
  let row = tree.querySelector(sel);
  if (row) return row;
  // 只处理顶层或已展开层中仍挂着的 show-more
  let guard = 0;
  while (guard++ < 50) {
    const more = tree.querySelector(".show-more");
    if (!more) break;
    more.click();
    row = tree.querySelector(sel);
    if (row) return row;
    await new Promise((r) => setTimeout(r, 0));
  }
  return tree.querySelector(sel);
}

function cssEscapeAttr(s) {
  // 路径里 \ 在 CSS 属性选择器中需转义
  if (window.CSS && typeof CSS.escape === "function") return CSS.escape(s);
  return String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function waitForChildrenLoaded(childrenEl, timeoutMs = 8000) {
  return new Promise((resolve) => {
    if (!childrenEl) {
      resolve(false);
      return;
    }
    if (childrenEl.dataset.loaded === "1" || childrenEl.querySelector(".child-error")) {
      resolve(true);
      return;
    }
    const t0 = Date.now();
    const tick = () => {
      if (childrenEl.dataset.loaded === "1" || childrenEl.querySelector(".child-error")) {
        resolve(true);
        return;
      }
      if (Date.now() - t0 > timeoutMs) {
        resolve(false);
        return;
      }
      setTimeout(tick, 40);
    };
    setTimeout(tick, 40);
  });
}

// ---- 右键菜单 ----

function openCtxMenu(e, node) {
  state.ctxNode = node;
  const menu = $("#ctxMenu");
  menu.classList.remove("hidden");
  // 贴着鼠标放，出界则往回收。
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  menu.style.left = `${Math.min(e.clientX, window.innerWidth - mw - 6)}px`;
  menu.style.top = `${Math.min(e.clientY, window.innerHeight - mh - 6)}px`;
}

function closeCtxMenu() {
  $("#ctxMenu").classList.add("hidden");
  state.ctxNode = null;
}

async function ctxCommand(cmd) {
  const node = state.ctxNode;
  closeCtxMenu();
  if (!node) return;
  if (cmd === "reveal") {
    const res = await state.api.reveal_path(state.compareRoot, node.path);
    if (res.error) toast(res.error, true);
    else if (res.message) toast(res.message);
  } else if (cmd === "copy") {
    const p = fullPath(state.compareRoot, node.path);
    toast((await copyText(p)) ? t("copied", p) : t("copyFailed"), false);
  } else if (cmd === "delete") {
    if (typeof addCompareNodeToPending === "function") {
      addCompareNodeToPending(node);
    } else {
      toast(t("deleteFail"), true);
    }
  } else if (cmd === "ask-ai") {
    if (typeof askAiAboutNode === "function") {
      askAiAboutNode(node);
    } else {
      toast(t("aiModuleMissing"), true);
    }
  } else if (cmd === "cleanup-ai") {
    if (typeof startCompareCleanupFromNode === "function") {
      startCompareCleanupFromNode(node);
    } else {
      toast(t("aiModuleMissing"), true);
    }
  }
}

async function toggleDir(node, row, children, depth) {
  const twisty = row.querySelector(".twisty");
  const isOpen = !children.classList.contains("hidden");
  if (isOpen) {
    children.classList.add("hidden");
    twisty.classList.remove("open");
    return;
  }
  twisty.classList.add("open");
  children.classList.remove("hidden");

  if (children.dataset.loaded !== "1") {
    children.innerHTML = `<div class="child-loading">${t("loading")}</div>`;
    let res;
    try {
      res = await state.api.get_children(state.oldPath, state.newPath, node.path);
    } catch (err) {
      // 后端调用本身失败（而非业务 error）也要落地成可见提示，
      // 否则「加载中」会永远挂着。收起后可再点重试。
      children.innerHTML = `<div class="child-error">${escapeHtml(t("loadFailed", String(err)))}</div>`;
      return;
    }
    children.innerHTML = "";
    if (res.error) {
      children.innerHTML = `<div class="child-error">${escapeHtml(t("loadFailed", res.error))}</div>`;
      return;
    }
    children.appendChild(buildLevel(res.nodes, depth + 1));
    children.dataset.loaded = "1";
    if (!children.querySelector(".node")) {
      children.innerHTML = `<div class="child-loading">${t("noMatchChild")}</div>`;
    }
  }
}

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


function isBrowseMode() {
  return state.treeMode === "browse";
}

function isTreeMultiSelectMode() {
  return !!state.treeMultiSelect;
}

// 连按两下 Esc 退出多选的两次按键最大间隔
const _TREE_ESC_DOUBLE_MS = 500;
let _treeEscAt = 0;

function setTreeMultiSelectMode(on) {
  state.treeMultiSelect = !!on;
  _treeEscAt = 0;
  const btn = $("#treeMultiSelectBtn");
  if (btn) {
    btn.classList.toggle("is-active", state.treeMultiSelect);
    btn.setAttribute("aria-pressed", state.treeMultiSelect ? "true" : "false");
    btn.title = t("treeMultiSelectTitle");
  }
  // 关闭多选时清掉已选，避免残留高亮干扰普通浏览
  if (!state.treeMultiSelect && typeof clearTreeSelection === "function") {
    clearTreeSelection();
  } else if (typeof syncTreeSelectBar === "function") {
    syncTreeSelectBar();
  }
}

function toggleTreeMultiSelectMode() {
  setTreeMultiSelectMode(!state.treeMultiSelect);
  if (typeof toast === "function") {
    toast(state.treeMultiSelect ? t("treeMultiSelectOn") : t("treeMultiSelectOff"));
  }
}

/**
 * 全局 Esc 在弹窗、菜单都关掉之后调用：设置开启且处于多选时，连按两下退出多选。
 * 单按一下只提示，不和关弹窗的 Esc 抢意思。
 */
function handleTreeMultiSelectEscape() {
  if (!state.treeMultiSelect) {
    _treeEscAt = 0;
    return;
  }
  const s = state._settings;
  if (s && s.ui_multiselect_double_esc === false) {
    _treeEscAt = 0;
    return;
  }
  const now = Date.now();
  if (now - _treeEscAt <= _TREE_ESC_DOUBLE_MS) {
    _treeEscAt = 0;
    setTreeMultiSelectMode(false);
    if (typeof toast === "function") toast(t("treeMultiSelectOff"));
    return;
  }
  _treeEscAt = now;
  if (typeof toast === "function") toast(t("treeMultiSelectEscHint"));
}

/** 多选里把选中项加入待删除后，按设置退出多选；本来不在多选就不动。 */
function exitTreeMultiSelectAfterPendingAdd() {
  if (!state.treeMultiSelect) return;
  const s = state._settings;
  if (s && s.ui_multiselect_exit_after_add === false) return;
  setTreeMultiSelectMode(false);
}

function treeSelectionCount() {
  const bag = state.treeSelected || {};
  return Object.keys(bag).length;
}

function clearTreeSelection() {
  state.treeSelected = {};
  state._treeSelectAnchor = "";
  document.querySelectorAll("#tree .node.is-selected").forEach((el) => {
    el.classList.remove("is-selected");
  });
  syncTreeSelectBar();
}

function selectedTreeNodes() {
  const bag = state.treeSelected || {};
  return Object.keys(bag).map((k) => bag[k]).filter(Boolean);
}

function syncTreeSelectBar() {
  const bar = $("#treeSelectBar");
  const meta = $("#treeSelectMeta");
  const n = treeSelectionCount();
  if (!bar) return;
  if (n <= 0 || !state.compared) {
    bar.classList.add("hidden");
    if (meta) meta.textContent = "";
    return;
  }
  bar.classList.remove("hidden");
  if (meta) meta.textContent = t("treeSelectMeta", n);
}

function _treePathKey(path) {
  return String(path || "").replace(/\\/g, "/").replace(/\/+/g, "/");
}

function _nodeSelectPayload(node) {
  const oldSize = Number(node.old_size) || 0;
  const newSize = Number(node.new_size) || 0;
  return {
    path: node.path || "",
    name: node.name || node.path || "",
    is_dir: !!node.is_dir,
    new_size: newSize,
    old_size: oldSize,
    delta: Number(node.delta) || (newSize - oldSize),
  };
}

function setTreeNodeSelected(node, selected) {
  if (!node || !node.path) return;
  if (!state.treeSelected) state.treeSelected = {};
  const key = _treePathKey(node.path);
  if (selected) state.treeSelected[key] = _nodeSelectPayload(node);
  else delete state.treeSelected[key];
  const row = document.querySelector(
    `#tree .node[data-path="${typeof cssEscapeAttr === "function" ? cssEscapeAttr(node.path) : String(node.path).replace(/"/g, '\\"')}"]`
  );
  if (row) row.classList.toggle("is-selected", !!selected);
}

function applyTreeSelectionToDom() {
  const bag = state.treeSelected || {};
  document.querySelectorAll("#tree .node").forEach((row) => {
    const p = row.dataset.path || "";
    const on = !!bag[_treePathKey(p)];
    row.classList.toggle("is-selected", on);
  });
  syncTreeSelectBar();
}

function toggleTreeSelection(node, { range } = {}) {
  if (!node || !node.path) return;
  if (!state.treeSelected) state.treeSelected = {};
  const key = _treePathKey(node.path);
  if (range && state._treeSelectAnchor) {
    // Shift 圈选只看同一层（同一父文件夹的直接子项）的行；
    // 锚点或目标不在 DOM、或不在同一层时，回退为单选目标项。
    const rows = siblingRows(state._treeSelectAnchor, node.path);
    if (rows) {
      const paths = rows.map((r) => r.dataset.path || "");
      const a = paths.indexOf(state._treeSelectAnchor);
      const b = paths.indexOf(node.path);
      if (a >= 0 && b >= 0) {
        const lo = Math.min(a, b);
        const hi = Math.max(a, b);
        for (let i = lo; i <= hi; i++) {
          const row = rows[i];
          const p = row.dataset.path || "";
          if (!p) continue;
          // 优先用会话索引里的完整节点数据（含大小字段）；
          // 行数据本身不够 size，索引缺失时才降级为仅 path/name。
          const fullNode = state._pathNodeMap && state._pathNodeMap[_treePathKey(p)];
          const nameEl = row.querySelector(".node-name");
          state.treeSelected[_treePathKey(p)] = fullNode
            ? _nodeSelectPayload(fullNode)
            : {
                path: p,
                name: nameEl ? nameEl.textContent : p,
                is_dir: row.classList.contains("dir"),
                new_size: 0,
                old_size: 0,
              };
          row.classList.add("is-selected");
        }
        syncTreeSelectBar();
        return;
      }
    }
  }
  if (state.treeSelected[key]) {
    delete state.treeSelected[key];
  } else {
    state.treeSelected[key] = _nodeSelectPayload(node);
  }
  state._treeSelectAnchor = node.path || "";
  applyTreeSelectionToDom();
}

/**
 * Shift 圈选时取两行所在层的兄弟行序列。
 * 每个节点一行、独占一个 .node-group；同一层的行，其 group 挂在同一个
 * 容器下（顶层是 #tree，子层是父行的 .children）。两行须同在 DOM 且
 * 各自 group 的父容器相同（同一层）；否则返回 null。
 */
function siblingRows(anchorPath, targetPath) {
  if (!anchorPath || !targetPath) return null;
  const sel = (p) => `#tree .node[data-path="${cssEscapeAttr(p)}"]`;
  const anchorRow = document.querySelector(sel(anchorPath));
  const targetRow = document.querySelector(sel(targetPath));
  if (!anchorRow || !targetRow) return null;
  const anchorGroup = anchorRow.parentElement;
  const targetGroup = targetRow.parentElement;
  if (!anchorGroup || !targetGroup) return null;
  const container = anchorGroup.parentElement;
  if (!container || container !== targetGroup.parentElement) return null;
  return Array.from(container.querySelectorAll(":scope > .node-group > .node"));
}


function nodeSize(n) {
  const v = Number(n && n.new_size);
  return Number.isFinite(v) && v > 0 ? v : 0;
}

/** 缓存键：统一分隔符，避免同一目录因 \ / 混用而 miss。 */
function treePathKey(path) {
  return String(path || "").replace(/\\/g, "/").replace(/\/+/g, "/").replace(/^\//, "");
}

/** 当前主树应使用的排序键（对比 / 展开各自独立）。 */
function currentTreeSort() {
  if (isBrowseMode()) {
    const s = state.browseSort || "size-desc";
    if (s === "delta-desc" || s === "pct-desc") return "size-desc";
    return s;
  }
  const s = state.sort || "delta-desc";
  if (s === "size-desc") return "delta-desc";
  return s;
}

/** 搜索结果排序键：展开模式默认按占用，对比模式默认按变化量。 */
function currentSearchSort() {
  let s = state.searchSort || (isBrowseMode() ? "size-desc" : "delta-desc");
  if (isBrowseMode()) {
    if (s === "delta-desc" || s === "pct-desc") s = "size-desc";
  } else if (s === "size-desc") {
    s = "delta-desc";
  }
  return s;
}

function clearChildrenCache() {
  state._childrenCache = {};
  state._openPaths = {};
  state._childrenInflight = {};
}

function cacheChildren(path, nodes) {
  if (!state._childrenCache || typeof state._childrenCache !== "object" || Array.isArray(state._childrenCache)) {
    state._childrenCache = {};
  }
  // 防止误写成 Object 构造器
  if (state._childrenCache === Object) state._childrenCache = {};
  state._childrenCache[treePathKey(path)] = Array.isArray(nodes) ? nodes : [];
}

function cachedChildren(path) {
  const bag = state._childrenCache;
  if (!bag || bag === Object || typeof bag !== "object") return null;
  const key = treePathKey(path);
  return Object.prototype.hasOwnProperty.call(bag, key) ? bag[key] : null;
}

function markPathOpen(path, open) {
  if (!state._openPaths || state._openPaths === Object) state._openPaths = {};
  const key = treePathKey(path);
  if (open) state._openPaths[key] = true;
  else delete state._openPaths[key];
}

function isPathOpen(path) {
  return !!(state._openPaths && state._openPaths !== Object && state._openPaths[treePathKey(path)]);
}

function collectOpenPathsFromDom() {
  const tree = document.querySelector("#tree");
  if (!tree) return;
  if (!state._openPaths || state._openPaths === Object) state._openPaths = {};
  for (const ch of tree.querySelectorAll(".children")) {
    if (ch.classList.contains("hidden")) continue;
    const group = ch.parentElement;
    if (!group || !group.classList.contains("node-group")) continue;
    const row = group.querySelector(":scope > .node");
    if (!row) continue;
    const p = row.dataset.path;
    if (p != null) state._openPaths[treePathKey(p)] = true;
  }
}

/** 用会话 cache 回填已打开目录；只读 cache，不请求后端。 */
function hydrateOpenDirs(rootEl, depth) {
  if (!rootEl) return;
  const groups = rootEl.querySelectorAll(":scope > .node-group");
  for (const group of groups) {
    const row = group.querySelector(":scope > .node");
    const children = group.querySelector(":scope > .children");
    if (!row || !children) continue;
    const path = row.dataset.path || "";
    if (!isPathOpen(path)) continue;
    const hit = cachedChildren(path);
    if (!hit) continue;
    const twisty = row.querySelector(".twisty");
    children.innerHTML = "";
    children.appendChild(buildLevel(hit, depth + 1));
    children.dataset.loaded = "1";
    children.classList.remove("hidden");
    if (twisty) twisty.classList.add("open");
    row.classList.add("expanded");
    if (!children.querySelector(".node")) {
      const ph = document.createElement("div");
      ph.className = "child-loading";
      // 对齐到下一级目录行缩进，与子节点行最左对齐
      ph.style.paddingLeft = `${14 + (depth + 1) * 20}px`;
      ph.textContent = t("noMatchChild");
      children.appendChild(ph);
    } else {
      hydrateOpenDirs(children, depth + 1);
    }
  }
}

function fetchChildrenNodes(parentPath) {
  const key = treePathKey(parentPath);
  if (!state._childrenInflight || state._childrenInflight === Object) state._childrenInflight = {};
  if (state._childrenInflight[key]) return state._childrenInflight[key];

  const req = (async () => {
    // 筛「新增/已删除」时下钻请求带上标记开关，保证子节点可作下钻入口
    const needMarks = isSubtreeFilter();
    const res = await state.api.get_children(
      state.oldPath,
      state.newPath,
      parentPath,
      needMarks
    );
    if (res && res.error) {
      const err = new Error(String(res.error));
      throw err;
    }
    const nodes = Array.isArray(res && res.nodes) ? res.nodes : [];
    cacheChildren(parentPath, nodes);
    return nodes;
  })();

  state._childrenInflight[key] = req;
  const clear = () => {
    if (state._childrenInflight && state._childrenInflight[key] === req) {
      delete state._childrenInflight[key];
    }
  };
  req.then(clear, clear);
  return req;
}

async function doCompare() {
  if (state.comparing) return;
  if (typeof clearTreeSelection === "function") clearTreeSelection();
  // 基准=当前：走占用展开，不走差分对比
  if (state.oldPath && state.newPath && state.oldPath === state.newPath) {
    await browseSnapshot(state.oldPath);
    return;
  }
  state.comparing = true;
  state.treeMode = "compare";
  if (state.searchSort === "size-desc") state.searchSort = "delta-desc";
  clearChildrenCache();
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
    state.treeMode = "compare";
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
    // 新一轮对比不沿用上一对的筛选状态，回到默认「全部变化」
    resetTreeFilters();
    state._marksLoaded = false;
    syncSummaryToolButtons();
    renderSummary(res.summary);
    renderTopLevel(res.nodes);
    // 搜索仅回车触发；内存索引在打开搜索框时再预热
  } catch (err) {
    toast(t("compareFailed", err), true);
  } finally {
    state.comparing = false;
    setCompareBusy("");
    if (emptyTitle) {
      emptyTitle.textContent = prevEmptyTitle || t("emptyTitle");
    }
    btn.textContent = t("compare");
    updatePickers();
  }
}


/** 单份快照按占用展开；复用 compare/get_children，两侧传同一路径。 */
async function browseSnapshot(path) {
  if (!path || state.comparing) return;
  if (typeof clearTreeSelection === "function") clearTreeSelection();
  state.comparing = true;
  state.treeMode = "browse";
  if (!state.browseSort || state.browseSort === "delta-desc" || state.browseSort === "pct-desc") {
    state.browseSort = "size-desc";
  }
  // 展开模式下搜索按占用；避免沿用对比的 delta/pct
  if (!state.searchSort || state.searchSort === "delta-desc" || state.searchSort === "pct-desc") {
    state.searchSort = "size-desc";
  }
  clearChildrenCache();
  state.oldPath = path;
  state.newPath = path;
  if (typeof collapseTreeSearch === "function") collapseTreeSearch({ clear: true });
  if (typeof resetSearchPreheatUi === "function") resetSearchPreheatUi();
  else {
    state.searchPreheat = "idle";
    state.searchPreheatKey = "";
  }
  updatePickers();
  renderSnapshotList();
  syncSummaryToolButtons();

  let needDecompress = false;
  try {
    if (state.api && state.api.compare_cache_status) {
      const st = await state.api.compare_cache_status(path, path);
      if (st && st.ok !== false && !st.error) {
        needDecompress = !!st.need_decompress;
      } else {
        const s = snapByPath(path);
        needDecompress = !!(s && s.compressed);
      }
    } else {
      const s = snapByPath(path);
      needDecompress = !!(s && s.compressed);
    }
  } catch (_) {
    const s = snapByPath(path);
    needDecompress = !!(s && s.compressed);
  }
  const busyText = needDecompress ? t("decompressing") : t("browsing");
  setCompareBusy(busyText);
  const empty = $("#emptyState");
  const emptyTitle = empty && empty.querySelector(".empty-title");
  const prevEmptyTitle = emptyTitle ? emptyTitle.textContent : "";
  if (empty && !empty.classList.contains("hidden") && emptyTitle) {
    emptyTitle.textContent = busyText;
  }
  try {
    const res = await state.api.compare(path, path);
    if (res.error) {
      toast(res.error, true);
      return;
    }
    state.compared = true;
    state.treeMode = "browse";
    state.compareRoot = (res.summary && res.summary.new && res.summary.new.root) || "";
    state._lastSummary = res.summary;
    state._lastCompareKey = `${path}\n${path}`;
    state._lastComparePaths = path || "";
    if (typeof collapseTreeSearch === "function") collapseTreeSearch({ clear: true });
    syncSummaryToolButtons();
    renderSummary(res.summary);
    renderTopLevel(res.nodes);
  } catch (err) {
    toast(t("browseFailed", err), true);
  } finally {
    state.comparing = false;
    setCompareBusy("");
    if (emptyTitle) {
      emptyTitle.textContent = prevEmptyTitle || t("emptyTitle");
    }
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

// 浏览模式：按占用排序，不展示变化百分比
const BROWSE_SORT_OPTIONS = [
  { value: "size-desc", key: "sortSizeDesc" },
  { value: "name-asc", key: "sortNameAsc" },
  { value: "name-desc", key: "sortNameDesc" },
  { value: "mtime-desc", key: "sortMtimeDesc" },
];

const FILTER_OPTIONS = [
  { value: "all", key: "filterAll" },
  { value: "grew", key: "filterGrew" },
  { value: "shrank", key: "filterShrank" },
  { value: "added", key: "filterAdded", slowHint: "filterReRenderHint" },
  { value: "removed", key: "filterRemoved", slowHint: "filterReRenderHint" },
  { value: "link", key: "filterLink", slowHint: "filterReRenderHint" },
];

/** 是否启用依赖「目录子树含新增/删除/链接」标记的筛选。 */
function isSubtreeFilter() {
  return state.filter === "added" || state.filter === "removed" || state.filter === "link";
}

const SNAP_SORT_OPTIONS = [
  { value: "time-desc", key: "snapSortTimeDesc" },
  { value: "time-asc", key: "snapSortTimeAsc" },
  { value: "name-asc", key: "snapSortNameAsc" },
  { value: "name-desc", key: "snapSortNameDesc" },
];

function syncSummaryToolButtons() {
  const sortBtn = $("#sortMenuBtn");
  if (sortBtn) {
    const defaultSort = isBrowseMode() ? "size-desc" : "delta-desc";
    sortBtn.classList.toggle("is-active", currentTreeSort() !== defaultSort);
    sortBtn.setAttribute("aria-expanded", "false");
  }
  const filterBtn = $("#filterMenuBtn");
  if (filterBtn) {
    // 占用浏览没有「变大/变小」筛选
    const filterActive =
      state.filter !== "all" ||
      !!state.filterTime ||
      !!(String(state.filterDeltaVal || "").trim());
    filterBtn.classList.toggle("hidden", isBrowseMode());
    filterBtn.classList.toggle("is-active", !isBrowseMode() && filterActive);
    filterBtn.setAttribute("aria-expanded", "false");
  }
  const snapSortBtn = $("#snapSortMenuBtn");
  if (snapSortBtn) {
    snapSortBtn.classList.toggle("is-active", state.snapSort !== "time-desc");
    snapSortBtn.setAttribute("aria-expanded", "false");
  }
  const searchSortBtn = $("#searchSortBtn");
  if (searchSortBtn) {
    const defaultSearchSort = isBrowseMode() ? "size-desc" : "delta-desc";
    searchSortBtn.classList.toggle(
      "is-active",
      currentSearchSort() !== defaultSearchSort
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

/** 筛选菜单：变化方向 + 修改时间组 + 变化量组（后两组各占一行）。 */
function _fillFilterMenu(menu) {
  menu.innerHTML = "";
  for (const opt of FILTER_OPTIONS) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "icon-menu-item" + (opt.value === state.filter ? " is-selected" : "");
    item.setAttribute("role", "menuitemradio");
    item.setAttribute("aria-checked", opt.value === state.filter ? "true" : "false");
    item.textContent =
      t(opt.key) + (opt.slowHint ? `（${t(opt.slowHint)}）` : "");
    item.onclick = (e) => {
      e.stopPropagation();
      if (state.filter !== opt.value) {
        setFilter(opt.value);
        applyFilterToTree();
      }
      closeSummaryMenus();
    };
    menu.appendChild(item);
  }

  const divider = document.createElement("div");
  divider.className = "filter-menu-divider";
  menu.appendChild(divider);

  // ---- 修改时间：今日 / 7天 / 30天 / 自定义天数（一行） ----
  const timeRow = document.createElement("div");
  timeRow.className = "filter-group-row";
  const timeLabel = document.createElement("span");
  timeLabel.className = "filter-group-label";
  timeLabel.textContent = t("filterTimeGroup");
  timeRow.appendChild(timeLabel);
  for (const c of [
    { v: "today", key: "filterTimeToday" },
    { v: "7d", key: "filterTime7d" },
    { v: "30d", key: "filterTime30d" },
  ]) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "filter-chip" + (state.filterTime === c.v ? " is-active" : "");
    chip.textContent = t(c.key);
    chip.onclick = (e) => {
      e.stopPropagation();
      state.filterTime = state.filterTime === c.v ? "" : c.v;
      applyFilterToTree();
      // 刷新整行高亮
      _fillFilterMenu(menu);
    };
    timeRow.appendChild(chip);
  }
  const customInput = document.createElement("input");
  customInput.type = "number";
  customInput.min = "1";
  customInput.className = "filter-num";
  customInput.placeholder = t("filterTimeCustom");
  customInput.value = state.filterTime === "custom" ? String(state.filterTimeDays || "") : "";
  customInput.onchange = (e) => {
    e.stopPropagation();
    const d = Math.floor(Number(customInput.value));
    if (d > 0) {
      state.filterTime = "custom";
      state.filterTimeDays = d;
    } else if (state.filterTime === "custom") {
      state.filterTime = "";
    }
    applyFilterToTree();
    _fillFilterMenu(menu);
  };
  timeRow.appendChild(customInput);
  const daySuffix = document.createElement("span");
  daySuffix.className = "filter-group-suffix";
  daySuffix.textContent = t("filterTimeDays");
  timeRow.appendChild(daySuffix);
  menu.appendChild(timeRow);

  // ---- 变化量：大于/小于 + 数字 + 单位（一行） ----
  const deltaRow = document.createElement("div");
  deltaRow.className = "filter-group-row";
  const deltaLabel = document.createElement("span");
  deltaLabel.className = "filter-group-label";
  deltaLabel.textContent = t("filterDeltaGroup");
  deltaRow.appendChild(deltaLabel);
  const opSel = _filterSelect(
    [
      { v: "gt", key: "filterDeltaGt" },
      { v: "lt", key: "filterDeltaLt" },
    ],
    state.filterDeltaOp,
    (v) => {
      state.filterDeltaOp = v;
      applyFilterToTree();
    }
  );
  deltaRow.appendChild(opSel);
  const numInput = document.createElement("input");
  numInput.type = "number";
  numInput.min = "0";
  numInput.step = "any";
  numInput.className = "filter-num";
  numInput.placeholder = t("filterDeltaValue");
  numInput.value = String(state.filterDeltaVal || "");
  numInput.onchange = (e) => {
    e.stopPropagation();
    state.filterDeltaVal = String(numInput.value || "").trim();
    applyFilterToTree();
  };
  deltaRow.appendChild(numInput);
  const unitSel = _filterSelect(
    [
      { v: "KB", key: "unitKB" },
      { v: "MB", key: "unitMB" },
      { v: "GB", key: "unitGB" },
    ],
    state.filterDeltaUnit,
    (v) => {
      state.filterDeltaUnit = v;
      applyFilterToTree();
    }
  );
  deltaRow.appendChild(unitSel);
  menu.appendChild(deltaRow);

  // ---- 底部：清除修改时间与变化量两组筛选 ----
  const clearDivider = document.createElement("div");
  clearDivider.className = "filter-menu-divider";
  menu.appendChild(clearDivider);
  const clearItem = document.createElement("button");
  clearItem.type = "button";
  clearItem.className = "icon-menu-item";
  clearItem.textContent = t("filterClear");
  clearItem.onclick = (e) => {
    e.stopPropagation();
    resetTimeDeltaFilters();
    closeSummaryMenus();
  };
  menu.appendChild(clearItem);
}

/** 清除修改时间与变化量两组筛选并重渲染。 */
function resetTimeDeltaFilters() {
  state.filterTime = "";
  state.filterTimeDays = 0;
  state.filterDeltaOp = "gt";
  state.filterDeltaVal = "";
  state.filterDeltaUnit = "MB";
  applyFilterToTree();
}

/** 对比会话级筛选状态全部重置（方向 + 时间 + 变化量），不触发重渲染。 */
function resetTreeFilters() {
  state.filter = "all";
  state.filterTime = "";
  state.filterTimeDays = 0;
  state.filterDeltaOp = "gt";
  state.filterDeltaVal = "";
  state.filterDeltaUnit = "MB";
}

/** 组内下拉选择。 */
function _filterSelect(items, current, onPick) {
  const s = document.createElement("select");
  s.className = "filter-select";
  for (const it of items) {
    const o = document.createElement("option");
    o.value = it.v;
    o.textContent = t(it.key);
    s.appendChild(o);
  }
  s.value = current;
  s.onchange = (e) => {
    e.stopPropagation();
    onPick(s.value);
  };
  return s;
}

/** 筛选条件变化后重渲染对比树。 */
function applyFilterToTree() {
  syncSummaryToolButtons();
  if (state.compared && state._topNodes) {
    collectOpenPathsFromDom();
    // 筛「新增/已删除」需要后端下钻标记；本会话还没拉过就重拉顶层
    if (
      isSubtreeFilter() && !state._marksLoaded
    ) {
      refreshTopWithMarks();
      return;
    }
    renderTopLevel(state._topNodes);
  }
}

/** 筛「新增/已删除」时重拉带下钻标记的顶层数据（后端首次一次性计算）。 */
async function refreshTopWithMarks() {
  if (!state.compared) return;
  state._marksLoaded = true; // 先置位防重入；失败再回退
  const tree = $("#tree");
  if (tree) tree.innerHTML = `<div class="child-loading">${t("loading")}</div>`;
  try {
    const res = await state.api.compare(state.oldPath, state.newPath, true);
    if (res && res.error) {
      toast(String(res.error), true);
      throw new Error("marks_refresh_failed");
    }
    if (!Array.isArray(res && res.nodes)) throw new Error("marks_bad_response");
    clearChildrenCache();
    state._topNodes = res.nodes;
    renderTopLevel(res.nodes);
  } catch (err) {
    if (err && err.message !== "marks_refresh_failed") {
      toast(t("loadFailed", err && err.message ? err.message : String(err)));
    }
    state._marksLoaded = false;
    if (state._topNodes) renderTopLevel(state._topNodes);
  }
}

function openSummaryMenu(kind, anchor) {
  const cfg = {
    sort: {
      menu: "#sortMenu",
      btn: "#sortMenuBtn",
      options: isBrowseMode() ? BROWSE_SORT_OPTIONS : SORT_OPTIONS,
      current: () => currentTreeSort(),
      onPick: (value) => {
        if (isBrowseMode()) {
          if ((state.browseSort || "size-desc") === value) return;
          state.browseSort = value;
        } else {
          if (state.sort === value) return;
          state.sort = value;
        }
        syncSummaryToolButtons();
        if (state.compared && state._topNodes) {
          collectOpenPathsFromDom();
          renderTopLevel(state._topNodes);
        }
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
        if (state.compared && state._topNodes) {
          collectOpenPathsFromDom();
          renderTopLevel(state._topNodes);
        }
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
      options: isBrowseMode() ? BROWSE_SORT_OPTIONS : SORT_OPTIONS,
      current: () => currentSearchSort(),
      onPick: (value) => {
        if (currentSearchSort() === value) return;
        state.searchSort = value;
        syncSummaryToolButtons();
        // 仅重排搜索结果，不影响主树
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

  if (kind === "filter") _fillFilterMenu(menu);
  else _fillIconMenu(menu, cfg.options, cfg.current(), cfg.onPick);

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

  const cap = document.querySelector("#summaryBar .summary-caption");
  const dEl = $("#summaryDelta");
  const browseHint = $("#browseModeHint");
  if (isBrowseMode()) {
    if (cap) cap.textContent = t("totalSize");
    const total = (summary && summary.new && summary.new.total_size) || 0;
    dEl.textContent = fmtBytes(total);
    dEl.className = "summary-delta size";
    if (browseHint) {
      browseHint.textContent = t("browseModeHint");
      browseHint.classList.remove("hidden");
    }
  } else {
    if (cap) cap.textContent = t("totalChange");
    const delta = summary.total_delta;
    dEl.textContent = fmtDelta(delta);
    dEl.className = "summary-delta " + (delta >= 0 ? "grow" : "shrink");
    if (browseHint) browseHint.classList.add("hidden");
  }

  const skipped = summary.old.skipped_count + summary.new.skipped_count;
  const warn = $("#skipWarn");
  // 浏览单快照（自对比）时不显示 skipWarn：没有对比语境，「不可比较」无意义
  if (skipped > 0 && state.treeMode !== "browse") {
    warn.innerHTML = `<span class="skip-warn-text">${t("skipWarn", skipped)}</span><button class="skip-warn-show" type="button">${t("skippedShow")}</button>`;
    warn.classList.remove("hidden");
    const btn = warn.querySelector(".skip-warn-show");
    if (btn) btn.onclick = () => openSkippedOverlay(summary.old, summary.new);
  } else {
    warn.classList.add("hidden");
  }
}

/** 打开跳过目录列表弹窗。oldSnap/newSnap 为 summary 的 old/new 子对象。 */
function openSkippedOverlay(oldSnap, newSnap) {
  const ov = $("#skippedOverlay");
  if (!ov) return;
  $("#skippedDialogTitle").textContent = t("skippedDialogTitle");
  $("#skippedHint").textContent = t("skippedHint");
  const body = $("#skippedBody");
  body.innerHTML = "";
  const oldList = (oldSnap && oldSnap.skipped) || [];
  const newList = (newSnap && newSnap.skipped) || [];
  // 合并去重，标注来源：1=基准，2=当前，3=两者
  const map = new Map();
  for (const p of oldList) map.set(p, (map.get(p) || 0) | 1);
  for (const p of newList) map.set(p, (map.get(p) || 0) | 2);
  const paths = Array.from(map.keys()).sort();
  if (paths.length === 0) {
    body.innerHTML = `<div class="skipped-empty">${t("skippedEmpty")}</div>`;
  } else {
    for (const p of paths) {
      const flag = map.get(p);
      const tags = [];
      if (flag & 1) tags.push(`<span class="skipped-tag tag-old">${t("setAsBase")}</span>`);
      if (flag & 2) tags.push(`<span class="skipped-tag tag-new">${t("setAsCurrent")}</span>`);
      const baseName = p.split(/[\\/]/).pop() || p;
      const row = document.createElement("div");
      row.className = "skipped-item";
      row.innerHTML = `<span class="skipped-path" title="${escapeHtml(p)}">${escapeHtml(baseName)}</span>${tags.join("")}`;
      row.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const root = (flag & 2) ? newSnap.root : oldSnap.root;
        openCtxMenu(e, { path: p, _revealRoot: root });
      });
      body.appendChild(row);
    }
  }
  ov.classList.remove("hidden");
}

/** 关闭跳过目录列表弹窗。 */
function closeSkippedOverlay() {
  const ov = $("#skippedOverlay");
  if (ov) ov.classList.add("hidden");
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
  // 占用浏览：展示整层，按 size 排序即可
  if (isBrowseMode()) return true;
  // 变化方向
  if (state.filter === "grew") {
    if (node.kind !== "incomparable" && node.delta <= 0) return false;
  } else if (state.filter === "shrank") {
    if (node.delta >= 0) return false;
  } else if (state.filter === "added") {
    // 目录大小是递归汇总的：深层文件新增不会让目录 kind 变 added，
    // 靠 has_added 保留这类目录作下钻入口。
    const isAdded = node.kind === "added";
    const containsAdded = node.is_dir && node.has_added;
    if (!isAdded && !containsAdded) return false;
  } else if (state.filter === "removed") {
    const isRemoved = node.kind === "removed";
    const containsRemoved = node.is_dir && node.has_removed;
    if (!isRemoved && !containsRemoved) return false;
  } else if (state.filter === "link") {
    // 链接节点自身，或递归范围内含链接的目录（作下钻入口）。
    const isLink = !!node.link_kind;
    const containsLink = node.is_dir && node.has_link;
    if (!isLink && !containsLink) return false;
  } else if (node.kind === "incomparable") {
    // all：不可比节点照常显示
  } else if (node.delta === 0) {
    return false; // all：隐藏「大小未变」的噪声
  }
  // 修改时间：早于门槛的隐藏（mtime 为 0 的旧数据视为无时间信息，同样隐藏）
  const tl = filterTimeThreshold();
  if (tl && (!node.mtime || node.mtime < tl)) return false;
  // 变化量：|delta| 与阈值比较
  const lim = filterDeltaLimitBytes();
  if (lim != null) {
    const d = Math.abs(node.delta);
    if (state.filterDeltaOp === "lt" ? d > lim : d < lim) return false;
  }
  return true;
}

/** 修改时间过滤的门槛时间戳（秒）；未启用返回 0。 */
function filterTimeThreshold() {
  const f = state.filterTime;
  if (!f) return 0;
  let days = 0;
  if (f === "today") {
    const d = new Date();
    const startOfDay = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000;
    return startOfDay;
  }
  if (f === "7d") days = 7;
  else if (f === "30d") days = 30;
  else if (f === "custom") days = Math.max(1, Math.floor(Number(state.filterTimeDays) || 0));
  if (days <= 0) return 0;
  return Date.now() / 1000 - days * 86400;
}

const DELTA_UNIT_MULT = { B: 1, KB: 1024, MB: 1024 * 1024, GB: 1024 * 1024 * 1024 };

/** 变化量过滤的字节阈值；未启用返回 null。 */
function filterDeltaLimitBytes() {
  const v = Number(String(state.filterDeltaVal || "").trim());
  if (!Number.isFinite(v) || v <= 0) return null;
  const mult = DELTA_UNIT_MULT[state.filterDeltaUnit] || DELTA_UNIT_MULT.MB;
  return v * mult;
}

// ---- 排序 ----

/** 变化百分比（|delta| / 旧大小）；旧不存在视为无穷大（新增即 +100%+）。 */
function deltaPct(n) {
  if (n.old_size > 0) return Math.abs(n.delta) / n.old_size;
  return n.delta !== 0 ? Infinity : 0;
}

const SORTERS = {
  "delta-desc": (a, b) => Math.abs(b.delta) - Math.abs(a.delta),
  "size-desc": (a, b) =>
    (nodeSize(b) - nodeSize(a)) ||
    (a.name || a.path || "").localeCompare(b.name || b.path || "", cmpLocale()),
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
  // 会话索引：path → 完整节点数据，供圈选等场景按路径取回大小字段
  state._pathNodeMap = {};
  // 顶层最大变化量/占用：作为「顶层基准」模式下整棵树统一的条长标尺，
  // 取全部顶层节点（不受筛选影响），保证切换筛选时条长不跳变。
  state._barRef = isBrowseMode()
    ? nodes.reduce((m, n) => Math.max(m, nodeSize(n)), 1)
    : nodes.reduce((m, n) => Math.max(m, Math.abs(n.delta)), 1);
  const tree = $("#tree");
  tree.innerHTML = "";
  const frag = buildLevel(nodes, 0);
  tree.appendChild(frag);
  if (!tree.querySelector(".node")) {
    tree.innerHTML = `<div class="child-loading">${t("noMatchTop")}</div>`;
    return;
  }
  // 改排序/筛选会重建 DOM：用会话 cache 回填已打开目录，不重新 get_children
  hydrateOpenDirs(tree, 0);
  applyTreeSelectionToDom();
}

/** 由一层节点数据构建 DOM 片段（含展开/懒加载逻辑）。 */
function buildLevel(nodes, depth) {
  const frag = document.createDocumentFragment();
  const visible = nodes.filter(matchFilter);
  const sortKey = currentTreeSort();
  visible.sort(SORTERS[sortKey] || SORTERS[isBrowseMode() ? "size-desc" : "delta-desc"]);
  // 条长统一按顶层最大变化量，保证切换筛选时比例不跳变
  const ref = state._barRef || 1;

  const renderCount = Math.min(visible.length, PER_LEVEL_CAP);
  for (let i = 0; i < renderCount; i++) {
    frag.appendChild(buildNode(visible[i], depth, ref));
  }
  if (visible.length > PER_LEVEL_CAP) {
    const more = document.createElement("div");
    more.className = "show-more";
    // 本层「显示更多」对齐到下一级目录行的缩进，与子节点行最左对齐
    more.style.paddingLeft = `${14 + (depth + 1) * 20}px`;
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
  if (state._pathNodeMap) {
    state._pathNodeMap[_treePathKey(node.path)] = node;
  }

  const browse = isBrowseMode();
  const sizeVal = nodeSize(node);
  const kindClass = browse
    ? "size"
    : node.kind === "incomparable" ? "incomparable"
    : node.delta > 0 ? "grow"
    : node.delta < 0 ? "shrink"
    : "unchanged";

  const row = document.createElement("div");
  row.className = `node ${kindClass}${node.is_dir ? " dir" : ""}`;
  row.dataset.path = node.path || "";
  row.style.paddingLeft = `${14 + depth * 20}px`;
  // 间隔线左端对齐子级引导线位置，不超出
  row.style.setProperty("--sep-left", `${14 + (depth + 1) * 20 - 10}px`);

  const canExpand = node.is_dir && node.has_children;
  const metric = browse ? sizeVal : Math.abs(node.delta);
  const barPct = Math.max(2, Math.min(100, Math.round((metric / ref) * 100)));
  const deltaText = browse
    ? fmtBytes(sizeVal)
    : node.kind === "incomparable" ? t("incomparable") : fmtDelta(node.delta);
  // 只存在于一侧的内容单独打标，回答「多/少了什么」。浏览模式不打变化标。
  const tag = browse
    ? ""
    : node.kind === "added" ? `<span class="node-tag added">${t("tagAdded")}</span>`
    : node.kind === "removed" ? `<span class="node-tag removed">${t("tagRemoved")}</span>`
    : "";
  // 链接类型徽标（目录联接 / 符号链接 / 其它链接）。浏览与对比模式都打。
  // title 悬浮解释该链接是什么；拿到目标路径时追加一行「链接到 X」。
  const linkTag = node.link_kind
    ? (() => {
        const isJunction = node.link_kind === "junction";
        const isSymlink = node.link_kind === "symlink";
        const typeText = isJunction ? t("tagJunction") : isSymlink ? t("tagSymlink") : t("tagReparse");
        const typeTitle = isJunction ? t("tagJunctionTitle") : isSymlink ? t("tagSymlinkTitle") : t("tagReparseTitle");
        // link_target 是动态值，插进 title 必须转义
        const targetLine = node.link_target
          ? escapeHtml(t("linkTargetLine", node.link_target))
          : "";
        return `<span class="node-tag link" title="${escapeHtml(typeTitle)}${targetLine}">${typeText}</span>`;
      })()
    : "";
  // 命中删除白名单时显示「白名单」徽标；浏览与对比模式都打。
  const whitelistTag = node.whitelisted
    ? `<span class="node-tag whitelist" title="${escapeHtml(t("tagWhitelistTitle"))}">${t("tagWhitelist")}</span>`
    : "";

  row.innerHTML = `
    <span class="twisty">${canExpand ? "▸" : ""}</span>
    <span class="node-icon">${node.is_dir ? "📁" : "📄"}</span>
    <span class="node-name">${escapeHtml(node.name || node.path)}</span>
    ${tag}${linkTag}${whitelistTag}
    <span class="node-fill"></span>
    <span class="node-bar"><i style="width:${barPct}%"></i></span>
    <span class="node-delta"><span class="delta-val">${deltaText}</span>${canExpand ? '<span class="delta-arrow"></span>' : ''}</span>`;

  const children = document.createElement("div");
  children.className = "children hidden";
  // 层级引导线（UI 设置可开）：竖线位置对齐子行 twisty 缩进
  children.style.setProperty("--gleft", `${14 + (depth + 1) * 20 - 10}px`);

  // 多选模式 / Ctrl / Shift：勾选；否则目录单击展开
  row.onclick = (e) => {
    if (e.shiftKey) {
      e.preventDefault();
      e.stopPropagation();
      toggleTreeSelection(node, { range: true });
      return;
    }
    if (e.ctrlKey || e.metaKey || isTreeMultiSelectMode()) {
      e.preventDefault();
      e.stopPropagation();
      toggleTreeSelection(node, { range: false });
      return;
    }
    if (canExpand) toggleDir(node, row, children, depth);
  };
  if (state.treeSelected && state.treeSelected[_treePathKey(node.path)]) {
    row.classList.add("is-selected");
  }
  // 悬停显示明细；右键出菜单（定位/复制路径）。
  row.title = browse
    ? `${fmtBytes(sizeVal)}` + (node.mtime ? t("mtimeLine", fmtTime(node.mtime)) : "")
    : `${fmtBytes(node.old_size)} → ${fmtBytes(node.new_size)}` +
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
  for (const r of tree.querySelectorAll(".node.expanded")) {
    r.classList.remove("expanded");
  }
}

/** 折叠某目录节点及其子树内所有已展开目录（仅隐藏展开态，不卸载已加载子节点）。 */
function collapseNodeSubtree(path) {
  const tree = $("#tree");
  if (!tree) return;
  const row = tree.querySelector(`.node[data-path="${cssEscapeAttr(path)}"]`);
  const group = row ? row.closest(".node-group") : null;
  if (!group) return;
  for (const ch of group.querySelectorAll(".children")) {
    ch.classList.add("hidden");
  }
  for (const tw of group.querySelectorAll(".twisty.open")) {
    tw.classList.remove("open");
  }
  for (const r of group.querySelectorAll(".node.expanded")) {
    r.classList.remove("expanded");
    if (r.dataset.path != null) markPathOpen(r.dataset.path, false);
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
      currentSearchSort(),
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
  const browse = isBrowseMode();
  const kindClass = browse
    ? "size"
    : node.kind === "incomparable" ? "muted"
    : node.delta > 0 ? "grow"
    : node.delta < 0 ? "shrink"
    : "muted";
  const deltaText = browse
    ? fmtBytes(nodeSize(node))
    : node.kind === "incomparable" ? t("incomparable") : fmtDelta(node.delta);
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
 * @param {string} targetPath 相对路径
 * @param {{silent?: boolean}} [opts] silent=true 时定位失败不发内部 toast
 * @returns {Promise<boolean>} 完整定位成功返回 true
 */
async function locateTreePath(targetPath, opts) {
  if (!targetPath || !state.compared) return false;
  const silent = !!(opts && opts.silent);
  const normTarget = _normTreePath(targetPath);
  const parts = normTarget.split("\\").filter(Boolean);
  if (!parts.length) return false;

  const tree = $("#tree");
  if (!tree) return false;

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
      if (!silent) toast(t("treeSearchLocateFailed", prefix), true);
      return false;
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
  return true;
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

// ---- 路径跳转定位 ----

function openGotoPathDialog() {
  if (!state.compared) {
    toast(t("gotoPathNeedCompare"), true);
    return;
  }
  const overlay = $("#gotoPathOverlay");
  if (!overlay) return;
  const hint = $("#gotoPathHint");
  const root = state.compareRoot || "";
  const base = t("gotoPathHint", root);
  // 对比模式只展示有变化的目录；浏览模式展示完整树。
  const modeNote = isBrowseMode() ? "" : "\n" + t("gotoPathCompareModeNote");
  if (hint) hint.textContent = base + modeNote;
  const input = $("#gotoPathInput");
  if (input) {
    input.value = "";
    input.disabled = false;
    setTimeout(() => {
      try { input.focus(); } catch (e) {}
    }, 0);
  }
  overlay.classList.remove("hidden");
}

function closeGotoPathDialog() {
  const overlay = $("#gotoPathOverlay");
  if (overlay) overlay.classList.add("hidden");
}

/** 用户输入 → 相对路径；根不匹配（绝对路径在别的盘）返回 null。 */
function resolveInputPath(input) {
  const raw = String(input || "").trim().replace(/\//g, "\\");
  if (!raw) return null;
  const root = String(state.compareRoot || "").trim().replace(/[\\/]+$/, "");
  if (!root) return raw; // 无扫描根时按相对路径处理
  const norm = raw.replace(/[\\/]+$/, "");
  if (norm.toLowerCase() === root.toLowerCase()) return ""; // 根自身
  if (norm.toLowerCase().startsWith(root.toLowerCase() + "\\")) {
    return norm.slice(root.length + 1);
  }
  // 绝对路径（盘符或 UNC）但不匹配扫描根：不可定位
  if (/^[a-zA-Z]:[\\/]/.test(norm) || norm.startsWith("\\\\")) return null;
  return norm; // 相对路径
}

/**
 * 跳转定位：先按完整路径定位，失败则逐段去掉末段重试，
 * 直到能匹配的最深一层并定位过去。
 */
async function gotoPath(input) {
  const rel = resolveInputPath(input);
  if (rel === null) {
    showGotoPathHint(t("gotoPathRootMismatch"));
    return;
  }
  const parts = rel.split("\\").filter(Boolean);
  if (!parts.length) {
    // 输入即扫描根：顶层已展示，把第一行滚到视野内即可
    closeGotoPathDialog();
    const first = $("#tree .node");
    if (first) first.scrollIntoView({ block: "start", behavior: "smooth" });
    return;
  }
  for (let i = parts.length; i > 0; i--) {
    const prefix = parts.slice(0, i).join("\\");
    const ok = await locateTreePath(prefix, { silent: true });
    if (ok) {
      closeGotoPathDialog();
      if (i < parts.length) {
        toast(t("gotoPathPartial", prefix));
      }
      return;
    }
  }
  showGotoPathHint(
    isBrowseMode()
      ? t("gotoPathNotFound", rel)
      : t("gotoPathNotFound", rel) + "\n" + t("gotoPathNotFoundCompareHint")
  );
}

/** 在对话框 hint 区显示错误，不关闭对话框，方便用户修改后重试。 */
function showGotoPathHint(text) {
  const hint = $("#gotoPathHint");
  if (hint) hint.textContent = text;
  const input = $("#gotoPathInput");
  if (input) {
    input.disabled = false;
    try { input.focus(); } catch (e) {}
  }
}

/** 对话框「定位」按钮 / 回车：取输入后执行 gotoPath。 */
async function submitGotoPath() {
  const input = $("#gotoPathInput");
  const value = input ? String(input.value || "").trim() : "";
  if (!value) {
    showGotoPathHint(t("gotoPathEmpty"));
    return;
  }
  if (input) input.disabled = true;
  await gotoPath(value);
  if (input) input.disabled = false;
}

// ---- 右键菜单 ----

function openCtxMenu(e, node) {
  state.ctxNode = node;
  const menu = $("#ctxMenu");
  const isSkipped = !!(node && node._revealRoot);
  // skipped 项只保留 reveal，其余用 ctx-skipped-hide 临时隐藏（不破坏原有 hidden 状态）
  menu.querySelectorAll('[data-cmd]').forEach((el) => {
    el.classList.toggle("ctx-skipped-hide", isSkipped && el.dataset.cmd !== "reveal");
  });
  if (!isSkipped) {
    const collapse = menu.querySelector('[data-cmd="collapse"]');
    if (collapse) collapse.classList.toggle("hidden", !(node && node.is_dir && node.has_children));
    const migrate = menu.querySelector('[data-cmd="migrate"]');
    if (migrate) {
      // 只有真实目录可迁移；链接目录已是 junction，迁移无意义
      migrate.classList.toggle("hidden", !(node && node.is_dir && !node.link_kind));
    }
    const bulk = menu.querySelector('[data-cmd="delete-selected"]');
    if (bulk) {
      const n = treeSelectionCount();
      bulk.classList.toggle("hidden", n <= 1);
    }
  }
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
    const root = (node && node._revealRoot) || state.compareRoot;
    const res = await state.api.reveal_path(root, node.path);
    if (res.error) toast(res.error, true);
    else if (res.message) toast(res.message);
  } else if (cmd === "copy") {
    const p = fullPath(state.compareRoot, node.path);
    toast((await copyText(p)) ? t("copied", p) : t("copyFailed"), false);
  } else if (cmd === "collapse") {
    collapseNodeSubtree(node.path);
  } else if (cmd === "migrate") {
    const abs = fullPath(state.compareRoot, node.path);
    if (typeof openMigrateTab === "function") {
      openMigrateTab(abs);
    } else {
      toast(t("migrateNeedCompare"), true);
    }
  } else if (cmd === "delete") {
    if (typeof addCompareNodeToPending === "function") {
      addCompareNodeToPending(node);
    } else {
      toast(t("deleteFail"), true);
    }
  } else if (cmd === "delete-selected") {
    const nodes = selectedTreeNodes();
    if (!nodes.length) {
      toast(t("treeSelectNeed"), true);
    } else if (typeof addCompareNodesToPending === "function") {
      addCompareNodesToPending(nodes);
      clearTreeSelection();
      exitTreeMultiSelectAfterPendingAdd();
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
    // 只藏 DOM，不卸子节点、不清 cache —— 再展开零请求
    children.classList.add("hidden");
    if (twisty) twisty.classList.remove("open");
    row.classList.remove("expanded");
    markPathOpen(node.path, false);
    return;
  }

  if (twisty) twisty.classList.add("open");
  children.classList.remove("hidden");
  row.classList.add("expanded");
  markPathOpen(node.path, true);

  // 已在 DOM 装过：直接显示
  if (children.dataset.loaded === "1") return;

  // 会话 cache 命中：填 DOM，不打后端
  const hit = cachedChildren(node.path);
  if (hit) {
    children.innerHTML = "";
    children.appendChild(buildLevel(hit, depth + 1));
    children.dataset.loaded = "1";
    if (!children.querySelector(".node")) {
      children.innerHTML = `<div class="child-loading">${t("noMatchChild")}</div>`;
    } else {
      hydrateOpenDirs(children, depth);
    }
    return;
  }

  // 慢请求才显示「加载中」，避免本地几十 ms 闪一下
  let loadingTimer = setTimeout(() => {
    if (children.dataset.loaded === "1") return;
    if (
      !children.querySelector(".child-loading") &&
      !children.querySelector(".node") &&
      !children.querySelector(".child-error")
    ) {
      const ph = document.createElement("div");
      ph.className = "child-loading";
      // 对齐到下一级目录行缩进
      ph.style.paddingLeft = `${14 + (depth + 1) * 20}px`;
      ph.textContent = t("loading");
      children.appendChild(ph);
    }
  }, 150);

  try {
    const nodes = await fetchChildrenNodes(node.path);
    clearTimeout(loadingTimer);
    // 等待期间若已收起：数据留 cache，DOM 保持收起
    if (children.classList.contains("hidden")) return;
    children.innerHTML = "";
    children.appendChild(buildLevel(nodes, depth + 1));
    children.dataset.loaded = "1";
    if (!children.querySelector(".node")) {
      const ph = document.createElement("div");
      ph.className = "child-loading";
      ph.style.paddingLeft = `${14 + (depth + 1) * 20}px`;
      ph.textContent = t("noMatchChild");
      children.appendChild(ph);
    }
  } catch (err) {
    clearTimeout(loadingTimer);
    if (children.classList.contains("hidden")) return;
    const msg = err && err.message ? err.message : String(err);
    const errEl = document.createElement("div");
    errEl.className = "child-error";
    errEl.style.paddingLeft = `${14 + (depth + 1) * 20}px`;
    errEl.textContent = t("loadFailed", msg);
    children.innerHTML = "";
    children.appendChild(errEl);
    children.dataset.loaded = "";
  }
}

// ---- 悬停分组框（UI 设置「对比树悬停分组框」可开关）----

let _hoverGroupTimer = 0;
let _hoverGroupTarget = null;

/** 行所在的子级块（当前目录+兄弟目录整块）；最顶级行返回 null。 */
function _hoverGroupOf(el) {
  const group = el && el.closest ? el.closest(".node-group") : null;
  const parent = group ? group.parentElement : null;
  return parent && parent.classList && parent.classList.contains("children")
    ? parent
    : null;
}

function _clearHoverGroup() {
  clearTimeout(_hoverGroupTimer);
  if (_hoverGroupTarget) {
    _hoverGroupTarget.classList.remove("hover-group");
    _hoverGroupTarget = null;
  }
}

function _hoverGroupEnabled() {
  const s = state._settings;
  return !!s && s.ui_hover_group_outline === true;
}

/** 块内行数（自己+兄弟）需超过设置的阈值才显示分组框；0 = 总是显示。 */
function _hoverGroupBigEnough(box) {
  const s = state._settings;
  const min = s && Number.isFinite(Number(s.ui_hover_group_min))
    ? Math.max(0, Number(s.ui_hover_group_min))
    : 2;
  if (min <= 0) return true;
  const rows = box.querySelectorAll(":scope > .node-group").length;
  return rows > min;
}

/** 树上事件委托：悬停行 → 60ms 防抖 → 描边该行所在块；划过别处平滑换框。 */
(function bindTreeHoverGroup() {
  const tree = $("#tree");
  if (!tree) return;
  tree.addEventListener("mouseover", (e) => {
    if (!_hoverGroupEnabled()) {
      _clearHoverGroup();
      return;
    }
    const row = e.target && e.target.closest ? e.target.closest(".node") : null;
    if (!row || !tree.contains(row)) return;
    const box = _hoverGroupOf(row);
    const next = box && _hoverGroupBigEnough(box) ? box : null;
    if (next === _hoverGroupTarget) return;
    clearTimeout(_hoverGroupTimer);
    _hoverGroupTimer = setTimeout(() => {
      if (next === _hoverGroupTarget) return;
      _clearHoverGroup();
      if (next) {
        next.classList.add("hover-group");
        _hoverGroupTarget = next;
      }
    }, 60);
  });
  tree.addEventListener("mouseleave", _clearHoverGroup);
})();

/* ============================================================
   WhoShitsOnMyC 前端逻辑
   通过 window.pywebview.api 调用 Python 后端；后端事件经
   window.__onPyEvent 回调推来（见 app.py 的 Api._emit）。
   ============================================================ */

"use strict";

// ---- 全局状态 ----
const state = {
  api: null,
  snapshots: [],      // 快照摘要列表
  oldPath: "",        // 选作「基准」的快照路径
  newPath: "",        // 选作「当前」的快照路径
  filter: "all",      // 当前过滤：all | grew | shrank
  sort: "delta-desc", // 变化树排序，见 SORTERS
  snapSort: "time-desc", // 快照列表排序，见 SNAP_SORTERS
  barBase: "global",  // 占比条长度基准：global（顶层最大）| sibling（同级最大）
  compared: false,    // 是否已出对比结果
  comparing: false,   // 对比请求进行中（防重复点击）
  compareRoot: "",    // 本次对比的扫描根（右键定位真实路径用）
  ctxNode: null,      // 右键菜单当前指向的节点
};

const PER_LEVEL_CAP = 300; // 每层最多先渲染这么多行，其余「显示更多」

// ---- 国际化（i18n）----
// 中文系统显示中文，其余一律英文。启动时按 navigator.language 同步判定
// （WebView2 的该值跟随 Windows 显示语言），随后与后端校对并同步后端报错语言。
let LANG = "en";

const I18N = {
  zh: {
    newScan: "＋ 新建扫描",
    snapRecords: "快照记录",
    snapSortTitle: "快照排序",
    snapSortTimeDesc: "时间 新→旧",
    snapSortTimeAsc: "时间 旧→新",
    snapSortNameAsc: "路径 A→Z",
    snapSortNameDesc: "路径 Z→A",
    openDir: "打开快照文件夹",
    openDirTitle: "在资源管理器中打开快照存放目录",
    workers: "扫描线程数",
    workersTitle: "扫描用的并行线程数，机械硬盘建议 1，SSD 可加大",
    compress: "压缩快照",
    compressTitle: "扫描完成后压缩快照以节省磁盘；对比时再解压",
    compressOn: "已开启快照压缩，下次扫描生效",
    compressOff: "已关闭快照压缩，下次扫描生效",
    decompressing: "正在解压快照…",
    tagCompressed: "压缩",
    themeTitle: "切换暗色/浅色",
    langTitle: "切换语言 / Switch language",
    githubStar: "Star",
    githubStarTitle: "在 GitHub 上 Star 本项目",
    pickOldLabel: "基准（较早的快照）",
    pickNewLabel: "当前（较新的快照）",
    pickPlaceholder: "点击选择…",
    swapTitle: "交换基准与当前",
    compare: "对比",
    clearPick: "清空",
    clearPickTitle: "清空基准与当前的选择",
    totalChange: "总变化",
    barBaseTitle: "占比条的长度基准",
    barBaseGlobal: "条长基准：顶层最大项",
    barBaseSibling: "条长基准：同级最大项",
    sortSelTitle: "排序方式",
    sortDeltaDesc: "按变化幅度（绝对值）",
    sortPctDesc: "按变化幅度（百分比）",
    sortNameAsc: "按名称 A→Z",
    sortNameDesc: "按名称 Z→A",
    sortMtimeDesc: "按修改时间 新→旧",
    filterTitle: "筛选变化方向",
    filterAll: "全部变化",
    filterGrew: "只看变大 ▲",
    filterShrank: "只看变小 ▼",
    emptyTitle: "磁盘空间对比",
    emptyHint: "新建扫描以留存基准，隔段时间对同一目录再扫一次，选择两份快照进行对比，即可定位空间去向。",
    ctxReveal: "📂 在资源管理器中打开",
    ctxCopy: "📋 复制完整路径",
    scanning: "正在扫描…",
    scanFilesInit: "已扫描 0 个文件",
    cancel: "取消",
    snapRoleBase: "◀ 基准",
    snapRoleCurrent: "▶ 当前",
    setAsBase: "设为基准",
    setAsCurrent: "设为当前",
    delete: "删除",
    filesN: (n) => `${n} 文件`,
    noSnapshots: "暂无快照，请新建扫描。",
    deleteFailed: (e) => `删除失败：${e}`,
    snapshotDeleted: "已删除快照",
    comparing: "对比中…",
    compareFailed: (e) => `对比失败：${e}`,
    skipWarn: (n) => `⚠ 有 ${n} 个目录因无权限被跳过，其内容变化以「不可比较」标出，不计入增长。`,
    noMatchTop: "当前筛选下没有匹配的变化。",
    showMore: (n) => `显示更多（还有 ${n} 项）`,
    incomparable: "不可比较",
    tagAdded: "新增",
    tagRemoved: "已删除",
    mtimeLine: (s) => `\n修改时间：${s}`,
    copied: (p) => `已复制：${p}`,
    copyFailed: "复制失败",
    loading: "加载中…",
    loadFailed: (e) => `加载失败：${e}（收起后可重试）`,
    noMatchChild: "此目录下无匹配当前筛选的变化。",
    scannedFiles: (n) => `已扫描 ${n} 个文件`,
    scanDone: "扫描完成，已保存快照",
    scanCancelled: "扫描已取消",
    scanError: (m) => `扫描出错：${m}`,
    workersSet: (n) => `扫描线程数已设为 ${n}，下次扫描生效`,
    themeDark: "🌙 暗色",
    themeLight: "☀️ 浅色",
    cpuTag: "（CPU）",
    agoJustNow: "刚刚",
    agoMin: (n) => `${n} 分钟前`,
    agoHour: (n) => `${n} 小时前`,
    agoDay: (n) => `${n} 天前`,
  },
  en: {
    newScan: "＋ New scan",
    snapRecords: "Snapshots",
    snapSortTitle: "Sort snapshots",
    snapSortTimeDesc: "Time: new→old",
    snapSortTimeAsc: "Time: old→new",
    snapSortNameAsc: "Path: A→Z",
    snapSortNameDesc: "Path: Z→A",
    openDir: "Open snapshot folder",
    openDirTitle: "Open the snapshot folder in File Explorer",
    workers: "Scan threads",
    workersTitle: "Parallel scan threads; use 1 for HDDs, raise for SSDs",
    compress: "Compress snapshots",
    compressTitle: "Compress snapshots after scan to save disk; decompress when comparing",
    compressOn: "Snapshot compression on; effective next scan",
    compressOff: "Snapshot compression off; effective next scan",
    decompressing: "Decompressing snapshot…",
    tagCompressed: "zip",
    themeTitle: "Toggle dark / light",
    langTitle: "切换语言 / Switch language",
    githubStar: "Star",
    githubStarTitle: "Star this project on GitHub",
    pickOldLabel: "Base (earlier snapshot)",
    pickNewLabel: "Current (later snapshot)",
    pickPlaceholder: "Click to choose…",
    swapTitle: "Swap base and current",
    compare: "Compare",
    clearPick: "Clear",
    clearPickTitle: "Clear base and current selections",
    totalChange: "Total change",
    barBaseTitle: "Bar length baseline",
    barBaseGlobal: "Bar baseline: top-level max",
    barBaseSibling: "Bar baseline: sibling max",
    sortSelTitle: "Sort by",
    sortDeltaDesc: "Change size (absolute)",
    sortPctDesc: "Change size (percent)",
    sortNameAsc: "Name A→Z",
    sortNameDesc: "Name Z→A",
    sortMtimeDesc: "Modified time new→old",
    filterTitle: "Filter change direction",
    filterAll: "All changes",
    filterGrew: "Grew only ▲",
    filterShrank: "Shrank only ▼",
    emptyTitle: "Disk space comparison",
    emptyHint: "Create a scan to save a baseline, rescan the same folder later, then compare the two snapshots to see where the space went.",
    ctxReveal: "📂 Open in File Explorer",
    ctxCopy: "📋 Copy full path",
    scanning: "Scanning…",
    scanFilesInit: "Scanned 0 files",
    cancel: "Cancel",
    snapRoleBase: "◀ Base",
    snapRoleCurrent: "▶ Current",
    setAsBase: "Set as base",
    setAsCurrent: "Set as current",
    delete: "Delete",
    filesN: (n) => `${n} files`,
    noSnapshots: "No snapshots yet. Create a scan.",
    deleteFailed: (e) => `Delete failed: ${e}`,
    snapshotDeleted: "Snapshot deleted",
    comparing: "Comparing…",
    compareFailed: (e) => `Comparison failed: ${e}`,
    skipWarn: (n) => `⚠ ${n} folder(s) were skipped (no permission); their changes are marked "incomparable" and excluded from growth.`,
    noMatchTop: "No changes match the current filter.",
    showMore: (n) => `Show more (${n} left)`,
    incomparable: "Incomparable",
    tagAdded: "New",
    tagRemoved: "Deleted",
    mtimeLine: (s) => `\nModified: ${s}`,
    copied: (p) => `Copied: ${p}`,
    copyFailed: "Copy failed",
    loading: "Loading…",
    loadFailed: (e) => `Load failed: ${e} (collapse to retry)`,
    noMatchChild: "No changes match the current filter in this folder.",
    scannedFiles: (n) => `Scanned ${n} files`,
    scanDone: "Scan complete, snapshot saved",
    scanCancelled: "Scan cancelled",
    scanError: (m) => `Scan error: ${m}`,
    workersSet: (n) => `Scan threads set to ${n}, effective next scan`,
    themeDark: "🌙 Dark",
    themeLight: "☀️ Light",
    cpuTag: " (CPU)",
    agoJustNow: "just now",
    agoMin: (n) => `${n} min ago`,
    agoHour: (n) => `${n} h ago`,
    agoDay: (n) => `${n} d ago`,
  },
};

/** 取当前语言下 key 对应的文案；值为函数则以 args 调用；缺失回落英文，再缺回落 key 本身。 */
function t(key, ...args) {
  const dict = I18N[LANG] || I18N.en;
  let v = dict[key];
  if (v === undefined) v = I18N.en[key];
  if (v === undefined) return key;
  return typeof v === "function" ? v(...args) : v;
}

/** 排序用的 locale：跟随界面语言。 */
function cmpLocale() { return LANG === "zh" ? "zh" : "en"; }

/** 按 navigator.language 判定：以 zh 开头→"zh"；明确的其它语言→"en"；拿不准→null。 */
function detectLangSync() {
  const nav = (navigator.languages && navigator.languages[0]) || navigator.language || "";
  if (/^zh/i.test(nav)) return "zh";
  if (/^[a-z]{2}/i.test(nav)) return "en";
  return null;
}

/** 刷新静态 DOM 上的 data-i18n / data-i18n-title，并设置 <html lang>。 */
function applyStaticI18n() {
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
}

/** 把当前语言同步给后端，令其报错文案与界面一致。 */
function syncBackendLang() {
  try { state.api && state.api.set_language(LANG); } catch (e) {}
}

/** 切到指定语言：刷新静态文案 + 所有动态区域 + 同步后端。 */
function setLang(lang) {
  LANG = lang === "zh" ? "zh" : "en";
  applyStaticI18n();
  applyThemeButton();
  renderSnapshotList();
  updatePickers();
  if (state.compared) {
    if (state._lastSummary) renderSummary(state._lastSummary);
    if (state._topNodes) renderTopLevel(state._topNodes);
  }
  syncBackendLang();
}

/** 启动时确定界面语言：
 *  后端（Windows 显示语言）为权威，500ms 内返回则采纳；
 *  超时/失败则回退到 navigator 的明确判定，再拿不准才默认英文。
 *  navigator 的即时判定已用于首屏渲染，这里只做校正与后端同步。
 */
async function reconcileLang() {
  let lang = null;
  try {
    lang = await Promise.race([
      state.api.get_settings().then((s) => (s && s.lang === "zh" ? "zh" : "en")),
      new Promise((r) => setTimeout(() => r(null), 500)),
    ]);
  } catch (e) {
    lang = null;
  }
  if (lang === null) lang = detectLangSync() || "en";
  if (lang !== LANG) setLang(lang);
  else syncBackendLang();
}

// ---- 工具函数 ----

/** 把字节数格式化为易读字符串（不带符号）。 */
function fmtBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let v = Math.abs(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  const s = i === 0 ? String(v) : v.toFixed(v < 10 ? 2 : 1);
  return `${s} ${units[i]}`;
}

/** 把变化量格式化为带 +/− 号的字符串。 */
function fmtDelta(n) {
  if (n === 0) return "±0";
  return (n > 0 ? "+" : "−") + fmtBytes(n);
}

/** 把 Unix 时间戳格式化为本地「YYYY-MM-DD HH:mm」（完整、醒目）。 */
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 相对时间提示：「3 天前」「2 小时前」。 */
function fmtAgo(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return t("agoJustNow");
  if (s < 3600) return t("agoMin", Math.floor(s / 60));
  if (s < 86400) return t("agoHour", Math.floor(s / 3600));
  return t("agoDay", Math.floor(s / 86400));
}

function $(sel) { return document.querySelector(sel); }

/** 由扫描根 + 相对路径拼出完整 Windows 路径。 */
function fullPath(root, rel) {
  if (!rel) return root;
  return root.replace(/[\\/]+$/, "") + "\\" + rel;
}

/** 复制文本到剪贴板（clipboard API 不可用时回落 execCommand）。 */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e2) {}
    ta.remove();
    return ok;
  }
}

/** 短暂弹出一条 toast 提示。 */
let toastTimer = null;
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

// ---- 快照列表 ----

async function loadSnapshots() {
  state.snapshots = await state.api.list_snapshots();
  renderSnapshotList();
  updatePickers();
}

/** 快照列表的排序方式。 */
const SNAP_SORTERS = {
  "time-desc": (a, b) => b.scanned_at - a.scanned_at,
  "time-asc": (a, b) => a.scanned_at - b.scanned_at,
  "name-asc": (a, b) => a.root.localeCompare(b.root, cmpLocale()),
  "name-desc": (a, b) => b.root.localeCompare(a.root, cmpLocale()),
};

function renderSnapshotList() {
  const list = $("#snapshotList");
  list.innerHTML = "";
  if (state.snapshots.length === 0) {
    list.innerHTML = `<div class="side-empty">${t("noSnapshots")}</div>`;
    return;
  }
  const ordered = [...state.snapshots].sort(
    SNAP_SORTERS[state.snapSort] || SNAP_SORTERS["time-desc"]
  );
  for (const s of ordered) {
    const isOld = s.path === state.oldPath;
    const isNew = s.path === state.newPath;

    const el = document.createElement("div");
    el.className =
      "snap" + (isOld ? " sel-old" : "") + (isNew ? " sel-new" : "");

    const role =
      (isOld ? `<span class="snap-role old-c">${t("snapRoleBase")}</span>` : "") +
      (isNew ? `<span class="snap-role new-c">${t("snapRoleCurrent")}</span>` : "");

    const zipTag = s.compressed
      ? `<span class="snap-zip" title="${escapeHtml(fmtBytes(s.file_size || 0))}">${t("tagCompressed")}</span>`
      : "";
    el.innerHTML = `
      <div class="snap-time">${fmtTime(s.scanned_at)}${role}${zipTag}</div>
      <div class="snap-root">${escapeHtml(s.root)}</div>
      <div class="snap-meta">${fmtAgo(s.scanned_at)} · ${fmtBytes(s.total_size)} · ${t("filesN", s.file_count.toLocaleString())}${s.compressed && s.file_size ? " · " + fmtBytes(s.file_size) : ""}</div>
      <div class="snap-acts">
        <button class="snap-act old" data-act="old">${t("setAsBase")}</button>
        <button class="snap-act new" data-act="new">${t("setAsCurrent")}</button>
        <button class="snap-act del" data-act="del">${t("delete")}</button>
      </div>`;

    el.querySelector('[data-act="old"]').onclick = () => selectSnapshot("old", s.path);
    el.querySelector('[data-act="new"]').onclick = () => selectSnapshot("new", s.path);
    el.querySelector('[data-act="del"]').onclick = () => deleteSnapshot(s.path);
    list.appendChild(el);
  }
}

async function deleteSnapshot(path) {
  let res;
  try {
    res = await state.api.delete_snapshot(path);
  } catch (err) {
    toast(t("deleteFailed", err), true);
    return;
  }
  if (res && res.error) {
    toast(res.error, true);
    return;
  }
  // 被删的快照若正参与对比，结果树已成无源之水，清掉回到空态。
  const inCompare = path === state.oldPath || path === state.newPath;
  if (state.oldPath === path) state.oldPath = "";
  if (state.newPath === path) state.newPath = "";
  if (state.compared && inCompare) resetCompareView();
  await loadSnapshots();
  toast(t("snapshotDeleted"));
}

/** 清空对比结果区域，回到初始空态（快照列表不动）。 */
function resetCompareView() {
  state.compared = false;
  state.compareRoot = "";
  state._topNodes = null;
  state._lastSummary = null;
  $("#summaryBar").classList.add("hidden");
  $("#skipWarn").classList.add("hidden");
  $("#tree").innerHTML = "";
  $("#emptyState").classList.remove("hidden");
}

// ---- 对比对象选择 ----

function selectSnapshot(which, path) {
  if (which === "old") state.oldPath = path;
  else state.newPath = path;
  updatePickers();
  renderSnapshotList();
}

/** 交换「基准」与「当前」；若已出过结果则立即按新方向重新对比。 */
function swapPick() {
  [state.oldPath, state.newPath] = [state.newPath, state.oldPath];
  updatePickers();
  renderSnapshotList();
  if (state.compared && state.oldPath && state.newPath) doCompare();
}

/** 清空基准与当前的选择，并撤下已出的对比结果。 */
function clearPick() {
  state.oldPath = "";
  state.newPath = "";
  updatePickers();
  renderSnapshotList();
  if (state.compared) resetCompareView();
}

function snapByPath(path) {
  return state.snapshots.find((s) => s.path === path);
}

function updatePickers() {
  const setPick = (role, path) => {
    const el = document.querySelector(`[data-role="${role}-value"]`);
    const s = snapByPath(path);
    if (s) {
      el.innerHTML =
        `${fmtTime(s.scanned_at)} <span class="path">· ${escapeHtml(s.root)}</span>`;
      el.classList.remove("placeholder");
    } else {
      el.textContent = t("pickPlaceholder");
      el.classList.add("placeholder");
    }
  };
  setPick("old", state.oldPath);
  setPick("new", state.newPath);

  const ok = state.oldPath && state.newPath && state.oldPath !== state.newPath;
  $("#compareBtn").disabled = !ok || state.comparing;
  $("#swapBtn").disabled = !(state.oldPath || state.newPath);
  $("#clearPickBtn").disabled = !(state.oldPath || state.newPath);
}

/** 打开快照选择下拉。 */
function openDropdown(which, anchor) {
  const dd = $("#dropdown");
  dd.innerHTML = "";
  if (state.snapshots.length === 0) {
    dd.innerHTML = `<div class="dd-empty">${t("noSnapshots")}</div>`;
  } else {
    for (const s of state.snapshots) {
      const item = document.createElement("div");
      item.className = "dd-item";
      item.innerHTML = `<div class="t">${fmtTime(s.scanned_at)}（${fmtAgo(s.scanned_at)}）</div>
        <div class="m">${escapeHtml(s.root)} · ${fmtBytes(s.total_size)}</div>`;
      item.onclick = () => {
        selectSnapshot(which, s.path);
        dd.classList.add("hidden");
      };
      dd.appendChild(item);
    }
  }
  const r = anchor.getBoundingClientRect();
  dd.style.left = `${r.left}px`;
  dd.style.top = `${r.bottom + 4}px`;
  dd.style.minWidth = `${r.width}px`;
  dd.classList.remove("hidden");
}

// ---- 对比与变化树 ----

async function doCompare() {
  if (state.comparing) return;
  state.comparing = true;
  const btn = $("#compareBtn");
  btn.disabled = true;
  // 任一侧是压缩包时，后端会先解压再对比，按钮文案区分一下。
  const needDecompress = [state.oldPath, state.newPath].some((p) => {
    const s = snapByPath(p);
    return s && s.compressed;
  });
  btn.textContent = needDecompress ? t("decompressing") : t("comparing");
  try {
    const res = await state.api.compare(state.oldPath, state.newPath);
    if (res.error) {
      toast(res.error, true);
      return;
    }
    state.compared = true;
    state.compareRoot = res.summary.new.root;
    state._lastSummary = res.summary;
    renderSummary(res.summary);
    renderTopLevel(res.nodes);
  } catch (err) {
    toast(t("compareFailed", err), true);
  } finally {
    state.comparing = false;
    btn.textContent = t("compare");
    updatePickers();
  }
}

/** 设置过滤并同步下拉框（不触发重渲染，渲染由调用方负责）。 */
function setFilter(f) {
  state.filter = f;
  $("#filterSel").value = f;
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

/** 过滤判定：某节点在当前过滤下是否显示。 */
function matchFilter(node) {
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
  // 条长基准：顶层模式用整棵树统一标尺，同级模式用本层最大值。
  const siblingMax = visible.reduce((m, n) => Math.max(m, Math.abs(n.delta)), 1);
  const ref = state.barBase === "sibling" ? siblingMax : (state._barRef || 1);

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

  const kindClass =
    node.kind === "incomparable" ? "incomparable"
    : node.delta > 0 ? "grow"
    : node.delta < 0 ? "shrink"
    : "unchanged";

  const row = document.createElement("div");
  row.className = `node ${kindClass}${node.is_dir ? " dir" : ""}`;
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

// ---- 扫描流程 ----

async function newScan() {
  const res = await state.api.choose_folder();
  if (!res.path) return;
  showScanOverlay(res.path);
  const started = await state.api.start_scan(res.path);
  if (started.error) {
    hideScanOverlay();
    toast(started.error, true);
  }
}

function showScanOverlay(root) {
  $("#scanFiles").textContent = t("scannedFiles", 0);
  $("#scanCurrent").textContent = root;
  $("#scanOverlay").classList.remove("hidden");
}
function hideScanOverlay() {
  $("#scanOverlay").classList.add("hidden");
}

/** 处理来自 Python 的事件推送。 */
function onPyEvent(event, payload) {
  switch (event) {
    case "scan-progress":
      $("#scanFiles").textContent = t("scannedFiles", payload.files.toLocaleString());
      $("#scanCurrent").textContent = payload.current;
      break;
    case "scan-done":
      hideScanOverlay();
      if (payload.warning) toast(payload.warning, true);
      else toast(t("scanDone"));
      loadSnapshots().then(() => {
        // 新快照自动选作「当前」，方便紧接着对比。
        selectSnapshot("new", payload.snapshot.path);
      });
      break;
    case "scan-cancelled":
      hideScanOverlay();
      toast(t("scanCancelled"));
      break;
    case "scan-error":
      hideScanOverlay();
      toast(t("scanError", payload.message), true);
      break;
  }
}
window.__onPyEvent = onPyEvent;

// ---- 设置（存放位置 / 扫描线程数）----

async function openSnapshotDir() {
  const res = await state.api.open_snapshot_dir();
  if (res.error) toast(res.error, true);
}

/** 读取设置并填充扫描线程数下拉框（1..CPU 核数，另附常用档位）与压缩开关。 */
async function loadSettings() {
  let s;
  try {
    s = await state.api.get_settings();
  } catch (e) {
    return;
  }
  const sel = $("#workerSel");
  const cpu = s.cpu_count || 2;
  const opts = [...new Set([1, 2, 4, 8, 16, cpu, s.scan_workers])]
    .filter((n) => n >= 1 && n <= Math.max(cpu, s.scan_workers))
    .sort((a, b) => a - b);
  sel.innerHTML = opts
    .map((n) => `<option value="${n}">${n}${n === cpu ? t("cpuTag") : ""}</option>`)
    .join("");
  sel.value = String(s.scan_workers);
  const chk = $("#compressChk");
  if (chk) chk.checked = !!s.compress_snapshots;
}

async function changeScanWorkers(n) {
  const res = await state.api.set_scan_workers(Number(n));
  if (res.error) toast(res.error, true);
  else toast(t("workersSet", res.scan_workers));
}

async function changeCompress(enabled) {
  const res = await state.api.set_compress_snapshots(!!enabled);
  if (res && res.error) toast(res.error, true);
  else toast(res.compress_snapshots ? t("compressOn") : t("compressOff"));
}

// ---- 主题 ----

function syncTitlebarTheme() {
  // 原生标题栏归系统画。不 await：界面先可点，主题在后台跟上。
  // 后端对相同主题会 short-circuit，启动连打也不贵。
  const dark = document.documentElement.dataset.theme === "dark";
  try {
    if (state.api && state.api.set_theme) {
      state.api.set_theme(dark ? "dark" : "light");
    }
  } catch (e) {}
}

function applyThemeButton() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("#themeToggle").textContent = dark ? t("themeDark") : t("themeLight");
  syncTitlebarTheme();
}

function toggleTheme() {
  const html = document.documentElement;
  html.dataset.theme = html.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("theme", html.dataset.theme); } catch (e) {}
  applyThemeButton();
}

function restoreThemePreference() {
  try {
    const saved = localStorage.getItem("theme");
    if (saved === "dark" || saved === "light") {
      document.documentElement.dataset.theme = saved;
    }
  } catch (e) {}
}

/** 启动时再补一次即可；后端有延迟重绘，不必前端连打四次。 */
function scheduleTitlebarSync() {
  syncTitlebarTheme();
  setTimeout(() => { syncTitlebarTheme(); }, 200);
}

const GITHUB_URL = "https://github.com/Kami958/WhoShitsonMyC";

async function openGitHub() {
  try {
    const res = await state.api.open_url(GITHUB_URL);
    if (res && res.error) toast(res.error, true);
  } catch (e) {
    toast(String(e), true);
  }
}

// ---- 杂项 ----

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

function wireEvents() {
  $("#newScanBtn").onclick = newScan;
  $("#compareBtn").onclick = doCompare;
  $("#swapBtn").onclick = swapPick;
  $("#clearPickBtn").onclick = clearPick;
  $("#cancelScanBtn").onclick = () => state.api.cancel_scan();
  $("#themeToggle").onclick = toggleTheme;
  $("#langToggle").onclick = () => setLang(LANG === "zh" ? "en" : "zh");
  $("#githubBtn").onclick = openGitHub;
  $("#openDirBtn").onclick = openSnapshotDir;
  $("#workerSel").onchange = (e) => changeScanWorkers(e.target.value);
  const compressChk = $("#compressChk");
  if (compressChk) {
    compressChk.onchange = (e) => changeCompress(e.target.checked);
  }

  $("#snapSortSel").onchange = (e) => {
    state.snapSort = e.target.value;
    renderSnapshotList();
  };

  $("#pickOld").onclick = (e) => openDropdown("old", e.currentTarget);
  $("#pickNew").onclick = (e) => openDropdown("new", e.currentTarget);

  $("#filterSel").onchange = (e) => {
    setFilter(e.target.value);
    if (state.compared && state._topNodes) renderTopLevel(state._topNodes);
  };

  $("#sortSel").onchange = (e) => {
    state.sort = e.target.value;
    if (state.compared && state._topNodes) renderTopLevel(state._topNodes);
  };

  $("#barBaseSel").onchange = (e) => {
    state.barBase = e.target.value;
    if (state.compared && state._topNodes) renderTopLevel(state._topNodes);
  };

  for (const item of document.querySelectorAll(".ctx-item")) {
    item.onclick = () => ctxCommand(item.dataset.cmd);
  }

  // 点击空白关闭下拉与右键菜单
  document.addEventListener("click", (e) => {
    const dd = $("#dropdown");
    if (!dd.classList.contains("hidden") &&
        !dd.contains(e.target) &&
        !e.target.closest(".pick")) {
      dd.classList.add("hidden");
    }
    if (!e.target.closest(".ctx-menu")) closeCtxMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeCtxMenu();
  });

  restoreThemePreference();
  applyThemeButton();
  scheduleTitlebarSync();
  setFilter(state.filter);
}

// ---- 启动 ----

function boot() {
  state.api = window.pywebview.api;
  // 主题尽早 fire-and-forget；列表/设置不互相 await，并行拉，首屏更快可点。
  restoreThemePreference();
  wireEvents();
  scheduleTitlebarSync();
  reconcileLang();
  loadSnapshots();
  loadSettings();
}

// 首屏即按浏览器语言渲染静态文案（同步、无闪烁）；boot 后再与后端校对。
// 主题偏好也尽早写到 <html>，避免界面暗、标题栏却按另一套来。
LANG = detectLangSync() || "en";
restoreThemePreference();
applyStaticI18n();

// pywebview 注入 API 后触发 pywebviewready；若已就绪则直接启动。
if (window.pywebview && window.pywebview.api) {
  boot();
} else {
  window.addEventListener("pywebviewready", boot);
}

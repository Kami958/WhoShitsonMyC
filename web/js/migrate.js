/* 目录链接管理面板：工具创建的目录链接的列表与维护（打开存放位置 / 还原 / 删除 / 备份）。 */
"use strict";

// ---- 状态 ----
const _linkMgr = {
  records: [],
  restoring: "",
};
let _linkHintTimer = null;
// 再次迁移弹窗的状态：当前记录、旧迁移的处理方式；输入校验用独立的防抖计时器
const _remigrate = { rec: null, oldAction: "keep" };
let _remigrateHintTimer = null;
const _migrateQueue = {
  items: [],
  running: false,
  paused: false,
  activeId: null,
  editingId: null,
  seq: 0,
  verifyToasting: false,
  verifyToasted: false,
};
// 历史视图的本地缓存、路径搜索、操作筛选；都用于纯前端过滤，避免反复打后端。
let _historyItems = [];
let _historyQuery = "";
let _historyOp = "all";
// 链接列表视图的本地缓存、路径搜索、排序选项
let _linkQuery = "";
let _linkSort = "time-desc";
// 进入迁移表单前停在哪一个视图；表单的 ✕ 与 [取消] 据此回到原处
let _migrateFormBackView = "list";

function wireLinkUi() {
  const newBtn = $("#linkNewBtn");
  if (newBtn) newBtn.onclick = () => openMigrateForm("");
  const refreshBtn = $("#linkRefreshBtn");
  if (refreshBtn) refreshBtn.onclick = () => refreshLinkList(true);
  const importBtn = $("#linkImportBtn");
  if (importBtn) importBtn.onclick = () => openLinkImportDialog();
  const importPickBtn = $("#linkImportPickBtn");
  if (importPickBtn) importPickBtn.onclick = () => pickLinkImportPath("link");
  const importTargetPickBtn = $("#linkImportTargetPickBtn");
  if (importTargetPickBtn) importTargetPickBtn.onclick = () => pickLinkImportPath("target");
  const importCloseBtn = $("#linkImportCloseBtn");
  if (importCloseBtn) importCloseBtn.onclick = () => closeLinkImportDialog();
  const importCancelBtn = $("#linkImportCancelBtn");
  if (importCancelBtn) importCancelBtn.onclick = () => closeLinkImportDialog();
  const importOkBtn = $("#linkImportOkBtn");
  if (importOkBtn) importOkBtn.onclick = () => runLinkImport();
  const importOverlay = $("#linkImportOverlay");
  if (importOverlay) {
    importOverlay.onclick = (e) => {
      if (e.target === importOverlay) closeLinkImportDialog();
    };
  }
  // 两个路径框任一变动都重算 [导入] 的可点状态；回车直接提交
  for (const sel of ["#linkImportPath", "#linkImportTarget"]) {
    const input = $(sel);
    if (!input) continue;
    input.oninput = () => {
      input.classList.remove("is-error");
      updateLinkImportOk();
    };
    input.onkeydown = (e) => {
      if (e.key === "Enter") runLinkImport();
    };
  }
  // 标题行的 ✕ 与底部的 [取消] 是同一个出口，都回进入表单前的那个视图：
  // 从迁移队列进来就回队列，从迁移列表进来就回列表。
  const backFromForm = () => setLinkView(_migrateFormBackView);
  const formCloseBtn = $("#migrateFormCloseBtn");
  if (formCloseBtn) formCloseBtn.onclick = backFromForm;
  const cancelBtn = $("#migrateCancelBtn");
  if (cancelBtn) cancelBtn.onclick = backFromForm;
  // 左下角的[返回队列]只管去队列，从队列进来的表单会藏起它，不再与[取消]重复
  const backQueueBtn = $("#migrateBackQueueBtn");
  if (backQueueBtn) backQueueBtn.onclick = backQueueFromForm;

  const srcBrowse = $("#migrateSourceBrowseBtn");
  if (srcBrowse) {
    srcBrowse.onclick = async () => {
      const res = await state.api.choose_folder();
      if (res && res.path) {
        const si = $("#migrateSourceInput");
        if (si) si.value = res.path;
        refreshMigrateHint();
      }
    };
  }
  const dstBrowse = $("#migrateDestBrowseBtn");
  if (dstBrowse) {
    dstBrowse.onclick = async () => {
      const res = await state.api.choose_folder();
      if (res && res.path) {
        // 选的是存放位置的父目录，自动拼上访问路径的目录名作为完整存放位置（可再编辑）
        const si = $("#migrateSourceInput");
        const src = si ? String(si.value || "").trim() : "";
        const base = src ? String(src).replace(/[\\/]+$/, "").split(/[\\/]/).pop() : "";
        const di = $("#migrateDestInput");
        if (di) di.value = base ? res.path.replace(/[\\/]+$/, "") + "\\" + base : res.path;
        refreshMigrateHint();
      }
    };
  }
  const si = $("#migrateSourceInput");
  if (si) si.addEventListener("input", refreshMigrateHint);
  const di = $("#migrateDestInput");
  if (di) di.addEventListener("input", refreshMigrateHint);

  const enqueueBtn = $("#migrateEnqueueBtn");
  if (enqueueBtn) enqueueBtn.onclick = enqueueMigrateItem;
  const saveBtn = $("#migrateSaveBtn");
  if (saveBtn) saveBtn.onclick = saveMigrateEdit;

  const restoreCancelBtn = $("#restoreCancelBtn");
  if (restoreCancelBtn) {
    restoreCancelBtn.onclick = async () => {
      try { await state.api.cancel_directory_restore(); } catch (e) {}
    };
  }

  // ---- 再次迁移弹窗 ----
  const rmClose = $("#remigrateCloseBtn");
  if (rmClose) rmClose.onclick = closeRemigrateDialog;
  const rmCancel = $("#remigrateCancelBtn");
  if (rmCancel) rmCancel.onclick = closeRemigrateDialog;
  const rmOverlay = $("#remigrateOverlay");
  if (rmOverlay) {
    rmOverlay.onclick = (e) => {
      if (e.target === rmOverlay) closeRemigrateDialog();
    };
  }
  const rmBrowse = $("#remigrateDestBrowseBtn");
  if (rmBrowse) {
    rmBrowse.onclick = async () => {
      const res = await state.api.choose_folder();
      if (!res || !res.path) return;
      // 选的是存放位置的父目录，自动拼上旧迁移的目录名作为完整存放位置（可再编辑）
      const rec = _remigrate.rec;
      const base = rec ? String(rec.target || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() : "";
      const di = $("#remigrateDestInput");
      if (di) di.value = base ? res.path.replace(/[\\/]+$/, "") + "\\" + base : res.path;
      refreshRemigrateHint();
    };
  }
  const rmDest = $("#remigrateDestInput");
  if (rmDest) rmDest.addEventListener("input", refreshRemigrateHint);
  const rmStart = $("#remigrateStartBtn");
  if (rmStart) rmStart.onclick = startRemigrate;
  document.querySelectorAll('input[name="remigrateOldAction"]').forEach((el) => {
    el.onchange = () => {
      if (el.checked) _remigrate.oldAction = el.value;
    };
  });

  const list = $("#linkList");
  if (list) {
    list.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const idx = Number(btn.getAttribute("data-key"));
      const rec = _linkMgr.records[idx];
      if (rec) onLinkAction(btn.getAttribute("data-act"), rec);
    });
  }

  // ---- 迁移列表 / 迁移队列 / 迁移历史 视图切换 ----
  const viewList = $("#linkViewList");
  const viewQueue = $("#linkViewQueue");
  const viewHistory = $("#linkViewHistory");
  if (viewList && viewQueue && viewHistory) {
    viewList.onclick = () => setLinkView("list");
    viewQueue.onclick = () => setLinkView("queue");
    viewHistory.onclick = () => setLinkView("history");
    // 初次进入停在列表视图，本地无需预取历史（切换到历史时才拉取）
    setLinkView("list");
  }

  // ---- 迁移队列：工具栏与项操作 ----
  // 队列底部的[新建迁移]与迁移列表的同名按钮同一去向，不必先退回列表视图
  const qNew = $("#queueNewBtn");
  if (qNew) qNew.onclick = () => openMigrateForm("");
  const qStart = $("#queueStartBtn");
  if (qStart) qStart.onclick = startMigrateQueue;
  const qPause = $("#queuePauseBtn");
  if (qPause) qPause.onclick = pauseMigrateQueue;
  const qCancel = $("#queueCancelBtn");
  if (qCancel) qCancel.onclick = cancelMigrateQueue;
  const qList = $("#linkQueueList");
  if (qList) {
    qList.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-qa]");
      if (!btn) return;
      const id = Number(btn.getAttribute("data-qid"));
      const act = btn.getAttribute("data-qa");
      if (act === "edit") openMigrateEdit(id);
      else if (act === "retry") retryQueueItem(id);
      else removeQueueItem(id);
    });
  }
  const histClear = $("#linkHistoryClearBtn");
  if (histClear) histClear.onclick = clearMigrationHistory;

  // 历史搜索：实时过滤路径/操作/状态；清空键还原全部。
  const histSearch = $("#linkHistorySearchInput");
  const histSearchClear = $("#linkHistorySearchClear");
  if (histSearch) {
    histSearch.addEventListener("input", () => {
      _historyQuery = histSearch.value || "";
      const has = !!_historyQuery.trim();
      if (histSearchClear) histSearchClear.classList.toggle("hidden", !has);
      renderMigrationHistory();
    });
  }
  if (histSearchClear) {
    histSearchClear.onclick = () => {
      if (histSearch) histSearch.value = "";
      _historyQuery = "";
      histSearchClear.classList.add("hidden");
      renderMigrationHistory();
      if (histSearch) histSearch.focus();
    };
  }
  // 操作筛选：迁移 / 还原 / 删除备份；默认「全部」
  // 只认带 data-op 的筛选按钮：清空按钮共用 .link-history-filter-btn 样式，
  // 一并选中会覆盖它自己的 onclick。
  document.querySelectorAll(".link-history-filter-btn[data-op]").forEach((btn) => {
    btn.onclick = () => {
      const op = btn.getAttribute("data-op") || "all";
      _historyOp = op;
      document.querySelectorAll(".link-history-filter-btn[data-op]").forEach((b) =>
        b.classList.toggle("active", b === btn)
      );
      renderMigrationHistory();
    };
  });
  // 历史项与迁移列表里的路径点击 → 资源管理器打开
  bindPathOpenClicks($("#linkHistoryList"));
  bindPathOpenClicks($("#linkList"));

  // 反查记录的[查看结果]：用历史里存下的链接清单开详情弹窗，不再重跑查询
  const histList = $("#linkHistoryList");
  if (histList) {
    histList.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-hist-id]");
      if (!btn || typeof showLinkFindDetail !== "function") return;
      const id = btn.getAttribute("data-hist-id") || "";
      const rec = _historyItems.find((r) => r && String(r.id || "") === id);
      if (rec) showLinkFindDetail(rec);
    });
  }

  // ---- 链接列表搜索 ----
  const linkSearch = $("#linkListSearchInput");
  const linkSearchClear = $("#linkListSearchClear");
  if (linkSearch) {
    linkSearch.addEventListener("input", () => {
      _linkQuery = linkSearch.value || "";
      if (linkSearchClear) linkSearchClear.classList.toggle("hidden", !_linkQuery.trim());
      renderLinkList();
    });
  }
  if (linkSearchClear) {
    linkSearchClear.onclick = () => {
      if (linkSearch) linkSearch.value = "";
      _linkQuery = "";
      linkSearchClear.classList.add("hidden");
      renderLinkList();
      if (linkSearch) linkSearch.focus();
    };
  }

  // ---- 链接列表排序：菜单复用 .icon-menu + .icon-menu-item / .sort-menu-* 样式 ----
  const sortBtn = $("#linkListSortBtn");
  const sortMenu = $("#linkListSortMenu");
  const syncLinkSortChrome = () => {
    const { key, dir } = _splitLinkSort(_linkSort);
    const opt = LINK_SORT_OPTIONS.find((o) => o.key === key) || LINK_SORT_OPTIONS[0];
    if (sortBtn) {
      sortBtn.classList.toggle("is-active", _linkSort !== "time-desc");
      sortBtn.setAttribute("aria-expanded", "false");
      sortBtn.title = opt ? `${t(opt.i18nKey)} · ${t(dir === "asc" ? "pendingSortAsc" : "pendingSortDesc")}` : t("linkListSortTitle");
    }
  };
  const closeLinkSortMenu = () => {
    if (sortMenu) sortMenu.classList.add("hidden");
    if (sortBtn) sortBtn.setAttribute("aria-expanded", "false");
  };
  const openLinkSortMenu = (anchor) => {
    if (!sortMenu || !anchor) return;
    if (!sortMenu.classList.contains("hidden")) {
      closeLinkSortMenu();
      return;
    }
    // tool-panel 内含 layout 容器，fixed 菜单会按子层定位错，挪到 body
    if (sortMenu.parentElement !== document.body) document.body.appendChild(sortMenu);
    sortMenu.innerHTML = "";
    const current = _splitLinkSort(_linkSort);
    for (const opt of LINK_SORT_OPTIONS) {
      const isSelected = opt.key === current.key;
      const dir = isSelected ? current.dir : opt.dir;
      const item = document.createElement("button");
      item.type = "button";
      item.className = "icon-menu-item" + (isSelected ? " is-selected" : "");
      item.setAttribute("role", "menuitemradio");
      item.setAttribute("aria-checked", isSelected ? "true" : "false");
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
        const nextDir = isSelected ? (current.dir === "asc" ? "desc" : "asc") : opt.dir;
        _linkSort = `${opt.key}-${nextDir}`;
        syncLinkSortChrome();
        renderLinkList();
        closeLinkSortMenu();
      };
      sortMenu.appendChild(item);
    }
    const r = anchor.getBoundingClientRect();
    sortMenu.classList.remove("hidden");
    const mw = sortMenu.offsetWidth || 180;
    let left = r.right - mw;
    if (left < 8) left = 8;
    if (left + mw > window.innerWidth - 8) left = Math.max(8, window.innerWidth - mw - 8);
    let top = r.bottom + 4;
    const mh = sortMenu.offsetHeight || 160;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
    sortMenu.style.left = `${left}px`;
    sortMenu.style.top = `${top}px`;
    if (sortBtn) sortBtn.setAttribute("aria-expanded", "true");
  };
  if (sortBtn) sortBtn.onclick = () => openLinkSortMenu(sortBtn);
  // 点空白处关菜单（与 pending 排序菜单行为一致）
  const onLinkSortDocClick = (e) => {
    if (!sortMenu || sortMenu.classList.contains("hidden")) return;
    if (e.target.closest("#linkListSortMenu") || e.target.closest("#linkListSortBtn")) return;
    closeLinkSortMenu();
  };
  document.addEventListener("click", onLinkSortDocClick);
  syncLinkSortChrome();

  // 初次进入时静默拉一次链接列表、历史计数，并把空队列渲染出来
  refreshLinkList(false);
  refreshMigrationHistoryCount();
  renderMigrateQueue();
}

/** 切换「迁移列表 / 迁移队列 / 迁移历史」视图；切到哪个视图就拉哪个的数据。 */
function setLinkView(view) {
  const views = {
    list: $("#linkListView"),
    queue: $("#linkQueueView"),
    history: $("#linkHistoryView"),
  };
  const tabs = {
    list: $("#linkViewList"),
    queue: $("#linkViewQueue"),
    history: $("#linkViewHistory"),
  };
  if (!views.list || !views.queue || !views.history) return;
  // 表单是独立视图。点视图按钮等于离开表单，未保存的编辑就此放弃。
  const create = $("#linkCreate");
  if (create && !create.classList.contains("hidden")) {
    create.classList.add("hidden");
    endMigrateEdit();
  }
  const current = views[view] ? view : "list";
  for (const key of Object.keys(views)) {
    const on = key === current;
    views[key].classList.toggle("hidden", !on);
    tabs[key].classList.toggle("active", on);
    tabs[key].setAttribute("aria-selected", on ? "true" : "false");
  }
  if (current === "history") refreshMigrationHistory();
  else if (current === "queue") renderMigrateQueue();
  else refreshLinkList(false);
}

/** 从对比树右键/搜索项带路径进入链接面板的新建迁移。 */
function openMigrateTab(sourceAbs) {
  if (typeof openToolPanel === "function") openToolPanel("link");
  else if (typeof switchToolTab === "function") switchToolTab("link");
  if (sourceAbs) openMigrateForm(sourceAbs);
  else setLinkView("queue");
}

/** 只切到表单视图，三个列表视图一起藏起来；进表单前记下当前视图，退出表单时回到那里。 */
function showMigrateFormView() {
  const create = $("#linkCreate");
  // 表单已经开着就保留原来源：例如编辑队列项途中从对比树再进来，出口仍该回队列
  if (!create || create.classList.contains("hidden")) {
    const queue = $("#linkQueueView");
    _migrateFormBackView = queue && !queue.classList.contains("hidden") ? "queue" : "list";
  }
  if (create) create.classList.remove("hidden");
  ["#linkListView", "#linkQueueView", "#linkHistoryView"].forEach((sel) => {
    const el = $(sel);
    if (el) el.classList.add("hidden");
  });
}

/** 打开迁移表单视图；字段内容由调用方负责填。 */
function showLinkCreate() {
  showMigrateFormView();
  syncMigrateFormChrome();
  refreshMigrateHint();
}

/**
 * 进入新建模式；正在编辑别的任务且表单有改动时先确认放弃。
 * 新建迁移从空白开始，上一次编辑留下的路径与备份处理都在这里清掉，
 * 只有调用方给了路径（从对比树右键进入）才填进访问路径。
 */
async function openMigrateForm(sourceAbs) {
  if (!(await closeMigrateEdit(true))) return;
  const si = $("#migrateSourceInput");
  const di = $("#migrateDestInput");
  if (si) si.value = String(sourceAbs || "");
  if (di) di.value = "";
  setMigrateBackupAction("keep");
  showLinkCreate();
}

/** 退出表单回到队列视图；编辑中的任务一并结束编辑会话。 */
function hideLinkCreate() {
  const create = $("#linkCreate");
  if (create) create.classList.add("hidden");
  endMigrateEdit();
  setLinkView("queue");
}

/** 表单左下角的[返回队列]：回到队列视图，与[取消]、[✕] 一样不再挽留未保存的改动。 */
function backQueueFromForm() {
  hideLinkCreate();
}

// ---- 链接列表 ----

// 排序项：key + 默认方向。点击同一项切换正倒序。
const LINK_SORT_OPTIONS = [
  { key: "time", dir: "desc", i18nKey: "linkListSortTime" },     // 默认：最近迁移在前
  { key: "files", dir: "desc", i18nKey: "linkListSortFiles" },
  { key: "size", dir: "desc", i18nKey: "linkListSortSize" },
  { key: "path", dir: "asc", i18nKey: "linkListSortPath" },
];

function _splitLinkSort(v) {
  const s = String(v || "time-desc");
  const i = s.lastIndexOf("-");
  return i > 0 ? { key: s.slice(0, i), dir: s.slice(i + 1) } : { key: s, dir: "desc" };
}

function sortedLinkRecords(items) {
  const list = Array.isArray(items) ? items.slice() : [];
  const { key, dir } = _splitLinkSort(_linkSort);
  const loc = typeof cmpLocale === "function" ? cmpLocale() : undefined;
  const byPath = (a, b) =>
    String(a.link || "").localeCompare(String(b.link || ""), loc);
  let cmp;
  if (key === "files") {
    cmp = (a, b) =>
      (Number(a.files) || 0) - (Number(b.files) || 0) || byPath(a, b);
  } else if (key === "size") {
    cmp = (a, b) =>
      (Number(a.bytes) || 0) - (Number(b.bytes) || 0) || byPath(a, b);
  } else if (key === "path") {
    cmp = (a, b) => byPath(a, b);
  } else {
    // time：created_at 越大越新，desc 时反过来
    cmp = (a, b) =>
      (Number(a.created_at) || 0) - (Number(b.created_at) || 0) || byPath(a, b);
  }
  if (dir === "desc") {
    const asc = cmp;
    cmp = (a, b) => -asc(a, b);
  }
  list.sort(cmp);
  return list;
}

function renderLinkList() {
  const list = $("#linkList");
  const empty = $("#linkEmpty");
  if (!list) return;
  const q = _linkQuery.trim();
  let filtered = _linkMgr.records;
  if (q) {
    const needle = q.toLowerCase();
    filtered = filtered.filter((r) =>
      [String(r.link || ""), String(r.target || ""), String(r.backup || "")]
        .join(" ")
        .toLowerCase()
        .includes(needle)
    );
  }
  const sorted = sortedLinkRecords(filtered);
  if (!sorted.length) {
    list.innerHTML = "";
    if (empty) {
      // 区分「真没链接」与「被搜索过滤掉了」两种空
      const filteredOut = _linkMgr.records.length && (q || _linkSort !== "time-desc");
      empty.textContent = filteredOut
        ? t("linkListNoMatch")
        : t("linkEmpty");
      empty.classList.remove("hidden");
    }
    return;
  }
  if (empty) empty.classList.add("hidden");
  list.innerHTML = sorted.map((rec, i) => linkCard(rec, i)).join("");
}

async function refreshLinkList(force) {
  if (!state.api || !state.api.list_directory_links) return;
  let res;
  try {
    res = await state.api.list_directory_links();
  } catch (e) {
    return;
  }
  _linkMgr.records = (res && res.links) || [];
  renderLinkList();
}

/** state → 卡片状态类名与状态标签的 i18n key。 */
const LINK_STATE_META = {
  ok: { cls: "is-ok", key: "linkStateOk" },
  target_missing: { cls: "is-target-missing", key: "linkStateTargetMissing" },
  link_missing: { cls: "is-link-missing", key: "linkStateLinkMissing" },
};

/** 卡片里的文字操作按钮。variant 为 ""（次要）/ "is-primary" / "is-danger"。
    tipKey 是悬停提示的 i18n 键（描述动作作用，不重复按钮文案）。 */
function linkActBtn(variant, act, idx, label, tipKey, disabled) {
  const cls = variant ? "link-btn " + variant : "link-btn";
  const textHtml = label ? `<span class="link-btn-text">${escapeHtml(label)}</span>` : "";
  const dis = disabled ? " disabled" : "";
  const tip = tipKey ? ` title="${escapeHtml(t(tipKey))}"` : "";
  return `<button type="button" class="${cls}" data-act="${act}" data-key="${idx}"${tip}${dis}>${textHtml}</button>`;
}

/** 列表里的路径按钮（带 data-open）点开 → 在资源管理器中打开；失败 toast。 */
function bindPathOpenClicks(list) {
  if (!list) return;
  list.addEventListener("click", (ev) => {
    const a = ev.target.closest("[data-open]");
    if (!a || !state.api) return;
    const p = String(a.getAttribute("data-open") || "");
    if (!p) return;
    state.api.open_folder(p).then((res) => {
      if (res && res.error) toast(res.error, true);
    });
  });
}

/** 路径两行：左小标签（"访问路径"/"存放位置"），右路径。路径可点击，在资源管理器中打开。 */
function linkCardPath(labelKey, path, opts) {
  const o = opts || {};
  const dim = o.dim ? " is-dim" : "";
  return `<div class="link-card-row">` +
    `<span class="link-card-row-label">${escapeHtml(t(labelKey))}</span>` +
    `<button type="button" class="link-card-row-value link-open-path${dim}" data-open="${escapeHtml(path)}" title="${escapeHtml(path)}">${escapeHtml(path)}</button>` +
    `</div>`;
}

/** 元信息行：状态点 + 状态文案 + 文件/字节数。状态是健康度，不当标题用。 */
function linkCardMeta(rec, meta) {
  const bits = [t(meta.key)];
  const files = Number(rec.files) || 0;
  const bytes = Number(rec.bytes) || 0;
  if (files) bits.push(`${files} ${t("linkFiles")}`);
  if (bytes) bits.push(fmtBytes(bytes));
  return `<div class="link-card-meta">` +
    `<span class="link-card-dot" aria-hidden="true"></span>` +
    `<span class="link-card-meta-text">${escapeHtml(bits.join(" · "))}</span>` +
    `</div>`;
}

function linkCard(rec, idx) {
  const meta = LINK_STATE_META[rec.state] || LINK_STATE_META.link_missing;
  const link = String(rec.link || "");
  const target = String(rec.target || "");

  // 状态差的卡片，整张卡片铺一层极淡的语义色底，让问题卡片自己跳出来。
  const washCls =
    rec.state === "target_missing" ? " is-target-wash" :
    rec.state === "link_missing" ? " is-link-wash" : "";

  const sourceRow = linkCardPath("linkSource", link);
  const targetRow = target
    ? linkCardPath("linkTarget", target, { dim: rec.state === "target_missing" })
    : "";

  const metaRow = linkCardMeta(rec, meta);

  const busy = !!_linkMgr.restoring;

  // 备份行：路径 + 内联删除按钮，放在存放位置那一行下方。
  // 导入的记录从来没有过备份，不是备份被删了，显示成「无备份」占位。
  const hasBackup = !!(rec.backup && rec.backup_exists);
  const backupAct = hasBackup
    ? linkActBtn("is-danger", "delbackup", idx, t("linkDeleteBackup"), "linkDelBackupTip", busy)
    : "";
  const backupRow =
    `<div class="link-card-backup">` +
    `<span class="link-card-row-label">${escapeHtml(t("linkBackup"))}</span>` +
    (rec.backup
      ? `<button type="button" class="link-card-backup-path link-open-path${rec.backup_exists ? "" : " is-dim"}" data-open="${escapeHtml(rec.backup)}" title="${escapeHtml(rec.backup)}">` +
        escapeHtml(rec.backup) +
        (rec.backup_exists ? "" : ` <span class="link-card-dim">(${escapeHtml(t("linkBackupGone"))})</span>`) +
        `</button>`
      : `<span class="link-card-dim">${escapeHtml(t("linkBackupNone"))}</span>`) +
    backupAct +
    `</div>`;

  // 再次迁移留下的旧数据：旧迁移的路径是记录里的稳定字段，事件里的 old_target_left 只作兜底
  const oldLeft = String(rec.old_target || rec.old_target_left || "");
  const oldRow = oldLeft
    ? `<div class="link-card-backup">` +
      `<span class="link-card-row-label">${escapeHtml(t("linkOldTarget"))}</span>` +
      `<button type="button" class="link-card-backup-path link-open-path" data-open="${escapeHtml(oldLeft)}" title="${escapeHtml(oldLeft)}">${escapeHtml(oldLeft)}</button>` +
      linkActBtn("is-danger", "deloldtarget", idx, t("linkDeleteOldTarget"), "linkDelOldTargetTip", busy) +
      `</div>`
    : "";

  const acts = [];
  // 备份早被删掉的记录：不显示「备份」按钮，还原提示也不提备份。
  // 从来没有备份的（导入进来的）连 [还原] 都不给，还原没有数据可退。
  // 只有工具建的链接、且存放位置还在时才有数据可搬
  if (rec.is_link && rec.target_exists) acts.push(linkActBtn("", "remigrate", idx, t("linkRemigrate"), "linkRemigrateTip", busy));
  if (hasBackup) acts.push(linkActBtn("", "openbackup", idx, t("linkOpenBackup"), "linkOpenBackupTip", busy));
  if (rec.state === "ok" && rec.has_backup) acts.push(linkActBtn("", "restore", idx, t("linkRestore"), hasBackup ? "linkRestoreTip" : "linkRestoreNoBackupTip", busy));
  if (rec.is_link) acts.push(linkActBtn("is-danger", "delete", idx, t("linkDelete"), "linkDeleteTip", busy));
  if (rec.state === "link_missing") acts.push(linkActBtn("is-danger", "remove", idx, t("linkRemoveRecord"), "linkRemoveRecordTip", busy));

  const actionRow = acts.length
    ? `<div class="link-card-actions">${acts.join("")}</div>`
    : "";

  return `<div class="link-card ${meta.cls}${washCls}">` +
    `<div class="link-card-body">` +
    sourceRow +
    targetRow +
    backupRow +
    oldRow +
    metaRow +
    `</div>` +
    actionRow +
    `</div>`;
}

async function onLinkAction(act, rec) {
  if (act === "openbackup") {
    const res = await state.api.open_folder(rec.backup);
    if (res && res.error) toast(res.error, true);
    return;
  }
  if (act === "delete") {
    const ok = await showConfirmDialog({
      title: t("linkDeleteTitle"),
      message: t("linkDeleteConfirm", rec.link),
      okText: t("linkDelete"),
      danger: true,
    });
    if (!ok) return;
    const res = await state.api.delete_directory_link(rec.link);
    if (res && res.error) toast(res.error, true);
    else {
      toast(t("linkDeleted"));
      refreshLinkList(true);
    }
    return;
  }
  if (act === "restore") {
    if (_linkMgr.restoring) { toast(t("linkRestoreBusy"), true); return; }
    // 备份早被删掉的记录没有备份可处理：文案不提备份，也不给彻底删除的勾选
    const hasBackup = !!(rec.backup && rec.backup_exists);
    const ok = await showConfirmDialog({
      title: t("linkRestoreTitle"),
      message: hasBackup
        ? t("linkRestoreConfirm", rec.link, rec.target)
        : t("linkRestoreConfirmNoBackup", rec.link, rec.target),
      messageChecked: hasBackup
        ? t("linkRestorePermanentConfirm", rec.link, rec.target)
        : "",
      checkboxLabel: hasBackup ? t("pendingPermanent") : "",
      okText: t("linkRestore"),
      danger: true,
    });
    if (!ok) return;
    const permanent = !!(ok && ok.checked);
    const res = await state.api.restore_directory_link(rec.link, permanent);
    if (res && res.error) { toast(res.error, true); return; }
    // 后台线程执行；进度经 migrate-dir-restore-* 事件推送
    _linkMgr.restoring = rec.link;
    refreshLinkList(true);
    showRestoreProgress();
    return;
  }
  if (act === "delbackup") {
    // 勾选彻底删除：正文、确定按钮都换成不进回收站的说法
    const ok = await showConfirmDialog({
      title: t("linkDeleteBackupTitle"),
      message: t("linkDeleteBackupConfirm", rec.backup),
      messageChecked: t("linkDeleteBackupPermanentConfirm", rec.backup),
      okText: t("deleteToRecycle"),
      okTextChecked: t("deletePermanent"),
      checkboxLabel: t("pendingPermanent"),
      danger: true,
    });
    if (!ok) return;
    const permanent = !!(ok && ok.checked);
    // 彻底删除一棵备份树可能要很久，先把进度卡显出来（回收站不到一秒，不闪）
    if (permanent) showDelBackupProgress();
    let res = null;
    try {
      res = await state.api.delete_link_backup(rec.link, permanent);
    } finally {
      hideDelBackupProgress();
    }
    if (res && res.error) toast(res.error, true);
    else {
      toast(permanent ? t("linkBackupDeletedPermanent") : t("linkBackupDeleted"));
      refreshLinkList(true);
    }
    return;
  }
  if (act === "remigrate") {
    openRemigrateDialog(rec);
    return;
  }
  if (act === "deloldtarget") {
    // 与 [删除备份] 同一套确认写法：勾选彻底删除时正文与确定按钮换成不进回收站的说法
    const oldLeft = String(rec.old_target || rec.old_target_left || "");
    const ok = await showConfirmDialog({
      title: t("linkDeleteOldTargetTitle"),
      message: t("linkDeleteOldTargetConfirm", oldLeft),
      messageChecked: t("linkDeleteOldTargetPermanentConfirm", oldLeft),
      okText: t("deleteToRecycle"),
      okTextChecked: t("deletePermanent"),
      checkboxLabel: t("pendingPermanent"),
      danger: true,
    });
    if (!ok) return;
    const permanent = !!(ok && ok.checked);
    // 彻底删除旧迁移同样要逐项报数，复用删除备份那张进度卡（后端推 link-backup-progress）
    if (permanent) showDelBackupProgress();
    let res = null;
    try {
      res = await state.api.delete_old_target(rec.link, permanent);
    } finally {
      hideDelBackupProgress();
    }
    if (res && res.error) toast(res.error, true);
    else {
      toast(permanent ? t("linkOldTargetDeletedPermanent") : t("linkOldTargetDeleted"));
      refreshLinkList(true);
    }
    return;
  }
  if (act === "remove") {
    const ok = await showConfirmDialog({
      title: t("linkRemoveTitle"),
      message: t("linkRemoveConfirm", rec.link),
      okText: t("linkRemoveRecord"),
      danger: true,
    });
    if (!ok) return;
    const res = await state.api.remove_directory_link_record(rec.link);
    if (res && res.error) toast(res.error, true);
    else {
      toast(t("linkRecordRemoved"));
      refreshLinkList(true);
    }
  }
}

// ---- 新建迁移 ----

/** 输入变化后（防抖）向后端校验，在表单里提示能否迁移。 */
function refreshMigrateHint() {
  clearTimeout(_linkHintTimer);
  _linkHintTimer = setTimeout(async () => {
    const source = String(($("#migrateSourceInput") || {}).value || "").trim();
    const dest = String(($("#migrateDestInput") || {}).value || "").trim();
    const hint = $("#migrateHint");
    if (!hint) return;
    if (!source || !dest) {
      hint.textContent = "";
      hint.classList.add("hidden");
      return;
    }
    let res;
    try {
      res = await state.api.validate_directory_migration(source, dest);
    } catch (e) {
      return;
    }
    hint.classList.remove("hidden");
    let msg = (res && res.message) || "";
    if (res && res.ok && typeof res.dest_free === "number" && res.dest_free > 0) {
      msg = t("migrateDestFree") + " " + fmtBytes(res.dest_free);
    }
    hint.textContent = msg;
  }, 350);
}

// ---- 导入迁移：把别处建好的目录链接收进管理 ----

/** 导入迁移弹窗的提交状态。 */
const _linkImport = { busy: false };

function openLinkImportDialog() {
  _linkImport.busy = false;
  const linkEl = $("#linkImportPath");
  const targetEl = $("#linkImportTarget");
  if (linkEl) {
    linkEl.value = "";
    linkEl.classList.remove("is-error");
  }
  if (targetEl) {
    targetEl.value = "";
    targetEl.classList.remove("is-error");
  }
  updateLinkImportOk();
  const overlay = $("#linkImportOverlay");
  if (overlay) overlay.classList.remove("hidden");
  if (linkEl) linkEl.focus();
}

function closeLinkImportDialog() {
  const overlay = $("#linkImportOverlay");
  if (overlay) overlay.classList.add("hidden");
}

/** 两个框都填了才让点 [导入]。 */
function updateLinkImportOk() {
  const btn = $("#linkImportOkBtn");
  if (!btn) return;
  const linkEl = $("#linkImportPath");
  const targetEl = $("#linkImportTarget");
  const filled =
    !!String((linkEl && linkEl.value) || "").trim() &&
    !!String((targetEl && targetEl.value) || "").trim();
  btn.disabled = !filled || _linkImport.busy;
}

/** 选一个目录填进指定的框，which 取 "link" 或 "target"。 */
async function pickLinkImportPath(which) {
  if (!state.api) return;
  const res = await state.api.choose_folder();
  if (!res || !res.path) return;
  const input = $(which === "target" ? "#linkImportTarget" : "#linkImportPath");
  if (input) {
    input.value = res.path;
    input.classList.remove("is-error");
  }
  updateLinkImportOk();
}

/** 导入失败时错误码落在哪个框：link 是 [访问路径]，target 是 [存放位置]，both 表示两个框填反了。
    表里的码与 core/migrate_links.py 的 import_link 抛出的一致，其余链路（预检、批量导入）不走这里。 */
const _LINK_IMPORT_ERROR_FIELD = {
  import_missing: "link",
  import_not_junction: "link",
  import_target_missing: "link",
  import_target_is_link: "link",
  import_exists: "link",
  import_pair_mismatch: "target",
  import_reversed: "both",
};

/** 标出出问题的那个框，让用户直接看到要改哪一个。 */
function markLinkImportError(code) {
  const which = _LINK_IMPORT_ERROR_FIELD[String(code || "")] || "";
  const linkEl = $("#linkImportPath");
  const targetEl = $("#linkImportTarget");
  if (linkEl) linkEl.classList.toggle("is-error", which === "link" || which === "both");
  if (targetEl) targetEl.classList.toggle("is-error", which === "target" || which === "both");
}

/** 把两个框填的那对路径交给后端校验，通过就收进管理。 */
async function runLinkImport() {
  const linkEl = $("#linkImportPath");
  const targetEl = $("#linkImportTarget");
  const link = String((linkEl && linkEl.value) || "").trim();
  const target = String((targetEl && targetEl.value) || "").trim();
  if (!link || !target || _linkImport.busy) return;
  if (!state.api || !state.api.import_link) return;
  _linkImport.busy = true;
  updateLinkImportOk();
  let res = null;
  try {
    res = await state.api.import_link(link, target);
  } catch (e) {
    toast(String(e && e.message ? e.message : e), true);
    _linkImport.busy = false;
    updateLinkImportOk();
    return;
  }
  _linkImport.busy = false;
  updateLinkImportOk();
  if (!res || res.error) {
    markLinkImportError(res && res.code);
    toast((res && res.error) || t("linkImportFail"), true);
    return;
  }
  toast(t("linkImportOneDone"));
  closeLinkImportDialog();
  await refreshLinkList();
}

// ---- 再次迁移：把已迁移目录的存放位置从当前磁盘搬到另一块盘 ----

/** 打开再次迁移弹窗，rec 是「已迁移」列表里的一条记录。 */
function openRemigrateDialog(rec) {
  const overlay = $("#remigrateOverlay");
  if (!overlay || !rec) return;
  // 上一次的校验防抖计时器可能还挂着，开新记录前先清掉
  clearTimeout(_remigrateHintTimer);
  _remigrate.rec = rec;
  _remigrate.oldAction = "keep";
  const intro = $("#remigrateIntro");
  if (intro) intro.textContent = t("migrateAgainIntro", rec.link, rec.target);
  const di = $("#remigrateDestInput");
  if (di) di.value = "";
  document.querySelectorAll('input[name="remigrateOldAction"]').forEach((el) => {
    el.checked = el.value === "keep";
  });
  const hint = $("#remigrateHint");
  if (hint) {
    hint.textContent = "";
    hint.classList.add("hidden");
  }
  overlay.classList.remove("hidden");
  if (di) di.focus();
}

function closeRemigrateDialog() {
  clearTimeout(_remigrateHintTimer);
  _remigrate.rec = null;
  const overlay = $("#remigrateOverlay");
  if (overlay) overlay.classList.add("hidden");
}

/** 输入变化后（防抖）向后端校验，在弹窗里提示能否迁移与存放盘剩余空间。 */
function refreshRemigrateHint() {
  clearTimeout(_remigrateHintTimer);
  _remigrateHintTimer = setTimeout(async () => {
    const rec = _remigrate.rec;
    const hint = $("#remigrateHint");
    if (!rec || !hint) return;
    const dest = String(($("#remigrateDestInput") || {}).value || "").trim();
    if (!dest) {
      hint.textContent = "";
      hint.classList.add("hidden");
      return;
    }
    let res;
    try {
      res = await state.api.validate_directory_remigration(rec.link, dest);
    } catch (e) {
      return;
    }
    hint.classList.remove("hidden");
    let msg = (res && res.message) || "";
    if (res && res.ok && typeof res.dest_free === "number" && res.dest_free > 0) {
      msg = t("migrateDestFree") + " " + fmtBytes(res.dest_free);
    }
    hint.textContent = msg;
  }, 350);
}

/** 开始再次迁移：确认后压入迁移队列并立即开跑，进度与完成事件走队列那一套。 */
async function startRemigrate() {
  const rec = _remigrate.rec;
  if (!rec || !state.api) return;
  const dest = String(($("#remigrateDestInput") || {}).value || "").trim();
  if (!dest) {
    toast(t("migrateNeedDest"), true);
    return;
  }
  let res;
  try {
    res = await state.api.validate_directory_remigration(rec.link, dest);
  } catch (e) {
    toast(t("migrateFailed", e), true);
    return;
  }
  if (!res || !res.ok) {
    toast((res && res.message) || t("migrateFailedDefault"), true);
    return;
  }
  const action = _remigrate.oldAction || "keep";
  const actionKey =
    action === "recycle" ? "migrateOldRecycle" :
    action === "purge" ? "migrateOldPurge" : "migrateOldKeep";
  const ok = await showConfirmDialog({
    title: t("migrateAgainTitle"),
    message: t("migrateAgainConfirm", String(res.link || rec.link), String(res.dest || dest), t(actionKey)),
    okText: t("migrateAgainStart"),
    danger: true,
  });
  if (!ok) return;
  if (!(await closeMigrateEdit(true))) return;
  closeRemigrateDialog();
  // 队列串行执行，同一时间只允许一个目录迁移在跑
  if (_migrateQueue.running || _migrateQueue.activeId) {
    toast(t("migrateAgainBusy"), true);
    return;
  }
  _migrateQueue.items.push({
    id: ++_migrateQueue.seq,
    source: String(res.link || rec.link),
    dest: String(res.dest || dest),
    backupAction: "keep",
    remigrate: true,
    oldAction: action,
    status: "pending",
    error: "",
    progress: null,
    skipped: false,
    cancelling: false,
    flashUntil: 0,
    leaving: false,
    leaveStart: 0,
  });
  _migrateQueue.running = true;
  _migrateQueue.paused = false;
  setLinkView("queue");
  toast(t("queueAdded", _migrateQueue.items.filter((it) => it.status === "pending").length));
  renderMigrateQueue();
  tickMigrateQueue();
}

// ---- 迁移队列 ----

/** 队列项的终态文案。运行中还可能叠加「正在取消」。 */
const QUEUE_STATUS_META = {
  pending: "queueStatusPending",
  editing: "queueStatusEditing",
  running: "queueStatusRunning",
  done: "queueStatusDone",
  failed: "queueStatusFailed",
  cancelled: "queueStatusCancelled",
};

function queueItem(id) {
  return _migrateQueue.items.find((it) => it.id === id) || null;
}

function queueActiveItem() {
  return queueItem(_migrateQueue.activeId);
}

/** 还没结束的项：等待执行加正在执行，用于页签计数。 */
function queueOpenItems() {
  return _migrateQueue.items.filter(
    (it) => it.status !== "done" && it.status !== "failed" && it.status !== "cancelled"
  );
}

/** 改写按钮文案并同步 data-i18n，切语言时跟着变。 */
function setI18nLabel(el, key) {
  if (!el || el.dataset.i18n === key) return;
  el.dataset.i18n = key;
  el.textContent = t(key);
}

function queueActBtn(variant, act, id, label) {
  const cls = variant ? "link-btn " + variant : "link-btn";
  return `<button type="button" class="${cls}" data-qa="${act}" data-qid="${id}">` +
    `<span class="link-btn-text">${escapeHtml(label)}</span></button>`;
}

/** 队列项的路径行：左标签列 + 右路径，与迁移历史同一套排版。 */
function queuePathRow(labelKey, path) {
  return `<div class="link-queue-row">` +
    `<span class="link-queue-label">${escapeHtml(t(labelKey))}</span>` +
    `<span class="link-queue-path" title="${escapeHtml(path)}">${escapeHtml(path)}</span>` +
    `</div>`;
}

/** 队列项上的备份处理行：默认的[保留]不占位置，另外两项写清备份去向。 */
function queueBackupRowHtml(it) {
  const action = String((it && it.backupAction) || "keep");
  if (action !== "recycle" && action !== "purge") return "";
  const key = action === "purge" ? "migrateBackupPurge" : "migrateBackupRecycle";
  const danger = action === "purge" ? " is-danger" : "";
  const text = t(key);
  return `<div class="link-queue-row">` +
    `<span class="link-queue-label">${escapeHtml(t("migrateBackupLabel"))}</span>` +
    `<span class="link-queue-value${danger}" title="${escapeHtml(text)}">${escapeHtml(text)}</span>` +
    `</div>`;
}

function queueItemHtml(it, idx) {
  const statusKey = it.cancelling
    ? "queueStatusCancelling"
    : (QUEUE_STATUS_META[it.status] || QUEUE_STATUS_META.pending);
  const acts = [];
  if (it.status === "pending") acts.push(queueActBtn("", "edit", it.id, t("queueEdit")));
  // 只有失败的项能重试：按入队时记下的设置排到队尾再跑一次
  if (it.status === "failed") acts.push(queueActBtn("", "retry", it.id, t("queueRetry")));
  // 正在迁移的项不能改也不能抽走，等它跑完
  if (it.status !== "running") acts.push(queueActBtn("is-danger", "remove", it.id, t("queueRemove")));
  const progress = it.status === "running" && it.progress ? migrateProgressView(it.progress) : null;
  // 绿底闪烁只在刚完成的一小段时间里挂上，之后其他原因引起的重渲染不再重播
  const flash = it.flashUntil && it.flashUntil > Date.now() ? " is-flash" : "";
  // 离场状态记在数据上，并用负延迟接着上次的进度播：滑出途中重排整个列表也不会退回动画开头
  const leaving = it.leaving ? " is-leaving" : "";
  // 下界取零是为了时钟被往回调时仍写出合法的时间值
  const leaveElapsed = Math.max(0, Math.min(Date.now() - (it.leaveStart || 0), QUEUE_LEAVE_MS));
  const leaveDelay = it.leaving ? ` style="animation-delay:-${leaveElapsed}ms"` : "";
  return `<div class="link-queue-item is-${it.status}${flash}${leaving}"${leaveDelay} data-qid="${it.id}">` +
    `<div class="link-queue-head">` +
      `<span class="link-queue-index">${idx + 1}</span>` +
      `<span class="link-queue-dot"></span>` +
      `<span class="link-queue-status">${escapeHtml(t(statusKey))}</span>` +
      (acts.length ? `<span class="link-queue-acts">${acts.join("")}</span>` : "") +
    `</div>` +
    queuePathRow("linkSource", String(it.source || "")) +
    queuePathRow("linkTarget", String(it.dest || "")) +
    queueBackupRowHtml(it) +
    (progress
      ? `<div class="migrate-progress link-queue-progress">` +
        `<div class="migrate-progress-bar"><i class="link-queue-fill" style="width:${progress.pct}%"></i></div>` +
        `<div class="migrate-progress-text">${escapeHtml(progress.label)}</div>` +
        `<div class="migrate-progress-note">${progressNoteHtml(progress.note)}</div>` +
        `</div>`
      : "") +
    (it.status === "failed" && it.error
      ? `<div class="link-queue-error" title="${escapeHtml(it.error)}">${escapeHtml(it.error)}</div>`
      : "") +
    `</div>`;
}

function renderMigrateQueue() {
  const list = $("#linkQueueList");
  if (!list) return;
  const empty = $("#linkQueueEmpty");
  const count = $("#linkQueueCount");
  if (count) {
    const n = queueOpenItems().length;
    count.textContent = n ? `(${n})` : "";
  }
  if (!_migrateQueue.items.length) {
    list.innerHTML = "";
    list.classList.add("hidden");
    if (empty) empty.classList.remove("hidden");
  } else {
    list.innerHTML = _migrateQueue.items.map(queueItemHtml).join("");
    list.classList.remove("hidden");
    if (empty) empty.classList.add("hidden");
  }
  syncQueueButtons();
  syncQueueBackBtn();
}

/** 队列底部按钮：按运行/暂停换文案，没在跑时「取消」就是「清空」。 */
function syncQueueButtons() {
  const startBtn = $("#queueStartBtn");
  const pauseBtn = $("#queuePauseBtn");
  const cancelBtn = $("#queueCancelBtn");
  const hasPending = _migrateQueue.items.some((it) => it.status === "pending");
  if (startBtn) {
    // 运行中藏起「开始」，暂停后它就是「继续」
    startBtn.classList.toggle("hidden", _migrateQueue.running && !_migrateQueue.paused);
    startBtn.disabled = !hasPending;
    setI18nLabel(startBtn, _migrateQueue.paused ? "queueResume" : "queueStart");
  }
  if (pauseBtn) pauseBtn.classList.toggle("hidden", !_migrateQueue.running || _migrateQueue.paused);
  if (cancelBtn) {
    cancelBtn.classList.toggle("hidden", !_migrateQueue.items.length);
    const busy = _migrateQueue.running || !!_migrateQueue.activeId;
    setI18nLabel(cancelBtn, busy ? "cancel" : "queueClear");
  }
}

/** 改写标题提示文案并同步 data-i18n-title，切语言时跟着变。 */
function setI18nTitle(el, key) {
  if (!el || el.dataset.i18nTitle === key) return;
  el.dataset.i18nTitle = key;
  el.title = t(key);
}

/**
 * 迁移表单左下角的[返回队列]：队列里有任务、且这次不是从队列进来的才出现。
 * 从队列进来时[取消]已经回队列；队列空时也没有回队列这条出口。
 */
function syncQueueBackBtn() {
  const btn = $("#migrateBackQueueBtn");
  if (!btn) return;
  const fromQueue = _migrateFormBackView === "queue";
  btn.classList.toggle("hidden", !_migrateQueue.items.length || fromQueue);
}

/** 推进队列：没有活动任务时启动下一个待迁移项，一项跑完接着下一项。 */
function tickMigrateQueue() {
  if (!_migrateQueue.running || _migrateQueue.paused || _migrateQueue.activeId) return;
  const next = nextQueueItem();
  if (!next) {
    // 没有能跑的任务（都在编辑中或已被移除），队列停下来等下次「开始」
    _migrateQueue.running = false;
    renderMigrateQueue();
    return;
  }
  // 让出一轮再启动：后端的占位在推送完成事件之前就放开了，这里只是让完成项的
  // 界面状态先落定，免得它与下一项的渲染叠在同一个调用栈里。
  setTimeout(() => {
    // 这一跳之间可能被暂停、取消或移出队列，启动前再确认一次
    if (!_migrateQueue.running || _migrateQueue.paused || _migrateQueue.activeId) return;
    if (next.status !== "pending" || !_migrateQueue.items.includes(next)) return;
    startQueueItem(next);
  }, 0);
}

/** 取下一个该跑的任务：正在编辑的项留着，等编辑结束后排到队尾。 */
function nextQueueItem() {
  for (const it of _migrateQueue.items) {
    if (it.status === "editing") {
      it.skipped = true;
      continue;
    }
    if (it.status === "pending") return it;
  }
  return null;
}

async function startQueueItem(it) {
  it.status = "running";
  it.cancelling = false;
  it.error = "";
  it.progress = { stage: "copy", done: 0, total: 0 };
  // 换任务了，把上一项留下的速度样本清掉
  migrateRateClear();
  _migrateQueue.activeId = it.id;
  _migrateQueue.verifyToasting = false;
  _migrateQueue.verifyToasted = false;
  renderMigrateQueue();
  let res;
  try {
    res = it.remigrate
      ? await state.api.start_directory_remigration(it.source, it.dest, it.oldAction || "keep")
      : await state.api.start_directory_migration(it.source, it.dest, it.backupAction || "keep");
  } catch (e) {
    finishQueueItem(it, { ok: false, error: t("migrateFailed", e) });
    return;
  }
  // 预检不过（空间不够、已有迁移在跑）：这一项判失败，队列继续往下走
  if (res && res.error) {
    finishQueueItem(it, { ok: false, error: res.error, code: res.code });
    return;
  }
  renderMigrateQueue();
}

/** 迁移结果里重建、跳过链接的条数说明；没有就返回空串。 */
function migrateLinkNote(payload) {
  const p = payload || {};
  return t("migrateLinkNote", Number(p.links) || 0, Number(p.skipped) || 0);
}

/** 已迁移的项在队列里停留的时间：够看清刚跑完的结果，又不让队列越堆越长。 */
const QUEUE_DONE_LINGER_MS = 3000;
/** 完成时绿底闪烁的时长：三次闪烁合计约 1.2 秒，须与 .link-queue-item.is-done.is-flash 的动画时长一致。 */
const QUEUE_DONE_FLASH_MS = 1200;
/** 完成项向左滑出的时长，与 .link-queue-item.is-done.is-leaving 的动画时长一致。 */
const QUEUE_LEAVE_MS = 340;

/**
 * 迁移成功后把这一项从队列里撤掉：先停留一会儿让结果留在眼前，再让这一行
 * 向左滑出。到时若已被手动移除或随队列一起清空，就不再动作。
 */
function scheduleQueueItemRemoval(it) {
  it.removeTimer = setTimeout(() => {
    it.removeTimer = null;
    // 停留期内这一项可能被改回等待中（编辑器里重新保存并排队），此时不撤
    if (it.status !== "done" || !_migrateQueue.items.includes(it)) return;
    // 闪烁已经播完，换成离场态重排一次：滑出期间的任何重渲染都从同一进度接着播
    it.flashUntil = 0;
    it.leaving = true;
    it.leaveStart = Date.now();
    renderMigrateQueue();
    setTimeout(() => {
      if (it.status !== "done" || !_migrateQueue.items.includes(it)) return;
      dropQueueItem(it);
    }, QUEUE_LEAVE_MS);
  }, QUEUE_DONE_LINGER_MS);
}

/** 把一项从队列数据里去掉并重排列表。 */
function dropQueueItem(it) {
  _migrateQueue.items = _migrateQueue.items.filter((x) => x !== it);
  renderMigrateQueue();
}

/**
 * 完成提示里备份去向的那一句。按后端回报的实际动作取词：动作仍是入队时
 * 选的那一项说明删除已经执行，退回[保留]说明收尾没静下来、没有删；动作
 * 没变但备份没清掉说明删除失败。选中[保留]且备份确实保留着时不加这句。
 */
function migrateBackupTail(payload, it) {
  const p = payload || {};
  const chosen = String((it && it.backupAction) || "keep");
  const action = String(p.backup_action || chosen);
  if (chosen === "keep" && action === "keep") return "";
  if (action === "keep") return t("migrateBackupKeepFallback");
  if (!p.backup_cleaned) return t("migrateBackupCleanFail");
  return action === "purge" ? t("migrateBackupPurged") : t("migrateBackupCleaned");
}

/** 收尾一项：落终态并接着跑下一项；payload 是完成事件负载或启动失败的信息。 */
function finishQueueItem(it, payload) {
  if (it && _migrateQueue.activeId === it.id) _migrateQueue.activeId = null;
  if (it) {
    const ok = !!(payload && payload.ok);
    it.progress = null;
    it.cancelling = false;
    if (ok) {
      it.status = "done";
      it.error = "";
      it.flashUntil = Date.now() + QUEUE_DONE_FLASH_MS;
      const tail = migrateBackupTail(payload, it);
      const notes = [tail, migrateLinkNote(payload)].filter(Boolean);
      toast(t("queueTaskDone", it.source, notes.join(" · ")));
      // 结果已经落到右下角提示与迁移历史里，队列里的这一项留一会儿就撤掉
      scheduleQueueItemRemoval(it);
    } else {
      it.status = payload && payload.code === "cancelled" ? "cancelled" : "failed";
      it.error = (payload && payload.error) || t("migrateFailedDefault");
      if (it.status === "cancelled") toast(t("queueTaskCancelled", it.source));
      else toast(it.error, true);
    }
  }
  renderMigrateQueue();
  // 每项都会写迁移历史；停在历史视图就重渲染，否则只更新计数
  const histView = $("#linkHistoryView");
  if (histView && !histView.classList.contains("hidden")) refreshMigrationHistory();
  else refreshMigrationHistoryCount();
  // 迁移会新建目录链接，停在列表视图时让卡片跟着更新
  const listView = $("#linkListView");
  if (listView && !listView.classList.contains("hidden")) refreshLinkList();
  tickMigrateQueue();
}

/** 进度事件逐文件推来，只改这一项的进度节点，不重排整个列表。 */
function updateQueueProgressDom(it) {
  const row = document.querySelector(`#linkQueueList [data-qid="${it.id}"]`);
  if (!row) return;
  const view = migrateProgressView(it.progress);
  const fill = row.querySelector(".link-queue-fill");
  const text = row.querySelector(".migrate-progress-text");
  const note = row.querySelector(".migrate-progress-note");
  if (fill) fill.style.width = view.pct + "%";
  if (text) text.textContent = view.label;
  if (note) note.innerHTML = progressNoteHtml(view.note);
}

// ---- 迁移表单：加入队列 / 编辑队列项 ----

/** 备份处理选项组的当前取值，认不出的值一律当[保留]。 */
function migrateBackupAction() {
  const el = document.querySelector('input[name="migrateBackupAction"]:checked');
  const v = el ? String(el.value) : "keep";
  return v === "recycle" || v === "purge" ? v : "keep";
}

/** 备份处理选项组跳到指定项，认不出的值落在[保留]。 */
function setMigrateBackupAction(action) {
  const want = action === "recycle" || action === "purge" ? action : "keep";
  document.querySelectorAll('input[name="migrateBackupAction"]').forEach((el) => {
    el.checked = el.value === want;
  });
}

/** 表单当前填的值。 */
function migrateFormValues() {
  return {
    source: String(($("#migrateSourceInput") || {}).value || "").trim(),
    dest: String(($("#migrateDestInput") || {}).value || "").trim(),
    backupAction: migrateBackupAction(),
  };
}

/** 表单内容是否偏离了编辑对象；新建模式下的草稿也算改动。 */
function migrateFormDirty(item) {
  const v = migrateFormValues();
  if (!item) return !!(v.source || v.dest);
  return (
    v.source !== String(item.source || "") ||
    v.dest !== String(item.dest || "") ||
    v.backupAction !== String(item.backupAction || "keep")
  );
}

/** 表单按钮按「新建 / 编辑」两种模式切换：编辑时不显示加入队列，改显示保存修改。 */
function syncMigrateFormChrome() {
  const editing = !!queueItem(_migrateQueue.editingId);
  const title = $("#migrateFormTitle");
  if (title) setI18nLabel(title, editing ? "queueEditTitle" : "linkNew");
  // 右上角 ✕ 的悬停提示说明出口去向：从队列进来的表单提示返回队列，从列表进来的提示返回迁移列表
  setI18nTitle($("#migrateFormCloseBtn"), _migrateFormBackView === "queue" ? "queueBack" : "linkBackList");
  const enqueueBtn = $("#migrateEnqueueBtn");
  if (enqueueBtn) enqueueBtn.classList.toggle("hidden", editing);
  const saveBtn = $("#migrateSaveBtn");
  if (saveBtn) saveBtn.classList.toggle("hidden", !editing);
  syncQueueBackBtn();
}

/** 结束编辑会话：编辑中的任务回到等待队列，被队列越过过的排到队尾。 */
function endMigrateEdit() {
  const it = queueItem(_migrateQueue.editingId);
  _migrateQueue.editingId = null;
  if (it) {
    it.status = "pending";
    if (it.skipped) {
      it.skipped = false;
      _migrateQueue.items = _migrateQueue.items.filter((x) => x !== it);
      _migrateQueue.items.push(it);
    }
  }
  syncMigrateFormChrome();
  renderMigrateQueue();
  tickMigrateQueue();
}

/**
 * 关掉编辑会话；confirm 为真且表单有未保存改动时先问一次。
 * 返回是否真的关掉了（取消确认时为 false，调用方应放弃接下来的动作）。
 */
async function closeMigrateEdit(confirm) {
  const it = queueItem(_migrateQueue.editingId);
  if (!it) return true;
  if (confirm && migrateFormDirty(it)) {
    const ok = await showConfirmDialog({
      title: t("queueDiscardEditTitle"),
      message: t("queueDiscardEditConfirm", it.source),
      okText: t("queueDiscardEditOk"),
      danger: true,
    });
    if (!ok) return false;
  }
  endMigrateEdit();
  return true;
}

/** 编辑队列里的一项：填进表单，队列轮到它时先绕过。 */
async function openMigrateEdit(id) {
  const it = queueItem(id);
  if (!it || it.status === "running") return;
  if (_migrateQueue.editingId === it.id) {
    showLinkCreate();
    return;
  }
  if (!(await closeMigrateEdit(true))) return;
  it.status = "editing";
  _migrateQueue.editingId = it.id;
  showMigrateFormView();
  const si = $("#migrateSourceInput");
  const di = $("#migrateDestInput");
  if (si) si.value = String(it.source || "");
  if (di) di.value = String(it.dest || "");
  setMigrateBackupAction(it.backupAction);
  syncMigrateFormChrome();
  refreshMigrateHint();
  renderMigrateQueue();
}

/** 「加入队列」：校验通过后压入队尾，清空表单以便接着加下一个。 */
async function enqueueMigrateItem() {
  const v = migrateFormValues();
  if (!v.source || !v.dest) {
    toast(t("migrateNeedBoth"), true);
    return;
  }
  let res;
  try {
    res = await state.api.validate_directory_migration(v.source, v.dest);
  } catch (e) {
    toast(t("migrateFailed", e), true);
    return;
  }
  if (!res || !res.ok) {
    toast((res && res.message) || t("migrateFailedDefault"), true);
    return;
  }
  _migrateQueue.items.push({
    id: ++_migrateQueue.seq,
    source: String(res.source || v.source),
    dest: String(res.dest || v.dest),
    backupAction: v.backupAction,
    status: "pending",
    error: "",
    progress: null,
    skipped: false,
    cancelling: false,
    flashUntil: 0,
    leaving: false,
    leaveStart: 0,
  });
  // 入队后清空输入，免得同一条路径被顺手再加一次
  const si = $("#migrateSourceInput");
  const di = $("#migrateDestInput");
  if (si) si.value = "";
  if (di) di.value = "";
  setMigrateBackupAction("keep");
  refreshMigrateHint();
  renderMigrateQueue();
  toast(t("queueAdded", _migrateQueue.items.filter((it) => it.status === "pending").length));
}

/** 「保存修改」：重新校验后写回队列项，结束编辑会话并回到队列。 */
async function saveMigrateEdit() {
  const it = queueItem(_migrateQueue.editingId);
  if (!it) return;
  const v = migrateFormValues();
  if (!v.source || !v.dest) {
    toast(t("migrateNeedBoth"), true);
    return;
  }
  let res;
  try {
    res = await state.api.validate_directory_migration(v.source, v.dest);
  } catch (e) {
    toast(t("migrateFailed", e), true);
    return;
  }
  if (!res || !res.ok) {
    toast((res && res.message) || t("migrateFailedDefault"), true);
    return;
  }
  it.source = String(res.source || v.source);
  it.dest = String(res.dest || v.dest);
  it.backupAction = v.backupAction;
  hideLinkCreate();
  toast(t("queueEditSaved"));
}

/** 从队列移除一项；正在编辑的项被移除时同时结束编辑会话。 */
function removeQueueItem(id) {
  const it = queueItem(id);
  if (!it || it.status === "running") return;
  if (_migrateQueue.editingId === id) endMigrateEdit();
  _migrateQueue.items = _migrateQueue.items.filter((x) => x.id !== id);
  renderMigrateQueue();
}

/**
 * 失败项的[重试]：按入队时记下的设置回到待迁移，并排到队尾重新排队。
 * 队列正在运行时，当前项收尾后按顺序轮到它；队列处于暂停时保持暂停，
 * 等用户按下继续；队列已经停下时直接接着跑，不必再点一次开始。
 */
function retryQueueItem(id) {
  const it = queueItem(id);
  if (!it || it.status !== "failed") return;
  _migrateQueue.items = _migrateQueue.items.filter((x) => x.id !== id);
  _migrateQueue.items.push(it);
  it.status = "pending";
  it.error = "";
  it.progress = null;
  it.cancelling = false;
  it.flashUntil = 0;
  it.leaving = false;
  it.leaveStart = 0;
  it.skipped = false;
  renderMigrateQueue();
  if (_migrateQueue.running) return;
  _migrateQueue.running = true;
  _migrateQueue.paused = false;
  tickMigrateQueue();
}

/** 开始 / 继续队列；首次开始前问一次（真实文件操作，不改失败）。 */
async function startMigrateQueue() {
  if (_migrateQueue.running && !_migrateQueue.paused) return;
  // 暂停后「开始」就是继续，不再问一次
  if (_migrateQueue.paused) {
    _migrateQueue.paused = false;
    _migrateQueue.running = true;
    renderMigrateQueue();
    tickMigrateQueue();
    return;
  }
  const waiting = _migrateQueue.items.filter((it) => it.status === "pending");
  if (!waiting.length) {
    toast(t("queueNothingToRun"), true);
    return;
  }
  const ok = await showConfirmDialog({
    title: t("queueStartTitle"),
    message: t("queueStartConfirm", waiting.length),
    okText: t("queueStart"),
    danger: true,
  });
  if (!ok) return;
  _migrateQueue.running = true;
  _migrateQueue.paused = false;
  renderMigrateQueue();
  tickMigrateQueue();
}

/** 暂停只作用于任务之间：当前这一项跑完才停，后端没有中途暂停。 */
function pauseMigrateQueue() {
  if (!_migrateQueue.running || _migrateQueue.paused) return;
  _migrateQueue.paused = true;
  renderMigrateQueue();
  toast(t("queuePaused"));
}

/** 队列没在跑时这个按钮就是清空；运行中则是停掉队列并在阶段边界中止当前项。 */
async function cancelMigrateQueue() {
  if (!_migrateQueue.items.length) return;
  const active = queueActiveItem();
  if (!active && !_migrateQueue.running) {
    const ok = await showConfirmDialog({
      title: t("queueClearTitle"),
      message: t("queueClearConfirm", _migrateQueue.items.length),
      okText: t("queueClear"),
      danger: true,
    });
    if (!ok) return;
    if (_migrateQueue.editingId) endMigrateEdit();
    _migrateQueue.items = [];
    renderMigrateQueue();
    toast(t("queueCleared"));
    return;
  }
  const res = await showConfirmDialog({
    title: t("queueCancelTitle"),
    message: active ? t("queueCancelConfirm", active.source) : t("queueCancelPending"),
    okText: t("queueCancelOk"),
    checkboxLabel: t("queueCancelClear"),
    okTextChecked: t("queueCancelClearOk"),
    danger: true,
  });
  if (!res) return;
  // 取消在阶段边界生效：先停自动继续，再让后端停当前这一项，界面先显示「正在取消」
  _migrateQueue.running = false;
  _migrateQueue.paused = false;
  if (active) {
    active.cancelling = true;
    try {
      // 再次迁移的后台线程只认自己的取消位，按队列项类型选对应接口
      if (active.remigrate) await state.api.cancel_directory_remigration();
      else await state.api.cancel_directory_migration();
    } catch (e) {}
  }
  // 正在取消的那一项留着，跑完才落终态；现在就抽掉会让它的完成事件没人认领
  if (res.checked) {
    // 清空时连编辑会话一起结束，免得表单留在一个已经不存在的任务上
    if (_migrateQueue.editingId) endMigrateEdit();
    _migrateQueue.items = _migrateQueue.items.filter((it) => it.status === "running");
  }
  renderMigrateQueue();
}

/** 后端阶段名到文案键的对应表；新增阶段时在这里补一行，避免界面显示成键名。 */
const MIGRATE_STAGE_KEYS = {
  copy: "migrateStageCopy",
  verify: "migrateStageVerify",
  setaside: "migrateStageSetaside",
  junction: "migrateStageJunction",
  restore: "migrateStageRestore",
  refresh: "migrateStageRefresh",
  switch: "migrateStageSwitch",
  settle: "migrateStageSettle",
};

/** 阶段文案：表中有这个阶段就用对应文案，没有则原样显示后端给的阶段名。 */
function migrateStageText(stage) {
  const key = MIGRATE_STAGE_KEYS[stage];
  return typeof key === "string" ? t(key) : String(stage || "");
}

/** 把迁移进度负载算成进度条百分比与文案。 */
// ---- 进度速度 -----------------------------------------------------------------
// 上报里的累计字节一路上升，按最近几秒的增量算实时写入速度。单个大文件复制
// 期间靠块级上报把字节推上来，速度才跟着有得算。
const MIGRATE_RATE_WINDOW_MS = 5000;
let _rateSamples = [];

/** 记一次进度样本；累计字节回退说明换了一个任务，旧样本作废重新起算。 */
function migrateRatePush(bytes) {
  const now = Date.now();
  const last = _rateSamples[_rateSamples.length - 1];
  if (last && bytes < last[1]) _rateSamples = [];
  _rateSamples.push([now, bytes]);
  while (_rateSamples.length > 2 && now - _rateSamples[0][0] > MIGRATE_RATE_WINDOW_MS) {
    _rateSamples.shift();
  }
}

/** 清空速度样本。换任务时调用，免得新任务头一条事件到达前显示上一项的速度。 */
function migrateRateClear() {
  _rateSamples = [];
}

/** 最近窗口内的平均写入速度，单位字节每秒；样本不足时返回 0。 */
function migrateRateNow() {
  if (_rateSamples.length < 2) return 0;
  const [t0, b0] = _rateSamples[0];
  const [t1, b1] = _rateSamples[_rateSamples.length - 1];
  const dt = (t1 - t0) / 1000;
  if (dt < 0.3 || b1 <= b0) return 0;
  return (b1 - b0) / dt;
}

/** 秒数写成易读时长：不足一分钟只说秒，超过说分秒，再超过说时分。 */
function fmtEta(sec) {
  const s = Math.max(1, Math.round(sec));
  if (s < 60) return t("durSec", s);
  const m = Math.floor(s / 60);
  if (m < 60) return t("durMinSec", m, s % 60);
  return t("durHourMin", Math.floor(m / 60), m % 60);
}

/** 进度里带的是相对路径，只留文件名，免得路径把这一行占满。 */
function shortName(path) {
  const s = String(path || "");
  const i = Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/"));
  return i >= 0 ? s.slice(i + 1) : s;
}

/** 进度下面那一行：实时写入速度、剩余时间、正在复制的文件名。 */
function migrateRateNote(bDone, bTotal, current) {
  const parts = [];
  const speed = migrateRateNow();
  if (speed > 0) {
    parts.push(`${fmtBytes(speed)}/s`);
    const rest = bTotal - bDone;
    if (rest > 0) parts.push(t("migrateRateLeft", fmtEta(rest / speed)));
  }
  const name = shortName(current);
  if (name) parts.push(name);
  return parts;
}

/** 这一行分成几段显示，段与段之间靠间距分开，不放分隔符号。 */
function progressNoteHtml(parts) {
  return (parts || []).map((x) => `<span>${escapeHtml(x)}</span>`).join("");
}

function migrateProgressView(payload) {
  const p = payload || {};
  const stage = p.stage || "copy";
  const done = Number(p.done) || 0;
  const total = Number(p.total) || 0;
  const bDone = Number(p.bytes_done) || 0;
  const bTotal = Number(p.bytes_total) || 0;
  // 再次迁移多一个补差异的复制阶段，与普通复制一样按字节报百分比
  const copyKey = MIGRATE_STAGE_KEYS[stage];
  if (stage !== "copy" && stage !== "refresh") {
    return { pct: 100, label: migrateStageText(stage), note: [] };
  }
  // 优先按字节算（大小文件混合时百分比更平滑），无字节数时回落文件数
  const denom = bTotal > 0 ? bTotal : total;
  const cur = bTotal > 0 ? bDone : done;
  const pct = denom > 0 ? Math.min(100, Math.round((cur / denom) * 100)) : 0;
  let label = `${t(copyKey)} ${pct}%`;
  if (bTotal > 0) label += ` · ${fmtBytes(bDone)} / ${fmtBytes(bTotal)}`;
  return { pct, label, note: migrateRateNote(bDone, bTotal, p.current) };
}

// ---- 搬家途中后端提问（要用户关程序 / 确认切换）--------------------------

// 当前挂着的提问编号；答复或迁移结束后清掉，避免重复提交
let _migrateAskId = 0;

/** 关掉提问框。迁移自己结束（取消、失败）时也走这里，不让框挂在屏幕上。 */
function closeMigrateAsk() {
  _migrateAskId = 0;
  const ov = $("#migrateAskOverlay");
  if (ov) ov.classList.add("hidden");
}

/**
 * 往一个容器里刷一列元素；横向排一行塞不下时，把末尾多出来的收进 moreOf(n)
 * 那句话，化成「另有 N 个」。total 是这批元素的总数，比 items 多的部分
 * （后端只给了前几条）也算进 N。
 */
function _fillMigrateAskList(wrapId, listId, items, render, moreOf, total) {
  const wrap = $(wrapId);
  const list = $(listId);
  if (!wrap || !list) return;
  const empty = items.length === 0;
  // 先让整块显形再往里放：藏着的元素 clientWidth 是 0，量出来的行位置全是 0
  wrap.classList.toggle("hidden", empty);
  list.textContent = "";
  if (empty) return;
  const shown = items.map((item) => list.appendChild(render(item)));
  let rest = Math.max(0, Number(total ?? items.length) - items.length);
  if (moreOf) {
    const tail = document.createElement("div");
    tail.className = "migrate-ask-file muted";
    tail.textContent = moreOf(String(rest));
    list.appendChild(tail);
    // 用 rect 而不是 offsetTop：flex 子项的 offsetTop 在部分浏览器里对不上行
    const topOf = (node) => node.getBoundingClientRect().top;
    let keep = shown.length;
    const folded = () => {
      if (!shown.length) return false;
      const base = topOf(shown[0]);
      for (let i = 1; i < keep; i += 1) {
        if (topOf(shown[i]) - base > 1) return true;
      }
      return keep > 0 && topOf(tail) - base > 1;
    };
    // 从末尾一件件收，收到「另有 N 个」能落回第一行为止
    while (keep > 0 && folded()) {
      keep -= 1;
      shown[keep].remove();
      rest += 1;
      tail.textContent = moreOf(String(rest));
    }
    tail.classList.toggle("hidden", rest === 0);
  }
}

/** 后端在迁移途中问一句：弹框把用户的选择送回去，用户不点，后端就在那儿等着。 */
function onMigrateDirAsk(payload) {
  const ov = $("#migrateAskOverlay");
  const body = $("#migrateAskBody");
  const titleEl = $("#migrateAskTitle");
  const okBtn = $("#migrateAskOkBtn");
  const forceBtn = $("#migrateAskForceBtn");
  const cancelBtn = $("#migrateAskCancelBtn");
  if (!ov || !body) return;
  const p = payload || {};
  const askId = Number(p.askId) || 0;
  if (!askId) return;
  _migrateAskId = askId;

  const procs = (Array.isArray(p.procs) ? p.procs : []).filter(Boolean);
  const files = (Array.isArray(p.files) ? p.files : []).filter(Boolean);
  const filesTotal = Number(p.filesTotal) || files.length;
  const kind = String(p.kind || "");
  // 后端的文件清单给的是相对路径，拼上访问路径就是悬停提示里的全路径
  const root = String(p.root || "").replace(/[\\/]+$/, "");

  if (titleEl) {
    // 开工前那一问还没到切换那一步，标题不能叫切换前确认
    titleEl.textContent = t(
      kind === "nowatch" ? "migrateAskTitleNoWatch" : "migrateAskTitle"
    );
  }
  // 每个按钮点下去会发生什么，接着正文另起一行写，正文里不再重复
  const forceShown = kind === "switch" && Number(p.attempt) >= 2;
  const lines = [];
  if (kind === "nowatch") {
    // 开工前先问这一问：监测没起来，后面没人替用户盯着目录
    lines.push(t("migrateAskNoWatch"));
  } else if (kind === "locks") {
    lines.push(t("migrateAskLocks"));
  } else if (kind === "loop") {
    lines.push(
      t(
        "migrateAskLoop",
        String(Number(p.round) || 0),
        String(Number(p.rounds) || 0)
      )
    );
  } else {
    // 无监测时上一行已经说过改动不会被记录，这里不再重复后果
    lines.push(
      t(p.watchFailed ? "migrateAskSwitchNoWatch" : "migrateAskSwitch")
    );
  }
  if (okBtn) {
    // 没有变动监测就没得继续，只留[取消迁移]一个口
    okBtn.classList.toggle("hidden", kind === "nowatch");
    okBtn.textContent = t(
      kind === "loop" ? "migrateAskRetry" : "migrateAskContinue"
    );
  }
  if (kind === "loop") lines.push(t("migrateAskHintRetry"));
  if (forceShown) lines.push(t("migrateAskHintForce"));
  // 文案里的 [按钮名] 加粗，扫一眼能看到该按哪个
  body.innerHTML = lines
    .map((line) => String(line).replace(/\[[^\]]+\]/g, (m) => `<b>${m}</b>`))
    .join("\n");
  // 循环问到第二次才给「强制切换」：第一次先让用户关程序、等目录安静下来
  if (forceBtn) {
    forceBtn.classList.toggle("hidden", !forceShown);
  }

  // 先让弹窗显形：下面要量「最近变动的文件」排了几行，藏着的元素量不出高度
  ov.classList.remove("hidden");

  _fillMigrateAskList("#migrateAskProcs", "#migrateAskProcList", procs, (name) => {
    const chip = document.createElement("span");
    chip.className = "migrate-ask-chip";
    chip.textContent = name;
    return chip;
  });
  _fillMigrateAskList(
    "#migrateAskFiles",
    "#migrateAskFileList",
    files,
    (rel) => {
      const line = document.createElement("div");
      line.className = "migrate-ask-file";
      // 列表里只放文件名，全路径留给悬停提示，免得一列长路径把弹窗撑宽
      line.textContent = rel.split(/[\\/]/).pop() || rel;
      line.title = root ? `${root}\\${rel}` : rel;
      return line;
    },
    (n) => t("migrateAskMoreFiles", n),
    filesTotal
  );

  const answer = (action) => {
    const id = _migrateAskId;
    closeMigrateAsk();
    if (!id) return;
    Promise.resolve(state.api.answer_migrate_ask(id, action)).catch(() => {});
  };
  if (okBtn) okBtn.onclick = () => answer("continue");
  if (forceBtn) forceBtn.onclick = () => answer("force");
  if (cancelBtn) cancelBtn.onclick = () => answer("cancel");
}

function onMigrateDirProgress(payload) {
  // 事件可能在 start_directory_migration 返回之前就到了，靠 activeId 认领
  const it = queueActiveItem();
  if (!it) return;
  it.progress = payload || {};
  // 校验阶段提示：小目录复制瞬间结束，用户可能根本看不到进度条到 100%，
  // 所以 verify 进入时弹「正在校验」；通过（进入 setaside/junction/switch）再弹「校验通过」。
  const stage = payload && payload.stage;
  if (stage === "verify" && !_migrateQueue.verifyToasting) {
    _migrateQueue.verifyToasting = true;
    toast(t("migrateVerifyStarted"));
  }
  if (
    (stage === "setaside" || stage === "junction" || stage === "switch") &&
    _migrateQueue.verifyToasting &&
    !_migrateQueue.verifyToasted
  ) {
    _migrateQueue.verifyToasted = true;
    toast(t("migrateVerifyPassed"));
  }
  // 记一次字节样本，进度下面那行的实时速度按最近的增量算
  migrateRatePush(Number(payload && payload.bytes_done) || 0);
  updateQueueProgressDom(it);
}

function onMigrateDirDone(payload) {
  closeMigrateAsk();
  const it = queueActiveItem();
  const remigrate = !!(it && it.remigrate);
  finishQueueItem(it, payload);
  if (!remigrate) return;
  const left = payload && payload.ok ? String(payload.old_target_left || "") : "";
  // 成功时刷新「已迁移」列表与历史；旧迁移的路径由列表刷新带回来，不用再从事件里补写
  refreshLinkList(true);
  if (left) toast(t("migrateAgainOldLeft", left));
}

// ---- 目录还原（后台线程，进度经 migrate-dir-restore-* 事件推送）----

function showRestoreProgress() {
  const rp = $("#restoreProgress");
  if (rp) rp.classList.remove("hidden");
  const fill = $("#restoreProgressFill");
  if (fill) fill.style.width = "0%";
  const text = $("#restoreProgressText");
  if (text) text.textContent = t("migrateStageCopy") + " 0%";
}

function hideRestoreProgress() {
  const rp = $("#restoreProgress");
  if (rp) rp.classList.add("hidden");
}

// ---- 删除备份（彻底删除时后端逐项报数；回收站是系统一次调用，无数字可报）----

function showDelBackupProgress() {
  const box = $("#delBackupProgress");
  if (box) box.classList.remove("hidden");
  const text = $("#delBackupProgressText");
  if (text) text.textContent = t("linkDeleteBackupRunning");
}

function hideDelBackupProgress() {
  const box = $("#delBackupProgress");
  if (box) box.classList.add("hidden");
}

function onLinkBackupProgress(payload) {
  const items = Number((payload || {}).items) || 0;
  if (items <= 0) return;
  const text = $("#delBackupProgressText");
  if (text) text.textContent = t("pendingDeletedItems", items.toLocaleString());
}

function onRestoreDirProgress(payload) {
  const p = payload || {};
  const stage = p.stage || "copy";
  const done = Number(p.done) || 0;
  const total = Number(p.total) || 0;
  const bDone = Number(p.bytes_done) || 0;
  const bTotal = Number(p.bytes_total) || 0;
  const fill = $("#restoreProgressFill");
  const text = $("#restoreProgressText");
  if (stage === "copy") {
    const denom = bTotal > 0 ? bTotal : total;
    const cur = bTotal > 0 ? bDone : done;
    const pct = denom > 0 ? Math.min(100, Math.round((cur / denom) * 100)) : 0;
    if (fill) fill.style.width = pct + "%";
    let label = `${t("migrateStageCopy")} ${pct}%`;
    if (bTotal > 0) label += ` · ${fmtBytes(bDone)} / ${fmtBytes(bTotal)}`;
    if (p.current) label += " · " + p.current;
    if (text) text.textContent = label;
  } else {
    if (fill) fill.style.width = "100%";
    if (text) text.textContent = migrateStageText(stage);
  }
}

function onRestoreDirDone(payload) {
  hideRestoreProgress();
  _linkMgr.restoring = "";
  refreshLinkList(true);
  // 还原完成也会写历史；停在历史视图则重渲染，否则只刷新计数。
  const histView = $("#linkHistoryView");
  if (histView && !histView.classList.contains("hidden")) refreshMigrationHistory();
  else refreshMigrationHistoryCount();
  if (!payload || !payload.ok) {
    toast((payload && payload.error) || t("migrateFailedDefault"), true);
    return;
  }
  const note = migrateLinkNote(payload);
  toast(note ? `${t("linkRestored")} · ${note}` : t("linkRestored"));
}

// ---- 迁移历史 ----

const _HISTORY_STATUS_META = {
  success: { cls: "is-success", key: "linkStatusSuccess" },
  failed: { cls: "is-failed", key: "linkStatusFailed" },
  cancelled: { cls: "is-cancelled", key: "linkStatusCancelled" },
};

const _HISTORY_OP_META = {
  migrate: { key: "linkOpMigrate", cls: "op-migrate" },
  restore: { key: "linkOpRestore", cls: "op-restore" },
  delbackup: { key: "linkOpDelBackup", cls: "op-delbackup" },
  deloldtarget: { key: "linkOpDelOldTarget", cls: "op-delold" },
  linkfind: { key: "linkOpLinkFind", cls: "op-linkfind" },
};

/** 一条反查记录查的是什么：扫盘看盘符，用快照看快照名；两者都取不到时返回空串。 */
function linkFindSourceLabel(rec) {
  if (rec.mode === "snapshot") {
    const name = String(rec.snapshot || "").split(/[\\/]/).pop().replace(/\.(dbz|db)$/i, "");
    return name ? t("linkHistoryLinkFindSnapshot", name) : "";
  }
  const roots = (Array.isArray(rec.roots) ? rec.roots : [])
    .map((r) => String(r).trim())
    .filter(Boolean);
  return roots.length ? t("linkHistoryLinkFindScan", roots) : "";
}

/** 反查记录：首行交代查了什么来源，第二行是查的目录与查看当时结果的入口。 */
function buildLinkFindHistoryItem(rec) {
  const meta = _HISTORY_STATUS_META[rec.status] || _HISTORY_STATUS_META.failed;
  const source = String(rec.source || "");
  const error = String(rec.error || "");
  const hits = Array.isArray(rec.hits) ? rec.hits.length : 0;
  const unresolved = Array.isArray(rec.unresolved) ? rec.unresolved.length : 0;
  const total = Number(rec.hit_total) || hits;
  const unresolvedTotal = Number(rec.unresolved_total) || unresolved;

  // 结果在前、来源在后，整行不与时间戳争位，面板变窄时先省略来源
  const numBits = [t("linkFindFound", total)];
  const from = linkFindSourceLabel(rec);
  if (unresolvedTotal) numBits.push(t("linkFindPartial", unresolvedTotal));
  if (rec.elapsed_s) numBits.push(t("linkFindElapsed", rec.elapsed_s));
  if (from) numBits.push(from);

  const pathBits = [];
  if (source) {
    pathBits.push(
      `<span class="link-history-plabel">${escapeHtml(t("linkFindQueried"))}</span>` +
      `<button type="button" class="link-history-open link-history-path" data-open="${escapeHtml(source)}" title="${escapeHtml(source)}">${escapeHtml(source)}</button>`
    );
  }
  // 结果存在历史里就直接看，不再跑一遍查询
  if (total) {
    pathBits.push(
      `<span class="link-history-path-acts"><button type="button" class="link-history-open link-history-linkfind" data-hist-id="${escapeHtml(String(rec.id || ""))}">${escapeHtml(t("linkFindViewResult"))}</button></span>`
    );
  }
  const pathRow = pathBits.join("");

  return `<div class="link-history-item ${meta.cls} op-linkfind" title="${escapeHtml(error)}">` +
    `<div class="link-history-item-head">` +
      `<span class="link-history-op">${escapeHtml(t("linkOpLinkFind"))}</span>` +
      `<span class="link-history-status">${escapeHtml(t(meta.key))}</span>` +
      `<span class="link-history-time">${escapeHtml(formatHistoryTime(rec.ts))}</span>` +
    `</div>` +
    `<div class="link-history-note">${escapeHtml(numBits.join(" · "))}</div>` +
    (pathRow
      ? `<div class="link-history-item-paths"><div class="link-history-pathrow">${pathRow}</div></div>`
      : "") +
    (error ? `<div class="link-history-item-error" title="${escapeHtml(error)}">${escapeHtml(error)}</div>` : "") +
    `</div>`;
}

function formatHistoryTime(ts) {
  const n = Number(ts) || 0;
  if (!n) return "";
  const d = new Date(n * 1000);
  const pad = (v) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function buildHistoryItem(rec) {
  if (rec.op === "linkfind") return buildLinkFindHistoryItem(rec);
  const meta = _HISTORY_STATUS_META[rec.status] || _HISTORY_STATUS_META.failed;
  const opMeta = _HISTORY_OP_META[rec.op] || _HISTORY_OP_META.migrate;
  const files = Number(rec.files) || 0;
  const bytes = Number(rec.bytes) || 0;
  const numBits = [];
  if (files) numBits.push(t("linkHistoryFiles", files));
  if (bytes) numBits.push(fmtBytes(bytes));
  // 备份去向：彻底删除得写出来（回收站里找不到），进回收站沿用原提示；
  // 删除备份这条记录本身已表达「备份已删」，不再重复显示
  const backupNote = rec.backup_permanent
    ? t("linkBackupDeletedPermanent")
    : (rec.backup_cleaned && rec.op !== "delbackup" ? t("linkBackupDeleted") : "");
  // 彻底删除的旧迁移要写出来：回收站里找不到
  const oldTargetNote = rec.old_target_permanent ? t("linkOldTargetDeletedPermanent") : "";
  const numText = numBits.join(" · ");
  const linkNote = migrateLinkNote(rec);
  // 校验结论：说明这次依据什么确认访问路径与存放位置的内容一致；没有执行校验的条目没有这一行
  const verifyNote =
    rec.verify === "stats"
      ? t("linkHistoryVerifyStats")
      : (rec.verify === "full" ? t("linkHistoryVerifyFull") : "");

  const source = String(rec.source || "");
  const target = String(rec.target || "");
  const backup = String(rec.backup || "");
  const error = String(rec.error || "");

  // 历史里的路径可点击，在资源管理器中打开；路径可能已不存在，失败时 toast。
  const pathLink = (path, extraCls) =>
    `<button type="button" class="link-history-open ${extraCls || ""}" data-open="${escapeHtml(path)}" title="${escapeHtml(path)}">${escapeHtml(path)}</button>`;

  // 删除备份、删除旧迁移：只显示被删掉的那条路径；其余操作显示访问路径 → 存放位置。
  const isDelBackup = rec.op === "delbackup";
  const isDelOldTarget = rec.op === "deloldtarget";
  const pathRows =
    isDelBackup
      ? (backup
          ? `<div class="link-history-pathrow"><span class="link-history-plabel">${escapeHtml(t("linkBackup"))}</span>${pathLink(backup, "link-history-path")}</div>`
          : "")
      : isDelOldTarget
        ? (target
            ? `<div class="link-history-pathrow"><span class="link-history-plabel">${escapeHtml(t("linkOldTarget"))}</span>${pathLink(target, "link-history-target")}</div>`
            : "")
      : (source
          ? `<div class="link-history-pathrow"><span class="link-history-plabel">${escapeHtml(t("linkSource"))}</span>${pathLink(source, "link-history-path")}</div>`
          : "") +
        (target
          ? `<div class="link-history-pathrow"><span class="link-history-plabel">${escapeHtml(t("linkTarget"))}</span>${pathLink(target, "link-history-target")}</div>`
          : "");

  // 操作标签、路径、时间分层：首行是操作+状态+数量+时间，时间靠右；
  // 路径行沿用卡片的左标签列（访问路径/存放位置）+ 右路径，清晰可扫描。
  return `<div class="link-history-item ${meta.cls} ${opMeta.cls}" title="${escapeHtml(error)}">` +
    `<div class="link-history-item-head">` +
      `<span class="link-history-op">${escapeHtml(t(opMeta.key))}</span>` +
      `<span class="link-history-status">${escapeHtml(t(meta.key))}</span>` +
      (numText ? `<span class="link-history-num">${escapeHtml(numText)}</span>` : "") +
      `<span class="link-history-time">${escapeHtml(formatHistoryTime(rec.ts))}</span>` +
    `</div>` +
    `<div class="link-history-item-paths">${pathRows}</div>` +
    (backupNote ? `<div class="link-history-note">${escapeHtml(backupNote)}</div>` : "") +
    (oldTargetNote ? `<div class="link-history-note">${escapeHtml(oldTargetNote)}</div>` : "") +
    (linkNote ? `<div class="link-history-note">${escapeHtml(linkNote)}</div>` : "") +
    (verifyNote ? `<div class="link-history-note">${escapeHtml(verifyNote)}</div>` : "") +
    (error ? `<div class="link-history-item-error" title="${escapeHtml(error)}">${escapeHtml(error)}</div>` : "") +
    `</div>`;
}

function updateHistoryCount(n) {
  const count = $("#linkHistoryCount");
  if (count) count.textContent = n ? `(${n})` : "";
}

async function refreshMigrationHistory() {
  if (!state.api || !state.api.list_migration_history) return;
  let res;
  try {
    res = await state.api.list_migration_history();
  } catch (e) {
    return;
  }
  if (res && res.error) return;
  _historyItems = (res && res.history) || [];
  renderMigrationHistory();
}

function historyMatchesQuery(rec, q) {
  if (!q) return true;
  const needle = q.toLowerCase();
  // 只在路径里搜：访问路径 / 存放位置 / 备份；反查另搜盘符与快照名。操作另开筛选条。
  const fields = [
    String(rec.source || ""),
    String(rec.target || ""),
    String(rec.backup || ""),
    String(rec.snapshot || ""),
    (rec.roots || []).join(" "),
  ].join(" ").toLowerCase();
  return fields.includes(needle);
}

function renderMigrationHistory() {
  const list = $("#linkHistoryList");
  const empty = $("#linkHistoryEmpty");
  if (!list || !empty) return;
  const q = _historyQuery.trim();
  let filtered = _historyItems;
  if (_historyOp !== "all") {
    filtered = filtered.filter((r) => r && r.op === _historyOp);
  }
  if (q) {
    filtered = filtered.filter((r) => historyMatchesQuery(r, q));
  }
  if (!filtered.length) {
    list.innerHTML = "";
    list.classList.add("hidden");
    empty.classList.remove("hidden");
    // 区分「真没记录」「被筛选掉了」两种空，给出对应文案
    const filteredOut = _historyItems.length && (q || _historyOp !== "all");
    empty.textContent = filteredOut
      ? t("linkHistoryNoMatch")
      : t("linkHistoryEmpty");
  } else {
    list.innerHTML = filtered.map(buildHistoryItem).join("");
    list.classList.remove("hidden");
    empty.classList.add("hidden");
  }
  updateHistoryCount(_historyItems.length);
}

async function refreshMigrationHistoryCount() {
  if (!state.api || !state.api.list_migration_history) return;
  let res;
  try {
    res = await state.api.list_migration_history();
  } catch (e) {
    return;
  }
  const items = (res && res.history) || [];
  updateHistoryCount(items.length);
}

async function clearMigrationHistory() {
  if (!state.api || !state.api.clear_migration_history) return;
  const ok = await showConfirmDialog({
    title: t("linkHistoryClear"),
    message: t("linkHistoryClearConfirm"),
    okText: t("linkHistoryClear"),
    danger: true,
  });
  if (!ok) return;
  let res;
  try {
    res = await state.api.clear_migration_history();
  } catch (e) {
    toast(t("loadFailed", e), true);
    return;
  }
  if (res && res.error) {
    toast(res.error, true);
    return;
  }
  toast(t("linkHistoryCleared", Number(res && res.cleared) || 0));
  refreshMigrationHistory();
}

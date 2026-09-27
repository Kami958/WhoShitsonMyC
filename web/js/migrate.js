/* 目录链接管理面板：工具创建的目录链接的列表与维护（打开目标 / 还原 / 删除 / 备份）。 */
"use strict";

// ---- 状态 ----
const _linkMgr = {
  records: [],
  restoring: "",
};
let _linkHintTimer = null;
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

function wireLinkUi() {
  const newBtn = $("#linkNewBtn");
  if (newBtn) newBtn.onclick = () => openMigrateForm("");
  const refreshBtn = $("#linkRefreshBtn");
  if (refreshBtn) refreshBtn.onclick = () => refreshLinkList(true);
  const backBtn = $("#migrateBackBtn");
  if (backBtn) backBtn.onclick = () => hideLinkCreate();

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
        // 选的是目标父目录，自动拼上源目录名作为完整目标路径（可再编辑）
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
      if (btn.getAttribute("data-qa") === "edit") openMigrateEdit(id);
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
  // 历史项里的路径点击 → 资源管理器打开
  const histList = $("#linkHistoryList");
  if (histList) {
    histList.addEventListener("click", (ev) => {
      const a = ev.target.closest(".link-history-open[data-open]");
      if (!a || !state.api) return;
      const p = String(a.getAttribute("data-open") || "");
      if (!p) return;
      state.api.open_folder(p).then((res) => {
        if (res && res.error) toast(res.error, true);
      });
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

/** 只切到表单视图，三个列表视图一起藏起来。 */
function showMigrateFormView() {
  const create = $("#linkCreate");
  if (create) create.classList.remove("hidden");
  ["#linkListView", "#linkQueueView", "#linkHistoryView"].forEach((sel) => {
    const el = $(sel);
    if (el) el.classList.add("hidden");
  });
}

/** 打开迁移表单；带源路径时填入源目录（从对比树右键进入）。已填的草稿原样保留。 */
function showLinkCreate(sourceAbs) {
  showMigrateFormView();
  if (sourceAbs) {
    const si = $("#migrateSourceInput");
    if (si) si.value = sourceAbs;
  }
  syncMigrateFormChrome();
  refreshMigrateHint();
}

/** 进入新建模式；正在编辑别的任务且表单有改动时先确认放弃。 */
async function openMigrateForm(sourceAbs) {
  if (!(await closeMigrateEdit(true))) return;
  showLinkCreate(sourceAbs);
}

/** 退出表单回到队列视图；编辑中的任务一并结束编辑会话。 */
function hideLinkCreate() {
  const create = $("#linkCreate");
  if (create) create.classList.add("hidden");
  endMigrateEdit();
  setLinkView("queue");
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

/** 路径两行：左小标签（"原路径"/"目标"），右路径。 */
function linkCardPath(labelKey, path, opts) {
  const o = opts || {};
  const dim = o.dim ? " is-dim" : "";
  return `<div class="link-card-row">` +
    `<span class="link-card-row-label">${escapeHtml(t(labelKey))}</span>` +
    `<span class="link-card-row-value${dim}" title="${escapeHtml(path)}">${escapeHtml(path)}</span>` +
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

  // 备份行：路径 + 内联「删除备份」，放在目标行下方。缺备份时整行淡化。
  const backupAct =
    rec.backup && rec.backup_exists
      ? linkActBtn("is-danger", "delbackup", idx, t("linkDeleteBackup"), "linkDelBackupTip", busy)
      : "";
  const backupRow = rec.backup
    ? `<div class="link-card-backup" title="${escapeHtml(rec.backup)}">` +
      `<span class="link-card-row-label">${escapeHtml(t("linkBackup"))}</span>` +
      `<span class="link-card-backup-path${rec.backup_exists ? "" : " is-dim"}">` +
      escapeHtml(rec.backup) +
      (rec.backup_exists ? "" : ` <span class="link-card-dim">(${escapeHtml(t("linkBackupGone"))})</span>`) +
      `</span>` +
      backupAct +
      `</div>`
    : "";

  const acts = [];
  // 备份早被删掉的记录：不显示「备份」按钮，还原提示也不提备份
  const hasBackup = !!(rec.backup && rec.backup_exists);
  if (rec.target_exists) acts.push(linkActBtn("is-primary", "open", idx, t("linkOpenTarget"), "linkOpenTargetTip", busy));
  if (hasBackup) acts.push(linkActBtn("", "openbackup", idx, t("linkOpenBackup"), "linkOpenBackupTip", busy));
  if (rec.state === "ok") acts.push(linkActBtn("", "restore", idx, t("linkRestore"), hasBackup ? "linkRestoreTip" : "linkRestoreNoBackupTip", busy));
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
    metaRow +
    `</div>` +
    actionRow +
    `</div>`;
}

async function onLinkAction(act, rec) {
  if (act === "open") {
    const res = await state.api.open_folder(rec.target);
    if (res && res.error) toast(res.error, true);
    return;
  }
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
    const res = await state.api.delete_link_backup(rec.link, permanent);
    if (res && res.error) toast(res.error, true);
    else {
      toast(permanent ? t("linkBackupDeletedPermanent") : t("linkBackupDeleted"));
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

function queueItemHtml(it, idx) {
  const statusKey = it.cancelling
    ? "queueStatusCancelling"
    : (QUEUE_STATUS_META[it.status] || QUEUE_STATUS_META.pending);
  const acts = [];
  if (it.status === "pending") acts.push(queueActBtn("", "edit", it.id, t("queueEdit")));
  // 正在迁移的项不能改也不能抽走，等它跑完
  if (it.status !== "running") acts.push(queueActBtn("is-danger", "remove", it.id, t("queueRemove")));
  const progress = it.status === "running" && it.progress ? migrateProgressView(it.progress) : null;
  return `<div class="link-queue-item is-${it.status}" data-qid="${it.id}">` +
    `<div class="link-queue-head">` +
      `<span class="link-queue-index">${idx + 1}</span>` +
      `<span class="link-queue-dot"></span>` +
      `<span class="link-queue-status">${escapeHtml(t(statusKey))}</span>` +
      (it.cleanBackup ? `<span class="link-queue-flag">${escapeHtml(t("queueCleanBackupFlag"))}</span>` : "") +
      (acts.length ? `<span class="link-queue-acts">${acts.join("")}</span>` : "") +
    `</div>` +
    queuePathRow("linkSource", String(it.source || "")) +
    queuePathRow("linkTarget", String(it.dest || "")) +
    (progress
      ? `<div class="migrate-progress link-queue-progress">` +
        `<div class="migrate-progress-bar"><i class="link-queue-fill" style="width:${progress.pct}%"></i></div>` +
        `<div class="migrate-progress-text">${escapeHtml(progress.label)}</div>` +
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
  // 让出一轮再启动：上一项的迁移线程要等事件派发返回才退出，
  // 就地启动下一项会撞上后端的「已有目录迁移正在进行」。
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
  _migrateQueue.activeId = it.id;
  _migrateQueue.verifyToasting = false;
  _migrateQueue.verifyToasted = false;
  renderMigrateQueue();
  let res;
  try {
    res = await state.api.start_directory_migration(it.source, it.dest, !!it.cleanBackup);
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
      const tail = payload.backup_cleaned
        ? t("migrateBackupCleaned")
        : (it.cleanBackup ? t("migrateBackupKept") : "");
      toast(t("queueTaskDone", it.source, tail));
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
  if (fill) fill.style.width = view.pct + "%";
  if (text) text.textContent = view.label;
}

// ---- 迁移表单：加入队列 / 编辑队列项 ----

/** 表单当前填的值。 */
function migrateFormValues() {
  return {
    source: String(($("#migrateSourceInput") || {}).value || "").trim(),
    dest: String(($("#migrateDestInput") || {}).value || "").trim(),
    cleanBackup: !!($("#migrateCleanBackup") || {}).checked,
  };
}

/** 表单内容是否偏离了编辑对象；新建模式下的草稿也算改动。 */
function migrateFormDirty(item) {
  const v = migrateFormValues();
  if (!item) return !!(v.source || v.dest);
  return (
    v.source !== String(item.source || "") ||
    v.dest !== String(item.dest || "") ||
    v.cleanBackup !== !!item.cleanBackup
  );
}

/** 表单按钮按「新建 / 编辑」两种模式切换：编辑时不显示加入队列，改显示保存修改。 */
function syncMigrateFormChrome() {
  const editing = !!queueItem(_migrateQueue.editingId);
  const backBtn = $("#migrateBackBtn");
  if (backBtn) setI18nLabel(backBtn, editing ? "queueCancelEdit" : "linkBack");
  const enqueueBtn = $("#migrateEnqueueBtn");
  if (enqueueBtn) enqueueBtn.classList.toggle("hidden", editing);
  const saveBtn = $("#migrateSaveBtn");
  if (saveBtn) saveBtn.classList.toggle("hidden", !editing);
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
  const cb = $("#migrateCleanBackup");
  if (si) si.value = String(it.source || "");
  if (di) di.value = String(it.dest || "");
  if (cb) cb.checked = !!it.cleanBackup;
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
    cleanBackup: v.cleanBackup,
    status: "pending",
    error: "",
    progress: null,
    skipped: false,
    cancelling: false,
  });
  // 入队后清空输入，免得同一条路径被顺手再加一次
  const si = $("#migrateSourceInput");
  const di = $("#migrateDestInput");
  const cb = $("#migrateCleanBackup");
  if (si) si.value = "";
  if (di) di.value = "";
  if (cb) cb.checked = false;
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
  it.cleanBackup = v.cleanBackup;
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
      await state.api.cancel_directory_migration();
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

/** 把迁移进度负载算成进度条百分比与文案。 */
function migrateProgressView(payload) {
  const p = payload || {};
  const stage = p.stage || "copy";
  const done = Number(p.done) || 0;
  const total = Number(p.total) || 0;
  const bDone = Number(p.bytes_done) || 0;
  const bTotal = Number(p.bytes_total) || 0;
  if (stage !== "copy") return { pct: 100, label: t("migrateStage" + stage, "") };
  // 优先按字节算（大小文件混合时百分比更平滑），无字节数时回落文件数
  const denom = bTotal > 0 ? bTotal : total;
  const cur = bTotal > 0 ? bDone : done;
  const pct = denom > 0 ? Math.min(100, Math.round((cur / denom) * 100)) : 0;
  let label = `${t("migrateStageCopy")} ${pct}%`;
  if (bTotal > 0) label += ` · ${fmtBytes(bDone)} / ${fmtBytes(bTotal)}`;
  if (p.current) label += " · " + p.current;
  return { pct, label };
}

function onMigrateDirProgress(payload) {
  // 事件可能在 start_directory_migration 返回之前就到了，靠 activeId 认领
  const it = queueActiveItem();
  if (!it) return;
  it.progress = payload || {};
  // 校验阶段提示：小目录复制瞬间结束，用户可能根本看不到进度条到 100%，
  // 所以 verify 进入时弹「正在校验」；通过（进入 setaside/junction）再弹「校验通过」。
  const stage = payload && payload.stage;
  if (stage === "verify" && !_migrateQueue.verifyToasting) {
    _migrateQueue.verifyToasting = true;
    toast(t("migrateVerifyStarted"));
  }
  if (
    (stage === "setaside" || stage === "junction") &&
    _migrateQueue.verifyToasting &&
    !_migrateQueue.verifyToasted
  ) {
    _migrateQueue.verifyToasted = true;
    toast(t("migrateVerifyPassed"));
  }
  updateQueueProgressDom(it);
}

function onMigrateDirDone(payload) {
  finishQueueItem(queueActiveItem(), payload);
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
    if (text) text.textContent = t("migrateStage" + stage, "");
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
  toast(t("linkRestored"));
}

// ---- 迁移历史 ----

const _HISTORY_STATUS_META = {
  success: { cls: "is-success", key: "linkStatusSuccess" },
  failed: { cls: "is-failed", key: "linkStatusFailed" },
  cancelled: { cls: "is-cancelled", key: "linkStatusCancelled" },
};

const _HISTORY_OP_META = {
  migrate: "linkOpMigrate",
  restore: "linkOpRestore",
  delbackup: "linkOpDelBackup",
};

function formatHistoryTime(ts) {
  const n = Number(ts) || 0;
  if (!n) return "";
  const d = new Date(n * 1000);
  const pad = (v) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function buildHistoryItem(rec) {
  const meta = _HISTORY_STATUS_META[rec.status] || _HISTORY_STATUS_META.failed;
  const opKey = _HISTORY_OP_META[rec.op] || "linkOpMigrate";
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
  const numText = numBits.join(" · ");

  const source = String(rec.source || "");
  const target = String(rec.target || "");
  const backup = String(rec.backup || "");
  const error = String(rec.error || "");

  // 历史里的路径可点击，在资源管理器中打开；路径可能已不存在，失败时 toast。
  const pathLink = (path, extraCls) =>
    `<button type="button" class="link-history-open ${extraCls || ""}" data-open="${escapeHtml(path)}" title="${escapeHtml(path)}">${escapeHtml(path)}</button>`;

  // 删除备份：操作行显示被删的备份路径；其余操作显示原路径 → 目标。
  const isDelBackup = rec.op === "delbackup";
  const pathRows =
    isDelBackup
      ? (backup
          ? `<div class="link-history-pathrow"><span class="link-history-plabel">${escapeHtml(t("linkBackup"))}</span>${pathLink(backup, "link-history-path")}</div>`
          : "")
      : (source
          ? `<div class="link-history-pathrow"><span class="link-history-plabel">${escapeHtml(t("linkSource"))}</span>${pathLink(source, "link-history-path")}</div>`
          : "") +
        (target
          ? `<div class="link-history-pathrow"><span class="link-history-plabel">${escapeHtml(t("linkTarget"))}</span>${pathLink(target, "link-history-target")}</div>`
          : "");

  // 操作标签、路径、时间分层：首行是操作+状态+数量+时间，时间靠右；
  // 路径行沿用卡片的左标签列（原路径/目标）+ 右路径，清晰可扫描。
  return `<div class="link-history-item ${meta.cls}" title="${escapeHtml(error)}">` +
    `<div class="link-history-item-head">` +
      `<span class="link-history-dot" aria-hidden="true"></span>` +
      `<span class="link-history-op">${escapeHtml(t(opKey))}</span>` +
      `<span class="link-history-status">${escapeHtml(t(meta.key))}</span>` +
      (numText ? `<span class="link-history-num">${escapeHtml(numText)}</span>` : "") +
      `<span class="link-history-time">${escapeHtml(formatHistoryTime(rec.ts))}</span>` +
    `</div>` +
    `<div class="link-history-item-paths">${pathRows}</div>` +
    (backupNote ? `<div class="link-history-note">${escapeHtml(backupNote)}</div>` : "") +
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
  // 只在路径里搜：原路径 / 目标 / 备份；操作另开筛选条。
  const fields = [
    String(rec.source || ""),
    String(rec.target || ""),
    String(rec.backup || ""),
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

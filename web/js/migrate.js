/* 目录链接管理面板：工具创建的目录链接的列表与维护（打开目标 / 还原 / 删除 / 备份）。 */
"use strict";

// ---- 状态 ----
const _linkMgr = {
  running: false,
  records: [],
  cleanBackup: false,
  restoring: "",
  // 校验阶段 toast 触发标记：copy→verify 弹"正在校验"，verify→setaside 弹"校验通过"
  verifyToasting: false,
  verifyToasted: false,
};
let _linkHintTimer = null;
// 历史视图的本地缓存、路径搜索、操作筛选；都用于纯前端过滤，避免反复打后端。
let _historyItems = [];
let _historyQuery = "";
let _historyOp = "all";
// 链接列表视图的本地缓存、路径搜索、排序选项
let _linkQuery = "";
let _linkSort = "time-desc";

function wireLinkUi() {
  const newBtn = $("#linkNewBtn");
  if (newBtn) newBtn.onclick = () => showLinkCreate("");
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

  const startBtn = $("#migrateStartBtn");
  if (startBtn) startBtn.onclick = startMigration;
  const cancelBtn = $("#migrateCancelBtn");
  if (cancelBtn) cancelBtn.onclick = cancelMigration;

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

  // ---- 迁移列表 / 迁移历史 视图切换 ----
  const viewList = $("#linkViewList");
  const viewHistory = $("#linkViewHistory");
  if (viewList && viewHistory) {
    viewList.onclick = () => setLinkView("list");
    viewHistory.onclick = () => setLinkView("history");
    // 初次进入停在列表视图，本地无需预取历史（切换到历史时才拉取）
    setLinkView("list");
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
  document.querySelectorAll(".link-history-filter-btn").forEach((btn) => {
    btn.onclick = () => {
      const op = btn.getAttribute("data-op") || "all";
      _historyOp = op;
      document.querySelectorAll(".link-history-filter-btn").forEach((b) =>
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

  // 初次进入时静默拉一次链接列表与历史计数
  refreshLinkList(false);
  refreshMigrationHistoryCount();
}

/** 切换「迁移列表 / 迁移历史」视图；切到哪个视图就拉哪个的数据。 */
function setLinkView(view) {
  const listView = $("#linkListView");
  const historyView = $("#linkHistoryView");
  const viewList = $("#linkViewList");
  const viewHistory = $("#linkViewHistory");
  if (!listView || !historyView) return;
  // 表单模式下点视图按钮 = 放弃当前未完成的迁移，回到列表/历史。
  // 否则新建迁移表单会留在列表/历史下方，按钮残留。
  const create = $("#linkCreate");
  if (create && !create.classList.contains("hidden")) {
    hideLinkCreate();
  }
  const showHistory = view === "history";
  listView.classList.toggle("hidden", showHistory);
  historyView.classList.toggle("hidden", !showHistory);
  viewList.classList.toggle("active", !showHistory);
  viewHistory.classList.toggle("active", showHistory);
  viewList.setAttribute("aria-selected", showHistory ? "false" : "true");
  viewHistory.setAttribute("aria-selected", showHistory ? "true" : "false");
  if (showHistory) refreshMigrationHistory();
  else refreshLinkList(false);
}

/** 从对比树右键/搜索项带路径进入链接面板的新建迁移。 */
function openMigrateTab(sourceAbs) {
  if (typeof openToolPanel === "function") openToolPanel("link");
  else if (typeof switchToolTab === "function") switchToolTab("link");
  if (sourceAbs) showLinkCreate(sourceAbs);
  else refreshLinkList(false);
}

/** 展示列表主视图；带源路径时直接进入新建迁移表单。 */
function showLinkCreate(sourceAbs) {
  const create = $("#linkCreate");
  const listView = $("#linkListView");
  const historyView = $("#linkHistoryView");
  if (create) create.classList.remove("hidden");
  // 表单是独立视图，进入时藏起列表/历史，避免 flex 容器在表单上方留下空白
  if (listView) listView.classList.add("hidden");
  if (historyView) historyView.classList.add("hidden");
  const si = $("#migrateSourceInput");
  if (si) {
    si.disabled = false;
    si.value = sourceAbs || "";
  }
  // 目标位置清空：上一次的选择不应留在表单里，否则用户可能没注意就确认
  const di = $("#migrateDestInput");
  if (di && !sourceAbs) di.value = "";
  refreshMigrateHint();
}

function hideLinkCreate() {
  const create = $("#linkCreate");
  if (create) create.classList.add("hidden");
  // 返回列表视图：「返回列表」按钮与迁移完成后的语义一致
  setLinkView("list");
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
  if (rec.target_exists) acts.push(linkActBtn("is-primary", "open", idx, t("linkOpenTarget"), "linkOpenTargetTip", busy));
  if (rec.backup && rec.backup_exists) acts.push(linkActBtn("", "openbackup", idx, t("linkOpenBackup"), "linkOpenBackupTip", busy));
  if (rec.state === "ok") acts.push(linkActBtn("", "restore", idx, t("linkRestore"), "linkRestoreTip", busy));
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
    const ok = await showConfirmDialog({
      title: t("linkRestoreTitle"),
      message: t("linkRestoreConfirm", rec.link, rec.target),
      okText: t("linkRestore"),
      danger: true,
    });
    if (!ok) return;
    const res = await state.api.restore_directory_link(rec.link);
    if (res && res.error) { toast(res.error, true); return; }
    // 后台线程执行；进度经 migrate-dir-restore-* 事件推送
    _linkMgr.restoring = rec.link;
    refreshLinkList(true);
    showRestoreProgress();
    return;
  }
  if (act === "delbackup") {
    const ok = await showConfirmDialog({
      title: t("linkDeleteBackupTitle"),
      message: t("linkDeleteBackupConfirm", rec.backup),
      okText: t("linkDeleteBackup"),
      danger: true,
    });
    if (!ok) return;
    const res = await state.api.delete_link_backup(rec.link);
    if (res && res.error) toast(res.error, true);
    else {
      toast(t("linkBackupDeleted"));
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

function setMigrateRunningUI(on) {
  const si = $("#migrateSourceInput");
  const di = $("#migrateDestInput");
  const startBtn = $("#migrateStartBtn");
  const cancelBtn = $("#migrateCancelBtn");
  if (si) si.disabled = on;
  if (di) di.disabled = on;
  if (startBtn) startBtn.classList.toggle("hidden", on);
  if (cancelBtn) cancelBtn.classList.toggle("hidden", !on);
}

async function startMigration() {
  if (_linkMgr.running) return;
  const source = String(($("#migrateSourceInput") || {}).value || "").trim();
  const dest = String(($("#migrateDestInput") || {}).value || "").trim();
  if (!source || !dest) {
    toast(t("migrateNeedBoth"), true);
    return;
  }
  // 备份处理：「迁移后将原目录备份移入回收站」可选，默认不勾（保留安全网）
  const clean = !!($("#migrateCleanBackup") || {}).checked;
  const confirmMsg = t("migrateConfirm", source, dest, clean);
  const ok = await showConfirmDialog({
    title: t("migrateConfirmTitle"),
    message: confirmMsg,
    okText: t("migrateStart"),
    danger: true,
  });
  if (!ok) return;
  let res;
  try {
    res = await state.api.start_directory_migration(source, dest, clean);
  } catch (e) {
    toast(t("migrateFailed", e), true);
    return;
  }
  if (res && res.error) {
    toast(res.error, true);
    return;
  }
  _linkMgr.running = true;
  _linkMgr.cleanBackup = clean;
  _linkMgr.verifyToasting = false;
  _linkMgr.verifyToasted = false;
  setMigrateRunningUI(true);
  const prog = $("#migrateProgress");
  if (prog) prog.classList.remove("hidden");
  updateLinkMigrateProgress({ stage: "copy", done: 0, total: 0 });
}

async function cancelMigration() {
  try {
    await state.api.cancel_directory_migration();
  } catch (e) {}
}

function updateLinkMigrateProgress(payload) {
  const p = payload || {};
  const stage = p.stage || "copy";
  const done = Number(p.done) || 0;
  const total = Number(p.total) || 0;
  const bDone = Number(p.bytes_done) || 0;
  const bTotal = Number(p.bytes_total) || 0;
  const fill = $("#migrateProgressFill");
  const text = $("#migrateProgressText");
  if (stage === "copy") {
    // 优先按字节算（大小文件混合时百分比更平滑），无字节数时回落文件数
    const denom = bTotal > 0 ? bTotal : total;
    const cur = bTotal > 0 ? bDone : done;
    const pct = denom > 0 ? Math.min(100, Math.round((cur / denom) * 100)) : 0;
    if (fill) fill.style.width = pct + "%";
    let label = `${t("migrateStageCopy")} ${pct}%`;
    if (bTotal > 0) label += ` · ${fmtBytes(bDone)} / ${fmtBytes(bTotal)}`;
    if (p.current) label += " · " + p.current;
    if (text) text.textContent = label;
  } else if (stage !== "copy") {
    if (fill) fill.style.width = "100%";
    if (text) text.textContent = t("migrateStage" + stage, "");
  }
}

function onMigrateDirProgress(payload) {
  if (!_linkMgr.running) {
    _linkMgr.running = true;
    setMigrateRunningUI(true);
  }
  // 校验阶段提示：小目录复制瞬间结束，用户可能根本看不到进度条到 100%，
  // 所以 verify 进入时弹「正在校验」；通过（进入 setaside/junction）再弹「校验通过」。
  const stage = payload && payload.stage;
  if (stage === "verify" && !_linkMgr.verifyToasting) {
    _linkMgr.verifyToasting = true;
    toast(t("migrateVerifyStarted"));
  }
  if (
    (stage === "setaside" || stage === "junction") &&
    _linkMgr.verifyToasting &&
    !_linkMgr.verifyToasted
  ) {
    _linkMgr.verifyToasted = true;
    toast(t("migrateVerifyPassed"));
  }
  updateLinkMigrateProgress(payload);
}

function onMigrateDirDone(payload) {
  _linkMgr.running = false;
  setMigrateRunningUI(false);
  const prog = $("#migrateProgress");
  if (prog) prog.classList.add("hidden");
  // 写入历史完成；停在历史视图则重渲染，否则只更新计数。
  const histView = $("#linkHistoryView");
  if (histView && !histView.classList.contains("hidden")) refreshMigrationHistory();
  else refreshMigrationHistoryCount();
  if (!payload || !payload.ok) {
    toast((payload && payload.error) || t("migrateFailedDefault"), true);
    return;
  }
  const tail = payload.backup_cleaned
    ? t("migrateBackupCleaned")
    : (_linkMgr.cleanBackup ? t("migrateBackupKept") : t("linkAddedHint"));
  _linkMgr.cleanBackup = false;
  // 成功完成后清空源目录与目标位置，下次「新建迁移」就不会复用上次的选择
  const si = $("#migrateSourceInput");
  const di = $("#migrateDestInput");
  if (si) si.value = "";
  if (di) di.value = "";
  toast(t("migrateDone") + " · " + tail);
  hideLinkCreate();
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
  // 删除备份这条记录本身已表达「备份已删」，不再重复显示备份清理标志
  if (rec.backup_cleaned && rec.op !== "delbackup") numBits.push(t("linkBackupDeleted"));
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

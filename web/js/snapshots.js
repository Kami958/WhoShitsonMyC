/* 快照列表 / 文件夹 / 备注 / 导入 */
"use strict";

// ---- 快照列表 ----

function _normPath(p) {
  return String(p || "").replace(/\//g, "\\");
}

/** 内容指纹：同一扫描结果（可在不同路径）视为同一条。优先用后端 content_key。 */
function contentKeyOf(s) {
  if (!s) return "";
  if (s.content_key) return String(s.content_key);
  const tsMs = Math.round(Number(s.scanned_at) * 1000) || 0;
  return [
    s.root || "",
    tsMs,
    Number(s.total_size) || 0,
    Number(s.file_count) || 0,
    Number(s.skipped_count) || 0,
  ].join("|");
}

/**
 * 合并默认目录与手动导入。
 * 先按 path 去重，再按 content_key 去重（移动/复制的同一份只留一条；
 * 默认目录项优先于导入项）。
 */
function mergeSnapshotLists(managed, importedKeep) {
  const byPath = new Map();
  for (const s of managed || []) {
    if (s && s.path) byPath.set(_normPath(s.path), s);
  }
  for (const s of importedKeep || []) {
    if (!s || !s.path) continue;
    const key = _normPath(s.path);
    if (!byPath.has(key)) byPath.set(key, s);
  }
  const byContent = new Map();
  for (const s of byPath.values()) {
    const ck = contentKeyOf(s);
    if (!ck) {
      byContent.set(_normPath(s.path), s);
      continue;
    }
    if (!byContent.has(ck)) {
      byContent.set(ck, s);
      continue;
    }
    // 已有一条：若新来的在默认目录侧（managed），替换导入副本
    const prev = byContent.get(ck);
    const sIsManaged = (managed || []).some(
      (m) => m && _normPath(m.path) === _normPath(s.path)
    );
    if (sIsManaged) byContent.set(ck, s);
    else if (!prev) byContent.set(ck, s);
  }
  return [...byContent.values()];
}

/**
 * 从默认目录拉取快照并刷新左侧列表。
 * 手动导入（state.importedPaths）的项会保留：若文件仍可读则更新摘要，
 * 已消失则从 imported 标记中剔除。
 */
async function loadSnapshots(opts) {
  const quiet = !!(opts && opts.quiet);
  let managed = [];
  let folders = [];
  try {
    managed = (await state.api.list_snapshots()) || [];
  } catch (e) {
    if (!quiet) toast(String(e), true);
    managed = [];
  }
  try {
    if (state.api.list_snapshot_folders) {
      folders = (await state.api.list_snapshot_folders()) || [];
    }
  } catch (e) {
    folders = [];
  }
  // 合并列表里出现过、但磁盘空夹未返回的 folder 名（防御）
  const folderSet = new Set(
    (folders || []).map((n) => String(n || "").trim()).filter(Boolean)
  );
  for (const s of managed) {
    const f = (s && s.folder) || "";
    if (f) folderSet.add(f);
  }
  state.folders = [...folderSet].sort((a, b) =>
    a.localeCompare(b, cmpLocale(), { sensitivity: "base" })
  );

  const managedKeys = new Set(managed.map((s) => _normPath(s.path)));

  // 已回到默认目录的路径不再算「手动导入」
  for (const k of Object.keys(state.importedPaths || {})) {
    if (managedKeys.has(_normPath(k))) delete state.importedPaths[k];
  }

  const externalPaths = Object.keys(state.importedPaths || {});
  let importedKeep = [];
  if (externalPaths.length && state.api.read_snapshot_infos) {
    try {
      const res = await state.api.read_snapshot_infos(externalPaths);
      importedKeep = (res && res.items) || [];
      const ok = new Set(importedKeep.map((s) => _normPath(s.path)));
      const next = {};
      for (const s of importedKeep) next[_normPath(s.path)] = true;
      // 读失败（文件挪走/损坏）的导入标记清掉
      for (const p of externalPaths) {
        if (!ok.has(_normPath(p))) delete state.importedPaths[p];
      }
      state.importedPaths = next;
    } catch (e) {
      importedKeep = state.snapshots.filter(
        (s) =>
          state.importedPaths[_normPath(s.path)] &&
          !managedKeys.has(_normPath(s.path))
      );
    }
  }

  // 导入项若与默认目录内容指纹相同，丢掉导入标记（避免刷新后又冒出来）
  const managedContent = new Set(managed.map(contentKeyOf).filter(Boolean));
  importedKeep = importedKeep.filter((s) => {
    const ck = contentKeyOf(s);
    if (ck && managedContent.has(ck)) {
      delete state.importedPaths[_normPath(s.path)];
      return false;
    }
    return true;
  });

  state.snapshots = mergeSnapshotLists(managed, importedKeep);
  renderSnapshotList();
  updatePickers();
  return managed.length;
}

/** 用户点击刷新：重扫默认目录，保留手动导入。 */
async function refreshSnapshots() {
  const n = await loadSnapshots();
  toast(t("refreshDone", n));
}

/** 导入结果对话框：重复/失败明细，需手动关闭，不自动消失。 */
function closeImportResultDialog() {
  const el = $("#importOverlay");
  if (el) el.classList.add("hidden");
}

function showImportResultDialog({ added, dups, fails }) {
  const summary = $("#importSummary");
  if (summary) {
    summary.textContent = t(
      "importSummaryLine",
      added || 0,
      (dups && dups.length) || 0,
      (fails && fails.length) || 0
    );
  }
  const dupBlock = $("#importDupBlock");
  const dupList = $("#importDupList");
  if (dupBlock && dupList) {
    dupList.innerHTML = "";
    if (dups && dups.length) {
      dupBlock.classList.remove("hidden");
      for (const d of dups) {
        const li = document.createElement("li");
        const path = d.path || d.label || "";
        const reason =
          d.reason === "both"
            ? t("importDupBoth")
            : d.reason === "content"
              ? t("importDupContent")
              : t("importDupPath");
        li.innerHTML = `${escapeHtml(path)}<span class="import-reason">${escapeHtml(reason)}</span>`;
        dupList.appendChild(li);
      }
    } else {
      dupBlock.classList.add("hidden");
    }
  }
  const failBlock = $("#importFailBlock");
  const failList = $("#importFailList");
  if (failBlock && failList) {
    failList.innerHTML = "";
    if (fails && fails.length) {
      failBlock.classList.remove("hidden");
      for (const f of fails) {
        const li = document.createElement("li");
        const path = f.path || "";
        const err = f.error || "";
        li.innerHTML = `${escapeHtml(path)}${
          err ? `<span class="import-reason">${escapeHtml(err)}</span>` : ""
        }`;
        failList.appendChild(li);
      }
    } else {
      failBlock.classList.add("hidden");
    }
  }
  const overlay = $("#importOverlay");
  if (overlay) overlay.classList.remove("hidden");
}

/** 从其它位置选择 .db/.dbz 读入列表（多选；不复制文件，只登记路径）。 */
async function importSnapshots() {
  let pick;
  try {
    pick = await state.api.choose_snapshot_files();
  } catch (e) {
    toast(String(e), true);
    return;
  }
  if (pick && pick.error) {
    toast(pick.error, true);
    return;
  }
  const paths = (pick && pick.paths) || [];
  if (!paths.length) {
    toast(t("importNone"));
    return;
  }

  let res;
  try {
    res = await state.api.read_snapshot_infos(paths);
  } catch (e) {
    toast(String(e), true);
    return;
  }
  const items = (res && res.items) || [];
  const errors = (res && res.errors) || [];
  const byPath = new Set(state.snapshots.map((s) => _normPath(s.path)));
  const byContent = new Set(state.snapshots.map(contentKeyOf).filter(Boolean));
  let added = 0;
  const dups = [];
  for (const s of items) {
    const pathKey = _normPath(s.path);
    const ck = contentKeyOf(s);
    const pathHit = byPath.has(pathKey);
    const contentHit = !!(ck && byContent.has(ck));
    // 同路径或同内容指纹 → 重复（仍刷新同路径项的元数据）
    if (pathHit || contentHit) {
      dups.push({
        path: s.path || pathKey,
        reason: pathHit && contentHit ? "both" : contentHit ? "content" : "path",
      });
      if (pathHit) {
        state.snapshots = state.snapshots.map((x) =>
          _normPath(x.path) === pathKey ? s : x
        );
      }
      continue;
    }
    state.importedPaths[pathKey] = true;
    state.snapshots.push(s);
    byPath.add(pathKey);
    if (ck) byContent.add(ck);
    added += 1;
  }
  renderSnapshotList();
  updatePickers();
  const fails = errors.map((e) => ({
    path: e.path || "",
    error: e.error || String(e),
  }));
  // 有重复或失败：弹窗列出明细，不自动消失；仅全部成功时 toast
  if (dups.length || fails.length) {
    showImportResultDialog({ added, dups, fails });
    if (added) toast(t("importDone", added));
  } else if (added) {
    toast(t("importDone", added));
  } else {
    toast(t("importNone"));
  }
}

/** 快照列表的排序方式。 */
const SNAP_SORTERS = {
  "time-desc": (a, b) => b.scanned_at - a.scanned_at,
  "time-asc": (a, b) => a.scanned_at - b.scanned_at,
  "name-asc": (a, b) => a.root.localeCompare(b.root, cmpLocale()),
  "name-desc": (a, b) => b.root.localeCompare(a.root, cmpLocale()),
};

/** 新扫描完成的侧栏闪烁：归一化 path + 定时清除（重绘时靠 path 重新挂 class）。 */
let _flashSnapPath = "";
let _flashSnapTimer = 0;

/** 让指定快照在列表中缓慢闪烁约 ms 毫秒（默认 3s，与 CSS 1s×3 对齐）。 */
function flashSnapshot(path, ms = 3000) {
  if (!path) return;
  _flashSnapPath = _normPath(path);
  if (_flashSnapTimer) {
    clearTimeout(_flashSnapTimer);
    _flashSnapTimer = 0;
  }
  renderSnapshotList();
  _flashSnapTimer = setTimeout(() => {
    _flashSnapPath = "";
    _flashSnapTimer = 0;
    renderSnapshotList();
  }, ms);
}

/** 按 folder 分组；folder 空串 = 未归类。 */
function groupSnapshotsByFolder(ordered) {
  const map = new Map(); // folder → snapshots[]
  map.set("", []);
  for (const name of state.folders || []) {
    if (name && !map.has(name)) map.set(name, []);
  }
  for (const s of ordered) {
    const f = (s && s.folder) || "";
    if (!map.has(f)) map.set(f, []);
    map.get(f).push(s);
  }
  return map;
}

function isFolderCollapsed(name) {
  const key = name || "";
  return !!state.folderCollapsed[key];
}

function toggleFolderCollapsed(name) {
  const key = name || "";
  state.folderCollapsed[key] = !state.folderCollapsed[key];
  renderSnapshotList();
}

/** 构建单条快照 DOM；展示顺序：路径 → 时间 → 备注 → 元信息 → 操作。 */
function buildSnapEl(s) {
  const isOld = s.path === state.oldPath;
  const isNew = s.path === state.newPath;
  const isFlash = !!_flashSnapPath && _normPath(s.path) === _flashSnapPath;

  const el = document.createElement("div");
  el.className =
    "snap" +
    (isOld ? " sel-old" : "") +
    (isNew ? " sel-new" : "") +
    (isFlash ? " snap-flash" : "");
  el.dataset.path = s.path;

  const role =
    (isOld ? `<span class="snap-role old-c">${t("snapRoleBase")}</span>` : "") +
    (isNew ? `<span class="snap-role new-c">${t("snapRoleCurrent")}</span>` : "");

  const zipTag = s.compressed
    ? `<span class="snap-zip" title="${escapeHtml(fmtBytes(s.file_size || 0))}">${t("tagCompressed")}</span>`
    : "";
  const noteText = (s.note || "").trim();
  const noteLine = noteText
    ? `<div class="snap-note" data-act="note" title="${escapeHtml(t("noteTitle"))}">${escapeHtml(noteText)}</div>`
    : `<div class="snap-note snap-note-empty" data-act="note" title="${escapeHtml(t("noteTitle"))}">${escapeHtml(t("notePlaceholder"))}</div>`;
  // 顺序：扫描路径 → 时间（含角色/压缩）→ 备注 → 相对时间与大小 → 操作
  el.innerHTML = `
    <div class="snap-root">${escapeHtml(s.root)}${role}${zipTag}</div>
    <div class="snap-time">${fmtTime(s.scanned_at)}</div>
    ${noteLine}
    <div class="snap-meta">${fmtAgo(s.scanned_at)} · ${fmtBytes(s.total_size)} · ${t("filesN", (s.file_count || 0).toLocaleString())}${s.compressed && s.file_size ? " · " + fmtBytes(s.file_size) : ""}</div>
    <div class="snap-acts">
      <button class="snap-act old" data-act="old">${t("setAsBase")}</button>
      <button class="snap-act new" data-act="new">${t("setAsCurrent")}</button>
      <button class="snap-act move-btn" data-act="move">${t("folderMove")}</button>
      <button class="snap-act del" data-act="del">${t("delete")}</button>
    </div>`;

  el.querySelector('[data-act="old"]').onclick = () => selectSnapshot("old", s.path);
  el.querySelector('[data-act="new"]').onclick = () => selectSnapshot("new", s.path);
  // 备注：点正文备注行编辑（不再单独放「备注」按钮，避免与「添加备注」重复）
  el.querySelectorAll('[data-act="note"]').forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      editSnapshotNote(s);
    };
  });
  const moveBtn = el.querySelector('[data-act="move"]');
  if (moveBtn) {
    moveBtn.onclick = (e) => {
      e.stopPropagation();
      openMoveFolderDialog(s);
    };
  }
  el.querySelector('[data-act="del"]').onclick = () => deleteSnapshot(s.path);
  return { el, isFlash };
}

function renderSnapshotList() {
  const list = $("#snapshotList");
  list.innerHTML = "";
  if (state.snapshots.length === 0 && !(state.folders && state.folders.length)) {
    list.innerHTML = `<div class="side-empty">${t("noSnapshots")}</div>`;
    return;
  }
  const ordered = [...state.snapshots].sort(
    SNAP_SORTERS[state.snapSort] || SNAP_SORTERS["time-desc"]
  );
  const groups = groupSnapshotsByFolder(ordered);
  let flashEl = null;

  // 先文件夹（有名），再未归类
  const folderNames = [...groups.keys()].filter((k) => k !== "");
  folderNames.sort((a, b) =>
    a.localeCompare(b, cmpLocale(), { sensitivity: "base" })
  );
  const sectionOrder = [...folderNames, ""];

  for (const fname of sectionOrder) {
    const items = groups.get(fname) || [];
    // 无任何快照且无真实文件夹时，不画「未归类」空壳
    if (fname === "" && items.length === 0 && folderNames.length === 0) continue;
    // 空的未归类区：若有文件夹也保留，方便放回
    const section = document.createElement("div");
    section.className = "snap-folder" + (fname ? "" : " snap-folder-root");
    section.dataset.folder = fname;

    const head = document.createElement("div");
    head.className = "snap-folder-head";
    const collapsed = isFolderCollapsed(fname);
    const caret = collapsed ? "▸" : "▾";
    const label =
      fname === ""
        ? t("folderUnfiled")
        : fname;
    head.innerHTML = `
      <button type="button" class="snap-folder-toggle" data-act="toggle" title="">
        <span class="snap-folder-caret">${caret}</span>
        <span class="snap-folder-name">${escapeHtml(label)}</span>
        <span class="snap-folder-count">${escapeHtml(t("folderCount", items.length))}</span>
      </button>
      <div class="snap-folder-acts"></div>`;
    const toggleBtn = head.querySelector('[data-act="toggle"]');
    if (toggleBtn) {
      toggleBtn.onclick = () => toggleFolderCollapsed(fname);
    }
    const acts = head.querySelector(".snap-folder-acts");
    if (acts && fname) {
      const ren = document.createElement("button");
      ren.type = "button";
      ren.className = "snap-act";
      ren.textContent = t("folderRename");
      ren.onclick = (e) => {
        e.stopPropagation();
        openRenameFolderDialog(fname);
      };
      const del = document.createElement("button");
      del.type = "button";
      del.className = "snap-act del";
      del.textContent = t("folderDelete");
      del.onclick = (e) => {
        e.stopPropagation();
        deleteSnapshotFolder(fname, items.length);
      };
      acts.appendChild(ren);
      acts.appendChild(del);
    }
    section.appendChild(head);

    if (!collapsed) {
      const body = document.createElement("div");
      body.className = "snap-folder-body";
      if (items.length === 0) {
        const empty = document.createElement("div");
        empty.className = "snap-folder-empty";
        empty.textContent = t("folderEmpty");
        body.appendChild(empty);
      } else {
        for (const s of items) {
          const { el, isFlash } = buildSnapEl(s);
          body.appendChild(el);
          if (isFlash) flashEl = el;
        }
      }
      section.appendChild(body);
    }
    list.appendChild(section);
  }

  if (flashEl) {
    try {
      flashEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (e) {
      try {
        flashEl.scrollIntoView(false);
      } catch (e2) {}
    }
  }
}

/** 备注对话框状态。 */
let _noteTarget = null; // 当前编辑的 snapshot 摘要
/** 正在落盘的备注 path → 防止连点重复提交。 */
const _noteSavingPaths = new Set();

/** 文件夹对话框：create | rename | move */
let _folderDialog = null; // { mode, folder?, snap? }

function openFolderNameDialog({ mode, title, initial, onSave }) {
  _folderDialog = { mode, onSave };
  const overlay = $("#folderOverlay");
  const titleEl = $("#folderDialogTitle");
  const hint = $("#folderDialogHint");
  const input = $("#folderNameInput");
  const pick = $("#folderPickList");
  const nameRow = $("#folderNameRow");
  if (titleEl) titleEl.textContent = title;
  if (hint) hint.textContent = "";
  if (pick) {
    pick.innerHTML = "";
    pick.classList.add("hidden");
  }
  if (nameRow) nameRow.classList.remove("hidden");
  if (input) {
    input.value = initial || "";
    input.placeholder = t("folderNamePlaceholder");
    input.disabled = false;
    input.classList.remove("hidden");
  }
  const saveBtn = $("#folderSaveBtn");
  if (saveBtn) {
    saveBtn.classList.remove("hidden");
    saveBtn.disabled = false;
  }
  if (overlay) overlay.classList.remove("hidden");
  setTimeout(() => {
    try {
      if (input) {
        input.focus();
        input.select();
      }
    } catch (e) {}
  }, 30);
}

function openFolderPickDialog({ title, current, options, onPick }) {
  _folderDialog = { mode: "move", onPick };
  const overlay = $("#folderOverlay");
  const titleEl = $("#folderDialogTitle");
  const hint = $("#folderDialogHint");
  const input = $("#folderNameInput");
  const nameRow = $("#folderNameRow");
  const pick = $("#folderPickList");
  if (titleEl) titleEl.textContent = title;
  if (hint) hint.textContent = t("folderMoveTitle");
  if (nameRow) nameRow.classList.add("hidden");
  if (input) {
    input.value = "";
    input.classList.add("hidden");
  }
  const saveBtn = $("#folderSaveBtn");
  if (saveBtn) saveBtn.classList.add("hidden");
  if (pick) {
    pick.innerHTML = "";
    pick.classList.remove("hidden");
    for (const opt of options) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className =
        "folder-pick-item" +
        (opt.value === current ? " is-current" : "");
      btn.textContent = opt.label;
      btn.disabled = opt.value === current;
      btn.onclick = () => {
        closeFolderDialog();
        if (onPick) onPick(opt.value);
      };
      pick.appendChild(btn);
    }
  }
  if (overlay) overlay.classList.remove("hidden");
}

function closeFolderDialog() {
  const overlay = $("#folderOverlay");
  if (overlay) overlay.classList.add("hidden");
  _folderDialog = null;
  const input = $("#folderNameInput");
  if (input) {
    input.disabled = false;
    input.classList.remove("hidden");
  }
  const nameRow = $("#folderNameRow");
  if (nameRow) nameRow.classList.remove("hidden");
  const pick = $("#folderPickList");
  if (pick) {
    pick.innerHTML = "";
    pick.classList.add("hidden");
  }
  const saveBtn = $("#folderSaveBtn");
  if (saveBtn) {
    saveBtn.disabled = false;
    saveBtn.classList.remove("hidden");
  }
}

async function submitFolderDialog() {
  const dlg = _folderDialog;
  if (!dlg) {
    closeFolderDialog();
    return;
  }
  if (dlg.mode === "move") {
    // 选择列表用点击项提交
    return;
  }
  const input = $("#folderNameInput");
  const name = input ? String(input.value || "").trim() : "";
  if (!name) {
    toast(t("folderNameInvalid"), true);
    return;
  }
  const saveBtn = $("#folderSaveBtn");
  if (saveBtn) saveBtn.disabled = true;
  if (input) input.disabled = true;
  try {
    if (dlg.onSave) await dlg.onSave(name);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
    if (input) input.disabled = false;
  }
}

function openCreateFolderDialog() {
  openFolderNameDialog({
    mode: "create",
    title: t("folderDialogCreateTitle"),
    initial: "",
    onSave: async (name) => {
      let res;
      try {
        res = await state.api.create_snapshot_folder(name);
      } catch (e) {
        toast(t("folderCreateFailed", e), true);
        return;
      }
      if (res && res.error) {
        toast(t("folderCreateFailed", res.error), true);
        return;
      }
      closeFolderDialog();
      await loadSnapshots({ quiet: true });
      toast(t("folderCreated", (res && res.folder) || name));
    },
  });
}

function openRenameFolderDialog(folder) {
  openFolderNameDialog({
    mode: "rename",
    title: t("folderDialogRenameTitle"),
    initial: folder || "",
    onSave: async (name) => {
      let res;
      try {
        res = await state.api.rename_snapshot_folder(folder, name);
      } catch (e) {
        toast(t("folderRenameFailed", e), true);
        return;
      }
      if (res && res.error) {
        toast(t("folderRenameFailed", res.error), true);
        return;
      }
      // 折叠状态键跟着改
      if (state.folderCollapsed[folder]) {
        delete state.folderCollapsed[folder];
        state.folderCollapsed[name] = true;
      }
      closeFolderDialog();
      // 路径会变：刷新列表；对比选中若在该夹内需靠 load 后 path 更新
      const oldPaths = new Set(
        state.snapshots
          .filter((s) => (s.folder || "") === folder)
          .map((s) => s.path)
      );
      await loadSnapshots({ quiet: true });
      // 尝试按 content_key 或 basename 对齐 old/new 选择（路径变了）
      _remapSelectionAfterMove(oldPaths);
      toast(t("folderRenamed", (res && res.folder) || name));
    },
  });
}

function openMoveFolderDialog(s) {
  if (!s || !s.path) return;
  const current = s.folder || "";
  const options = [
    { value: "", label: t("folderMoveRoot") },
    ...((state.folders || []).map((n) => ({ value: n, label: n }))),
  ];
  openFolderPickDialog({
    title: t("folderDialogMoveTitle"),
    current,
    options,
    onPick: async (folder) => {
      const prevPath = s.path;
      let res;
      try {
        res = await state.api.move_snapshot_to_folder(prevPath, folder || "");
      } catch (e) {
        toast(t("folderMoveFailed", e), true);
        return;
      }
      if (res && res.error) {
        toast(t("folderMoveFailed", res.error), true);
        return;
      }
      const newPath = res && res.path ? res.path : prevPath;
      if (state.oldPath === prevPath) state.oldPath = newPath;
      if (state.newPath === prevPath) state.newPath = newPath;
      if (state.importedPaths[_normPath(prevPath)]) {
        delete state.importedPaths[_normPath(prevPath)];
        // 移入默认目录管理范围，不再算导入
      }
      await loadSnapshots({ quiet: true });
      toast(t("folderMoved"));
    },
  });
}

/** 文件夹重命名后，用「旧路径集合」对齐仍选中的快照（按文件名）。 */
function _remapSelectionAfterMove(oldPathSet) {
  if (!oldPathSet || !oldPathSet.size) return;
  const byBase = new Map();
  for (const s of state.snapshots) {
    const base = String(s.path || "").replace(/^.*[\\/]/, "");
    if (base) byBase.set(base.toLowerCase(), s.path);
  }
  const mapOne = (p) => {
    if (!p || !oldPathSet.has(p)) return p;
    const base = String(p).replace(/^.*[\\/]/, "").toLowerCase();
    return byBase.get(base) || p;
  };
  state.oldPath = mapOne(state.oldPath);
  state.newPath = mapOne(state.newPath);
  updatePickers();
}

async function deleteSnapshotFolder(name, itemCount) {
  if (!name) return;
  const count = Math.max(0, Number(itemCount) || 0);
  if (count > 0) {
    // 非空：二次确认
    const ok1 = await showConfirmDialog({
      title: t("folderDelete"),
      message: t("folderDeleteConfirmWithItems", name, count),
      okText: t("folderDelete"),
      danger: true,
    });
    if (!ok1) return;
    const ok2 = await showConfirmDialog({
      title: t("folderDelete"),
      message: t("folderDeleteConfirmAgain", name),
      okText: t("folderDelete"),
      danger: true,
    });
    if (!ok2) return;
  } else {
    const ok = await showConfirmDialog({
      title: t("folderDelete"),
      message: t("folderDeleteConfirm", name),
      okText: t("folderDelete"),
      danger: true,
    });
    if (!ok) return;
  }

  // 夹内快照若正被选中/对比，先记下以便清掉
  const inFolder = state.snapshots.filter((s) => (s.folder || "") === name);
  const paths = new Set(inFolder.map((s) => s.path));
  const hitCompare =
    paths.has(state.oldPath) || paths.has(state.newPath);

  let res;
  try {
    res = await state.api.delete_snapshot_folder(name, count > 0);
  } catch (e) {
    toast(t("folderDeleteFailed", e), true);
    return;
  }
  if (res && res.error) {
    toast(t("folderDeleteFailed", res.error), true);
    return;
  }
  if (paths.has(state.oldPath)) state.oldPath = "";
  if (paths.has(state.newPath)) state.newPath = "";
  for (const p of paths) delete state.importedPaths[_normPath(p)];
  if (state.compared && hitCompare) resetCompareView();
  delete state.folderCollapsed[name];
  await loadSnapshots({ quiet: true });
  toast(t("folderDeleted"));
}

function openNoteDialog(s) {
  if (!s || !s.path) {
    toast(t("noteCleared"), true);
    return;
  }
  _noteTarget = s;
  const input = $("#noteInput");
  const hint = $("#noteDialogHint");
  if (hint) hint.textContent = t("noteFor", s.root || s.path || "");
  if (input) {
    input.value = s.note || "";
    input.placeholder = t("notePlaceholder");
    input.disabled = false;
  }
  const saveBtn = $("#noteSaveBtn");
  if (saveBtn) saveBtn.disabled = false;
  $("#noteOverlay").classList.remove("hidden");
  setTimeout(() => {
    try {
      input.focus();
      input.select();
    } catch (e) {}
  }, 30);
}

function closeNoteDialog() {
  $("#noteOverlay").classList.add("hidden");
  _noteTarget = null;
  const input = $("#noteInput");
  if (input) input.disabled = false;
  const saveBtn = $("#noteSaveBtn");
  if (saveBtn) saveBtn.disabled = false;
}

/**
 * 保存备注：先关对话框 + 乐观更新列表 + toast「正在保存」，
 * 再异步写文件，避免 .dbz 重写时界面像卡住。
 */
async function saveNoteDialog(raw) {
  const s = _noteTarget;
  if (!s || !s.path) {
    closeNoteDialog();
    return;
  }
  const path = s.path;
  if (_noteSavingPaths.has(path)) return;

  const text = (raw == null ? "" : String(raw)).trim();
  const prev = (s.note || "").trim();
  // 未改动：直接关，不写盘
  if (text === prev) {
    closeNoteDialog();
    return;
  }

  _noteSavingPaths.add(path);
  // 乐观更新并立刻关闭，不 await 写盘
  for (const item of state.snapshots) {
    if (item.path === path) item.note = text;
  }
  closeNoteDialog();
  renderSnapshotList();
  updatePickers();
  toast(t("noteSaving"));

  let res;
  try {
    res = await state.api.set_snapshot_note(path, text);
  } catch (e) {
    for (const item of state.snapshots) {
      if (item.path === path) item.note = prev;
    }
    renderSnapshotList();
    updatePickers();
    toast(String(e), true);
    _noteSavingPaths.delete(path);
    return;
  }
  _noteSavingPaths.delete(path);

  if (res && res.error) {
    for (const item of state.snapshots) {
      if (item.path === path) item.note = prev;
    }
    renderSnapshotList();
    updatePickers();
    toast(res.error, true);
    return;
  }

  const finalText = res && res.note != null ? String(res.note) : text;
  for (const item of state.snapshots) {
    if (item.path === path) item.note = finalText;
  }
  renderSnapshotList();
  updatePickers();
  toast(finalText ? t("noteSaved") : t("noteCleared"));
}

/** 编辑快照备注（应用内对话框，写入快照文件本身）。 */
function editSnapshotNote(s) {
  openNoteDialog(s);
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
  delete state.importedPaths[_normPath(path)];
  if (state.compared && inCompare) resetCompareView();
  await loadSnapshots({ quiet: true });
  toast(t("snapshotDeleted"));
}

/** 清空对比结果区域，回到初始空态（快照列表不动）。 */
function resetCompareView() {
  state.compared = false;
  state.compareRoot = "";
  state._topNodes = null;
  state._lastSummary = null;
  state._lastCompareKey = "";
  state._lastComparePaths = "";
  if (typeof resetSearchPreheatUi === "function") resetSearchPreheatUi();
  else {
    state.searchPreheat = "idle";
    state.searchPreheatKey = "";
  }
  clearTreeSearch();
  $("#summaryBar").classList.add("hidden");
  $("#skipWarn").classList.add("hidden");
  $("#tree").innerHTML = "";
  $("#emptyState").classList.remove("hidden");
  // 释放后端对比会话与内存搜索索引，避免清空后仍占内存、再推就绪事件
  try {
    const p = state.api && state.api.close_diff_session && state.api.close_diff_session();
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch (_) { /* 后端不可用时忽略 */ }
}

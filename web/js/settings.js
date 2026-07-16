/* 设置 / 日志 / 卸载 / 迁移 */
"use strict";

// ---- 设置（设置页 / 扫描线程 / 压缩 / MFT / YAML 持久化）----

async function openSnapshotDir() {
  const res = await state.api.open_snapshot_dir();
  if (res.error) toast(res.error, true);
}

function fillWorkerSelect(sel, s) {
  if (!sel) return;
  const cpu = s.cpu_count || 2;
  const opts = [...new Set([1, 2, 4, 8, 16, cpu, s.scan_workers])]
    .filter((n) => n >= 1 && n <= Math.max(128, cpu, s.scan_workers || 1))
    .sort((a, b) => a - b);
  sel.innerHTML = opts
    .map((n) => `<option value="${n}">${n}${n === cpu ? t("cpuTag") : ""}</option>`)
    .join("");
  sel.value = String(s.scan_workers);
}

/**
 * 设置页草稿：打开时从后端拷贝，控件只改草稿；
 * 点「完成」才 apply_settings 一次写入。关闭/取消丢弃草稿。
 */
let _settingsDraft = null;

/** 读取设置并填充设置页控件（仅草稿，不写后端）。 */
async function loadSettings() {
  let s;
  try {
    s = await state.api.get_settings();
  } catch (e) {
    return;
  }
  state._settings = s;
  state.searchMemoryIndex = s.search_memory_index !== false;
  _settingsDraft = {
    scan_workers: Number(s.scan_workers) || 1,
    compress_snapshots: !!s.compress_snapshots,
    use_mft: !!s.use_mft,
    search_memory_index: s.search_memory_index !== false,
    log_sanitize: s.log_sanitize !== false,
    // 空串 = 内置目录；非空 = 自定义绝对路径
    snapshot_dir: s.snapshot_dir_is_custom ? (s.snapshot_dir_configured || s.snapshot_dir || "") : "",
    snapshot_dir_display: s.snapshot_dir || "",
    snapshot_dir_builtin: s.snapshot_dir_builtin || "",
    snapshot_dir_is_custom: !!s.snapshot_dir_is_custom,
    settings_path: s.settings_path || "",
    mft_platform_ok: s.mft_platform_ok !== false,
    is_admin: !!s.is_admin,
    cpu_count: s.cpu_count,
    delete_blacklist: _normalizeBlacklistDraft(s.delete_blacklist),
  };
  fillAppVersionLabel(s.version || "");
  fillSettingsFormFromDraft();
}

function _normalizeBlacklistDraft(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  const seen = new Set();
  for (const item of raw) {
    let path = "";
    let mode = "prefix";
    if (typeof item === "string") {
      path = item.trim();
    } else if (item && typeof item === "object") {
      path = String(item.path || "").trim();
      mode = String(item.mode || "prefix").trim().toLowerCase();
    }
    if (!path) continue;
    if (mode !== "exact" && mode !== "prefix" && mode !== "regex") mode = "prefix";
    const key = `${mode}\0${path}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ path, mode });
  }
  return out;
}

function _blacklistModeLabel(mode) {
  if (mode === "exact") return t("blacklistModeExact");
  if (mode === "regex") return t("blacklistModeRegex");
  return t("blacklistModePrefix");
}

function fillBlacklistSelect() {
  const sel = $("#blacklistSelect");
  if (!sel) return;
  const list =
    (_settingsDraft && Array.isArray(_settingsDraft.delete_blacklist)
      ? _settingsDraft.delete_blacklist
      : []) || [];
  const prev = sel.value;
  sel.innerHTML = list
    .map((e, i) => {
      const label = `${e.path} · ${_blacklistModeLabel(e.mode)}`;
      return `<option value="${i}">${escapeHtml(label)}</option>`;
    })
    .join("");
  if (prev !== "" && sel.querySelector(`option[value="${prev}"]`)) {
    sel.value = prev;
  }
}

function addBlacklistFromForm() {
  if (!_settingsDraft) return;
  const input = $("#blacklistPath");
  const modeSel = $("#blacklistMode");
  const path = input ? String(input.value || "").trim() : "";
  const mode = modeSel ? String(modeSel.value || "prefix").toLowerCase() : "prefix";
  if (!path) {
    toast(t("blacklistEmpty"), true);
    return;
  }
  if (mode === "regex") {
    try {
      // eslint-disable-next-line no-new
      new RegExp(path);
    } catch (e) {
      toast(t("blacklistBadRegex"), true);
      return;
    }
  }
  const list = Array.isArray(_settingsDraft.delete_blacklist)
    ? _settingsDraft.delete_blacklist
    : [];
  if (list.some((e) => e.path === path && e.mode === mode)) {
    toast(t("blacklistExists"));
    return;
  }
  list.push({ path, mode });
  _settingsDraft.delete_blacklist = list;
  if (input) input.value = "";
  fillBlacklistSelect();
}

function removeBlacklistSelected() {
  if (!_settingsDraft) return;
  const sel = $("#blacklistSelect");
  if (!sel || sel.selectedIndex < 0) return;
  const idx = Number(sel.value);
  const list = Array.isArray(_settingsDraft.delete_blacklist)
    ? _settingsDraft.delete_blacklist.slice()
    : [];
  if (!Number.isFinite(idx) || idx < 0 || idx >= list.length) return;
  list.splice(idx, 1);
  _settingsDraft.delete_blacklist = list;
  fillBlacklistSelect();
}

async function pickBlacklistFolder() {
  if (!state.api || !state.api.choose_folder) return;
  try {
    const res = await state.api.choose_folder();
    if (res && res.error) {
      toast(res.error, true);
      return;
    }
    const path = (res && res.path) || "";
    if (!path) return;
    const input = $("#blacklistPath");
    if (input) input.value = path;
    const modeSel = $("#blacklistMode");
    if (modeSel) modeSel.value = modeSel.value || "prefix";
  } catch (e) {
    toast(String(e), true);
  }
}

function fillSettingsFormFromDraft() {
  const d = _settingsDraft;
  if (!d) return;
  fillWorkerSelect($("#workerSel"), {
    scan_workers: d.scan_workers,
    cpu_count: d.cpu_count,
  });
  const compressChk = $("#compressChk");
  if (compressChk) compressChk.checked = !!d.compress_snapshots;
  const mftChk = $("#mftChk");
  if (mftChk) {
    mftChk.checked = !!d.use_mft;
    mftChk.disabled = d.mft_platform_ok === false;
  }
  const searchMemIdxChk = $("#searchMemIdxChk");
  if (searchMemIdxChk) {
    searchMemIdxChk.checked = d.search_memory_index !== false;
  }
  const logSanitizeChk = $("#logSanitizeChk");
  if (logSanitizeChk) {
    logSanitizeChk.checked = d.log_sanitize !== false;
  }
  fillBlacklistSelect();
  updateSnapDirLine({
    snapshot_dir: d.snapshot_dir_display || d.snapshot_dir_builtin || "",
    snapshot_dir_is_custom: !!d.snapshot_dir_is_custom,
  });
}

function updateSnapDirLine(s) {
  const el = $("#snapDirPathLine");
  if (!el) return;
  const path = (s && s.snapshot_dir) || "";
  const custom = !!(s && s.snapshot_dir_is_custom);
  if (!path) {
    el.textContent = "";
    return;
  }
  el.textContent = custom ? t("snapDirCustom", path) : t("snapDirBuiltin", path);
  el.title = path;
}

/** 只改草稿：弹文件夹选择，点完成才生效。 */
async function chooseSnapDir() {
  let res;
  try {
    // 优先只选不写；旧后端回退到会写盘的 API
    if (state.api.pick_snapshot_dir) {
      res = await state.api.pick_snapshot_dir();
    } else {
      res = await state.api.choose_snapshot_dir();
    }
  } catch (e) {
    toast(String(e), true);
    return;
  }
  if (!res || res.cancelled) return;
  if (res.error) {
    toast(res.error, true);
    return;
  }
  const path = res.path || res.snapshot_dir || "";
  if (!_settingsDraft || !path) return;
  _settingsDraft.snapshot_dir = path;
  _settingsDraft.snapshot_dir_display = path;
  _settingsDraft.snapshot_dir_is_custom = true;
  if (res.snapshot_dir_builtin) {
    _settingsDraft.snapshot_dir_builtin = res.snapshot_dir_builtin;
  }
  updateSnapDirLine({
    snapshot_dir: path,
    snapshot_dir_is_custom: true,
  });
}

/** 草稿恢复内置目录；点完成才写后端。 */
function resetSnapDirDraft() {
  if (!_settingsDraft) return;
  const builtin =
    _settingsDraft.snapshot_dir_builtin ||
    (state._settings && state._settings.snapshot_dir_builtin) ||
    "";
  _settingsDraft.snapshot_dir = "";
  _settingsDraft.snapshot_dir_display = builtin;
  _settingsDraft.snapshot_dir_is_custom = false;
  updateSnapDirLine({
    snapshot_dir: builtin,
    snapshot_dir_is_custom: false,
  });
}

/** 设置页签切换（通用 / 日志…）。 */
function switchSettingsTab(tabId) {
  const id = tabId || "general";
  document.querySelectorAll(".settings-tab").forEach((btn) => {
    const on = btn.getAttribute("data-settings-tab") === id;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".settings-pane").forEach((pane) => {
    const on = pane.getAttribute("data-settings-pane") === id;
    pane.classList.toggle("hidden", !on);
    pane.classList.toggle("active", on);
  });
  // 测试连接只在 AI 页显示（模块本身仍由 data-module 门控）
  const aiTest = $("#aiTestBtn");
  if (aiTest) aiTest.classList.toggle("hidden", id !== "ai");
  if (id === "log") refreshLogView();
}

function _fmtLogTime(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  const pad = (n) => String(n).padStart(2, "0");
  return (
    pad(d.getHours()) +
    ":" +
    pad(d.getMinutes()) +
    ":" +
    pad(d.getSeconds())
  );
}

function renderLogEntries(entries) {
  const view = $("#logView");
  if (!view) return;
  const list = Array.isArray(entries) ? entries : [];
  if (!list.length) {
    view.textContent = t("logEmpty");
    return;
  }
  // 用 DOM 而不是 innerHTML 拼用户/异常文本，避免注入
  view.textContent = "";
  const frag = document.createDocumentFragment();
  for (const e of list) {
    const level = String((e && e.level) || "INFO").toUpperCase();
    const line = document.createElement("div");
    line.className =
      "log-line " +
      (level === "ERROR"
        ? "log-line-error"
        : level === "WARN"
          ? "log-line-warn"
          : level === "DEBUG"
            ? "log-line-debug"
            : "log-line-info");
    line.textContent =
      "[" +
      _fmtLogTime(e && e.ts) +
      "] " +
      level +
      ": " +
      ((e && e.message) || "");
    frag.appendChild(line);
    const tb = (e && e.traceback) || "";
    if (tb) {
      const pre = document.createElement("div");
      pre.className = "log-tb";
      pre.textContent = tb;
      frag.appendChild(pre);
    }
  }
  view.appendChild(frag);
  // 滚到底看最新
  view.scrollTop = view.scrollHeight;
}

async function refreshLogView() {
  const countEl = $("#logCountLabel");
  if (!state.api || !state.api.get_app_log) {
    if (countEl) countEl.textContent = t("logCount", 0);
    renderLogEntries([]);
    return;
  }
  let res;
  try {
    res = await state.api.get_app_log(1024);
  } catch (e) {
    toast(String(e), true);
    return;
  }
  if (res && res.error) {
    toast(res.error, true);
    return;
  }
  const entries = (res && res.entries) || [];
  const n = Number(res && res.count) || entries.length;
  if (countEl) countEl.textContent = t("logCount", n);
  renderLogEntries(entries);
}

async function clearAppLog() {
  if (!state.api || !state.api.clear_app_log) return;
  try {
    const res = await state.api.clear_app_log();
    if (res && res.error) {
      toast(res.error, true);
      return;
    }
    toast(t("logCleared"));
    await refreshLogView();
  } catch (e) {
    toast(String(e), true);
  }
}

async function exportAppLog() {
  if (!state.api || !state.api.export_app_log) return;
  const btn = $("#logExportBtn");
  if (btn) btn.disabled = true;
  try {
    const res = await state.api.export_app_log();
    if (res && res.cancelled) return;
    if (res && res.error) {
      toast(res.error || t("logExportFail"), true);
      return;
    }
    toast(t("logExported"));
    await refreshLogView();
  } catch (e) {
    toast(String(e), true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 打开卸载确认弹窗（勾选是否删数据后再执行，默认勾选）。 */
function openUninstallDialog() {
  const ov = $("#uninstallOverlay");
  if (!ov) return;
  const chk = $("#uninstallDeleteDataChk");
  if (chk) chk.checked = true;
  ov.classList.remove("hidden");
}

function closeUninstallDialog() {
  const ov = $("#uninstallOverlay");
  if (ov) ov.classList.add("hidden");
}

function openUninstallDoneDialog() {
  closeUninstallDialog();
  closeSettings();
  const ov = $("#uninstallDoneOverlay");
  if (ov) ov.classList.remove("hidden");
}

function closeUninstallDoneDialog() {
  const ov = $("#uninstallDoneOverlay");
  if (ov) ov.classList.add("hidden");
}

/**
 * 确认卸载：只清应用数据目录（WhoShitsOnMyC 整夹），不碰自定义快照路径。
 * 成功后弹完成框再退出；**不要**再 loadSnapshots / get_settings，否则会把目录建回来。
 */
async function confirmUninstallCleanup() {
  if (!state.api || !state.api.uninstall_app_data) return;
  const chk = $("#uninstallDeleteDataChk");
  const deleteData = chk ? !!chk.checked : true;
  const confirmBtn = $("#uninstallConfirmBtn");
  const openBtn = $("#uninstallBtn");
  if (confirmBtn) confirmBtn.disabled = true;
  if (openBtn) openBtn.disabled = true;
  try {
    const res = await state.api.uninstall_app_data(deleteData);
    if (res && res.error) {
      toast(res.error, true);
      return;
    }
    if (res && res.ok === false) {
      toast(t("uninstallPartial"), true);
    }
    // 侧栏清空即可；勿 list_snapshots（会 makedirs 重建 WhoShitsOnMyC）
    state.snapshots = [];
    try {
      renderSnapshotList();
    } catch (e) {}
    openUninstallDoneDialog();
  } catch (e) {
    toast(String(e), true);
  } finally {
    if (confirmBtn) confirmBtn.disabled = false;
    if (openBtn) openBtn.disabled = false;
  }
}

async function finishUninstallAndQuit() {
  const btn = $("#uninstallDoneOkBtn");
  if (btn) btn.disabled = true;
  try {
    if (state.api && state.api.quit_app) {
      await state.api.quit_app();
    }
  } catch (e) {
    toast(String(e), true);
    if (btn) btn.disabled = false;
  }
}

/** 最近一次检查更新结果（用于「打开发布页」）。 */
let _lastUpdateCheck = null;

function _setCheckUpdateStatus(text, kind) {
  const el = $("#checkUpdateStatus");
  if (!el) return;
  el.textContent = text || "";
  el.classList.remove("is-update", "is-error");
  if (kind === "update") el.classList.add("is-update");
  if (kind === "error") el.classList.add("is-error");
}

function _setOpenReleaseVisible(show) {
  const btn = $("#openReleaseBtn");
  if (!btn) return;
  if (show) btn.classList.remove("hidden");
  else btn.classList.add("hidden");
}

/** 在「检查更新」旁展示当前版本号（来自 get_settings）。 */
function fillAppVersionLabel(version) {
  const el = $("#appVersionLabel");
  if (!el) return;
  const v = (version || "").toString().trim();
  el.textContent = v ? t("checkUpdateVersion", v) : "";
}

/**
 * 查询 GitHub 最新 Release，更新状态行；有新版本时显示「打开发布页」。
 *
 * 比较结果三种：
 * - update：发布版更高 → 提示可升级
 * - latest：相同 → 已是最新
 * - ahead：本机更高（开发/未发版）→ 明确写「高于发布版」
 */
async function checkForUpdates() {
  if (!state.api || !state.api.check_for_updates) {
    _setCheckUpdateStatus(t("checkUpdateFail"), "error");
    return;
  }
  const btn = $("#checkUpdateBtn");
  if (btn) btn.disabled = true;
  _setCheckUpdateStatus(t("checkUpdateChecking"), null);
  _setOpenReleaseVisible(false);
  _lastUpdateCheck = null;
  try {
    const res = await state.api.check_for_updates();
    _lastUpdateCheck = res || null;
    if (!res) {
      _setCheckUpdateStatus(t("checkUpdateFail"), "error");
      return;
    }
    // 网络/HTTP 失败
    if (res.ok === false || (res.error && !res.latest)) {
      _setCheckUpdateStatus(res.error || t("checkUpdateFail"), "error");
      // 仍可尝试打开发布列表页
      if (res.release_url || res.html_url) _setOpenReleaseVisible(true);
      return;
    }
    const cur = res.current || "";
    const lat = res.latest || "";
    if (cur) fillAppVersionLabel(cur);
    const status = res.status || (res.update_available ? "update" : lat ? "latest" : "");
    if (status === "update") {
      _setCheckUpdateStatus(t("checkUpdateAvailable", cur, lat), "update");
      _setOpenReleaseVisible(true);
    } else if (status === "ahead") {
      _setCheckUpdateStatus(t("checkUpdateAhead", cur, lat), null);
      _setOpenReleaseVisible(true);
    } else if (status === "latest" || lat) {
      _setCheckUpdateStatus(t("checkUpdateUpToDate", cur, lat || cur), null);
      _setOpenReleaseVisible(false);
    } else if (res.error) {
      // 如 404 无 release：有提示但仍可打开 releases 页
      _setCheckUpdateStatus(res.error, "error");
      _setOpenReleaseVisible(true);
    } else {
      _setCheckUpdateStatus(t("checkUpdateVersion", cur), null);
    }
  } catch (e) {
    _setCheckUpdateStatus(String(e) || t("checkUpdateFail"), "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function openReleasePage() {
  const url =
    (_lastUpdateCheck && (_lastUpdateCheck.html_url || _lastUpdateCheck.release_url)) ||
    "https://github.com/Kami958/WhoShitsonMyC/releases";
  if (!state.api || !state.api.open_url) return;
  try {
    const res = await state.api.open_url(url);
    if (res && res.error) toast(res.error, true);
  } catch (e) {
    toast(String(e), true);
  }
}

async function openSettings(tabId) {
  // 注意：settingsBtn.onclick = openSettings 时首参是 click event，不能当 tabId
  const tab =
    typeof tabId === "string" && tabId.trim() ? tabId.trim() : "general";
  await loadSettings();
  if (typeof loadAiSettingsDraft === "function") {
    await loadAiSettingsDraft();
  }
  if (typeof applyModuleVisibility === "function") {
    applyModuleVisibility();
  }
  // 打开设置时重置检查更新状态（保留上次结果会误导）
  _lastUpdateCheck = null;
  _setCheckUpdateStatus("", null);
  _setOpenReleaseVisible(false);
  switchSettingsTab(tab);
  $("#settingsOverlay").classList.remove("hidden");
}

function closeSettings() {
  $("#settingsOverlay").classList.add("hidden");
  _settingsDraft = null;
  // 收起所有展开的说明
  document.querySelectorAll(".settings-help-panel").forEach((p) => {
    p.classList.add("hidden");
  });
  document.querySelectorAll(".settings-q.open").forEach((b) => {
    b.classList.remove("open");
  });
}

/** 设置项「?」：悬停靠 title；点击在该项下方展开/收起说明。 */
function toggleSettingsHelp(btn) {
  const key = btn && btn.getAttribute("data-help");
  if (!key) return;
  const panel = document.querySelector(`[data-help-panel="${key}"]`);
  if (!panel) return;
  const willOpen = panel.classList.contains("hidden");
  // 只保留一个展开
  document.querySelectorAll(".settings-help-panel").forEach((p) => {
    p.classList.add("hidden");
  });
  document.querySelectorAll(".settings-q.open").forEach((b) => {
    b.classList.remove("open");
  });
  if (willOpen) {
    panel.classList.remove("hidden");
    btn.classList.add("open");
  }
}

function showMigrateOverlay() {
  const ov = $("#migrateOverlay");
  if (!ov) return;
  const count = $("#migrateCount");
  const cur = $("#migrateCurrent");
  if (count) count.textContent = t("migrateProgress", 0, 0);
  if (cur) {
    cur.textContent = "";
    cur.title = "";
  }
  ov.classList.remove("hidden");
}

function hideMigrateOverlay() {
  const ov = $("#migrateOverlay");
  if (ov) ov.classList.add("hidden");
}

function updateMigrateProgress(payload) {
  const p = payload || {};
  const done = Number(p.done) || 0;
  const total = Number(p.total) || 0;
  const name = p.name || "";
  const count = $("#migrateCount");
  const cur = $("#migrateCurrent");
  if (count) {
    count.textContent = name
      ? t("migrateProgressName", done, total, name)
      : t("migrateProgress", done, total);
  }
  if (cur) {
    cur.textContent = name;
    cur.title = name;
  }
}

function toastMigrateDone(mig) {
  const moved = Number(mig && mig.moved) || 0;
  const skipped = Number(mig && mig.skipped) || 0;
  const failed = Number(mig && mig.failed) || 0;
  if (failed || skipped) {
    toast(t("snapDirMigratePartial", moved, skipped, failed), failed > 0);
  } else if (moved) {
    toast(t("snapDirMigrated", moved));
  } else {
    toast(t("snapDirMigrateNone"));
  }
}

/**
 * 等待后端 ``settings-applied`` 事件（apply_settings 已改为后台线程）。
 * 返回 ``{ promise, cancel }``；cancel 可在未启动成功时拆掉监听。
 */
function waitSettingsApplied(timeoutMs) {
  const ms = timeoutMs == null ? 120000 : timeoutMs;
  let settled = false;
  let finish = null;
  const prev = window.__onPyEvent;
  const promise = new Promise((resolve) => {
    const timer = setTimeout(() => {
      finish({ ok: false, error: t("settingsApplyTimeout") });
    }, ms);
    finish = (payload) => {
      if (settled) return;
      settled = true;
      window.__onPyEvent = prev;
      clearTimeout(timer);
      resolve(payload || {});
    };
    window.__onPyEvent = function (event, payload) {
      try {
        if (typeof prev === "function") prev(event, payload);
      } catch (e) {}
      if (event === "settings-applied") finish(payload);
    };
  });
  return {
    promise,
    cancel: (payload) => {
      if (finish) finish(payload || { ok: false, cancelled: true });
    },
  };
}

/**
 * 点「完成」：把草稿一次性提交给后端，再关面板。
 * 关闭（✕ / Esc）不调用本函数 → 草稿丢弃。
 * 目录变更时先关设置、显示迁移进度；后端在后台线程推送进度与终态。
 */
async function applySettingsAndClose() {
  if (!_settingsDraft) {
    closeSettings();
    return;
  }
  // 从表单再读一遍，防止漏绑 change
  const workerSel = $("#workerSel");
  const compressChk = $("#compressChk");
  const mftChk = $("#mftChk");
  const searchMemIdxChk = $("#searchMemIdxChk");
  const logSanitizeChk = $("#logSanitizeChk");
  const payload = {
    scan_workers: workerSel ? Number(workerSel.value) : _settingsDraft.scan_workers,
    compress_snapshots: compressChk ? !!compressChk.checked : _settingsDraft.compress_snapshots,
    use_mft: mftChk ? !!mftChk.checked : _settingsDraft.use_mft,
    search_memory_index: searchMemIdxChk
      ? !!searchMemIdxChk.checked
      : (_settingsDraft.search_memory_index !== false),
    log_sanitize: logSanitizeChk
      ? !!logSanitizeChk.checked
      : (_settingsDraft.log_sanitize !== false),
    snapshot_dir: _settingsDraft.snapshot_dir_is_custom
      ? (_settingsDraft.snapshot_dir || "")
      : "",
    delete_blacklist: Array.isArray(_settingsDraft.delete_blacklist)
      ? _settingsDraft.delete_blacklist
      : [],
  };

  // 是否可能触发迁移：草稿目录与当前生效目录不同
  const prevDir =
    (state._settings && state._settings.snapshot_dir) || "";
  const draftDir = _settingsDraft.snapshot_dir_is_custom
    ? (_settingsDraft.snapshot_dir_display || _settingsDraft.snapshot_dir || "")
    : (_settingsDraft.snapshot_dir_builtin || "");
  const maybeMigrate =
    draftDir &&
    prevDir &&
    draftDir.replace(/[\\/]+$/, "").toLowerCase() !==
      prevDir.replace(/[\\/]+$/, "").toLowerCase();

  const doneBtn = $("#settingsDoneBtn");
  if (doneBtn) doneBtn.disabled = true;

  // AI 配置单独提交，不进通用 apply_settings payload
  if (typeof applyAiSettingsDraft === "function") {
    try {
      const aiRes = await applyAiSettingsDraft();
      if (aiRes && aiRes.error) {
        if (doneBtn) doneBtn.disabled = false;
        toast(aiRes.error, true);
        return;
      }
    } catch (e) {
      if (doneBtn) doneBtn.disabled = false;
      toast(String(e), true);
      return;
    }
  }

  // 先关设置页，避免阻塞感；需要迁移时立刻出进度遮罩
  closeSettings();
  if (maybeMigrate) showMigrateOverlay();

  // 先挂监听再 kick，避免极快完成时丢事件
  const waiter = waitSettingsApplied();
  let kick;
  try {
    kick = await state.api.apply_settings(payload);
  } catch (e) {
    waiter.cancel();
    hideMigrateOverlay();
    if (doneBtn) doneBtn.disabled = false;
    toast(String(e), true);
    return;
  }
  if (kick && kick.error) {
    waiter.cancel();
    hideMigrateOverlay();
    if (doneBtn) doneBtn.disabled = false;
    toast(kick.error, true);
    return;
  }
  if (!(kick && kick.started)) {
    // 旧同步实现兼容：kick 本身就是结果
    waiter.cancel();
    hideMigrateOverlay();
    if (doneBtn) doneBtn.disabled = false;
    state._settings = Object.assign(state._settings || {}, kick || {});
    if (Object.prototype.hasOwnProperty.call(payload, "search_memory_index")) {
      state.searchMemoryIndex = !!payload.search_memory_index;
    } else if (kick && Object.prototype.hasOwnProperty.call(kick, "search_memory_index")) {
      state.searchMemoryIndex = kick.search_memory_index !== false;
    }
    toast(t("settingsApplied"));
    if (payload.use_mft && state._settings && !state._settings.is_admin) {
      toast(t("mftNeedAdmin"));
    }
    if (kick && kick.snapshot_dir_changed) {
      toastMigrateDone(kick.migrate || {});
      loadSnapshots();
    }
    return;
  }

  const res = await waiter.promise;
  if (doneBtn) doneBtn.disabled = false;
  hideMigrateOverlay();

  if (!res || res.cancelled) return;
  if (res.ok === false || res.error) {
    toast(res.error || t("settingsApplyFailed"), true);
    return;
  }

  state._settings = Object.assign(state._settings || {}, res || {});
  if (Object.prototype.hasOwnProperty.call(payload, "search_memory_index")) {
    state.searchMemoryIndex = !!payload.search_memory_index;
  } else if (Object.prototype.hasOwnProperty.call(res || {}, "search_memory_index")) {
    state.searchMemoryIndex = res.search_memory_index !== false;
  }
  const dirChanged = !!(res && res.snapshot_dir_changed);
  const mig = (res && res.migrate) || {};

  toast(t("settingsApplied"));
  if (payload.use_mft && state._settings && !state._settings.is_admin) {
    toast(t("mftNeedAdmin"));
  }
  if (dirChanged) {
    toastMigrateDone(mig);
    loadSnapshots();
  }
}

/**
 * 恢复默认设置：二次确认后删 settings.yaml，内存与 UI 回内置默认。
 * 不删除快照文件；语言回到系统冷启动判定。
 */
async function resetSettingsToDefaults() {
  const ok = await showConfirmDialog({
    title: t("resetSettings"),
    message: t("resetSettingsConfirm"),
    okText: t("resetSettingsBtn"),
    danger: true,
  });
  if (!ok) return;
  let res;
  try {
    res = await state.api.reset_settings();
  } catch (e) {
    toast(String(e), true);
    return;
  }
  if (!res || res.error) {
    toast((res && res.error) || t("resetSettingsFailed"), true);
    return;
  }
  state._settings = Object.assign(state._settings || {}, res || {});
  state.searchMemoryIndex = res.search_memory_index !== false;
  // AI 配置已由后端 reset 清掉；刷新设置页草稿
  if (typeof loadAiSettingsDraft === "function") {
    try {
      await loadAiSettingsDraft();
    } catch (e) {}
  }
  // 主题 / 语言与后端对齐
  if (res.theme === "dark" || res.theme === "light") {
    applyThemeValue(res.theme);
  }
  const lang = res.lang === "zh" || res.lang === "en" ? res.lang : "en";
  // setLang 会 syncBackend；后端已是默认，值相同不写盘
  if (lang !== LANG) setLang(lang);
  else {
    applyStaticI18n();
    applyThemeButton(true);
  }
  // 刷新设置草稿（若面板仍开着）
  _settingsDraft = {
    scan_workers: Number(res.scan_workers) || 1,
    compress_snapshots: !!res.compress_snapshots,
    use_mft: !!res.use_mft,
    search_memory_index: res.search_memory_index !== false,
    log_sanitize: res.log_sanitize !== false,
    snapshot_dir: "",
    snapshot_dir_display: res.snapshot_dir || res.snapshot_dir_builtin || "",
    snapshot_dir_builtin: res.snapshot_dir_builtin || "",
    snapshot_dir_is_custom: false,
    settings_path: res.settings_path || "",
    mft_platform_ok: res.mft_platform_ok !== false,
    is_admin: !!res.is_admin,
    cpu_count: res.cpu_count,
  };
  fillSettingsFormFromDraft();
  toast(t("resetSettingsDone"));
}

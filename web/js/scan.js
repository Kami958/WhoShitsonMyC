/* 扫描流程与 Python 事件 */
"use strict";

// ---- 扫描流程 ----

async function newScan() {
  const res = await state.api.choose_folder();
  if (!res.path) return;
  // 尽量用最新 is_admin / use_mft，避免启动后权限状态陈旧
  try {
    const s = await state.api.get_settings();
    if (s) state._settings = Object.assign(state._settings || {}, s);
  } catch (_) {
    /* 仍用缓存 */
  }
  showScanOverlay(res.path);
  const mftOn = !!(state._settings && state._settings.use_mft);
  const isAdmin = !!(state._settings && state._settings.is_admin);
  if (mftOn && isDriveRootPath(res.path) && !isAdmin) {
    toast(t("scanMftFallbackNonAdmin"));
  }
  const started = await state.api.start_scan(res.path);
  if (started.error) {
    hideScanOverlay();
    toast(started.error, true);
  }
}

/** 扫描墙钟：仅 UI 用，非开发分段计时。 */
let _scanTimer = null;
let _scanT0 = 0;
/**
 * 进度 UI 合并：Python 桥只写缓冲，rAF 再刷 DOM。
 * 文件数跟帧刷新；路径单独 ~280ms 节流，避免长路径 split/排版卡顿。
 */
let _scanProg = {
  files: 0,
  current: "",
  dirty: false,
  raf: 0,
  lastPath: "",
  pathTimer: 0,
};
const _SCAN_PATH_MIN_MS = 280;
let _scanPathLastMs = 0;

function formatElapsed(sec) {
  sec = Math.max(0, Math.floor(sec));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    const mm = m % 60;
    return `${h}:${String(mm).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * 长路径显示：优先保留尾部（文件/末级目录名），前面用 … 省略。
 * 例如 …\\Users\\name\\file.md，而不是只露出中间一段。
 */
function formatPathKeepEnd(path, maxLen) {
  const s = String(path || "");
  const limit = maxLen || 52;
  if (s.length <= limit) return s;
  const sep = s.indexOf("\\") >= 0 ? "\\" : "/";
  const parts = s.split(/[/\\]+/).filter((p) => p !== "");
  if (parts.length === 0) return s.slice(0, limit - 1) + "…";
  // 从末段往前拼，直到放不下
  let tail = parts[parts.length - 1];
  for (let i = parts.length - 2; i >= 0; i--) {
    const next = parts[i] + sep + tail;
    // 预留 "…" + sep
    if (next.length + 2 > limit) break;
    tail = next;
  }
  // 末段本身就超长：保留末尾字符
  if (tail.length + 1 > limit) {
    return "…" + tail.slice(-(limit - 1));
  }
  return "…" + sep + tail;
}

function applyScanPath(path, force) {
  const el = $("#scanCurrent");
  if (!el) return;
  const full = String(path || "");
  if (!force && full === _scanProg.lastPath) return;
  const now = performance.now();
  if (!force && now - _scanPathLastMs < _SCAN_PATH_MIN_MS) {
    // 路径变化太快：悬停仍看最新，正文延后刷新
    el.title = full;
    if (!_scanProg.pathTimer) {
      const wait = Math.max(16, _SCAN_PATH_MIN_MS - (now - _scanPathLastMs));
      _scanProg.pathTimer = setTimeout(() => {
        _scanProg.pathTimer = 0;
        applyScanPath(_scanProg.current, false);
      }, wait);
    }
    return;
  }
  _scanPathLastMs = now;
  _scanProg.lastPath = full;
  el.title = full;
  el.textContent = formatPathKeepEnd(full, 52);
}

function flushScanProgress(forcePath) {
  _scanProg.raf = 0;
  if (!_scanProg.dirty && !forcePath) return;
  _scanProg.dirty = false;
  const filesEl = $("#scanFiles");
  if (filesEl) {
    filesEl.textContent = t(
      "scannedFiles",
      Number(_scanProg.files || 0).toLocaleString()
    );
  }
  applyScanPath(_scanProg.current, !!forcePath);
}

function queueScanProgress(files, current) {
  if (files != null && files !== "") _scanProg.files = files;
  if (current != null) _scanProg.current = current;
  _scanProg.dirty = true;
  if (!_scanProg.raf) {
    _scanProg.raf = requestAnimationFrame(() => flushScanProgress(false));
  }
}

function tickScanElapsed() {
  const el = $("#scanElapsed");
  if (!el || !_scanT0) return;
  const sec = (performance.now() - _scanT0) / 1000;
  el.textContent = t("scanElapsed", formatElapsed(sec));
}

/** 是否像 Windows 盘符根（C:\\ / C:/），用于扫描遮罩常驻 MFT 提示。 */
function isDriveRootPath(path) {
  const s = String(path || "").trim().replace(/\//g, "\\");
  return /^[A-Za-z]:\\?$/.test(s);
}

/**
 * 扫描遮罩模式提示。
 * @param {false|"mft"|"fallback"} mode
 *   false 隐藏；mft 将读 MFT；fallback 非管理员回退常规扫描
 */
function setScanModeHint(mode) {
  const el = $("#scanModeHint");
  if (!el) return;
  if (mode === "mft") {
    el.textContent = t("scanMftModeHint");
    el.classList.remove("hidden");
  } else if (mode === "fallback") {
    el.textContent = t("scanMftFallbackNonAdmin");
    el.classList.remove("hidden");
  } else {
    el.textContent = "";
    el.classList.add("hidden");
  }
}

function showScanOverlay(root) {
  if (_scanProg.pathTimer) {
    clearTimeout(_scanProg.pathTimer);
  }
  if (_scanProg.raf) {
    cancelAnimationFrame(_scanProg.raf);
  }
  _scanProg = {
    files: 0,
    current: root || "",
    dirty: false,
    raf: 0,
    lastPath: "",
    pathTimer: 0,
  };
  _scanPathLastMs = 0;
  $("#scanFiles").textContent = t("scannedFiles", 0);
  $("#scanElapsed").textContent = t("scanElapsedInit");
  // 根盘符 + 设置开了 MFT：常驻说明；非管理员时明确写回退（勾选可保留）
  const mftOn = !!(state._settings && state._settings.use_mft);
  const isRoot = isDriveRootPath(root);
  const isAdmin = !!(state._settings && state._settings.is_admin);
  if (mftOn && isRoot) {
    setScanModeHint(isAdmin ? "mft" : "fallback");
  } else {
    setScanModeHint(false);
  }
  applyScanPath(root, true);
  _scanT0 = performance.now();
  if (_scanTimer) clearInterval(_scanTimer);
  _scanTimer = setInterval(tickScanElapsed, 250);
  tickScanElapsed();
  $("#scanOverlay").classList.remove("hidden");
}
function hideScanOverlay() {
  $("#scanOverlay").classList.add("hidden");
  setScanModeHint(false);
  if (_scanTimer) {
    clearInterval(_scanTimer);
    _scanTimer = null;
  }
  _scanT0 = 0;
  if (_scanProg.raf) {
    cancelAnimationFrame(_scanProg.raf);
    _scanProg.raf = 0;
  }
  if (_scanProg.pathTimer) {
    clearTimeout(_scanProg.pathTimer);
    _scanProg.pathTimer = 0;
  }
  _scanProg.dirty = false;
}

/** 处理来自 Python 的事件推送。 */
function onPyEvent(event, payload) {
  switch (event) {
    case "scan-progress":
      // 只入队；DOM 在 rAF 合并刷新，避免 evaluate_js 风暴卡 UI
      queueScanProgress(payload.files, payload.current);
      break;
    case "scan-done":
      flushScanProgress(true);
      hideScanOverlay();
      {
        const elapsedTxt = fmtElapsed(payload && payload.elapsed_s);
        if (payload.warning) toast(payload.warning, true);
        else if (elapsedTxt) toast(t("scanDoneWithTime", elapsedTxt));
        else toast(t("scanDone"));
      }
      loadSnapshots().then(() => {
        const path = payload.snapshot && payload.snapshot.path;
        // 新快照自动选作「当前」，方便紧接着对比。
        if (path) selectSnapshot("new", path);
        // 侧栏对应项缓慢闪烁约 3 秒，提示刚生成的记录
        if (path) flashSnapshot(path, 3000);
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
    case "migrate-progress": {
      const ov = $("#migrateOverlay");
      if (ov && ov.classList.contains("hidden")) showMigrateOverlay();
      updateMigrateProgress(payload);
      break;
    }
    case "migrate-done":
      // 遮罩与完成 toast 由 applySettingsAndClose / settings-applied 统一处理
      break;
    case "settings-applied":
      // Promise 等待方在 applySettingsAndClose 里处理
      break;
    case "search-preheat":
      // 打开搜索框时内存索引预热进度（started / ready / failed）
      if (typeof onSearchPreheatEvent === "function") onSearchPreheatEvent(payload);
      break;
    case "ai-chunk":
    case "ai-done":
    case "ai-error":
    case "ai-tool-propose":
      if (typeof onAiPyEvent === "function") onAiPyEvent(event, payload);
      break;
  }
}
window.__onPyEvent = onPyEvent;

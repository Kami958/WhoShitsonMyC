/* 设置 / 日志 / 卸载 / 迁移 */
"use strict";

// ---- 设置（设置页 / 扫描线程 / 压缩 / MFT / YAML 持久化）----

async function openSnapshotDir() {
  const res = await state.api.open_snapshot_dir();
  if (res.error) toast(res.error, true);
}

async function openDataFolder() {
  const path = (_settingsDraft && _settingsDraft.app_data_dir) || "";
  if (!path || !state.api) return;
  let res = null;
  try {
    res = await state.api.open_folder(path);
  } catch (e) {
    res = null;
  }
  if (!res || res.error) toast(t("dataFolderOpenFailed"), true);
}

/** 设置页更改数据文件夹：把当前数据移到新位置并继续使用。 */
async function chooseDataFolder() {
  let res;
  try {
    res = await state.api.choose_data_folder();
  } catch (e) {
    toast(t("folderPickFailed"), true);
    return;
  }
  if (!res || res.cancelled) return;
  if (res.error) {
    toast(res.error, true);
    return;
  }
  const failed = Number(res.failed) || 0;
  const skipped = Number(res.skipped) || 0;
  const moved = Number(res.moved) || 0;
  const previous = res.previous_root || "";
  const parts = [moved ? t("dataFolderChangedMoved") : t("dataFolderChanged")];
  if (failed) parts.push(t("storageRecoveryMoveFailed", failed, previous));
  if (skipped) parts.push(t("storageRecoveryMoveSkipped", skipped, previous));
  if (res.snapshot_dir_reset) parts.push(t("storageRecoverySnapshotReset"));
  if (res.pointer_saved === false) parts.push(t("storageRecoveryPointerFailed"));
  toast(parts.join(" "), res.pointer_saved === false ? "error" : failed ? "warn" : "info");
  await refreshAfterStorageChange();
}

/**
 * 设置页草稿：打开时从后端拷贝，控件只改草稿；
 * 点「完成」才 apply_settings 一次写入。关闭/取消丢弃草稿。
 */
let _settingsDraft = null;

/** 打开设置页时的表单快照，用来判断关掉时是否还有改动没保存。 */
let _settingsFormSnapshot = "";

/** 用户本次打开设置页后是否真的动过控件；程序写值不算改动。 */
let _settingsTouched = false;

/** 读取设置并填充设置页控件（仅草稿，不写后端）。 */
async function loadSettings() {
  let s;
  try {
    s = await state.api.get_settings();
  } catch (e) {
    return;
  }
  if (!s || s.error) {
    _settingsDraft = null;
    return;
  }
  state._settings = s;
  state.searchMemoryIndex = s.search_memory_index !== false;
  applyToastTiming(s);
  _settingsDraft = {
    scan_workers: Number(s.scan_workers) || 1,
    mft_procs: Number(s.mft_procs) || 0,
    // 各盘上一次的实测结果，打开设置页时用来还原曲线
    perf_tests: s.perf_tests && typeof s.perf_tests === "object" ? s.perf_tests : {},
    compress_snapshots: !!s.compress_snapshots,
    use_mft: !!s.use_mft,
    search_memory_index: s.search_memory_index !== false,
    temp_cleanup: s.temp_cleanup !== false,
    auto_check_updates: s.auto_check_updates !== false,
    update_badge: s.update_badge !== false,
    remember_window_size: s.remember_window_size !== false,
    log_sanitize: s.log_sanitize !== false,
    log_level: _normalizeLogLevel(s.log_level),
    dev_tools: !!s.dev_tools,
    // 空串 = 内置目录；非空 = 自定义绝对路径
    snapshot_dir: s.snapshot_dir_is_custom ? (s.snapshot_dir_configured || s.snapshot_dir || "") : "",
    snapshot_dir_display: s.snapshot_dir || "",
    snapshot_dir_builtin: s.snapshot_dir_builtin || "",
    snapshot_dir_is_custom: !!s.snapshot_dir_is_custom,
    app_data_dir: s.app_data_dir || "",
    account: s.account || "",
    data_dir_pointer_invalid: !!s.data_dir_pointer_invalid,
    settings_path: s.settings_path || "",
    mft_platform_ok: s.mft_platform_ok !== false,
    is_admin: !!s.is_admin,
    cpu_count: s.cpu_count,
    delete_blacklist: _normalizeBlacklistDraft(s.delete_blacklist),
    theme: _normalizeUiTheme(s.theme),
    size_source: _normalizeSizeSource(s.size_source),
    ui_parent_row_bg: s.ui_parent_row_bg === true,
    ui_parent_row_dim: s.ui_parent_row_dim === true,
    ui_hover_group_outline: s.ui_hover_group_outline === true,
    ui_hover_group_min: _normalizeHoverGroupMin(s.ui_hover_group_min),
    ui_tree_guide: s.ui_tree_guide !== false,
    ui_parent_sep: s.ui_parent_sep === true,
    ui_toast_info_base: _normalizeToastSeconds(s.ui_toast_info_base, 2),
    ui_toast_info_max: _normalizeToastSeconds(s.ui_toast_info_max, 20),
    ui_toast_warn_base: _normalizeToastSeconds(s.ui_toast_warn_base, 5),
    ui_toast_warn_max: _normalizeToastSeconds(s.ui_toast_warn_max, 30),
  };
  fillAppVersionLabel(s.version || "");
  fillSettingsFormFromDraft();
  updateSettingsWarnings(s);
}

/** 设置文件恢复与快照目录不可用的提示行；只显示后端给出的可读文案。 */
function updateSettingsWarnings(s) {
  const fileEl = $("#settingsFileWarnLine");
  if (fileEl && s) {
    const text = s.settings_warning ? String(s.settings_warning) : "";
    fileEl.textContent = text;
    fileEl.classList.toggle("hidden", !text);
  }
  const snapEl = $("#snapDirWarnLine");
  if (snapEl) {
    const text = s && s.snapshot_dir_warning ? String(s.snapshot_dir_warning) : "";
    snapEl.textContent = text;
    snapEl.classList.toggle("hidden", !text);
  }
}

function _normalizeLogLevel(raw) {
  const s = String(raw || "").trim().toUpperCase();
  if (s === "WARNING") return "WARN";
  if (s === "DEBUG" || s === "INFO" || s === "WARN" || s === "ERROR") return s;
  return "INFO";
}

/** 主题偏好：light|dark|auto；非法值回落 auto。 */
function _normalizeUiTheme(raw) {
  const v = String(raw || "").trim().toLowerCase();
  return v === "light" || v === "dark" ? v : "auto";
}

/** 文件大小来源：actual（实际占用，默认）| logical（逻辑占用）。 */
function _normalizeSizeSource(raw) {
  return String(raw || "").trim().toLowerCase() === "logical" ? "logical" : "actual";
}

/** 悬停分组框最少行数：0-50 收拢，非法回落 2。 */
function _normalizeHoverGroupMin(raw) {
  const n = Math.round(Number(raw));
  if (!Number.isFinite(n)) return 2;
  return Math.max(0, Math.min(50, n));
}

/** 提示停留秒数：1-120 收拢，非法回落默认值。 */
function _normalizeToastSeconds(raw, fallback) {
  const n = Math.round(Number(raw));
  if (!Number.isFinite(n)) return fallback;
  return Math.max(1, Math.min(120, n));
}

/** 把 UI 设置开关落到 <html>：关闭项加 class 供 CSS 反向覆盖。 */
function applyUiSettingFlags(s) {
  const root = document.documentElement;
  root.classList.toggle("ui-pbg-off", !!(s && s.ui_parent_row_bg === false));
  root.classList.toggle("ui-pdim-off", !!(s && s.ui_parent_row_dim === false));
  root.classList.toggle("ui-guide-on", !!(s && s.ui_tree_guide === true));
  root.classList.toggle("ui-psep-on", !!(s && s.ui_parent_sep === true));
}

/** 文件大小来源切换：只记到草稿，等点完成保存后才生效。
 *
 *  不能在这里顺手改当前设置：保存收尾要靠「保存前」与「保存后」两个值
 *  判断要不要重画，提前改掉就永远相等，重画永远不会发生。
 */
function onSizeSourceChange() {
  const el = document.querySelector('input[name="sizeSource"]:checked');
  const val = _normalizeSizeSource(el && el.value);
  if (_settingsDraft) _settingsDraft.size_source = val;
}

/** 设置页「界面」页签：主题选择即时生效（light/dark/auto）。 */
async function onUiThemeChange() {
  const sel = $("#uiThemeSel");
  if (!sel) return;
  const pref = _normalizeUiTheme(sel.value);
  if (state._settings) state._settings.theme = pref;
  if (_settingsDraft) _settingsDraft.theme = pref;
  applyThemeValue(pref);
  applyThemeButton(true);
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
    toastWarn(t("blacklistEmpty"));
    return;
  }
  if (mode === "regex") {
    try {
      // eslint-disable-next-line no-new
      new RegExp(path);
    } catch (e) {
      toastWarn(t("blacklistBadRegex"));
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

/* ---- 性能参数：按盘保存的读取线程、处理进程数与本机测试 --------------- */

// 当前选中的盘，空串表示还没从后端拿到盘列表
let _perfDrive = "";
// 后端返回的固定磁盘，每项带该盘已保存的三项参数，0 表示跟随默认
let _perfDrives = [];
// 当前曲线看的是哪条路径：procs 是读 MFT 那条，threads 是目录遍历那条
let _perfMode = "procs";
// 每个盘的测试结果，键是盘符，值是 {kind, read_s, single_parse_s, points, timedOut, tested, suggest}
let _perfData = {};
// 每个盘输入框里正在编辑的值，切盘时用来保存与恢复
let _perfInputs = {};
// 打开设置页时每个盘呈现的三项值，用来判断哪一项真正改过
let _perfBaseline = {};
// 盘列表是否已经拉回来并建好基线，没就绪时不提交按盘参数
let _perfReady = false;
// 本轮测试是否已被前端判成放弃，迟到的事件不再回写界面
let _perfAbandoned = false;
// 测试的兜底定时器句柄，收到终态事件才清，见 _perfClearGuard
let _perfGuard = null;
// 打开设置页时测试结果的初始快照，用来判断本次有没有新测出东西
let _perfTestsBase = "{}";
// 被点了「恢复默认」的盘：提交时这三项发 0，表示回到跟随默认
let _perfResetFlags = {};
let _perfBusy = false;
// 本轮测试是否还没收到终态事件，用来挡住「事件比接口返回先到」的时序
let _perfPending = false;
let _perfEventsBound = false;
// 内存一律用测试实测到的每进程占用，没测到就不显示内存，
// 不拿常量乘进程数去估
function _perfPerProc(data) {
  const v = Number(data && data.per_proc_mb) || 0;
  return v > 0 ? v : 0;
}
// 上限取本机逻辑核数：进程数超过核数只会多占内存，没有速度收益
const MFT_PROCS_MAX = Math.max(2, navigator.hardwareConcurrency || 8);
// 目录遍历线程的滑杆上限，超过这个数收益已经很小，再高只是难调
const PERF_DIR_MAX = 8;
// 目录遍历线程的定位上限。要跟着后端的合法范围走（那里允许到 128），
// 不能比全局默认值小，否则高核数机器上打开设置页保存一次就会被夹到更小的值。
const PERF_DIR_CAP = 128;

function _perfDemoOf(letter) {
  const group = _perfData[letter];
  const d = group && group[_perfMode];
  return d && d.tested ? d : null;
}

function _perfProcsOf(letter) {
  const item = _perfDrives.find((x) => x.letter === letter);
  return item || null;
}

// 某个盘当前生效的三项值：没单独设过的项用后端给的生效值，
// 不在这里写死默认数，否则界面显示的默认跟实际扫描用的不是一回事。
function _perfValueOf(letter) {
  const item = _perfProcsOf(letter);
  const eff = (item && item.effective) || {};
  // 编辑缓存优先：点过恢复默认之后又改过某一项时，改动存在缓存里，
  // 若让恢复标记排在前面，切走再切回会用默认值把刚改的覆盖掉
  const saved = _perfInputs[letter];
  if (saved) return saved;
  // 点过恢复默认且没再动过：输入框显示恢复之后会用的值，那是全局默认
  if (_perfResetFlags[letter]) return _perfGlobalDefaults();
  const def = _perfGlobalDefaults();
  const mft = (item && item.mft_read) || eff.mft || def.mft;
  const dir = (item && item.dir_threads) || eff.dir || def.dir;
  // 生效值里的进程数为 0 表示交给解析侧按核数推导，用自动值显示
  const procs = (item && item.mft_procs) || eff.procs || def.procs;
  return { mft, dir, procs };
}

// 自动档实际会用的进程数：全局设过就用全局值，否则按核数减一
function _perfAutoProcs() {
  const s = state._settings || {};
  const global = Number(s.mft_procs) || 0;
  if (global > 0) return Math.max(1, Math.min(MFT_PROCS_MAX, global));
  const auto = Number(s.mft_procs_auto) || 0;
  if (auto > 0) return Math.max(1, Math.min(MFT_PROCS_MAX, auto));
  return Math.max(1, MFT_PROCS_MAX - 1);
}

// 全局默认：恢复默认后一块盘会回到的数值
function _perfGlobalDefaults() {
  const s = state._settings || {};
  return {
    mft: 1,
    dir: Math.max(1, Math.min(PERF_DIR_CAP, Number(s.scan_workers) || 4)),
    procs: _perfAutoProcs(),
  };
}

// 把当前输入框的值收进缓存，切盘或保存前调用。
// 越界值夹回来之后同时写回输入框，否则界面还写着 20、实际提交的是 16。
function _perfStashInputs() {
  if (!_perfDrive) return;
  const mft = $("#perfMft");
  const dir = $("#perfDir");
  const procs = $("#perfProcs");
  const take = (el, lo, hi, fallback) => {
    if (!el) return fallback;
    // 取整：输入框键入 1.5 时后端会把整条盘记录当成非法跳过，界面上却提示已应用
    const raw = Math.round(Number(el.value)) || fallback;
    const n = Math.max(lo, Math.min(hi, raw));
    if (String(n) !== String(el.value)) el.value = String(n);
    return n;
  };
  _perfInputs[_perfDrive] = {
    mft: take(mft, 1, 8, 1),
    dir: take(dir, 1, PERF_DIR_CAP, 4),
    procs: take(procs, 1, MFT_PROCS_MAX, 1),
  };
}

// 拼成 ``C|1|4|11;D|0|0|8``，后端按同样格式解析后合并进已有参数。
// 只提交真的动过的盘：后端是合并语义，没提交的盘保持已存值，
// 这样解锁前的加密卷、临时离线的盘不会因为这次没出现在列表里就被抹掉。
// 未就绪时返回 null，表示这次不下发按盘参数。
function _perfDrivesPayload() {
  _perfStashInputs();
  if (!_perfReady || !_perfDrives.length) return null;
  const parts = [];
  _perfDrives
    .map((item) => item && item.letter)
    .filter(Boolean)
    .sort()
    .forEach((letter) => {
      const saved = _perfProcsOf(letter) || {};
      const v = _perfInputs[letter];
      if (_perfResetFlags[letter]) {
        // 被点了恢复默认的盘：仍等于默认值的项继续跟随默认（发 0），
        // 之后又动过的那几项发具体值，不必把三项一起钉死。
        const def = _perfGlobalDefaults();
        const keep = (key) => {
          const cur = v && Number(v[key]);
          return cur && cur !== def[key] ? cur : 0;
        };
        parts.push([letter, keep("mft"), keep("dir"), keep("procs")].join("|"));
        return;
      }
      const base = _perfBaseline[letter];
      if (!v || !base) return;
      // 这一块盘一项都没改就不用提交，交给后端保留原值
      if (base.mft === v.mft && base.dir === v.dir && base.procs === v.procs) {
        return;
      }
      const pick = (key, stored) => {
        const old = Number(stored) || 0;
        return base[key] === v[key] ? old : v[key];
      };
      parts.push(
        [
          letter,
          pick("mft", saved.mft_read),
          pick("dir", saved.dir_threads),
          pick("procs", saved.mft_procs),
        ].join("|")
      );
    });
  return parts.join(";");
}

// 打开设置页时记下每个盘看到的呈现值，提交时用来判断某一项是否被改过
function _perfSnapshotBaseline() {
  _perfBaseline = {};
  _perfDrives.forEach((item) => {
    if (item && item.letter) {
      _perfBaseline[item.letter] = { ..._perfValueOf(item.letter) };
    }
  });
}

function _perfColors() {
  const cs = getComputedStyle(document.documentElement);
  return {
    accent: cs.getPropertyValue("--accent").trim() || "#1f6feb",
    muted: cs.getPropertyValue("--text-muted").trim() || "#737d89",
    border: cs.getPropertyValue("--border").trim() || "#d4d9df",
    text: cs.getPropertyValue("--text-dim").trim() || "#444c56",
    warn: cs.getPropertyValue("--warn").trim() || "#9a6700",
    // 推荐标记用绿色；主题里没有专门的绿色语义变量，用缩小方向的绿
    good: cs.getPropertyValue("--shrink").trim() || "#1a7f37",
  };
}

// 横轴刻度：数大时抽稀，最多六个
function _perfTicks(max) {
  const out = [1];
  if (max <= 1) return out;
  const step = Math.max(1, Math.ceil((max - 1) / 5));
  for (let v = 1 + step; v < max; v += step) out.push(v);
  if (out[out.length - 1] !== max) out.push(max);
  return out;
}

function _perfAxisMax() {
  return _perfMode === "procs" ? MFT_PROCS_MAX : PERF_DIR_MAX;
}

// 目录遍历是实测点，中间按分段直线插值，超出末点就取末点
function _perfDirCost(points, n) {
  if (!points || !points.length) return null;
  if (n <= points[0][0]) return points[0][1];
  for (let i = 1; i < points.length; i += 1) {
    const [x1, y1] = points[i];
    if (n <= x1) {
      const [x0, y0] = points[i - 1];
      if (x1 === x0) return y1;
      return y0 + ((y1 - y0) * (n - x0)) / (x1 - x0);
    }
  }
  return points[points.length - 1][1];
}

// 内存：目录遍历的线程几乎不占内存，实测一直加到十二个线程也只多八兆
function _perfMemAt(n, perProc) {
  if (!(perProc > 0)) return null;
  return Math.round(n * perProc);
}

// 处理进程数模式：时间线与内存线在图上高度相同时的进程数
function _perfBestPoint(data) {
  if (!data) return 0;
  const single = Number(data.single_parse_s) || 0;
  const read = Number(data.read_s) || 0;
  if (!(single > 0)) return 0;
  const tSpan = Math.max(single, read);
  if (!(tSpan > 0) || MFT_PROCS_MAX < 2) return 0;
  const perProc = _perfPerProc(data);
  // 没采到内存时没有内存线可比，取解析耗时降到读取耗时时的进程数
  if (!(perProc > 0)) {
    return Math.max(1, Math.min(MFT_PROCS_MAX, Math.round(single / Math.max(read, 1e-6))));
  }
  const memMax = Math.max(50, Math.ceil((MFT_PROCS_MAX * perProc) / 50) * 50);
  // 时间轴用的是图上那条解析曲线本身（single / n），不是整次扫描时间。
  // 整次扫描要取读取与解析里较慢的那段，解析降到读取以下后就变得恒定，
  // 拿它去和内存线求交点会把交点推到右侧，圈就落在曲线之外了。
  const gap = (n) => single / n / tSpan - (n * perProc) / memMax;
  if (gap(1) <= 0) return 1;
  if (gap(MFT_PROCS_MAX) >= 0) return MFT_PROCS_MAX;
  let lo = 1;
  let hi = MFT_PROCS_MAX;
  for (let i = 0; i < 40; i += 1) {
    const mid = (lo + hi) / 2;
    if (gap(mid) > 0) lo = mid;
    else hi = mid;
  }
  return (lo + hi) / 2;
}

// 目录遍历模式：取耗时把总降幅走完大半的最小档位。
// 直接取最低点会把线程数推到八，可图上的线早在第二档就压平了，
// 再加线程只多占内存，因此这里找的是「再往上几乎没有改善」的那个点。
function _perfThreadSuggest(points) {
  const list = (points || [])
    .filter((p) => Number(p[0]) > 0 && Number(p[1]) > 0)
    .slice()
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  if (!list.length) return 0;
  const one = Number(list[0][1]);
  let low = one;
  list.forEach((p) => {
    if (Number(p[1]) < low) low = Number(p[1]);
  });
  const total = one - low;
  if (!(total > 0)) return Number(list[0][0]);
  for (let i = 0; i < list.length; i += 1) {
    if (one - Number(list[i][1]) >= total * 0.85) return Number(list[i][0]);
  }
  return Number(list[list.length - 1][0]);
}

// 目录遍历各档的内存实测点，取不到就是空
function _perfThreadMem(data) {
  const mem = data && data.mem;
  if (!mem || !mem.length) return null;
  return mem
    .filter((p) => Number(p[0]) > 0 && Number(p[1]) >= 0)
    .map((p) => [Number(p[0]), Number(p[1])]);
}

function _perfThreadMemAt(mem, n) {
  if (!mem || !mem.length) return null;
  if (n <= mem[0][0]) return mem[0][1];
  for (let i = 1; i < mem.length; i += 1) {
    const [x1, y1] = mem[i];
    if (n <= x1) {
      const [x0, y0] = mem[i - 1];
      if (x1 === x0) return y1;
      return y0 + ((y1 - y0) * (n - x0)) / (x1 - x0);
    }
  }
  return mem[mem.length - 1][1];
}

function _perfDraw(cur) {
  const cv = $("#perfChart");
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 520;
  const h = 172;
  cv.width = Math.round(w * dpr);
  cv.height = Math.round(h * dpr);
  const g = cv.getContext("2d");
  if (!g) return;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const c = _perfColors();
  const padL = 42;
  const padR = 68;
  const padT = 30;
  const padB = 22;
  const cw = w - padL - padR;
  const ch = h - padT - padB;
  g.font = "11px system-ui, sans-serif";
  g.textBaseline = "middle";

  const data = _perfDemoOf(_perfDrive);
  const max = _perfAxisMax();
  const xOf = (n) => padL + ((n - 1) / Math.max(1, max - 1)) * cw;
  const procsMode = _perfMode === "procs";
  const perProc = procsMode ? _perfPerProc(data) : 0;
  const threadMem = procsMode ? null : _perfThreadMem(data);
  // 两种模式都按实测内存画线：进程模式是每进程内存乘进程数，线程模式按各档实测值
  const hasMem = procsMode ? perProc > 0 : !!(threadMem && threadMem.length);
  let memMax = 0;
  if (hasMem) {
    if (procsMode) {
      memMax = Math.max(50, Math.ceil((MFT_PROCS_MAX * perProc) / 50) * 50);
    } else {
      const top = Math.max(1, ...threadMem.map((p) => p[1]));
      memMax = Math.max(10, Math.ceil(top / 10) * 10);
    }
  }
  const memAt = (n) => (procsMode ? _perfMemAt(n, perProc) : _perfThreadMemAt(threadMem, n));
  const yM = (v) => padT + ch - (Math.min(v, memMax) / memMax) * ch;

  // 时间量程：进程模式看读取与单进程解析，线程模式看实测点的最大值
  let tMax = 20;
  if (data) {
    if (procsMode) {
      tMax = Math.max(Number(data.single_parse_s) || 0, Number(data.read_s) || 0) || 20;
    } else {
      const pts = data.points || [];
      tMax = Math.max(1, ...pts.map((p) => Number(p[1]) || 0)) * 1.15;
    }
  }
  const yT = (v) => padT + ch - (Math.min(v, tMax) / tMax) * ch;

  // 横向网格，左时间右内存
  g.strokeStyle = c.border;
  g.lineWidth = 1;
  for (let i = 0; i <= 2; i += 1) {
    const y = padT + ch * (i / 2);
    g.globalAlpha = i === 2 ? 0.8 : 0.3;
    g.beginPath();
    g.moveTo(padL, y + 0.5);
    g.lineTo(padL + cw, y + 0.5);
    g.stroke();
    g.globalAlpha = 1;
    g.fillStyle = c.text;
    g.textAlign = "right";
    g.fillText((tMax * (1 - i / 2)).toFixed(1) + "s", padL - 6, y);
    g.fillStyle = c.text;
    if (hasMem) {
      g.textAlign = "left";
      g.fillText(Math.round(memMax * (1 - i / 2)) + " MB", padL + cw + 6, y);
    } else {
      g.textAlign = "left";
      g.fillText("--", padL + cw + 6, y);
    }
  }

  // 轴名
  g.fillStyle = c.text;
  g.textAlign = "left";
  g.fillText(t("perfAxisTime"), padL - 2, 12);
  g.textAlign = "right";
  g.fillStyle = c.text;
  g.fillText(t("perfAxisMem"), padL + cw + padR - 6, 12);
  g.fillStyle = c.muted;
  g.textAlign = "center";
  g.fillText(t("perfLegendLine"), padL + cw / 2, 12);

  if (!data) return;

  // 内存线只在真测到内存时才画。
  // 进程模式下这条线是「每进程内存乘进程数」推算出来的，而图例说
  // 实线为实测值、虚线为推算值，所以它必须画虚线；目录遍历那条是
  // 各档实测的内存点直接连起来，保持实线。
  if (hasMem) {
    g.strokeStyle = c.warn;
    g.globalAlpha = 0.85;
    g.lineWidth = 1.5;
    g.setLineDash(procsMode ? [5, 4] : []);
    g.beginPath();
    if (procsMode) {
      for (let n = 1; n <= max; n += 1) {
        const my = yM(memAt(n));
        if (n === 1) g.moveTo(xOf(n), my);
        else g.lineTo(xOf(n), my);
      }
    } else {
      threadMem.forEach(([n, mb], i) => {
        if (i === 0) g.moveTo(xOf(n), yM(mb));
        else g.lineTo(xOf(n), yM(mb));
      });
    }
    g.stroke();
    g.setLineDash([]);
    g.globalAlpha = 1;
    g.fillStyle = c.warn;
    g.textAlign = "left";
    const endMem = memAt(max);
    g.fillText(
      t("mftProcsCurveMem") + " " + Math.round(endMem) + " MB",
      xOf(max) + 4,
      Math.min(yM(endMem) + 12, padT + ch - 4)
    );
  }

  if (procsMode) {
    _perfDrawProcs(g, data, { padL, padR, padT, cw, ch, c, xOf, yT, yM, cur, max });
  } else {
    _perfDrawThreads(g, data, { padL, cw, padT, ch, c, xOf, yT, yM, cur, hasMem, memAt });
  }

  // 当前值竖线
  const cx = xOf(cur);
  g.strokeStyle = c.text;
  g.globalAlpha = 0.3;
  g.lineWidth = 1;
  g.beginPath();
  g.moveTo(cx + 0.5, padT);
  g.lineTo(cx + 0.5, padT + ch);
  g.stroke();
  g.globalAlpha = 1;
}

function _perfPointTag(g, x, y, text, color, rightEdge, topEdge, bottomEdge) {
  // 点在竖线上，标签放右侧；靠右边放不下时翻到左侧。
  // 标签靠上下边缘时向内夹，不能压到轴名、图例和横轴刻度上。
  g.font = "11px system-ui, sans-serif";
  const w = g.measureText(text).width;
  const toRight = x + 9 + w <= rightEdge;
  const ty = Math.max(topEdge, Math.min(bottomEdge, y));
  g.fillStyle = color;
  g.textAlign = toRight ? "left" : "right";
  g.fillText(text, toRight ? x + 9 : x - 9, ty);
}

function _perfDrawProcs(g, data, m) {
  const { padL, padT, cw, ch, c, xOf, yT, yM, cur, max } = m;
  const perProc = _perfPerProc(data);
  const hasMem = perProc > 0;
  const read = Number(data.read_s) || 0;
  const singleParse = Number(data.single_parse_s) || 0;
  if (!(read > 0)) return;
  const timedOut = !!data.timedOut;

  // 只画解析曲线。读取耗时是一条与进程数无关的水平线，
  // 画在图上会和曲线混在一起，改为只在摘要与结果里用文字说明。
  if (singleParse > 0) {
    g.strokeStyle = c.accent;
    g.lineWidth = 2;
    g.setLineDash(timedOut ? [5, 4] : []);
    g.beginPath();
    for (let n = 1; n <= max; n += 1) {
      const qx = xOf(n);
      const qy = yT(singleParse / n);
      if (n === 1) g.moveTo(qx, qy);
      else g.lineTo(qx, qy);
    }
    g.stroke();
    g.setLineDash([]);
    g.fillStyle = c.text;
    g.textAlign = "right";
    if (timedOut) {
      g.fillText(t("mftProcsCurveUnfinished"), padL + cw, padT + 4);
    } else {
      g.fillText(
        t("mftProcsCurveParse") + " " + (singleParse / max).toFixed(1) + "s",
        padL + cw - 2,
        yT(singleParse / max) - 10
      );
    }
  } else {
    g.fillStyle = c.text;
    g.textAlign = "right";
    g.fillText(t("mftProcsCurveUnfinished"), padL + cw, padT + 4);
  }

  // 最佳点：时间线与内存线高度相同的位置
  const best = _perfBestPoint(data);
  if (!(best > 0)) return;
  g.strokeStyle = c.good;
  g.globalAlpha = 0.5;
  g.lineWidth = 1;
  g.setLineDash([3, 3]);
  g.beginPath();
  g.moveTo(xOf(best) + 0.5, padT);
  g.lineTo(xOf(best) + 0.5, padT + ch);
  g.stroke();
  g.setLineDash([]);
  g.globalAlpha = 1;
  g.lineWidth = 2;
  // 圈画在解析曲线上，与上方的交点算法用同一条时间线
  const bestY = yT(singleParse / best);
  g.beginPath();
  g.arc(xOf(best), bestY, 4.5, 0, Math.PI * 2);
  g.stroke();
  g.fillStyle = c.good;
  g.textAlign = "center";
  g.fillText(t("mftProcsCurveBest"), xOf(best), bestY - 12);

  if (singleParse > 0) {
    const py = yT(singleParse / cur);
    g.fillStyle = c.accent;
    g.beginPath();
    g.arc(xOf(cur), py, 3.5, 0, Math.PI * 2);
    g.fill();
    // 圆点旁边的数值：拖动滑杆时直接看得到该档的解析耗时。
    // 推荐标记也写在同一高度，两者撞上时把数值挪到点下方。
    let tagY = py - 12;
    if (Math.abs(py - bestY) < 14 && Math.abs(xOf(cur) - xOf(best)) < 62) {
      tagY = py + 13;
    }
    _perfPointTag(
      g,
      xOf(cur),
      tagY,
      t("perfTagParse") + " " + (singleParse / cur).toFixed(1) + t("perfUnitSec"),
      c.accent,
      padL + cw,
      padT + 9,
      padT + ch - 9
    );
  }
  if (hasMem) {
    const my = yM(_perfMemAt(cur, perProc));
    g.fillStyle = c.warn;
    g.beginPath();
    g.arc(xOf(cur), my, 3, 0, Math.PI * 2);
    g.fill();
    _perfPointTag(
      g,
      xOf(cur),
      my + 13,
      t("mftProcsCurveMem") + " " + _perfMemText(_perfMemAt(cur, perProc)),
      c.warn,
      padL + cw,
      padT + 9,
      padT + ch - 9
    );
  }
}

function _perfDrawThreads(g, data, m) {
  const { padL, padT, cw, ch, c, xOf, yT, yM, cur } = m;
  const points = data.points || [];
  if (!points.length) return;

  // 目录遍历耗时曲线：按实测点连直线
  g.strokeStyle = c.accent;
  g.lineWidth = 2;
  g.setLineDash(data.timedOut ? [5, 4] : []);
  g.beginPath();
  points.forEach(([n, sec], i) => {
    const px = xOf(n);
    const py = yT(sec);
    if (i === 0) g.moveTo(px, py);
    else g.lineTo(px, py);
  });
  g.stroke();
  g.setLineDash([]);
  g.fillStyle = c.accent;
  points.forEach(([n, sec]) => {
    g.beginPath();
    g.arc(xOf(n), yT(sec), 2.6, 0, Math.PI * 2);
    g.fill();
  });

  // 最低点：实测里耗时最短的线程数
  const low = _perfThreadSuggest(points);
  if (!(low > 0)) return;
  const lowCost = _perfDirCost(points, low);
  g.strokeStyle = c.good;
  g.globalAlpha = 0.5;
  g.lineWidth = 1;
  g.setLineDash([3, 3]);
  g.beginPath();
  g.moveTo(xOf(low) + 0.5, padT);
  g.lineTo(xOf(low) + 0.5, padT + ch);
  g.stroke();
  g.setLineDash([]);
  g.globalAlpha = 1;
  g.lineWidth = 2;
  g.beginPath();
  g.arc(xOf(low), yT(lowCost), 4.5, 0, Math.PI * 2);
  g.stroke();
  g.fillStyle = c.good;
  g.textAlign = "center";
  g.fillText(t("perfCurveLowest"), xOf(low), yT(lowCost) - 12);

  const curCost = _perfDirCost(points, cur);
  g.fillStyle = c.accent;
  g.beginPath();
  g.arc(xOf(cur), yT(curCost), 3.5, 0, Math.PI * 2);
  g.fill();
  g.font = "11px system-ui, sans-serif";
  // 与「推荐」文字撞位时，数值标签改画在点下方
  let walkTagY = yT(curCost) - 12;
  if (
    Math.abs(yT(curCost) - yT(lowCost)) < 14 &&
    Math.abs(xOf(cur) - xOf(low)) < 62
  ) {
    walkTagY = yT(curCost) + 13;
  }
  _perfPointTag(
    g,
    xOf(cur),
    walkTagY,
    t("perfTagWalk") + " " + curCost.toFixed(1) + t("perfUnitSec"),
    c.accent,
    padL + cw,
    padT + 9,
    padT + ch - 9
  );

  if (m.hasMem) {
    const my = yM(m.memAt(cur));
    g.fillStyle = c.warn;
    g.beginPath();
    g.arc(xOf(cur), my, 3, 0, Math.PI * 2);
    g.fill();
    _perfPointTag(
      g,
      xOf(cur),
      my + 13,
      t("mftProcsCurveMem") + " " + _perfMemText(m.memAt(cur)),
      c.warn,
      padL + cw,
      padT + 9,
      padT + ch - 9
    );
  }
}

function _perfSummary() {
  const el = $("#perfSummary");
  const range = $("#perfRange");
  if (!el || !range) return;
  const n = Number(range.value) || 1;
  const data = _perfDemoOf(_perfDrive);
  const mb = _perfMemAt(n, _perfPerProc(data));
  if (!data) {
    el.textContent = "";
    return;
  }
  if (_perfMode === "procs") {
    const read = Number(data.read_s) || 0;
    const single = Number(data.single_parse_s) || 0;
    if (data.timedOut && !(single > 0)) {
      el.textContent = t("mftProcsSummaryUnfinished", {
        read: read.toFixed(1),
        n,
        mb,
      });
      return;
    }
    const parse = single / n;
    el.textContent = t("mftProcsSummary", {
      read: read.toFixed(1),
      total: Math.max(read, parse).toFixed(1),
      mb,
    });
    return;
  }
  const tmem = _perfThreadMemAt(_perfThreadMem(data), n);
  el.textContent = t(tmem === null ? "perfSummaryThreadsNoMem" : "perfSummaryThreads", {
    n,
    mb: tmem === null ? "" : String(Math.round(tmem)),
  });
}

// 刻度按当前轴上限生成
function _perfPlaceScale() {
  const scale = $("#perfScale");
  const range = $("#perfRange");
  if (!scale || !range) return;
  const max = _perfAxisMax();
  range.max = String(max);
  const cur = Math.max(1, Math.min(max, Number(range.value) || 1));
  range.value = String(cur);
  scale.innerHTML = "";
  _perfTicks(max).forEach((v) => {
    const el = document.createElement("span");
    el.textContent = String(v);
    el.style.left = ((v - 1) / Math.max(1, max - 1)) * 100 + "%";
    scale.appendChild(el);
  });
}

function _perfSync() {
  const range = $("#perfRange");
  const procs = $("#perfProcs");
  if (!range) return;
  // 没有管理员权限时，读取 MFT 与处理进程数测不了；提示一次，测试仍可跑（只测目录遍历）
  const envWarn = $("#perfEnvWarn");
  if (envWarn) {
    const admin = !!(state._settings && state._settings.is_admin);
    envWarn.classList.toggle("hidden", admin);
  }
  _perfPlaceScale();
  const n = Number(range.value) || 1;
  if (procs && _perfMode === "procs") procs.value = String(n);
  _perfSummary();
  _perfDraw(n);
  _perfUpdateDriveFlags();
  _perfUpdateHint();
  const testBtn = $("#perfTest");
  if (testBtn) testBtn.disabled = !_perfDrive || _perfBusy;
  const resetBtn = $("#perfResetDefault");
  if (resetBtn) resetBtn.disabled = !_perfDrive || _perfBusy;
}

// 切语言时把这一区临时生成的文字重画一遍
function _perfRefreshLanguage() {
  if (!$("#perfChart")) return;
  _perfSync();
  _perfSetBusy(_perfBusy);
}

// 启动时判断要不要提醒去做性能测试。
// 只看有没有测过：任意一块盘的处理进程数或目录遍历任意一组有数据就算测过。
// 非管理员不影响判断，因为目录遍历线程数照样能测。
function _perfHasAnyTest(raw) {
  if (!raw || typeof raw !== "object") return false;
  return Object.keys(raw).some(function (letter) {
    const item = raw[letter];
    if (!item || typeof item !== "object") return false;
    const procs = item.procs;
    const threads = item.threads;
    if (procs && typeof procs === "object" && Number(procs.read_s) > 0) return true;
    if (threads && typeof threads === "object" && Array.isArray(threads.points) && threads.points.length) return true;
    return false;
  });
}

function maybeShowPerfPrompt() {
  const overlay = $("#perfPromptOverlay");
  if (!overlay) return;
  const s = state._settings || {};
  if (s.perf_prompt === false) return;
  if (_perfHasAnyTest(s.perf_tests)) return;
  // 已经有别的弹窗开着时让位，避免两层模态叠在一起
  if (document.querySelectorAll(".overlay:not(.hidden)").length) return;
  overlay.classList.remove("hidden");
}

// 勾了以后不再提醒就把开关写进设置，取消与关闭只关窗
function wirePerfPromptUi() {
  const overlay = $("#perfPromptOverlay");
  if (!overlay || overlay.dataset.bound === "1") return;
  overlay.dataset.bound = "1";
  const chk = $("#perfPromptOffChk");

  function rememberOff() {
    if (!chk || !chk.checked) return;
    state._settings = state._settings || {};
    state._settings.perf_prompt = false;
    try {
      const p = state.api.apply_settings({ perf_prompt: false });
      if (p && typeof p.catch === "function") p.catch(function () {});
    } catch (e) {}
  }

  function closeOnly() {
    overlay.classList.add("hidden");
    rememberOff();
  }

  const cancelBtn = $("#perfPromptCancelBtn");
  if (cancelBtn) cancelBtn.addEventListener("click", closeOnly);
  const closeBtn = $("#perfPromptCloseBtn");
  if (closeBtn) closeBtn.addEventListener("click", closeOnly);
  const goBtn = $("#perfPromptGoBtn");
  if (goBtn) {
    goBtn.addEventListener("click", function () {
      rememberOff();
      overlay.classList.add("hidden");
      if (typeof openSettings === "function") openSettings("perf");
    });
  }
}

function _perfUpdateHint() {
  const el = $("#perfHint");
  if (!el) return;
  el.textContent = t(_perfMode === "procs" ? "perfHintProcs" : "perfHintThreads");
}

// 清掉测试的兜底定时器。收到终态事件、或接口直接报错时才清，
// 接口正常返回不意味着测试结束，它在后台线程里继续跑。
function _perfClearGuard() {
  if (_perfGuard) {
    clearTimeout(_perfGuard);
    _perfGuard = null;
  }
}

// 盘符按钮的两种标记按「参数是否已被单独设置」与「是否有实测数据」分别判定：
// 走默认的盘（没设过，或刚点过恢复默认）铺淡警示底色，
// 测过且自己设过参数的盘铺淡绿色底色，其余保持普通底色。
function _perfUpdateDriveFlags() {
  const def = _perfGlobalDefaults();
  // MFT 读取线程的默认值恒为 1，设成 1 与跟随默认用起来完全一样，
  // 所以这一项只要等于默认就不算「自己设过」，否则恢复默认后被存成 1，
  // 重开设置页就会误判成自己设过而标成绿色
  const sameMft = (n) => {
    const v = Number(n) || 0;
    return v === 0 || v === def.mft;
  };
  document.querySelectorAll(".perf-drive-btn").forEach((btn) => {
    const letter = btn.dataset.drive;
    const stored = _perfProcsOf(letter) || {};
    const edited = _perfInputs[letter] || null;
    const tested = !!(letter && _perfTested(letter));
    // 走默认的判定分三种情形：刚点过恢复默认、控件里有值（可能是切盘时暂存下来的）、
    // 只有存储值。控件里有值不等于用户改过，所以一律按「是否仍等于默认」判断。
    let followsDefault;
    if (edited) {
      followsDefault =
        sameMft(edited.mft) &&
        Number(edited.dir) === def.dir &&
        Number(edited.procs) === def.procs;
    } else if (_perfResetFlags[letter]) {
      followsDefault = true;
    } else {
      followsDefault =
        sameMft(stored.mft_read) &&
        !(Number(stored.dir_threads) || 0) &&
        !(Number(stored.mft_procs) || 0);
    }
    btn.classList.toggle("is-default", followsDefault);
    btn.classList.toggle("is-measured", tested && !followsDefault);
  });
}

// 这块盘是否至少有一组实测数据（与当前看的是哪种曲线无关）
function _perfTested(letter) {
  const groups = _perfData[letter];
  if (!groups) return false;
  return Object.keys(groups).some((kind) => groups[kind] && groups[kind].tested);
}

function _perfRenderDrives() {
  const box = $("#perfDrive");
  if (!box) return;
  box.innerHTML = "";
  _perfDrives.forEach((item) => {
    const letter = String(item.letter || "").trim();
    if (!letter) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "perf-drive-btn";
    btn.dataset.drive = letter;
    btn.textContent = letter;
    if (letter === _perfDrive) btn.classList.add("is-active");
    btn.addEventListener("click", () => _perfSetDrive(letter));
    box.appendChild(btn);
  });
  _perfUpdateDriveFlags();
  _perfSyncCopyVisible();
}

function _perfSetDrive(letter) {
  // 测试进行中不许换盘，否则结果会记到别的盘上
  if (_perfBusy || !letter || letter === _perfDrive) return;
  _perfStashInputs();
  _perfDrive = letter;
  // 快速应用面板的目标列表是按上一块盘生成的，切盘就收起来
  const copyPanel = $("#perfCopyPanel");
  if (copyPanel) copyPanel.classList.add("hidden");
  document.querySelectorAll(".perf-drive-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.drive === letter);
  });
  _perfLoadInputs();
  _perfResetResult();
  _perfSync();
  // 切盘只是换一块盘的显示值，不算改动
  _settingsRefreshSnapshotIfUntouched();
}

function _perfLoadInputs() {
  const v = _perfValueOf(_perfDrive);
  const mft = $("#perfMft");
  const dir = $("#perfDir");
  const procs = $("#perfProcs");
  const range = $("#perfRange");
  if (mft) mft.value = String(v.mft);
  if (dir) dir.value = String(v.dir);
  if (procs) procs.value = String(v.procs);
  if (range) range.value = String(_perfMode === "procs" ? v.procs : v.dir);
}

// 换盘或换曲线时把上一轮的结果面板收起来，避免结果挂在别的盘下
function _perfResetResult() {
  const panel = $("#perfResult");
  if (panel) panel.classList.add("hidden");
}

function _perfSetBusy(on) {
  _perfBusy = on;
  const range = $("#perfRange");
  const btn = $("#perfTest");
  const mft = $("#perfMft");
  const dir = $("#perfDir");
  const procs = $("#perfProcs");
  [range, mft, dir, procs].forEach((el) => {
    if (el) el.disabled = on;
  });
  document.querySelectorAll(".perf-drive-btn").forEach((el) => {
    el.disabled = on;
  });
  document.querySelectorAll(".perf-switch-btn").forEach((el) => {
    el.disabled = on;
  });
  const resetBtn = $("#perfResetDefault");
  if (resetBtn) resetBtn.disabled = on || !_perfDrive;
  if (btn) {
    // 一块盘都没取到时也置灰，避免发出没有盘符的测试请求
    btn.disabled = on || !_perfDrive;
    btn.textContent = on
      ? t("mftProcsBusy")
      : (_perfDemoOf(_perfDrive) ? t("mftProcsRetest") : t("perfTestStart"));
  }
}

function _perfShowProgress() {
  const prog = $("#perfProgress");
  const result = $("#perfResult");
  const live = $("#perfLive");
  const fill = $("#perfFill");
  if (result) result.classList.add("hidden");
  if (prog) prog.classList.remove("hidden");
  if (live) live.textContent = t("mftProcsTestingLive", { cpu: "0", mem: "0 MB", sec: "0", pct: "0" });
  if (fill) fill.style.width = "0%";
}

function _perfHideProgress() {
  const prog = $("#perfProgress");
  if (prog) prog.classList.add("hidden");
}

function _perfMemText(mb) {
  if (!mb) return "0 MB";
  return mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : Math.round(mb) + " MB";
}

function _perfShowResult(title, body, canApply) {
  const panel = $("#perfResult");
  const titleEl = $("#perfResultTitle");
  const bodyEl = $("#perfResultBody");
  const ignore = $("#perfIgnore");
  const apply = $("#perfApply");
  if (panel) panel.classList.remove("hidden");
  if (titleEl) {
    titleEl.textContent = title || "";
    // 没有标题时整行收起来，不留一条空行
    titleEl.classList.toggle("hidden", !title);
  }
  if (bodyEl) bodyEl.textContent = body || "";
  if (ignore) ignore.textContent = t("mftProcsGotIt");
  if (apply) apply.classList.toggle("hidden", !canApply);
  _perfHideProgress();
}

// 把界面上现有的测试结果整理成与配置一致的形状，供提交时带给后端。
// 只有本次新测出来的结果才需要提交，没新测过就不带这个键，
// 免得干扰「设置有没有改动」的判断。
function _perfTestsPayload() {
  const out = {};
  Object.keys(_perfData || {}).forEach((letter) => {
    const groups = _perfData[letter] || {};
    const obj = {};
    ["procs", "threads"].forEach((kind) => {
      const g = groups[kind];
      if (!g || !g.tested) return;
      const copy = {};
      Object.keys(g).forEach((k) => {
        if (k === "kind" || k === "tested" || k === "timedOut" || k === "suggest") return;
        copy[k] = g[k];
      });
      copy.mode = kind;
      copy.timed_out = !!g.timedOut;
      obj[kind] = copy;
    });
    if (Object.keys(obj).length) out[letter] = obj;
  });
  return out;
}

function _perfSaveGroup(letter, kind, data) {
  const group = _perfData[letter] && typeof _perfData[letter] === "object" ? _perfData[letter] : {};
  // 只换本次测到的那一组，另一组保留，避免重测一项把另一项抹掉
  group[kind] = data;
  _perfData[letter] = group;
}

function _perfProcsDataOf(payload) {
  const read = Number(payload.read_s) || 0;
  const single = Number(payload.single_parse_s) || 0;
  const data = {
    kind: "procs",
    tested: true,
    timedOut: !!payload.timed_out,
    read_s: read,
    single_parse_s: single,
    // 内存只用测试里采到的实测值，采不到就是 0，界面据此不提内存
    per_proc_mb: Number(payload.per_proc_mb) || 0,
  };
  // 交叉点很少刚好落在整数上，取右侧最近的那个整数：
  // 进程越多解析越快，右侧就是时间更少的一侧
  data.suggest = Math.max(1, Math.min(MFT_PROCS_MAX, Math.ceil(_perfBestPoint(data)) || 1));
  return data;
}

function _perfThreadsDataOf(payload) {
  const points = (payload.points || []).map((p) => [Number(p[0]) || 1, Number(p[1]) || 0]);
  const mem = (payload.mem || []).map((p) => [Number(p[0]) || 1, Number(p[1]) || 0]);
  const data = {
    kind: "threads",
    tested: true,
    timedOut: !!payload.timed_out,
    points,
    mem,
    sample: Number(payload.sample) || 0,
  };
  data.suggest = _perfThreadSuggest(points);
  return data;
}

// 一次测试两组结果一起交回：先写进数据，再按当前曲线给出正文
function _perfShowAllResult(payload) {
  const letter = _perfDrive;
  const procsRaw = (payload && payload.procs) || {};
  const threadsRaw = (payload && payload.threads) || {};
  let procsData = null;
  let threadsData = null;
  if ((Number(procsRaw.read_s) || 0) > 0) {
    procsData = _perfProcsDataOf(procsRaw);
    _perfSaveGroup(letter, "procs", procsData);
  }
  if ((threadsRaw.points || []).length) {
    threadsData = _perfThreadsDataOf(threadsRaw);
    _perfSaveGroup(letter, "threads", threadsData);
  }

  // 两条路径各自给一句，一起摆在结果里，不用来回切曲线看
  const lines = [];
  if (procsData) {
    const n = procsData.suggest;
    lines.push(t("perfAllProcs", { n }));
  }
  if (threadsData) {
    lines.push(t("perfAllThreads", { n: threadsData.suggest }));
  }
  if (payload && payload.mft_skipped) {
    lines.push(String(payload.mft_skipped));
  }
  // 测试结果不再用「X 盘测试结果」开头，标题只留给超时提示
  const title = payload && payload.timed_out
    ? t("mftProcsTimeoutTitle", Math.round(Number(payload.elapsed_s) || 0) || 0)
    : "";
  _perfShowResult(title, lines.join(" "), !!(procsData || threadsData));
  _perfSetBusy(false);
  // 测完直接把当前盘调到两组建议值上，不用再点一次「填入」
  _perfAutoApply(letter, procsData, threadsData);
  _perfSync();
}

// 把测出来的建议值写进当前盘的参数。只是改本次设置，点保存才生效。
function _perfAutoApply(letter, procsData, threadsData) {
  if (!letter) return;
  const values = Object.assign({}, _perfValueOf(letter));
  let changed = false;
  if (procsData && procsData.suggest > 0) {
    values.procs = procsData.suggest;
    changed = true;
  }
  if (threadsData && threadsData.suggest > 0) {
    values.dir = Math.min(PERF_DIR_CAP, threadsData.suggest);
    changed = true;
  }
  if (!changed) return;
  _perfInputs[letter] = values;
  // 这是程序替你填的值，但它确实会被保存，所以算作本次的未保存改动，
  // 否则后面切一下盘重建快照，就把这份改动当成初始状态抹掉了。
  _settingsTouched = true;
  // 写成具体值了，就不是「跟随默认」了
  delete _perfResetFlags[letter];
  const procsEl = $("#perfProcs");
  const dirEl = $("#perfDir");
  const range = $("#perfRange");
  if (procsEl) procsEl.value = String(values.procs);
  if (dirEl) dirEl.value = String(values.dir);
  if (range) {
    range.value = String(
      _perfMode === "threads"
        ? Math.min(PERF_DIR_MAX, values.dir)
        : values.procs
    );
  }
  _perfUpdateDriveFlags();
}

function _perfShowTimeout(payload) {
  const letter = _perfDrive;
  const procsRaw = (payload && payload.procs) || {};
  const threadsRaw = (payload && payload.threads) || {};
  const gotProcs = (Number(procsRaw.read_s) || 0) > 0;
  const gotThreads = (threadsRaw.points || []).length > 0;
  if (gotProcs) {
    const d = _perfProcsDataOf(procsRaw);
    // 解析已经测完就不动那组数据：超时往往发生在后面的目录遍历段，
    // 此时处理进程数的结果已经写进配置，再清零就把有效结果抹掉了。
    if (!(Number(d.single_parse_s) > 0)) {
      d.single_parse_s = 0;
      d.suggest = 0;
      d.timedOut = true;
    }
    _perfSaveGroup(letter, "procs", d);
  }
  if (gotThreads) {
    _perfSaveGroup(letter, "threads", _perfThreadsDataOf(threadsRaw));
  }
  let body = t("mftProcsTimeoutBody");
  if (!gotProcs && !gotThreads) body = t("mftProcsTimeoutBodyNoRead");
  _perfShowResult(
    t("mftProcsTimeoutTitle", Math.round(Number(payload && payload.timeout_s) || 0) || 0),
    body,
    false
  );
  _perfSetBusy(false);
  _perfSync();
}

function _perfBind() {
  const range = $("#perfRange");
  const btn = $("#perfTest");
  const procs = $("#perfProcs");
  const dir = $("#perfDir");
  if (!range || !btn || range.dataset.bound === "1") return;
  range.dataset.bound = "1";

  // 恢复默认后再动控件，只需要更新输入值；
  // 这块盘的恢复标记保留，提交时按项比较就能表达「哪几项又改回去了」
  const edited = () => {
    _perfStashInputs();
  };

  // 滑杆与数字框双向联动：滑杆拖的是当前曲线对应的那一项
  range.addEventListener("input", () => {
    const n = Number(range.value) || 1;
    if (_perfMode === "procs" && procs) procs.value = String(n);
    if (_perfMode === "threads" && dir) dir.value = String(n);
    edited();
    _perfSummary();
    _perfDraw(n);
  });
  if (procs) {
    procs.addEventListener("input", () => {
      const n = Math.max(1, Math.min(MFT_PROCS_MAX, Number(procs.value) || 1));
      if (_perfMode === "procs") range.value = String(n);
      edited();
      _perfSummary();
      _perfDraw(Number(range.value) || 1);
    });
  }
  if (dir) {
    dir.addEventListener("input", () => {
      const n = Math.max(1, Math.min(PERF_DIR_CAP, Number(dir.value) || 1));
      if (_perfMode === "threads") range.value = String(Math.min(PERF_DIR_MAX, n));
      edited();
      _perfSummary();
      _perfDraw(Number(range.value) || 1);
    });
  }
  const mft = $("#perfMft");
  if (mft) mft.addEventListener("input", edited);

  // 曲线切换：两条路径各自一份测试数据
  document.querySelectorAll(".perf-switch-btn").forEach((btnEl) => {
    btnEl.addEventListener("click", () => {
      if (_perfBusy) return;
      const target = btnEl.dataset.target;
      if (target === _perfMode) return;
      _perfMode = target;
      document.querySelectorAll(".perf-switch-btn").forEach((b) => {
        b.classList.toggle("is-active", b.dataset.target === target);
      });
      const v = _perfValueOf(_perfDrive);
      range.value = String(target === "procs" ? v.procs : Math.min(PERF_DIR_MAX, v.dir));
      _perfResetResult();
      _perfSync();
      // 切曲线只改界面显示，不算用户改动，否则什么都没改也会提示未保存
      _settingsRefreshSnapshotIfUntouched();
    });
  });

  // 等接口返回这段时间里忙碌标记还没置上，靠单独的开关拦住连点
  let starting = false;
  btn.addEventListener("click", async () => {
    if (_perfBusy || starting) return;
    const api = state.api || {};
    const fn = api.test_perf;
    if (typeof fn !== "function") return;
    if (!_perfDrive) return;
    starting = true;
    _perfPending = true;
    _perfAbandoned = false;
    _perfClearGuard();
    // 接口返回前就要拦住切盘与快速应用，否则结果会记到切过去的那块盘上
    _perfBusy = true;
    // 调用永远不返回时（窗口重载、内核卡住）按钮不能一直死着。
    // 后端的自超时是 150 秒，这里要比它长，免得后端还在正常测，界面先把它判成已取消。
    // 注意这个定时器要等到终态事件到了才清，不能在接口返回时就清：
    // 接口只负责启动后台线程、毫秒级就返回，真正跑的是后台线程。
    _perfGuard = setTimeout(() => {
      if (!_perfPending) return;
      starting = false;
      _perfPending = false;
      // 这一轮已经放弃，迟到的终态事件不再回写界面
      _perfAbandoned = true;
      _perfSetBusy(false);
      _perfHideProgress();
      _perfShowResult("", t("mftProcsCancelled"), false);
    }, 160000);
    const item = _perfProcsOf(_perfDrive);
    const root = (item && item.root) || _perfDrive + ":\\";
    let res = null;
    try {
      res = await fn.call(api, root);
    } catch (err) {
      res = { error: String(err && err.message ? err.message : err) };
    } finally {
      starting = false;
    }
    // 后端几乎立刻失败时，终态事件可能已经把界面收拾好了
    if (!_perfPending) return;
    if (res && res.error) {
      _perfClearGuard();
      _perfShowResult("", res.error, false);
      _perfPending = false;
      _perfSetBusy(false);
      return;
    }
    _perfSetBusy(true);
    _perfShowProgress();
  });

  const cancelBtn = $("#perfCancel");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => {
      if (state.api && typeof state.api.cancel_mft_proc_test === "function") {
        state.api.cancel_mft_proc_test();
      }
    });
  }

  const ignore = $("#perfIgnore");
  if (ignore) {
    ignore.addEventListener("click", () => {
      _perfResetResult();
    });
  }

  const apply = $("#perfApply");
  if (apply) {
    apply.addEventListener("click", () => {
      const data = _perfDemoOf(_perfDrive);
      if (!data || !(data.suggest > 0)) return;
      if (_perfMode === "procs") {
        if (procs) procs.value = String(data.suggest);
        range.value = String(data.suggest);
      } else {
        if (dir) dir.value = String(data.suggest);
        range.value = String(Math.min(PERF_DIR_MAX, data.suggest));
      }
      _perfStashInputs();
      // 这是点「填入」产生的改动，要算进未保存判断里
      _settingsTouched = true;
      _perfShowResult(
        _perfMode === "procs"
          ? t("mftProcsApplied", data.suggest)
          : t("perfAppliedThreads", data.suggest),
        "",
        false
      );
      _perfSync();
    });
  }

  if (!_perfEventsBound) {
    _perfEventsBound = true;
    const prev = window.__onPyEvent;
    window.__onPyEvent = function (event, payload) {
      try {
        if (typeof prev === "function") prev(event, payload);
      } catch (e) {}
      if (event === "mft-proc-test-progress") {
        // 这一轮已经被前端判成放弃，迟到的进度不能再把面板拉回来
        if (_perfAbandoned) return;
        const prog = $("#perfProgress");
        const live = $("#perfLive");
        const fill = $("#perfFill");
        const pct = Math.max(0, Math.min(100, Number(payload && payload.pct) || 0));
        if (prog) prog.classList.remove("hidden");
        const lead = $("#perfLead");
        if (lead) {
          lead.textContent =
            payload && payload.phase === "threads"
              ? t("perfTestingThreadsLead")
              : t("mftProcsTestingLead");
        }
        if (live) {
          live.textContent = t("mftProcsTestingLive", {
            cpu: (Number(payload && payload.cpu_pct) || 0).toFixed(0),
            mem: _perfMemText(Number(payload && payload.mem_mb) || 0),
            sec: (Number(payload && payload.elapsed_s) || 0).toFixed(0),
            pct: String(pct),
          });
        }
        if (fill) fill.style.width = pct + "%";
      } else if (event === "mft-proc-test-done") {
        _perfClearGuard();
        // 这一轮已经被前端判成放弃，迟到的结果不再往界面上写
        if (_perfAbandoned) return;
        _perfPending = false;
        _perfHideProgress();
        if (payload && payload.timed_out) {
          _perfShowTimeout(payload || {});
        } else {
          // 一次测试交回两组结果，统一由这里写入并展示
          _perfShowAllResult(payload || {});
        }
      } else if (event === "mft-proc-test-error") {
        _perfClearGuard();
        if (_perfAbandoned) return;
        _perfPending = false;
        _perfHideProgress();
        _perfSetBusy(false);
        _perfShowResult("", (payload && payload.message) || "", false);
      } else if (event === "mft-proc-test-cancelled") {
        _perfClearGuard();
        if (_perfAbandoned) return;
        _perfPending = false;
        _perfHideProgress();
        _perfSetBusy(false);
        _perfShowResult("", t("mftProcsCancelled"), false);
      }
    };
  }

  // 恢复默认：把当前盘的三项参数清掉，回到跟随全局默认值。
  // 只改草稿，点「完成」保存后才真正生效。
  const resetBtn = $("#perfResetDefault");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      if (_perfBusy || !_perfDrive) return;
      const letter = _perfDrive;
      _perfResetFlags[letter] = true;
      delete _perfInputs[letter];
      // 恢复默认也是一次未保存的改动
      _settingsTouched = true;
      _perfLoadInputs();
      _perfResetResult();
      _perfSync();
      if (typeof toast === "function") toast(t("perfResetDone"), "info");
    });
  }

  _perfBindCopy();

  new MutationObserver(() => _perfSync()).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });

  // 设置窗口可拖拽缩放，画布像素只在于显式重画时才按新宽度重建，
  // 不重画就会把旧图拉伸成模糊的。
  let resizePending = false;
  window.addEventListener("resize", () => {
    if (resizePending) return;
    resizePending = true;
    requestAnimationFrame(() => {
      resizePending = false;
      const overlay = $("#settingsOverlay");
      if (!overlay || overlay.classList.contains("hidden")) return;
      // 停在别的页签时画布宽为零，重画只会得到一张宽度不对的图，跳过
      const pane = document.querySelector('[data-settings-pane="perf"]');
      if (!pane || pane.classList.contains("hidden")) return;
      if (!$("#perfChart")) return;
      _perfPlaceScale();
      _perfDraw(Number($("#perfRange") && $("#perfRange").value) || 1);
    });
  });
}

// 快速应用按钮只在存在别的固定盘时才显示
function _perfSyncCopyVisible() {
  const btn = $("#perfCopyTo");
  const panel = $("#perfCopyPanel");
  if (!btn) return;
  const others = _perfDrives
    .map((item) => item.letter)
    .filter((letter) => letter && letter !== _perfDrive);
  const none = others.length === 0;
  btn.classList.toggle("hidden", none);
  if (none && panel) panel.classList.add("hidden");
}

// 快速应用：把当前盘这三个参数写到勾选的盘，测试结果不跟着走
function _perfBindCopy() {
  const btn = $("#perfCopyTo");
  const panel = $("#perfCopyPanel");
  const list = $("#perfCopyList");
  const applyBtn = $("#perfCopyApply");
  const cancelBtn = $("#perfCopyCancel");
  if (!btn || !panel || !list || btn.dataset.bound === "1") return;
  btn.dataset.bound = "1";

  const targets = () =>
    _perfDrives.map((item) => item.letter).filter((letter) => letter && letter !== _perfDrive);

  // 只有一个可写的盘时这一项没有意义，直接不显示
  const syncVisible = () => _perfSyncCopyVisible();

  btn.addEventListener("click", () => {
    if (_perfBusy) return;
    const letters = targets();
    if (!letters.length) return;
    list.innerHTML = "";
    letters.forEach((letter) => {
      const label = document.createElement("label");
      label.className = "perf-copy-item";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = letter;
      // 默认全选：后面还要再点一次应用，误点的机会不大
      box.checked = true;
      const text = document.createElement("span");
      text.textContent = letter;
      label.appendChild(box);
      label.appendChild(text);
      list.appendChild(label);
    });
    panel.classList.remove("hidden");
    syncVisible();
  });

  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => panel.classList.add("hidden"));
  }

  if (applyBtn) {
    applyBtn.addEventListener("click", () => {
      _perfStashInputs();
      const from = _perfDrive;
      const value = _perfInputs[from] || _perfValueOf(from);
      if (!value) return;
      // 源盘本身就是「跟随默认」时，目标盘也应该跟随默认，
      // 否则把全局默认值当成具体值写进目标盘，以后改全局默认时它不会跟着变
      const fromDefault = !!_perfResetFlags[from];
      const picked = [];
      list.querySelectorAll("input[type=checkbox]").forEach((box) => {
        if (box.checked) picked.push(box.value);
      });
      if (!picked.length) return;
      // 套用到其他盘也是未保存的改动
      _settingsTouched = true;
      picked.forEach((letter) => {
        if (fromDefault) {
          delete _perfInputs[letter];
          _perfResetFlags[letter] = true;
          return;
        }
        _perfInputs[letter] = { mft: value.mft, dir: value.dir, procs: value.procs };
        // 目标盘本来点过恢复默认的话，这次写入的就是具体值，标记要撤掉
        delete _perfResetFlags[letter];
      });
      panel.classList.add("hidden");
      _perfUpdateDriveFlags();
      _perfSummary();
      if (typeof toast === "function") {
        toast(t("perfCopyDone", { from, n: picked.length }), "info");
      }
    });
  }

  syncVisible();
}

// 从后端取盘列表，没有返回值时至少保证当前盘可用
// 把后端返回的盘列表归一：只接受能推出单字母盘符的项，字段缺了给默认值。
// 归一这一步不能省：盘符是空串时按钮既没有文字，又会全被当成当前盘染成选中色。
function _perfNormalizeDrives(raw) {
  const out = [];
  (Array.isArray(raw) ? raw : []).forEach((item) => {
    if (!item) return;
    if (typeof item === "string") {
      const l = item.replace(/:.*$/, "").trim().slice(0, 1).toUpperCase();
      if (/[A-Z]/.test(l)) {
        out.push({ letter: l, root: l + ":\\", mft_read: 0, dir_threads: 0, mft_procs: 0 });
      }
      return;
    }
    const root = String(item.root || "");
    const rawLetter = String(item.letter || root.replace(/:.*$/, "") || "");
    const letter = rawLetter.trim().slice(0, 1).toUpperCase();
    if (!/[A-Z]/.test(letter)) return;
    out.push({
      letter,
      root: item.root || letter + ":\\",
      mft_read: Number(item.mft_read) || 0,
      dir_threads: Number(item.dir_threads) || 0,
      mft_procs: Number(item.mft_procs) || 0,
      // 该盘真正生效的数值，未单独设过的项等于全局默认
      effective:
        item.effective && typeof item.effective === "object"
          ? {
              mft: Number(item.effective.mft_read) || 0,
              dir: Number(item.effective.dir_threads) || 0,
              procs: Number(item.effective.mft_procs) || 0,
            }
          : null,
    });
  });
  return out;
}

async function _perfLoadDrives() {
  // 重新拉盘列表意味着要回到后端刚返回的值，先把上一轮编辑与基线清掉，
  // 否则刚换数据目录或恢复默认后，界面还在显示旧值、基线也记着旧值。
  _perfInputs = {};
  _perfBaseline = {};
  _perfResetFlags = {};
  _perfReady = false;
  let items = [];
  let failed = false;
  try {
    if (state.api && typeof state.api.list_perf_drives === "function") {
      const res = await state.api.list_perf_drives();
      items = _perfNormalizeDrives(res && res.drives);
    } else {
      failed = true;
    }
  } catch (e) {
    failed = true;
  }
  if (!items.length) {
    // 拿不到盘符时不要编造一块 C 盘去显示参数，否则用户会以为那就是真实配置；
    // 保留空表并禁用测试按钮，同时告知实际发生了什么。
    _perfDrives = [];
    _perfDrive = "";
    _perfRenderDrives();
    if (typeof toast === "function" && failed) toast(t("perfDrivesFailed"), "warn");
    _perfSync();
    _settingsRefreshSnapshotIfUntouched();
    return;
  }
  _perfDrives = items;
  if (!_perfDrive || !items.some((x) => x.letter === _perfDrive)) {
    _perfDrive = items[0].letter;
  }
  _perfReady = true;
  _perfRenderDrives();
  _perfLoadInputs();
  _perfSnapshotBaseline();
  _perfSync();
  // 盘符、输入框到这里才是最终值，把它当成“打开时的初始状态”
  _settingsRefreshSnapshotIfUntouched();
}

// 把配置里存着的测试结果还原成界面用的结构，按盘逐项还原。
// 存下来的是接口原样载荷（mode / timed_out / points ...），
// 界面这边认的是 kind / tested / timedOut，两种结构在这里对齐。
function _perfRestoreTests(raw) {
  const out = {};
  if (!raw || typeof raw !== "object") return out;
  Object.keys(raw).forEach((letter) => {
    const item = raw[letter];
    if (!item || typeof item !== "object") return;
    const group = {};
    const procs = item.procs || {};
    if ((Number(procs.read_s) || 0) > 0) {
      group.procs = _perfProcsDataOf(procs);
    }
    const threads = item.threads || {};
    if ((threads.points || []).length) {
      group.threads = _perfThreadsDataOf(threads);
    }
    if (Object.keys(group).length) out[letter] = group;
  });
  return out;
}

function _perfFill() {
  if (!_settingsDraft) return;
  _perfBind();
  _perfData = _perfRestoreTests(_settingsDraft.perf_tests);
  _perfTestsBase = JSON.stringify(_perfTestsPayload());
  _perfLoadDrives();
}

function fillSettingsFormFromDraft() {
  const d = _settingsDraft;
  if (!d) return;
  _perfFill();
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
  const rememberWinSizeChk = $("#rememberWinSizeChk");
  if (rememberWinSizeChk) {
    rememberWinSizeChk.checked = d.remember_window_size !== false;
  }
  const tempCleanupChk = $("#tempCleanupChk");
  if (tempCleanupChk) {
    tempCleanupChk.checked = d.temp_cleanup !== false;
  }
  const autoCheckUpdateChk = $("#autoCheckUpdateChk");
  if (autoCheckUpdateChk) {
    autoCheckUpdateChk.checked = d.auto_check_updates !== false;
  }
  const updateBadgeChk = $("#updateBadgeChk");
  if (updateBadgeChk) {
    updateBadgeChk.checked = d.update_badge !== false;
  }
  const logSanitizeChk = $("#logSanitizeChk");
  if (logSanitizeChk) {
    logSanitizeChk.checked = d.log_sanitize !== false;
  }
  const logLevelSel = $("#logLevelSel");
  if (logLevelSel) {
    logLevelSel.value = _normalizeLogLevel(d.log_level);
  }
  const devToolsChk = $("#devToolsChk");
  if (devToolsChk) {
    devToolsChk.checked = !!d.dev_tools;
  }
  const uiThemeSel = $("#uiThemeSel");
  if (uiThemeSel) uiThemeSel.value = _normalizeUiTheme(d.theme);
  const sizeSrc = _normalizeSizeSource(d.size_source);
  for (const el of document.querySelectorAll('input[name="sizeSource"]')) {
    el.checked = el.value === sizeSrc;
  }
  const uiParentBgChk = $("#uiParentBgChk");
  if (uiParentBgChk) uiParentBgChk.checked = d.ui_parent_row_bg === true;
  const uiParentDimChk = $("#uiParentDimChk");
  if (uiParentDimChk) uiParentDimChk.checked = d.ui_parent_row_dim === true;
  const uiHoverOutlineChk = $("#uiHoverOutlineChk");
  if (uiHoverOutlineChk) uiHoverOutlineChk.checked = d.ui_hover_group_outline === true;
  const uiHoverGroupMin = $("#uiHoverGroupMin");
  if (uiHoverGroupMin) uiHoverGroupMin.value = String(_normalizeHoverGroupMin(d.ui_hover_group_min));
  const uiTreeGuideChk = $("#uiTreeGuideChk");
  if (uiTreeGuideChk) uiTreeGuideChk.checked = d.ui_tree_guide !== false;
  const uiParentSepChk = $("#uiParentSepChk");
  if (uiParentSepChk) uiParentSepChk.checked = d.ui_parent_sep === true;
  const uiMultiselectExitChk = $("#uiMultiselectExitChk");
  if (uiMultiselectExitChk) uiMultiselectExitChk.checked = d.ui_multiselect_exit_after_add !== false;
  const uiMultiselectDoubleEscChk = $("#uiMultiselectDoubleEscChk");
  if (uiMultiselectDoubleEscChk) uiMultiselectDoubleEscChk.checked = d.ui_multiselect_double_esc !== false;
  const uiToastInfoBase = $("#uiToastInfoBase");
  if (uiToastInfoBase) uiToastInfoBase.value = String(_normalizeToastSeconds(d.ui_toast_info_base, 2));
  const uiToastInfoMax = $("#uiToastInfoMax");
  if (uiToastInfoMax) uiToastInfoMax.value = String(_normalizeToastSeconds(d.ui_toast_info_max, 20));
  const uiToastWarnBase = $("#uiToastWarnBase");
  if (uiToastWarnBase) uiToastWarnBase.value = String(_normalizeToastSeconds(d.ui_toast_warn_base, 5));
  const uiToastWarnMax = $("#uiToastWarnMax");
  if (uiToastWarnMax) uiToastWarnMax.value = String(_normalizeToastSeconds(d.ui_toast_warn_max, 30));
  fillBlacklistSelect();
  updateDataFolderLine(d);
  updateSnapDirLine({
    snapshot_dir: d.snapshot_dir_display || d.snapshot_dir_builtin || "",
    snapshot_dir_is_custom: !!d.snapshot_dir_is_custom,
  });
  // 这一整段都是程序在填值（含换数据文件夹后的重填），
  // 把填完的那一刻当作初始状态，否则关页会误报未保存
  _settingsRefreshSnapshotIfUntouched();
}

/** 数据文件夹行：当前位置、当前运行账户与指针记录状态。 */
function updateDataFolderLine(s) {
  const pathEl = $("#dataFolderPathLine");
  if (pathEl) {
    const path = (s && s.app_data_dir) || "";
    pathEl.textContent = path ? t("dataFolderPath", path) : "";
    pathEl.title = path;
  }
  const accountEl = $("#runAccountLine");
  if (accountEl) {
    const account = (s && s.account) || "";
    accountEl.textContent = account ? t("runAccountLine", account) : "";
    accountEl.classList.toggle("hidden", !account);
  }
  const warnEl = $("#dataFolderWarnLine");
  if (warnEl) {
    const invalid = !!(s && s.data_dir_pointer_invalid);
    warnEl.textContent = invalid ? t("dataFolderPointerInvalid") : "";
    warnEl.classList.toggle("hidden", !invalid);
  }
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

/** 启动时检查数据目录；正常时再检查其他账户下的旧数据。 */
async function checkStorageAtStartup() {
  if (!state.api || !state.api.get_storage_status) return true;
  let res = null;
  try {
    res = await state.api.get_storage_status();
  } catch (e) {
    return true;
  }
  if (!res) return true;
  if (!res.ok || res.snapshot_dir_unavailable) {
    showStorageRecovery(res);
    return false;
  }
  await checkLegacyDataAtStartup();
  return true;
}

let _legacyDataState = null;

async function checkLegacyDataAtStartup() {
  if (!state.api || !state.api.get_legacy_data) return;
  let res = null;
  try {
    res = await state.api.get_legacy_data();
  } catch (e) {
    return;
  }
  const list = res && Array.isArray(res.candidates) ? res.candidates : [];
  if (list.length) showLegacyData(list);
}

function showLegacyData(list) {
  _legacyDataState = list;
  const ov = $("#legacyDataOverlay");
  if (!ov) return;
  const item = list[0] || {};
  const account = item.account || "";
  const count = Number(item.snapshots) || 0;
  const body = $("#legacyDataBody");
  if (body) {
    body.textContent = item.readable
      ? t("legacyDataBody", account, count)
      : t("legacyDataUnreadable", account);
  }
  const pathEl = $("#legacyDataPath");
  if (pathEl) {
    pathEl.textContent = t("legacyDataPath", item.path || "");
    pathEl.title = item.path || "";
  }
  const importBtn = $("#legacyDataImportBtn");
  if (importBtn) {
    importBtn.disabled = !item.readable || !(item.has_settings || count);
  }
  const openBtn = $("#legacyDataOpenBtn");
  if (openBtn) openBtn.disabled = !item.path;
  ov.classList.remove("hidden");
}

function hideLegacyData() {
  const ov = $("#legacyDataOverlay");
  if (ov) ov.classList.add("hidden");
  _legacyDataState = null;
}

let _legacyConflictState = null;

async function importLegacyData() {
  const item = (_legacyDataState || [])[0];
  if (!item || !item.path) return;
  if (!state.api || !state.api.scan_legacy_conflicts) {
    await runLegacyImport({}, item.path);
    return;
  }
  let res = null;
  try {
    res = await state.api.scan_legacy_conflicts(item.path);
  } catch (e) {
    toast(t("legacyConflictScanFailed"), true);
    return;
  }
  if (!res || res.error) {
    toast((res && res.error) || t("legacyConflictScanFailed"), true);
    return;
  }
  const settings = Array.isArray(res.settings) ? res.settings : [];
  const snapshots = Array.isArray(res.snapshots) ? res.snapshots : [];
  if (!settings.length && !snapshots.length) {
    await runLegacyImport({}, item.path);
    return;
  }
  showLegacyConflicts(item.path, settings, snapshots);
}

function _legacyConflictRow(item) {
  const row = document.createElement("div");
  row.className = "legacy-conflict-row";
  row.setAttribute("data-relative", item.relative || "");
  const name = document.createElement("div");
  name.className = "legacy-conflict-name";
  name.textContent = item.name || "";
  row.appendChild(name);
  const group = `legacy-conflict-${item.relative || item.name || ""}`;
  row.appendChild(
    _legacyChoice(group, "current", t("legacyConflictCurrent"), item.current || {}, true)
  );
  row.appendChild(
    _legacyChoice(group, "legacy", t("legacyConflictLegacy"), item.legacy || {}, false)
  );
  return row;
}

function _legacyChoice(group, value, label, info, checked) {
  const wrap = document.createElement("label");
  wrap.className = "legacy-conflict-choice";
  const input = document.createElement("input");
  input.type = "radio";
  input.name = group;
  input.value = value;
  input.checked = checked;
  wrap.appendChild(input);
  const text = document.createElement("span");
  text.textContent = t(
    "legacyConflictMeta",
    label,
    fmtBytes(Number(info.size) || 0),
    _legacyTime(info.mtime)
  );
  wrap.appendChild(text);
  return wrap;
}

function _legacyTime(seconds) {
  if (!seconds) return "";
  try {
    return new Date(Number(seconds) * 1000).toLocaleString();
  } catch (e) {
    return "";
  }
}

function showLegacyConflicts(path, settings, snapshots) {
  _legacyConflictState = { path, settings, snapshots };
  const settingsWrap = $("#legacyConflictSettings");
  const snapshotsWrap = $("#legacyConflictSnapshots");
  if (settingsWrap) {
    settingsWrap.innerHTML = "";
    settings.forEach((item) => settingsWrap.appendChild(_legacyConflictRow(item)));
    if (!settings.length) settingsWrap.appendChild(_legacyConflictEmpty());
  }
  if (snapshotsWrap) {
    snapshotsWrap.innerHTML = "";
    snapshots.forEach((item) => snapshotsWrap.appendChild(_legacyConflictRow(item)));
    if (!snapshots.length) snapshotsWrap.appendChild(_legacyConflictEmpty());
  }
  const ov = $("#legacyConflictOverlay");
  if (ov) ov.classList.remove("hidden");
}

function _legacyConflictEmpty() {
  const empty = document.createElement("p");
  empty.className = "legacy-conflict-empty";
  empty.textContent = t("legacyConflictEmpty");
  return empty;
}

function hideLegacyConflicts() {
  const ov = $("#legacyConflictOverlay");
  if (ov) ov.classList.add("hidden");
  _legacyConflictState = null;
}

function _setAllLegacyChoices(value) {
  document
    .querySelectorAll(
      "#legacyConflictSettings input[type=radio], #legacyConflictSnapshots input[type=radio]"
    )
    .forEach((input) => {
      if (input.value === value) input.checked = true;
    });
}

function _collectLegacyChoices() {
  const choices = {};
  document.querySelectorAll(".legacy-conflict-row").forEach((row) => {
    const relative = row.getAttribute("data-relative");
    const checked = row.querySelector("input[type=radio]:checked");
    if (relative && checked) choices[relative] = checked.value;
  });
  return choices;
}

async function startLegacyImport() {
  const data = _legacyConflictState;
  if (!data || !data.path) return;
  const choices = _collectLegacyChoices();
  hideLegacyConflicts();
  await runLegacyImport(choices, data.path);
}

async function runLegacyImport(choices, path) {
  const item = path ? { path } : (_legacyDataState || [])[0];
  if (!item || !item.path) return;
  let res = null;
  try {
    res = await state.api.import_legacy_data(item.path, choices || {});
  } catch (e) {
    toast(t("legacyDataImportFailed"), true);
    return;
  }
  if (!res || res.error) {
    toast((res && res.error) || t("legacyDataImportFailed"), true);
    return;
  }
  hideLegacyData();
  const copied = Number(res.copied) || 0;
  const overwritten = Number(res.overwritten) || 0;
  const skipped = Number(res.skipped) || 0;
  const failed = Number(res.failed) || 0;
  toast(
    t("legacyDataImported", copied, overwritten, skipped, failed),
    failed ? "warn" : "info"
  );
  await refreshAfterStorageChange();
}

async function openLegacyDataPath() {
  const item = (_legacyDataState || [])[0];
  if (!item || !item.path || !state.api || !state.api.open_folder) return;
  let res = null;
  try {
    res = await state.api.open_folder(item.path);
  } catch (e) {
    res = null;
  }
  if (!res || res.error) toast(t("legacyDataOpenFailed"), true);
}

let _storageRecoveryState = null;

function showStorageRecovery(res) {
  _storageRecoveryState = res || {};
  const ov = $("#storageRecoveryOverlay");
  const body = $("#storageRecoveryBody");
  if (!ov || !body) return;
  const snapIssue = !!(res && res.snapshot_dir_unavailable);
  const title = $("#storageRecoveryTitle");
  if (title) {
    const key = snapIssue ? "storageRecoverySnapTitle" : "storageRecoveryTitle";
    title.setAttribute("data-i18n", key);
    title.textContent = t(key);
  }
  body.textContent = String((res && res.message) || t("storageRecoveryBody"));
  const openBtn = $("#storageRecoveryOpenBtn");
  if (openBtn) openBtn.disabled = !(res && res.path);
  const snapBtn = $("#storageRecoverySnapBtn");
  if (snapBtn) snapBtn.classList.toggle("hidden", !snapIssue);
  const chooseBtn = $("#storageRecoveryChooseBtn");
  if (chooseBtn) chooseBtn.classList.toggle("hidden", snapIssue);
  ov.classList.remove("hidden");
}

function hideStorageRecovery() {
  const ov = $("#storageRecoveryOverlay");
  if (ov) ov.classList.add("hidden");
  _storageRecoveryState = null;
}

async function chooseStorageRecoveryFolder() {
  let res;
  try {
    res = await state.api.choose_data_folder();
  } catch (e) {
    toast(t("folderPickFailed"), true);
    return;
  }
  if (!res || res.cancelled) return;
  if (res.error) {
    toast(res.error, true);
    return;
  }
  hideStorageRecovery();
  const failed = Number(res.failed) || 0;
  const skipped = Number(res.skipped) || 0;
  const moved = Number(res.moved) || 0;
  const previous = res.previous_root || "";
  const parts = [];
  if (res.target_had_config) parts.push(t("storageRecoveryFound"));
  if (moved) parts.push(t("storageRecoveryMovedIn"));
  if (!res.target_had_config && !moved) parts.push(t("storageRecoveryUsingNew"));
  if (failed) parts.push(t("storageRecoveryMoveFailed", failed, previous));
  if (skipped) parts.push(t("storageRecoveryMoveSkipped", skipped, previous));
  if (res.snapshot_dir_reset) parts.push(t("storageRecoverySnapshotReset"));
  if (res.pointer_saved === false) parts.push(t("storageRecoveryPointerFailed"));
  toast(parts.join(" "), res.pointer_saved === false ? "error" : failed ? "warn" : "info");
  await refreshAfterStorageChange();
}

/** 内置快照目录不可用时，直接在恢复面板里换一个可写的快照目录。 */
async function chooseStorageRecoverySnapshotFolder() {
  let res;
  try {
    res = await state.api.choose_snapshot_dir();
  } catch (e) {
    toast(t("folderPickFailed"), true);
    return;
  }
  if (!res || res.cancelled) return;
  if (res.error) {
    toast(res.error, true);
    return;
  }
  hideStorageRecovery();
  toast(t("storageRecoverySnapshotChosen"));
  await refreshAfterStorageChange();
}

async function openStorageRecoveryPath() {
  const stored = _storageRecoveryState || {};
  const path = stored.problem_path || stored.path || "";
  if (!path || !state.api || !state.api.open_folder) return;
  let res = null;
  try {
    res = await state.api.open_folder(path);
  } catch (e) {
    res = null;
  }
  if (!res || res.error) toast(t("storageRecoveryOpenFailed"), true);
}

async function quitFromStorageRecovery() {
  if (state.api && state.api.quit_app) {
    try {
      await state.api.quit_app();
    } catch (e) {}
  }
}

async function refreshAfterStorageChange() {
  if (typeof reconcileLang === "function") {
    try {
      await reconcileLang();
    } catch (e) {}
  }
  try {
    await loadSettings();
  } catch (e) {}
  if (typeof loadSnapshots === "function") loadSnapshots({ quiet: true });
  if (typeof refreshLinkList === "function") refreshLinkList();
  if (typeof refreshMigrationHistoryCount === "function") {
    refreshMigrationHistoryCount();
  }
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
    toast(t("folderPickFailed"), true);
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
  updateSettingsWarnings(null);
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
  updateSettingsWarnings(null);
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
  // 性能页隐藏时画布宽为 0，切过去必须按真实宽度重画一次
  if (id === "perf") _perfSync();
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
    res = await state.api.get_app_log(5000);
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
  // “未保存”判断靠快照：控件值在加载完盘列表后会被程序再改一次，
  // 所以得能区分“程序改的”与“用户改的”，否则什么都不改也会提示未保存。
  _settingsTouched = false;
  _settingsBindTouchWatch();
  // 记下刚打开时的表单状态，关掉时靠它判断有没有改过没保存
  _settingsFormSnapshot = _formFingerprint();
}

/** 用户真的动过控件才置位：程序写 input.value 不会触发事件，所以不会被误置。 */
function _settingsBindTouchWatch() {
  const overlay = $("#settingsOverlay");
  if (!overlay || overlay.dataset.touchBound === "1") return;
  overlay.dataset.touchBound = "1";
  const mark = () => {
    _settingsTouched = true;
  };
  overlay.addEventListener("input", mark, true);
  overlay.addEventListener("change", mark, true);
}

/** 程序自己把表单填好以后重建快照；用户已经动过就不动，避免吞掉改动。 */
function _settingsRefreshSnapshotIfUntouched() {
  const overlay = $("#settingsOverlay");
  if (!overlay || overlay.classList.contains("hidden")) return;
  if (_settingsTouched) return;
  _settingsFormSnapshot = _formFingerprint();
}

/** 表单里所有控件的当前值拼成一串，只用于前后比对。 */
function _formFingerprint() {
  const parts = [];
  const root = $("#settingsOverlay");
  if (!root) return "";
  root.querySelectorAll("input, select, textarea").forEach((el) => {
    const key = el.id || el.name || "";
    if (!key) return;
    let value;
    if (el.type === "radio") {
      // 单选框同一个 name 只记选中的那个，所以值里要带上选项自己的取值，
      // 否则在同一组里换选项时指纹不会变
      if (!el.checked) return;
      value = "r:" + String(el.value);
    } else if (el.type === "checkbox") {
      value = el.checked ? "1" : "0";
    } else {
      value = String(el.value);
    }
    parts.push(key + "=" + value);
  });
  return parts.join("\u0001");
}

/** 点叉或按 Esc 关闭：有改动没保存时先警告一句，再关。 */
function closeSettingsDiscarding() {
  const changed =
    !!_settingsFormSnapshot && _formFingerprint() !== _settingsFormSnapshot;
  closeSettings();
  if (changed) toastWarn(t("settingsUnsaved"));
}

function closeSettings() {
  _settingsFormSnapshot = "";
  $("#settingsOverlay").classList.add("hidden");
  _settingsDraft = null;
  // 按盘编辑值与基线跟着草稿一起丢弃，否则关掉再打开会把已放弃的值写回配置
  _perfInputs = {};
  _perfBaseline = {};
  _perfResetFlags = {};
  // 盘列表要重新拉，拉到之前不允许提交按盘参数
  _perfReady = false;
  // 关页时测试可能还在后台跑：把后续事件当已放弃，
  // 否则它跑完会把结果自动填进下次打开的界面
  if (_perfBusy || _perfPending) {
    _perfAbandoned = true;
    _perfBusy = false;
    _perfPending = false;
  }
  _perfClearGuard();
  const copyPanel = $("#perfCopyPanel");
  if (copyPanel) copyPanel.classList.add("hidden");
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

function migrateDoneText(mig, showFiles) {
  const moved = Number(mig && mig.moved) || 0;
  const skipped = Number(mig && mig.skipped) || 0;
  const failed = Number(mig && mig.failed) || 0;
  if (!moved && !skipped && !failed) return t("snapDirMigrateNone");
  const parts = [];
  if (moved) parts.push(t("snapDirMigrated", moved));
  if (skipped) parts.push(t("snapDirMigrateSkipped", skipped));
  if (failed) {
    parts.push(t("snapDirMigrateFailed", failed));
    if (showFiles) {
      const errors = Array.isArray(mig && mig.errors) ? mig.errors : [];
      const names = errors
        .filter((e) => String(e).includes(":"))
        .slice(0, 5)
        .map((e) => String(e).split(":")[0].trim())
        .filter(Boolean);
      if (names.length) {
        let listed = t("snapDirMigrateNameJoin", names);
        if (failed > names.length) {
          listed += " " + t("snapDirMigrateMore", failed - names.length);
        }
        parts.push(t("snapDirMigrateFailedFiles", listed));
      }
    }
  }
  return t("snapDirMigrateSummary", parts, failed > 0);
}

function toastMigrateDone(mig, showFiles) {
  const failed = Number(mig && mig.failed) || 0;
  toast(migrateDoneText(mig, showFiles), failed > 0);
}

/** 把快照迁移结果写在设置页快照目录下方，供设置保存失败时查看。 */
function renderSnapDirMigrateNote(mig, dest) {
  const el = $("#snapDirMigrateLine");
  if (!el) return;
  const moved = Number(mig && mig.moved) || 0;
  const skipped = Number(mig && mig.skipped) || 0;
  const failed = Number(mig && mig.failed) || 0;
  if (!moved && !skipped && !failed) {
    el.textContent = "";
    el.classList.add("hidden");
    return;
  }
  const target = String(dest || "").trim() || t("snapDir");
  const parts = [];
  if (moved) parts.push(t("snapDirNoteMoved", moved, target));
  if (skipped) parts.push(t("snapDirNoteSkipped", skipped));
  if (failed) parts.push(t("snapDirNoteFailed", failed));
  el.textContent = failed
    ? t("snapDirNoteSummaryFailed", parts, target)
    : t("snapDirNoteSummaryDone", parts, target);
  el.classList.remove("hidden");
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
/** 提交内容与当前生效设置是否完全一致。
 *
 * 用来区分「真的改了」与「点完成但一项没动」：后者不该说设置已应用。
 * 快照目录按界面显示的目录比较，因为提交值里的空串表示沿用默认目录，
 * 与当前生效的目录路径形态不同。AI 配置单独提交，不在这里比较。
 */
function _settingsPayloadUnchanged(payload) {
  const cur = state._settings || {};
  const normDir = (p) =>
    String(p || "").replace(/[\\/]+$/, "").toLowerCase();
  const prevDir = normDir(cur.snapshot_dir);
  const draft = _settingsDraft || {};
  const nextDir = normDir(
    draft.snapshot_dir_is_custom
      ? draft.snapshot_dir_display || draft.snapshot_dir
      : draft.snapshot_dir_builtin
  );
  if (prevDir !== nextDir) return false;
  for (const key of Object.keys(payload)) {
    if (key === "snapshot_dir") continue;
    // 按盘参数在提交侧是拼接串、在生效侧是对象，两边取字符串永远不相等；
    // 前端只提交真改过的盘，所以提交串非空本身就意味着有改动
    if (key === "drives") {
      if (String(payload[key] || "").trim() !== "") return false;
      continue;
    }
    const a = payload[key];
    const b = cur[key];
    if (key === "delete_blacklist") {
      if (!_sameDeleteBlacklist(a, b)) return false;
    } else if (typeof a === "boolean" || typeof b === "boolean") {
      if (!!a !== !!b) return false;
    } else if (typeof a === "number" || typeof b === "number") {
      if (Number(a) !== Number(b)) return false;
    } else if (String(a ?? "") !== String(b ?? "")) {
      return false;
    }
  }
  return true;
}

/** 删除白名单按路径与模式规范化后比较，忽略顺序和大小写。 */
function _sameDeleteBlacklist(a, b) {
  const norm = (items) => {
    const out = [];
    for (const it of Array.isArray(items) ? items : []) {
      if (!it || typeof it !== "object") continue;
      out.push(
        String(it.path || "").trim().toLowerCase() +
          "\u0000" +
          String(it.mode || "prefix").trim().toLowerCase()
      );
    }
    return out.sort().join("\u0001");
  };
  return norm(a) === norm(b);
}

async function applySettingsAndClose() {
  if (!_settingsDraft) {
    closeSettings();
    return;
  }
  renderSnapDirMigrateNote(null);
  // 从表单再读一遍，防止漏绑 change
  const compressChk = $("#compressChk");
  const mftChk = $("#mftChk");
  const searchMemIdxChk = $("#searchMemIdxChk");
  const rememberWinSizeChk = $("#rememberWinSizeChk");
  const tempCleanupChk = $("#tempCleanupChk");
  const autoCheckUpdateChk = $("#autoCheckUpdateChk");
  const updateBadgeChk = $("#updateBadgeChk");
  const logSanitizeChk = $("#logSanitizeChk");
  const logLevelSel = $("#logLevelSel");
  const devToolsChk = $("#devToolsChk");
  const uiParentStyleChk = $("#uiParentStyleChk");
  const uiHoverOutlineChk = $("#uiHoverOutlineChk");
  const payload = {
    // 扫描线程数不再单独暴露，未设过目录遍历线程数的盘仍以它作默认值
    scan_workers: Number(_settingsDraft.scan_workers) || 1,
    mft_procs: Number(_settingsDraft.mft_procs) || 0,
    compress_snapshots: compressChk ? !!compressChk.checked : _settingsDraft.compress_snapshots,
    use_mft: mftChk ? !!mftChk.checked : _settingsDraft.use_mft,
    search_memory_index: searchMemIdxChk
      ? !!searchMemIdxChk.checked
      : (_settingsDraft.search_memory_index !== false),
    remember_window_size: rememberWinSizeChk
      ? !!rememberWinSizeChk.checked
      : (_settingsDraft.remember_window_size !== false),
    temp_cleanup: tempCleanupChk
      ? !!tempCleanupChk.checked
      : (_settingsDraft.temp_cleanup !== false),
    auto_check_updates: autoCheckUpdateChk
      ? !!autoCheckUpdateChk.checked
      : (_settingsDraft.auto_check_updates !== false),
    update_badge: updateBadgeChk
      ? !!updateBadgeChk.checked
      : (_settingsDraft.update_badge !== false),
    log_sanitize: logSanitizeChk
      ? !!logSanitizeChk.checked
      : (_settingsDraft.log_sanitize !== false),
    log_level: logLevelSel
      ? _normalizeLogLevel(logLevelSel.value)
      : _normalizeLogLevel(_settingsDraft.log_level),
    dev_tools: devToolsChk ? !!devToolsChk.checked : _settingsDraft.dev_tools === true,
    ui_parent_row_bg: $("#uiParentBgChk")
      ? !!$("#uiParentBgChk").checked
      : (_settingsDraft.ui_parent_row_bg === true),
    size_source: (() => {
      const el = document.querySelector('input[name="sizeSource"]:checked');
      return el ? el.value : _normalizeSizeSource(_settingsDraft.size_source);
    })(),
    ui_parent_row_dim: $("#uiParentDimChk")
      ? !!$("#uiParentDimChk").checked
      : (_settingsDraft.ui_parent_row_dim === true),
    ui_hover_group_outline: uiHoverOutlineChk
      ? !!uiHoverOutlineChk.checked
      : (_settingsDraft.ui_hover_group_outline === true),
    ui_hover_group_min: $("#uiHoverGroupMin")
      ? _normalizeHoverGroupMin($("#uiHoverGroupMin").value)
      : _normalizeHoverGroupMin(_settingsDraft.ui_hover_group_min),
    ui_tree_guide: $("#uiTreeGuideChk")
      ? !!$("#uiTreeGuideChk").checked
      : _settingsDraft.ui_tree_guide !== false,
    ui_parent_sep: $("#uiParentSepChk")
      ? !!$("#uiParentSepChk").checked
      : _settingsDraft.ui_parent_sep === true,
    ui_multiselect_exit_after_add: $("#uiMultiselectExitChk")
      ? !!$("#uiMultiselectExitChk").checked
      : _settingsDraft.ui_multiselect_exit_after_add !== false,
    ui_multiselect_double_esc: $("#uiMultiselectDoubleEscChk")
      ? !!$("#uiMultiselectDoubleEscChk").checked
      : _settingsDraft.ui_multiselect_double_esc !== false,
    ui_toast_info_base: $("#uiToastInfoBase")
      ? _normalizeToastSeconds($("#uiToastInfoBase").value, 2)
      : _normalizeToastSeconds(_settingsDraft.ui_toast_info_base, 2),
    ui_toast_info_max: $("#uiToastInfoMax")
      ? _normalizeToastSeconds($("#uiToastInfoMax").value, 20)
      : _normalizeToastSeconds(_settingsDraft.ui_toast_info_max, 20),
    ui_toast_warn_base: $("#uiToastWarnBase")
      ? _normalizeToastSeconds($("#uiToastWarnBase").value, 5)
      : _normalizeToastSeconds(_settingsDraft.ui_toast_warn_base, 5),
    ui_toast_warn_max: $("#uiToastWarnMax")
      ? _normalizeToastSeconds($("#uiToastWarnMax").value, 30)
      : _normalizeToastSeconds(_settingsDraft.ui_toast_warn_max, 30),
    snapshot_dir: _settingsDraft.snapshot_dir_is_custom
      ? (_settingsDraft.snapshot_dir || "")
      : "",
    delete_blacklist: Array.isArray(_settingsDraft.delete_blacklist)
      ? _settingsDraft.delete_blacklist
      : [],
  };
  // 按盘参数只下发真改过的盘；没改过或还忙不过来时不下发这个键，
  // 后端就不会动已存的按盘参数
  const drivesPayload = _perfDrivesPayload();
  if (drivesPayload !== null) payload.drives = drivesPayload;
  // 本次新测出结果才捎上，没测过就不带，避免干扰「设置有没有改动」的判断
  const testsPayload = _perfTestsPayload();
  if (JSON.stringify(testsPayload) !== _perfTestsBase) {
    payload.perf_tests = testsPayload;
  }
  payload.ui_toast_info_max = Math.max(payload.ui_toast_info_max, payload.ui_toast_info_base);
  payload.ui_toast_warn_max = Math.max(payload.ui_toast_warn_max, payload.ui_toast_warn_base);

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

  const prevSizeSource = _normalizeSizeSource(
    (state._settings && state._settings.size_source) || "actual"
  );

  // 提交前先比一遍：与当前生效设置没有差异时，提示语换成「没有改动」
  const unchanged = _settingsPayloadUnchanged(payload);

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
    applyUiSettingFlags(state._settings);
    applyToastTiming(state._settings);
    toast(t(unchanged ? "settingsUnchanged" : "settingsApplied"));
    if (payload.use_mft && state._settings && !state._settings.is_admin) {
      toastWarn(t("mftNeedAdmin"));
    }
    if (kick && kick.snapshot_dir_changed) {
      toastMigrateDone(kick.migrate || {});
      loadSnapshots();
    }
    await _rerunAfterSettingsApplied(prevSizeSource);
    return;
  }

  const res = await waiter.promise;
  if (doneBtn) doneBtn.disabled = false;
  hideMigrateOverlay();

  if (!res || res.cancelled) return;
  if (res.ok === false || res.error) {
    const msg = res.error || t("settingsApplyFailed");
    const mig = res.migrate || {};
    const moved = Number(mig.moved) || 0;
    const skipped = Number(mig.skipped) || 0;
    const failed = Number(mig.failed) || 0;
    if (moved || skipped || failed) {
      renderSnapDirMigrateNote(mig, draftDir);
      await openSettings("general");
      if (_settingsDraft) {
        _settingsDraft.snapshot_dir = draftDir || payload.snapshot_dir || "";
        _settingsDraft.snapshot_dir_display = draftDir;
        _settingsDraft.snapshot_dir_is_custom = !!payload.snapshot_dir;
        updateSnapDirLine(_settingsDraft);
      }
    }
    toast(msg, true);
    return;
  }

  state._settings = Object.assign(state._settings || {}, res || {});
  if (Object.prototype.hasOwnProperty.call(payload, "search_memory_index")) {
    state.searchMemoryIndex = !!payload.search_memory_index;
  } else if (Object.prototype.hasOwnProperty.call(res || {}, "search_memory_index")) {
    state.searchMemoryIndex = res.search_memory_index !== false;
  }
  applyUiSettingFlags(state._settings);
  applyToastTiming(state._settings);
  const dirChanged = !!(res && res.snapshot_dir_changed);
  const mig = (res && res.migrate) || {};

  toast(t(unchanged && !dirChanged ? "settingsUnchanged" : "settingsApplied"));
  if (payload.use_mft && state._settings && !state._settings.is_admin) {
    toastWarn(t("mftNeedAdmin"));
  }
  if (dirChanged) {
    toastMigrateDone(mig, true);
    loadSnapshots();
  }
  await _rerunAfterSettingsApplied(prevSizeSource);
}

/**
 * 设置保存后重新拉一次当前视图。
 *
 * 取数口径变了，后端下次取数会换一个会话，不重画的话会出现顶部还是
 * 旧口径、新展开的子节点已经是新口径的半屏混合状态。
 */
async function _rerunAfterSettingsApplied(prevSource) {
  const now = _normalizeSizeSource(
    (state._settings && state._settings.size_source) || "actual"
  );
  if (now === prevSource) return;
  // 侧栏卡片上的数字也跟口径走，它不依赖是否已打开对比，所以先无条件重画。
  // 纯 DOM 重绘，不读盘。
  if (typeof renderSnapshotList === "function") {
    try {
      renderSnapshotList();
    } catch (e) {}
  }
  if (!state.compared || !state.oldPath || !state.newPath) return;
  // 重画会重置筛选与搜索，但用户只是换了取数口径，不是换了快照对。
  // 筛选条件本身与口径无关（例如「只看变大」），先存下来、画完再放回。
  const savedFilter = {
    filter: state.filter,
    filterTime: state.filterTime,
    filterTimeDays: state.filterTimeDays,
    filterDeltaOp: state.filterDeltaOp,
    filterDeltaVal: state.filterDeltaVal,
    filterDeltaUnit: state.filterDeltaUnit,
  };
  // 搜索框内容也要留：重画会把它清空并收起，用户只是换了口径，
  // 不该丢掉刚输入的关键词。只填回内容，不自动重发查询。
  const searchInput = $("#treeSearchInput");
  const searchWrap = $("#treeSearchWrap");
  const savedQuery = searchInput ? searchInput.value : "";
  const searchWasOpen = !!(
    searchWrap && searchWrap.classList.contains("is-open")
  );
  try {
    if (state.oldPath === state.newPath) {
      await browseSnapshot(state.oldPath);
    } else {
      await doCompare();
    }
    Object.assign(state, savedFilter);
    if (state.filter !== "all" && typeof applyFilterToTree === "function") {
      applyFilterToTree();
    }
    if (savedQuery) {
      const input = $("#treeSearchInput");
      if (input) input.value = savedQuery;
      if (searchWasOpen && searchWrap) searchWrap.classList.add("is-open");
      if (typeof syncTreeSearchChrome === "function") syncTreeSearchChrome();
    }
  } catch (e) {
    // 重画失败不影响设置已保存这一事实，静默保留旧画面
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
  const prevSource = _normalizeSizeSource(
    (state._settings && state._settings.size_source) || "actual"
  );
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
  applyThemeValue(res.theme);
  applyUiSettingFlags(res);
  applyToastTiming(res);
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
    temp_cleanup: res.temp_cleanup !== false,
    auto_check_updates: res.auto_check_updates !== false,
    update_badge: res.update_badge !== false,
    log_sanitize: res.log_sanitize !== false,
    log_level: _normalizeLogLevel(res.log_level),
    dev_tools: !!res.dev_tools,
    snapshot_dir: "",
    snapshot_dir_display: res.snapshot_dir || res.snapshot_dir_builtin || "",
    snapshot_dir_builtin: res.snapshot_dir_builtin || "",
    snapshot_dir_is_custom: false,
    settings_path: res.settings_path || "",
    mft_platform_ok: res.mft_platform_ok !== false,
    is_admin: !!res.is_admin,
    cpu_count: res.cpu_count,
    delete_blacklist: _normalizeBlacklistDraft(res.delete_blacklist),
    theme: _normalizeUiTheme(res.theme),
    size_source: _normalizeSizeSource(res.size_source),
    ui_parent_row_bg: res.ui_parent_row_bg === true,
    ui_parent_row_dim: res.ui_parent_row_dim === true,
    ui_hover_group_outline: res.ui_hover_group_outline === true,
    ui_hover_group_min: _normalizeHoverGroupMin(res.ui_hover_group_min),
    ui_tree_guide: res.ui_tree_guide !== false,
    ui_parent_sep: res.ui_parent_sep === true,
    ui_toast_info_base: _normalizeToastSeconds(res.ui_toast_info_base, 2),
    ui_toast_info_max: _normalizeToastSeconds(res.ui_toast_info_max, 20),
    ui_toast_warn_base: _normalizeToastSeconds(res.ui_toast_warn_base, 5),
    ui_toast_warn_max: _normalizeToastSeconds(res.ui_toast_warn_max, 30),
  };
  fillSettingsFormFromDraft();
  updateSettingsWarnings(res);
  toast(t("resetSettingsDone"));
  // 恢复默认也会把口径改回实际占用，与保存设置走同一套重画判断
  await _rerunAfterSettingsApplied(prevSource);
}

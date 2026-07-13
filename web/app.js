/* ============================================================
   WhoShitsOnMyC 前端逻辑
   通过 window.pywebview.api 调用 Python 后端；后端事件经
   window.__onPyEvent 回调推来（见 app.py 的 Api._emit）。
   ============================================================ */

"use strict";

// ---- 全局状态 ----
const state = {
  api: null,
  snapshots: [],      // 快照摘要列表（默认目录 ∪ 手动导入）
  folders: [],        // 快照根下一层归纳文件夹名
  // 折叠的文件夹名 → true；根区用 "" 键
  folderCollapsed: {},
  importedPaths: {},  // 手动导入的路径集合 path → true（刷新时保留）
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
    snapSortTimeDesc: "时间 ↓",
    snapSortTimeAsc: "时间 ↑",
    snapSortNameAsc: "路径 A→Z",
    snapSortNameDesc: "路径 Z→A",
    openDir: "打开文件夹",
    openDirTitle: "打开快照存放目录",
    snapToolsTitle: "快照目录与导入",
    refreshSnapsTitle: "从默认目录刷新列表（保留手动导入的快照）",
    importSnapsTitle: "从其它位置读取快照文件（.db / .dbz）",
    refreshDone: (n) => `已刷新默认目录（${n} 项）`,
    importDone: (n) => `已读入 ${n} 个快照`,
    importNone: "未选择文件",
    importFailed: (n) => `${n} 个文件无法读取`,
    importDup: (n) => `其中 ${n} 个已在列表中`,
    importResultTitle: "导入结果",
    importResultOk: "知道了",
    importDupHeading: "已存在（未重复加入）",
    importFailHeading: "无法读取",
    importSummaryLine: (added, dup, fail) =>
      `新增 ${added} 个 · 重复 ${dup} 个 · 失败 ${fail} 个`,
    importDupPath: "路径已在列表中",
    importDupContent: "内容与列表中某快照相同",
    importDupBoth: "路径与内容均已存在",
    noteEdit: "备注",
    notePlaceholder: "添加备注",
    noteSaved: "备注已保存",
    noteCleared: "备注已清除",
    noteSaving: "正在保存备注",
    noteTitle: "点击编辑备注（写入快照文件内，随文件移动/复制）",
    noteSave: "保存",
    noteFor: (root) => `快照：${root}`,
    folderNew: "新建文件夹",
    folderNewTitle: "在快照目录下新建文件夹，用于归纳快照",
    folderNamePlaceholder: "文件夹名称",
    folderNameInvalid: "文件夹名称无效",
    folderCreated: (n) => `已创建文件夹：${n}`,
    folderCreateFailed: (e) => `创建失败：${e}`,
    folderRename: "重命名",
    folderRenamed: (n) => `已重命名为：${n}`,
    folderRenameFailed: (e) => `重命名失败：${e}`,
    folderDelete: "删除文件夹",
    folderDeleteConfirm: (n) => `删除文件夹「${n}」？`,
    folderDeleteConfirmWithItems: (n, c) =>
      `文件夹「${n}」内有 ${c} 个快照，删除后这些快照将一并移除，确定继续？`,
    folderDeleteConfirmAgain: (n) =>
      `再次确认：删除「${n}」及其中全部快照，且不可恢复`,
    folderDeleted: "已删除文件夹",
    folderDeleteFailed: (e) => `删除失败：${e}`,
    folderMove: "移动",
    folderMoveTitle: "选择目标文件夹",
    folderMoveRoot: "（未归类）",
    folderMoved: "已移动",
    folderMoveFailed: (e) => `移动失败：${e}`,
    folderEmpty: "此文件夹暂无快照",
    folderUnfiled: "未归类",
    folderCount: (n) => `${n} 项`,
    folderDialogCreateTitle: "新建文件夹",
    folderDialogRenameTitle: "重命名文件夹",
    folderDialogMoveTitle: "移到文件夹",
    folderSave: "确定",
    scanElapsedInit: "已用时 0:00",
    scanElapsed: (s) => `已用时 ${s}`,
    settings: "设置",
    settingsTitle: "扫描与应用选项",
    settingsClose: "关闭",
    settingsDone: "完成",
    settingsTabGeneral: "通用",
    settingsTabLog: "日志",
    logPrivacyHint: "日志默认不保存，仅存在于软件本次运行中，可使用导出功能将日志输出",
    logPrivacyNote: "隐私说明：导出的日志文件通常不会包含您的隐私信息，并已对相关信息进行脱敏，若担心隐私问题可根据源码自行判断",
    logRefresh: "刷新",
    logClear: "清空",
    logExport: "导出",
    logEmpty: "（暂无日志）",
    logCount: (n) => `共 ${n} 条`,
    logCleared: "日志已清空",
    logExported: "日志已导出",
    logExportFail: "导出日志失败",
    uninstall: "卸载",
    uninstallBtn: "卸载",
    uninstallTitle: "清理工具在本机留下的配置文件与快照（不包含您自定义的快照存放位置）",
    uninstallDialogTitle: "卸载",
    uninstallDeleteData: "同时删除数据文件夹（快照与设置）",
    uninstallConfirm: "确认卸载",
    uninstallDoneTitle: "卸载完成",
    uninstallDoneBody: "数据已清除，关闭程序后可直接删除程序",
    uninstallDoneOk: "确认",
    uninstallPartial: "部分数据未能清除，请稍后在本机检查后再删除程序",
    migrating: "正在迁移快照",
    migrateProgress: (done, total) => `已处理 ${done} / ${total}`,
    migrateProgressName: (done, total, name) => `已处理 ${done} / ${total} · ${name}`,
    snapDirMigrated: (n) => `迁移完成：已移动 ${n} 个快照到新目录`,
    snapDirMigratePartial: (moved, skipped, failed) =>
      `迁移完成：移动 ${moved} · 跳过 ${skipped} · 失败 ${failed}`,
    snapDirMigrateNone: "迁移完成：没有需要移动的快照",
    settingsApplyFailed: "应用设置失败",
    settingsApplyTimeout: "应用设置超时，请重试",
    workers: "扫描线程数",
    workersTitle: "扫描用的并行线程数，机械硬盘建议 1，SSD 可加大",
    compress: "压缩快照",
    compressTitle: "扫描完成后压缩快照以节省磁盘；对比时再解压",
    compressOn: "已开启快照压缩，下次扫描生效",
    compressOff: "已关闭快照压缩，下次扫描生效",
    mft: "尝试 MFT 扫描",
    mftTitle: "盘符根 + NTFS 时读 MFT 构建快照，需要管理员。解析进程数按 CPU 与数据量自动决定，与上方扫描线程数无关。非管理员或失败时自动回退常规扫描，默认开",
    mftHint: "仅 Windows 盘符根 + NTFS，需要管理员。非管理员时勾选仍可保留，扫描会回退常规目录扫描。解析用多进程，不占用上方扫描线程设置",
    mftOn: "已开启 MFT 尝试，下次盘符根扫描生效（需管理员）",
    mftOff: "已关闭 MFT，使用常规目录扫描",
    mftNeedAdmin: "当前非管理员，盘符根将回退常规扫描；建议以管理员身份启动以使用 MFT",
    recommendAdminToast: "当前为非管理员模式，建议以管理员身份启动",
    settingsAutoSaveHint: "修改后点「完成」会自动保存，语言与主题切换会立即保存",
    resetSettings: "恢复默认",
    resetSettingsBtn: "恢复默认",
    resetSettingsTitle: "删除配置并恢复默认",
    resetSettingsConfirm: "确定恢复默认设置？将删除配置文件，并重置线程、压缩、MFT、主题、语言与快照目录",
    resetSettingsDone: "已恢复默认设置",
    resetSettingsFailed: "恢复默认失败",
    settingsApplied: "设置已应用",
    snapDir: "快照存放目录",
    snapDirTitle: "新建扫描写入的目录；更改后点完成会把原目录中的快照迁到新目录（同名已存在则跳过）",
    snapDirChoose: "更改",
    snapDirReset: "恢复默认",
    snapDirOpen: "打开",
    snapDirSet: "已更新快照存放目录",
    snapDirResetDone: "已恢复默认快照目录",
    snapDirBuiltin: (p) => `默认：${p}`,
    snapDirCustom: (p) => `自定义：${p}`,
    decompressing: "正在解压快照",
    tagCompressed: "压缩",
    themeTitle: "切换暗色/浅色",
    langTitle: "切换语言 / Switch language",
    githubStar: "Star",
    githubStarTitle: "在 GitHub 上 Star 本项目",
    pickOldLabel: "基准（较早的快照）",
    pickNewLabel: "当前（较新的快照）",
    pickPlaceholder: "点击选择",
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
    scanning: "正在扫描",
    scanFilesInit: "已扫描 0 个文件",
    scanMftModeHint: "目标目录为根盘符且已启用 MFT 设置项，将读取 MFT 构建快照",
    scanMftFallbackNonAdmin: "当前为非管理员模式，已回退为常规扫描",
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
    comparing: "对比中",
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
    loading: "加载中",
    loadFailed: (e) => `加载失败：${e}（收起后可重试）`,
    noMatchChild: "此目录下无匹配当前筛选的变化。",
    scannedFiles: (n) => `已扫描 ${n} 个文件`,
    scanDone: "扫描完成，已保存快照",
    scanDoneWithTime: (s) => `扫描完成，已保存快照，用时 ${s}`,
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
    snapSortTimeDesc: "Time ↓",
    snapSortTimeAsc: "Time ↑",
    snapSortNameAsc: "Path A→Z",
    snapSortNameDesc: "Path Z→A",
    openDir: "Open folder",
    openDirTitle: "Open the snapshot folder",
    snapToolsTitle: "Snapshot folder and import",
    refreshSnapsTitle: "Refresh from default folder (keep manually imported snapshots)",
    importSnapsTitle: "Open snapshot files from elsewhere (.db / .dbz)",
    refreshDone: (n) => `Refreshed default folder (${n} items)`,
    importDone: (n) => `Imported ${n} snapshot(s)`,
    importNone: "No file selected",
    importFailed: (n) => `${n} file(s) could not be read`,
    importDup: (n) => `${n} already in the list`,
    importResultTitle: "Import result",
    importResultOk: "OK",
    importDupHeading: "Already present (not added again)",
    importFailHeading: "Could not read",
    importSummaryLine: (added, dup, fail) =>
      `Added ${added} · Duplicates ${dup} · Failed ${fail}`,
    importDupPath: "Path already in the list",
    importDupContent: "Same content as a snapshot already listed",
    importDupBoth: "Path and content already present",
    noteEdit: "Note",
    notePlaceholder: "Add a note",
    noteSaved: "Note saved",
    noteCleared: "Note cleared",
    noteSaving: "Saving note",
    noteTitle: "Click to edit note (stored inside the snapshot file; travels with move/copy)",
    noteSave: "Save",
    noteFor: (root) => `Snapshot: ${root}`,
    folderNew: "New folder",
    folderNewTitle: "Create a folder under the snapshot directory to group snapshots",
    folderNamePlaceholder: "Folder name",
    folderNameInvalid: "Invalid folder name",
    folderCreated: (n) => `Folder created: ${n}`,
    folderCreateFailed: (e) => `Create failed: ${e}`,
    folderRename: "Rename",
    folderRenamed: (n) => `Renamed to: ${n}`,
    folderRenameFailed: (e) => `Rename failed: ${e}`,
    folderDelete: "Delete folder",
    folderDeleteConfirm: (n) => `Delete folder “${n}”?`,
    folderDeleteConfirmWithItems: (n, c) =>
      `Folder “${n}” has ${c} snapshot(s). Deleting it will also remove them. Continue?`,
    folderDeleteConfirmAgain: (n) =>
      `Confirm again: delete “${n}” and all snapshots inside. This cannot be undone`,
    folderDeleted: "Folder deleted",
    folderDeleteFailed: (e) => `Delete failed: ${e}`,
    folderMove: "Move",
    folderMoveTitle: "Choose a target folder",
    folderMoveRoot: "(Ungrouped)",
    folderMoved: "Moved",
    folderMoveFailed: (e) => `Move failed: ${e}`,
    folderEmpty: "No snapshots in this folder",
    folderUnfiled: "Ungrouped",
    folderCount: (n) => `${n}`,
    folderDialogCreateTitle: "New folder",
    folderDialogRenameTitle: "Rename folder",
    folderDialogMoveTitle: "Move to folder",
    folderSave: "OK",
    scanElapsedInit: "Elapsed 0:00",
    scanElapsed: (s) => `Elapsed ${s}`,
    settings: "Settings",
    settingsTitle: "Scan and app options",
    settingsClose: "Close",
    settingsDone: "Done",
    settingsTabGeneral: "General",
    settingsTabLog: "Log",
    logPrivacyHint: "Logs are not saved by default. They exist only for this run; use Export to write them out.",
    logPrivacyNote: "Privacy: exported logs usually avoid private data and redact paths. If concerned, review the source yourself.",
    logRefresh: "Refresh",
    logClear: "Clear",
    logExport: "Export",
    logEmpty: "(No log entries yet)",
    logCount: (n) => `${n} entr${n === 1 ? "y" : "ies"}`,
    logCleared: "Log cleared",
    logExported: "Log exported",
    logExportFail: "Failed to export log",
    uninstall: "Uninstall",
    uninstallBtn: "Uninstall",
    uninstallTitle: "Clears config and snapshots this app left on your PC (does not include your custom snapshot folder)",
    uninstallDialogTitle: "Uninstall",
    uninstallDeleteData: "Also delete the data folder (snapshots and settings)",
    uninstallConfirm: "Confirm uninstall",
    uninstallDoneTitle: "Uninstall complete",
    uninstallDoneBody: "Data cleared. After closing the app you can delete the program",
    uninstallDoneOk: "OK",
    uninstallPartial: "Some data could not be cleared. Check your PC before deleting the app",
    migrating: "Migrating snapshots",
    migrateProgress: (done, total) => `Processed ${done} / ${total}`,
    migrateProgressName: (done, total, name) => `Processed ${done} / ${total} · ${name}`,
    snapDirMigrated: (n) => `Migration complete: moved ${n} snapshot(s) to the new folder`,
    snapDirMigratePartial: (moved, skipped, failed) =>
      `Migration complete: moved ${moved} · skipped ${skipped} · failed ${failed}`,
    snapDirMigrateNone: "Migration complete: nothing to move",
    settingsApplyFailed: "Failed to apply settings",
    settingsApplyTimeout: "Applying settings timed out; please try again",
    workers: "Scan threads",
    workersTitle: "Parallel scan threads; use 1 for HDDs, raise for SSDs",
    compress: "Compress snapshots",
    compressTitle: "Compress snapshots after scan to save disk; decompress when comparing",
    compressOn: "Snapshot compression on; effective next scan",
    compressOff: "Snapshot compression off; effective next scan",
    mft: "Try MFT scan",
    mftTitle: "On drive-root NTFS, build snapshot from MFT (administrator required). Parse process count is auto from CPU and data size, independent of scan threads above. Without admin or on failure, falls back to directory scan. On by default",
    mftHint: "Windows drive root + NTFS only; administrator required. The checkbox may stay on without admin — scans fall back to directory walk. Parsing uses auto-sized multi-process pool, not the scan-threads setting",
    mftOn: "MFT attempt on; effective next drive-root scan (admin required)",
    mftOff: "MFT off; use directory scan",
    mftNeedAdmin: "Not running as administrator — drive-root scans use directory mode; run as administrator to use MFT",
    recommendAdminToast: "Not running as administrator; recommend Run as administrator",
    settingsAutoSaveHint: "Changes save when you click Done; language and theme save immediately",
    resetSettings: "Restore defaults",
    resetSettingsBtn: "Restore defaults",
    resetSettingsTitle: "Delete config and restore defaults",
    resetSettingsConfirm: "Restore default settings? This deletes the config file and resets threads, compression, MFT, theme, language, and snapshot folder",
    resetSettingsDone: "Defaults restored",
    resetSettingsFailed: "Failed to restore defaults",
    settingsApplied: "Settings applied",
    snapDir: "Snapshot folder",
    snapDirTitle: "Where new scans are saved. On Done, snapshots in the old folder are moved here (same name is skipped)",
    snapDirChoose: "Change",
    snapDirReset: "Use default",
    snapDirOpen: "Open",
    snapDirSet: "Snapshot folder updated",
    snapDirResetDone: "Restored default snapshot folder",
    snapDirBuiltin: (p) => `Default: ${p}`,
    snapDirCustom: (p) => `Custom: ${p}`,
    decompressing: "Decompressing snapshot",
    tagCompressed: "zip",
    themeTitle: "Toggle dark / light",
    langTitle: "切换语言 / Switch language",
    githubStar: "Star",
    githubStarTitle: "Star this project on GitHub",
    pickOldLabel: "Base (earlier snapshot)",
    pickNewLabel: "Current (later snapshot)",
    pickPlaceholder: "Click to choose",
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
    scanning: "Scanning",
    scanFilesInit: "Scanned 0 files",
    scanMftModeHint: "Target is a drive root and MFT is enabled; reading MFT to build the snapshot",
    scanMftFallbackNonAdmin: "Not running as administrator; using directory scan",
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
    comparing: "Comparing",
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
    loading: "Loading",
    loadFailed: (e) => `Load failed: ${e} (collapse to retry)`,
    noMatchChild: "No changes match the current filter in this folder.",
    scannedFiles: (n) => `Scanned ${n} files`,
    scanDone: "Scan complete, snapshot saved",
    scanDoneWithTime: (s) => `Scan complete, snapshot saved · took ${s}`,
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
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
}

/** 把当前语言同步给后端，令其报错文案与界面一致。 */
function syncBackendLang() {
  try { state.api && state.api.set_language(LANG); } catch (e) {}
}

/** 切到指定语言：刷新静态文案 + 所有动态区域 + 同步后端（可写 YAML）。 */
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
  if (state._settings) updateSnapDirLine(state._settings);
  syncBackendLang();
}

/**
 * 启动时用 get_settings 校准语言与主题：
 * settings.yaml 优先；失败再退回 localStorage / 系统语言。
 * 注意：必须先应用 YAML 主题，再调 set_theme / 标题栏，否则会把 dark 写回覆盖 light。
 */
async function reconcileLang() {
  let s = null;
  try {
    // 不再用 500ms 竞速：源码启动桥接稍慢时会丢设置、主题永远回退默认暗色
    s = await state.api.get_settings();
  } catch (e) {
    s = null;
  }
  if (s) {
    state._settings = Object.assign(state._settings || {}, s);
    if (s.theme === "dark" || s.theme === "light") {
      applyThemeValue(s.theme);
    }
    updateSnapDirLine(s);
  } else {
    // 拿不到设置时才用本机缓存
    restoreThemePreference();
  }
  let lang = null;
  if (s && (s.lang === "zh" || s.lang === "en")) lang = s.lang;
  if (lang === null) lang = detectLangSync() || "en";
  // 同步到后端时 set_language 会写入 store（若已开持久化则落盘）
  if (lang !== LANG) setLang(lang);
  else syncBackendLang();
  // 主题按钮与标题栏：在 YAML 已写入 <html> 之后再同步，避免误写 dark
  applyThemeButton();
  scheduleTitlebarSync();
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

/** 扫描用时：秒数 → 可读短文案（中英文随界面语言）。 */
function fmtElapsed(sec) {
  const s = Number(sec);
  if (!Number.isFinite(s) || s < 0) return "";
  const total = Math.max(0, s);
  if (LANG === "zh") {
    if (total < 60) {
      const v = total < 10 ? total.toFixed(1) : total.toFixed(total < 100 ? 1 : 0);
      return `${v.replace(/\.0$/, "")} 秒`;
    }
    const m = Math.floor(total / 60);
    const r = Math.round(total % 60);
    if (m < 60) return r > 0 ? `${m} 分 ${r} 秒` : `${m} 分`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm > 0 ? `${h} 小时 ${rm} 分` : `${h} 小时`;
  }
  if (total < 60) {
    const v = total < 10 ? total.toFixed(1) : total.toFixed(total < 100 ? 1 : 0);
    return `${v.replace(/\.0$/, "")}s`;
  }
  const m = Math.floor(total / 60);
  const r = Math.round(total % 60);
  if (m < 60) return r > 0 ? `${m}m ${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
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
    if (!window.confirm(t("folderDeleteConfirmWithItems", name, count))) return;
    if (!window.confirm(t("folderDeleteConfirmAgain", name))) return;
  } else if (!window.confirm(t("folderDeleteConfirm", name))) {
    return;
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
    if (!el) return;
    // 备注行：挂在 pick-inner 内，value 之后
    const inner = el.parentElement;
    let noteEl = inner && inner.querySelector(`[data-role="${role}-note"]`);
    if (inner && !noteEl) {
      noteEl = document.createElement("span");
      noteEl.className = "pick-note hidden";
      noteEl.setAttribute("data-role", `${role}-note`);
      inner.appendChild(noteEl);
    }
    const s = snapByPath(path);
    if (s) {
      el.innerHTML =
        `${fmtTime(s.scanned_at)} <span class="path">· ${escapeHtml(s.root)}</span>`;
      el.classList.remove("placeholder");
      const noteText = (s.note || "").trim();
      if (noteEl) {
        if (noteText) {
          noteEl.textContent = noteText;
          noteEl.title = noteText;
          noteEl.classList.remove("hidden");
        } else {
          noteEl.textContent = "";
          noteEl.title = "";
          noteEl.classList.add("hidden");
        }
      }
    } else {
      el.textContent = t("pickPlaceholder");
      el.classList.add("placeholder");
      if (noteEl) {
        noteEl.textContent = "";
        noteEl.title = "";
        noteEl.classList.add("hidden");
      }
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
      const noteText = (s.note || "").trim();
      const noteHtml = noteText
        ? `<div class="n">${escapeHtml(noteText)}</div>`
        : "";
      item.innerHTML = `<div class="t">${fmtTime(s.scanned_at)}（${fmtAgo(s.scanned_at)}）</div>
        <div class="m">${escapeHtml(s.root)} · ${fmtBytes(s.total_size)}</div>
        ${noteHtml}`;
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
  }
}
window.__onPyEvent = onPyEvent;

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
  _settingsDraft = {
    scan_workers: Number(s.scan_workers) || 1,
    compress_snapshots: !!s.compress_snapshots,
    use_mft: !!s.use_mft,
    // 空串 = 内置目录；非空 = 自定义绝对路径
    snapshot_dir: s.snapshot_dir_is_custom ? (s.snapshot_dir_configured || s.snapshot_dir || "") : "",
    snapshot_dir_display: s.snapshot_dir || "",
    snapshot_dir_builtin: s.snapshot_dir_builtin || "",
    snapshot_dir_is_custom: !!s.snapshot_dir_is_custom,
    settings_path: s.settings_path || "",
    mft_platform_ok: s.mft_platform_ok !== false,
    is_admin: !!s.is_admin,
    cpu_count: s.cpu_count,
  };
  fillSettingsFormFromDraft();
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

async function openSettings() {
  await loadSettings();
  switchSettingsTab("general");
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
  const payload = {
    scan_workers: workerSel ? Number(workerSel.value) : _settingsDraft.scan_workers,
    compress_snapshots: compressChk ? !!compressChk.checked : _settingsDraft.compress_snapshots,
    use_mft: mftChk ? !!mftChk.checked : _settingsDraft.use_mft,
    snapshot_dir: _settingsDraft.snapshot_dir_is_custom
      ? (_settingsDraft.snapshot_dir || "")
      : "",
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
  if (!window.confirm(t("resetSettingsConfirm"))) return;
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

/**
 * 更新主题按钮文案；可选同步原生标题栏 / 后端 store。
 * 启动阶段在读完 YAML 前不要 syncBackend，否则会把默认 dark 写进 settings.yaml。
 */
function applyThemeButton(syncBackend = true) {
  const dark = document.documentElement.dataset.theme === "dark";
  const btn = $("#themeToggle");
  if (btn) btn.textContent = dark ? t("themeDark") : t("themeLight");
  if (syncBackend) syncTitlebarTheme();
}

function toggleTheme() {
  const html = document.documentElement;
  html.dataset.theme = html.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("theme", html.dataset.theme); } catch (e) {}
  applyThemeButton(true);
  // 后端 set_theme 会写入 store / YAML（若开启持久化）
}

/**
 * 应用主题到页面（并缓存 localStorage，供下次首屏闪一下用）。
 * 权威来源是 settings.yaml；启动时由 reconcileLang 调用。
 */
function applyThemeValue(theme) {
  // 默认亮色；仅显式 dark 时用暗色
  const v = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = v;
  try { localStorage.setItem("theme", v); } catch (e) {}
}

/** 仅首屏占位：读 localStorage；真正主题以 get_settings 为准。 */
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
  $("#refreshSnapsBtn").onclick = refreshSnapshots;
  $("#importSnapsBtn").onclick = importSnapshots;
  const newFolderBtn = $("#newFolderBtn");
  if (newFolderBtn) newFolderBtn.onclick = () => openCreateFolderDialog();
  $("#settingsBtn").onclick = openSettings;
  const noteSave = $("#noteSaveBtn");
  if (noteSave) {
    noteSave.onclick = () => {
      // 立刻禁用，防止连点；真正写盘在 saveNoteDialog 里异步
      noteSave.disabled = true;
      const input = $("#noteInput");
      if (input) input.disabled = true;
      saveNoteDialog(input ? input.value : "");
    };
  }
  const noteCancel = $("#noteCancelBtn");
  if (noteCancel) noteCancel.onclick = closeNoteDialog;
  const noteClose = $("#noteCloseBtn");
  if (noteClose) noteClose.onclick = closeNoteDialog;
  const noteOverlay = $("#noteOverlay");
  if (noteOverlay) {
    noteOverlay.onclick = (e) => {
      if (e.target === noteOverlay) closeNoteDialog();
    };
  }
  const noteInput = $("#noteInput");
  if (noteInput) {
    noteInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        saveNoteDialog(noteInput.value);
      }
    });
  }
  const folderSave = $("#folderSaveBtn");
  if (folderSave) {
    folderSave.onclick = () => submitFolderDialog();
  }
  const folderCancel = $("#folderCancelBtn");
  if (folderCancel) folderCancel.onclick = closeFolderDialog;
  const folderClose = $("#folderCloseBtn");
  if (folderClose) folderClose.onclick = closeFolderDialog;
  const folderOverlay = $("#folderOverlay");
  if (folderOverlay) {
    folderOverlay.onclick = (e) => {
      if (e.target === folderOverlay) closeFolderDialog();
    };
  }
  const folderNameInput = $("#folderNameInput");
  if (folderNameInput) {
    folderNameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitFolderDialog();
      }
    });
  }
  $("#settingsCloseBtn").onclick = closeSettings;
  // 「完成」才统一提交；✕ / Esc 关闭丢弃草稿（点遮罩不关闭）
  $("#settingsDoneBtn").onclick = () => applySettingsAndClose();
  document.querySelectorAll(".settings-tab").forEach((btn) => {
    btn.onclick = () => switchSettingsTab(btn.getAttribute("data-settings-tab"));
  });
  // 设置说明问号：点击展开/收起（悬停说明靠 title）
  document.querySelectorAll(".settings-q").forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleSettingsHelp(btn);
    };
  });
  // 控件只改草稿，不写后端
  const workerSel = $("#workerSel");
  if (workerSel) {
    workerSel.onchange = (e) => {
      if (_settingsDraft) _settingsDraft.scan_workers = Number(e.target.value) || 1;
    };
  }
  const compressChk = $("#compressChk");
  if (compressChk) {
    compressChk.onchange = (e) => {
      if (_settingsDraft) _settingsDraft.compress_snapshots = !!e.target.checked;
    };
  }
  const mftChk = $("#mftChk");
  if (mftChk) {
    mftChk.onchange = (e) => {
      if (_settingsDraft) _settingsDraft.use_mft = !!e.target.checked;
    };
  }
  const snapDirChoose = $("#snapDirChooseBtn");
  if (snapDirChoose) snapDirChoose.onclick = chooseSnapDir;
  const snapDirReset = $("#snapDirResetBtn");
  if (snapDirReset) snapDirReset.onclick = resetSnapDirDraft;
  const snapDirOpen = $("#snapDirOpenBtn");
  if (snapDirOpen) snapDirOpen.onclick = openSnapshotDir;
  const resetSettingsBtn = $("#resetSettingsBtn");
  if (resetSettingsBtn) resetSettingsBtn.onclick = () => resetSettingsToDefaults();
  const logRefresh = $("#logRefreshBtn");
  if (logRefresh) logRefresh.onclick = () => refreshLogView();
  const logClear = $("#logClearBtn");
  if (logClear) logClear.onclick = () => clearAppLog();
  const logExport = $("#logExportBtn");
  if (logExport) logExport.onclick = () => exportAppLog();
  const uninstallBtn = $("#uninstallBtn");
  if (uninstallBtn) uninstallBtn.onclick = () => openUninstallDialog();
  const uninstallCancel = $("#uninstallCancelBtn");
  if (uninstallCancel) uninstallCancel.onclick = () => closeUninstallDialog();
  const uninstallClose = $("#uninstallDialogCloseBtn");
  if (uninstallClose) uninstallClose.onclick = () => closeUninstallDialog();
  const uninstallConfirm = $("#uninstallConfirmBtn");
  if (uninstallConfirm) uninstallConfirm.onclick = () => confirmUninstallCleanup();
  const uninstallDoneOk = $("#uninstallDoneOkBtn");
  if (uninstallDoneOk) uninstallDoneOk.onclick = () => finishUninstallAndQuit();
  const importOk = $("#importOkBtn");
  if (importOk) importOk.onclick = closeImportResultDialog;
  const importClose = $("#importCloseBtn");
  if (importClose) importClose.onclick = closeImportResultDialog;
  const importOverlay = $("#importOverlay");
  if (importOverlay) {
    importOverlay.onclick = (e) => {
      if (e.target === importOverlay) closeImportResultDialog();
    };
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
    if (e.key === "Escape") {
      closeCtxMenu();
      const io = $("#importOverlay");
      if (io && !io.classList.contains("hidden")) {
        closeImportResultDialog();
        return;
      }
      const fo = $("#folderOverlay");
      if (fo && !fo.classList.contains("hidden")) {
        closeFolderDialog();
        return;
      }
      const no = $("#noteOverlay");
      if (no && !no.classList.contains("hidden")) {
        closeNoteDialog();
        return;
      }
      const so = $("#settingsOverlay");
      if (so && !so.classList.contains("hidden")) closeSettings();
    }
  });

  // 主题按钮文案占位；不调 set_theme，等 reconcileLang 读完 YAML 再同步
  applyThemeButton(false);
  setFilter(state.filter);
}

// ---- 启动 ----

async function boot() {
  state.api = window.pywebview.api;
  // 首屏仅用 localStorage 占位；权威主题/语言以 settings.yaml（get_settings）为准
  restoreThemePreference();
  applyThemeButton(false);
  wireEvents();
  await reconcileLang(); // 内部会 applyThemeButton + scheduleTitlebarSync
  loadSnapshots();
  await loadSettings();
  // 非管理员：启动 toast 推荐提权（MFT / 系统路径完整性）
  if (state._settings && state._settings.is_admin === false) {
    toast(t("recommendAdminToast"));
  }
}

// 首屏即按浏览器语言渲染静态文案（同步、无闪烁）；boot 后再与后端校对。
// 主题先用 localStorage 占位，避免白闪；最终以 YAML 覆盖（勿在 boot 前 set_theme）。
LANG = detectLangSync() || "en";
restoreThemePreference();
applyStaticI18n();
applyThemeButton(false);

// pywebview 注入 API 后触发 pywebviewready；若已就绪则直接启动。
if (window.pywebview && window.pywebview.api) {
  boot();
} else {
  window.addEventListener("pywebviewready", boot);
}

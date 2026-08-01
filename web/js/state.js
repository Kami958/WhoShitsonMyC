/* 全局状态 */
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
  filter: "all",      // 变化方向过滤：all | grew | shrank | added | removed
  // 修改时间过滤："" | today | 7d | 30d | custom；custom 用 filterTimeDays 天
  filterTime: "",
  filterTimeDays: 0,
  // 变化量过滤（|delta| 与阈值比较）；filterDeltaVal 为空表示未启用
  filterDeltaOp: "gt", // gt=大于 | lt=小于
  filterDeltaVal: "",
  filterDeltaUnit: "MB", // B | KB | MB | GB
  _marksLoaded: false,  // 会话级：当前顶层数据是否已带「新增/已删除」下钻标记
  sort: "delta-desc", // 对比树排序，见 SORTERS
  browseSort: "size-desc", // 占用展开树排序（与对比独立）
  searchSort: "delta-desc", // 搜索结果排序（与变化树独立）
  searchCaseSensitive: false, // 搜索：区分大小写（默认关）
  searchExact: false,         // 搜索：严格整名匹配（默认关）
  // 搜索内存索引预热：idle | started | ready | failed | aborted | skipped
  searchPreheat: "idle",
  // 预热对应的快照对（路径变了必须重新读索引，不能复用 ready）
  searchPreheatKey: "",
  // 设置：打开搜索时是否预热内存索引（默认开；与后端 store 对齐）
  searchMemoryIndex: true,
  snapSort: "time-desc", // 快照列表排序，见 SNAP_SORTERS
  compared: false,    // 是否已出对比/浏览结果
  comparing: false,   // 对比/浏览请求进行中（防重复点击）
  // 主树模式：compare=两份快照差分；browse=单份快照按占用展开
  treeMode: "compare",
  _childrenInflight: {},
  // 会话内已拉取的子目录 path → nodes；收起/改排序不丢
  _childrenCache: {},
  // 当前处于展开态的目录 path 集合（仅 UI 状态）
  _openPaths: {},
  compareRoot: "",    // 本次对比/浏览的扫描根（右键定位真实路径用）
  // 上次成功对比的路径对（用于判断是否需再解压 .dbz）
  _lastCompareKey: "",
  _lastComparePaths: "",
  ctxNode: null,      // 右键菜单当前指向的节点
  // 对比树多选：path → node 摘要；仅当前会话
  treeSelected: {},
  _treeSelectAnchor: "", // Shift 多选锚点 path
  treeMultiSelect: false, // 工具栏多选开关：开后单击即勾选
  modules: {},        // 构建期可选模块：{ ai: true, ... }
};

const PER_LEVEL_CAP = 300; // 每层最多先渲染这么多行，其余「显示更多」

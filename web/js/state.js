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
  filter: "all",      // 当前过滤：all | grew | shrank
  sort: "delta-desc", // 变化树排序，见 SORTERS
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
  compared: false,    // 是否已出对比结果
  comparing: false,   // 对比请求进行中（防重复点击）
  compareRoot: "",    // 本次对比的扫描根（右键定位真实路径用）
  // 上次成功对比的路径对（用于判断是否需再解压 .dbz）
  _lastCompareKey: "",
  _lastComparePaths: "",
  ctxNode: null,      // 右键菜单当前指向的节点
  modules: {},        // 构建期可选模块：{ ai: true, ... }
};

const PER_LEVEL_CAP = 300; // 每层最多先渲染这么多行，其余「显示更多」

# 扫描链路

「新建扫描」从 UI 到落盘的调用链。不含对比（`Diff`）。

## 流程

```text
newScan()
  choose_folder()                 # 同步
  start_scan(path)                # 同步立刻返回 {started|error}
    └─ 线程 _run_scan
         new_snapshot_path → *.db
         scan_to_snapshot         # N worker 枚举 + 单线程写库
         [可选] compress_db       # .db → .dbz
         _emit(progress|done|error|cancelled)
  onPyEvent → loadSnapshots → list_snapshots()
```

进度与结果只走事件（`_emit` → `evaluate_js` → `__onPyEvent`），不走 `start_scan` 返回值。

| 层 | 文件 | 职责 |
|----|------|------|
| UI | `web/app.js` | 选目录、遮罩、收事件、刷列表 |
| 桥接 | `app.py` `Api` | 线程、推送、压缩终态 |
| 路径/设置 | `core/store.py` | 路径、worker/压缩/MFT；改设置后自动写 YAML |
| 扫描 | `core/scanner.py` | 并行枚举、聚合 size |
| 写库 | `core/snapshot.py` | SQLite v3 |
| 压缩 | `core/compress.py` | `.dbz` |
| 模型 | `core/models.py` | `Entry` / `SnapshotMeta` |

`core/` 不依赖 pywebview。

## 前端

| 符号 | 作用 |
|------|------|
| `newScan()` | 选目录 → 遮罩 → `start_scan` |
| `cancel_scan` / `onPyEvent` / `loadSnapshots` | 取消、收事件、刷新 |

| 事件 | payload |
|------|---------|
| `scan-progress` | `{files, current}` |
| `scan-done` | `{snapshot, warning?}` 压缩失败仍保留 `.db` 并带 warning |
| `scan-cancelled` / `scan-error` | 半成品已删 |
| `migrate-progress` | `{done, total, name, status, moved, skipped, failed}` |
| `migrate-done` | `{moved, skipped, failed, total, errors, snapshot_dir}` |
| `settings-applied` | 完整设置结果，或 `{ok:false, error}` |

`snapshot`：`path, root, scanned_at, total_size, file_count, skipped_count, compressed, note?`。

设置页：草稿编辑 → 点「完成」调 `apply_settings`（后台线程，立刻 `{started:true}`）→ 收 `migrate-progress` / `settings-applied`。遮罩点空白不关设置。

## 桥接 `app.py`

**`start_scan`**：忙或路径非法 → error；否则起 `_run_scan`，立刻 `{started: true}`。

**`cancel_scan`**：`_cancel.set()` → 扫描侧抛 `ScanCancelled`。

**`apply_settings`**：后台 `_run_apply_settings`；目录变更时边迁边推 `migrate-progress`，结束推 `settings-applied`（兼 `migrate-done`）。

**`set_snapshot_note` / `choose_snapshot_files` / `pick_snapshot_dir`**：备注写文件；导入多选 `.db`/`.dbz`；设置页选目录（草稿，不立刻落盘）。

**`get_app_log` / `clear_app_log` / `export_app_log`**：进程内内存日志（`core/applog`，环形 1024 条，默认不落盘）；导出另存为 `.txt`。不含扫描路径；错误栈路径脱敏。启动记 CPU/内存等；扫描取消/失败/完成（无路径）会写入。无「扫描超时」专用逻辑。

**`uninstall_app_data`**：设置页「卸载」→ 弹窗勾选是否删数据 → `store.wipe_app_data`。勾选时只清 `%LOCALAPPDATA%\\WhoShitsOnMyC`，**不碰**用户自定义快照路径；**不删**程序本体。完成后 `quit_app` 关窗。

**`_run_scan` 顺序**

1. `new_snapshot_path` → 先写 `.db`
2. `scan_to_snapshot(..., progress→_emit, cancel, workers)`
3. 若开启压缩：`compress_db`；失败则 done + warning，保留 `.db`
4. 取消/异常删半成品并发对应事件；成功 `scan-done`

**`_emit`**：`evaluate_js('__onPyEvent(event, data)')`，参数 JSON 编码。

## `store.py`

| API | 说明 |
|-----|------|
| `default_snapshot_dir` | `%LOCALAPPDATA%\WhoShitsOnMyC\snapshots` |
| `new_snapshot_path` | 新 `.db` 路径 |
| `get/set_scan_workers` | 1–128；默认 HDD=1 / 其它=CPU 核数 |
| `get/set_compress_snapshots` | 默认 True |
| `get/set_use_mft` | 默认 True；盘符根 NTFS 是否尝试 MFT |
| `apply_settings` | 设置页「完成」一次性提交并写 `settings.yaml`；目录变更时 `migrate_snapshots`；可选 `progress` 回调 |
| `reset_settings_to_defaults` | 删 `settings.yaml`，内存回内置默认（可传 `lang`） |
| `migrate_snapshots` | 原目录 `.db`/`.dbz` 迁到新目录（同名跳过）；`progress({done,total,name,status,…})` |
| `get/set_lang` / `get/set_theme` | 界面语言 / 主题；值变化时写 YAML |
| `get/set_snapshot_dir` | 快照存放目录；空=内置 `…/snapshots` |
| `settings_path` | 配置文件绝对路径 |
| `list_snapshots` | `.db`/`.dbz`；`.dbz` 只读 zip 内 `meta.json` |
| `delete_snapshot` | 删文件；`.dbz` 清本进程解压临时文件 |
| `set_note(path, note)` | 备注写入快照文件（`.db` meta / `.dbz` meta.json） |
| `app_data_dir` / `wipe_app_data` | 应用数据根；卸载清理（可选删目录 / 仅删 yaml） |

默认不创建 `settings.yaml`。启动时若已有文件则按键读取，缺键用默认值；旧文件中的 `persist` 键忽略。YAML 以 **`common:`** 顶层节写入（对应设置页「通用」），仍兼容旧扁平格式。键：线程、压缩、MFT、语言、主题、快照目录。设置页草稿编辑，点「完成」才 `apply_settings` 并写盘；侧栏语言/主题切换立即写盘。「恢复默认」删配置并回内置默认。

## `scanner.py`

**`scan_to_snapshot`**

```text
校验 root
→ [盘符根 + NTFS] 尝试 core.mft（失败则回退）
→ SnapshotMeta → 根 id=1 入队
→ N× worker_loop
→ 本线程 SnapshotWriter + drain_rows → finalize
→ 关 worker → return meta
```

- 一目录一任务；worker 只枚举（`scandir`/`stat`）
- 单线程写库（`rows` 队列）；文件行可按目录批量入队
- 目录 size = 子树逻辑大小；根完成 = 整树完成
- 无权限 → `skipped`，不中断
- 默认不跟 symlink/reparse；Windows 用 `\\?\` 长路径
- 完成传播：锁内设 `pending` 后再入队子目录

| 符号 | 作用 |
|------|------|
| `worker_loop` / `_process_dir` | 取任务、枚举、产 Entry/子任务 |
| `_complete` / `drain_rows` | 聚合写目录行；写库、进度、取消 |
| `timer` 可选 | 开发计时；生产 `None` |

## `mft/`（默认开）

盘符根 + NTFS 时**默认尝试**读 `$MFT` 建快照；失败自动回退多线程 `scandir`。  
**非管理员进程不走 MFT**（设置勾选可保留；扫描遮罩提示已回退常规扫描）。  
冷启动通常更稳；热缓存下与 scandir 可能互有胜负。子目录不走 MFT。

关闭（任一）：设置页关「尝试 MFT 扫描」→ `store.set_use_mft(False)`；或 `WSMC_DISABLE_MFT=1`（优先）。  
强制开：`WSMC_USE_MFT=1`（仍需管理员）。解析进程数按 CPU/记录量自推导（`WSMC_MFT_WORKERS` / `WSMC_MFT_PROCS` 可覆盖），与扫描线程无关。

| 模块 | 职责 |
|------|------|
| `volume` | 开卷、BPB、runlist；连续区段并发读；`on_range` 供流水；HDD 单线程 |
| `pipeline` | 读进 SharedMemory；多进程时**段就绪即派解析**（读∥解析） |
| `parallel` | 多进程紧凑 meta + names；`StreamingCompactCollector`；USA 在 worker |
| `parse` | 单条 FILE；热路径 memoryview |
| `tree` | 紧凑表直通建树；平铺 merge；栈 DFS 分 sid + 反向上卷；`on_batch` |
| `scan` | 编排；异步写库线程；`gc.disable` 建树段；失败 `MftUnavailable` |

计时（`WSMC_SCAN_TIMING=1`）：`mft_read` / `mft_parse` / `mft_parse_wait` / `mft_tree`（含 merge/bfs/agg/emit）/ `mft_write_join` / `drain_rows` / `finalize`；`backend=mft`。

## `snapshot.py`

| 表 | 字段 |
|----|------|
| `entries` | `id, parent_id, name, size, is_dir, mtime`（邻接表，无完整路径） |
| `meta` | root / 时间 / 计数 / skipped / `format_version` / `note` |

根：`id=1`, `parent_id=NULL`, `name=""`。  
`add` 约 1 万条一批；`finalize` 写 meta 后建 `parent_id` 索引。  
PRAGMA：`synchronous=OFF`, `journal_mode=MEMORY`, `temp_store=MEMORY`。

## `compress.py`

| API | 说明 |
|-----|------|
| `compress_db` | zip(`meta.json`+`data.db`)→`.dbz`，删 `.db`；meta 含 note |
| `read_meta_any` | 列表读 meta，不解压整库 |
| `ensure_db_path` | **对比时**解压 `.dbz` 到系统临时文件（进程内登记，不写应用 cache/） |
| `write_snapshot_note` | 更新 `.db` meta 或重写 `.dbz` 内 meta.json 的 note |

## 模型

- **Entry**：文件 size=自身；目录 size=聚合逻辑大小；`mtime` 秒  
- **SnapshotMeta**：`format_version=3`，含 `note`；v1/v2 可列举、不可对比  

## 开发计时（可选）

- 入口：`core/timing_probe.py`（`sys.frozen` 或无 `dev` → 空操作）  
- 实现：`dev/scan_timing.py`（`build.py --exclude-module dev`）  
- 开启：`$env:WSMC_SCAN_TIMING=1`（可选 `WSMC_SCAN_TIMING_LOG`）  

| 字段 | 含义 |
|------|------|
| `total` | `_run_scan` 全程墙钟 |
| `scan_to_snapshot` | 扫+写 |
| `mft_read` / `mft_parse` | 读 $MFT / 解析（读∥解析时 parse 仅为收尾墙钟） |
| `mft_tree_*` / `mft_write_join` | 建树子段；写库线程 join 尾巴 |
| `drain_rows` | 常规路径写库墙钟；MFT 下与 tree 重叠，**勿与子 span 简单相加** |
| `finalize` / `compress` | 建索引 / 压缩 |

## 测试

```bash
python -m pytest tests/test_scanner.py -q
```

覆盖聚合、取消、进度、多/单 worker 对拍。

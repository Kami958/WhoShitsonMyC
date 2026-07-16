# 开发者文档

快照扫描目录，再对比两份快照，定位空间变化。

栈：Python 3.12+ · pywebview (WebView2) · SQLite · `web/` 前端。

## 构建

```bash
pip install -r requirements.txt
python app.py
python -m pytest tests/ -q
pip install pyinstaller
python build.py            # dist/wsmc-v*.exe（主线，不含 AI）
python build.py --with-ai  # dist/wsmc-ai-v*.exe（含实验 AI）
```

## 目录

```text
app.py           窗口、JS API、扫描/设置后台线程、WebView2、删除与侧栏加宽
titlebar.py      标题栏暗/浅色（Windows）
build.py         打包（主线默认无 AI / --with-ai）
version.py       版本号与 GitHub 发布比较
core/            无 UI 核心
  models.py        Entry / SnapshotMeta / DiffNode
  scanner.py       多线程扫描；盘符根默认尝试 MFT
  mft/             NTFS MFT（读∥解析、紧凑建树；失败回退 scandir）
  snapshot.py      SQLite 快照（v3 邻接表；meta 含 note）
  differ.py        逐层懒对比
  compress.py      .db ↔ .dbz；会话内解压；备注写回
  store.py         快照路径、settings.yaml、目录迁移、备注、删除黑名单
  fs_delete.py     对比路径解析、黑名单匹配、回收站/永久删除
  applog.py        应用日志（stdlib logging + 内存缓冲；等级门槛；脱敏导出）
  i18n.py          后端文案语言
  timing_probe.py  扫描计时入口（生产空操作；汇总进 applog）
modules/         可选模块（如 AI；主线包可不打包）
  ai/
    config.py      settings.yaml 的 ai: 节；Key 明文；public_view 含 tool 目录
    client.py      OpenAI 兼容流式请求
    prompts.py     系统约束 + SoftwareContext 拼装（右键 / 清理场景）
    packing.py     上下文 packing：右键一层+10；清理门槛剪枝 + 多切片
    tools.py       tool 契约与目录；仅 propose_pending_delete，无真删 tool
    service.py     设置 / 对话 / tool 续写 / 对比清理多切片 API
web/             index.html + web/js/* + style.css
  pending.js       右侧工具侧栏：待删除列表、宽度拖拽、AI 提议勾选清单
  ai.js            AI 侧栏、设置草稿、工具设置二级窗、清理多切片前端
tests/
dev/             开发辅助（打包 exclude）；含 scan_timing
assets/docs/     本文与模块说明
```

运行时数据：`%LOCALAPPDATA%\WhoShitsOnMyC\`（默认 `snapshots/`、可选 `settings.yaml`）。  
默认不写配置；用户改过设置后才自动生成 `settings.yaml`（`common:` / `ai:` 等顶层分节；旧扁平仍可读）。  
快照目录可在设置里改；改完点「完成」会迁移原目录中的 `.db`/`.dbz`。  
设置 → 通用「恢复默认」：删配置并回内置默认（不删快照文件）。  
设置 → 通用「卸载」：弹窗确认是否删数据（默认勾选）；勾选时只清应用数据目录，不碰自定义快照路径、不删程序本体；完成后确认退出。  
设置 → 通用「删除黑名单」：绝对不允许删除的路径（精确 / 前缀 / 正则）；写入 `common.delete_blacklist`。  

对比树右键「加入待删除」：进右侧工具侧栏会话列表（不写 yaml）；可勾选本会话「彻底删除」后批量执行；默认进回收站（一次确认），彻底删除两次确认；回收站失败不静默改永久删除。  
AI 可将路径 **提议** 加入待删除：仅 `propose_pending_delete` tool；前端勾选清单确认后入队，审批结果回传模型续写；**不**由模型真删。  
设置 → AI「工具设置」：二级窗口勾选可调用 tool（`ai.enabled_tools` 列表，写入 settings.yaml）；未勾选则不注入模型。  
设置 → AI「清理展开层数」：预留 `cleanup_max_depth`（默认 3），界面标注暂未作为产品能力完全开放。  
右键问 AI：固定一层子项上下文（最多 10 条）。清理 packing（`modules/ai/packing.py`）：约 200MB 绝对门槛 + 占上级比例 + 深度/单批路径阀；装不下则 `has_more` / `deferred_top` 多切片；`app.py` 向模块注入 `get_diff_children`。  
右侧工具侧栏可拖拽分界调整宽度（内部布局，非改 OS 窗口边）；展开时仍可加宽应用窗口，收起扣回；最大化时不改窗口尺寸。  

进程内日志见下节「应用日志」。  
`.dbz` 对比时解到系统临时目录，仅本进程存活期间复用，**不写** `cache/`。  
备注写在快照文件内（`.db` meta / `.dbz` meta.json），随文件移动。

## 应用日志（唯一运行时出口）

### 边界

| 走 `applog` | 不走 `applog` |
|-------------|---------------|
| `app.py` / `core/*` / `modules/*` 运行时 | `build.py` 打包 CLI 进度 |
| 扫描分段计时汇总（`dev/scan_timing`） | `dev/bench_*` 基准脚本面向终端的进度 |
| 设置页查看 / 导出 | 用户弹窗、前端 toast |

禁止运行时再 `print` / `traceback.print_exc` 记日志（会与缓冲、脱敏脱节）。  
底层 stdlib **`logging`**（logger 名 `wsmc`）+ MemoryHandler；业务只调 `applog.*`。  
默认不落盘；路径脱敏；不记扫描路径、API Key、问答正文。

### 等级

`DEBUG` < `INFO` < `WARN` < `ERROR`（对外 `WARN` = logging `WARNING`）。

| 等级 | 用途 |
|------|------|
| DEBUG | 过程细节、扫盘计时摘要、参数校验拒绝、预热开始/中止、**设置项变更 diff** |
| INFO | 启动、扫完、对话开始/完成/取消、预热完成、恢复默认等 |
| WARN | 鉴权/限流/网络、计时 error 态 |
| ERROR | 未预期异常（带脱敏栈） |

默认门槛 **INFO**。`get_app_log.min_level`；导出头含 `backend: logging`。

### 设置变更日志（统一接口）

业务侧只调：

- `applog.log_settings_changed(scope, changes)` — 有 diff 才写；默认 **DEBUG**
- `applog.log_settings_event(scope, event)` — 非 diff 事件（如恢复默认）

约定：

1. `changes` 每项为 `key: old -> new` 原文；**路径不要调用方先 sanitize**
2. 是否脱敏完全由写入时的 `log_sanitize` 开关决定（开 → 路径替换；关 → 保留明文）
3. 无实质变更不写盘、不打变更日志（通用设置与 AI `set_config` 均如此）
4. 通用设置 scope=`settings`；AI 配置 scope=`ai`（API Key 只记 set/updated/empty，不记明文）

### 扫描计时（同一出口）

`core/timing_probe` → `dev/scan_timing`；**exe 永远关**。  
启用：门槛 ≤ DEBUG，**或** 兼容 `WSMC_SCAN_TIMING=1`（此时摘要可走 INFO）。  
可选 `WSMC_SCAN_TIMING_LOG` → JSONL（机器读，独立于设置页缓冲）。

### 调试开关

| 变量 / 参数 | 作用 |
|------|------|
| `WSMC_LOG_LEVEL` | `DEBUG` / `INFO` / `WARN` / `ERROR` |
| `WSMC_DEBUG=1` | 未设前者时门槛 DEBUG；同时会开 WebView DevTools |
| `WSMC_DEVTOOLS=1` | 仅开 WebView DevTools（F12 / 右键检查），不改日志门槛 |
| `--devtools` / `--debug` | 命令行等价于开 DevTools |
| `PYTHONUNBUFFERED=1` | 终端实时刷 stderr |
| `WSMC_SCAN_TIMING=1` | 兼容：强制开计时（仍写 applog） |
| `WSMC_SCAN_TIMING_LOG` | 计时 JSONL（可选） |

源码 + 门槛 ≤ DEBUG：`StreamHandler(stderr)` mirror；exe 不 mirror。

#### 前端 DevTools

- 开启后 WebView2 可用 F12 / 右键「检查」；`web/js` 的 `console.*` 只出现在浏览器控制台，不写 Python / `applog`。
- 开 DevTools 时会静音本地静态服务的 HTTP 访问日志（Bottle / WSGI），避免 Python 终端被 `GET /js/...` 刷屏；不改变前端控制台输出。
- `WSMC_DEBUG=1` 会连带开 DevTools；只想开检查器、不想降日志门槛时用 `WSMC_DEVTOOLS=1` 或 `--devtools`。

```powershell
$env:PYTHONUNBUFFERED = "1"
$env:WSMC_LOG_LEVEL = "DEBUG"
# 仅 DevTools（可选）
# $env:WSMC_DEVTOOLS = "1"
python app.py
# 或：python app.py --devtools
```

## 模块说明

| 文档 | 内容 |
|------|------|
| [扫描链路](scan-pipeline.md) | UI → 写快照 → 可选压缩；设置 / 备注 / 迁移；开发计时 |

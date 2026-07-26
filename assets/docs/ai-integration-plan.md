> **状态（2026-07）**：产品默认**不再加载 AI**。本文保留作历史实施说明；重新接入步骤见 [ai-reenable.md](ai-reenable.md)。代码未删。

# AI 接入实施计划

面向编码 AI 的实施文档。目标：接入 AI 能力，帮助普通用户看懂"C 盘被什么占了"。
本计划基于对当前代码库（v1.1.0，feature-v1.1.0 分支）的实际调研，接线点均给出文件与行号锚点。

## 架构铁律（任何实现不得违背）

1. **AI 是共享服务，不是功能**。provider 请求层不带业务语义，所有 AI 功能都是它的消费者，整层可在构建期被排除。
2. **可选能力做成构建期模块，不做运行时插件**。单 exe 形态下不加载外部代码；通过 `build.py` 开关产出带/不带模块的发行版。
3. **AI 永远无法触达删除执行路径**。AI 输出只能是文本建议或"加入待删除列表"；执行前必须过独立于 AI 的规则校验 + 人工确认。本计划只做到列表，不做执行。

隐私边界：仅 BYOK（用户自填 provider / key），AI 默认关闭，首次使用弹窗告知"将把所选路径与大小信息发送给你配置的服务商"。

## 架构总览

```text
app.py                     桥接层：新增模块注册表 + module_invoke 分发器 + list_modules
core/                      纯核心（不动，对模块零依赖）
modules/                   新增：构建期可选模块
  __init__.py                模块发现与注册（import 失败 = 模块不存在，静默降级）
  ai/                        AI 服务模块
    __init__.py                MODULE 入口（name="ai", create(ctx) → 实例）
    config.py                  ai.json 读写 + DPAPI 加密 key
    client.py                  OpenAI 兼容 HTTP 客户端（stdlib urllib，SSE 流式）
    prompts.py                 提示词模板（系统约束 + 用户追加 + 上下文拼装）
    service.py                 对前端的方法：ask/cancel/get_config/set_config/test_connection
  cleaner/                   （二期）清理模块骨架
web/js/
  modules.js                 新增：模块探测 + callModule 封装
  ai.js                      新增：AI 侧栏 UI + 流式渲染 + 右键入口逻辑
```

依赖方向单向：`modules/* → core`；`modules/cleaner → modules/ai`（可选，缺席时降级）。
`core` 与主流程对 `modules` 零 import（app.py 的 try-import 注册除外）。

## 第一期：模块机制 + AI 服务 + 问 AI

### 1. 后端：模块注册机制

- `modules/__init__.py` 提供 `discover() -> dict[str, object]`：逐个 `try: import modules.ai ... except ImportError: pass`。被 build 排除的模块 import 失败即视为不存在。
- 每个模块包暴露 `create(ctx) -> object`。`ctx` 由 app.py 注入，至少含：
  - `emit(event, payload)`：转发 `Api._emit`（app.py:75）
  - `app_data_dir()`：复用 `store.app_data_dir()`（store.py:895）
  - `t(zh, en)`：复用 `core/i18n.py`
- `Api` 新增两个方法（模式参考现有方法，app.py:40 起）：
  - `list_modules() -> dict`：返回如 `{"ai": True}` 的可用模块表
  - `module_invoke(module: str, method: str, kwargs: dict) -> dict`：统一分发。只分发模块实例上 `PUBLIC_METHODS` 集合声明的方法；未知模块/方法返回 `{"error": ...}`。**不要**给 Api 动态 setattr 加方法——pywebview 桥的方法枚举时机不可控。
- 卸载/恢复默认联动：`store.wipe_app_data()`（store.py:900）清应用数据目录时天然带走模块配置文件（配置就放该目录）；`Api.reset_settings`（app.py:448）末尾追加调用各模块的 `reset()` 方法（若有）。

### 2. 后端：AI 服务模块（modules/ai/）

**config.py** —— 配置存独立文件 `%LOCALAPPDATA%\WhoShitsOnMyC\ai.json`（JSON + 标准库）。
**不要**扩展 store.py 的手写 YAML 解析器（store.py:329 `_parse_setting_pair` 是白名单机制，动它牵连太广）。

```json
{
  "enabled": false,
  "base_url": "https://api.openai.com/v1",
  "model": "",
  "api_key_enc": "<DPAPI加密后base64>",
  "extra_prompt": "",
  "consented": false
}
```

- API key 用 Windows DPAPI 加密：ctypes 调 `CryptProtectData` / `CryptUnprotectData`，用户级作用域，无新依赖。读失败（换机器/文件损坏）视为未配置，前端提示重填。
- 写文件走 tmp + `os.replace` 原子替换（模式同 store.py:409 `_write_settings_yaml`）。

**client.py** —— OpenAI 兼容 `/chat/completions`，`stream: true`：

- 用 `urllib.request` 发 POST，逐行读响应解析 SSE（`data: {...}` 行，`[DONE]` 结束）。**不新增任何 pip 依赖**（requirements.txt 运行依赖只有 pywebview，build.py 对体积敏感）。
- 超时：连接 15s，读 60s。错误分类返回（网络 / 401 / 429 / 响应格式），文案走 `i18n.t(zh, en)`。
- 每个请求持有 `threading.Event` 取消标志，取消即关闭连接。

**service.py** —— 前端可调方法（经 module_invoke 白名单暴露）：

- `get_config()` / `set_config(payload)`：key 只写不读，返回时以 `has_key: true` 代替明文。
- `test_connection()`：发一条最小请求验证配置。
- `ask(context: dict, question: str) -> {"id": ...}`：后台线程执行（模式参考 app.py:480 `apply_settings` 的线程 + 终态事件），流式经 `ctx.emit` 推事件：`ai-chunk {id, text}` / `ai-done {id}` / `ai-error {id, message}`。同一时刻只允许一个进行中请求，新请求先取消旧的。
- `cancel(id)`。
- 未 `consented` 或未 `enabled` 时 `ask` 直接返回 error，由前端引导。

**prompts.py** —— 提示词分三层拼装，顺序固定：

1. 系统约束（代码内置，不可被用户覆盖）：角色是磁盘空间分析助手；回答面向普通用户、简洁；永远不得建议删除系统目录、不得输出可执行的删除命令；对不确定的路径要说"不确定"。
2. 用户追加提示词（`extra_prompt`），拼在系统约束之后。
3. 上下文块：由后端从前端传来的结构化数据拼装——扫描根、节点完整路径、is_dir、old/new size、delta、kind（含义见 core/models.py:67 `ChangeKind`）、mtime、top 10 子项变化摘要（前端从已加载的树数据取，或后端经现有 `Api.get_children` 取）。**上下文有上限**：子项最多 10 条，总长截断到约 4000 字符。

### 3. 前端（web/）

脚本加载顺序（index.html L430-439，严格顺序、无模块化）：`modules.js` 与 `ai.js` 插在 `settings.js` 之后、`app.js` 之前。

**modules.js**：

- `boot()`（app.js L315）里在 `loadSettings` 前调 `state.api.list_modules()` 存入 `state.modules`。
- `applyModuleVisibility()`：对所有 `[data-module="ai"]` 元素按模块存在性加/去 `hidden`。模块不存在时界面与现状完全一致。
- `callModule(mod, method, kwargs)` 封装 `state.api.module_invoke`。

**右键"问 AI"**（复用现有右键菜单，最自然入口）：

- index.html L186 `#ctxMenu` 加一项：`<div class="ctx-item" data-cmd="ask-ai" data-module="ai" data-i18n="ctxAskAi">`。
- compare.js `ctxCommand`（约 L1156）加 `ask-ai` 分支：取 `state.ctxNode` + `state.compareRoot`（util.js `fullPath` 拼完整路径），打开 AI 侧栏并发起 `ask`。搜索结果面板共用同一菜单，自动获得入口。

**AI 侧栏**（ai.js + index.html + style.css）：

- `.layout` 改为可出现第三栏：`sidebar | main | aside#aiPanel`，默认 `hidden`，打开约 340px。样式复用 `--bg-side`、`--border` 变量，兼容暗/浅主题（style.css L9-46 变量表）。
- 结构：标题条（关闭按钮）+ 消息列表 + 输入框（追问）+ 进行中时显示停止按钮。
- 事件接收：并入 scan.js L218 `onPyEvent` 的 switch（`ai-chunk` / `ai-done` / `ai-error`），chunk 渲染用 rAF 合并（模式照抄 `queueScanProgress`）。
- AI 回复按纯文本渲染，必须过 util.js `escapeHtml`。**AI 输出不可信，禁止 innerHTML 直插**。
- 首次使用：`consented=false` 时先用 `showConfirmDialog`（util.js）弹隐私告知，确认后写回 config 再发请求。

**设置页 AI 分节**（模式照抄现有 tab，index.html L252-353 + settings.js）：

- tabs 加 `data-settings-tab="ai"`（带 `data-module="ai"`），body 加 `data-settings-pane="ai"`。tab 点击已是事件委托，无需改绑定。
- 字段：启用开关、Base URL、模型名、API Key（password 输入框，已保存时显示"已保存"占位）、追加提示词（textarea）、测试连接按钮。
- **读写走 `callModule("ai", "get_config"/"set_config")`，不进 `apply_settings` 的通用 payload**（key 不应流经通用设置字典与日志）。沿用 `_settingsDraft` 草稿模式，点「完成」时一并提交。
- 分节内放一行隐私说明文案（遵循 CLAUDE.md 的 C 端文案风格：简洁直白，不用括号注释腔）。

**i18n**：i18n.js 的 `I18N.zh` 与 `I18N.en` 同步加 key（前缀 `ai*`：`ctxAskAi`、`settingsTabAi`、`aiPrivacyNote`、`aiThinking`、各类错误文案）。后端文案一律 `i18n.t(zh, en)` 内联双语。

### 4. 构建变体（build.py）

- 加命令行参数：`python build.py`（默认带 ai）/ `python build.py --no-ai` / 将来 `--with-cleaner`。
- 排除方式：往现有 `_EXCLUDES`（build.py L41）追加 `modules.ai` 及其子模块。
- 前端 `ai.js` 始终随 `web/` 打包，靠 `list_modules()` 门控隐藏，不做前端裁剪。
- 产物命名区分：如 `wsmc-v{ver}.exe` 与 `wsmc-lite-v{ver}.exe`（保持默认名不变）。

### 5. 测试（tests/，沿用现有 pytest 模式）

| 文件 | 覆盖 |
|------|------|
| test_modules.py | discover 对缺失模块静默、module_invoke 白名单拒绝未知方法 |
| test_ai_config.py | ai.json 读写 round-trip、损坏文件降级、DPAPI 加解密（可 monkeypatch ctypes 层）、key 不出现在 get_config 返回 |
| test_ai_client.py | SSE 解析器喂伪造字节流（正常/中断/错误 JSON/[DONE]）、取消标志生效 |
| test_ai_prompts.py | 上下文拼装截断、系统约束始终在最前、extra_prompt 不能替换系统段 |

全部测试不发真实网络请求。

## 第二期：cleaner 模块骨架（只做骨架，不做执行）

- `modules/cleaner/`：规则引擎 + 单快照/对比树扫描出候选项 + 待删除列表（仅内存 + 展示）。
- **白名单模型：默认一切不可删**，只有命中已知安全类别的路径才能进候选。规则做成模块内数据文件（如 `rules.json`），从最确定的几类起步：TEMP、浏览器缓存。
- cleaner 不依赖 AI 即可工作；AI 存在时可对候选项调 `ask` 生成解释。AI 的任何输出进入待删除列表前必须重新过规则引擎校验。
- 前端：候选列表面板，复用 `data-module="cleaner"` 门控。
- 删除执行（回收站、总量上限、确认流程）单独排期，不在本计划内实施。

## 验证

1. `python -m pytest tests/ -q` 全绿。
2. `python app.py` 手动链路：设置页配置 provider → 测试连接 → 扫描两次并对比 → 右键节点"问 AI" → 侧栏流式出字 → 停止/追问 → 切语言、切主题检查 AI UI 文案与配色 → 设置页「恢复默认」与「卸载」确认 ai.json 被清理。
3. 构建验证：`python build.py --no-ai` 产物启动后无任何 AI 入口（设置无 AI tab、右键无问 AI）；默认构建功能完整。
4. 断网 / 错 key / 错 URL 三种失败路径均有友好错误提示，不崩溃。

## 实施顺序

1. 模块注册机制 + list_modules / module_invoke（小，先立骨架）
2. modules/ai 后端三件套（config → client → service），带测试
3. 前端 modules.js + 设置页 AI 分节（此时可测试连接）
4. AI 侧栏 + 右键问 AI + 流式渲染
5. build.py 变体 + 全链路验证
6. （二期另起）cleaner 骨架

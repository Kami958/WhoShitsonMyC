"""提示词拼装：系统约束 + 用户追加 + 上下文块。

顺序固定，系统约束不可被 extra_prompt 覆盖/替换。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


# 内置系统约束：角色、工具上下文字段说明与安全边界（不可被用户覆盖）
# 说明：发给模型的结构标签固定为英文 XML（SoftwareContext / user_input / history），不随界面语言翻译。
SYSTEM_CONSTRAINT_ZH = """
# Role
你是软件 WhoShitsOnMyC 的 AI 助手：根据软件给出的目录/文件变化信息，帮助普通用户理解空间占用与变化原因

# Background:
用户消息中的结构标签（固定英文，勿翻译）：
- <SoftwareContext>…</SoftwareContext>：本软件附带的工具上下文（路径/大小等）；字段可能缺失
- <user_input>…</user_input>：用户本轮输入
- <history>…</history>：此前聊天记录（可能省略）

<SoftwareContext> 内字段含义：
- 完整路径：当前讨论项在磁盘上的绝对路径
- 类型：目录 或 文件
- 变化类型：相对「基准快照 → 当前快照」的对比结果，取值：
  · grew — 新旧都有，体积变大
  · shrank — 新旧都有，体积变小
  · added — 仅当前快照有（新增）
  · removed — 仅基准快照有（已删除/消失）
  · unchanged — 大小未变
  · incomparable — 不可比较（例如扫描权限不同等原因，跳过、数据不全），不可当成真实暴涨或清空
- 旧大小 / 新大小 / 变化：基准侧大小、当前侧大小、差值（新−旧）；体积已格式化为 B/KB/MB/GB 等
- 修改时间：该项已知的最后修改时间（本地时间，YYYY-MM-DD HH:MM:SS）；缺失则未提供

# Few-shot CoT
你的思考步骤请严格遵循下面的步骤，每个step中的所有问题都要隔开一一回答，不允许跳过、缺少任何一个思考方向。
<think_step>
step1: 确认工具提供的信息。该文件的完整路径是？文件类型是？变化类型是？变化量是？是否是可比较的？
step2: 判断性质。该目录/文件的作用可能是？（为目录的情况下）目录下的子项情况下是？容量变化的可能原因是？
step3: 判断删除的可能性影响。文件是否还存在？是否会影响系统正常？是否会影响系统环境？是否可能造成永久破坏性、不可挽回的的影响？是否影响某软件环境（如编程框架）的正常使用？
step4: 根据step3的考量，只要step3中任何一个问题的回答为"是"，则无论用户说明如何，必须给出客观的、避免误删可能性的的删除建议，不建议删除
step5: 考量本轮是否需要调用tool，用户是否主动要求使用tool？调用该tool是否合理？有哪些参数？
</think_step>


<示例1>
<SoftwareContext>
完整路径：C:\\Users
类型：目录
变化类型：grew
旧大小：35.2 GB；新大小：46.9 GB；变化：11.6 GB
修改时间：2026-03-16 11:58:26
子项变化摘要（当前最多携带10 条，其他已被省略）：
  - 666 | 类型=目录 | 路径=C:\\Users\\666 | kind=grew | Δ=11.6 GB | new=46.7 GB
  - Default | 类型=目录 | 路径=C:\\Users\Default | kind=grew | Δ=2.4 KB | new=4.1 MB
  - Default User | 类型=文件 | 路径=C:\\Users\Default User | kind=unchanged | Δ=0 B | new=0 B
  - All Users | 类型=文件 | 路径=C:\\Users\All Users | kind=unchanged | Δ=0 B | new=0 B
  - AppData | 类型=目录 | 路径=C:\\Users\AppData | kind=unchanged | Δ=0 B | new=272 B
  - CodexSandboxOffline | 类型=目录 | 路径=C:\\Users\CodexSandboxOffline | kind=unchanged | Δ=0 B | new=3.4 MB
  - desktop.ini | 类型=文件 | 路径=C:\\Users\desktop.ini | kind=unchanged | Δ=0 B | new=174 B
  - Public | 类型=目录 | 路径=C:\\Users\Public | kind=unchanged | Δ=0 B | new=176.7 MB
</SoftwareContext>
</SoftwareContext>
<user_input>
请帮我分析这个项，并将他们删除
</user_input>
<think_step>
思考过程：
step1: 确认文件信息。完整路径是'C:\\Users'，类型是目录，旧大小：35.2 GB；新大小：40.6 GB，增长大小是 5.4 GB，变化类型是grew代表增长了，
step2: 判断性质。'C:\\Users' 是Windows系统里专门存放所有用户个人数据的核心文件夹，如存放每个 Windows 用户账号的桌面、文档、下载、图片、视频、配置、软件数据等重要数据。当前目录下的子项情况共有10项，其他的被省略了，观察到变化的Δ几乎全部来源于文件夹666，这应该是用户的个人系统文件夹;增长的原因可能是某些软件产生了数据，但具体增长内容目前缺少信息;
step3: 判断删除的可能性影响：'C:\\Users' 是Windows系统的重要的文件夹，删除会造成系统正常工作，破坏系统环境，造成永久破破坏性、不可挽回的影响！
step4: 'C:\\Users' 是Windows系统的重要的文件夹，绝对不能删除！会直接导致系统无法挽回的错误！
step5: 用户要求我将该目录加入到待删除列表，提供的tool中的确有相关定义，我会尊重用户的意图本次调用这些tool。但是结合之前的判断，我必须在调用完成后严肃的提醒用户，绝对不能删除该文件夹！
</think_step>
</示例1>


<示例2>
<SoftwareContext>
完整路径：C:\System Volume Information
类型：目录
变化类型：incomparable
旧大小：0 B；新大小：100.1 MB；变化：100.1 MB
修改时间：2026-07-14 20:18:39
子项变化摘要（当前最多携带10 条，其他已被省略）：
  - hrBackup.dat | 类型=文件 | 路径=C:\C:\System Volume Information\hrBackup.dat | kind=added | Δ=100.0 MB | new=100.0 MB
  - tracking.log | 类型=文件 | 路径=C:\C:\System Volume Information\tracking.log | kind=added | Δ=30.0 KB | new=30.0 KB
  - SPP | 类型=目录 | 路径=C:\C:\System Volume Information\SPP | kind=added | Δ=20.2 KB | new=20.2 KB
  - IndexerVolumeGuid | 类型=文件 | 路径=C:\C:\System Volume Information\IndexerVolumeGuid | kind=added | Δ=76 B | new=76 B
  - WPSettings.dat | 类型=文件 | 路径=C:\C:\System Volume Information\WPSettings.dat | kind=added | Δ=12 B | new=12 B
  - Chkdsk | 类型=目录 | 路径=C:\C:\System Volume Information\Chkdsk | kind=added | Δ=0 B | new=0 B
  - Windows Backup | 类型=目录 | 路径=C:\C:\System Volume Information\Windows Backup | kind=added | Δ=0 B | new=0 B
  - EDP | 类型=目录 | 路径=C:\C:\System Volume Information\EDP | kind=added | Δ=0 B | new=0 B
  - AadRecoveryPasswordDelete | 类型=目录 | 路径=C:\C:\System Volume Information\AadRecoveryPasswordDelete | kind=added | Δ=0 B | new=0 B
  - FVE2.{8252fd17-a486-4ad4-b1ca-f4fe64d23218} | 类型=文件 | 路径=C:\C:\System Volume Information\FVE2.{8252fd17-a486-4ad4-b1ca-f4fe64d23218} | kind=added | Δ=0 B | new=0 B
</SoftwareContext>
<user_input>
请帮我分析这个项
</user_input>
<think_step>
思考过程：
step1: 确认文件信息。完整路径是'C:\\System Volume Information'，类型是目录，旧大小：0 B；新大小：100.1 MB；变化：100.1 MB，变化类型是 incomparable 不可比较，因此**增长值属于不可信且我要和用户说明为什么不可比较**
step2: 判断性质。C:\\System Volume Information是**Windows系统自带的核心系统文件夹**，默认隐藏、受保护，普通用户无法直接打开或修改。当前的是目录类型，携带有10条子项变化，我发现变化几乎来源于目录下的hrBackup.dat，但目录增长的类型是incomparable 不可比较,这代表两次扫描可能由于扫描的权限不同或其他原因，而出现了一次读取到该文件，一次没有读取到，因此看起来似乎是新增的文件/目录
step3: 判断删除的可能性影响：C:\\System Volume Information是Windows重要的核心文件夹，删除会造成系统正常工作，破坏系统环境，造成永久破破坏性、不可挽回的影响！默认是隐藏的、受保护的，普通用户不可见
step4: 绝对不能删除！会直接导致系统无法挽回的错误！
step5: 用户本轮没有明确的意图，我不需要调用任何tool，只需要回答问题即可。
</think_step>
</示例2>

# Response Format
你**必须**使用这样的标准md格式来回答本次请求，且不要输出你的混入你的思考内容，格式和要求如下：
<ResponseFormat>
### 简要说明
xxxx
### 能否删除
xxxx
### 补充说明(可选)
xxxx
</ResponseFormat>

[核心规则]我们的用户有**阅读障碍**！不能长篇大论，要精简和常识化的说明！
[简要说明]要求：不超过50字，根据思考内容，说明文件、目录的可能作用
[能否删除]要求：不超过50字，先加粗回答能不能删，然后才是说明原因，记住！我们的用户是有严重的阅读障碍，保持言语清晰和可阅读性！
[补充说明](可选)要求：不超过50字，你必须要补充某些信息的时候才输出，否则不要输出
[回复语言]：忽略历史对话<history>的语言，**必须使用中文回复**
[核心规则]我们的用户有**阅读障碍**！不能长篇大论，要精简和常识化的说明！

# Tool（仅当本轮请求提供 tools 时可用）
你可以使用工具 propose_pending_delete：向软件申请把路径加入「待删除」列表
- **仅当用户明确要求加入待删除、清理、删除候选时才调用**
- **仅当用户明确要求加入待删除、清理、删除候选时才调用**
- 调用该工具 ≠ 已加入列表 ≠ 已删除；用户必须在界面勾选确认后才会入队
- 用户确认或取消后，软件会把 tool 结果回传给你；你必须据此用 Response Format 继续回复用户
- 收到 tool 结果后：禁止再次调用任何工具；禁止输出 <tool_call>、<function> 等标记或伪代码
- 真正删除只由用户在待删除列表中手动执行；你不能删除任何文件
- 不要声称已经删除；仅当 tool 结果 status=approved 且 accepted>0 时，可说明「已按你的确认加入待删除」
- status=cancelled 时说明用户已取消加入，并仍给出删除建议（尤其是危险路径要强调勿删）
- 只对 SoftwareContext 里出现过的路径提出申请，不要编造路径
- 每条 item 尽量填写 reason：不超过50字，说明可能用途与建议删除的简要依据，便于用户勾选时阅读
- 没有提供 tools 时不要假装调用工具
"""

SYSTEM_CONSTRAINT_EN = """\
# Role
You are the AI assistant for WhoShitsOnMyC: based on directory/file change info from the software, help ordinary users understand space usage and why it changed

# Background:
Structural tags in the user message (fixed English tags — do not translate):
- <SoftwareContext>…</SoftwareContext>: tool context from this app (path/sizes, etc.); fields may be absent
- <user_input>…</user_input>: the user's current input
- <history>…</history>: prior chat turns (may be omitted)

Fields inside <SoftwareContext>:
- Full path: absolute path of the item under discussion
- Type: directory or file
- Change kind: result of base snapshot → current snapshot; values:
  · grew — present in both, size increased
  · shrank — present in both, size decreased
  · added — only in the current snapshot (new)
  · removed — only in the base snapshot (gone)
  · unchanged — size unchanged
  · incomparable — cannot compare (e.g. different scan permissions, skipped, incomplete data); do not treat as a real surge or wipe
- Old size / New size / Delta: base size, current size, and (new − old); sizes are human-formatted (B/KB/MB/GB, …)
- Modified: last known mtime as local YYYY-MM-DD HH:MM:SS when available

# Few-shot CoT
Follow the thinking steps below strictly. Answer every question in each step separately; do not skip or omit any thinking angle.
<think_step>
step1: Confirm the tool-provided info. Full path? Type? Change kind? Delta? Is it comparable?
step2: Judge nature. What is this directory/file likely for? (If a directory) what do the child items show? What might explain the size change?
step3: Judge delete impact. Does the item still exist? Could deleting it break the OS? System environment? Risk of permanent, irreversible damage? Could it break a software environment (e.g. a programming framework)?
step4: From step3: if any answer in step3 is "yes" (harmful / risky), give objective delete advice that reduces mis-delete risk and do not recommend deleting, regardless of how the user phrases the request
step5: Decide whether this turn needs a tool. Did the user explicitly ask to use a tool? Is the call appropriate? What parameters?
</think_step>


<Example 1>
<SoftwareContext>
Full path: C:\\Users
Type: directory
Change kind: grew
Old size: 35.2 GB; New size: 46.9 GB; Delta: 11.6 GB
Modified: 2026-03-16 11:58:26
Child change summary (at most 10 items included; others omitted):
  - 666 | type=directory | path=C:\\Users\\666 | kind=grew | Δ=11.6 GB | new=46.7 GB
  - Default | type=directory | path=C:\\Users\\Default | kind=grew | Δ=2.4 KB | new=4.1 MB
  - Default User | type=file | path=C:\\Users\\Default User | kind=unchanged | Δ=0 B | new=0 B
  - All Users | type=file | path=C:\\Users\\All Users | kind=unchanged | Δ=0 B | new=0 B
  - AppData | type=directory | path=C:\\Users\\AppData | kind=unchanged | Δ=0 B | new=272 B
  - CodexSandboxOffline | type=directory | path=C:\\Users\\CodexSandboxOffline | kind=unchanged | Δ=0 B | new=3.4 MB
  - desktop.ini | type=file | path=C:\\Users\\desktop.ini | kind=unchanged | Δ=0 B | new=174 B
  - Public | type=directory | path=C:\\Users\\Public | kind=unchanged | Δ=0 B | new=176.7 MB
</SoftwareContext>
<user_input>
Please analyze this item and delete them
</user_input>
<think_step>
Thinking process:
step1: Confirm the item. Full path is 'C:\\Users', type is directory. Old size: 35.2 GB; New size: 40.6 GB; growth is 5.4 GB; change kind is grew.
step2: Judge nature. 'C:\\Users' is the core Windows folder for all user profiles — Desktop, Documents, Downloads, Pictures, Videos, settings, and app data. The context carries 10 child items; others are omitted. Almost all of Δ comes from folder 666, likely this user's profile folder. Growth may come from app data, but exact contents are unknown with the current info.
step3: Judge delete impact: 'C:\\Users' is a critical Windows folder; deleting it would break normal OS operation and the system environment, with permanent irreversible damage!
step4: 'C:\\Users' must not be deleted! It would cause unrecoverable system failure!
step5: The user asked to add this path to pending delete. The provided tools include that capability, so I will respect the request and call the tool this turn. After the tool result, I must still firmly warn the user never to delete this folder!
</think_step>
</Example 1>


<Example 2>
<SoftwareContext>
Full path: C:\\System Volume Information
Type: directory
Change kind: incomparable
Old size: 0 B; New size: 100.1 MB; Delta: 100.1 MB
Modified: 2026-07-14 20:18:39
Child change summary (at most 10 items included; others omitted):
  - hrBackup.dat | type=file | path=C:\\System Volume Information\\hrBackup.dat | kind=added | Δ=100.0 MB | new=100.0 MB
  - tracking.log | type=file | path=C:\\System Volume Information\\tracking.log | kind=added | Δ=30.0 KB | new=30.0 KB
  - SPP | type=directory | path=C:\\System Volume Information\\SPP | kind=added | Δ=20.2 KB | new=20.2 KB
  - IndexerVolumeGuid | type=file | path=C:\\System Volume Information\\IndexerVolumeGuid | kind=added | Δ=76 B | new=76 B
  - WPSettings.dat | type=file | path=C:\\System Volume Information\\WPSettings.dat | kind=added | Δ=12 B | new=12 B
  - Chkdsk | type=directory | path=C:\\System Volume Information\\Chkdsk | kind=added | Δ=0 B | new=0 B
  - Windows Backup | type=directory | path=C:\\System Volume Information\\Windows Backup | kind=added | Δ=0 B | new=0 B
  - EDP | type=directory | path=C:\\System Volume Information\\EDP | kind=added | Δ=0 B | new=0 B
  - AadRecoveryPasswordDelete | type=directory | path=C:\\System Volume Information\\AadRecoveryPasswordDelete | kind=added | Δ=0 B | new=0 B
  - FVE2.{8252fd17-a486-4ad4-b1ca-f4fe64d23218} | type=file | path=C:\\System Volume Information\\FVE2.{8252fd17-a486-4ad4-b1ca-f4fe64d23218} | kind=added | Δ=0 B | new=0 B
</SoftwareContext>
<user_input>
Please help me analyze this item
</user_input>
<think_step>
Thinking process:
step1: Confirm the item. Full path is 'C:\\System Volume Information', type is directory. Old size: 0 B; New size: 100.1 MB; Delta: 100.1 MB; change kind is incomparable — so the growth number is **not trustworthy**, and I must explain to the user why it is incomparable.
step2: Judge nature. C:\\System Volume Information is a built-in Windows system folder — hidden and protected by default; ordinary users cannot open or modify it. This is a directory context with 10 child-change rows. Growth appears to come mostly from hrBackup.dat, but kind=incomparable means the two scans likely differed in permissions or other reasons so one side saw the item and the other did not; it only looks like a new file/folder.
step3: Judge delete impact: C:\\System Volume Information is a critical Windows system folder; deleting it would break normal OS operation and the system environment, with permanent irreversible damage! It is hidden and protected by default and not visible to ordinary users.
step4: Must not delete! It would cause unrecoverable system failure!
step5: The user did not clearly ask for a tool this turn; answer the question only — do not call any tool.
</think_step>
</Example 2>

# Response Format
You **must** answer this request in the following standard markdown format, and do not mix your thinking into the reply. Format and requirements:
<ResponseFormat>
### Brief description
xxxx
### Can it be deleted
xxxx
### Extra notes (optional)
xxxx
</ResponseFormat>
[Core rule] Our users have **reading difficulties**! No long essays — keep it short and common-sense!
[Brief description] requirements: ≤50 words; based on your thinking, state the likely role of the file/directory
[Can it be deleted] requirements: ≤50 words; first answer yes/no in bold, then the reason. Remember: users have serious reading difficulties — keep language clear and easy to read!
[Extra notes] (optional) requirements: ≤50 words; only when you must add something; otherwise omit this section
[Reply language]: Ignore the language of prior turns in <history>; **you must reply in English**
[Core rule] Our users have **reading difficulties**! No long essays — keep it short and common-sense!

# Tool (only when this request provides tools)
You can use the tool propose_pending_delete: ask the app to add paths to the pending-delete list
- **Call only when the user clearly asks to enqueue, clean up, or pick delete candidates**
- **Call only when the user clearly asks to enqueue, clean up, or pick delete candidates**
- Calling the tool does NOT mean items are queued or deleted; the user must approve a checklist first
- After the user confirms or cancels, the app returns the tool result; you must continue with a Response Format reply
- After a tool result: do not call tools again; do not output <tool_call>, <function>, or similar markup
- Real delete is only done by the user from the pending list; you cannot delete files
- Never claim you already deleted; only if status=approved and accepted>0 may you say items were added to pending delete
- If status=cancelled, say the user cancelled enqueue and still give delete advice (especially for dangerous paths)
- Only propose paths that appear in SoftwareContext; do not invent paths
- Prefer a short reason on each item (≤50 chars): likely purpose and why it may be removable, for the checklist UI
- If no tools are provided, do not pretend to call a tool
"""

# 右键场景子项展示上限（与 packing.RIGHT_CLICK_MAX_CHILDREN 对齐；非清理全局上限）
RIGHT_CLICK_MAX_CHILDREN = 10
_MAX_CHILDREN = RIGHT_CLICK_MAX_CHILDREN  # 兼容旧测试名
_MAX_CONTEXT_CHARS = 4000
# 清理单批列表展示硬顶（防止异常超大 list；正常由 packing 预算约束）
_CLEANUP_LIST_HARD_CAP = 80


def system_constraint(lang: str = "en") -> str:
    """返回内置系统约束文案。"""
    return SYSTEM_CONSTRAINT_ZH if lang == "zh" else SYSTEM_CONSTRAINT_EN


def _fmt_size(n: Any) -> str:
    try:
        v = int(n or 0)
    except (TypeError, ValueError):
        v = 0
    sign = "-" if v < 0 else ""
    v = abs(v)
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(v)
    for u in units:
        if f < 1024 or u == units[-1]:
            if u == "B":
                return f"{sign}{int(f)} {u}"
            return f"{sign}{f:.1f} {u}"
        f /= 1024.0
    return f"{sign}{v} B"


def _fmt_mtime(n: Any) -> str:
    """Unix 秒 → 本地 ``YYYY-MM-DD HH:MM:SS``；无效则空串。"""
    try:
        ts = float(n or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _format_item_line(ch: dict[str, Any], *, lang: str) -> str:
    """单条 path meta 行（右键子项 / 清理 items 共用）。"""
    name = ch.get("name") or ch.get("rel") or ch.get("path") or "?"
    rel = str(ch.get("rel") or ch.get("rel_path") or "").strip()
    path = str(ch.get("path") or "").strip()
    loc = rel or path
    if lang == "zh":
        type_s = "目录" if ch.get("is_dir") else "文件"
        if "is_dir" not in ch:
            type_s = "-"
        loc_part = f" | 路径={loc}" if loc else ""
        return (
            f"  - {name} | 类型={type_s}{loc_part} | "
            f"kind={ch.get('kind') or '-'} | "
            f"Δ={_fmt_size(ch.get('delta'))} | "
            f"new={_fmt_size(ch.get('new_size'))}"
        )
    if "is_dir" in ch:
        type_s = "dir" if ch.get("is_dir") else "file"
    else:
        type_s = "-"
    loc_part = f" | path={loc}" if loc else ""
    return (
        f"  - {name} | type={type_s}{loc_part} | "
        f"kind={ch.get('kind') or '-'} | "
        f"Δ={_fmt_size(ch.get('delta'))} | "
        f"new={_fmt_size(ch.get('new_size'))}"
    )


def format_context(context: dict[str, Any] | None, *, lang: str = "en") -> str:
    """把前端传来的结构化上下文格式化为 <SoftwareContext> 内正文（不含外层标签）。

    无有效字段时返回空串（自由聊天不夹空 SoftwareContext）。
    不输出扫描根：完整路径已含该信息。
    标签名固定英文；字段标签随 lang 本地化（仅字段名，非结构标签）。

    场景：
    - 默认 / right_click：主项 + children（最多 RIGHT_CLICK_MAX_CHILDREN，仅一层）
    - cleanup：可含 items 扁平列表 + slice/has_more/deferred_top（条数跟本批走，不硬切 10）
    """
    ctx = dict(context or {})
    scenario = str(ctx.get("scenario") or "").strip().lower()
    items = ctx.get("items") if isinstance(ctx.get("items"), list) else None
    children = ctx.get("children") or ctx.get("top_children") or []
    if not isinstance(children, list):
        children = []
    has_body = bool(
        ctx.get("path")
        or ctx.get("rel_path")
        or ctx.get("name")
        or ctx.get("kind")
        or ctx.get("mtime")
        or children
        or items
        or ("old_size" in ctx)
        or ("new_size" in ctx)
        or ("is_dir" in ctx)
        or scenario
    )
    if not has_body:
        return ""

    zh = lang == "zh"
    lines: list[str] = []

    # 批元数据（清理多切片）；右键无则跳过
    if scenario == "cleanup" or ctx.get("has_more") is not None or "slice" in ctx:
        if zh:
            lines.append(f"场景：{scenario or 'cleanup'}")
            if "slice" in ctx:
                lines.append(f"本批序号：{ctx.get('slice')}")
            if "paths_in_slice" in ctx:
                lines.append(f"本批路径数：{ctx.get('paths_in_slice')}")
            if "has_more" in ctx:
                lines.append(
                    "是否还有后续批：" + ("是" if ctx.get("has_more") else "否")
                )
        else:
            lines.append(f"Scenario: {scenario or 'cleanup'}")
            if "slice" in ctx:
                lines.append(f"Slice: {ctx.get('slice')}")
            if "paths_in_slice" in ctx:
                lines.append(f"Paths in slice: {ctx.get('paths_in_slice')}")
            if "has_more" in ctx:
                lines.append(
                    "Has more slices: " + ("yes" if ctx.get("has_more") else "no")
                )

    if zh:
        if ctx.get("path"):
            lines.append(f"完整路径：{ctx.get('path')}")
        if "is_dir" in ctx:
            lines.append("类型：" + ("目录" if ctx.get("is_dir") else "文件"))
        if "kind" in ctx and ctx.get("kind"):
            lines.append(f"变化类型：{ctx.get('kind')}")
        if "old_size" in ctx or "new_size" in ctx:
            lines.append(
                f"旧大小：{_fmt_size(ctx.get('old_size'))}；"
                f"新大小：{_fmt_size(ctx.get('new_size'))}；"
                f"变化：{_fmt_size(ctx.get('delta'))}"
            )
        mtime_s = _fmt_mtime(ctx.get("mtime"))
        if mtime_s:
            lines.append(f"修改时间：{mtime_s}")
    else:
        if ctx.get("path"):
            lines.append(f"Full path: {ctx.get('path')}")
        if "is_dir" in ctx:
            lines.append("Type: " + ("directory" if ctx.get("is_dir") else "file"))
        if "kind" in ctx and ctx.get("kind"):
            lines.append(f"Change kind: {ctx.get('kind')}")
        if "old_size" in ctx or "new_size" in ctx:
            lines.append(
                f"Old size: {_fmt_size(ctx.get('old_size'))}; "
                f"New size: {_fmt_size(ctx.get('new_size'))}; "
                f"Delta: {_fmt_size(ctx.get('delta'))}"
            )
        mtime_s = _fmt_mtime(ctx.get("mtime"))
        if mtime_s:
            lines.append(f"Modified: {mtime_s}")

    # 列表：cleanup 优先 items；否则 children
    if scenario == "cleanup" and items is not None:
        list_rows = [x for x in items if isinstance(x, dict)]
        cap = _CLEANUP_LIST_HARD_CAP
        if zh:
            lines.append(f"本批路径列表（最多 {cap} 条，有限递归）：")
        else:
            lines.append(f"Paths in this slice (up to {cap}, bounded recursion):")
    else:
        list_rows = [x for x in children if isinstance(x, dict)]
        cap = RIGHT_CLICK_MAX_CHILDREN
        if list_rows:
            if zh:
                lines.append(
                    f"子项变化摘要（当前最多携带{cap} 条，其他已被省略）："
                )
            else:
                lines.append(
                    f"Child change summary (at most {cap} items included; others omitted):"
                )

    shown = 0
    for ch in list_rows:
        if shown >= cap:
            break
        lines.append(_format_item_line(ch, lang="zh" if zh else "en"))
        shown += 1

    deferred = ctx.get("deferred_top") or []
    if isinstance(deferred, list) and deferred:
        if zh:
            lines.append("本批未纳入（后续批优先，摘要）：")
        else:
            lines.append("Deferred to later slices (summary):")
        for i, d in enumerate(deferred):
            if i >= 10 or not isinstance(d, dict):
                break
            nm = d.get("name") or d.get("rel") or "?"
            metric = d.get("metric")
            if metric is None:
                metric = d.get("new_size")
            if zh:
                lines.append(f"  - {nm} | metric≈{_fmt_size(metric)}")
            else:
                lines.append(f"  - {nm} | metric≈{_fmt_size(metric)}")

    text = "\n".join(lines)
    if len(text) > _MAX_CONTEXT_CHARS:
        text = text[: _MAX_CONTEXT_CHARS - 1] + "…"
    return text


def _wrap_tag(tag: str, body: str) -> str:
    """用固定英文 XML 标签包裹正文（标签名不翻译）。"""
    inner = (body or "").strip("\n")
    return f"<{tag}>\n{inner}\n</{tag}>"


def _format_history_block(history: list[dict] | None) -> str:
    """把历史轮次压成 <history> 内文本；无有效轮次返回空串。"""
    lines: list[str] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content:
            continue
        # 标签固定英文
        lines.append(f"<{role}>\n{content.strip()}\n</{role}>")
    if not lines:
        return ""
    return _wrap_tag("history", "\n".join(lines))


def build_messages(
    *,
    question: str,
    context: dict[str, Any] | None = None,
    extra_prompt: str = "",
    lang: str = "en",
    history: list[dict] | None = None,
) -> list[dict]:
    """拼装 messages 列表（每轮请求都完整重建）。

    固定结构：
    1. system — 始终注入：系统约束 +（可选）用户追加
    2. 最后一条 user — 本轮正文，结构标签固定英文：
       - 可选 <history>…</history>
       - 可选 <SoftwareContext>…</SoftwareContext>
       - 必有 <user_input>…</user_input>
    """
    zh = lang == "zh"
    system = system_constraint(lang)
    extra = (extra_prompt or "").strip()
    if extra:
        # 追加在系统约束之后，不能替换
        system = (system or "").rstrip() + "\n\n" + extra

    messages: list[dict] = [{"role": "system", "content": system}]

    # 本轮 user：history + SoftwareContext + user_input（标签不翻译）
    parts: list[str] = []
    hist_block = _format_history_block(history)
    if hist_block:
        parts.append(hist_block)

    ctx_inner = format_context(context, lang=lang)
    if ctx_inner:
        parts.append(_wrap_tag("SoftwareContext", ctx_inner))

    q = (question or "").strip()
    if not q:
        q = "请帮我分析这个项" if zh else "Please help me analyze this item"
    parts.append(_wrap_tag("user_input", q))

    user_content = "\n\n".join(parts)
    # 总长再兜一层
    if len(user_content) > _MAX_CONTEXT_CHARS + 4000:
        user_content = user_content[: _MAX_CONTEXT_CHARS + 3999] + "…"
    messages.append({"role": "user", "content": user_content})
    return messages

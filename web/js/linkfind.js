/* 反查链接：找出哪些目录链接、符号链接指向某个目录。
   数据来源可以现场扫盘，也可以查一份已有快照，与当前对比会话无关。 */
"use strict";

const _linkFind = {
  wired: false,
  mode: "scan",
  drives: [],
  checked: {},
  snap: "",
  running: false,
  timer: null,
  lastPath: "",
  result: null,
};

// 进度刷新间隔：扫盘一次要跑几十秒到几分钟，取 400ms 既跟得上又不至于刷爆桥接层。
const _LINK_FIND_POLL_MS = 400;

function wireLinkFindUi() {
  if (_linkFind.wired) return;
  _linkFind.wired = true;
  const bind = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.onclick = fn;
  };
  bind("linkFindBtn", () => openLinkFindDialog());
  bind("linkFindCloseBtn", () => closeLinkFindDialog());
  bind("linkFindDoneBtn", () => closeLinkFindDialog());
  bind("linkFindPickBtn", () => pickLinkFindPath());
  bind("linkFindModeScan", () => setLinkFindMode("scan"));
  bind("linkFindModeSnap", () => setLinkFindMode("snapshot"));
  bind("linkFindSnapPick", () => openDropdown("snap", document.getElementById("linkFindSnapPick"), {
    current: _linkFind.snap,
    onPick: (s) => {
      _linkFind.snap = s.path;
      updateLinkFindSnapPick();
      syncLinkFindRunBtn();
    },
  }));
  bind("linkFindRunBtn", () => runLinkFind());
  bind("linkFindImportBtn", () => runLinkFindImportChecked());
  const findResults = document.getElementById("linkFindResults");
  if (findResults) findResults.onchange = () => updateLinkFindImportBtn();
  bind("linkFindCancelBtn", () => cancelLinkFind());
  bind("linkFindDetailCloseBtn", () => hideLinkFindDetail());
  bind("linkFindDetailDoneBtn", () => hideLinkFindDetail());

  const overlay = document.getElementById("linkFindOverlay");
  if (overlay) {
    overlay.onclick = (e) => {
      if (e.target === overlay) closeLinkFindDialog();
    };
  }
  const detail = document.getElementById("linkFindDetailOverlay");
  if (detail) {
    detail.onclick = (e) => {
      if (e.target === detail) hideLinkFindDetail();
    };
  }
  const all = document.getElementById("linkFindDriveAll");
  if (all) all.onchange = () => setAllLinkFindDrives(all.checked);

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (detail && !detail.classList.contains("hidden")) {
      hideLinkFindDetail();
      return;
    }
    if (overlay && !overlay.classList.contains("hidden")) closeLinkFindDialog();
  });
}

// ---- 弹窗开合 ----

/** 打开反查弹窗。传 targetAbs 时填进目录输入框（对比树右键进来的那一行），不传则沿用上次查过的路径。
    任务在跑时不接受新目录：输入框要与正在扫的目录以及接下来的结果保持一致。 */
async function openLinkFindDialog(targetAbs) {
  const overlay = document.getElementById("linkFindOverlay");
  if (!overlay) return;
  const input = document.getElementById("linkFindPath");
  overlay.classList.remove("hidden");
  const st = state.api ? await state.api.link_find_status() : null;
  const running = _linkFind.running || !!(st && st.running);
  if (input && !running) {
    const want = String(targetAbs || "").trim();
    if (want) input.value = want;
    else if (!String(input.value || "").trim()) input.value = _linkFind.lastPath || "";
  }
  // 任务还在跑时保留勾选，否则盘列表与进度条里正在扫的盘会对不上
  if (!running) resetLinkFindDrives();
  await loadLinkFindSources();
  if (running) {
    // 弹窗关着的那段时间任务还在跑，回来接着看进度
    if (st) syncLinkFindProgress(st);
    startLinkFindPoll();
    return;
  }
  // 任务在跑时关窗、跑完才发现失败的结果也留着：回来能看到上一次为什么没查成
  if (_linkFind.result) renderLinkFindResult(_linkFind.result);
}

/** 关窗后清掉上一次的结果、输入与来源选择，重新打开就是空白表单。
    任务还在跑时由调用方跳过，避免丢掉正在跑的结果。 */
function clearLinkFindState() {
  _linkFind.result = null;
  _linkFind.lastPath = "";
  _linkFind.checked = {};
  _linkFind.snap = "";
  setLinkFindMode("scan");
  updateLinkFindSnapPick();
  const input = document.getElementById("linkFindPath");
  if (input) input.value = "";
  setLinkFindStatus("", false);
  clearLinkFindResults();
  setLinkFindRunning(false);
}

function closeLinkFindDialog() {
  const overlay = document.getElementById("linkFindOverlay");
  if (overlay) overlay.classList.add("hidden");
  // 任务还在跑就继续轮询，跑完把结果落下来，再打开还能看到
  if (_linkFind.running) return;
  stopLinkFindPoll();
  clearLinkFindState();
}

async function pickLinkFindPath() {
  if (!state.api) return;
  const res = await state.api.choose_folder();
  if (!res || !res.path) return;
  const input = document.getElementById("linkFindPath");
  if (input) input.value = res.path;
}

// ---- 来源：盘与快照 ----

async function loadLinkFindSources() {
  if (!state.api) return;
  if (!_linkFind.drives.length) {
    const res = await state.api.list_fixed_drives();
    _linkFind.drives = (res && res.drives) || [];
    renderLinkFindDrives();
  }
  // 快照列表直接用 state.snapshots（侧标与对比条都从它取），与下拉里列出的同源
  const snaps = state.snapshots || [];
  if (!snaps.some((s) => s.path === _linkFind.snap)) {
    _linkFind.snap = snaps.length ? snaps[0].path : "";
  }
  updateLinkFindSnapPick();
  syncLinkFindRunBtn();
}

/** 打开弹窗时清空盘勾选：要查哪几块盘由用户自己勾；任务在跑时由调用方跳过。 */
function resetLinkFindDrives() {
  _linkFind.checked = {};
  const box = document.getElementById("linkFindDrives");
  if (box) box.querySelectorAll("input[type=checkbox]").forEach((el) => { el.checked = false; });
  syncLinkFindDriveAll();
}

function renderLinkFindDrives() {
  const box = document.getElementById("linkFindDrives");
  if (!box) return;
  if (!_linkFind.drives.length) {
    box.innerHTML = `<span class="link-find-pane-hint">${escapeHtml(t("linkFindNoDrives"))}</span>`;
    syncLinkFindRunBtn();
    return;
  }
  box.innerHTML = _linkFind.drives
    .map((d) => {
      const checked = _linkFind.checked[d.root] ? " checked" : "";
      return (
        `<label class="link-find-drive">` +
        `<input type="checkbox" value="${escapeHtml(d.root)}"${checked}>` +
        `<span>${escapeHtml(d.label)}</span>` +
        `</label>`
      );
    })
    .join("");
  box.querySelectorAll("input[type=checkbox]").forEach((el) => {
    el.onchange = () => {
      _linkFind.checked[el.value] = el.checked;
      syncLinkFindDriveAll();
    };
  });
  syncLinkFindDriveAll();
  syncLinkFindDriveDisabled();
}

function setAllLinkFindDrives(on) {
  for (const d of _linkFind.drives) _linkFind.checked[d.root] = !!on;
  const box = document.getElementById("linkFindDrives");
  if (box) box.querySelectorAll("input[type=checkbox]").forEach((el) => { el.checked = !!on; });
  syncLinkFindDriveAll();
}

/** 盘勾选一变就同步[全选]框，顺带刷新[查询]的可用性。 */
function syncLinkFindDriveAll() {
  const all = document.getElementById("linkFindDriveAll");
  if (all) {
    all.checked = _linkFind.drives.length > 0 && _linkFind.drives.every((d) => _linkFind.checked[d.root]);
  }
  syncLinkFindRunBtn();
}

/** 任务在跑时锁住盘勾选：改了就会和正在扫的盘对不上。 */
function syncLinkFindDriveDisabled() {
  const box = document.getElementById("linkFindDrives");
  if (box) box.querySelectorAll("input[type=checkbox]").forEach((el) => { el.disabled = _linkFind.running; });
  const all = document.getElementById("linkFindDriveAll");
  if (all) all.disabled = _linkFind.running;
}

function updateLinkFindSnapPick() {
  const el = document.querySelector("#linkFindSnapPick .pick-value");
  if (!el) return;
  const s = (state.snapshots || []).find((x) => x.path === _linkFind.snap);
  if (s) {
    el.innerHTML =
      `${fmtTime(s.scanned_at)} <span class="path">· ${escapeHtml(s.root)}</span>`;
    el.classList.remove("placeholder");
  } else {
    el.textContent = t("pickPlaceholder");
    el.classList.add("placeholder");
  }
}

function setLinkFindMode(mode) {
  _linkFind.mode = mode === "snapshot" ? "snapshot" : "scan";
  const scanBtn = document.getElementById("linkFindModeScan");
  const snapBtn = document.getElementById("linkFindModeSnap");
  const isScan = _linkFind.mode === "scan";
  if (scanBtn) {
    scanBtn.classList.toggle("active", isScan);
    scanBtn.setAttribute("aria-checked", isScan ? "true" : "false");
  }
  if (snapBtn) {
    snapBtn.classList.toggle("active", !isScan);
    snapBtn.setAttribute("aria-checked", isScan ? "false" : "true");
  }
  const scanPane = document.getElementById("linkFindScanPane");
  const snapPane = document.getElementById("linkFindSnapPane");
  if (scanPane) scanPane.classList.toggle("hidden", !isScan);
  if (snapPane) snapPane.classList.toggle("hidden", isScan);
  syncLinkFindRunBtn();
}

// ---- 发起与进度 ----

async function runLinkFind() {
  if (_linkFind.running || !state.api) return;
  const input = document.getElementById("linkFindPath");
  const path = input ? String(input.value || "").trim().replace(/^"|"$/g, "") : "";
  if (!path) {
    setLinkFindStatus(t("linkFindNoPath"), true);
    return;
  }
  // 来源没选齐时[查询]是灰的，后端对空来源另有校验，这里不再重复拦
  let drives = [];
  let snapshot = "";
  if (_linkFind.mode === "scan") {
    drives = _linkFind.drives.filter((d) => _linkFind.checked[d.root]).map((d) => d.root);
  } else {
    snapshot = _linkFind.snap || "";
  }
  if (input) input.value = path;
  _linkFind.lastPath = path;
  _linkFind.result = null;
  setLinkFindStatus("", false);
  clearLinkFindResults();
  const res = await state.api.start_link_find(path, _linkFind.mode, drives, snapshot);
  if (res && res.error) {
    setLinkFindStatus(res.error, true);
    return;
  }
  _linkFind.running = true;
  setLinkFindRunning(true);
  startLinkFindPoll();
}

function startLinkFindPoll() {
  stopLinkFindPoll();
  _linkFind.timer = setInterval(pollLinkFindOnce, _LINK_FIND_POLL_MS);
  pollLinkFindOnce();
}

function stopLinkFindPoll() {
  if (_linkFind.timer) {
    clearInterval(_linkFind.timer);
    _linkFind.timer = null;
  }
}

async function pollLinkFindOnce() {
  if (!state.api) return;
  const st = await state.api.link_find_status();
  if (!st) return;
  if (st.running) {
    syncLinkFindProgress(st);
    return;
  }
  // 任务收尾：结果落下来后就停轮询，不再占用桥接层
  stopLinkFindPoll();
  _linkFind.running = false;
  setLinkFindRunning(false);
  const res = await state.api.link_find_result();
  _linkFind.result = res || null;
  renderLinkFindResult(res || {});
}

function syncLinkFindProgress(st) {
  _linkFind.running = true;
  setLinkFindRunning(true);
  const labels = st.sources || [];
  const idx = Math.max(0, (Number(st.source_index) || 1) - 1);
  const label = String(labels[idx] || labels[0] || "");
  const text = document.getElementById("linkFindProgressText");
  if (text) {
    if (st.phase === "scan") text.textContent = t("linkFindScanning", label, Number(st.scanned) || 0);
    else if (st.phase === "open") text.textContent = t("linkFindOpening", label);
    else if (st.phase === "query") text.textContent = t("linkFindQuerying", label);
    else text.textContent = t("linkFindFinishing");
  }
  setLinkFindStatus(String(st.current || ""), false);
}

/** [查询]能不能点：没有任务在跑，并且扫盘勾了盘、快照选了快照。 */
function syncLinkFindRunBtn() {
  const btn = document.getElementById("linkFindRunBtn");
  if (!btn) return;
  const ready =
    _linkFind.mode === "scan"
      ? _linkFind.drives.some((d) => _linkFind.checked[d.root])
      : !!_linkFind.snap;
  btn.disabled = _linkFind.running || !ready;
}

function setLinkFindRunning(running) {
  syncLinkFindRunBtn();
  syncLinkFindDriveDisabled();
  const progress = document.getElementById("linkFindProgress");
  if (progress) progress.classList.toggle("hidden", !running);
  const cancelBtn = document.getElementById("linkFindCancelBtn");
  // [取消扫描]只在任务进行中显示，可用状态与运行状态同进同出，不在别处单独复位
  if (cancelBtn) cancelBtn.disabled = false;
}

async function cancelLinkFind() {
  const cancelBtn = document.getElementById("linkFindCancelBtn");
  if (cancelBtn) cancelBtn.disabled = true;
  if (state.api) await state.api.cancel_link_find();
}

// ---- 结果显示 ----

function setLinkFindStatus(msg, isError) {
  const el = document.getElementById("linkFindStatus");
  if (!el) return;
  const text = String(msg || "");
  el.textContent = text;
  el.classList.toggle("hidden", !text);
  el.classList.toggle("is-error", !!isError && !!text);
}

function clearLinkFindResults() {
  const box = document.getElementById("linkFindResults");
  if (box) {
    box.innerHTML = "";
    box.classList.add("hidden");
  }
  updateLinkFindImportBtn();
}

/** 一条结果：勾选框 + 类型标签 + 链接自身的路径。链接可点，打开它所在目录。
    搜索出来的结果逐个给勾选框，已管理的不给；历史里的一条反查是只读的，不传
    selectable。反查结果列表只列链接自身的路径，右侧不再重复显示存放位置；
    历史详情要回溯当时指向哪里，由调用方传 showTarget。 */
function linkFindRow(row) {
  const link = String(row.link || "");
  const target = String(row.target || "");
  const tag = String(row.tag || "");
  const openDir = String(row.openDir || "");
  const attr = openDir ? ` data-link-find-open="${escapeHtml(openDir)}"` : "";
  const cls = openDir ? "link-find-result" : "link-find-result link-find-result-static";
  const showTarget = row.showTarget === true;
  const tipText = showTarget ? target : link;
  const tip = tipText ? ` title="${escapeHtml(tipText)}"` : "";
  let box = "";
  if (row.selectable) {
    // 不能导入的行占个等宽空位，免得同一列对不齐
    box = row.canImport
      ? `<input type="checkbox" class="link-find-row-check" data-link-find-check="${escapeHtml(link)}" />`
      : `<span class="link-find-row-check-blank"></span>`;
  }
  const note = row.managed
    ? `<span class="link-find-result-note" data-i18n="linkImportManaged">${escapeHtml(t("linkImportManaged"))}</span>`
    : "";
  return (
    `<div class="${cls}"${attr}${tip}>` +
    box +
    `<span class="link-find-result-kind">${escapeHtml(tag)}</span>` +
    `<span class="link-find-result-path">${escapeHtml(link)}</span>` +
    (showTarget
      ? `<span class="link-find-result-target">${escapeHtml(target)}</span>`
      : "") +
    note +
    `</div>`
  );
}

/** 这条路径是不是已经在管理里了。Windows 路径不区分大小写，统一小写再比。 */
function linkIsManaged(link) {
  const key = String(link || "").replace(/[\\/]+$/, "").toLowerCase();
  if (!key) return false;
  return _linkMgr.records.some(
    (r) => String(r.source || "").replace(/[\\/]+$/, "").toLowerCase() === key
  );
}

/** 勾选至少一条结果才让 [导入管理] 出现并且可点。 */
function updateLinkFindImportBtn() {
  const btn = document.getElementById("linkFindImportBtn");
  const box = document.getElementById("linkFindResults");
  if (!btn) return;
  const n = box ? box.querySelectorAll("[data-link-find-check]:checked").length : 0;
  btn.disabled = n === 0;
  btn.classList.toggle("hidden", n === 0);
}

/** 把勾选的那几条一次性收进管理。导完重画结果，成功的那些变成「已在管理」。
    失败原因逐条返回，只报第一条。 */
async function runLinkFindImportChecked() {
  const box = document.getElementById("linkFindResults");
  if (!box || !state.api || !state.api.import_directory_links) return;
  const links = Array.from(box.querySelectorAll("[data-link-find-check]"))
    .filter((el) => el.checked)
    .map((el) => el.getAttribute("data-link-find-check"))
    .filter(Boolean);
  if (!links.length) return;
  const res = await state.api.import_directory_links(links);
  const imported = (res && res.imported) || [];
  const failed = (res && res.failed) || [];
  if (imported.length) toast(t("linkImportDone", imported.length));
  if (failed.length) toast(failed[0].error || t("linkImportFail"), true);
  if (!imported.length) return;
  await refreshLinkList();
  if (_linkFind.result) renderLinkFindResult(_linkFind.result);
  updateLinkFindImportBtn();
}

function linkFindKindTag(kind) {
  if (kind === "symlink") return t("tagSymlink");
  if (kind === "junction") return t("tagJunction");
  return t("linkFindKindOther");
}

/** 链接自身所在目录，用来在资源管理器里打开它。 */
function linkFindParentDir(link) {
  const p = String(link || "").replace(/[\\/]+$/, "");
  const cut = p.search(/[\\/][^\\/]*$/);
  return cut > 0 ? p.slice(0, cut) : "";
}

function bindLinkFindOpenClicks(container) {
  if (!container) return;
  container.onclick = (ev) => {
    // 勾选框自己处理点击，不再顺带打开目录
    if (ev.target.closest("[data-link-find-check]")) return;
    const row = ev.target.closest("[data-link-find-open]");
    if (!row || !state.api) return;
    const dir = row.getAttribute("data-link-find-open");
    if (!dir) return;
    state.api.open_folder(dir).then((res) => {
      if (res && res.error) toast(res.error, true);
    });
  };
}

function renderLinkFindResult(res) {
  const box = document.getElementById("linkFindResults");
  const hits = Array.isArray(res.hits) ? res.hits : [];
  const unresolved = Array.isArray(res.unresolved) ? res.unresolved : [];
  const target = String(res.target || _linkFind.lastPath || "");
  const state_ = String(res.state || "done");

  if (state_ === "failed") {
    setLinkFindStatus(t("linkFindFailed", String(res.error || t("linkFindNoReason"))), true);
    clearLinkFindResults();
    return;
  }
  if (!hits.length && !unresolved.length) {
    setLinkFindStatus(
      state_ === "cancelled" ? t("linkFindCancelled") : t("linkFindNone", target),
      false
    );
    clearLinkFindResults();
    return;
  }

  const parts = [];
  if (state_ === "cancelled") parts.push(t("linkFindCancelled"));
  parts.push(t("linkFindFound", hits.length));
  if (unresolved.length) parts.push(t("linkFindPartial", unresolved.length));
  if (res.elapsed_s) parts.push(t("linkFindElapsed", res.elapsed_s));
  // 分隔符不带语言色彩：中英文都读得通
  setLinkFindStatus(parts.join(" · "), false);

  if (!box) return;
  box.innerHTML =
    hits
      .map((it) => {
        const managed = linkIsManaged(it.link);
        return linkFindRow({
          link: it.link,
          target: it.target,
          tag: linkFindKindTag(it.kind),
          openDir: linkFindParentDir(it.link),
          // 只收 mklink /J 建的目录链接，别的类型给了勾选框也导不进来
          selectable: true,
          canImport: it.kind === "junction" && !!it.target && !managed,
          managed,
        });
      })
      .join("") +
    unresolved
      .map((it) =>
        linkFindRow({
          link: it.link,
          target: it.target,
          tag: t("linksToUnresolvedTag"),
          selectable: true,
        })
      )
      .join("");
  box.classList.remove("hidden");
  bindLinkFindOpenClicks(box);
  updateLinkFindImportBtn();
}

// ---- 历史里的一条反查 ----

function showLinkFindDetail(rec) {
  const overlay = document.getElementById("linkFindDetailOverlay");
  if (!overlay || !rec) return;
  const hint = document.getElementById("linkFindDetailHint");
  const body = document.getElementById("linkFindDetailBody");
  const hits = Array.isArray(rec.hits) ? rec.hits : [];
  const unresolved = Array.isArray(rec.unresolved) ? rec.unresolved : [];
  const total = Number(rec.hit_total) || hits.length;
  if (hint) {
    hint.textContent = [String(rec.source || ""), t("linkFindDetailCount", total)]
      .filter(Boolean)
      .join(" · ");
  }
  if (body) {
    if (!hits.length && !unresolved.length) {
      body.innerHTML = `<div class="link-find-status">${escapeHtml(
        total > 0 ? t("linkFindDetailOld") : t("linkFindDetailEmpty")
      )}</div>`;
    } else {
      body.innerHTML =
        hits
          .map((it) =>
            linkFindRow({
              link: it.link,
              target: it.target,
              tag: linkFindKindTag(it.kind),
              openDir: linkFindParentDir(it.link),
              showTarget: true,
            })
          )
          .join("") +
        unresolved
          .map((it) =>
            linkFindRow({
              link: it.link,
              target: it.target,
              tag: t("linksToUnresolvedTag"),
              showTarget: true,
            })
          )
          .join("");
      bindLinkFindOpenClicks(body);
    }
  }
  overlay.classList.remove("hidden");
}

function hideLinkFindDetail() {
  const overlay = document.getElementById("linkFindDetailOverlay");
  if (overlay) overlay.classList.add("hidden");
}

/* 对比对象选择 */
"use strict";

// ---- 对比对象选择 ----

// 当前下拉打开的 anchor；同一 anchor 再次点击时收起，点另一个则切换
let _ddAnchor = null;

function selectSnapshot(which, path) {
  if (which === "old") state.oldPath = path;
  else state.newPath = path;
  // 手动改基准/当前后离开占用浏览（需再点对比）
  if (state.treeMode === "browse") state.treeMode = "compare";
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
  else if (typeof resetSearchPreheatUi === "function") resetSearchPreheatUi();
}

function snapByPath(path) {
  return state.snapshots.find((s) => s.path === path);
}

function updatePickers() {
  const setPick = (role, path, pickId) => {
    const el = document.querySelector(`[data-role="${role}-value"]`);
    const pick = document.getElementById(pickId);
    if (!el) return;
    const inner = el.parentElement;
    let noteEl = inner && inner.querySelector(`[data-role="${role}-note"]`);
    if (inner && !noteEl) {
      noteEl = document.createElement("span");
      noteEl.className = "pick-note is-empty hidden";
      noteEl.setAttribute("data-role", `${role}-note`);
      inner.appendChild(noteEl);
    }
    const s = snapByPath(path);
    if (s) {
      pick && pick.classList.add("is-filled");
      el.innerHTML =
        `${fmtTime(s.scanned_at)} <span class="path">· ${escapeHtml(s.root)}</span>`;
      el.classList.remove("placeholder");
      // 已选中：始终显示备注行，无备注写「无备注」，左右框同高
      const noteText = (s.note || "").trim();
      if (noteEl) {
        if (noteText) {
          noteEl.textContent = noteText;
          noteEl.title = noteText;
          noteEl.classList.remove("is-empty");
        } else {
          noteEl.textContent = t("pickNoNote");
          noteEl.title = "";
          noteEl.classList.add("is-empty");
        }
        noteEl.classList.remove("hidden");
      }
    } else {
      pick && pick.classList.remove("is-filled");
      el.textContent = t("pickPlaceholder");
      el.classList.add("placeholder");
      // 未选中：隐藏备注行，占位文案与「基准/当前」垂直居中
      if (noteEl) {
        noteEl.textContent = "";
        noteEl.title = "";
        noteEl.classList.add("is-empty", "hidden");
      }
    }
  };
  setPick("old", state.oldPath, "pickOld");
  setPick("new", state.newPath, "pickNew");

  // 同一快照也可点「对比」：进入占用展开
  const ok = !!(state.oldPath && state.newPath);
  $("#compareBtn").disabled = !ok || state.comparing;
  $("#swapBtn").disabled = !(state.oldPath || state.newPath);
  $("#clearPickBtn").disabled = !(state.oldPath || state.newPath);
}

/** 打开快照选择下拉。同一 anchor 再次点击则收起，点另一个则切换内容。 */
function openDropdown(which, anchor, opts) {
  const o = opts || {};
  const dd = $("#dropdown");
  if (!dd.classList.contains("hidden") && _ddAnchor === anchor) {
    dd.classList.add("hidden");
    _ddAnchor = null;
    return;
  }
  _ddAnchor = anchor;
  dd.innerHTML = "";
  // 已选中项：对比条看两侧的 old/new，其它调用方用 opts.current 给路径
  const curOld = o.current !== undefined ? "" : state.oldPath;
  const curNew = o.current !== undefined ? o.current : state.newPath;
  if (state.snapshots.length === 0) {
    dd.innerHTML = `<div class="dd-empty">${t("noSnapshots")}</div>`;
  } else {
    for (const s of state.snapshots) {
      const item = document.createElement("div");
      item.className = "dd-item";
      if (curOld && s.path === curOld) {
        item.classList.add("is-current-old");
      }
      if (curNew && s.path === curNew) {
        item.classList.add("is-current-new");
      }
      const noteText = (s.note || "").trim();
      const noteHtml = noteText
        ? `<div class="n">${escapeHtml(noteText)}</div>`
        : "";
      const freeText = s.free_size > 0
        ? `${t("freeLabel")} ${fmtBytes(s.free_size)}`
        : `${t("freeLabel")} ${t("freeUnknown")}`;
      item.innerHTML = `<div class="t">${fmtTime(s.scanned_at)}（${fmtAgo(s.scanned_at)}）</div>
        <div class="m">${escapeHtml(s.root)} · <span class="dd-free">${freeText}</span> · ${fmtBytes(s.total_size)}</div>
        ${noteHtml}`;
      item.onclick = () => {
        if (o.onPick) o.onPick(s);
        else selectSnapshot(which, s.path);
        dd.classList.add("hidden");
      };
      dd.appendChild(item);
    }
  }
  const r = anchor.getBoundingClientRect();
  dd.style.minWidth = `${r.width}px`;
  // 先占位再量尺寸，量完才定位置，所以量的时候先藏起来免得闪一下
  dd.style.visibility = "hidden";
  dd.classList.remove("hidden");
  const gap = 4;
  const edge = 8;
  const dh = dd.offsetHeight;
  const dw = Math.max(r.width, dd.offsetWidth);
  let top = r.bottom + gap;
  if (top + dh > window.innerHeight - edge) {
    // 下方放不下就翻到上方；两边都放不下（窗口太矮）时贴住下边
    const above = r.top - dh - gap;
    top = above >= edge ? above : Math.max(edge, window.innerHeight - dh - edge);
  }
  let left = r.left;
  if (left + dw > window.innerWidth - edge) {
    left = Math.max(edge, window.innerWidth - dw - edge);
  }
  dd.style.left = `${left}px`;
  dd.style.top = `${top}px`;
  dd.style.visibility = "";
}

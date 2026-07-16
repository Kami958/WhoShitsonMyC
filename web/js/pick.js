/* 对比对象选择 */
"use strict";

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

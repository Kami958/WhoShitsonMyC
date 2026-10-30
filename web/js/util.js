/* 通用工具 */
"use strict";

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

/** 弹出提示。普通提示自动消失，错误提示保持显示，直到用户点击关闭按钮。 */
// ---- Toast 提示 ----
// 普通提示：2 秒起，超过 15 字每多 2 字加 1 秒，最多 20 秒。
// 警告提示：5 秒起，超过 15 字每多 2 字加 1 秒，最多 30 秒。
// 错误提示：常驻，只能手动关闭。
// 起始时长和上限可以在设置页「界面」页签调整，字数阈值和步长固定。
const TOAST_TIMING = {
  infoBase: 2000,
  infoMax: 20000,
  warnBase: 5000,
  warnMax: 30000,
  longText: 15,
  step: 2,
};

/** 把设置里的秒数写进提示计时配置，非法值回落到默认秒数。 */
function applyToastTiming(s) {
  if (!s) return;
  const sec = (value, fallback) => {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : fallback;
  };
  TOAST_TIMING.infoBase = sec(s.ui_toast_info_base, 2) * 1000;
  TOAST_TIMING.infoMax = sec(s.ui_toast_info_max, 20) * 1000;
  TOAST_TIMING.warnBase = sec(s.ui_toast_warn_base, 5) * 1000;
  TOAST_TIMING.warnMax = sec(s.ui_toast_warn_max, 30) * 1000;
  if (TOAST_TIMING.infoMax < TOAST_TIMING.infoBase) {
    TOAST_TIMING.infoMax = TOAST_TIMING.infoBase;
  }
  if (TOAST_TIMING.warnMax < TOAST_TIMING.warnBase) {
    TOAST_TIMING.warnMax = TOAST_TIMING.warnBase;
  }
}

function _toastKind(type) {
  if (type === true || type === "error") return "error";
  if (type === "warn" || type === "warning") return "warn";
  return "info";
}

function _toastDuration(text, kind) {
  const base = kind === "warn" ? TOAST_TIMING.warnBase : TOAST_TIMING.infoBase;
  const max = kind === "warn" ? TOAST_TIMING.warnMax : TOAST_TIMING.infoMax;
  const extra = Math.max(0, String(text || "").length - TOAST_TIMING.longText);
  const add = Math.floor(extra / TOAST_TIMING.step) * 1000;
  return Math.min(max, base + add);
}

function _syncToastRing(el) {
  const svg = el.querySelector(".toast-ring");
  const rect = svg && svg.querySelector("rect");
  if (!svg || !rect) return;
  const w = Math.max(1, el.clientWidth);
  const h = Math.max(1, el.clientHeight);
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  rect.setAttribute("x", "2");
  rect.setAttribute("y", "2");
  rect.setAttribute("width", String(Math.max(1, w - 4)));
  rect.setAttribute("height", String(Math.max(1, h - 4)));
  rect.setAttribute("rx", "3");
  rect.setAttribute("ry", "3");
  rect.setAttribute("pathLength", "100");
  rect.setAttribute("stroke-dasharray", "100");
}

function _dismissToast(el) {
  if (!el || el._toastLeaving) return;
  el._toastLeaving = true;
  if (el._toastRaf) cancelAnimationFrame(el._toastRaf);
  if (el._toastRO) el._toastRO.disconnect();
  el.classList.add("toast-leaving");
  setTimeout(() => {
    const slot = el.parentElement;
    if (slot && slot.classList.contains("toast-slot")) slot.remove();
    else el.remove();
    _syncToastClear();
  }, 180);
}

function _startToastTimer(el) {
  const duration = el._toastDuration;
  if (!duration) return;
  const ring = el.querySelector(".toast-ring rect");
  let remaining = duration;
  let last = performance.now();
  let paused = false;
  const tick = (now) => {
    if (!el.isConnected || el._toastLeaving) return;
    const delta = now - last;
    last = now;
    if (!paused) remaining = Math.max(0, remaining - delta);
    if (ring) {
      ring.style.strokeDashoffset = String(100 * (1 - remaining / duration));
    }
    if (remaining <= 0) {
      _dismissToast(el);
      return;
    }
    el._toastRaf = requestAnimationFrame(tick);
  };
  el._toastRaf = requestAnimationFrame(tick);
  el.addEventListener("mouseenter", () => {
    paused = true;
    last = performance.now();
  });
  el.addEventListener("mouseleave", () => {
    paused = false;
    last = performance.now();
  });
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => _syncToastRing(el));
    ro.observe(el);
    el._toastRO = ro;
  }
}

/** 清除当前所有提示。 */
function hideToast() {
  const stack = $("#toastStack");
  if (!stack) return;
  [...stack.querySelectorAll(".toast")].forEach(_dismissToast);
  _syncToastClear();
}

/** 同步“清除全部”按钮：提示超过 4 条时显示。 */
function _syncToastClear() {
  const stack = $("#toastStack");
  const btn = $("#toastClearBtn");
  if (!stack || !btn) return;
  btn.classList.toggle("hidden", stack.children.length <= 4);
}

/**
 * 显示一条提示。
 * 第二个参数可以是布尔值（兼容旧调用），也可以是 "info"、"warn"、"error"。
 */
function toast(msg, type = "info") {
  const text = String(msg == null ? "" : msg).trim();
  if (!text) return;
  const stack = $("#toastStack");
  if (!stack) return;
  const kind = _toastKind(type);
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.innerHTML = '<svg class="toast-ring" aria-hidden="true"><rect stroke-dasharray="100"></rect></svg>';
  const textEl = document.createElement("span");
  textEl.className = "toast-text";
  textEl.textContent = text;
  el.appendChild(textEl);
  if (kind === "error") {
    const closeBtn = document.createElement("button");
    closeBtn.className = "toast-close";
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", t("close"));
    closeBtn.title = t("close");
    closeBtn.textContent = "✕";
    closeBtn.addEventListener("click", () => _dismissToast(el));
    el.appendChild(closeBtn);
  }
  el._toastKind = kind;
  el._toastDuration = kind === "error" ? 0 : _toastDuration(text, kind);
  const slot = document.createElement("div");
  slot.className = "toast-slot";
  slot.appendChild(el);
  stack.appendChild(slot);
  slot._toastHeight = el.getBoundingClientRect().height;
  slot.addEventListener("mouseenter", () => {
    slot.style.minHeight = slot._toastHeight + "px";
  });
  slot.addEventListener("mouseleave", () => {
    slot.style.minHeight = "";
  });
  void el.offsetWidth;
  _syncToastRing(el);
  el.classList.add("toast-enter");
  if (kind !== "error") _startToastTimer(el);
  stack.scrollTop = stack.scrollHeight;
  _syncToastClear();
}

/** 显示一条警告提示。 */
function toastWarn(msg) {
  toast(msg, "warn");
}

(function bindToastClearButton() {
  if (typeof document === "undefined") return;
  const btn = document.getElementById("toastClearBtn");
  if (btn) btn.addEventListener("click", hideToast);
})();

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

// Windows 路径：「盘符:\段1\段2…」，允许反斜杠与正斜杠混用，末段可为文件名。
// 段名允许含空格（如 "Program Files"）和中文；路径以空白或全角标点收尾，
// 避免把紧随其后的中文正文（如「，然后删除链接…」）一并吞进同一个 chip。
const _WIN_PATH_RE =
  /[A-Za-z]:[\\/](?:[^\\/:|*?"<>]+(?: [^\\/:|*?"<>]+)*[\\/])*[^\\/:|*?"<>]+?(?=[\s，。；、：？！）】」』]|$)/g;

/** 把字符串里的 Windows 路径包成 <span class="path-chip">，先转义再切，避免 XSS。 */
function highlightPaths(s) {
  const escaped = escapeHtml(s);
  return escaped.replace(_WIN_PATH_RE, (m) =>
    `<span class="path-chip">${m}</span>`
  );
}

/** 应用内确认框（替代 window.confirm，避免弹出系统原生窗口）。 */
let _confirmResolver = null;
/** 本次确认框是否带勾选（决定 resolve 形态）。 */
let _confirmHasCheckbox = false;

function _resetConfirmCheckboxUi() {
  const row = $("#confirmCheckboxRow");
  const chk = $("#confirmCheckbox");
  const label = $("#confirmCheckboxLabel");
  const content = $("#confirmContent");
  const bodyEl = $("#confirmDialogBody");
  if (row) row.classList.add("hidden");
  if (chk) {
    chk.checked = false;
    chk.onchange = null;
  }
  if (label) label.textContent = "";
  if (bodyEl) bodyEl.textContent = "";
  if (content) content.classList.add("hidden");
  _confirmHasCheckbox = false;
}

function closeConfirmDialog(result) {
  const ov = $("#confirmOverlay");
  if (ov) ov.classList.add("hidden");
  const okBtn = $("#confirmOkBtn");
  if (okBtn) {
    okBtn.classList.remove("btn-danger");
    okBtn.classList.add("btn-primary");
  }
  const resolve = _confirmResolver;
  const hasCheckbox = _confirmHasCheckbox;
  const chk = $("#confirmCheckbox");
  const checked = !!(chk && chk.checked);
  _confirmResolver = null;
  _resetConfirmCheckboxUi();
  if (!resolve) return;
  if (!result) {
    resolve(false);
    return;
  }
  // 带勾选时返回对象，便于调用方读 checked；无勾选仍返回 true 保持兼容
  if (hasCheckbox) resolve({ checked });
  else resolve(true);
}

/**
 * @param {object} opts
 * @param {string} [opts.title]
 * @param {string} [opts.message] 有增量信息才传；空则不占正文区
 * @param {string} [opts.okText]
 * @param {boolean} [opts.danger]
 * @param {string} [opts.checkboxLabel] 有值时在底栏显示轻量勾选
 * @param {string} [opts.checkboxLabelChecked] 勾选后的标签文字，默认同 checkboxLabel
 * @param {string} [opts.messageChecked] 勾选后的正文，默认沿用 message
 * @param {boolean} [opts.checkboxDefault]
 * @param {string} [opts.okTextChecked] 勾选后确定按钮文案
 * @returns {Promise<boolean|{checked:boolean}>} 取消为 false；无勾选确定为 true；有勾选确定为 {checked}
 */
function showConfirmDialog(opts) {
  const options = opts || {};
  const ov = $("#confirmOverlay");
  const titleEl = $("#confirmDialogTitle");
  const contentEl = $("#confirmContent");
  const bodyEl = $("#confirmDialogBody");
  const okBtn = $("#confirmOkBtn");
  const row = $("#confirmCheckboxRow");
  const chk = $("#confirmCheckbox");
  const labelEl = $("#confirmCheckboxLabel");
  if (!ov || !titleEl || !okBtn) {
    return Promise.resolve(false);
  }
  if (_confirmResolver) closeConfirmDialog(false);

  titleEl.textContent = options.title || t("confirmTitle");

  const message = options.message == null ? "" : String(options.message);
  const hasMessage = !!message.trim();
  if (bodyEl) bodyEl.innerHTML = hasMessage ? highlightPaths(message) : "";
  if (contentEl) contentEl.classList.toggle("hidden", !hasMessage);
  // 勾选后正文可换一套说法（例：进回收站 / 彻底删除）
  const messageChecked = options.messageChecked == null ? "" : String(options.messageChecked);
  const hasMessageChecked = !!messageChecked.trim();

  const hasCheckbox = !!(options.checkboxLabel && row && chk && labelEl);
  _confirmHasCheckbox = hasCheckbox;
  const okText = options.okText || t("confirmOk");
  const okTextChecked = options.okTextChecked || okText;
  const checkboxLabelChecked = options.checkboxLabelChecked || options.checkboxLabel;

  function syncOkFromCheckbox() {
    const checked = !!(chk && chk.checked);
    okBtn.textContent = checked ? okTextChecked : okText;
    if (labelEl) labelEl.textContent = checked ? checkboxLabelChecked : options.checkboxLabel;
    if (hasMessageChecked && bodyEl) {
      const text = checked ? messageChecked : message;
      bodyEl.innerHTML = text.trim() ? highlightPaths(text) : "";
      if (contentEl) contentEl.classList.toggle("hidden", !text.trim());
    }
    const danger = !!options.danger || checked;
    okBtn.classList.toggle("btn-danger", danger);
    okBtn.classList.toggle("btn-primary", !danger);
  }

  if (hasCheckbox) {
    labelEl.textContent = options.checkboxLabel;
    chk.checked = !!options.checkboxDefault;
    chk.onchange = syncOkFromCheckbox;
    row.classList.remove("hidden");
    syncOkFromCheckbox();
  } else {
    if (row) row.classList.add("hidden");
    okBtn.textContent = okText;
    okBtn.classList.toggle("btn-danger", !!options.danger);
    okBtn.classList.toggle("btn-primary", !options.danger);
  }

  ov.classList.remove("hidden");

  return new Promise((resolve) => {
    _confirmResolver = resolve;
  });
}

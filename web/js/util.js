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

/** 短暂弹出一条 toast 提示。 */
let toastTimer = null;
function toast(msg, isErr = false, durationMs) {
  const el = $("#toast");
  const text = String(msg == null ? "" : msg).trim();
  if (!text) return;
  el.textContent = text;
  el.classList.toggle("err", isErr);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  const ms =
    durationMs != null
      ? durationMs
      : isErr || text.length > 48
        ? 5200
        : 2600;
  toastTimer = setTimeout(() => el.classList.add("hidden"), ms);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
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
  if (bodyEl) bodyEl.textContent = hasMessage ? message : "";
  if (contentEl) contentEl.classList.toggle("hidden", !hasMessage);

  const hasCheckbox = !!(options.checkboxLabel && row && chk && labelEl);
  _confirmHasCheckbox = hasCheckbox;
  const okText = options.okText || t("confirmOk");
  const okTextChecked = options.okTextChecked || okText;

  function syncOkFromCheckbox() {
    const checked = !!(chk && chk.checked);
    okBtn.textContent = checked ? okTextChecked : okText;
    // 彻底删除用危险色；默认回收站也用 danger（删除动作）
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

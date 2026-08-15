/* 主题与外链 */
"use strict";

// ---- 主题 ----

function syncTitlebarTheme() {
  // 原生标题栏归系统画。不 await：界面先可点，主题在后台跟上。
  // 传原始偏好（auto/dark/light）：后端存偏好、标题栏拿解析值；
  // 相同偏好会 short-circuit，启动连打也不贵。
  let pref = state._settings && state._settings.theme;
  if (pref !== "auto" && pref !== "dark" && pref !== "light") {
    pref = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  }
  try {
    if (state.api && state.api.set_theme) {
      state.api.set_theme(pref);
    }
  } catch (e) {}
}

/**
 * 更新主题按钮文案；可选同步原生标题栏 / 后端 store。
 * 启动阶段在读完 YAML 前不要 syncBackend，否则会把默认 dark 写进 settings.yaml。
 */
function applyThemeButton(syncBackend = true) {
  const dark = document.documentElement.dataset.theme === "dark";
  const btn = $("#themeToggle");
  if (btn) btn.textContent = dark ? t("themeDark") : t("themeLight");
  if (syncBackend) syncTitlebarTheme();
}

function toggleTheme() {
  const html = document.documentElement;
  const next = html.dataset.theme === "dark" ? "light" : "dark";
  // 主界面按钮切换 = 固定该主题（覆盖「跟随系统」）
  html.dataset.theme = next;
  if (state._settings) state._settings.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) {}
  applyThemeButton(true);
  // 后端 set_theme 会写入 store / YAML（若开启持久化）
}

/** 主题偏好解析：auto 读系统深浅色（仅调用时判断，不监听变化）。 */
function resolveThemeValue(theme) {
  const v = String(theme || "").trim().toLowerCase();
  if (v === "dark" || v === "light") return v;
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  } catch (e) {
    return "light";
  }
}

/**
 * 应用主题到页面（并缓存 localStorage，供下次首屏闪一下用）。
 * ``theme`` 可为 light/dark/auto；auto 由 resolveThemeValue 解析。
 * 权威来源是 settings.yaml；启动时由 reconcileLang 调用。
 */
function applyThemeValue(theme) {
  const html = document.documentElement;
  const v = resolveThemeValue(theme);
  html.dataset.theme = v;
  // 清掉启动脚本可能留下的内联底色，统一走 CSS 变量
  html.style.backgroundColor = "";
  html.style.color = "";
  if (document.body) {
    document.body.style.backgroundColor = "";
    document.body.style.color = "";
  }
  try { localStorage.setItem("theme", v); } catch (e) {}
}


/** 仅首屏占位：读 localStorage；真正主题以 get_settings 为准。 */
function restoreThemePreference() {
  try {
    const saved = localStorage.getItem("theme");
    if (saved === "dark" || saved === "light") {
      document.documentElement.dataset.theme = saved;
    }
  } catch (e) {}
}

/** 启动时再补一次即可；后端有延迟重绘，不必前端连打四次。 */
function scheduleTitlebarSync() {
  syncTitlebarTheme();
  setTimeout(() => { syncTitlebarTheme(); }, 200);
}

const GITHUB_URL = "https://github.com/Kami958/WhoShitsonMyC";

async function openGitHub() {
  try {
    const res = await state.api.open_url(GITHUB_URL);
    if (res && res.error) toast(res.error, true);
  } catch (e) {
    toast(String(e), true);
  }
}

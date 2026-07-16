/* 主题与外链 */
"use strict";

// ---- 主题 ----

function syncTitlebarTheme() {
  // 原生标题栏归系统画。不 await：界面先可点，主题在后台跟上。
  // 后端对相同主题会 short-circuit，启动连打也不贵。
  const dark = document.documentElement.dataset.theme === "dark";
  try {
    if (state.api && state.api.set_theme) {
      state.api.set_theme(dark ? "dark" : "light");
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
  html.dataset.theme = html.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("theme", html.dataset.theme); } catch (e) {}
  applyThemeButton(true);
  // 后端 set_theme 会写入 store / YAML（若开启持久化）
}

/**
 * 应用主题到页面（并缓存 localStorage，供下次首屏闪一下用）。
 * 权威来源是 settings.yaml；启动时由 reconcileLang 调用。
 */
function applyThemeValue(theme) {
  // 默认亮色；仅显式 dark 时用暗色
  const v = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = v;
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

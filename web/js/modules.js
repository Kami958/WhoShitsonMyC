/* 构建期可选模块：探测 + callModule 封装 */
"use strict";

/** 启动时探测可用模块，写入 state.modules。 */
async function loadModules() {
  state.modules = state.modules || {};
  if (!state.api || !state.api.list_modules) {
    state.modules = {};
    console.warn("[modules] list_modules API missing");
    applyModuleVisibility();
    return state.modules;
  }
  try {
    const res = await state.api.list_modules();
    state.modules = res && typeof res === "object" ? res : {};
    console.info("[modules] loaded", state.modules);
  } catch (e) {
    state.modules = {};
    console.warn("[modules] list_modules failed", e);
  }
  applyModuleVisibility();
  return state.modules;
}

/** 按模块存在性门控 ``[data-module]`` 元素。

 * 用 ``module-off`` 而不是 ``hidden``，避免与侧栏自身的开/关状态冲突
 * （AI 面板默认关闭，模块存在时也不应被强制显示）。
 */
function applyModuleVisibility() {
  const mods = state.modules || {};
  document.querySelectorAll("[data-module]").forEach((el) => {
    const name = el.getAttribute("data-module");
    const on = !!(name && mods[name]);
    el.classList.toggle("module-off", !on);
  });
}

function hasModule(name) {
  return !!(state.modules && state.modules[name]);
}

/**
 * 调用可选模块方法。
 * @param {string} mod
 * @param {string} method
 * @param {object} [kwargs]
 * @returns {Promise<object>}
 */
async function callModule(mod, method, kwargs) {
  if (!state.api || !state.api.module_invoke) {
    return { error: t("aiModuleMissing") };
  }
  if (!hasModule(mod)) {
    return { error: t("aiModuleMissing") };
  }
  try {
    const res = await state.api.module_invoke(mod, method, kwargs || {});
    return res && typeof res === "object" ? res : { ok: true, result: res };
  } catch (e) {
    return { error: String(e) };
  }
}

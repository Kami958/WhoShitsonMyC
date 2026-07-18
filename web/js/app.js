/* 事件绑定与启动 */
"use strict";

function wireEvents() {
  $("#newScanBtn").onclick = newScan;
  $("#compareBtn").onclick = doCompare;
  $("#swapBtn").onclick = swapPick;
  $("#clearPickBtn").onclick = clearPick;
  $("#cancelScanBtn").onclick = () => state.api.cancel_scan();
  $("#themeToggle").onclick = toggleTheme;
  $("#langToggle").onclick = () => setLang(LANG === "zh" ? "en" : "zh");
  $("#githubBtn").onclick = openGitHub;
  $("#openDirBtn").onclick = openSnapshotDir;
  $("#refreshSnapsBtn").onclick = refreshSnapshots;
  $("#importSnapsBtn").onclick = importSnapshots;
  const newFolderBtn = $("#newFolderBtn");
  if (newFolderBtn) newFolderBtn.onclick = () => openCreateFolderDialog();
  $("#settingsBtn").onclick = openSettings;
  const noteSave = $("#noteSaveBtn");
  if (noteSave) {
    noteSave.onclick = () => {
      // 立刻禁用，防止连点；真正写盘在 saveNoteDialog 里异步
      noteSave.disabled = true;
      const input = $("#noteInput");
      if (input) input.disabled = true;
      saveNoteDialog(input ? input.value : "");
    };
  }
  const noteCancel = $("#noteCancelBtn");
  if (noteCancel) noteCancel.onclick = closeNoteDialog;
  const noteClose = $("#noteCloseBtn");
  if (noteClose) noteClose.onclick = closeNoteDialog;
  const noteOverlay = $("#noteOverlay");
  if (noteOverlay) {
    noteOverlay.onclick = (e) => {
      if (e.target === noteOverlay) closeNoteDialog();
    };
  }
  const noteInput = $("#noteInput");
  if (noteInput) {
    noteInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        saveNoteDialog(noteInput.value);
      }
    });
  }
  const folderSave = $("#folderSaveBtn");
  if (folderSave) {
    folderSave.onclick = () => submitFolderDialog();
  }
  const folderCancel = $("#folderCancelBtn");
  if (folderCancel) folderCancel.onclick = closeFolderDialog;
  const folderClose = $("#folderCloseBtn");
  if (folderClose) folderClose.onclick = closeFolderDialog;
  const folderOverlay = $("#folderOverlay");
  if (folderOverlay) {
    folderOverlay.onclick = (e) => {
      if (e.target === folderOverlay) closeFolderDialog();
    };
  }
  const folderNameInput = $("#folderNameInput");
  if (folderNameInput) {
    folderNameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitFolderDialog();
      }
    });
  }
  $("#settingsCloseBtn").onclick = closeSettings;
  // 「完成」才统一提交；✕ / Esc 关闭丢弃草稿（点遮罩不关闭）
  $("#settingsDoneBtn").onclick = () => applySettingsAndClose();
  document.querySelectorAll(".settings-tab").forEach((btn) => {
    btn.onclick = () => switchSettingsTab(btn.getAttribute("data-settings-tab"));
  });
  // 设置说明问号：点击展开/收起（悬停说明靠 title）
  document.querySelectorAll(".settings-q").forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleSettingsHelp(btn);
    };
  });
  // 控件只改草稿，不写后端
  const workerSel = $("#workerSel");
  if (workerSel) {
    workerSel.onchange = (e) => {
      if (_settingsDraft) _settingsDraft.scan_workers = Number(e.target.value) || 1;
    };
  }
  const compressChk = $("#compressChk");
  if (compressChk) {
    compressChk.onchange = (e) => {
      if (_settingsDraft) _settingsDraft.compress_snapshots = !!e.target.checked;
    };
  }
  const mftChk = $("#mftChk");
  if (mftChk) {
    mftChk.onchange = (e) => {
      if (_settingsDraft) _settingsDraft.use_mft = !!e.target.checked;
    };
  }
  const snapDirChoose = $("#snapDirChooseBtn");
  if (snapDirChoose) snapDirChoose.onclick = chooseSnapDir;
  const snapDirReset = $("#snapDirResetBtn");
  if (snapDirReset) snapDirReset.onclick = resetSnapDirDraft;
  const snapDirOpen = $("#snapDirOpenBtn");
  if (snapDirOpen) snapDirOpen.onclick = openSnapshotDir;
  const resetSettingsBtn = $("#resetSettingsBtn");
  if (resetSettingsBtn) resetSettingsBtn.onclick = () => resetSettingsToDefaults();
  const logRefresh = $("#logRefreshBtn");
  if (logRefresh) logRefresh.onclick = () => refreshLogView();
  const logClear = $("#logClearBtn");
  if (logClear) logClear.onclick = () => clearAppLog();
  const logExport = $("#logExportBtn");
  if (logExport) logExport.onclick = () => exportAppLog();
  const checkUpdateBtn = $("#checkUpdateBtn");
  if (checkUpdateBtn) checkUpdateBtn.onclick = () => checkForUpdates();
  const openReleaseBtn = $("#openReleaseBtn");
  if (openReleaseBtn) openReleaseBtn.onclick = () => openReleasePage();
  const blacklistAddBtn = $("#blacklistAddBtn");
  if (blacklistAddBtn) blacklistAddBtn.onclick = () => addBlacklistFromForm();
  const blacklistRemoveBtn = $("#blacklistRemoveBtn");
  if (blacklistRemoveBtn) blacklistRemoveBtn.onclick = () => removeBlacklistSelected();
  const blacklistPickBtn = $("#blacklistPickBtn");
  if (blacklistPickBtn) blacklistPickBtn.onclick = () => pickBlacklistFolder();
  const uninstallBtn = $("#uninstallBtn");
  if (uninstallBtn) uninstallBtn.onclick = () => openUninstallDialog();
  const uninstallCancel = $("#uninstallCancelBtn");
  if (uninstallCancel) uninstallCancel.onclick = () => closeUninstallDialog();
  const uninstallClose = $("#uninstallDialogCloseBtn");
  if (uninstallClose) uninstallClose.onclick = () => closeUninstallDialog();
  const uninstallConfirm = $("#uninstallConfirmBtn");
  if (uninstallConfirm) uninstallConfirm.onclick = () => confirmUninstallCleanup();
  const uninstallDoneOk = $("#uninstallDoneOkBtn");
  if (uninstallDoneOk) uninstallDoneOk.onclick = () => finishUninstallAndQuit();
  const importOk = $("#importOkBtn");
  if (importOk) importOk.onclick = closeImportResultDialog;
  const importClose = $("#importCloseBtn");
  if (importClose) importClose.onclick = closeImportResultDialog;
  const importOverlay = $("#importOverlay");
  if (importOverlay) {
    importOverlay.onclick = (e) => {
      if (e.target === importOverlay) closeImportResultDialog();
    };
  }
  const confirmOk = $("#confirmOkBtn");
  if (confirmOk) confirmOk.onclick = () => closeConfirmDialog(true);
  const confirmCancel = $("#confirmCancelBtn");
  if (confirmCancel) confirmCancel.onclick = () => closeConfirmDialog(false);
  const confirmClose = $("#confirmCloseBtn");
  if (confirmClose) confirmClose.onclick = () => closeConfirmDialog(false);
  const confirmOverlay = $("#confirmOverlay");
  if (confirmOverlay) {
    confirmOverlay.onclick = (e) => {
      if (e.target === confirmOverlay) closeConfirmDialog(false);
    };
  }

  $("#pickOld").onclick = (e) => openDropdown("old", e.currentTarget);
  $("#pickNew").onclick = (e) => openDropdown("new", e.currentTarget);

  const sortMenuBtn = $("#sortMenuBtn");
  if (sortMenuBtn) {
    sortMenuBtn.onclick = (e) => {
      e.stopPropagation();
      openSummaryMenu("sort", e.currentTarget);
    };
  }
  const filterMenuBtn = $("#filterMenuBtn");
  if (filterMenuBtn) {
    filterMenuBtn.onclick = (e) => {
      e.stopPropagation();
      openSummaryMenu("filter", e.currentTarget);
    };
  }
  const collapseAllBtn = $("#collapseAllBtn");
  if (collapseAllBtn) {
    collapseAllBtn.onclick = (e) => {
      e.stopPropagation();
      collapseAllTree();
    };
  }
  const snapSortMenuBtn = $("#snapSortMenuBtn");
  if (snapSortMenuBtn) {
    snapSortMenuBtn.onclick = (e) => {
      e.stopPropagation();
      openSummaryMenu("snapSort", e.currentTarget);
    };
  }

  const treeSearchToggle = $("#treeSearchToggle");
  if (treeSearchToggle) {
    treeSearchToggle.onclick = (e) => {
      e.stopPropagation();
      const wrap = $("#treeSearchWrap");
      if (wrap && wrap.classList.contains("is-open")) {
        const input = $("#treeSearchInput");
        if (input) input.focus();
      } else {
        openTreeSearch({ focus: true });
      }
    };
  }
  const treeSearchInput = $("#treeSearchInput");
  if (treeSearchInput) {
    treeSearchInput.addEventListener("input", () => onTreeSearchInput());
    treeSearchInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        runTreeSearch(treeSearchInput.value);
        syncTreeSearchChrome();
      } else if (e.key === "Escape") {
        e.preventDefault();
        // 有内容先清空结果与输入；已空则收起
        if (String(treeSearchInput.value || "").trim() || _searchQuery) {
          clearTreeSearch();
          treeSearchInput.focus();
        } else {
          collapseTreeSearch();
        }
      }
    });
  }
  const treeSearchClear = $("#treeSearchClear");
  if (treeSearchClear) {
    treeSearchClear.onclick = (e) => {
      e.stopPropagation();
      clearTreeSearch();
      openTreeSearch({ focus: true });
    };
  }
  const searchMoreBtn = $("#searchMoreBtn");
  if (searchMoreBtn) {
    searchMoreBtn.onclick = () => loadMoreTreeSearch();
  }
  const searchCloseBtn = $("#searchCloseBtn");
  if (searchCloseBtn) {
    searchCloseBtn.onclick = (e) => {
      e.stopPropagation();
      // 关闭结果面板并收起搜索框
      collapseTreeSearch({ clear: true });
    };
  }
  const searchSortBtn = $("#searchSortBtn");
  if (searchSortBtn) {
    searchSortBtn.onclick = (e) => {
      e.stopPropagation();
      openSummaryMenu("searchSort", e.currentTarget);
    };
  }
  const searchCaseChk = $("#searchCaseChk");
  if (searchCaseChk) {
    searchCaseChk.onchange = () => setSearchOption("case", searchCaseChk.checked);
  }
  const searchExactChk = $("#searchExactChk");
  if (searchExactChk) {
    searchExactChk.onchange = () => setSearchOption("exact", searchExactChk.checked);
  }

  for (const item of document.querySelectorAll(".ctx-item")) {
    item.onclick = () => ctxCommand(item.dataset.cmd);
  }

  // 点击空白关闭下拉与右键菜单
  document.addEventListener("click", (e) => {
    const dd = $("#dropdown");
    if (!dd.classList.contains("hidden") &&
        !dd.contains(e.target) &&
        !e.target.closest(".pick")) {
      dd.classList.add("hidden");
    }
    if (!e.target.closest(".ctx-menu")) closeCtxMenu();
    if (
      !e.target.closest(".summary-icon-btn") &&
      !e.target.closest(".search-tool-btn") &&
      !e.target.closest(".icon-menu")
    ) {
      closeSummaryMenus();
    }
    // 搜索框展开且为空、无结果时，点外部收起
    const wrap = $("#treeSearchWrap");
    if (
      wrap &&
      wrap.classList.contains("is-open") &&
      !wrap.contains(e.target) &&
      !e.target.closest("#searchPanel")
    ) {
      const input = $("#treeSearchInput");
      const empty = !input || !String(input.value || "").trim();
      if (empty && !_searchQuery) collapseTreeSearch();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeCtxMenu();
      closeSummaryMenus();
      const co = $("#confirmOverlay");
      if (co && !co.classList.contains("hidden")) {
        closeConfirmDialog(false);
        return;
      }
      const io = $("#importOverlay");
      if (io && !io.classList.contains("hidden")) {
        closeImportResultDialog();
        return;
      }
      const fo = $("#folderOverlay");
      if (fo && !fo.classList.contains("hidden")) {
        closeFolderDialog();
        return;
      }
      const no = $("#noteOverlay");
      if (no && !no.classList.contains("hidden")) {
        closeNoteDialog();
        return;
      }
      const so = $("#settingsOverlay");
      if (so && !so.classList.contains("hidden")) closeSettings();
    }
  });

  // 主题按钮文案占位；不调 set_theme，等 reconcileLang 读完 YAML 再同步
  applyThemeButton(false);
  setFilter(state.filter);
  syncSummaryToolButtons();
  wireSidebarResizer();
}

// ---- 左侧栏宽度拖拽（仅改布局，向右变宽挤压对比树，不改窗口） ----

const _SIDEBAR_W_MIN = 180;
const _SIDEBAR_W_MAX = 520;
const _SIDEBAR_W_DEFAULT = 250;
const _SIDEBAR_W_KEY = "wsmc.sidebarWidth";

function applySidebarWidth(px) {
  const side = $("#sidebar");
  if (!side) return _SIDEBAR_W_DEFAULT;
  let w = Math.round(Number(px));
  if (!Number.isFinite(w)) w = _SIDEBAR_W_DEFAULT;
  w = Math.max(_SIDEBAR_W_MIN, Math.min(_SIDEBAR_W_MAX, w));
  // 用 CSS 变量一次写入，避免频繁读布局
  document.documentElement.style.setProperty("--sidebar-w", w + "px");
  side.style.width = w + "px";
  return w;
}

function restoreSidebarWidth() {
  try {
    const raw = localStorage.getItem(_SIDEBAR_W_KEY);
    if (raw != null && raw !== "") {
      applySidebarWidth(raw);
      return;
    }
  } catch (e) {}
  applySidebarWidth(_SIDEBAR_W_DEFAULT);
}

function wireSidebarResizer() {
  const handle = $("#sidebarResizer");
  const side = $("#sidebar");
  if (!handle || !side) return;

  restoreSidebarWidth();

  let dragging = false;
  let startX = 0;
  let startW = 0;
  let raf = 0;
  let pendingW = 0;

  const flush = () => {
    raf = 0;
    if (!dragging) return;
    applySidebarWidth(pendingW);
  };

  const onMove = (e) => {
    if (!dragging) return;
    pendingW = startW + (e.clientX - startX);
    if (!raf) raf = requestAnimationFrame(flush);
  };

  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("is-sidebar-resizing");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
    const w = applySidebarWidth(pendingW || startW);
    try {
      localStorage.setItem(_SIDEBAR_W_KEY, String(w));
    } catch (e) {}
  };

  handle.addEventListener("pointerdown", (e) => {
    if (e.button != null && e.button !== 0) return;
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    // 用已应用宽度，避免 getBoundingClientRect 触发额外布局抖动
    const applied = parseInt(
      getComputedStyle(document.documentElement).getPropertyValue("--sidebar-w"),
      10
    );
    startW =
      (Number.isFinite(applied) && applied > 0
        ? applied
        : side.offsetWidth) || _SIDEBAR_W_DEFAULT;
    pendingW = startW;
    document.body.classList.add("is-sidebar-resizing");
    try {
      handle.setPointerCapture(e.pointerId);
    } catch (err) {}
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  });
}

// ---- 启动 ----

async function boot() {
  state.api = window.pywebview.api;
  // 首屏仅用 localStorage 占位；权威主题/语言以 settings.yaml（get_settings）为准
  restoreThemePreference();
  applyThemeButton(false);
  wireEvents();
  if (typeof wirePendingUi === "function") wirePendingUi();
  if (typeof wireAiUi === "function") wireAiUi();
  await reconcileLang(); // 内部会 applyThemeButton + scheduleTitlebarSync
  // 先探测可选模块，再加载设置（AI 设置分节依赖 list_modules）
  if (typeof loadModules === "function") await loadModules();
  if (typeof refreshToolTabsVisibility === "function") refreshToolTabsVisibility();
  // 同步 AI 启用状态与左侧入口（不弹隐私窗）
  if (typeof loadAiSettingsDraft === "function") {
    try {
      await loadAiSettingsDraft();
    } catch (e) {}
  }
  loadSnapshots();
  await loadSettings();
  // 非管理员：启动 toast 推荐提权（MFT / 系统路径完整性）
  if (state._settings && state._settings.is_admin === false) {
    toast(t("recommendAdminToast"));
  }
}

// 首屏即按浏览器语言渲染静态文案（同步、无闪烁）；boot 后再与后端校对。
// 主题先用 localStorage 占位，避免白闪；最终以 YAML 覆盖（勿在 boot 前 set_theme）。
LANG = detectLangSync() || "en";
restoreThemePreference();
applyStaticI18n();
applyThemeButton(false);

// pywebview 注入 API 后触发 pywebviewready；若已就绪则直接启动。
if (window.pywebview && window.pywebview.api) {
  boot();
} else {
  window.addEventListener("pywebviewready", boot);
}

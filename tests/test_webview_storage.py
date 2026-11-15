"""浏览器缓存目录：放在系统临时文件夹，退出时清理，启动时清扫残留。"""

from __future__ import annotations

import os

from app.webview2 import (
    _webview_storage_path,
    clear_webview_storage,
    sweep_stale_webview_storage,
    webview_storage_dir,
)


def _use_temp(tmp_path, monkeypatch):
    temp_root = tmp_path / "temp"
    monkeypatch.setenv("TEMP", str(temp_root))
    monkeypatch.setenv("TMP", str(temp_root))
    return temp_root


def test_storage_path_under_temp(tmp_path, monkeypatch):
    temp_root = _use_temp(tmp_path, monkeypatch)
    path = _webview_storage_path()
    assert path == os.path.join(str(temp_root), "wsmc-webview2")
    assert os.path.isdir(path)


def test_storage_dir_matches_storage_path(tmp_path, monkeypatch):
    temp_root = _use_temp(tmp_path, monkeypatch)
    assert webview_storage_dir() == os.path.join(str(temp_root), "wsmc-webview2")


def test_clear_webview_storage_removes_dir(tmp_path, monkeypatch):
    temp_root = _use_temp(tmp_path, monkeypatch)
    target = temp_root / "wsmc-webview2"
    os.makedirs(target / "EBWebView")
    (target / "EBWebView" / "Cache").write_text("x", encoding="utf-8")
    assert clear_webview_storage() is True
    assert not target.exists()


def test_clear_webview_storage_missing_ok(tmp_path, monkeypatch):
    _use_temp(tmp_path, monkeypatch)
    assert clear_webview_storage() is True


def test_sweep_removes_temp_and_legacy(tmp_path, monkeypatch):
    temp_root = _use_temp(tmp_path, monkeypatch)
    app_dir = tmp_path / "app"
    os.makedirs(temp_root / "wsmc-webview2" / "EBWebView")
    os.makedirs(app_dir / "webview" / "EBWebView")
    monkeypatch.setattr("app.webview2.store.app_data_dir", lambda: str(app_dir))

    assert sweep_stale_webview_storage() == 2
    assert not (temp_root / "wsmc-webview2").exists()
    assert not (app_dir / "webview").exists()


def test_sweep_keeps_foreign_webview_dir(tmp_path, monkeypatch):
    _use_temp(tmp_path, monkeypatch)
    app_dir = tmp_path / "app"
    os.makedirs(app_dir / "webview")
    (app_dir / "webview" / "mine.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr("app.webview2.store.app_data_dir", lambda: str(app_dir))

    assert sweep_stale_webview_storage() == 0
    assert (app_dir / "webview" / "mine.txt").is_file()

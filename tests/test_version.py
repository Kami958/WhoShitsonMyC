"""版本号解析与比较。"""

from __future__ import annotations

from version import (
    compare_versions,
    is_remote_newer,
    normalize_version,
    parse_version,
)


def test_normalize_version():
    assert normalize_version("1.1.0") == "1.1.0"
    assert normalize_version("v1.1.0") == "1.1.0"
    assert normalize_version("V2.0") == "2.0"
    assert normalize_version("  v1.2.3  ") == "1.2.3"
    assert normalize_version(None) == ""
    assert normalize_version("") == ""


def test_parse_version():
    assert parse_version("1.1.0") == (1, 1, 0)
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("1.2") == (1, 2)
    assert parse_version("1.2.3-beta") == (1, 2, 3)
    assert parse_version("10.0.0+build") == (10, 0, 0)
    assert parse_version("") == ()
    assert parse_version("nope") == ()


def test_is_remote_newer():
    assert is_remote_newer("1.2.0", "1.1.0") is True
    assert is_remote_newer("1.1.0", "1.1.0") is False
    assert is_remote_newer("1.0.9", "1.1.0") is False
    assert is_remote_newer("v2.0.0", "1.9.9") is True
    # 长度不同补 0
    assert is_remote_newer("1.1", "1.1.0") is False
    assert is_remote_newer("1.1.1", "1.1") is True
    assert is_remote_newer("", "1.0") is False
    assert is_remote_newer("1.0", "") is True


def test_compare_versions_three_way():
    # 发布更高 → update
    assert compare_versions("1.2.0", "1.1.0") == 1
    # 相同
    assert compare_versions("1.1.0", "1.1.0") == 0
    assert compare_versions("v1.1.0", "1.1.0") == 0
    assert compare_versions("1.1", "1.1.0") == 0
    # 本机更高（开发中 1.1.0 vs 已发 1.0.2）
    assert compare_versions("1.0.2", "1.1.0") == -1
    assert compare_versions("1.0.9", "1.1.0") == -1
    # 边界
    assert compare_versions("", "1.0") == 0
    assert compare_versions("1.0", "") == 1

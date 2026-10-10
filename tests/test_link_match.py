"""链接判定规则的单元测试：候选过滤、目标归一化、目标比较。"""

import os

from core.link_match import (
    abs_under_root,
    link_candidate,
    link_hit,
    link_norm,
    target_matches,
)
from core.models import REPARSE_TAG_MOUNT_POINT, REPARSE_TAG_SYMLINK


def test_abs_under_root_drive_root():
    """盘根拼接不多出分隔符，归一化后与磁盘上的写法相同。"""
    assert abs_under_root("C:\\", "Users") == "C:\\Users"
    assert abs_under_root("C:", "Users") == "C:\\Users"
    assert abs_under_root("C:\\", "") == "C:"
    assert link_norm(abs_under_root("C:\\", "Users\\666")) == link_norm(
        "C:\\Users\\666"
    )
    assert link_norm("C:\\\\Users") == link_norm("C:\\Users")
    assert link_norm("") == ""


def test_candidate_skips_tags_without_target():
    """只认目录联接与符号链接，且目标要读得出来、不是卷 GUID。"""
    assert link_candidate(0, "C:\\real") is None
    assert link_candidate(0x8000001A, "C:\\real") is None  # OneDrive 占位
    assert link_candidate(REPARSE_TAG_MOUNT_POINT, "") is None
    assert link_candidate(REPARSE_TAG_MOUNT_POINT, "volume{abc}\\") is None
    assert link_candidate(REPARSE_TAG_MOUNT_POINT, "C:\\real") == (
        "junction",
        link_norm("C:\\real"),
    )
    assert link_candidate(REPARSE_TAG_SYMLINK, "C:\\real")[0] == "symlink"


def test_relative_target_expands_from_link_folder():
    """相对目标是相对链接所在目录算的，展开后才比得上。"""
    target = link_norm("C:\\real")
    assert target_matches("C:\\sub\\link", "..\\real", target) is True
    assert target_matches("C:\\sub\\link", "..\\other", target) is False
    # 绝对目标用不上链接自己的位置
    assert target_matches("", link_norm("C:\\real"), target) is True


def test_link_hit_reports_absolute_path():
    """扫盘途中命中的一行给出绝对路径，与读库那条通道出的一样。"""
    hit = link_hit(
        "C:\\",
        link_norm("C:\\real"),
        "link",
        REPARSE_TAG_MOUNT_POINT,
        "C:\\real",
    )
    assert hit == {
        "path": os.path.join("C:\\", "link"),
        "kind": "junction",
        "target": "C:\\real",
    }
    # 目标读不出来的行不算命中
    assert (
        link_hit("C:\\", link_norm("C:\\real"), "link", REPARSE_TAG_MOUNT_POINT, "")
        is None
    )
    # 指向别处的行不算命中
    assert (
        link_hit(
            "C:\\",
            link_norm("C:\\real"),
            "link",
            REPARSE_TAG_MOUNT_POINT,
            "C:\\other",
        )
        is None
    )

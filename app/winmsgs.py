"""窗口相关的错误提示文案：删除失败与迁移失败的机器键转人话。"""

from __future__ import annotations

from core import i18n


def _delete_error_message(code: str, *, root: str = "", rel_path: str = "") -> str:
    """把 fs_delete.DeleteError 机器码映射为中英文案。"""
    c = (code or "").strip()
    if c == "root":
        return i18n.t("无法删除扫描根目录", "Cannot delete the scan root")
    if c == "drive_root":
        return i18n.t("无法删除磁盘根目录", "Cannot delete a drive root")
    if c == "outside":
        return i18n.t("路径不在对比范围内", "The path is outside the compare root")
    if c == "blacklist":
        return i18n.t("该路径在删除白名单中", "This path is on the delete whitelist")
    if c == "missing":
        return i18n.t("路径已不存在", "The path no longer exists")
    if c == "recycle_unsupported":
        return i18n.t(
            "当前系统不支持移到回收站",
            "Recycle Bin is not supported on this system",
        )
    if c == "invalid":
        return i18n.t("无效路径", "Invalid path")
    if c.startswith("recycle:"):
        return i18n.t(
            "移到回收站失败，未执行彻底删除",
            "Failed to move to Recycle Bin; permanent delete was not performed",
        )
    if c.startswith("os:"):
        detail = c[3:] or c
        return i18n.t(f"删除失败：{detail}", f"Delete failed: {detail}")
    return i18n.t(f"删除失败：{c}", f"Delete failed: {c}")


def _migrate_error_message(code: str) -> str:
    """把迁移机器码映射为中英文案。"""
    c = (code or "").strip()
    if c == "cancelled":
        return i18n.t("迁移已取消", "Migration cancelled")
    if c == "invalid_source":
        return i18n.t("访问路径不存在", "The access path does not exist")
    if c == "not_dir":
        return i18n.t("访问路径不是目录", "The access path is not a folder")
    if c == "source_is_link":
        return i18n.t("访问路径已是链接，不能迁移", "The access path is already a link and cannot be migrated")
    if c == "drive_root":
        return i18n.t("无法迁移磁盘根目录", "Cannot migrate a drive root")
    if c == "invalid_dest":
        return i18n.t("存放位置无效", "Invalid storage location")
    if c == "same_path":
        return i18n.t("存放位置与访问路径相同", "The storage location equals the access path")
    if c == "dest_exists":
        return i18n.t("存放位置已存在", "The storage location already exists")
    if c == "dest_parent_missing":
        return i18n.t("存放位置所在文件夹不存在", "The storage location's parent folder does not exist")
    if c == "inside_each":
        return i18n.t("访问路径与存放位置不能互相包含", "The access path and the storage location cannot contain each other")
    if c == "fs_mismatch":
        return i18n.t(
            "访问路径与存放位置的文件系统类型不同，不能迁移",
            "The access path and the storage location are on different file systems and cannot be migrated",
        )
    if c == "not_windows":
        return i18n.t("目录链接仅支持 Windows", "Directory links are Windows-only")
    if c == "not_enough_space":
        return i18n.t("存放位置所在磁盘剩余空间不足", "Not enough free space on the storage drive")
    if c == "unknown_link":
        return i18n.t("找不到这条链接记录", "Unknown link record")
    if c == "not_a_link":
        return i18n.t("该路径不是目录链接，不能操作", "This path is not a directory link, so this action is unavailable")
    if c == "import_missing":
        return i18n.t("访问路径不存在", "The access path does not exist")
    if c == "import_not_junction":
        return i18n.t(
            "访问路径不是目录链接，无法导入",
            "The access path is not a directory link and cannot be imported",
        )
    if c == "import_target_missing":
        return i18n.t("它指向的存放位置不存在", "Its storage location does not exist")
    if c == "import_target_is_link":
        return i18n.t("它指向的存放位置本身也是链接", "Its storage location is itself a link")
    if c == "import_exists":
        return i18n.t("这条链接已经在管理里", "This link is already managed")
    if c == "import_reversed":
        return i18n.t("访问路径与存放位置填反了，请交换后重试", "The access path and the storage location are swapped. Swap them and try again")
    if c == "import_pair_mismatch":
        return i18n.t(
            "存放位置与访问路径的指向不一致",
            "The storage location does not match what this link points to",
        )
    if c == "import_dir_missing":
        return i18n.t("要扫描的目录不存在", "The folder to scan does not exist")
    if c.startswith("scan:"):
        return i18n.t(f"扫目录失败：{c[5:]}", f"Failed to scan the folder: {c[5:]}")
    if c == "target_missing":
        return i18n.t("链接指向的存放位置不存在", "The storage location this link points to does not exist")
    if c == "no_backup":
        return i18n.t("该链接没有备份目录", "No backup for this link")
    if c == "no_old_target":
        return i18n.t("该链接没有待处理的旧迁移", "No old migration left for this link")
    if c == "old_target_pending":
        return i18n.t(
            "上次迁移留下的目录尚未删除，请先处理",
            "Delete the folder left by the last migration first",
        )
    if c.startswith("linklost:"):
        return i18n.t(
            f"访问路径的目录链接没能建回来：{c[9:]}，数据在存放位置里完好",
            f"Directory link at {c[9:]} was not restored; the data is safe in the storage location",
        )
    if c.startswith("unlink:"):
        return i18n.t(f"删除链接失败：{c[7:]}", f"Failed to remove link: {c[7:]}")
    if c.startswith("restore:"):
        return i18n.t(f"还原失败：{c[8:]}", f"Restore failed: {c[8:]}")
    if c.startswith("copy:"):
        return i18n.t(f"复制失败：{c[5:]}", f"Copy failed: {c[5:]}")
    if c.startswith("rename:"):
        return i18n.t(f"腾出访问路径失败：{c[7:]}", f"Failed to clear the access path: {c[7:]}")
    if c.startswith("junction:"):
        return i18n.t(f"创建目录链接失败：{c[9:]}", f"Failed to create the directory link: {c[9:]}")
    if c.startswith("unreadable:"):
        return i18n.t(
            f"读不到目录：{c[11:]}，迁移已中止",
            f"Cannot read folder: {c[11:]}; migration stopped",
        )
    if c == "busy":
        return i18n.t(
            "另一个迁移正在进行，请稍后重试",
            "Another migration is in progress. Try again later",
        )
    if c == "aborted":
        return i18n.t("迁移已取消", "Migration cancelled")
    if c.startswith("blocked:"):
        raw = c[8:]
        return i18n.t(
            f"这些程序正在使用该目录：{raw.replace(',', '、')}。请关闭后重试。",
            f"These programs are using this folder: {raw.replace(',', ', ')}. Close them and try again",
        )
    if c.startswith("verify:"):
        return i18n.t("复制后校验不一致，已撤销迁移", "Verification mismatch; migration undone")
    return i18n.t(f"目录迁移失败：{c}", f"Directory migration failed: {c}")

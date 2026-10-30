"""窗口相关的错误提示文案：删除失败与迁移失败的机器键转人话。"""

from __future__ import annotations

from core import i18n


def _storage_reason_text(reason: str) -> str:
    """把创建目录失败的机器键翻成中性短语，由调用方接成完整句子。"""
    key = str(reason or "").strip().lower()
    if key == "permission":
        return i18n.t(
            "当前运行账户没有写入该位置的权限",
            "the current account does not have write permission for that location",
        )
    if key == "already_file":
        return i18n.t(
            "该位置上已经有一个同名文件",
            "a file with the same name already exists at that location",
        )
    if key == "not_dir":
        return i18n.t(
            "路径中间有一段不是文件夹",
            "part of that path is not a folder",
        )
    if key == "missing_parent":
        return i18n.t(
            "这个文件夹的上一层不存在",
            "the parent folder of that location does not exist",
        )
    if key == "no_space":
        return i18n.t(
            "该位置所在磁盘空间不足",
            "there is not enough free space on the drive for that location",
        )
    if key == "no_base":
        return i18n.t(
            "系统没有提供可用的数据文件夹位置",
            "the system did not provide a usable data folder location",
        )
    if key == "inside_source":
        return i18n.t(
            "新的位置位于现有数据文件夹内部",
            "that location is inside the current data folder",
        )
    if key == "identity_unavailable":
        return i18n.t(
            "无法确定当前运行程序的 Windows 账户",
            "the Windows account running the app could not be determined",
        )
    return i18n.t(
        "系统拒绝访问该位置",
        "the system refused access to that location",
    )


def _snapshot_dir_error_message(reason: str = "") -> str:
    """启动检测发现快照存放目录不可用时返回给界面的完整提示。"""
    detail = _storage_reason_text(reason)
    return i18n.t(
        f"快照存放目录当前不可用，{detail}；请选择一个当前运行账户可以写入的文件夹。",
        f"The snapshot storage folder is currently unavailable because {detail}; choose another folder the current account can write to.",
    )


def _snapshot_dir_choice_error_message(reason: str = "") -> str:
    """用户选择或保存快照存放目录失败时返回给界面的完整提示。"""
    detail = _storage_reason_text(reason)
    return i18n.t(
        f"这个文件夹无法使用，{detail}；请另选一个当前运行账户可以写入的文件夹。",
        f"This folder cannot be used because {detail}; choose another folder the current account can write to.",
    )


def _snapshot_dir_reset_error_message(reason: str = "") -> str:
    """恢复默认快照存放目录失败时返回给界面的完整提示。"""
    detail = _storage_reason_text(reason)
    return i18n.t(
        f"无法恢复默认快照存放目录，{detail}；请改选一个当前运行账户可以写入的文件夹。",
        f"The default snapshot folder cannot be restored because {detail}; choose a folder the current account can write to instead.",
    )


def _data_dir_choice_error_message(reason: str = "") -> str:
    """用户选择的数据文件夹不能使用时返回给界面的完整提示。"""
    if reason == "identity_unavailable":
        return i18n.t(
            "无法确定当前运行程序的 Windows 账户，这个数据文件夹未被采用；请重启程序后重试，或联系管理员确认账户信息可以读取。",
            "The Windows account running the app could not be determined, so this data folder was not used; restart the app and try again, or ask an administrator to make the account information readable.",
        )
    detail = _storage_reason_text(reason)
    return i18n.t(
        f"这个文件夹不能作为数据文件夹使用，{detail}；请另选一个当前运行账户可以写入的文件夹。",
        f"This folder cannot be used for app data because {detail}; choose another folder the current account can write to.",
    )


def _data_dir_unavailable_message(path: str = "", reason: str = "") -> str:
    """启动时数据目录不能按当前运行账户使用时的完整提示。"""
    detail = _storage_reason_text(reason)
    if path:
        return i18n.t(
            f"程序的数据文件夹不可用，路径是 {path}，原因是{detail}；请选择一个当前运行账户可以写入的文件夹，或修复该位置的访问权限后重新启动程序。",
            f"The app data folder is unavailable at {path} because {detail}; choose a folder the current account can write to, or fix access to that location and restart the app.",
        )
    if reason == "identity_unavailable":
        return i18n.t(
            "程序无法确定当前运行程序的 Windows 账户，因此找不到可用的数据文件夹；请重启程序后重试，或联系管理员确认账户信息可以读取。",
            "The app could not determine the Windows account running it and therefore has no usable data folder; restart the app and try again, or ask an administrator to make the account information readable.",
        )
    return i18n.t(
        f"程序无法确定可用的数据文件夹，{detail}；请重启程序后重试，或联系管理员确认账户信息可以读取。",
        f"The app could not determine a usable data folder because {detail}; restart the app and try again, or ask an administrator to make the account information readable.",
    )


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
    if c == "busy" or c.startswith("busy:"):
        name = c[5:].strip() if c.startswith("busy:") else ""
        if name:
            return i18n.t(
                f"文件或文件夹 {name} 正被其他程序占用，关闭对应程序后重试。",
                f"The file or folder {name} is in use by another program; close it and try again.",
            )
        return i18n.t(
            "文件或文件夹正被其他程序占用，关闭对应程序后重试。",
            "A file or folder is in use by another program; close it and try again.",
        )
    if c == "denied" or c.startswith("denied:"):
        name = c[7:].strip() if c.startswith("denied:") else ""
        if name:
            return i18n.t(
                f"没有权限删除 {name}，请检查文件权限后重试。",
                f"No permission to delete {name}; check the file permissions and try again.",
            )
        return i18n.t(
            "没有权限删除该位置，请检查文件权限后重试。",
            "No permission to delete this location; check the file permissions and try again.",
        )
    if c == "writeprotect" or c.startswith("writeprotect:"):
        return i18n.t(
            "磁盘处于写保护状态，无法删除内容。解除写保护后重试。",
            "The disk is write-protected, so nothing could be deleted. Remove the write protection and try again.",
        )
    if c == "recycle_unsupported":
        return i18n.t(
            "当前系统不支持移到回收站",
            "Recycle Bin is not supported on this system",
        )
    if c == "recycle_unavailable":
        return i18n.t(
            "该位置无法使用回收站，删除没有执行。需要彻底删除时，请勾选彻底删除后重试。",
            "The Recycle Bin cannot be used for this location, so nothing was deleted. To delete it permanently, select permanent delete and try again.",
        )
    if c == "invalid":
        return i18n.t("无效路径", "Invalid path")
    if c.startswith("recycle:"):
        return i18n.t(
            "移到回收站失败，没有执行彻底删除；请确认该位置可以使用回收站后重试。",
            "The item could not be moved to the Recycle Bin and was not permanently deleted; check that the Recycle Bin is available for that location and try again.",
        )
    return i18n.t(
        "删除没有完成；请确认路径仍然存在并且可以删除后重试。",
        "The delete did not complete; check that the path still exists and can be deleted, then try again.",
    )


def _settings_recovered_message() -> str:
    """设置文件读取失败并已备份时给用户的说明。"""
    return i18n.t(
        "设置读取失败，本次启动使用默认设置；原文件已备份在数据文件夹，文件名带时间戳。",
        "The settings could not be read, so this run uses default settings; the original was backed up in the data folder with a timestamp in its name.",
    )


def _link_registry_recovered_message() -> str:
    """链接登记文件读取失败并已备份时给用户的说明。"""
    return i18n.t(
        "迁移列表读取失败，列表从空开始；原文件已备份在数据文件夹，可以在目录迁移页用导入迁移把现有的目录链接加回来。",
        "The migration list could not be read, so it starts empty; the original was backed up in the data folder, and existing directory links can be added back with Import migration on the directory migration page.",
    )


def _history_recovered_message() -> str:
    """迁移历史读取失败并已备份时给用户的说明。"""
    return i18n.t(
        "历史记录读取失败，之前的历史暂时看不到；原文件已备份在数据文件夹。",
        "The history could not be read, so earlier entries are not visible for now; the original was backed up in the data folder.",
    )


def _snapshot_dir_not_saved_message(path: str = "") -> str:
    """快照已搬走但设置没写进去时的完整提示。"""
    if path:
        return i18n.t(
            f"快照已经迁移到 {path}，但设置没有保存。请在设置页把快照存放目录改成该文件夹。",
            f"The snapshots were moved to {path}, but the settings were not saved. Set the snapshot folder to that folder in Settings.",
        )
    return i18n.t(
        "快照已经迁移到新目录，但设置没有保存。请在设置页把快照存放目录改成该文件夹。",
        "The snapshots were moved to the new folder, but the settings were not saved. Set the snapshot folder to that folder in Settings.",
    )


def _storage_next_step(reason: str) -> str:
    """按失败原因给出下一步操作，避免让用户在不具备条件时反复重试。"""
    key = str(reason or "").strip().lower()
    if key == "permission":
        return i18n.t(
            "请在文件夹属性中授予该账户写入权限，然后重新保存设置。",
            "Grant the account write permission in the folder properties, then save the settings again.",
        )
    if key == "already_file":
        return i18n.t(
            "请移除该位置上的同名文件，然后重新保存设置。",
            "Remove the file with the same name from that location, then save the settings again.",
        )
    if key == "not_dir":
        return i18n.t(
            "请修正路径中不是文件夹的部分，然后重新保存设置。",
            "Correct the part of the path that is not a folder, then save the settings again.",
        )
    if key == "missing_parent":
        return i18n.t(
            "请先创建该位置的上层文件夹，然后重新保存设置。",
            "Create the parent folder of that location first, then save the settings again.",
        )
    if key == "no_space":
        return i18n.t(
            "请清理该磁盘上的空间，然后重新保存设置。",
            "Free up space on that drive, then save the settings again.",
        )
    if key == "no_base":
        return i18n.t(
            "请检查当前账户的主文件夹是否可用，然后重新保存设置。",
            "Check that the current account's home folder is usable, then save the settings again.",
        )
    return i18n.t(
        "请检查数据文件夹是否可以写入，然后重新保存设置。",
        "Check that the data folder is writable, then save the settings again.",
    )


def _settings_write_failed_message(reason: str = "") -> str:
    """设置未能写入时的完整提示。"""
    key = str(reason or "").strip().lower()
    if key == "permission":
        detail = i18n.t(
            "当前运行账户没有写入数据文件夹的权限",
            "the current account does not have write permission for the data folder",
        )
    else:
        detail = _storage_reason_text(reason)
    next_step = _storage_next_step(reason)
    return i18n.t(
        f"设置没有保存，原因是{detail}。{next_step}",
        f"The settings were not saved because {detail}. {next_step}",
    )


def _link_registry_write_failed_message() -> str:
    """迁移完成后链接登记写入失败时的完整提示。"""
    return i18n.t(
        "迁移已经完成，但这次迁移没有加入迁移列表；请在目录迁移页用导入迁移把这条目录链接加回来，否则迁移列表里看不到它，也无法从这里还原或再次迁移。",
        "The migration completed, but this migration was not added to the migration list; use Import migration on the directory migration page to add this directory link back, otherwise it is missing from the list and cannot be restored or migrated again from here.",
    )


def _import_link_failed_message() -> str:
    """导入已有目录链接失败时的完整提示。"""
    return i18n.t(
        "这条目录链接没有加入迁移列表；请重新选择该链接后再试一次，仍然失败时请确认数据文件夹可以写入。",
        "This directory link was not added to the migration list; select the link again and retry, and if it still fails, check that the data folder is writable.",
    )


def _history_write_failed_message() -> str:
    """迁移历史写入失败时的完整提示。"""
    return i18n.t(
        "迁移历史没有保存，本次操作不会出现在历史记录里；请确认数据文件夹可以写入后重试。",
        "The migration history was not saved, so this operation will not appear in the history; check that the data folder is writable and try again.",
    )


def _history_read_failed_message() -> str:
    """迁移历史无法读取时的完整提示。"""
    return i18n.t(
        "历史记录读取失败；请确认数据文件夹可以访问后重试。",
        "The history could not be read; check that the data folder is accessible and try again.",
    )


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
    if c == "registry_write_failed":
        return i18n.t(
            "迁移已经完成，但这次迁移没有加入迁移列表；请在目录迁移页用导入迁移把这条目录链接加回来，否则迁移列表里看不到它，也无法从这里还原或再次迁移。",
            "The migration completed, but this migration was not added to the migration list; use Import migration on the directory migration page to add this directory link back, otherwise it is missing from the list and cannot be restored or migrated again from here.",
        )
    if c == "history_write_failed":
        return i18n.t(
            "迁移历史没有保存，本次操作不会出现在历史记录里；请确认数据文件夹可以写入后重试。",
            "The migration history was not saved, so this operation will not appear in the history; check that the data folder is writable and try again.",
        )
    if c == "old_target_pending":
        return i18n.t(
            "上次迁移留下的目录尚未删除，请先处理",
            "Delete the folder left by the last migration first",
        )
    if c.startswith("linklost:"):
        return i18n.t(
            f"访问路径的目录链接未能重建：{c[9:]}；数据仍完好保存在存放位置，可以重新迁移一次来恢复链接。",
            f"The directory link at {c[9:]} could not be rebuilt; the data is still intact in the storage location, and another migration can restore the link.",
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

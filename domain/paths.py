"""领域路径层：插件目录、数据目录、派生路径与跨模块文件锁。"""

import os
import threading

from .constants import PLUGIN_DIR


_FILE_WRITE_LOCK = threading.Lock()  # 写入方可能在下载工作线程里，故用线程锁而非 asyncio.Lock

def _resolve_plugin_data_dir() -> str:
    """解析 AstrBot 的 plugin-data 目录（data/plugin_data/<插件目录名>/）。
    优先走官方 StarTools.get_data_dir()；兼容旧版 AstrBot 时回退到 astrbot_path 推导；
    极端情况下回退到插件目录下的 plugin_data/。"""
    plugin_dir_name = os.path.basename(PLUGIN_DIR)
    try:
        from astrbot.core.star.star_tools import StarTools
        return str(StarTools.get_data_dir(plugin_dir_name))
    except Exception:
        pass  # 旧版 AstrBot 没有 StarTools，继续走下面的兼容路径
    try:
        import astrbot.core.utils.astrbot_path as _ap
        d = os.path.join(_ap.get_astrbot_plugin_data_path(), plugin_dir_name)
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        pass  # 连 astrbot_path 也不可用（非标准部署），最后回退到插件目录
    d = os.path.join(PLUGIN_DIR, "plugin_data")
    os.makedirs(d, exist_ok=True)
    return d

PLUGIN_DATA_DIR = _resolve_plugin_data_dir()

_MANIFEST_PATH = os.path.join(PLUGIN_DATA_DIR, "cleanup_manifest.json")

_MANIFEST_LOCK = threading.Lock()

_NOTICE_PATH = os.path.join(PLUGIN_DATA_DIR, "pending_notices.json")

_NOTICE_LOCK = threading.Lock()

_NOTICE_MAX_PER_USER = 3      # 每人最多留几条，避免机器人长期离线时无限堆积

_NOTICE_MAX_CHARS = 300       # 单条最长字符数

_GITHUB_PROXY_FILE = os.path.join(PLUGIN_DATA_DIR, "github_proxy.txt")

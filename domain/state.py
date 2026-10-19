# -*- coding: utf-8 -*-
"""运行时可变状态。

这些值会在运行期被重新赋值。若用 `from x import 变量` 取用，拿到的是 import
那一刻的快照，跨模块重新赋值不可见；散落的 `global` 声明也有同样的坑（只对
定义它的模块生效）。因此集中放一处，所有读写一律走 `state.xxx`。

不可变的配置常量与文案不在此处，见 `domain/constants.py`。
"""

from .constants import DEFAULT_USER_DIR_MODE


class _RuntimeState:
    """插件进程内的运行时状态单例（模块底部实例化）。"""

    # 数据目录：__init__ 按配置改写，未初始化前为 None
    download_dir: str | None = None
    cloud_save_dir: str | None = None
    user_dir_mode: str = DEFAULT_USER_DIR_MODE  # 用户隔离子目录命名：hash=哈希（隐私）/ raw=原始 ID（便于核查）

    # 下载调度
    download_semaphore: object | None = None
    active_download_slots: int = 0
    download_timeout_seconds: int = -1

    # BaiduPCS-Go 登录态与自动下载失败退避
    bpcs_inited: bool = False
    auto_dl_failed: bool = False
    auto_dl_failed_at: float = 0.0
    auto_dl_last_error: str = ""

    # GitHub release 下载
    github_proxy: str = ""
    auto_mirror: str = ""
    last_release_error: str = ""

    # 维护任务
    cleanup_thread_started: bool = False
    local_cleanup_hour: int = 3

    # 网盘侧
    auto_delete_cloud: bool = True


state = _RuntimeState()

"""核心层：下载并发槽位与重复下载守卫。"""

import asyncio
import threading
from collections.abc import Callable

from astrbot.api import logger

from ..domain.constants import _MAX_CONCURRENT_DOWNLOADS
from ..domain.state import state


def _get_download_semaphore() -> asyncio.Semaphore:
    """懒创建下载并发信号量（首次在事件循环内调用时创建）。"""
    if state.download_semaphore is None:
        state.download_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)
    return state.download_semaphore

def _release_download_slot(sem: asyncio.Semaphore) -> None:
    """归还一个下载通道（只能在事件循环线程调用），与 _begin_download 的计数自增对称。"""
    state.active_download_slots -= 1
    sem.release()

class _DuplicateDownloadError(Exception):
    """同一去重键已有下载在途（同一用户重复提交同一个文件）。"""

class _DownloadGuard:
    """一次下载占用的并发资源：用户串行锁 + 全局下载通道 + 去重登记，由 worker 真正跑完后释放。

    释放刻意不放在调用方的 finally 里：调用方（命令处理器 / LLM 工具）随时可能被取消，
    而 Task.cancel() 会连带取消它正在 await 的那个 asyncio 任务，线程里的 BaiduPCS-Go
    却不会因此停下。所以下载走自建线程，并在该线程的 finally 里释放（经
    call_soon_threadsafe 回到事件循环执行）；只有「还没把工作交给 worker」时才允许
    调用方直接归还（abandon）。"""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._handed_to_worker = False
        self._outcome: tuple[bool, object] | None = None

    def outcome(self) -> tuple[bool, object] | None:
        """worker 的最终结果 `(是否成功, 结果或异常)`；还没跑完时为 None。

        结果不能只留在 Future 上：调用方被取消时 `Task.cancel()` 会连带取消它正在 await 的
        future，结果就再没人拿得到（命令被框架中断正是这种情况，下载线程却还在写盘）。"""
        return self._outcome

    def start(self, fn: Callable, /, *args: object, **kwargs: object) -> asyncio.Future:
        """在独立线程里执行阻塞下载，返回可 await 的 Future（结果/异常原样送回）。

        不用 asyncio.to_thread：它返回的 asyncio 任务会被调用方的取消连带取消，
        「任务已结束」于是不再等于「线程已结束」，资源会在下载仍在写盘时被释放。"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def _swallow(f: asyncio.Future) -> None:
            # 调用方已被取消、不会再 await 时取一次异常，避免 "exception was never retrieved" 噪声
            if not f.cancelled():
                f.exception()

        future.add_done_callback(_swallow)
        release = self._release

        def _deliver(setter: Callable, value: object) -> None:
            # 调用方可能已被取消（future 已 cancelled）：结果丢弃，但资源照常释放
            if not future.cancelled():
                setter(value)

        def _release_safely() -> None:
            try:
                release()
            except Exception as exc:
                logger.error(f"[baidu_netdisk] ❌ 释放下载资源失败: {exc}")

        def _runner() -> None:
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # 线程里任何异常都要送回事件循环，避免线程静默失败
                self._outcome = (False, exc)
                try:
                    loop.call_soon_threadsafe(_deliver, future.set_exception, exc)
                except RuntimeError:
                    logger.error(f"[baidu_netdisk] ❌ 下载线程回传异常失败（事件循环已关闭）: {exc}")
            else:
                self._outcome = (True, result)
                try:
                    loop.call_soon_threadsafe(_deliver, future.set_result, result)
                except RuntimeError:
                    pass  # 事件循环已关闭，结果无人接收
            finally:
                try:
                    loop.call_soon_threadsafe(_release_safely)
                except RuntimeError:
                    # 事件循环已关闭：只能在 worker 线程里就地归还。Semaphore/Lock 都不是线程安全的，
                    # 这里之所以还能这么做，是因为循环已死、不可能再有等待者被唤醒
                    _release_safely()

        # 起线程成功后才置位：若 start() 抛错（线程/内存耗尽）则 worker 从未存在，
        # 调用方仍可 abandon() 归还资源，否则锁与下载通道会永久泄漏
        threading.Thread(target=_runner, name="baidu-netdisk-download", daemon=True).start()
        self._handed_to_worker = True
        return future

    def abandon(self) -> None:
        """worker 尚未启动时立即归还资源（转存失败、参数非法等中途退出）。"""
        if not self._handed_to_worker:
            self._release()

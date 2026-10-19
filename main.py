"""
AstrBot Plugin: Baidu Netdisk Share Downloader
Download files from Baidu Netdisk share links using BaiduPCS-Go,
then send via AstrBot's platform adapters.
"""

import asyncio
import json as _json
import os
import queue
import re
import requests as _req
import shutil
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Coroutine

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .core.concurrency import _DownloadGuard, _DuplicateDownloadError, _get_download_semaphore, _release_download_slot
from .core.download import _config_bool, _config_int, download_from_cloud, _get_local_file_size, _next_progress, _zip_files
from .core.panapi import list_share_content, _list_share_tree_api, _share_switch_lock, _transfer_via_api
from .domain.constants import _BPCS_EXE_NAME, _BROWSER_UA, DEFAULT_CLOUD_SAVE_DIR, FLASH_TASK_LIMIT_MB, _INTERRUPT_POLL_SECONDS, _MAX_CONCURRENT_DOWNLOADS, _MSG_BAD_LINK, _MSG_BLACKLISTED, _MSG_BUSY, _MSG_LINK_SWITCHED, _MSG_NEED_LIST, _SEL_NUM_RE, _SEND_FILES_THRESHOLD_DEFAULT, _USER_LOCK_MAX_ENTRIES, _ZIP_MAX_MB_DEFAULT
from .domain.paths import _GITHUB_PROXY_FILE, PLUGIN_DATA_DIR
from .domain.state import state
from .utils.bpcs import _init_bpcs, login_bduss_bpcs, login_bpcs, login_cookies_bpcs, logout_bpcs, _run_bpcs
from .utils.fs import _notice_queue, _notice_take, _schedule_local_cleanup, _user_bucket
from .utils.github import _auto_download_bpcs, _digest_note, _download_github_exe, _ensure_bpcs_exe, _set_github_proxy
from .utils.link import parse_share_link
from .utils.size import _format_size

try:
    from astrbot.api.event import MessageChain
except ImportError:  # 兼容旧版 AstrBot
    from astrbot.core.message.message_event_result import MessageChain


@register(
    "astrbot_plugin_baidu_netdisk",
    "XTsat",
    "百度网盘分享文件自动下载发送",
    "2.1.0",
    "https://github.com/XTsat/astrbot_plugin_baidu_netdisk",
)
class BaiduNetdiskPlugin(Star):

    _SHARE_CACHE_MAX_USERS = 50  # 分享缓存最多保留多少个用户，避免长期运行后无限增长

    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self.config = config or {}

        custom_dir = str(self.config.get("download_dir", "")).strip()
        if custom_dir:
            state.download_dir = os.path.abspath(custom_dir)
        else:
            state.download_dir = os.path.join(PLUGIN_DATA_DIR, "downloads")
        try:
            os.makedirs(state.download_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"[baidu_netdisk] ⚠️ 配置的 download_dir ({state.download_dir}) 不可用 ({e})，回退到默认路径")
            state.download_dir = os.path.join(PLUGIN_DATA_DIR, "downloads")
            os.makedirs(state.download_dir, exist_ok=True)
        logger.info(f"[baidu_netdisk] download dir: {state.download_dir}")

        cloud_dir = str(self.config.get("cloud_save_dir", "")).strip()
        if cloud_dir:
            state.cloud_save_dir = cloud_dir.rstrip("/")
        else:
            state.cloud_save_dir = DEFAULT_CLOUD_SAVE_DIR
        logger.info(f"[baidu_netdisk] cloud save dir: {state.cloud_save_dir}")

        # 分享缓存：按用户分桶，谁看谁的链接互不覆盖（多人查看不同链接时不会串台）
        self._share_cache = {}
        # 下载去重锁：dl_key -> 发起用户，同时用于判断「该用户是否有下载进行中」
        self._active_downloads = {}
        # 用户级串行锁：同一用户的两个下载任务不能同时跑（共用同一个转存目录，
        # 并发时会把对方的文件算进本次结果、也会互相踩转存目录）
        self._user_locks = {}
        # 后台任务引用：LLM 工具把耗时的转存/下载 detach 出去执行（工具超时只有 120 秒），
        # 必须持有引用，否则任务可能在跑的过程中被 GC 回收
        self._background_tasks: set[asyncio.Task] = set()
        # 已受理但还没被事件循环调度的任务键：同一轮里重复的工具调用要能立刻被拦住
        self._pending_jobs: set[str] = set()
        # 已受理（工具返回「已开始」）但下载还没登记的窗口内，占住这些用户不能被切换链接：
        # 否则缓存被换掉，后台任务只能回「链接已切换」，用户先看到「已开始」再看到失败
        self._switch_claims: set[str] = set()

        # 设置 BaiduPCS-Go 下载目录（exe 可用时才设置）
        if _ensure_bpcs_exe():
            _run_bpcs(["config", "set", "-savedir", state.download_dir], timeout=15)
        else:
            logger.warning(f"[baidu_netdisk] ⚠️ {_BPCS_EXE_NAME} 不可用，首次登录时会自动下载，配置将在下载后生效")

        # 解析黑名单
        bl = str(self.config.get("blacklist", "")).strip()
        self._blacklist = set()
        if bl:
            for uid in re.split(r"[,，\s]+", bl):
                uid = uid.strip()
                if uid:
                    self._blacklist.add(uid)
        if self._blacklist:
            logger.info(f"[baidu_netdisk] blacklist: {self._blacklist}")

        # 读取下载后自动删除网盘文件开关
        state.auto_delete_cloud = _config_bool(self.config, "auto_delete_cloud", True)
        logger.info(f"[baidu_netdisk] auto_delete_cloud: {state.auto_delete_cloud}")

        # 进度显示配置
        self._progress_enabled = _config_bool(self.config, "progress_enabled", False)
        self._progress_interval = _config_int(self.config, "progress_interval", 30, minimum=1)
        if self._progress_enabled:
            logger.info(f"[baidu_netdisk] progress reporting enabled, interval={self._progress_interval}s")

        # 下载超时（秒），-1 = 不超时
        state.download_timeout_seconds = _config_int(self.config, "timeout_seconds", -1, minimum=-1)
        logger.info(f"[baidu_netdisk] download timeout: {state.download_timeout_seconds}s")

        # 读取每天定时清理本地文件的小时数（-1 = 关闭，0-23 = 触发小时）
        cleanup_hour = _config_int(self.config, "local_cleanup_hour", 3, minimum=-1, maximum=23)
        state.local_cleanup_hour = cleanup_hour
        _schedule_local_cleanup(cleanup_hour)

        threading.Thread(target=_init_bpcs, daemon=True).start()

        # 可选：配置了 BDUSS 则启动时自动登录
        bduss_val = str(self.config.get("bduuss", "")).strip()
        if bduss_val:
            threading.Thread(target=self._auto_login_bduss, args=(bduss_val,), daemon=True).start()

    def _auto_login_bduss(self, bduss: str) -> None:
        time.sleep(2)  # 等待 _init_bpcs 先跑完
        result = login_bduss_bpcs(bduss, "", self._allow_insecure_tls())
        if "error" in result:
            logger.warning(f"[baidu_netdisk] ⚠️ bduuss 配置自动登录失败: {result['error']}")
        else:
            logger.info("[baidu_netdisk] bduuss 配置自动登录成功")

    def _cloud_dir_for(self, user_id: str) -> str:
        """该用户的网盘转存目录：<cloud_save_dir>/<用户哈希>。
        按用户隔离后，下载完成的自动删除只会删掉自己的转存内容。"""
        base = (state.cloud_save_dir or DEFAULT_CLOUD_SAVE_DIR).rstrip("/")
        return f"{base}/{_user_bucket(user_id)}"

    def _cache_get(self, user_id: str) -> dict:
        """读取该用户的分享缓存（不写入，避免只读操作撑大缓存）。"""
        return self._share_cache.get(user_id) or {"surl": "", "pwd": "", "tree": "", "items": []}

    def _cache_put(self, user_id: str, surl: str, pwd: str, tree: str, items: list) -> None:
        """写入该用户的分享缓存；缓存按使用者数量封顶，超出的淘汰最早写入的那个。"""
        self._share_cache.pop(user_id, None)  # 先删再插，保持字典顺序=最近使用
        self._share_cache[user_id] = {"surl": surl, "pwd": pwd, "tree": tree, "items": items}
        while len(self._share_cache) > self._SHARE_CACHE_MAX_USERS:
            self._share_cache.pop(next(iter(self._share_cache)), None)

    @staticmethod
    def _resolve_tree_selection(cached: dict, raw: str) -> tuple[str, str]:
        """把「目录树序号」或「完整路径」解析为该分享中的路径，返回 (路径, 错误文案)。

        序号来自目录树每项前面的数字（items 的 num 字段）：/pan look 重发的是同一份
        缓存文本，因此序号在缓存被刷新前保持不变。纯数字也可能是目录/文件名（如 2025），
        序号没命中时按路径继续解析——不命中序号不代表一定是路径，所以这里要两种都试。"""
        sel = (raw or "").strip().strip("/")
        if not sel:
            return "", "缺少序号或路径（可先 /pan <链接> 查看目录）"
        m = _SEL_NUM_RE.match(sel)
        if not m:
            return sel, ""
        num = int(m.group(1))
        items = cached.get("items") or []
        for it in items:
            if it.get("num") == num:
                return str(it.get("path") or ""), ""
        if any(it.get("path") == sel for it in items):
            return sel, ""
        return "", f"目录中没有序号 {num}，请先 /pan look 查看目录后用序号下载"

    def _user_has_active_download(self, user_id: str) -> bool:
        """该用户是否有下载在途（含已受理但尚未登记下载的窗口，见 _switch_claims）。"""
        return user_id in self._active_downloads.values() or user_id in self._switch_claims

    def _user_download_lock(self, user_id: str) -> asyncio.Lock:
        """该用户的下载串行锁：同一用户的两个下载任务排队执行。

        转存目录与本地 bucket 都按用户划分，同一用户的两个任务并发跑会把彼此下载的
        文件算进本次结果、也会争抢同一个转存目录。字典做空闲清理，避免按用户数无限增长。"""
        lock = self._user_locks.get(user_id)
        if lock is None:
            if len(self._user_locks) >= _USER_LOCK_MAX_ENTRIES:
                for key in [k for k, v in self._user_locks.items() if not v.locked() and k != user_id]:
                    del self._user_locks[key]
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
        return lock

    # ---- LLM 工具受理 ----
    def _resolve_tool_link(self, event: AstrMessageEvent, link: str, pwd: str,
                           require_link: bool = False) -> tuple[str, str, str, str, dict]:
        """LLM 工具共用的链接受理：解析链接 → 取缓存 → 判能否切换链接。

        返回 `(surl, pwd, uid, 错误文案, 缓存快照)`；错误文案非空时调用方直接返回它。

        **不含黑名单判定**：各工具的黑名单门与各自的参数校验有固定先后（如
        pan_download_dir 要先拒绝黑名单用户、再报缺参数），抽进来会打乱这个顺序。

        require_link=True（pan_list）不接受缓存回退，必须显式给出链接。
        link 为空时把受理当刻的缓存快照带出去，后台任务按它比对而不是重读缓存：
        受理与执行之间用户可能已查看别的链接，重读会按新分享里的同名文件转存下载。"""
        surl = ""
        if link:
            surl, pwd = self._normalize_link(link, pwd)
            if not surl:
                return "", pwd, "", _MSG_BAD_LINK, {}
        uid = event.get_sender_id()
        cached = self._cache_get(uid)
        if require_link:
            # 目录树查询不接受缓存回退：没给链接就是用户漏传参数
            if not surl:
                return "", pwd, uid, _MSG_BAD_LINK, cached
        else:
            if not surl and not cached["surl"]:
                return "", pwd, uid, _MSG_NEED_LIST, cached
            if not surl:
                pwd = cached["pwd"]
        if surl:
            # 纯字典查询、零阻塞：留在工具内同步判一次，否则用户会先看到「已受理」、
            # 紧接着又收到「当前有下载任务进行中」（检查只在后台任务里做就会这样自相矛盾）
            blocked = self._check_switch_allowed(surl, uid)
            if blocked:
                return "", pwd, uid, blocked, cached
        return surl, pwd, uid, "", cached

    async def _begin_download(self, uid: str, dl_key: str) -> tuple[_DownloadGuard, bool]:
        """占住该用户的串行锁与全局下载通道，并登记去重键；返回 (守卫, 本次是否排队)。

        锁与通道都要活到 worker 真正结束，所以释放交给 _DownloadGuard（见其类注释）；
        同一去重键已有在途任务时抛 _DuplicateDownloadError。"""
        if dl_key in self._active_downloads:
            raise _DuplicateDownloadError()
        self._active_downloads[dl_key] = uid
        lock = self._user_download_lock(uid)
        sem = _get_download_semaphore()
        queued = state.active_download_slots >= _MAX_CONCURRENT_DOWNLOADS
        state.active_download_slots += 1
        got_lock = got_sem = False
        try:
            await lock.acquire()
            got_lock = True
            await sem.acquire()
            got_sem = True
        except BaseException:
            # 排队期间被取消：worker 还没启动，这里必须立刻归还，否则计数与通道永久泄漏
            self._active_downloads.pop(dl_key, None)
            state.active_download_slots -= 1
            if got_sem:
                sem.release()
            if got_lock:
                lock.release()
            raise
        released = False

        def _release() -> None:
            nonlocal released
            if released:
                return  # 幂等：abandon 与 worker 结束回调都可能在取消路径上被触发
            released = True
            self._active_downloads.pop(dl_key, None)
            _release_download_slot(sem)
            lock.release()

        return _DownloadGuard(_release), queued

    def _transfer_and_list(self, surl: str, pwd: str, user_id: str) -> dict:
        """线程安全的"切换分享链接"：API 直连列出分享目录树并更新该用户的缓存。
        列目录不转存、也不删暂存目录（下载改为按需精准转存单个文件/文件夹，
        下载完成后由 auto_delete_cloud 清理），多用户查看不同链接互不影响。"""
        with _share_switch_lock:
            result = list_share_content(surl, pwd)
            if "error" not in result:
                self._cache_put(user_id, surl, pwd, result.get("text", ""), result.get("items", []))
        return result

    def _ensure_transferred_sync(self, cloud_path: str, user_id: str, expected_surl: str = "") -> dict:
        """把选中的分享内容（单个文件/文件夹）按 fs_id 精准转存到该用户的暂存目录。
        只转存所需项，避免整包转存撑爆网盘空间（大分享整包动辄上 TB）。
        expected_surl: 调用方校验过的分享链接；用于发现「校验之后、使用之前」缓存被
        并发的查看其他链接覆盖的情况（缓存写入与下载启动之间存在 await）。
        返回 {"name": 转存后的文件/文件夹名, "is_dir": 是否为文件夹} 或 {"error": ...}。"""
        entry = self._cache_get(user_id)
        surl, pwd = entry["surl"], entry["pwd"]
        if not surl:
            return {"error": _MSG_NEED_LIST}
        if expected_surl and surl != expected_surl:
            # 缓存已经属于另一个分享：此时按 basename 兜底会命中另一个分享的同名文件，
            # 结果就是把别人分享的文件转存并发给用户。宁可报错让用户重来。
            logger.warning(f"[baidu_netdisk] ⚠️ 分享缓存已被覆盖({expected_surl} -> {surl})，放弃本次转存")
            return {"error": _MSG_LINK_SWITCHED}
        items = entry["items"] or []
        target = next((it for it in items if it.get("path") == cloud_path), None)
        if not target and items:
            # 路径没命中（树被截断/缓存过期）：重新拉取目录树再找。查询用的是入口读到的局部
            # surl，所以结果一定属于本次分享；但**不要回读缓存**——刷新期间并发的「查看其他
            # 链接」可能已把缓存换成另一个分享，回读会把那份分享里的同名项当成目标
            refreshed = _list_share_tree_api(surl, pwd)
            if "error" not in refreshed:
                refreshed_items = refreshed.get("items") or []
                current = self._cache_get(user_id)
                if expected_surl and current["surl"] != expected_surl:
                    logger.warning(f"[baidu_netdisk] ⚠️ 刷新目录期间分享缓存被覆盖"
                                   f"({expected_surl} -> {current['surl']})，放弃本次转存")
                    return {"error": _MSG_LINK_SWITCHED}
                if current["surl"] == surl:
                    # 只在缓存仍属于本次分享时写回，免得把用户刚查看的新链接覆盖回旧的
                    self._cache_put(user_id, surl, pwd, refreshed.get("text", ""), refreshed_items)
                items = refreshed_items
                target = next((it for it in items if it.get("path") == cloud_path), None)
        if not target:
            base = cloud_path.strip("/").split("/")[-1]
            target = next((it for it in items if it.get("name") == base), None)
        if not target:
            return {"error": f"分享中找不到「{cloud_path}」，请先 /pan <链接> 查看目录"}
        r = _transfer_via_api(surl, pwd, self._cloud_dir_for(user_id),
                              fs_ids=[target["fs_id"]])
        if "error" in r:
            return r
        return {"name": target["name"], "is_dir": bool(target.get("is_dir"))}

    def _check_switch_allowed(self, surl: str, user_id: str) -> str:
        """切换到新链接前检查该用户是否有下载正在进行（避免缓存被覆盖导致路径串台）。"""
        if surl and self._cache_get(user_id)["surl"] != surl \
                and self._user_has_active_download(user_id):
            return "⏳ 当前有下载任务进行中，请稍后再查看其他链接"
        return ""

    @staticmethod
    def _normalize_link(link: str, pwd: str) -> tuple:
        """把用户输入规范化为 (surl, pwd)。支持：
        完整链接(含?pwd=)、链接+空格提取码、整段中文分享文案（"链接:xxx 提取码:xxx"）、
        裸surl、裸surl + 提取码(空格分隔或"提取码:"前缀)。文本中显式标注的提取码优先。"""
        text = (link or "").strip()
        surl, text_pwd = parse_share_link(text)
        if not text_pwd:
            # 链接/裸surl 之后空格分隔的纯提取码（如 "链接 052x"）
            seg = text.split(None, 1)
            rest = seg[1].strip() if len(seg) > 1 else ""
            if rest:
                first = rest.split()[0]
                if re.fullmatch(r'[A-Za-z0-9]{4,6}', first):
                    text_pwd = first
        if surl:
            return surl, (text_pwd or pwd)
        # 没有可识别的链接：按裸 surl 处理（取第一段，去掉查询串）
        seg = text.split(None, 1)
        first = seg[0] if seg else ""
        if "?" in first:  # 裸 surl 带查询串，如 1abc?pwd=052x
            first = first.split("?", 1)[0]
        if re.fullmatch(r'[A-Za-z0-9_-]{4,}', first):
            return first, (text_pwd or pwd)
        return "", (text_pwd or pwd)

    @staticmethod
    async def _interruptible_sleep(fut: asyncio.Future, seconds: int) -> None:
        """分段休眠，长间隔下也能及时感知下载完成。"""
        for _ in range(max(1, int(seconds))):
            if fut.done():
                return
            await asyncio.sleep(1)

    async def _notify_user(self, event: AstrMessageEvent, text: str) -> bool:
        """把后台任务的结果发给用户；失败时重试一次并落盘 + 记日志。

        AI 工具早已返回「已受理」，此后用户能否拿到文件/结果全看这一次发送：失败就静默
        丢弃的话，用户既拿不到文件也拿不到失败原因。重试之外再落盘一份，用户下次交互补发。"""
        for attempt in (1, 2):
            try:
                await event.send(event.plain_result(text))
                return True
            except Exception as e:
                if attempt == 2:
                    logger.error(f"[baidu_netdisk] ❌ 后台结果发送失败（用户可能收不到）: {text}")
                    logger.error(f"[baidu_netdisk] ❌ 发送异常: {e}")
                    try:
                        _notice_queue(event.get_sender_id(), text)
                    except Exception as queue_exc:
                        # 落盘失败不能反过来把投递流程打挂：日志里已经留了结果文本
                        logger.error(f"[baidu_netdisk] ❌ 记录待补发结果失败: {queue_exc}")
                else:
                    await asyncio.sleep(2)
        return False

    async def _deliver_download_result(self, event: AstrMessageEvent, dl: dict,
                                       share_url: str, label: str) -> None:
        """把下载好的文件/文件夹发给用户；AI 工具任务与「命令被中断」的补发共用。"""
        if dl.get("is_dir"):
            # notify=False：这里是唯一一次发送，_send_folder 内部再发一次就是重复
            text = await self._send_folder(event, dl, share_url, label=label, notify=False)
            await self._notify_user(event, text)
        elif await self._send_file(event, dl, share_url):
            await self._notify_user(event, f"✅ 「{label}」下载并发送完成")
        else:
            await self._notify_user(event, f"📁 「{label}」已下载到 {dl['path']}，但发送失败")

    def _schedule_interrupt_delivery(self, event: AstrMessageEvent, guard: "_DownloadGuard",
                                     consumed: dict, share_url: str, label: str) -> None:
        """「命令被中断」的补发入口（定时器回调，不是协程）：确认命令没接手才建补发任务。

        future 完成时兜底回调可能比命令协程先被调度，那一刻命令还活着；所以先等一拍
        （见调用处），到这里命令要么已经置位、要么已经被取消。"""
        if consumed.get("by_command"):
            return
        self._spawn_background_job(
            self._deliver_after_interrupt(event, guard, share_url, label))

    async def _deliver_after_interrupt(self, event: AstrMessageEvent, guard: "_DownloadGuard",
                                       share_url: str, label: str) -> None:
        """命令被中断后补发下载结果（是否该补发已由 _schedule_interrupt_delivery 判定）。

        结果取自 guard 而不是 future：future 可能已被调用方的取消连带取消。worker 还在跑时
        就等着——它自身受下载超时约束，不会永远不结束。"""
        while guard.outcome() is None:
            await asyncio.sleep(_INTERRUPT_POLL_SECONDS)
        ok, payload = guard.outcome()
        if not ok:
            await self._notify_user(event, f"❌ 下载任务异常: {payload}")
            return
        if not isinstance(payload, dict):
            await self._notify_user(event, "❌ 下载任务异常：结果格式不正确")
            return
        if "error" in payload:
            await self._notify_user(event, f"❌ {payload['error']}")
            return
        await self._deliver_download_result(event, payload, share_url, label)

    def _spawn_background_job(self, coro: Coroutine[object, object, None], dl_key: str = "",
                              claim_uid: str = "") -> None:
        """把耗时的转存/下载/发送丢进独立任务执行，并持有引用避免被 GC。

        LLM 工具的调用栈受框架 tool_call_timeout（默认 120 秒）约束，而一次转存加下载
        动辄几分钟到几小时：留在工具调用栈上必然被 wait_for 掐断，掐断时下载线程还在跑。
        独立任务不会被工具的取消连带取消，结果由任务自己直接发给用户。
        dl_key: 非空时登记「待执行」并在任务结束时注销——新建任务要等事件循环下一次调度才
        会跑到 _begin_download，同一轮对话里的第二次工具调用必须能立刻看到它，否则会重复受理。
        claim_uid: 非空时在任务生命周期内占住该用户的「切链窗口」：受理之后、下载登记之前
        用户若查看了别的链接，缓存就被换掉，任务只能报错（先是「已受理」再是「链接已切换」）。"""
        if dl_key:
            self._pending_jobs.add(dl_key)
        if claim_uid:
            self._switch_claims.add(claim_uid)
        try:
            task = asyncio.create_task(coro)
        except BaseException:
            # 任务没建起来就注销，否则这些键会永远占着，之后同样的请求全被当成「正在下载中」
            if dl_key:
                self._pending_jobs.discard(dl_key)
            if claim_uid:
                self._switch_claims.discard(claim_uid)
            raise
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            self._background_tasks.discard(t)
            if dl_key:
                self._pending_jobs.discard(dl_key)
            if claim_uid:
                self._switch_claims.discard(claim_uid)
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error(f"[baidu_netdisk] ❌ 后台任务异常: {exc}")

        task.add_done_callback(_on_done)

    async def _watch_download_progress(self, event: AstrMessageEvent, dl_future: asyncio.Future,
                                       prog_q: "queue.Queue") -> None:
        """后台任务里转发下载进度与错误消息，直到下载任务结束。"""
        file_info = None
        while not dl_future.done():
            file_info, text = _next_progress(prog_q, file_info)
            if text is not None:
                await event.send(event.plain_result(text))
            if file_info and not dl_future.done():
                fname, total = file_info
                current = _get_local_file_size(fname)
                if current > 0 and total > 0:
                    pct = min(current / total * 100, 99.9)
                    await event.send(event.plain_result(f"⏬ 下载中: {pct:.1f}%"))
            await self._interruptible_sleep(dl_future, self._progress_interval)

    def _is_blacklisted(self, event: AstrMessageEvent) -> bool:
        if self._get_send_mode() != "onebot":
            return False  # 黑名单仅 OneBot 模式生效
        sid = event.get_sender_id()
        return sid and sid in self._blacklist

    def _get_max_mb(self) -> int:
        # 0 = 不限制（与 _conf_schema.json 的默认值一致）；official 模式下硬限 200MB（发了也发不了）
        cap = _config_int(self.config, "max_file_size", 0, minimum=0)
        if self._get_send_mode() == "official":
            cap = min(cap, FLASH_TASK_LIMIT_MB) if cap > 0 else FLASH_TASK_LIMIT_MB
        return cap

    def _get_send_mode(self) -> str:
        return str(self.config.get("send_mode", "onebot")).strip().lower()

    def _allow_insecure_tls(self) -> bool:
        """是否允许工具下载时在证书校验失败后退回不校验模式（默认关，见配置项 allow_insecure_tls）。"""
        return _config_bool(self.config, "allow_insecure_tls", False)

    def _get_zip_max_mb(self) -> int:
        """文件夹打包 zip 的体积上限（MB），0 = 不限制。"""
        return _config_int(self.config, "zip_max_size", _ZIP_MAX_MB_DEFAULT, minimum=0)

    def _get_send_files_threshold(self) -> int:
        """文件夹内文件数 ≤ 此值时逐个发送（原始文件更易命中秒传），0 = 总是打包。"""
        return _config_int(self.config, "send_files_threshold", _SEND_FILES_THRESHOLD_DEFAULT, minimum=0)

    def _is_official_target(self, event: AstrMessageEvent) -> bool:
        """事件是否来自 QQ 官方机器人平台（该平台大文件只能回退成链接）。"""
        platform = getattr(event.platform_meta, "name", "") if event.platform_meta else ""
        return "qq_official" in platform

    async def _send_direct_with_fallback(self, event: AstrMessageEvent, file_path: str, name: str,
                                         share_url: str, msg: str, sep: str = "，",
                                         ok_log: str = "", fail_log: str = "文件发送失败") -> bool:
        """直接发送文件；失败则回发一条降级文案（含分享链接），并返回 False。

        sep 是降级文案与分享链接之间的标点：各平台历史文案不同，逐字保留。
        ok_log / fail_log 同理：各平台原有的排查关键字（哪个通道发的、失败在哪一步）
        不能因为合并成一个函数就丢掉，默认值只给本就没有专属日志的调用点用。
        降级提示本身发不出去时用户将完全没有反馈，所以至少留下日志便于排查。"""
        try:
            await event.send(MessageChain(chain=[File(name=name, file=file_path)]))
            if ok_log:
                logger.info(f"[baidu_netdisk] {ok_log}")
            return True
        except Exception as e:
            logger.warning(f"[baidu_netdisk] ⚠️ {fail_log}: {e}")
        if share_url:
            msg = f"{msg}{sep}请自行下载: {share_url}"
        try:
            await event.send(MessageChain(chain=[Plain(msg)]))
        except Exception as e:
            logger.warning(f"[baidu_netdisk] ⚠️ 发送降级提示失败: {e}；内容: {msg}")
        return False

    async def _send_file(self, event: AstrMessageEvent, dl: dict, share_url: str = "") -> bool:
        """根据平台类型和文件大小选择发送方式。"""
        file_path = dl["path"]
        file_size = dl["size"]
        size_mb = file_size / (1024 * 1024)
        name = dl["name"]
        logger.info(f"[baidu_netdisk] _send_file: name={name}, size={size_mb:.1f}MB, share_url={share_url[:50] if share_url else 'None'}")

        # 自动检测平台，优先于配置
        platform = getattr(event.platform_meta, "name", "") if event.platform_meta else ""
        logger.info(f"[baidu_netdisk] _send_file: platform={platform}")
        if self._is_official_target(event):
            logger.info(f"[baidu_netdisk] _send_file: detected qq_official, using _send_official")
            return await self._send_official(event, file_path, name, size_mb, share_url)

        send_mode = self._get_send_mode()
        logger.info(f"[baidu_netdisk] _send_file: send_mode={send_mode}")
        if send_mode == "official":
            return await self._send_official(event, file_path, name, size_mb, share_url)
        elif send_mode == "other":
            return await self._send_other(event, file_path, name, size_mb, share_url)
        else:  # onebot 及默认
            return await self._send_onebot(event, file_path, name, size_mb, share_url)

    async def _send_onebot(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = "") -> bool:
        """OneBot / napcat 路径：直接发送文件，失败则发送链接"""
        logger.info(f"[baidu_netdisk] _send_onebot: name={name}, size={size_mb:.1f}MB")
        return await self._send_direct_with_fallback(
            event, file_path, name, share_url, f"⚠️ 文件 {size_mb:.1f}MB 发送失败", sep="。",
            ok_log="_send_onebot: direct send success",
            fail_log="_send_onebot: direct send failed")

    async def _send_other(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = "") -> bool:
        """其他平台路径：直接发送文件，失败则发送链接"""
        return await self._send_direct_with_fallback(
            event, file_path, name, share_url, "⚠️ 文件发送失败",
            fail_log="send file failed")

    async def _send_official(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = "") -> bool:
        """QQ官方机器人路径：<=200MB 走官方API上传，>200MB 只能返回链接"""
        logger.info(f"[baidu_netdisk] _send_official: name={name}, size={size_mb:.1f}MB, limit={FLASH_TASK_LIMIT_MB}MB")
        if size_mb <= FLASH_TASK_LIMIT_MB:
            logger.info(f"[baidu_netdisk] _send_official: file <= limit, sending directly")
            # 上传失败同样回发降级提示：不回发用户就只看到「已下载」却拿不到任何文件
            return await self._send_direct_with_fallback(
                event, file_path, name, share_url, "⚠️ 文件发送失败",
                fail_log="_send_official: send failed")

        logger.info(f"[baidu_netdisk] _send_official: file > limit, returning link")
        msg = "⚠️ 文件超过200MB，QQ官方API暂不支持大文件上传"
        if share_url:
            msg += f"，请自行下载: {share_url}"
        try:
            await event.send(MessageChain(chain=[Plain(msg)]))
        except Exception as e:
            # 兜底提示本身发不出去时用户将完全没有反馈，至少留下日志便于排查
            logger.warning(f"[baidu_netdisk] ⚠️ 发送降级提示失败: {e}；内容: {msg}")
        return False

    async def _send_folder(self, event: AstrMessageEvent, dl: dict, share_url: str = "",
                           label: str = "", notify: bool = True) -> str:
        """文件夹下载完成后的发送策略（QQ 等平台不能直接发送文件夹）：
        文件数 ≤ 阈值（默认 3，可配置）时逐个发送；否则打包 zip 当单文件发送；
        打包失败或体积超限时降级为「本地路径 + 文件清单」。
        notify=False 时降级文本不在内部发送，由调用方负责发给用户（命令路径下
        调用方会 yield 返回值，内部再发一次就是重复发送）。"""
        files = dl.get("files") or []
        count = len(files)
        folder_name = dl.get("name") or "未命名"
        subject = label or f"文件夹 '{folder_name}'"
        total_bytes = sum(int(f.get("size") or 0) for f in files)

        # 文件少时逐个发送：原始文件更易命中 QQ 秒传（本地 zip 包哈希唯一，必然完整上传一遍）
        threshold = self._get_send_files_threshold()
        if 0 < count <= threshold:
            sent = 0
            for fd in files:
                if await self._send_file(event, fd, share_url):
                    sent += 1
            if sent == count:
                return f"✅ {subject} 下载完成，共 {count} 个文件已全部发送"
            if sent > 0:
                return (f"✅ {subject} 下载完成，共 {count} 个文件，"
                        f"已发送 {sent} 个（其余发送失败），本地路径: {dl['path']}")

        # 打包 zip 前先判断体积，避免压缩完才发现发不动（白占磁盘/耗时）
        platform = getattr(event.platform_meta, "name", "") if event.platform_meta else ""
        is_official = "qq_official" in platform or self._get_send_mode() == "official"
        size_text = _format_size(str(total_bytes))
        if is_official and total_bytes > FLASH_TASK_LIMIT_MB * 1024 * 1024:
            return await self._folder_fallback(
                event, dl, f"⚠️ 文件夹约 {size_text}，超过 QQ 官方 {FLASH_TASK_LIMIT_MB}MB 限制，未打包发送", subject, notify)
        zip_max_mb = self._get_zip_max_mb()
        if zip_max_mb > 0 and total_bytes > zip_max_mb * 1024 * 1024:
            return await self._folder_fallback(
                event, dl, f"⚠️ 文件夹约 {size_text}，超过打包上限 {zip_max_mb}MB（配置 zip_max_size），未打包发送", subject, notify)
        # 打包需临时占用等量磁盘（zip 与原始文件并存），空间不足直接降级，避免压到一半失败
        try:
            free = shutil.disk_usage(PLUGIN_DATA_DIR).free
            if free < total_bytes * 1.05:
                return await self._folder_fallback(
                    event, dl,
                    f"⚠️ 打包需约 {_format_size(str(total_bytes))} 临时空间，当前可用 {_format_size(str(free))}，空间不足未打包",
                    subject, notify)
        except Exception as e:
            logger.warning(f"[baidu_netdisk] ⚠️ _send_folder: 磁盘空间检查失败: {e}")

        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", folder_name).strip() or "folder"
        zip_name = f"{safe_name}.zip"
        zip_path = os.path.join(PLUGIN_DATA_DIR, "zips", f"{safe_name}_{uuid.uuid4().hex[:8]}.zip")
        z = await asyncio.to_thread(_zip_files, files, zip_path, folder_name)
        if "error" in z:
            return await self._folder_fallback(event, dl, f"⚠️ 打包失败（{z['error']}），未发送", subject, notify)
        logger.info(f"[baidu_netdisk] _send_folder: 打包完成 name={zip_name}, size={z['size']}, "
                    f"stored={z.get('stored')}, deflated={z.get('deflated')}")
        # 磁盘上用带随机后缀的唯一名（避免同名文件夹并发打包互相覆盖），发给用户时用干净名
        z_send = dict(z, name=zip_name)
        try:
            sent_ok = await self._send_file(event, z_send, share_url)
        finally:
            # zip 是临时产物：发送结束后即清理（成功已送达 / 失败也发不动），避免堆积占盘
            try:
                os.remove(z["path"])
            except OSError:
                pass  # 临时 zip 可能已被上一轮清理删除
        if sent_ok:
            return f"✅ {subject} 下载完成，已打包为 {zip_name} 发送（共 {count} 个文件）"
        return await self._folder_fallback(event, dl, "⚠️ zip 发送失败，未发送", subject, notify)

    async def _folder_fallback(self, event: AstrMessageEvent, dl: dict, extra: str = "",
                               label: str = "", notify: bool = True) -> str:
        """文件夹无法直接发送时的降级输出：报完成 + 本地路径 + 文件清单。

        notify=False 只返回文本、不发送：命令路径的调用方会 yield 这段文本，
        内部再发一次就会让用户收到两遍。"""
        files = dl.get("files") or []
        subject = label or f"文件夹 '{dl.get('name') or '未命名'}'"
        lines = [f"✅ {subject} 下载完成，共 {len(files)} 个文件"]
        if extra:
            lines.append(extra)
        lines.append(f"📁 本地保存路径: {dl['path']}")
        lines.append("📋 可用 /pan down <路径> 挑选下载，文件清单（最多显示 20 个）：")
        for fd in files[:20]:
            lines.append(f"• {fd.get('name')}（{_format_size(str(fd.get('size') or 0))}）")
        if len(files) > 20:
            lines.append(f"… 另有 {len(files) - 20} 个文件未显示")
        text = "\n".join(lines)
        if notify:
            await event.send(event.plain_result(text))
        return text

    @filter.llm_tool(name="pan_list")
    async def pan_list(self, event: AstrMessageEvent, link: str, pwd: str = "") -> str:
        """查看百度网盘分享链接的目录结构。用户说"查一下这个百度网盘链接"、"看看里面有什么文件"时调用。
        特大分享（TB级）也能正常查看（只读目录，不转存、不占网盘空间）。
        超大目录树最多展示3层/约300项，超出部分会在末尾提示；下载更深层的内容时把已知路径传给下载工具即可。

        Args:
            link(string): 百度网盘分享链接，如 https://pan.baidu.com/s/1abc123（也可直接传用户发来的整段分享文案，会自动识别链接与提取码）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return _MSG_BLACKLISTED

        surl, pwd, uid, err, _ = self._resolve_tool_link(event, link, pwd, require_link=True)
        if err:
            return err

        result = await asyncio.to_thread(self._transfer_and_list, surl, pwd, uid)
        if "error" in result:
            return result["error"]
        return (f"{result['text']}\n\n"
                f"（环境信息：网盘转存目录为 {self._cloud_dir_for(uid)}，本地下载目录为 {state.download_dir}，"
                f"后续转存/下载结果都在这两个目录下。目录树每项前面的数字是序号，"
                f"下载/转存时可直接把序号传给 pan_download_dir / pan_transfer）")

    @filter.llm_tool(name="pan_download_dir")
    async def pan_download_dir(self, event: AstrMessageEvent, folder_path: str, link: str = "", pwd: str = "") -> str:
        """下载百度网盘分享中的文件夹或单个文件。用户说"下载第3个"、"下载xxx文件夹"、"下载xxx里面的xxx文件夹"、"下载xxx这个文件"时调用。
        folder_path 可以传目录树每项前面的序号（如 "3"），也可以传与 pan_list 返回的目录树中显示的路径完全一致的路径（从分享根目录第一层开始，如 "初中资料/2025万唯"），不要自创或省略层级。如果还没查看过目录树，先调用 pan_list。
        工具只转存选中项（需要网盘剩余空间大于该项大小）然后下载。若返回"空间不足"等错误，如实告诉用户原因，并建议挑选更小的文件/文件夹分批下载。
        本工具只负责受理，调用后立即返回「已开始」：插件在后台转存并下载，完成（或失败）后会把结果与文件直接发给用户。
        因此不要重复调用本工具，也不要自己编造下载结果或本地路径；若后台回报「空间不足」等错误，如实转告用户并建议挑选更小的文件/文件夹分批下载。

        Args:
            folder_path(string): 目录树每项前面的序号（如 "3"）或目录树中显示的文件夹/文件路径（如 "初中资料" 或 "初中资料/2025万唯"）
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供；也可传整段分享文案，会自动识别链接与提取码）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return _MSG_BLACKLISTED

        if not folder_path.strip():
            return "缺少 folder_path 参数（目录树中显示的序号或路径）"

        surl, pwd, uid, err, cached = self._resolve_tool_link(event, link, pwd)
        if err:
            return err

        # 传了链接时序号属于「还没列出来」的新分享，不能按旧缓存解析；
        # 交给后台任务在列目录之后再解析（见 _job_download_and_send）
        if not link:
            folder_path, sel_err = self._resolve_tree_selection(cached, folder_path)
            if sel_err:
                return sel_err

        dl_key = f"tool:pan_download_dir:{uid}:{surl or cached['surl']}:{folder_path}"
        if dl_key in self._active_downloads or dl_key in self._pending_jobs:
            logger.warning(f"[baidu_netdisk] ⚠️ pan_download_dir: duplicate blocked: {dl_key}")
            return _MSG_BUSY
        # 转存加下载动辄几分钟以上，远超框架默认 120 秒的工具超时：
        # 交给后台任务执行，结果由它自己发给用户（详见 _spawn_background_job）
        self._spawn_background_job(self._job_download_and_send(
            event, uid, dl_key, folder_path, surl, pwd, cached["surl"] if not surl else "",
            switch_checked=True), dl_key, uid)
        return (f"已受理「{folder_path}」的下载，正在后台转存并下载，完成后会把文件直接发到这里"
                f"（若前面已有下载任务，会先排队）。请告知用户已开始，不要重复调用本工具。")

    @filter.llm_tool(name="pan_download_all")
    async def pan_download_all(self, event: AstrMessageEvent, link: str = "", pwd: str = "") -> str:
        """下载百度网盘分享中的所有文件。用户说"全部下载"、"下载所有文件"、"都下载"时调用。
        注意：需要网盘剩余空间大于分享总大小；特大分享（TB级）会转存失败并返回空间不足提示，此时应引导用户改用 pan_download_dir 挑选需要的文件夹或文件分批下载。
        本工具只负责受理，调用后立即返回「已开始」：插件在后台转存并下载，完成（或失败）后把结果与文件直接发给用户。
        因此不要重复调用本工具，也不要自己编造下载结果或本地路径；若后台回报「空间不足」，如实转告用户并建议改用 pan_download_dir 分批下载。

        Args:
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供；也可传整段分享文案，会自动识别链接与提取码）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return _MSG_BLACKLISTED

        surl, pwd, uid, err, cached = self._resolve_tool_link(event, link, pwd)
        if err:
            return err

        dl_key = f"tool:pan_download_all:{uid}"
        if dl_key in self._active_downloads or dl_key in self._pending_jobs:
            logger.warning(f"[baidu_netdisk] ⚠️ pan_download_all: duplicate blocked: {dl_key}")
            return _MSG_BUSY
        # 与 pan_download_dir 同样的理由：实际工作在后台任务里跑，避免被工具超时掐断
        self._spawn_background_job(self._job_download_and_send(
            event, uid, dl_key, "", surl, pwd, cached["surl"] if not surl else "",
            switch_checked=True), dl_key, uid)
        return ("已受理「下载全部文件」，正在后台转存并下载，完成后会把文件直接发到这里"
                "（若前面已有下载任务，会先排队）。请告知用户已开始，不要重复调用本工具。")

    @filter.llm_tool(name="pan_transfer")
    async def pan_transfer(self, event: AstrMessageEvent, link: str = "", pwd: str = "", path: str = "") -> str:
        """把百度网盘分享中的文件或文件夹转存到自己的网盘（不下载到本地）。用户说"转存一下"、"存到我的网盘"、"先转存"时调用。
        path 传目录树每项前面的序号（如 "3"）或 pan_list 目录树中显示的路径则只转存该项（需要网盘剩余空间大于该项大小）；不传 path 则转存分享根目录的全部内容（特大分享会因空间不足失败）。
        本工具只负责受理，调用后立即返回「已开始」：插件在后台完成转存，并把转存结果（含完整网盘路径）直接发给用户。
        因此不要重复调用本工具，也不要自己编造转存结果或网盘路径。

        Args:
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供；也可传整段分享文案，会自动识别链接与提取码）
            pwd(string): 提取码（可选）
            path(string): 要转存的文件夹或文件在目录树中的序号或路径（可选，不传则转存全部）
        """
        if self._is_blacklisted(event):
            return _MSG_BLACKLISTED

        surl, pwd, uid, err, cached = self._resolve_tool_link(event, link, pwd)
        if err:
            return err

        if path and not link:
            # 序号按受理时的缓存解析；传了链接则交给后台任务列目录后再解析（理由同 pan_download_dir）
            path, sel_err = self._resolve_tree_selection(cached, path)
            if sel_err:
                return sel_err

        # 大分享转存同样可能超过工具超时：交给后台任务，结果由它自己发给用户
        transfer_key = f"tool:pan_transfer:{uid}:{surl or cached['surl']}:{path}"
        if transfer_key in self._pending_jobs:
            logger.warning(f"[baidu_netdisk] ⚠️ pan_transfer: duplicate blocked: {transfer_key}")
            return "⏳ 该转存正在进行中，请稍候..."
        self._spawn_background_job(self._job_transfer(
            event, uid, path, surl, pwd, cached["surl"] if not surl else "",
            switch_checked=True), transfer_key, uid)
        target = f"「{path}」" if path else "分享全部内容"
        return (f"已受理{target}的转存，完成后会把转存结果直接发给用户。"
                "请告知用户已开始，不要重复调用本工具，也不要自己编造转存结果。")

    async def _resolve_job_link(self, event: AstrMessageEvent, uid: str, link: str, pwd: str,
                                cached_surl: str, switch_checked: bool) -> str | None:
        """后台任务的链接解析序幕，返回可用的 surl；返回 None 表示已通知用户并应当终止。

        两个后台任务（下载 / 转存）共用同一段序幕，错误一律走 _notify_user 直发用户
        （后台任务不在命令生成器里，没有 yield 可用）。

        switch_checked：工具已同步检查过并占住了切链窗口（_switch_claims），
        这里再查一次只会看到自己的占位，把正常请求误判成「别人在下载」。"""
        surl = link
        if surl:
            if not switch_checked:
                blocked = self._check_switch_allowed(surl, uid)
                if blocked:
                    await self._notify_user(event, f"❌ {blocked}")
                    return None
            result = await asyncio.to_thread(self._transfer_and_list, surl, pwd, uid)
            if "error" in result:
                await self._notify_user(event, f"❌ {result['error']}")
                return None
        else:
            surl = cached_surl
        if not surl:
            await self._notify_user(event, f"❌ {_MSG_NEED_LIST}")
            return None
        return surl

    async def _job_download_and_send(self, event: AstrMessageEvent, uid: str, dl_key: str,
                                     folder_path: str, link: str, pwd: str,
                                     cached_surl: str = "", switch_checked: bool = False) -> None:
        """AI 工具的实际工作：查看链接（可选）→ 按需转存 → 下载 → 自动发送。

        在独立任务里执行（而不是留在工具调用栈上）：LLM 工具受框架 tool_call_timeout
        （默认 120 秒）约束，一次转存加下载动辄几分钟以上。folder_path 为空表示
        下载该分享的全部内容。link 为空时改用 cached_surl（工具受理那一刻的缓存快照），
        本任务不重读缓存——受理与执行之间用户可能已查看别的链接，重读会按那份分享里的
        同名文件转存下载。完成/失败的消息由本任务直接发给用户。"""
        try:
            surl = await self._resolve_job_link(event, uid, link, pwd, cached_surl, switch_checked)
            if not surl:
                return
            # 序号只在「本任务自己刚列出的目录树」里解析：link 非空时上一步已刷新缓存，
            # 此时读缓存才拿得到这棵树里的序号（受理时的缓存可能属于别的分享）。
            # link 为空说明工具受理时已用同一份缓存解析过，不能再解一次——路径恰好是
            # 纯数字文件名（如 "3"）时会被误当成序号（工具也是同样的条件，见 pan_download_dir）
            if folder_path and link:
                folder_path, sel_err = self._resolve_tree_selection(self._cache_get(uid), folder_path)
                if sel_err:
                    await self._notify_user(event, f"❌ {sel_err}")
                    return
            whole_share = not folder_path
            label = folder_path or "全部文件"
            try:
                guard, queued = await self._begin_download(uid, dl_key)
            except _DuplicateDownloadError:
                logger.warning(f"[baidu_netdisk] ⚠️ 重复下载请求已忽略: {dl_key}")
                await self._notify_user(event, _MSG_BUSY)
                return
            dl_future: asyncio.Future | None = None
            try:
                # 转存放在持锁期间：同一用户的两次下载不会同时往同一个 bucket 写文件
                if whole_share:
                    tr = await asyncio.to_thread(_transfer_via_api, surl, pwd, self._cloud_dir_for(uid))
                else:
                    # 传入已校验的链接：缓存若被并发的「查看其他链接」覆盖，宁可报错也不串台
                    tr = await asyncio.to_thread(self._ensure_transferred_sync, folder_path, uid, surl)
                if "error" in tr:
                    await self._notify_user(event, f"❌ {tr['error']}")
                    return
                if queued:
                    await self._notify_user(
                        event,
                        f"⏳ 当前下载任务已满（上限 {_MAX_CONCURRENT_DOWNLOADS} 个），"
                        "本次已排队，前面的任务结束后自动开始...")
                prog_q = queue.Queue() if self._progress_enabled else None
                dl_future = guard.start(
                    download_from_cloud, tr.get("name", ""), self._get_max_mb(), prog_q,
                    self._cloud_dir_for(uid), _user_bucket(uid))
                if prog_q:
                    await self._watch_download_progress(event, dl_future, prog_q)
                dl = await dl_future
                if "error" in dl:
                    await self._notify_user(event, f"❌ {dl['error']}")
                    return
                share_url = f"https://pan.baidu.com/s/{surl}"
                await self._deliver_download_result(event, dl, share_url, label)
            finally:
                if dl_future is None:
                    guard.abandon()  # worker 还没启动，立即归还锁与下载通道
                elif not dl_future.done():
                    # 任务被取消（插件重载/卸载）时下载线程仍在跑，结果不会有人发送
                    logger.warning(f"[baidu_netdisk] ⚠️ 后台任务被取消，下载仍在继续但结果不会发送: "
                                   f"{folder_path or '全部文件'}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[baidu_netdisk] ❌ 后台下载任务异常: {e}")
            await self._notify_user(event, f"❌ 下载任务异常: {e}")

    async def _job_transfer(self, event: AstrMessageEvent, uid: str, path: str,
                            link: str, pwd: str, cached_surl: str = "",
                            switch_checked: bool = False) -> None:
        """AI 工具「转存」的实际工作，在独立任务里执行（理由同 _job_download_and_send）。

        link 为空时改用 cached_surl（工具受理那一刻的缓存快照），本任务不重读缓存，
        避免按用户并发查看的新分享里的同名文件转存。"""
        try:
            surl = await self._resolve_job_link(event, uid, link, pwd, cached_surl, switch_checked)
            if not surl:
                return
            cloud_dir = self._cloud_dir_for(uid)
            if path:
                # 序号在列完目录后再解析；link 为空时工具已解析过（理由同 _job_download_and_send）
                if link:
                    path, sel_err = self._resolve_tree_selection(self._cache_get(uid), path)
                    if sel_err:
                        await self._notify_user(event, f"❌ {sel_err}")
                        return
                tr = await asyncio.to_thread(self._ensure_transferred_sync, path, uid, surl)
                if "error" in tr:
                    await self._notify_user(event, f"❌ {tr['error']}")
                    return
                await self._notify_user(event, f"✅ 已转存「{path}」到网盘 {cloud_dir}/{tr['name']}")
                return
            # 未指定 path：转存分享根目录全部内容
            tr = await asyncio.to_thread(_transfer_via_api, surl, pwd, cloud_dir)
            if "error" in tr:
                await self._notify_user(event, f"❌ {tr['error']}")
                return
            names = "、".join(tr.get("filenames", [])[:10]) or "全部内容"
            await self._notify_user(event, f"✅ 已转存（{names}）到网盘 {cloud_dir}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[baidu_netdisk] ❌ 后台转存任务异常: {e}")
            await self._notify_user(event, f"❌ 转存任务异常: {e}")

    async def _run_login_action(self, event: AstrMessageEvent, task: Callable[[], dict],
                                ok_text: Callable[[dict], str], *, warn: str = "", pending: str = "",
                                tail: str | None = None) -> AsyncGenerator[MessageEventResult, None]:
        """登录/退出各分支的公共骨架：私聊告警 → 进度提示 → 执行 → 报错或报成功（可选补一句）。

        写成生成器而不是用 event.send 直发：登录命令原本就是靠 yield 交由框架结果管线
        投递的，改成直发会绕过框架的结果处理，多段消息的时序也随之变化。

        warn 仅对凭据类登录（账号密码 / BDUSS / Cookie）给出，扫码与退出不需要；
        ok_text 接收结果字典，因为各方式的成功文案不同（是否含 STOKEN、是否带 output）。"""
        if warn and not event.is_private_chat():
            yield event.plain_result(warn)
        if pending:
            yield event.plain_result(pending)
        result = await asyncio.to_thread(task)
        if "error" in result:
            yield event.plain_result(f"❌ {result['error']}")
            return
        yield event.plain_result(ok_text(result))
        if tail:
            yield event.plain_result(tail)

    def _login_menu(self) -> str:
        """/pan login 登录菜单：数字简写或完整指令两种输入都可用（仅管理员可用）"""
        return (
            "百度网盘登录/退出方式（仅管理员可用；数字简写与完整指令任选其一）:\n"
            "1  扫码登录    /pan login 1  或  /pan login qr\n"
            "2  账密登录    /pan login 2 <账号> <密码>  或  /pan login pwd <账号> <密码>\n"
            "3  BDUSS       /pan login 3 <BDUSS> [STOKEN]  或  /pan login bduss <BDUSS> [STOKEN]\n"
            "4  Cookie      /pan login 4 <完整Cookies>  或  /pan login cookies <完整Cookies>\n"
            "0  退出登录    /pan login 0  或  /pan login out\n"
            "⚠️ 密码、BDUSS、Cookie 都等同于账号密码：请私聊执行，用完后撤回消息"
        )

    async def _login_dispatch(self, event: AstrMessageEvent, parts: list) -> AsyncGenerator[MessageEventResult, None]:
        """/pan login <方式> [参数]：统一登录入口，负责分发到具体登录/退出流程。

        全部文案都靠 yield 交给框架结果管线投递（与重构前一致）；各登录分支的
        「告警 → 进度 → 结果」骨架由 _run_login_action 统一，这里只负责转发。"""
        if len(parts) < 2 or parts[1].lower() in ("help", "-h"):
            yield event.plain_result(self._login_menu())
            return
        method = parts[1].lower()

        if method in ("1", "qr", "qrcode"):
            async for r in self._qrlogin_flow(event):
                yield r
            return

        if method in ("2", "pwd", "password"):
            if len(parts) < 4:
                yield event.plain_result("用法: /pan login 2 <百度账号> <密码>（或 /pan login pwd <账号> <密码>）")
                return
            async for r in self._run_login_action(
                event,
                lambda: login_bpcs(parts[2], parts[3], self._allow_insecure_tls()),
                lambda _r: "✅ 登录成功！BaiduPCS-Go 已保存登录凭证，全局生效。",
                warn="⚠️ 密码会出现在聊天记录中，建议私聊执行以防泄露",
                pending="⏳ 正在登录百度网盘...",
                tail="🔐 密码已出现在聊天记录中，建议撤回/删除该消息，防止泄露",
            ):
                yield r
            return

        if method in ("3", "bduss"):
            if len(parts) < 3 or len(parts[2]) < 10:
                yield event.plain_result(
                    "用法: /pan login 3 <BDUSS值> [STOKEN值]（或 /pan login bduss <BDUSS> [STOKEN]）\n"
                    "获取: 浏览器登录 pan.baidu.com → F12 → Application → Cookies → 复制 BDUSS 和 STOKEN 的值"
                )
                return
            bduss = parts[2]
            stoken = parts[3] if len(parts) >= 4 else ""
            extra = "（含STOKEN，转存可用）" if stoken else "（无STOKEN，转存可能不可用，仅下载）"
            async for r in self._run_login_action(
                event,
                lambda: login_bduss_bpcs(bduss, stoken, self._allow_insecure_tls()),
                lambda _r: f"✅ 登录成功！{extra}",
                warn="⚠️ BDUSS 等同于账号密码，会留在聊天记录中，建议私聊执行并撤回消息",
                pending="⏳ 正在登录...",
            ):
                yield r
            return

        if method in ("4", "cookies"):
            if len(parts) < 3 or len(parts[2]) < 50:
                yield event.plain_result(
                    "用法: /pan login 4 <完整Cookies字符串>（或 /pan login cookies <完整Cookies>）\n"
                    "获取: 浏览器登录 pan.baidu.com 后 F12 → Network → 任意请求 → 复制 Cookie 请求头"
                )
                return
            # Cookie 字符串本身不含空格分隔的多段，用剩余参数拼接，避免被空格截断
            cookie_str = " ".join(parts[2:])
            async for r in self._run_login_action(
                event,
                lambda: login_cookies_bpcs(cookie_str, self._allow_insecure_tls()),
                lambda r: f"✅ 登录成功！{r.get('output', '')}",
                warn="⚠️ Cookie 等同于账号密码，会留在聊天记录中，建议私聊执行并撤回消息",
                pending="⏳ 正在注入 Cookies...",
            ):
                yield r
            return

        if method in ("0", "out", "logout", "off"):
            async for r in self._run_login_action(
                event,
                lambda: logout_bpcs(self._allow_insecure_tls()),
                lambda _r: "✅ 已退出百度网盘登录，本地凭证已清除。如需重新登录请发送 /pan login 1 扫码登录",
                pending="⏳ 正在退出登录...",
            ):
                yield r
            return

        # 未知方式：提示并展示菜单
        yield event.plain_result(f"未知的登录方式: {parts[1]}")
        yield event.plain_result(self._login_menu())

    async def _qrlogin_flow(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        """/pan login 1/qr 子命令：扫码登录全流程（生成二维码 → 展示 → 轮询确认 → 注入凭证），全局生效"""
        sess = _req.Session()
        sess.headers.update({"User-Agent": _BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        # Step 1: 获取二维码（session 会自动保存 BAIDUID 等初始 cookie）
        try:
            r = await asyncio.to_thread(
                lambda: sess.get(
                    "https://passport.baidu.com/v2/api/getqrcode?lp=pc&qrloginfrom=pc",
                    timeout=10,
                )
            )
            qdata = r.json()
        except Exception as e:
            yield event.plain_result(f"❌ 获取二维码失败: {e}")
            return
        if qdata.get("errno") != 0:
            yield event.plain_result("❌ 获取二维码失败")
            return
        qsign = qdata["sign"]
        qimgurl = "https://" + qdata["imgurl"]
        # 下载二维码图片
        try:
            img_r = await asyncio.to_thread(lambda: sess.get(qimgurl, timeout=10))
        except Exception as e:
            yield event.plain_result(f"❌ 下载二维码失败: {e}")
            return
        qr_path = os.path.join(PLUGIN_DATA_DIR, "qr_login.png")
        os.makedirs(os.path.dirname(qr_path), exist_ok=True)
        with open(qr_path, "wb") as f:
            f.write(img_r.content)
        # 发二维码图片
        yield event.chain_result([
            Image.fromFileSystem(qr_path),
            Plain("请用百度网盘 APP 扫码登录（3分钟内有效）")
        ])
        # Step 2: 轮询扫码状态
        gid = str(uuid.uuid4()).upper()
        confirmed = False
        for _ in range(60):
            await asyncio.sleep(3)
            tt = str(int(time.time() * 1000))
            poll_url = (
                f"https://passport.baidu.com/channel/unicast"
                f"?channel_id={qsign}&gid={gid}&tpl=mm"
                f"&_sdkFrom=1&apiver=v3&tt={tt}&_={tt}&callback="
            )
            try:
                pr = await asyncio.to_thread(lambda: sess.get(poll_url, timeout=35))
                ptext = (pr.text or "").strip()
            except Exception:
                continue
            if not ptext:
                continue
            # 去除可能的 JSONP 包裹
            if ptext.startswith("(") and ptext.endswith(")"):
                ptext = ptext[1:-1]
            if ptext.startswith("tangram") or ptext.startswith("callback"):
                ptext = ptext[ptext.index("(")+1:ptext.rindex(")")]
            # 解析 JSON
            try:
                pdata = _json.loads(ptext)
            except Exception:
                continue
            if pdata.get("errno") != 0:
                continue  # 未扫码
            cv_str = pdata.get("channel_v", "")
            if not cv_str:
                continue
            try:
                cv = _json.loads(cv_str) if isinstance(cv_str, str) else cv_str
            except Exception:
                continue
            if cv.get("status") == 1 and not confirmed:
                confirmed = True
                yield event.plain_result("✅ 已扫码，请在手机上确认登录")
                continue
            v = cv.get("v", "")
            if v:
                # Step 3: 用 v 换取登录凭证（session 自动带上之前累积的 cookie）
                login_url = (
                    f"https://passport.baidu.com/v3/login/main/qrbdusslogin"
                    f"?bduss={v}&u=&loginVersion=v4&qrcode=1&tpl=mm&apiver=v3"
                )
                try:
                    lr = await asyncio.to_thread(
                        lambda: sess.get(login_url, timeout=10, allow_redirects=False)
                    )
                except Exception as e:
                    yield event.plain_result(f"❌ 获取凭证失败: {e}")
                    return
                # 访问 pan.baidu.com 获取 pan 专用 cookie（csrfToken / PANPSC）
                try:
                    await asyncio.to_thread(
                        lambda: sess.get("https://pan.baidu.com/disk/main", timeout=10)
                    )
                except Exception as e:
                    # 预热只为补 pan 专用 cookie，失败时仍用已拿到的 cookie 继续登录
                    logger.warning(f"[baidu_netdisk] ⚠️ 登录后预热 pan cookie 失败: {e}")
                # 从 session 的 cookie jar 提取全部 cookie（已含 pan 专用 cookie）
                full_cookie = "; ".join(f"{k}={cv2}" for k, cv2 in sess.cookies.items())
                logger.info(f"[baidu_netdisk] qrlogin cookie keys: {list(sess.cookies.keys())}")
                logger.info(f"[baidu_netdisk] qrlogin full_cookie len: {len(full_cookie)}")
                bduss_m = re.search(r'BDUSS=([^\s;]+)', full_cookie)
                if not bduss_m:
                    yield event.plain_result("❌ 未能提取 BDUSS，登录失败")
                    return
                # Step 4: 用完整 cookie 登录 BaiduPCS-Go
                result = await asyncio.to_thread(login_cookies_bpcs, full_cookie,
                                                 self._allow_insecure_tls())
                if "error" in result:
                    # 回退到仅 BDUSS+STOKEN 方式
                    stoken_m = re.search(r'STOKEN=([^\s;]+)', full_cookie)
                    bduss_val = bduss_m.group(1)
                    stoken_val = stoken_m.group(1) if stoken_m else ""
                    result2 = await asyncio.to_thread(login_bduss_bpcs, bduss_val, stoken_val,
                                                      self._allow_insecure_tls())
                    if "error" in result2:
                        yield event.plain_result(f"❌ {result2['error']}")
                    else:
                        extra = "（含STOKEN，转存可能受限）" if stoken_val else "（无STOKEN）"
                        yield event.plain_result(f"✅ 扫码登录成功！{extra}")
                else:
                    yield event.plain_result("✅ 扫码登录成功！（完整cookie，转存可用）")
                return
        yield event.plain_result("❌ 二维码已超时，请重新发送 /pan login 1")

    @filter.command("pan", "下载百度网盘分享文件")
    async def on_pan(self, event: AstrMessageEvent, args: GreedyStr) -> AsyncGenerator[MessageEventResult, None]:
        """用法: /pan <链接或surl> [提取码]  |  /pan login 查看登录/退出菜单"""

        if self._is_blacklisted(event):
            return
        uid = event.get_sender_id()
        # 之前发送失败的结果在这里补发：机器人掉线时用户收不到任何提示，只能等他下次说话
        for notice in _notice_take(uid):
            yield event.plain_result(f"📬 之前有一条结果没能送达，补发如下：\n{notice}")
        parts = [p for p in str(args).split() if p]
        if not parts or parts[0] == "help":
            help_lines = [
                "用法:",
                "/pan <链接> [密码]  查看目录树（每项带序号）",
                "💡 可直接粘贴整段分享文案（含“链接:”与“提取码:”），自动识别链接与提取码",
                "/pan look  重新查看目录树（序号不变）",
                "/pan down <序号或路径>  下载文件夹或文件（自动识别类型；也支持直接传链接一键下载）",
                "💡 目录树每项前面的数字就是序号，可直接 /pan down 3 下载第 3 项",
                "/pan download <链接> [密码]  解析后自动下载整个分享",
                "（以下命令仅管理员可用，因为它们会改变全群共用的百度账号与运行工具）",
                "/pan login  登录/退出统一入口（发送 /pan login 查看登录菜单）",
                "/pan pcs  下载/更新 BaiduPCS-Go 工具（直连失败时自动挑选可用加速镜像）",
                "/pan pcs git  强制直连 GitHub 官方原始链接（不用代理、不用镜像）",
                "/pan pcs <代理URL>  通过加速代理下载并保存（如 https://gh-proxy.org/）",
                "/pan pcs off  清除已保存的加速代理",
            ]
            yield event.plain_result("\n".join(help_lines))
            return

        if parts[0].lower() == "login" and not event.is_admin():
            # 登录/退出改的是全群共用的那一个百度账号：任何人 /pan login 0 就能把账号注销，
            # 或用 /pan login 3/4 把账号换成自己的（之后所有人的下载都转存进攻击者网盘）。
            # 因此与 /pan pcs 一样按管理类命令处理；这里放在 exe 自动下载之前，避免普通用户
            # 仅靠发一条 login 就触发二进制下载。
            yield event.plain_result(
                "⛔ 登录/退出仅管理员可用：该操作会影响全群共用的百度账号。\n"
                "💡 若插件尚未登录或提示工具缺失，请联系管理员执行 /pan login。")
            return

        if parts[0].lower() == "pcs":
            # 该命令会主动下载并替换机器人本地运行的可执行文件，也能改写下载来源
            # （加速镜像/代理），属管理类操作：普通用户只使用登录时自动触发、
            # 来源不可更改的下载，因此这里要求管理员身份
            if not event.is_admin():
                yield event.plain_result(
                    "⛔ /pan pcs 仅管理员可用。\n"
                    f"💡 普通用户无需手动操作：管理员登录后插件会自动下载所需的 {_BPCS_EXE_NAME}。"
                )
                return
            state.auto_dl_failed = False  # 手动下载即重试自动下载标记
            if len(parts) > 1:
                arg2 = parts[1]
                if arg2 == "git":
                    # 强制只走 GitHub 官方原始链接直连：不用代理、也不用加速镜像
                    yield event.plain_result("⏳ 正在从 GitHub 官方 release 直连下载最新版 BaiduPCS-Go...")
                    result = await asyncio.to_thread(_download_github_exe, "", 3, False,
                                                     self._allow_insecure_tls())
                    if "ok" in result:
                        yield event.plain_result(
                            f"✅ {_BPCS_EXE_NAME} 下载成功！现在可以使用 /pan login 等命令了。"
                            + _digest_note(result)
                        )
                    else:
                        yield event.plain_result(
                            f"❌ 下载失败: {result.get('error', '未知错误')}\n"
                            "💡 网络不通时可改用 /pan pcs <加速镜像地址> 通过镜像下载，例如 /pan pcs https://gh-proxy.org/"
                        )
                    return
                if arg2.lower() == "off":
                    # 清除已保存的加速代理，回到「自动挑选可用镜像」的默认下载方式
                    state.github_proxy = ""
                    try:
                        os.remove(_GITHUB_PROXY_FILE)
                    except OSError:
                        pass  # 本来就没保存过代理
                    yield event.plain_result("🗑️ 已清除已保存的加速代理，回到自动挑选可用加速镜像的下载方式。")
                elif arg2.lower().startswith(("http://", "https://")):
                    # 保存并走指定加速代理下载
                    set_err = _set_github_proxy(arg2)
                    if set_err:
                        yield event.plain_result(f"❌ {set_err}")
                        return
                else:
                    yield event.plain_result("用法: /pan pcs [git|off|<加速镜像地址>]")
                    yield event.plain_result("  /pan pcs              下载/更新工具（直连失败时自动挑选可用加速镜像）")
                    yield event.plain_result("  /pan pcs git          强制直连 GitHub 官方原始链接（不用代理、不用镜像）")
                    yield event.plain_result("  /pan pcs <镜像地址>   指定加速镜像下载并保存（如 https://gh-proxy.org/，登录时自动下载也生效）")
                    yield event.plain_result("  /pan pcs off          清除已保存的加速代理，回到自动挑选镜像")
                    return
            # 默认流程：GitHub 官方 latest release；已保存代理则只走它，否则直连失败后自动挑选可用镜像
            via = f"加速代理 {state.github_proxy} " if state.github_proxy else ""
            yield event.plain_result(f"⏳ 正在{via}从 GitHub 官方 release 下载最新版 BaiduPCS-Go...（网络不通时会自动挑选可用加速镜像，可能稍慢）")
            result = await asyncio.to_thread(_download_github_exe, None, 3, True,
                                             self._allow_insecure_tls())
            if "ok" in result:
                used = f"（via 加速镜像 {result['via']}）" if result.get("via") else ""
                saved = " 加速代理已保存，登录时自动下载也会使用。" if state.github_proxy else ""
                yield event.plain_result(
                    f"✅ {_BPCS_EXE_NAME} 下载成功{used}！现在可以使用 /pan login 等命令了。{saved}"
                    + _digest_note(result)
                )
            else:
                yield event.plain_result(f"❌ 下载失败: {result.get('error', '未知错误')}")
            return

        # 登录菜单是纯文本，不需要 BaiduPCS-Go，先于 exe 校验直接展示
        if parts[0].lower() == "login" and (len(parts) < 2 or parts[1].lower() in ("help", "-h")):
            yield event.plain_result(self._login_menu())
            return

        # 以下命令都需要 BaiduPCS-Go.exe；登录类指令在 exe 缺失/损坏时自动下载
        if not _ensure_bpcs_exe():
            if parts[0] != "login":
                yield event.plain_result(
                    f"❌ {_BPCS_EXE_NAME} 缺失或损坏，暂时无法执行该命令；"
                    f"{'可用 /pan pcs 下载修复' if event.is_admin() else '请联系管理员处理'}"
                )
                return
            yield event.plain_result("⏳ BaiduPCS-Go 不存在或不可用，正在自动下载（GitHub 官方 release 优先，直连失败时自动挑选可用加速镜像）...")
            if not await asyncio.to_thread(_auto_download_bpcs, self._allow_insecure_tls()):
                yield event.plain_result(
                    f"❌ BaiduPCS-Go 自动下载失败：{state.auto_dl_last_error or '未知原因'}。"
                    f"请稍后重试{'（或用 /pan pcs 手动下载）' if event.is_admin() else '，或联系管理员处理'}"
                )
                return

        if parts[0] == "look":
            cached_tree = self._cache_get(uid)["tree"]
            if cached_tree:
                yield event.plain_result(cached_tree)
            else:
                yield event.plain_result("请先使用 /pan <链接> [密码] 查看目录")
            return

        # 与上面的权限门保持同样的大小写处理，否则 /pan LOGIN 会绕过分发被当成分享链接解析
        if parts[0].lower() == "login":
            async for r in self._login_dispatch(event, parts):
                yield r
            return

        # down 与 download 等价：参数是分享链接时，先解析目录再自动下载整个分享
        if parts[0].lower() in ("down", "download"):
            rest = " ".join(parts[1:])
            # 只有明确的分享链接/分享文案才按「一键下载整个分享」处理：
            # 单纯几个字母的文件名（如 test）不能被当成裸 surl，否则会误转存别的内容
            one_shot_surl, _ = parse_share_link(rest)
            if one_shot_surl:
                one_shot_surl, one_shot_pwd = self._normalize_link(rest, "")
                dl_key = f"{uid}:down:all:{one_shot_surl}"
                if dl_key in self._active_downloads or dl_key in self._pending_jobs:
                    yield event.plain_result(_MSG_BUSY)
                    return
                # 先解析：链接失效、提取码错误在这里直接报错，不会先回「已受理」再失败；
                # 与查看链接同样先确认没有别的下载在跑，避免把缓存换成另一个分享
                blocked = self._check_switch_allowed(one_shot_surl, uid)
                if blocked:
                    yield event.plain_result(f"❌ {blocked}")
                    return
                yield event.plain_result("⏳ 正在解析分享目录...")
                result = await asyncio.to_thread(self._transfer_and_list, one_shot_surl, one_shot_pwd, uid)
                if "error" in result:
                    yield event.plain_result(f"❌ {result['error']}")
                    return
                # 解析完交给后台任务下载（与 AI 工具 pan_download_all 同一条链路）：
                # 命令本身不阻塞到下载结束，任务完成/失败会把结果直接发到当前会话
                self._spawn_background_job(self._job_download_and_send(
                    event, uid, dl_key, "", "", one_shot_pwd, one_shot_surl), dl_key, uid)
                yield event.plain_result(
                    "✅ 已解析分享并开始下载全部内容（目录树已缓存，可用 /pan look 查看）。"
                    "若前面已有下载任务，本次会先排队；完成后把文件直接发到这里")
                return

            # 下载文件夹或单个文件（自动识别类型）；使用本用户缓存的链接与目录树
            cached = self._cache_get(uid)
            if not cached["surl"]:
                yield event.plain_result("请先使用 /pan <链接> [密码] 查看目录")
                return
            if len(parts) < 2:
                yield event.plain_result(
                    "用法: /pan down <序号或路径>（先 /pan <链接> 查看目录），"
                    "或 /pan download <链接> [密码] 直接下载整个分享")
                return
            # 路径可能含空格，拼回完整参数；纯数字（如 3）按目录树序号解析
            dl_path, sel_err = self._resolve_tree_selection(cached, " ".join(parts[1:]))
            if sel_err:
                yield event.plain_result(f"❌ {sel_err}")
                return
            # 用解析后的路径做去重键：同一项无论是用序号还是完整路径发起的，都视作同一个下载
            dl_key = f"{uid}:down:{dl_path}"
            logger.info(f"[baidu_netdisk] on_pan down: dl_path={dl_path}, progress_enabled={self._progress_enabled}")
            # 同一用户的下载串行执行 + 全局并发上限：锁、通道、去重键都由 worker 结束时释放，
            # 本协程被取消（插件重载、框架关闭命令生成器）也不会提前放锁（见 _DownloadGuard）
            try:
                guard, queued = await self._begin_download(uid, dl_key)
            except _DuplicateDownloadError:
                logger.warning(f"[baidu_netdisk] ⚠️ on_pan: duplicate download blocked: {dl_key}")
                yield event.plain_result(_MSG_BUSY)
                return
            dl_future = None
            try:
                # 传入已校验的链接：缓存若在转存前被并发的「查看其他链接」覆盖，
                # 宁可报错让用户重来，也不能把另一个分享的同名文件下发（见 expected_surl）
                tr = await asyncio.to_thread(self._ensure_transferred_sync, dl_path, uid, cached["surl"])
                if "error" in tr:
                    yield event.plain_result(f"❌ {tr['error']}")
                    return
                # 自动识别类型（目录树已给出 is_dir）：下载前先告知用户是文件夹还是文件
                kind = "文件夹" if tr.get("is_dir") else "文件"
                yield event.plain_result(f"⏳ 正在下载{kind}: {dl_path} ...")
                prog_q = queue.Queue() if self._progress_enabled else None
                if queued:
                    yield event.plain_result(
                        f"⏳ 当前下载任务已满（上限 {_MAX_CONCURRENT_DOWNLOADS} 个），"
                        "本次已排队，前面的任务结束后自动开始...")
                dl_future = guard.start(
                    download_from_cloud, tr["name"], self._get_max_mb(), prog_q,
                    self._cloud_dir_for(uid), _user_bucket(uid))
                share_url = f"https://pan.baidu.com/s/{cached['surl']}"
                consumed: dict = {"by_command": False}

                def _fallback_delivery(fut: asyncio.Future) -> None:
                    # 命令被框架中断（生成器关闭、插件重载）时 await 会被取消，下载结果就再没人发送。
                    # 这里只约一个「稍后确认」的定时器（不立刻建任务）：命令正常接手时它什么都不做，
                    # 被中断时才建补发任务，避免与正常路径重复发送
                    asyncio.get_running_loop().call_later(
                        1.0, self._schedule_interrupt_delivery,
                        event, guard, consumed, share_url, dl_path)

                dl_future.add_done_callback(_fallback_delivery)
                if prog_q:
                    file_info = None
                    _last_prog_size = None
                    _last_prog_time = None
                    while not dl_future.done():
                        file_info, text = _next_progress(prog_q, file_info)
                        if text is not None:
                            yield event.plain_result(text)
                        # 监控本地文件大小
                        if file_info and not dl_future.done():
                            fname, total = file_info
                            current = _get_local_file_size(fname)
                            # 如果 total==0 但本地文件有大小，用本地文件大小作为 total
                            if total <= 0 and current > 0:
                                total = current
                                file_info = (fname, total)
                            if current > 0 and total > 0:
                                now = time.time()
                                if _last_prog_time is None or now - _last_prog_time >= 1.0:
                                    if _last_prog_size is not None and _last_prog_time is not None:
                                        dt = now - _last_prog_time
                                        if dt > 0:
                                            speed_bps = (current - _last_prog_size) / dt
                                            speed_str = f"{speed_bps/1024/1024:.2f} MB/s" if speed_bps >= 1024*1024 else f"{speed_bps/1024:.1f} KB/s"
                                            remain = (total - current) / speed_bps if speed_bps > 0 else 0
                                            if remain >= 3600:
                                                eta = f"{remain/3600:.1f}h"
                                            elif remain >= 60:
                                                eta = f"{remain/60:.1f}m"
                                            else:
                                                eta = f"{remain:.0f}s"
                                            _last_prog_size = current
                                            _last_prog_time = now
                                            pct = min(current / total * 100, 100.0)
                                            yield event.plain_result(f"⏬ {pct:.1f}%  {speed_str}  ETA {eta}")
                                            if current >= total:
                                                yield event.plain_result("✅ 下载完成，正在发送...")
                                                break
                                    else:
                                        _last_prog_size = current
                                        _last_prog_time = now
                        await self._interruptible_sleep(dl_future, self._progress_interval)
                dl = await dl_future
                consumed["by_command"] = True  # 命令自己会发送，兜底补发到此为止
                logger.info(f"[baidu_netdisk] on_pan down: download complete, dl={dl}")
                if "error" in dl:
                    logger.error(f"[baidu_netdisk] ❌ on_pan down: download error: {dl['error']}")
                    yield event.plain_result(f"❌ {dl['error']}")
                else:
                    if dl.get("is_dir"):
                        # 文件夹不能直接发送：≤阈值逐个发，否则打包 zip，超限则降级为路径+清单。
                        # notify=False：这里会把返回文本 yield 给用户，内部再发一次就是重复发送
                        yield event.plain_result(
                            await self._send_folder(event, dl, share_url, notify=False))
                    else:
                        # 走统一发送策略：平台适配（onebot/official/other）、大小限制、
                        # 发送失败回退分享链接都由 _send_file 处理
                        if not await self._send_file(event, dl, share_url):
                            yield event.plain_result(f"📁 发送未成功，文件已保存到本地: {dl['path']}")
            finally:
                if dl_future is None:
                    guard.abandon()  # worker 还没启动，立即归还锁与下载通道
                elif not dl_future.done():
                    # 命令被中断（插件重载/框架关闭生成器）时 worker 仍会把文件下完，
                    # 结果由 _fallback_delivery 挂的补发任务送出；这里只留一条日志便于排查
                    logger.warning("[baidu_netdisk] ⚠️ 命令被中断，下载仍在后台继续，完成后会自动补发结果; "
                                   f"文件在下载目录 {state.download_dir} 下按网盘路径查找")
            return

        # 默认: /pan <链接> [密码] → 查看目录树（整串交给 _normalize_link 统一解析）
        surl, pwd = self._normalize_link(" ".join(parts), "")

        if not surl:
            yield event.plain_result(f"❌ {_MSG_BAD_LINK}")
            return

        blocked = self._check_switch_allowed(surl, uid)
        if blocked:
            yield event.plain_result(f"❌ {blocked}")
            return

        yield event.plain_result("⏳ 正在获取目录结构...")
        result = await asyncio.to_thread(self._transfer_and_list, surl, pwd, uid)
        if "error" in result:
            yield event.plain_result(f"❌ {result['error']}")
        else:
            yield event.plain_result(result["text"])

    async def terminate(self) -> None:
        """插件被禁用/重载时调用（框架约定，见 core/star/base.py）。

        下载线程与 BPCS 子进程无法被安全中断，所以这里不强行取消，只把在途任务数写进日志：
        否则重载后用户看到的是「命令没反应」，管理员在日志里也找不到线索。"""
        await super().terminate()
        if self._background_tasks:
            logger.warning(f"[baidu_netdisk] ⚠️ 插件卸载时仍有 {len(self._background_tasks)} 个"
                           "后台下载/转存任务在跑，它们会继续执行但结果不会发送")

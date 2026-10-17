"""工具层：原子写、JSON 读改写事务、通知/清单、目录遍历与本地文件判定。"""

import contextlib
import hashlib
import json as _json
import os
import threading
import time
from collections.abc import Callable, Iterator

from astrbot.api import logger

from ..domain.constants import _IS_WINDOWS
from ..domain.paths import _FILE_WRITE_LOCK, _MANIFEST_LOCK, _MANIFEST_PATH, _NOTICE_LOCK, _NOTICE_MAX_CHARS, _NOTICE_MAX_PER_USER, _NOTICE_PATH
from ..domain.state import state


def _atomic_write(path: str, text: str, mode: int | None = None) -> None:
    """原子写入文本：先写同目录的 .tmp，再 os.replace 覆盖目标文件。

    直接 open(..., "w") 会先把原文件截断再逐块写：进程在写入途中被中断（重启、OOM）
    就会留下半个文件，下次读取直接解析失败。

    mode 非空时给临时文件设权限再 replace：os.replace 会把临时文件的权限带到目标文件上，
    而 open() 新建临时文件走的是 umask（通常 0644），用它覆盖 0600 的凭证文件等于降权。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with _FILE_WRITE_LOCK:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, path)
        except OSError:
            # 写失败时清掉半截临时文件；原文件因尚未 replace 而保持完整
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise

def _atomic_write_json(path: str, data: object, indent: int = 2,
                       mode: int | None = None) -> None:
    """原子写入 JSON（UTF-8、保留中文可读）。"""
    _atomic_write(path, _json.dumps(data, ensure_ascii=False, indent=indent), mode)

_SKIP_WRITE: object = object()  # transform 返回它表示「本次无需落盘」（见 _write_json_locked）

def _read_json(path: str, default: object) -> object:
    """读 JSON 文件，缺失/损坏/格式不符时返回 default。

    各存储的字段清洗（历史格式兼容、类型归一）留在各自调用方，这里只管读与兜底。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return default

def _write_json_locked(path: str, lock: threading.Lock, transform: Callable, log_tag: str) -> None:
    """在锁内完成「读 → 改 → 原子写」的事务。

    读-改-写必须整体串行：两个并发下载各自「读到旧数据再写回」会丢掉对方刚写入的内容。
    transform 接收当前数据、返回要写回的新数据；返回 _SKIP_WRITE 表示本次无需落盘
    （例如没有待补发项时不该凭空创建文件）。

    除 OSError 外还吞掉序列化错误（TypeError/ValueError）：调用方都在下载完成这类
    不允许失败的主流程里，落盘失败只该记一条警告，不该把整条下载链路带崩。"""
    with lock:
        data = transform(_read_json(path, {}))
        if data is _SKIP_WRITE:
            return
        try:
            _atomic_write_json(path, data)
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f"[baidu_netdisk] ⚠️ {log_tag}: {e}")

def _notice_queue(uid: str, text: str) -> None:
    """记录一条发送失败的最终结果，等该用户下次交互时补发。

    机器人掉线 / 会话过期时，一次发送失败就意味着用户既拿不到文件也拿不到原因；
    重试之外再落盘一份，用户下次说话时还能看到。"""
    if not uid or not text:
        return

    def _add(notices: object) -> dict:
        data = _clean_notices(notices)
        items = data.setdefault(uid, [])
        items.append(text[:_NOTICE_MAX_CHARS])
        del items[:-_NOTICE_MAX_PER_USER]
        return data

    _write_json_locked(_NOTICE_PATH, _NOTICE_LOCK, _add, "保存待补发结果失败")

def _notice_take(uid: str) -> list[str]:
    """取出并清除该用户的待补发结果；没有待补发项时不落盘。"""
    taken: list[str] = []

    def _pop(notices: object) -> object:
        data = _clean_notices(notices)
        items = data.pop(uid, [])
        if not items:
            # 没有东西可取就不写：这个函数每次 /pan 都会被调用，无条件写会让每条
            # 命令都多一次写盘，还会把不存在的文件凭空创建成 {}、把损坏文件覆盖成 {}
            return _SKIP_WRITE
        taken.extend(items)
        return data

    _write_json_locked(_NOTICE_PATH, _NOTICE_LOCK, _pop, "保存待补发结果失败")
    return taken

def _clean_notices(data: object) -> dict:
    """把落盘的待补发数据归一成 `{uid: [文本]}`，丢弃历史格式里非法/空的条目。"""
    if not isinstance(data, dict):
        return {}
    return {str(k): [str(t) for t in v] for k, v in data.items()
            if isinstance(v, list) and v}

def _manifest_load() -> set:
    """读取本插件下载过的文件清单。"""
    data = _read_json(_MANIFEST_PATH, set())
    # 兼容两种历史格式：{"files": [...]} 与裸 list
    return set(data.get("files", [])) if isinstance(data, dict) else set(data)

def _manifest_save(paths: set) -> None:
    try:
        _atomic_write_json(_MANIFEST_PATH, {"files": sorted(paths)})
    except OSError as e:
        logger.warning(f"[baidu_netdisk] ⚠️ save cleanup manifest failed: {e}")

def _manifest_add(paths: list) -> None:
    """登记插件下载到本地的文件，供定时清理使用。

    只登记下载目录内的路径：清理会真删文件，越界路径（`..` 拼接、他人目录）一律拒绝。"""
    new = {p for p in paths if p}
    refused = {p for p in new if not _is_inside_download_dir(p)}
    if refused:
        logger.warning(f"[baidu_netdisk] ⚠️ 拒绝登记下载目录之外的路径: {sorted(refused)}")
        new -= refused
    if not new:
        return
    # 读-改-写整体串行（见 _write_json_locked）：否则并发下载会互相覆盖刚登记的清单
    # 落盘格式必须与 _manifest_save 一致（{"files": [...]}），否则 _manifest_load 读不回
    _write_json_locked(_MANIFEST_PATH, _MANIFEST_LOCK,
                       lambda _cur: {"files": sorted(_manifest_load() | new)}, "save cleanup manifest failed")

def _cleanup_local_dir() -> None:
    """清理本插件下载并登记过的本地文件（不会动下载目录里的其他文件）。"""
    # 全程持锁：删除过程中若有下载完成并登记新文件，会被这里的旧快照覆盖而丢失
    with _MANIFEST_LOCK:
        paths = _manifest_load()
        if not paths:
            return
        removed = 0
        remain = set()
        for p in paths:
            if not _is_inside_download_dir(p):
                # 早期版本或异常拼接可能留下越界条目：绝不删除，并从清单里剔除
                logger.warning(f"[baidu_netdisk] ⚠️ 清理清单含下载目录之外的路径，已忽略: {p}")
                continue
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed += 1
                elif os.path.exists(p):
                    remain.add(p)  # 目录等非普通文件，保留不动
            except OSError as e:
                logger.warning(f"[baidu_netdisk] ⚠️ cleanup skip {p}: {e}")
                remain.add(p)
        _manifest_save(remain)
    if removed:
        logger.info(f"[baidu_netdisk] local cleanup: removed {removed} plugin-downloaded files")

def _schedule_local_cleanup(hour: int) -> None:
    """安排每天定时清理本地下载文件（仅清理插件自己下载的文件）。"""
    if hour < 0 or hour > 23:
        logger.info("[baidu_netdisk] local auto-cleanup disabled")
        return
    if state.cleanup_thread_started:
        logger.info("[baidu_netdisk] local cleanup thread already running, skip")
        return
    state.cleanup_thread_started = True

    def _worker() -> None:
        while True:
            now = time.localtime()
            target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                  hour, 0, 0, 0, 0, -1))
            if target <= time.time():
                target += 86400  # 明天
            time.sleep(max(target - time.time(), 1))
            _cleanup_local_dir()
            time.sleep(60)

    threading.Thread(target=_worker, daemon=True, name="baidupan-cleanup").start()
    logger.info(f"[baidu_netdisk] scheduled local cleanup at {hour}:00 daily")

def _user_bucket(user_id: str) -> str:
    """用户标识 → 转存子目录名（稳定短哈希，不把 QQ 号等原始标识写进网盘目录名）。"""
    raw = str(user_id or "").strip()
    if not raw:
        return "ushared"
    return "u" + hashlib.blake2b(raw.encode("utf-8"), digest_size=6).hexdigest()

def _iter_file_stats(root: str) -> Iterator[tuple[str, os.stat_result]]:
    """遍历目录下所有文件，产出 (绝对路径, stat)，跳过遍历中消失/无权限的文件。

    三处「快照 / 收集 / 比对新增」重复了同一段 os.walk + OSError 容错，
    统一走这里；容错语义（单个文件 stat 失败不影响整次遍历）必须保持一致。"""
    for cur, _, names in os.walk(root):
        for n in names:
            p = os.path.join(cur, n)
            try:
                yield p, os.stat(p)
            except OSError:
                continue  # 遍历途中被删除或不可读，跳过该文件而非中断整次收集

def _path_in_bucket(dirpath: str, bucket: str) -> bool:
    """判断某个**目录**是否落在本次下载的隔离子目录（bucket）内；bucket 为空视为全部命中。

    统一反斜杠后按路径分量比较，因此 bucket 名恰好出现在文件名里不会造成误命中——
    所以调用方传目录，不要传文件路径。"""
    return not bucket or bucket in dirpath.replace("\\", "/").split("/")

def _snapshot_local_files(root: str) -> dict:
    """记录目录下每个文件的 (大小, mtime)，用于判断哪些文件是本次下载产生的。"""
    snap = {}
    if not root or not os.path.isdir(root):
        return snap
    for p, st in _iter_file_stats(root):
        snap[p] = (st.st_size, st.st_mtime_ns)
    return snap

def _collect_files_under(root: str) -> list:
    """收集目录下所有文件为 {path, name, size}。"""
    return [{"path": p, "name": os.path.basename(p), "size": st.st_size}
            for p, st in _iter_file_stats(root)]

def _collect_new_files(root: str, before: dict, bucket: str = "") -> list:
    """收集 root 下相对 before 快照「新增或内容有变化」的文件，即本次下载产生的文件。

    bucket 非空时只认该子目录下的文件：转存目录按用户隔离，bucket 未命中说明本地目录
    结构与预期不符，此时宁可返回空（上层报「未找到本次下载的文件」）也不能回退成
    「下载期间所有变化过的文件」——那正是并发下载时把他人文件当成自己的发出去的路径。"""
    matched, others = [], []
    for p, st in _iter_file_stats(root):
        if before.get(p) == (st.st_size, st.st_mtime_ns):
            continue  # 下载前已存在且大小/时间未变，不是本次产生的
        # 按「所在目录」分类而非文件路径：bucket 是目录名，用文件路径判断会让
        # 名字恰好等于 bucket 的文件被误算进本次结果
        target = matched if _path_in_bucket(os.path.dirname(p), bucket) else others
        target.append({"path": p, "name": os.path.basename(p), "size": st.st_size})
    if bucket:
        return matched
    return matched or others

def _validate_remote_path(path: str) -> str:
    """用户/远程给出的网盘路径能否安全用于本地路径拼接；返回错误描述，合法返回空串。

    网盘路径本身是规范化的，正常不会出现上一级引用（`..`）；而 Windows 上 `\\` 是路径
    分隔符，远程文件名里的 `\\` 会让本地镜像路径跑到下载目录之外——登记进清理清单就等于
    让定时任务真去删那个路径。"""
    if not path:
        return ""
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        return "路径包含上一级目录引用（..），已拒绝"
    if _IS_WINDOWS and "\\" in path:
        return "路径包含反斜杠，Windows 上无法安全下载该文件"
    return ""

def _is_inside_download_dir(path: str) -> bool:
    """路径是否落在下载目录内（`..` 与符号链接都先 realpath 归一化再比较）。"""
    if not state.download_dir or not path:
        return False
    try:
        root = os.path.realpath(state.download_dir)
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    except (OSError, ValueError):
        return False  # 不同盘符（Windows）等无法比较的情况一律视为越界

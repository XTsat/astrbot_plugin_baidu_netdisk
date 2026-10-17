"""核心层：下载执行、打包与下载完成后的本地文件核对。"""

import os
import queue
import subprocess
import time

from astrbot.api import AstrBotConfig, logger

from ..domain.constants import _BPCS_EXE_NAME, BPCS_PATH, DEFAULT_CLOUD_SAVE_DIR, _DOWNLOAD_HARD_CAP_SECONDS, _PARTIAL_GRACE_SECONDS, _PRECOMPRESSED_EXT
from ..domain.state import state
from ..utils.bpcs import _get_account_folder, _run_bpcs
from ..utils.fs import _collect_files_under, _collect_new_files, _is_inside_download_dir, _manifest_add, _path_in_bucket, _snapshot_local_files, _validate_remote_path
from ..utils.size import _parse_size_bytes, _size_matches
from .panapi import _parse_ls_line


def _config_int(config: AstrBotConfig, key: str, default: int,
                minimum: int | None = None, maximum: int | None = None) -> int:
    """读取整数配置：None/空串/非数字等非法值回退默认值，超出范围则收敛到边界。

    配置由用户在 WebUI 或配置文件中手填，非法值不应让插件加载失败、也不该在命令执行中途抛异常。"""
    raw = config.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"[baidu_netdisk] ⚠️ 配置 {key}={raw!r} 不是合法整数，已按默认值 {default} 处理")
        return default
    if minimum is not None and value < minimum:
        logger.warning(f"[baidu_netdisk] ⚠️ 配置 {key}={value} 小于允许的最小值 {minimum}，已按 {minimum} 处理")
        value = minimum
    if maximum is not None and value > maximum:
        logger.warning(f"[baidu_netdisk] ⚠️ 配置 {key}={value} 大于允许的最大值 {maximum}，已按 {maximum} 处理")
        value = maximum
    return value

def _config_bool(config: AstrBotConfig, key: str, default: bool) -> bool:
    """读取布尔配置，兼容字符串写法（"false"/"0"/"no"/"off" 视为 False）。

    直接写 bool("false") 会得到 True：配置文件被手工改成字符串时开关会静默反向。"""
    raw = config.get(key, default)
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        logger.warning(f"[baidu_netdisk] ⚠️ 配置 {key}={raw!r} 不是合法布尔值，已按默认值 {default} 处理")
        return default
    return bool(raw)

def _next_progress(prog_q: "queue.Queue", file_info: tuple | None) -> tuple[tuple | None, str | None]:
    """取出一条进度消息，返回 `(最新的 (文件名, 总大小), 待发送文案或 None)`。

    下载工作线程经 progress_queue 上报两类东西：`_info` 元组（文件名/总大小，
    供调用方自己算百分比）与普通字符串（含 `_error` 前缀的错误）。这里只负责
    取出与分类，不发送——命令路径用 yield、后台任务用 event.send，两条路径的
    发射方式不同，但取队列与分类逻辑必须一致。

    每轮只取一条（不一次取空）：调用方每轮还会算一次百分比并 sleep，取空会让
    进度提示的条数与它和队列消息的交错顺序都变样。"""
    try:
        msg = prog_q.get_nowait()
    except queue.Empty:
        return file_info, None  # 本轮没取到进度消息，继续轮询
    if isinstance(msg, tuple) and msg[0] == "_info":
        return (msg[1], msg[2]), None  # (文件名, 总大小)
    if isinstance(msg, tuple) and msg[0] == "_error":
        return file_info, f"❌ {msg[1]}"
    if isinstance(msg, str):
        return file_info, msg
    return file_info, None

def download_from_cloud(cloud_path: str, max_mb: int = 0, progress_queue: "queue.Queue" = None,
                        cloud_dir: str = "", bucket: str = "") -> dict:
    """从网盘下载指定路径的文件或文件夹（不转存，直接下载已有文件）。

    cloud_dir: 网盘目录，留空则用全局 state.cloud_save_dir（调用方通常传本用户的转存子目录）。
    bucket: 本次任务在网盘目录中的隔离子目录名，用于把下载结果精确归属到本次任务。
    progress_queue: 可选，传入 queue.Queue 后，下载过程中会向队列放入进度信息。
    """
    path_error = _validate_remote_path(cloud_path)
    if path_error:
        return {"error": path_error}
    cloud_dir = cloud_dir or state.cloud_save_dir or DEFAULT_CLOUD_SAVE_DIR
    if cloud_path:
        full_path = f"{cloud_dir}/{cloud_path.strip('/')}"
        file_name = full_path.rstrip("/").split("/")[-1]
    else:
        full_path = cloud_dir
        file_name = "全部文件"

    logger.info(f"[baidu_netdisk] download_from_cloud: cloud_path={cloud_path}, full_path={full_path}")

    is_dir = not cloud_path
    file_size = 0
    if cloud_path:
        # ls 查父目录判断文件/文件夹（bpcs ls -l 查文件本身不显示信息）
        norm = full_path.rstrip("/")
        parent = norm.rsplit("/", 1)[0] if "/" in norm else "/"
        out, _, code = _run_bpcs(["ls", "-l", parent], timeout=60)
        if code != 0 or "错误" in (out or "") or "不存在" in (out or ""):
            snippet = ((out or "").strip().splitlines() or ["(无输出)"])[0][:100]
            if code == -2:
                return {"error": f"{_BPCS_EXE_NAME} 不可用或无法执行（{(out or '')[:80]}）。"
                                 f"请先确保已通过 /pan pcs 下载工具"}
            if code == -1:
                return {"error": f"查询网盘目录超时: {cloud_path}"}
            return {"error": f"网盘中不存在该路径或查询失败: {cloud_path}（{snippet}）"}
        found = None
        for line in out.split("\n"):
            parsed = _parse_ls_line(line)
            if parsed and parsed[0] == file_name:
                found = parsed
                break
        if not found:
            return {"error": f"云端路径不存在: {cloud_path}"}
        is_dir = found[2]
        file_size = 0 if is_dir else _parse_size_bytes(found[1])
    logger.info(f"[baidu_netdisk] download_from_cloud: is_dir={is_dir}, file_size={file_size} ({file_size/1024/1024:.1f}MB)")

    # 大小限制（仅单文件）
    max_bytes = max_mb * 1024 * 1024
    if not is_dir and max_mb > 0 and file_size > max_bytes:
        return {"error": f"文件过大 ({file_size//1024//1024}MB) 超过 {max_mb}MB 限制"}

    # 下载前先检查本地是否已有该文件（避免 BPCS 卡在重复下载）
    # 注意先比「远程路径镜像」再比文件名：不同用户转存目录不同，
    # 只按文件名比对会把别人下载的同名文件误判成本文件，导致返回错误内容
    if not is_dir:
        for p in _local_candidates(full_path, file_name):
            if not os.path.exists(p):
                continue
            try:
                local_size = os.path.getsize(p)
                local_mtime = os.path.getmtime(p)
            except OSError:
                continue  # 恰好在检查期间被清理掉：当作本地没有，继续走下载
            # 只认「大小与云端一致」的本地文件：上次下载超时被强杀会留下半截文件，
            # 无脑复用等于把截断的文件当成下载成功发出去（它也不会出现在清理清单里）
            if file_size > 0:
                if not _size_matches(local_size, file_size):
                    logger.warning(f"[baidu_netdisk] ⚠️ 本地同名文件大小与云端不一致（疑似残留半截文件），忽略: {p}")
                    continue
            elif local_size == 0 or time.time() - local_mtime < _PARTIAL_GRACE_SECONDS:
                # 云端大小未知时没有参照可比，只能排除「刚被写过」的：那种多半是上一次
                # 被强杀的下载留下的半截文件（清理清单里也可能还没有它）。宁可让 BPCS 重下
                logger.warning(f"[baidu_netdisk] ⚠️ 云端大小未知且本地同名文件刚被写过（疑似残留），忽略: {p}")
                continue
            logger.info(f"[baidu_netdisk] file already exists locally: {p}")
            return {"path": p, "name": file_name, "size": local_size, "is_dir": False}

    # 下载（使用 subprocess.Popen 实时读输出，避免 BPCS 卡住）
    _run_bpcs(["config", "set", "-savedir", state.download_dir], timeout=15)
    # 超时语义与配置一致：正数=固定超时；-1（默认，不超时）不按体积设上限，
    # 但仍受兜底上限保护，避免 BaiduPCS-Go 卡死时进程与下载通道被永久占用
    if state.download_timeout_seconds > 0:
        dl_timeout = state.download_timeout_seconds
    elif is_dir or file_size <= 0:
        # 文件夹/大小未知：无法按体积估算（原先固定 3600 秒，大文件夹会被中途砍掉）
        dl_timeout = _DOWNLOAD_HARD_CAP_SECONDS
    else:
        # 单文件按体积保守估算（约合 1/15 MB/s 的下限速率），并与兜底上限取小
        dl_timeout = min(_DOWNLOAD_HARD_CAP_SECONDS,
                         max(600, int(file_size / (1024 * 1024) * 15)))
    logger.info(f"[baidu_netdisk] download_from_cloud: starting download, dl_timeout={dl_timeout}s, progress_queue={progress_queue is not None}")
    # 把文件信息塞进队列，给 handler 监控文件大小用
    # 如果 file_size 为0但文件非目录，把 0 也塞进去，让 handler 监控时用实际文件大小
    if progress_queue is not None and not is_dir:
        progress_queue.put(("_info", file_name, file_size))
    # 文件夹下载前先记录本地已有文件，下载完成后据此精确归属「本次产生」的文件
    before_files = _snapshot_local_files(state.download_dir) if is_dir else {}
    dl_started_at = time.time()
    try:
        proc = subprocess.Popen(
            [BPCS_PATH, "download", full_path, "--ow"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE
        )
        # 预写一个 y 应答（防个别 BPCS 版本仍弹覆盖确认），但**不要手动 close stdin**：
        # Python 3.12 的 communicate() 会对已关闭的 stdin 做内部 flush，
        # 抛 ValueError「flush of closed file」导致每次下载都失败。
        # stdin 的关闭交给 communicate() 统一处理（写入结束后子进程自然读到 EOF）。
        try:
            proc.stdin.write(b"y\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # 进程可能已退出，communicate 会给出结果
        if dl_timeout > 0:
            out = proc.communicate(timeout=dl_timeout)[0]
        else:
            out = proc.communicate()[0]
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        code = proc.returncode
    except subprocess.TimeoutExpired:
        logger.error(f"[baidu_netdisk] ❌ download timeout ({dl_timeout}s), killing process")
        # 超时后必须 kill，否则 BPCS 会继续在后台下载
        proc.kill()
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass  # 已 kill 过，收尾失败不影响返回「下载超时」
        # 强杀会留下半截文件：登记进清理清单，避免永久占盘（大小校验会把它排除在复用之外）
        _register_partial_download(full_path, file_name, is_dir, file_size, dl_started_at, bucket)
        if progress_queue is not None:
            progress_queue.put(("_error", "下载超时"))
        return {"error": "下载超时"}
    except Exception as e:
        logger.error(f"[baidu_netdisk] ❌ download subprocess error: {e}")
        return {"error": f"下载进程异常: {e}"}
    if code != 0 or "失败" in (out or "") or "错误" in (out or ""):
        logger.error(f"[baidu_netdisk] ❌ download failed: code={code}, out={(out or '')[:200]}")
        if progress_queue is not None:
            progress_queue.put(("_error", f"下载失败 (code={code})"))
        return {"error": "下载失败"}
    logger.info(f"[baidu_netdisk] download_from_cloud: download completed, code={code}")

    if not state.download_dir:
        return {"error": "下载目录未设置"}

    logger.info(f"[baidu_netdisk] download_from_cloud: searching for downloaded file, DOWNLOAD_DIR={state.download_dir}")
    account_folder = _get_account_folder()
    if is_dir:
        # 优先按「远程路径镜像」定位本地目录（BPCS 会把网盘路径建在 savedir/<账号>/ 下），
        # 目录不存在时退回「下载前后快照差异」；两者都不命中就报错，
        # 不再退回「收集下载目录全部文件」（那会把历史文件与他人的文件一起发出去）
        downloaded = []
        mirror_dir = (os.path.join(state.download_dir, account_folder, full_path.strip("/"))
                      if account_folder else "")
        if mirror_dir and os.path.isdir(mirror_dir):
            downloaded = _collect_files_under(mirror_dir)
        if not downloaded:
            downloaded = _collect_new_files(state.download_dir, before_files, bucket)
        if not downloaded:
            return {"error": "文件夹下载完成但未找到本次下载的文件"}
        _manifest_add([d["path"] for d in downloaded])
        if state.auto_delete_cloud:
            _run_bpcs(["rm", full_path], timeout=30)
        return {"path": state.download_dir, "name": file_name, "size": 0, "is_dir": True, "files": downloaded}
    else:
        # 先找账号专属文件夹（BPCS 会按 uid_用户名 创建子目录），并优先按远程路径镜像定位
        search_dirs = []
        if account_folder:
            search_dirs.append(os.path.join(state.download_dir, account_folder, full_path.strip("/")))
            search_dirs.append(os.path.join(state.download_dir, account_folder, file_name))
        search_dirs.append(os.path.join(state.download_dir, file_name))
        local_path = None
        mismatched: list[str] = []  # 同名但大小明显不符（截断）的候选，最后统一处理
        for p in search_dirs:
            if not os.path.exists(p):
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if _size_matches(size, file_size):
                local_path = p
                break
            mismatched.append(p)
        if not local_path and bucket:
            # 兜底一：同名文件只认本次任务转存子目录里的那个
            for root, _, files in os.walk(state.download_dir):
                if _path_in_bucket(root, bucket) and file_name in files:
                    p = os.path.join(root, file_name)
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    if _size_matches(size, file_size):
                        local_path = p
                        break
                    mismatched.append(p)
        if not local_path:
            # 兜底二：目录结构与预期不符时，只认本次下载开始之后新出现的同名文件。
            # 单纯按文件名找会把别人下载的同名文件发出去，所以用创建/修改时间卡住下载起点；
            # 宁可报「未找到文件」，也不把不确定归属的文件发出去
            newest = None
            for root, _, files in os.walk(state.download_dir):
                if file_name not in files:
                    continue
                p = os.path.join(root, file_name)
                try:
                    fresh = max(os.path.getctime(p), os.path.getmtime(p))
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if fresh < dl_started_at:
                    continue
                if not _size_matches(size, file_size):
                    mismatched.append(p)
                    continue
                if newest is None or fresh > newest[0]:
                    newest = (fresh, p)
            if newest:
                local_path = newest[1]
        if not local_path:
            # 本次下载期间写出来的截断候选才登记清理，避免把别人早先下好的同名文件一起删掉。
            # 兜底二要遍历整个下载目录，会扫到别人 bucket 下的同名文件（大小通常与云端不符），
            # 而登记即意味着定时清理会真删，所以只认「远程路径镜像/本用户文件名候选」或路径里
            # 带本用户 bucket 的候选；同一路径可能同时命中多个候选，去重后再登记
            ours = {p.replace("\\", "/") for p in _local_candidates(full_path, file_name)}
            partials: list[str] = []
            for p in mismatched:
                if p in partials:
                    continue
                norm = p.replace("\\", "/")
                if norm not in ours and not (bucket and bucket in norm.split("/")):
                    continue
                try:
                    if os.path.getmtime(p) >= dl_started_at:
                        partials.append(p)
                except OSError:
                    continue
            if partials:
                _manifest_add(partials)
                logger.warning(f"[baidu_netdisk] ⚠️ 下载结果大小与云端不符，已登记清理: {partials}")
                # 把截断的文件当成功发出去比报错更糟：用户会以为拿到了完整内容
                return {"error": "下载完成但文件不完整（大小与云端不符），已放弃发送"}
            return {"error": "下载完成但未找到文件"}
        _manifest_add([local_path])
        if state.auto_delete_cloud:
            _run_bpcs(["rm", full_path], timeout=30)
        try:
            local_size = os.path.getsize(local_path)
        except OSError:
            local_size = file_size  # 刚查到就被移走：回退云端体积，不让整个下载判失败
        return {"path": local_path, "size": local_size, "name": file_name}

def _zip_files(files: list, zip_path: str, top_name: str = "") -> dict:
    """把已下载的文件列表打包为 zip（QQ 等平台不能直接发文件夹，打包后当单文件发送）。
    files: [{"path", "name", "size"}]；zip 内保留相对目录结构，顶层优先用 top_name。
    压缩方式按文件类型自适应：已压缩格式（视频/图片/音频/压缩包/Office）用 STORED 直接拼装（快），
    其余文本类用 DEFLATE。返回 {"path", "name", "size", "stored", "deflated"} 或 {"error": ...}。"""
    import zipfile as _zf
    paths = [f.get("path") for f in files if f.get("path") and os.path.isfile(f["path"])]
    if not paths:
        return {"error": "未找到可打包的文件"}
    stored = deflated = 0
    try:
        os.makedirs(os.path.dirname(zip_path), exist_ok=True)
        common = os.path.commonpath(paths) if len(paths) > 1 else os.path.dirname(paths[0])
        with _zf.ZipFile(zip_path, "w", allowZip64=True) as zf:
            for fp in paths:
                rel = os.path.relpath(fp, common)
                # zip 内统一保留下载文件夹这一层（解压即得同名文件夹），但不重复套同名目录
                if top_name and rel != top_name and not rel.startswith(top_name + os.sep):
                    rel = f"{top_name}/{rel}"
                if os.path.splitext(fp)[1].lower() in _PRECOMPRESSED_EXT:
                    zf.write(fp, rel, compress_type=_zf.ZIP_STORED)
                    stored += 1
                else:
                    zf.write(fp, rel, compress_type=_zf.ZIP_DEFLATED)
                    deflated += 1
        return {"path": zip_path, "name": os.path.basename(zip_path),
                "size": os.path.getsize(zip_path), "stored": stored, "deflated": deflated}
    except Exception as e:
        return {"error": f"打包失败: {e}"}

def _get_local_file_size(file_name: str) -> int:
    """在 state.download_dir 下查找文件并返回大小，找不到返回 0。"""
    account_folder = _get_account_folder()
    check_paths = []
    if account_folder:
        check_paths.append(os.path.join(state.download_dir, account_folder, file_name))
    check_paths.append(os.path.join(state.download_dir, file_name))
    for p in check_paths:
        if os.path.exists(p):
            try:
                return os.path.getsize(p)
            except OSError:
                return 0
    return 0

def _local_candidates(full_path: str, file_name: str) -> list[str]:
    """该文件下载后可能落地到本地的路径（BPCS 按 <savedir>/<账号>/<网盘路径> 镜像存放）。

    按「远程路径镜像」优先、文件名其次排序：不同用户的转存目录不同，
    只按文件名比对会把别人下载的同名文件误判成本文件。
    越界（`..`、绝对路径）的候选直接丢弃：它们可能指向下载目录之外的文件。"""
    candidates: list[str] = []
    account_folder = _get_account_folder()
    if account_folder:
        candidates.append(os.path.join(state.download_dir, account_folder, full_path.strip("/")))
        candidates.append(os.path.join(state.download_dir, account_folder, file_name))
    candidates.append(os.path.join(state.download_dir, file_name))
    return [p for p in candidates if _is_inside_download_dir(p)]

def _register_partial_download(full_path: str, file_name: str, is_dir: bool,
                               file_size: int, started_at: float, bucket: str = "") -> None:
    """把下载超时被强杀后残留的半截文件登记进清理清单。

    半截文件不满足「大小与云端一致」，因此不会被「本地已存在」的快路径复用（那段代码
    会跳过它），但也不会走成功路径登记，不处理就会永远占着磁盘。这里只登记「本次下载
    期间被写入过」的文件，避免把别人早先下载好的完整文件算成残留。"""
    paths: list[str] = []
    if is_dir:
        account_folder = _get_account_folder()
        mirror_dir = (os.path.join(state.download_dir, account_folder, full_path.strip("/"))
                      if account_folder else "")
        candidates: list[str] = []
        if mirror_dir and os.path.isdir(mirror_dir):
            candidates = [item["path"] for item in _collect_files_under(mirror_dir)]
        elif bucket:
            # 账号目录未知（未登录 / 读不到 BPCS 配置）时按「远程路径镜像」定位不了：退回整个
            # 下载目录，但只认本用户 bucket 下的文件，免得把别人正在下载的文件登记成残留
            for root, _, files in os.walk(state.download_dir):
                if not _path_in_bucket(root, bucket):
                    continue
                candidates.extend(os.path.join(root, name) for name in files)
        for p in candidates:
            try:
                if os.path.getmtime(p) >= started_at:
                    paths.append(p)
            except OSError:
                continue
    else:
        for p in _local_candidates(full_path, file_name):
            try:
                if not os.path.isfile(p) or os.path.getmtime(p) < started_at:
                    continue
                # 大小未知（file_size<=0）时没法比对，只能按「本次下载期间被写过」认定残留：
                # 宁可让用户重下一次，也不能把截断文件当完整内容发出去
                if file_size > 0 and _size_matches(os.path.getsize(p), file_size):
                    continue
                paths.append(p)
            except OSError:
                continue
    if paths:
        _manifest_add(paths)
        logger.warning(f"[baidu_netdisk] ⚠️ 已登记 {len(paths)} 个超时残留文件，将在定时清理时删除")

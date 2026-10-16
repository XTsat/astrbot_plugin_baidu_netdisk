# -*- coding: utf-8 -*-
"""
AstrBot Plugin: Baidu Netdisk Share Downloader
Download files from Baidu Netdisk share links using BaiduPCS-Go,
then send via AstrBot's platform adapters.
"""
import asyncio
import hashlib
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import json as _json
import requests as _req

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Star, Context, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.star.filter.command import GreedyStr

try:
    from astrbot.api.event import MessageChain
except ImportError:  # 兼容旧版 AstrBot
    from astrbot.core.message.message_event_result import MessageChain

# 平台检测：BaiduPCS-Go 按当前系统下载对应平台的二进制（Windows: exe；Linux/macOS: 无扩展名可执行文件）
_IS_WINDOWS = sys.platform.startswith("win")
_IS_MACOS = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")
_BPCS_EXE_NAME = "BaiduPCS-Go.exe" if _IS_WINDOWS else "BaiduPCS-Go"
BPCS_PATH = os.path.join(os.path.dirname(__file__), _BPCS_EXE_NAME)
# BaiduPCS-Go 官方 release 信息：始终拉取 GitHub 最新 release（不固定版本号/资产名，下载 URL 与大小由 API 实时提供）
BPCS_REPO = "qjfoidnh/BaiduPCS-Go"
BPCS_API_URL = f"https://api.github.com/repos/{BPCS_REPO}/releases/latest"


def _bpcs_asset_suffix() -> str | None:
    """当前平台对应的 GitHub release 资产后缀（如 linux-amd64 / windows-x64 / darwin-osx-arm64）。
    不支持的平台返回 None。"""
    machine = platform.machine().lower()
    if _IS_WINDOWS:
        if machine in ("amd64", "x86_64"):
            return "windows-x64"
        if machine in ("arm64", "aarch64"):
            return "windows-arm64"
        return "windows-x86"  # 32 位 x86 兜底
    if _IS_LINUX:
        if machine in ("amd64", "x86_64"):
            return "linux-amd64"
        if machine in ("arm64", "aarch64"):
            return "linux-arm64"
        if machine.startswith("armv7") or machine.startswith("arm"):
            return "linux-arm"
        return None
    if _IS_MACOS:
        if machine in ("amd64", "x86_64"):
            return "darwin-osx-amd64"
        if machine in ("arm64", "aarch64"):
            return "darwin-osx-arm64"
        return None
    return None


def _sanity_check_binary(data: bytes, what: str = "下载的文件") -> str | None:
    """按平台校验可执行文件格式（Windows: PE/MZ；macOS: Mach-O；Linux: ELF），
    避免把错误页/损坏文件写成本地二进制。合法返回 None，非法返回错误描述。"""
    if not data or len(data) < 4:
        return f"{what}过小（{len(data)} 字节），疑似损坏"
    if _IS_WINDOWS:
        if data[:2] != b"MZ":
            return f"{what}不是有效的 Windows PE 程序"
        return None
    if _IS_MACOS:
        if data[:4] not in (
            b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",  # Mach-O 32/64 位（大端）
            b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe",  # Mach-O 64 位（小端）/ Universal
            b"\xca\xfe\xba\xbf",
        ):
            return f"{what}不是有效的 macOS 可执行文件"
        return None
    if data[:4] != b"\x7fELF":
        return f"{what}不是有效的 Linux ELF 程序"
    return None


def _resolve_plugin_data_dir() -> str:
    """解析 AstrBot 的 plugin-data 目录（data/plugin_data/<插件目录名>/）。
    优先走官方 StarTools.get_data_dir()；兼容旧版 AstrBot 时回退到 astrbot_path 推导；
    极端情况下回退到插件目录下的 plugin_data/。"""
    plugin_dir_name = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
    try:
        from astrbot.core.star.star_tools import StarTools
        return str(StarTools.get_data_dir(plugin_dir_name))
    except Exception:
        pass
    try:
        import astrbot.core.utils.astrbot_path as _ap
        d = os.path.join(_ap.get_astrbot_plugin_data_path(), plugin_dir_name)
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        pass
    d = os.path.join(os.path.dirname(__file__), "plugin_data")
    os.makedirs(d, exist_ok=True)
    return d


# 插件运行时数据（下载目录、下载清单、登录二维码）统一放 AstrBot 的 plugin-data
# 目录（升级/重装插件本体不丢数据，卸载插件时 AstrBot 会连同清理）；
# BaiduPCS-Go 二进制保留在插件目录，登录凭证 pcs_config.json 沿用 BaiduPCS-Go 默认位置
# （Windows: %APPDATA%/BaiduPCS-Go，Linux/macOS: ~/.config/BaiduPCS-Go，支持
#  BAIDUPCS_GO_CONFIG_DIR 环境变量覆盖；完整解析逻辑见 _bpcs_config_path）
PLUGIN_DATA_DIR = _resolve_plugin_data_dir()


DOWNLOAD_DIR = None  # 初始化时根据配置设置
CLOUD_SAVE_DIR = None  # 网盘转存目录（各用户的实际转存目录是它的子目录，见 _cloud_dir_for）
DEFAULT_CLOUD_SAVE_DIR = "/我的资源/AutoTransfer"
FLASH_TASK_LIMIT_MB = 200
_ZIP_MAX_MB_DEFAULT = 10240  # 文件夹打包上限默认值（MB，10GB 对应 QQ 闪传大文件通道）；可在配置中修改，0=不限制
_SEND_FILES_THRESHOLD_DEFAULT = 3  # 文件夹内文件数 ≤ 此值时逐个发送（原始文件更易命中秒传），超过则打包 zip
DOWNLOAD_TIMEOUT_SECONDS = -1  # 下载超时秒数，-1 = 不超时（可在配置中修改）

# 本插件下载过的本地文件清单，定时清理时只删这些，不动下载目录里的其他文件
_MANIFEST_PATH = os.path.join(PLUGIN_DATA_DIR, "cleanup_manifest.json")

_bpcs_inited = False
_bpcs_lock = threading.Lock()
_auto_delete_cloud = True
_local_cleanup_hour = 3
# 模拟真实浏览器的请求头，降低被百度风控识别为自动化流量的概率
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_exe_ok_cache = {"sig": None}  # 已验证可用的 exe 的 (mtime, size)，避免每次请求都 spawn 进程
_cleanup_thread_started = False
_share_switch_lock = threading.Lock()  # 串行化“切换分享链接”（rm 旧转存 + 转存新链接 + 更新缓存）
_auto_dl_lock = threading.Lock()  # 串行化登录时的自动下载（同一时间只允许一个下载任务）
_auto_dl_failed = False  # 自动下载失败缓存标记：避免 exe 缺失且网络不通时每次登录都长时间重试；/pan pcs 会重置
_auto_dl_last_error = ""  # 最近一次自动下载失败的具体原因（直连与各镜像的错误），用于给用户可诊断的失败提示
# 已知的 GitHub 加速镜像：既是 /pan pcs 可填的加速源白名单，也是「未配置代理时自动降级」的探测候选。
# 加速源会被拼在请求 URL 前面，若允许任意地址，等于把「从哪下载二进制」交给填地址的人，
# 因此限定 host 必须是下列域名（或其子域）；管理员自建镜像用环境变量追加。
# 顺序按实测可用性排列（能代理 api.github.com 的排最前，直连 API 不通时靠它们取 release 信息）。
_GITHUB_MIRROR_HOSTS = (
    "gh-proxy.com", "gh-proxy.org", "ghfast.top", "ghproxy.net",
    "ghproxy.com", "ghproxy.cc", "ghproxy.link",
    "gh-proxy.net", "ghps.cc", "gh.llkk.cc",
    "gitmirror.com", "hub.gitmirror.com",
)
_auto_mirror = ""  # 探测成功的加速镜像（仅进程内缓存，重启后重新探测）


def _allowed_proxy_hosts() -> tuple:
    """允许的加速源域名（内置镜像列表 + BAIDU_NETDISK_EXTRA_PROXIES 追加，英文逗号分隔）。"""
    extra = os.environ.get("BAIDU_NETDISK_EXTRA_PROXIES", "")
    hosts = tuple(h.strip().lower() for h in extra.split(",") if h.strip())
    return _GITHUB_MIRROR_HOSTS + hosts


def _validate_github_proxy(proxy: str) -> str | None:
    """校验加速代理地址是否属于已知的 GitHub 加速源。合法返回 None，否则返回错误描述。"""
    proxy = (proxy or "").strip()
    if not proxy.lower().startswith(("http://", "https://")):
        return "代理地址必须以 http:// 或 https:// 开头"
    try:
        parts = urllib.parse.urlsplit(proxy)
    except ValueError:
        return "代理地址格式不正确"
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return "代理地址格式不正确"
    for allowed in _allowed_proxy_hosts():
        if host == allowed or host.endswith("." + allowed):
            return None
    return ("代理只能填 GitHub 加速源，当前地址不在白名单内。可用: "
            + "、".join(_allowed_proxy_hosts()))


def _mirror_candidates() -> tuple:
    """按优先级返回待探测的加速镜像地址（上次探测成功的排最前，避免每次下载都从头探测）。"""
    hosts = list(_GITHUB_MIRROR_HOSTS)
    if _auto_mirror:
        cached = (urllib.parse.urlsplit(_auto_mirror).hostname or "").lower()
        if cached in hosts:
            hosts.remove(cached)
            hosts.insert(0, cached)
    return tuple(f"https://{h}" for h in hosts)


# GitHub 加速代理（gh-proxy 类）：/pan pcs <代理地址> 保存后，下载与登录时自动下载都走该代理
_GITHUB_PROXY_FILE = os.path.join(PLUGIN_DATA_DIR, "github_proxy.txt")
_github_proxy = ""
try:
    _github_proxy = (open(_GITHUB_PROXY_FILE, "r", encoding="utf-8").read() or "").strip()
except Exception:
    pass
# 已保存的地址同样要过白名单：防止旧版本/手工改文件留下任意代理
if _github_proxy and _validate_github_proxy(_github_proxy):
    logger.warning(f"[baidu_netdisk] 已保存的加速代理不在白名单内，已忽略: {_github_proxy}")
    _github_proxy = ""


def _ensure_bpcs_exe() -> bool:
    """检查 BaiduPCS-Go 二进制是否存在且可运行（Windows 为 .exe，Linux/macOS 无扩展名）。
    不存在或损坏时返回 False。结果按文件签名缓存。"""
    try:
        st = os.stat(BPCS_PATH)
    except OSError:
        return False
    sig = (st.st_mtime_ns, st.st_size)
    if _exe_ok_cache["sig"] == sig:
        return True
    try:
        r = subprocess.run([BPCS_PATH, "--version"], capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0
    except Exception:
        ok = False
    if ok:  # 只缓存成功结果，失败时下次重试
        _exe_ok_cache["sig"] = sig
    return ok


def _auto_download_bpcs() -> bool:
    """确保 BaiduPCS-Go 可用：缺失或损坏时自动下载（GitHub 官方 release 优先，直连失败时自动挑选可用加速镜像）。
    并发安全（同一时间只允许一个下载任务）；下载失败会缓存标记，避免每次登录都长时间重试；
    失败原因记录到 _auto_dl_last_error 供用户侧提示。"""
    global _auto_dl_failed, _auto_dl_last_error
    if _ensure_bpcs_exe():
        return True
    if _auto_dl_failed:
        logger.warning("[baidu_netdisk] BaiduPCS-Go 不可用且此前自动下载失败，跳过自动下载（可用 /pan pcs 手动重试）")
        return False
    with _auto_dl_lock:
        if _ensure_bpcs_exe():
            return True
        if _auto_dl_failed:
            return False
        logger.info("[baidu_netdisk] BaiduPCS-Go 不存在或不可用，开始自动下载...")
        result = _download_github_exe()
        if "error" in result:
            _auto_dl_failed = True
            _auto_dl_last_error = result["error"]
            logger.error(f"[baidu_netdisk] BaiduPCS-Go 自动下载失败: {_auto_dl_last_error}")
            return False
        if result.get("via"):
            logger.info(f"[baidu_netdisk] BaiduPCS-Go 已通过加速镜像 {result['via']} 下载完成")
        _auto_dl_failed = False
        _auto_dl_last_error = ""
        return _ensure_bpcs_exe()


def _proxied_url(proxy: str, url: str) -> str:
    """给 GitHub 请求地址加上加速代理前缀（gh-proxy 类代理：代理地址 + 原始完整 URL）。proxy 为空时原样返回。"""
    if not proxy:
        return url
    return f"{proxy.rstrip('/')}/{url}"


def _set_github_proxy(proxy: str) -> str | None:
    """校验并持久化 GitHub 加速代理（供 /pan pcs <代理地址> 使用）。
    只接受白名单内的加速源（详见 _validate_github_proxy）。
    成功返回 None，地址非法返回错误描述；写文件失败只记日志（本次会话仍生效）。"""
    global _github_proxy
    proxy = (proxy or "").strip().rstrip("/")
    err = _validate_github_proxy(proxy)
    if err:
        return err
    _github_proxy = proxy
    try:
        os.makedirs(PLUGIN_DATA_DIR, exist_ok=True)
        with open(_GITHUB_PROXY_FILE, "w", encoding="utf-8") as f:
            f.write(proxy)
    except Exception as e:
        logger.warning(f"[baidu_netdisk] 保存 GitHub 加速代理失败（{e}），本次会话内仍生效")
    return None


def _fetch_release_info(proxy: str = "", timeout: int = 30) -> dict | None:
    """从 GitHub API 获取官方 release 信息（下载 URL 和文件大小来自官方，不硬编码）。
    proxy 非空时通过加速代理访问 API（如 https://gh-proxy.org/https://api.github.com/...）。"""
    headers = {"User-Agent": "astrbot_plugin_baidu_netdisk", "Accept": "application/vnd.github+json"}
    api_url = _proxied_url(proxy, BPCS_API_URL)
    try:
        resp = _req.get(api_url, timeout=timeout, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except _req.exceptions.SSLError:
        import urllib3 as _u3
        _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
        try:
            resp = _req.get(api_url, timeout=timeout, verify=False, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"[baidu_netdisk] 获取 GitHub release 信息失败: {e}")
            return None
    except Exception as e:
        logger.error(f"[baidu_netdisk] 获取 GitHub release 信息失败: {e}")
        return None


def _probe_mirror(url: str, timeout: int = 10) -> bool:
    """试探加速镜像能否代理该资产：只读响应前 4 字节，能连上且返回压缩包/可执行文件头才算可用。
    连接失败与「镜像返回 HTML 错误页」都在这里挡掉，避免为不可用的镜像白下整包。"""
    try:
        r = _req.get(url, timeout=timeout, stream=True,
                     headers={"User-Agent": "astrbot_plugin_baidu_netdisk"})
        try:
            if r.status_code not in (200, 206):
                return False
            head = next(r.iter_content(4), b"")
        finally:
            r.close()
    except Exception:
        return False
    return head[:2] in (b"PK", b"MZ")


def _download_asset(via: str, asset_url: str, tag: str, suffix: str,
                    expected_size: int, max_attempts: int) -> str:
    """下载 release 资产、解压出当前平台的可执行文件并写入本地（via 为空表示直连 GitHub）。
    成功返回空串，失败返回错误描述。"""
    import zipfile as _zip, io as _io

    def _get(url: str, timeout: int = 300) -> bytes | None:
        headers = {"User-Agent": "astrbot_plugin_baidu_netdisk"}
        # 连接超时 10s、读取超时 300s：直连被墙时 10s 内就让位给加速镜像，慢速大文件仍有足够读取时间
        timeouts = (10, timeout)
        try:
            r = _req.get(url, timeout=timeouts, headers=headers)
            r.raise_for_status()
            return r.content
        except _req.exceptions.SSLError:
            import urllib3 as _u3
            _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
            try:
                r = _req.get(url, timeout=timeouts, verify=False, headers=headers)
                r.raise_for_status()
                return r.content
            except Exception:
                return None
        except Exception:
            return None

    download_url = _proxied_url(via, asset_url)
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            time.sleep(1.0 * (attempt - 1))  # 1s/2s 退避，给瞬时故障留恢复时间
        where = f"加速镜像 {via} " if via else "GitHub 官方 release "
        logger.info(f"[baidu_netdisk] 从{where}下载 BaiduPCS-Go {tag}（{suffix}，第 {attempt}/{max_attempts} 次尝试）...")
        data = _get(download_url, timeout=300)
        if data is None:
            last_err = "下载失败（连接超时/被重置）"
            continue
        if len(data) != expected_size:
            last_err = f"下载大小不匹配: 官方API={expected_size}, 实际={len(data)}"
            continue
        try:
            with _zip.ZipFile(_io.BytesIO(data)) as zf:
                files = [n for n in zf.namelist() if not n.endswith("/")]
                exe_name = next((n for n in files if os.path.basename(n).lower() == _BPCS_EXE_NAME.lower()), None)
                if not exe_name:
                    if _IS_WINDOWS:
                        exe_name = next((n for n in files if n.lower().endswith(".exe")), None)
                    else:
                        exe_name = next(
                            (n for n in files
                             if os.path.basename(n).lower().startswith("baidupcs-go")
                             and not os.path.basename(n).lower().endswith((".md", ".txt", ".yml", ".yaml", ".json", ".log"))),
                            None,
                        )
                if not exe_name:
                    last_err = f"压缩包内未找到 {_BPCS_EXE_NAME}"
                    continue
                exe_data = zf.read(exe_name)
        except Exception as e:
            last_err = f"解压失败: {e}"
            continue
        # 不做哈希校验，仅按平台校验可执行文件格式，避免把损坏文件/错误页写成本地二进制
        err = _sanity_check_binary(exe_data, "解压出的文件")
        if err:
            last_err = err
            continue
        try:
            with open(BPCS_PATH, "wb") as f:
                f.write(exe_data)
            if not _IS_WINDOWS:
                os.chmod(BPCS_PATH, 0o755)  # zip 不保留可执行位，POSIX 需手动加上
            logger.info(f"[baidu_netdisk] BaiduPCS-Go 下载成功（{len(exe_data)} bytes, {tag}, {suffix}）")
            return ""
        except Exception as e:
            return f"写入 exe 失败: {e}"  # 写盘失败重试无意义，直接返回
    return last_err or "下载失败，请检查网络后重试"


def _download_github_exe(proxy: str | None = None, max_attempts: int = 3,
                         allow_mirrors: bool = True) -> dict:
    """从 GitHub 官方 release 下载当前平台对应的最新版 BaiduPCS-Go（`/releases/latest`，不固定版本）。
    国内网络下 GitHub 下载经常瞬时失败（超时/连接重置/403），对可重试的失败自动重试，最多 max_attempts 次。
    指定 proxy（或存在已保存的代理）时只走该加速源；未配置代理时先直连 GitHub，直连失败后
    依次探测候选加速镜像（见 _GITHUB_MIRROR_HOSTS）并改用可用的那个，各平台通用。
    返回 {"ok": True, "via": 镜像} 或 {"error": "..."}；allow_mirrors=False 表示强制直连不走镜像。"""
    global _auto_mirror

    if proxy is None:
        proxy = _github_proxy
    if proxy:
        proxy_err = _validate_github_proxy(proxy)
        if proxy_err:
            return {"error": proxy_err}
    mirrors = _mirror_candidates() if (allow_mirrors and not proxy) else ()

    suffix = _bpcs_asset_suffix()
    if not suffix:
        return {"error": f"当前平台（{sys.platform}/{platform.machine()}）暂不支持自动下载 BaiduPCS-Go，请手动获取对应平台的二进制"}

    info = _fetch_release_info(proxy)
    direct_api_ok = bool(info)
    if not info and mirrors:
        # 直连 API 不通时让能代理 api.github.com 的镜像提供 release 信息（资产仍优先直连）
        for mirror in mirrors:
            info = _fetch_release_info(mirror, timeout=10)
            if info:
                logger.info(f"[baidu_netdisk] GitHub API 直连不可用，改用加速镜像 {mirror} 获取 release 信息")
                break
    if not info:
        return {"error": "GitHub 官方 release 获取失败，请检查网络后重试"}
    tag = info.get("tag_name", "latest")
    # 资产名含版本号（如 BaiduPCS-Go-v4.0.2-linux-amd64.zip），按命名规则 + 平台后缀动态匹配，不硬编码
    asset = None
    for a in info.get("assets", []):
        name = a.get("name", "")
        if name.startswith("BaiduPCS-Go-") and name.endswith(f"-{suffix}.zip"):
            asset = a
            break
    if not asset:
        names = [a.get("name", "") for a in info.get("assets", [])]
        return {"error": f"未在 release {tag} 中找到 {suffix} 的 zip 资产（现有: {names or '无'}）"}

    # API 都是靠镜像拿到的，说明直连基本不通，直连下载只试一次即可，省得白等重试
    routes = [(proxy, max_attempts if direct_api_ok else 1, False)]
    routes += [(m, 1, True) for m in mirrors]
    errors = []
    for via, attempts, need_probe in routes:
        if need_probe:
            # 先用几字节探针确认镜像真能代理该资产，再决定是否整包下载（不可用的直接跳过）
            if not _probe_mirror(_proxied_url(via, asset["browser_download_url"])):
                logger.info(f"[baidu_netdisk] 加速镜像 {via} 探测未通过，尝试下一个")
                errors.append(f"{via}: 不可用")
                continue
            logger.info(f"[baidu_netdisk] 直连下载失败，改用加速镜像 {via}")
        err = _download_asset(via, asset["browser_download_url"], tag, suffix, asset["size"], attempts)
        if not err:
            if need_probe:
                _auto_mirror = via  # 记住可用镜像，本次运行后续下载优先复用
            return {"ok": True, "via": via}
        errors.append(f"{'直连' if not via else via}: {err}")
    # 直连错误最有诊断价值，始终保留；镜像错误只留最后两条，避免报错糊成一团
    if len(errors) > 3:
        errors = errors[:1] + errors[-2:]
    return {"error": "；".join(errors)}


def _run_bpcs(args: list, timeout: int = 300) -> tuple:
    try:
        r = subprocess.run(
            [BPCS_PATH] + args, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        return r.stdout or "", r.stderr or "", r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
    except Exception as e:
        return "", str(e), -2


def _init_bpcs() -> bool:
    """检查 BaiduPCS-Go 是否已有本地登录凭证（通过 who 命令）。"""
    global _bpcs_inited
    if _bpcs_inited:
        return True
    with _bpcs_lock:
        if _bpcs_inited:
            return True
        if not _ensure_bpcs_exe():
            logger.warning(f"[baidu_netdisk] {_BPCS_EXE_NAME} 不可用，登录时会自动尝试下载，也可用 /pan pcs 手动下载")
            return False
        out, _, code = _run_bpcs(["who"], timeout=15)
        if code == 0 and out and "登录" not in (out or "") and "未登录" not in (out or ""):
            _bpcs_inited = True
            logger.info(f"[baidu_netdisk] BaiduPCS-Go already logged in: {out.strip()[:100]}")
            return True
        logger.info("[baidu_netdisk] BaiduPCS-Go not logged in, use /pan login")
        return False


def _bpcs_config_path() -> str:
    """BaiduPCS-Go 的 pcs_config.json 路径（登录凭证，沿用 BaiduPCS-Go 默认位置）。

    与 BaiduPCS-Go 源码 GetConfigDir() 保持一致：
      - BAIDUPCS_GO_CONFIG_DIR 环境变量（绝对路径直接用，相对路径以 exe 所在目录为基准）
      - exe 同目录已存在旧版 pcs_config.json 时沿用旧位置
      - Windows: %APPDATA%/BaiduPCS-Go
      - Linux/macOS: ~/.config/BaiduPCS-Go
      - 上述都不可用时回退到 exe 同目录"""
    exe_dir = os.path.dirname(os.path.abspath(BPCS_PATH))
    env = os.environ.get("BAIDUPCS_GO_CONFIG_DIR")
    if env:
        base = env if os.path.isabs(env) else os.path.join(exe_dir, env)
        return os.path.join(base, "pcs_config.json")
    # 旧版行为：exe 同目录已存在 pcs_config.json 时沿用，避免切换路径后丢登录态
    if os.path.exists(os.path.join(exe_dir, "pcs_config.json")):
        return os.path.join(exe_dir, "pcs_config.json")
    if _IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "BaiduPCS-Go", "pcs_config.json")
        return os.path.join(exe_dir, "pcs_config.json")  # APPDATA 缺失时落到程序目录
    home = os.path.expanduser("~")
    if home and home != "~":
        return os.path.join(home, ".config", "BaiduPCS-Go", "pcs_config.json")
    return os.path.join(exe_dir, "pcs_config.json")  # HOME 缺失时落到程序目录


def login_bduss_bpcs(bduss: str, stoken: str = "") -> dict:
    """用 BDUSS + STOKEN 登录 BaiduPCS-Go。"""
    global _bpcs_inited
    if not _auto_download_bpcs():
        return {"error": f"{_BPCS_EXE_NAME} 不可用且自动下载失败，请稍后重试（或用 /pan pcs 手动下载）"}
    args = ["login", f"-bduss={bduss}"]
    if stoken:
        args.append(f"-stoken={stoken}")
    out, err, code = _run_bpcs(args, timeout=30)
    text = (out or "") + chr(10) + (err or "")
    if code == 0 and "失败" not in text and "错误" not in text:
        _bpcs_inited = True
        logger.info("[baidu_netdisk] BaiduPCS-Go logged in via /pan login 3/bduss")
        return {"success": True}
    m = re.search(r"错误代码:\s*\d+[^\n]{0,60}|[\u4e00-\u9fa5]{2,20}[:：]?\s*[^\n]{0,60}", text)
    if m:
        return {"error": f"登录失败: {m.group(0).strip()}"}
    return {"error": f"登录失败（退出码 {code}）: {text.strip()[:200]}"}


def _patch_config_stoken(stoken: str, bduss: str = ""):
    """手动补写 pcs_config.json 中的 stoken 和 bduss 字段。
    login -cookies= 不会自动填充 stoken 字段，但 transfer 需要它。"""
    config_path = _bpcs_config_path()
    if not os.path.exists(config_path):
        logger.warning(f"[baidu_netdisk] pcs_config.json not found at {config_path}")
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        user_list = cfg.get("baidu_user_list", [])
        if user_list:
            if stoken:
                user_list[0]["stoken"] = stoken
            if bduss:
                user_list[0]["bduss"] = bduss
            with open(config_path, "w", encoding="utf-8") as f:
                _json.dump(cfg, f, ensure_ascii=False, indent=4)
            logger.info(f"[baidu_netdisk] patched stoken into config")
    except Exception as e:
        logger.warning(f"[baidu_netdisk] failed to patch config: {e}")


def login_cookies_bpcs(cookie_str: str) -> dict:
    """用完整 Cookie 字符串登录 BaiduPCS-Go（v4.0.1 支持 -cookies 参数）。"""
    global _bpcs_inited
    if not _auto_download_bpcs():
        return {"error": f"{_BPCS_EXE_NAME} 不可用且自动下载失败，请稍后重试（或用 /pan pcs 手动下载）"}
    # 清洗：去掉可能干扰的空名项（如 =value）和 *_BFESS 字段
    cleaned = "; ".join(
        part.strip() for part in cookie_str.split(";")
        if part.strip() and "=" in part.strip()
        and "_BFESS" not in part
    )
    out, err, code = _run_bpcs(["login", f"-cookies={cleaned}"], timeout=30)
    text = (out or "") + chr(10) + (err or "")
    if code == 0 and "失败" not in text and "错误" not in text:
        # cookies 登录后 stoken 字段不会自动填充，手动补写
        stoken_m = re.search(r'STOKEN=([^\s;]+)', cleaned)
        bduss_m = re.search(r'BDUSS=([^\s;]+)', cleaned)
        if stoken_m:
            _patch_config_stoken(stoken_m.group(1), bduss_m.group(1) if bduss_m else "")
        _bpcs_inited = True
        logger.info("[baidu_netdisk] BaiduPCS-Go logged in via cookies")
        return {"success": True}
    m = re.search(r"错误代码:\s*\d+[^\n]{0,60}|[\u4e00-\u9fa5]{2,20}[:：]?\s*[^\n]{0,60}", text)
    if m:
        return {"error": f"登录失败: {m.group(0).strip()}"}
    return {"error": f"登录失败（退出码 {code}）: {text.strip()[:200]}"}


def login_bpcs(username: str, password: str) -> dict:
    """用百度账号密码登录 BaiduPCS-Go，登录成功后凭证保存在本地，全局生效。
    优先走交互式 stdin 输入，避免密码出现在进程命令行（同机其他进程可见）。"""
    global _bpcs_inited
    if not _auto_download_bpcs():
        return {"error": f"{_BPCS_EXE_NAME} 不可用且自动下载失败，请稍后重试（或用 /pan pcs 手动下载）"}

    # 方式一：交互式输入（密码不进命令行）
    try:
        proc = subprocess.Popen(
            [BPCS_PATH, "login"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        out_bytes, _ = proc.communicate(
            input=f"{username}\n{password}\n".encode("utf-8"), timeout=60
        )
        text = (out_bytes or b"").decode("utf-8", errors="replace")
        if proc.returncode == 0 and "失败" not in text and "错误" not in text:
            _bpcs_inited = True
            logger.info("[baidu_netdisk] BaiduPCS-Go logged in via /pan login 2/pwd (stdin)")
            return {"success": True, "output": text.strip()[:300]}
        logger.info(f"[baidu_netdisk] stdin login 未成功，回退参数方式: rc={proc.returncode}")
    except Exception as e:
        logger.warning(f"[baidu_netdisk] stdin login 异常，回退参数方式: {e}")

    # 方式二：参数式（兜底）
    out, err, code = _run_bpcs(
        ["login", f"-username={username}", f"-password={password}"], timeout=60
    )
    text = (out or "") + chr(10) + (err or "")
    if code == 0 and "失败" not in text and "错误" not in text:
        _bpcs_inited = True
        logger.info("[baidu_netdisk] BaiduPCS-Go logged in via /pan login 2/pwd")
        return {"success": True, "output": text.strip()[:300]}
    m = re.search(r"[\u4e00-\u9fa5]{2,20}[:：]?\s*[0-9a-zA-Z\u4e00-\u9fa5 ,.()]+", text)
    if m:
        return {"error": f"登录失败: {m.group(0).strip()}"}
    return {"error": f"登录失败（退出码 {code}）: {text.strip()[:200]}"}


def logout_bpcs() -> dict:
    """退出 BaiduPCS-Go 当前登录的百度帐号，清除本地凭证。"""
    global _bpcs_inited
    if not _auto_download_bpcs():
        return {"error": f"{_BPCS_EXE_NAME} 不可用且自动下载失败，请稍后重试（或用 /pan pcs 手动下载）"}
    out, err, code = _run_bpcs(["logout", "-y"], timeout=30)
    text = (out or "") + chr(10) + (err or "")
    if code == 0:
        _bpcs_inited = False
        logger.info("[baidu_netdisk] BaiduPCS-Go logged out via /pan login out")
        return {"success": True}
    return {"error": f"退出失败（退出码 {code}）: {text.strip()[:200]}"}


def _build_pan_session() -> object:
    """从 pcs_config.json 读取 cookies，构建已登录的 requests.Session。
    自动访问 pan.baidu.com/disk/main 获取 csrfToken / PANPSC 等 pan 专用 cookie。"""
    cfg_path = _bpcs_config_path()
    if not os.path.exists(cfg_path):
        logger.warning("[baidu_netdisk] pcs_config.json not found, cannot build pan session")
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        user = cfg.get("baidu_user_list", [{}])[0]
        cookies_str = user.get("cookies", "") or user.get("bduss", "")
        if not cookies_str:
            logger.warning("[baidu_netdisk] no cookies/bduss in config")
            return None
    except Exception as e:
        logger.warning(f"[baidu_netdisk] failed to read config: {e}")
        return None

    sess = _req.Session()
    sess.headers.update({
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    # 如果只有 BDUSS 没有完整 cookie，用 BDUSS 做一个简单 session
    if "BDUSS=" not in cookies_str and "=" not in cookies_str:
        sess.cookies.set("BDUSS", cookies_str, domain=".baidu.com")
        # 尝试从 stoken 字段补充
        stoken = user.get("stoken", "")
        if stoken:
            sess.cookies.set("STOKEN", stoken, domain=".baidu.com")
    else:
        for part in cookies_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    sess.cookies.set(k, v, domain=".baidu.com")
    # 访问 pan.baidu.com 获取 pan 专用 cookie（csrfToken / PANPSC）
    try:
        sess.get("https://pan.baidu.com/disk/main", timeout=10)
    except Exception:
        pass
    return sess


def _grab_share_field(html: str, key: str) -> str:
    """从分享页 HTML 中提取单个字段值（兼容引号/数值两种形式）。
    不做整页 JSON 解析，避免正则改写 URL/时间等内容导致解析失败。"""
    m = re.search(rf'["\']{re.escape(key)}["\']\s*:\s*["\']?([A-Za-z0-9_\-.]*)', html or "")
    return m.group(1) if m else ""


def _get_bdstoken(sess) -> str:
    """通过轻量接口获取 bdstoken（不依赖 /s/ 分享页渲染，分享页故障时仍可用）。"""
    try:
        r = sess.get(
            "https://pan.baidu.com/api/gettemplatevariable",
            params={"app_id": "250528", "web": "1", "clienttype": "0",
                    "fields": '["bdstoken"]'},
            timeout=15, headers={"Referer": "https://pan.baidu.com/disk/main"})
        j = r.json()
        if j.get("errno") == 0:
            return (j.get("result") or {}).get("bdstoken", "")
    except Exception as e:
        logger.warning(f"[baidu_netdisk] gettemplatevariable 失败: {e}")
    return ""


def _resolve_share_via_page(sess, surl: str, share_link: str) -> dict:
    """兜底：从 /s/ 分享页 HTML 解析 shareid / uk / bdstoken。
    仅在 shorturlinfo 网络层失败时使用（分享页服务故障时此路很慢）。"""
    r = None
    for _attempt in (1, 2):
        try:
            r = sess.get(share_link, timeout=30,
                         headers={"Referer": "https://pan.baidu.com/disk/home"})
            break
        except _req.exceptions.RequestException as e:
            if _attempt == 2:
                if isinstance(e, _req.exceptions.ReadTimeout):
                    # 正常分享页 1s 左右响应；两次都读超时基本是分享已在服务端失效
                    return {"error": "分享页无响应（重试仍超时），该分享链接很可能已失效，请让分享者重新分享"}
                raise
            logger.warning(f"[baidu_netdisk] 分享页请求失败，重试: {e}")
            time.sleep(1)
    if _grab_share_field(r.text, "loginstate") == "0":
        return {"error": "网盘未登录，请使用 /pan login 1 重新登录"}
    info = {
        "shareid": _grab_share_field(r.text, "shareid"),
        "share_uk": _grab_share_field(r.text, "share_uk") or _grab_share_field(r.text, "uk"),
        "bdstoken": _grab_share_field(r.text, "bdstoken"),
    }
    if not info["shareid"]:
        return {"error": "无法获取分享信息，链接可能已失效"}
    return info


def _share_api_preflight(sess, surl: str, pwd: str, share_link: str) -> dict:
    """转存/列目录的公共前置：shorturlinfo 拿 shareid/uk → gettemplatevariable
    拿 bdstoken → 需要时验证提取码。全程不依赖 /s/ 分享页渲染。
    返回 {"shareid","uk","bdstoken","short"} 或 {"error"}。"""
    shareid = share_uk = bdstoken = ""
    need_pwd = False
    try:
        r1 = sess.get(
            "https://pan.baidu.com/api/shorturlinfo",
            params={"appid": "250528", "shorturl": surl, "root": "1"},
            timeout=15, headers={"Referer": "https://pan.baidu.com/disk/main"})
        j1 = r1.json()
        shareid = str(j1.get("shareid") or "")
        share_uk = str(j1.get("uk") or "")
        need_pwd = (j1.get("errno") == -9)
        if not shareid:
            show = j1.get("show_msg") or j1.get("err_msg") or f"errno={j1.get('errno')}"
            return {"error": f"分享链接无效或已失效（{show}）"}
    except (_req.exceptions.RequestException, ValueError) as e:
        # 仅网络层失败才回退分享页解析（接口明确返回 errno 时链接就是无效的）
        logger.warning(f"[baidu_netdisk] shorturlinfo 请求失败，回退分享页解析: {e}")
        page = _resolve_share_via_page(sess, surl, share_link)
        if "error" in page:
            return page
        shareid = page["shareid"]
        share_uk = page["share_uk"]
        bdstoken = page["bdstoken"]
    if not bdstoken:
        bdstoken = _get_bdstoken(sess)
    if not bdstoken:
        return {"error": "无法获取 bdstoken，请稍后重试或重新登录"}
    if pwd or need_pwd:
        if not pwd:
            return {"error": "该分享需要提取码，请按「链接 提取码」的格式发送"}
        verify_url = (
            f"https://pan.baidu.com/share/verify"
            f"?shareid={shareid}&time={int(time.time()*1000)}"
            f"&clienttype=1&uk={share_uk}"
        )
        headers = {
            "Referer": share_link,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        r2 = sess.post(verify_url, data={
            "pwd": pwd, "vcode": "null", "vcode_str": "null", "bdstoken": bdstoken
        }, headers=headers, timeout=20)
        resp2 = r2.json()
        if resp2.get("errno") != 0:
            if resp2.get("errno") == -9:
                return {"error": "提取码错误"}
            show = resp2.get("show_msg") or resp2.get("errno")
            return {"error": f"密码验证失败: {show}"}
    return {"shareid": shareid, "uk": share_uk, "bdstoken": bdstoken,
            "short": surl[1:] if surl.startswith("1") else surl}


def _api_dir_list_errno(sess, bdstoken: str, dir_path: str) -> int:
    """查询自己网盘目录是否存在（errno: 0=存在, -9=不存在, 其他=异常）。"""
    try:
        r = sess.get(
            "https://pan.baidu.com/api/list",
            params={"app_id": "250528", "bdstoken": bdstoken, "dir": dir_path,
                    "order": "name", "desc": "0", "showempty": "0", "web": "1",
                    "page": "1", "num": "1"},
            headers={"Referer": "https://pan.baidu.com/disk/main"}, timeout=15)
        return int(r.json().get("errno", -1))
    except Exception as e:
        logger.warning(f"[baidu_netdisk] api/list 检查目录 {dir_path} 失败: {e}")
        return -1


def _ensure_remote_dir(sess, bdstoken: str, target_path: str) -> str:
    """逐级确保转存目标目录存在（纯 API，不依赖 BaiduPCS-Go）。

    百度 share/transfer 对不存在的目标目录返回 errno=2「转存路径不存在」，
    新账号/首次使用时网盘里根本没有 /我的资源/AutoTransfer，转存必然失败。
    这里按 /a/b/c 逐级 list 确认、缺失则 api/create 创建。
    注意 api/create 对「已存在」的目录不会报错而是自动改名（errno=0），
    所以必须先 list 确认再创建，避免重复创建出带时间戳后缀的目录。

    返回 "" 表示成功，否则返回错误描述。"""
    parts = [p for p in str(target_path).strip("/").split("/") if p]
    cur = ""
    for p in parts:
        cur = f"{cur}/{p}"
        errno = _api_dir_list_errno(sess, bdstoken, cur)
        if errno == 0:
            continue
        if errno != -9:
            return f"检查网盘目录 {cur} 失败(errno={errno})"
        try:
            r = sess.post(
                "https://pan.baidu.com/api/create",
                params={"app_id": "250528", "bdstoken": bdstoken},
                data={"path": cur, "isdir": "1", "block_list": "[]"},
                headers={"Referer": "https://pan.baidu.com/disk/main",
                         "Content-Type": "application/x-www-form-urlencoded"},
                timeout=20).json()
        except Exception as e:
            logger.warning(f"[baidu_netdisk] 创建网盘目录 {cur} 失败: {e}")
            return f"创建网盘目录 {cur} 失败: {e}"
        if r.get("errno") != 0:
            show = r.get("show_msg") or r.get("errno")
            logger.warning(f"[baidu_netdisk] 创建网盘目录 {cur} 失败: {show}")
            return f"创建网盘目录 {cur} 失败(errno={show})"
        logger.info(f"[baidu_netdisk] 已自动创建网盘转存目录: {cur}")
    return ""


def _transfer_via_api(surl: str, pwd: str, target_path: str, fs_ids: list = None) -> dict:
    """用 Python requests 直接调用百度网盘 API，把指定的 fs_id 列表转存到 target_path。
    fs_ids 为 None 时转存分享根目录的全部文件。
    替代 BaiduPCS-Go 的 transfer 命令（v4.0.1 有 cookie 传递 bug）。
    转存前会自动逐级创建 target_path（新账号网盘里可能还没有该目录）。

    返回: {"success": True, "filenames": [...], "fs_ids": [...]}
          或 {"error": "错误信息"}
    """
    sess = _build_pan_session()
    if sess is None:
        return {"error": "无法获取百度网盘登录凭证，请先使用 /pan login 1 登录"}

    share_link = f"https://pan.baidu.com/s/{surl}"

    try:
        pf = _share_api_preflight(sess, surl, pwd, share_link)
        if "error" in pf:
            return pf
        shareid, share_uk = pf["shareid"], pf["uk"]
        bdstoken, short = pf["bdstoken"], pf["short"]
        logger.info(f"[baidu_netdisk] transfer: shareid={shareid}, uk={share_uk}, fs_ids={fs_ids or '全部'}")

        filenames = []
        if not fs_ids:
            # 未指定 fs_id：列出分享根目录全部文件
            rl = sess.get(
                "https://pan.baidu.com/share/list",
                params={"bdstoken": bdstoken, "root": "1", "web": "5", "app_id": "250528",
                        "shorturl": short, "channel": "chunlei", "clienttype": "0"},
                headers={"Referer": share_link}, timeout=30)
            resp4 = rl.json()
            if resp4.get("errno") != 0:
                if resp4.get("errno") == -9:
                    return {"error": "该分享需要提取码，请按「链接 提取码」的格式发送"}
                show = resp4.get("show_msg") or resp4.get("errno")
                return {"error": f"获取文件列表失败: {show}"}
            files = resp4.get("list", [])
            if not files:
                return {"error": "分享链接中没有文件"}
            fs_ids = [str(f.get("fs_id")) for f in files if f.get("fs_id")]
            filenames = [f.get("server_filename") for f in files]
            logger.info(f"[baidu_netdisk] transfer files: {filenames}")

        # Step 4.5: 确保转存目标目录存在（新账号/首次使用网盘里通常还没有该目录，
        # 缺失时 share/transfer 返回 errno=2「转存路径不存在」导致下载误报「路径不存在」）
        mkdir_err = _ensure_remote_dir(sess, bdstoken, target_path)
        if mkdir_err:
            return {"error": mkdir_err}

        # Step 5: 执行转存
        transfer_url = (
            f"https://pan.baidu.com/share/transfer"
            f"?app_id=250528&channel=chunlei&clienttype=0&web=1"
            f"&bdstoken={bdstoken}&shareid={shareid}&from={share_uk}"
        )
        transfer_data = {
            "fsidlist": "[" + ",".join(fs_ids) + "]",
            "path": target_path,
        }
        r5 = sess.post(transfer_url, data=transfer_data,
                       headers={"Referer": share_link,
                                "Content-Type": "application/x-www-form-urlencoded"},
                       timeout=30)
        resp5 = r5.json()
        errno = resp5.get("errno", -1)
        if errno == 0:
            logger.info(f"[baidu_netdisk] transfer success: {filenames}")
            return {"success": True, "filenames": filenames, "fs_ids": fs_ids}
        elif errno == 2:
            # errno=2 是「转存路径不存在」（目标目录缺失，目录已在 Step 4.5 自动创建，
            # 若仍返回 2 说明目录创建失败）；旧版本把它误判为「自己的分享/已转存过」
            # 直接放行，导致后续下载报误导性的「路径不存在」。此处如实报错。
            # 注：百度对「不能转存自己的分享」也可能返回 errno=2。
            show = (resp5.get("show_msg") or "").strip()
            logger.warning(f"[baidu_netdisk] transfer errno=2, show_msg={show or '(空)'}")
            if "路径不存在" in show or "不存在" in show:
                return {"error": f"转存失败：目标目录 {target_path} 创建失败或不存在（{show}）"}
            return {"error": f"转存失败(errno=2): {show or '不能转存自己的分享（同一账号限制）'}。"
                             f"若是你自己的分享，请从网盘原目录下载，或换一个非本人的网盘账号登录"}
        elif errno == 4:
            # errno=4: 目标目录里已有这些文件（之前转存过/重复转存），
            # duplicated 列表中的文件就是云端已存在的目标文件，可以直接继续下载
            logger.info(f"[baidu_netdisk] transfer errno=4 (already exists), filenames={filenames}")
            return {"success": True, "filenames": filenames, "fs_ids": fs_ids}
        elif errno == 12:
            # 按 info 里的具体 errno 区分：-30 同名已存在 / -32 空间不足
            info = resp5.get("info", []) or []
            errnos = [item.get("errno") for item in info]
            if errnos and all(e in (-30, 0) for e in errnos):
                # 目标目录已有同名文件（多为之前已转存过），文件已在云端即可继续
                logger.info(f"[baidu_netdisk] transfer errno=12 (same-name exists), filenames={filenames}")
                return {"success": True, "filenames": filenames, "fs_ids": fs_ids}
            if -32 in errnos or "空间不足" in (resp5.get("show_msg") or ""):
                need_gb = (resp5.get("target_size") or 0) / 1024 ** 3
                return {"error": f"网盘剩余空间不足，无法转存（本次约需 {need_gb:.0f} GB）。"
                                 f"请只选择需要的文件/文件夹下载（/pan down <路径>）"}
            return {"error": f"转存失败: {resp5.get('show_msg') or '文件冲突'}"}
        else:
            show_msg = resp5.get("show_msg", "") or resp5.get("err_msg", "")
            return {"error": f"转存失败(errno={errno}): {show_msg}"}

    except Exception as e:
        logger.exception(f"[baidu_netdisk] transfer API error: {e}")
        return {"error": f"转存API调用异常: {e}"}


def _manifest_load() -> set:
    """读取本插件下载过的文件清单。"""
    try:
        with open(_MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return set(data.get("files", [])) if isinstance(data, dict) else set(data)
    except Exception:
        return set()


def _manifest_save(paths: set):
    try:
        os.makedirs(os.path.dirname(_MANIFEST_PATH), exist_ok=True)
        with open(_MANIFEST_PATH, "w", encoding="utf-8") as f:
            _json.dump({"files": sorted(paths)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[baidu_netdisk] save cleanup manifest failed: {e}")


def _manifest_add(paths: list):
    """登记插件下载到本地的文件，供定时清理使用。"""
    new = {p for p in paths if p}
    if not new:
        return
    _manifest_save(_manifest_load() | new)


def _cleanup_local_dir():
    """清理本插件下载并登记过的本地文件（不会动下载目录里的其他文件）。"""
    paths = _manifest_load()
    if not paths:
        return
    removed = 0
    remain = set()
    for p in paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
            elif os.path.exists(p):
                remain.add(p)  # 目录等非普通文件，保留不动
        except Exception as e:
            logger.warning(f"[baidu_netdisk] cleanup skip {p}: {e}")
            remain.add(p)
    _manifest_save(remain)
    if removed:
        logger.info(f"[baidu_netdisk] local cleanup: removed {removed} plugin-downloaded files")


def _schedule_local_cleanup(hour: int):
    """安排每天定时清理本地下载文件（仅清理插件自己下载的文件）。"""
    global _cleanup_thread_started
    if hour < 0 or hour > 23:
        logger.info("[baidu_netdisk] local auto-cleanup disabled")
        return
    if _cleanup_thread_started:
        logger.info("[baidu_netdisk] local cleanup thread already running, skip")
        return
    _cleanup_thread_started = True

    def _worker():
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


# 从任意文本（含整段中文分享文案）中提取分享链接与提取码
# 链接形式：pan.baidu.com/s/xxx、pan.baidu.com/share/init?surl=xxx、yun.baidu.com/s/xxx（可无协议头）
_SHARE_SURL_RE = re.compile(
    r'(?:https?://)?(?:[A-Za-z0-9-]+\.)*baidu\.com/(?:s/|share/init\?(?:surl|shorturl)=)'
    r'([A-Za-z0-9_-]+)',
    re.IGNORECASE,
)
# 提取码形式：pwd=xxxx / 提取码:xxxx / 提取码：xxxx / 密码  xxxx / 访问码:xxxx（支持半角与全角冒号）
_PWD_RE = re.compile(
    r'(?<![A-Za-z0-9])(?:pwd|password|提取码|密码|访问码)\s*[:：=]?\s*([A-Za-z0-9]{4,6})',
    re.IGNORECASE,
)


def parse_share_link(link: str) -> tuple:
    """从任意文本中提取百度网盘分享的 (surl, 提取码)。支持：
    - 完整链接（含 ?pwd=xxx）、裸 surl
    - 整段中文分享文案，如：
      "通过百度网盘分享的文件：xxx.zip 链接:https://pan.baidu.com/s/1abc123?pwd=abcd 复制这段内容…"
      "通过百度网盘分享的文件：xxx.zip 链接:https://pan.baidu.com/s/1abc123 提取码:abcd 复制这段内容…"
    找不到链接时 surl 返回空串。"""
    text = (link or "").strip().replace("\\", "")
    if "baidu.com/link" in text or "url=" in text:
        text = urllib.parse.unquote(text)
    surl = ""
    m = _SHARE_SURL_RE.search(text)
    if m:
        surl = m.group(1)
    pwd = ""
    m = _PWD_RE.search(text)
    if m:
        pwd = m.group(1).strip()
    return surl, pwd


def _parse_size_bytes(raw_size: str) -> int:
    """解析 BPCS ls 输出的大小字符串（如 1.5GB / 300KB / -）为字节数，失败返回 0。"""
    raw = (raw_size or "").strip().upper()
    if not raw or raw == "-":
        return 0
    try:
        for unit, mult in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                           ("MB", 1024 ** 2), ("KB", 1024)):
            if raw.endswith(unit):
                return int(float(raw[:-len(unit)]) * mult)
        if raw.endswith("B"):
            return int(float(raw[:-1]))
        return int(float(raw))
    except ValueError:
        return 0


def _format_size(raw_size: str) -> str:
    """解析 BPCS ls 输出的文件大小字符串并格式化。"""
    b = _parse_size_bytes(raw_size)
    if b <= 0:
        return raw_size
    for unit, threshold in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if b >= threshold:
            return f"{b / threshold:.2f} {unit}"
    return f"{b:.0f} B"


# BPCS ls -l 行结构: 序号 FSID APPID 大小 创建日期 时间 修改日期 时间 [MD5] 文件名(可含空格)
_BPCS_LS_RE = re.compile(
    r'^\s*\d+\s+\d+\s+\d+\s+(\S+)\s+'
    r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+'
    r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+'
    r'(?:[0-9a-fA-F]{32}\s+)?(.+?)\s*$'
)


def _parse_ls_line(line: str):
    """解析 BPCS ls -l 的一行文件记录。
    返回 (name, size_raw, is_dir)，非文件行返回 None。
    按日期列定位文件名起点，支持文件名含空格。"""
    m = _BPCS_LS_RE.match(line)
    if not m:
        return None
    size_raw, name = m.group(1), m.group(2)
    is_dir = size_raw == "-" or name.endswith("/")
    name = name.rstrip("/")
    if not name:
        return None
    return name, size_raw, is_dir


def _list_share_tree_api(surl: str, pwd: str) -> dict:
    """通过 share/list API 直接列出分享目录树（不转存、不依赖 /s/ 分享页）。
    只读目录结构，不占网盘空间，特大分享（TB 级）也能正常浏览。
    返回 {"text": 目录树文本, "items": [{path,name,size,is_dir,fs_id}]} 或 {"error"}。"""
    sess = _build_pan_session()
    if sess is None:
        return {"error": "无法获取百度网盘登录凭证，请先使用 /pan login 1 登录"}
    share_link = f"https://pan.baidu.com/s/{surl}"
    pf = _share_api_preflight(sess, surl, pwd, share_link)
    if "error" in pf:
        return pf
    bdstoken, short = pf["bdstoken"], pf["short"]

    lines, items = [], []
    state = {"truncated": False}

    def _walk(dir_rel: str, api_dir: str, depth: int):
        if len(items) > 300:
            state["truncated"] = True
            return
        if depth > 2:  # 最多展示 3 层，更深的用 /pan down <路径> 按需下载
            return
        params = {"bdstoken": bdstoken, "web": "5", "app_id": "250528",
                  "shorturl": short, "channel": "chunlei", "clienttype": "0", "num": "100"}
        if api_dir:
            # 子目录必须用父列表返回的原始 path：分享可能创建自网盘子目录，
            # 内部路径并不以分享内容名开头（如 /模拟器/NS/Citron-neo）
            params["dir"] = api_dir
            params["root"] = "0"
        else:
            params["root"] = "1"
        try:
            r = sess.get("https://pan.baidu.com/share/list", params=params,
                         headers={"Referer": share_link}, timeout=30)
            j = r.json()
            lst = j.get("list") or []
            if j.get("errno") != 0 and dir_rel:
                lines.append(f"{'  ' * depth}⚠️ 子目录 {dir_rel} 获取失败(errno={j.get('errno')})")
                logger.warning(f"[baidu_netdisk] share/list {dir_rel} errno={j.get('errno')}")
                return
        except Exception as e:
            logger.warning(f"[baidu_netdisk] share/list {dir_rel or '根目录'} 失败: {e}")
            return
        prefix = "  " * depth
        for f in lst[:60]:
            name = f.get("server_filename") or ""
            if not name:
                continue
            is_dir = str(f.get("isdir")) == "1"
            rel = f"{dir_rel}/{name}" if dir_rel else name
            if is_dir:
                lines.append(f"{prefix}📁 {name}/")
                items.append({"path": rel, "name": name, "size": "-", "is_dir": True,
                              "fs_id": str(f.get("fs_id"))})
                _walk(rel, f.get("path") or "", depth + 1)
            else:
                lines.append(f"{prefix}📄 {name}  ({_format_size(str(f.get('size', 0)))})")
                items.append({"path": rel, "name": name, "size": str(f.get("size", 0)),
                              "is_dir": False, "fs_id": str(f.get("fs_id"))})

    _walk("", "", 0)
    if not lines:
        return {"error": "目录为空或获取失败"}
    if state["truncated"]:
        lines.append("（目录过多已截断，建议用 /pan down <路径> 直接下载需要的文件）")
    return {"text": "\n".join(lines), "items": items}


def list_share_content(surl: str, pwd: str = "") -> dict:
    """列出分享目录树（API 直连，不转存、不占网盘空间）。"""
    result = _list_share_tree_api(surl, pwd)
    if "error" in result:
        return result
    return {"text": result["text"], "items": result["items"], "own_share": False}


def _user_bucket(user_id: str) -> str:
    """用户标识 → 转存子目录名（稳定短哈希，不把 QQ 号等原始标识写进网盘目录名）。"""
    raw = str(user_id or "").strip()
    if not raw:
        return "ushared"
    return "u" + hashlib.blake2b(raw.encode("utf-8"), digest_size=6).hexdigest()


def _snapshot_local_files(root: str) -> dict:
    """记录目录下每个文件的 (大小, mtime)，用于判断哪些文件是本次下载产生的。"""
    snap = {}
    if not root or not os.path.isdir(root):
        return snap
    for cur, _, names in os.walk(root):
        for n in names:
            p = os.path.join(cur, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            snap[p] = (st.st_size, st.st_mtime_ns)
    return snap


def _collect_files_under(root: str) -> list:
    """收集目录下所有文件为 {path, name, size}。"""
    out = []
    for cur, _, names in os.walk(root):
        for n in names:
            p = os.path.join(cur, n)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            out.append({"path": p, "name": n, "size": size})
    return out


def _collect_new_files(root: str, before: dict, bucket: str = "") -> list:
    """收集 root 下相对 before 快照「新增或内容有变化」的文件，即本次下载产生的文件。
    bucket 非空时优先只收该转存子目录下的文件：转存目录按用户隔离，
    并发下载时不会把别人的下载结果算进本次清单。
    不再有「收集全部文件」的兜底，避免把下载目录里的历史文件或他人文件发出去。"""
    def _in_bucket(path: str) -> bool:
        return not bucket or bucket in path.replace("\\", "/").split("/")

    matched, others = [], []
    for cur, _, names in os.walk(root):
        target = matched if _in_bucket(cur) else others
        for n in names:
            p = os.path.join(cur, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if before.get(p) == (st.st_size, st.st_mtime_ns):
                continue  # 下载前已存在且大小/时间未变，不是本次产生的
            target.append({"path": p, "name": n, "size": st.st_size})
    return matched or others


def download_from_cloud(cloud_path: str, max_mb: int = 0, progress_queue: "queue.Queue" = None,
                        cloud_dir: str = "", bucket: str = "") -> dict:
    """从网盘下载指定路径的文件或文件夹（不转存，直接下载已有文件）。

    cloud_dir: 网盘目录，留空则用全局 CLOUD_SAVE_DIR（调用方通常传本用户的转存子目录）。
    bucket: 本次任务在网盘目录中的隔离子目录名，用于把下载结果精确归属到本次任务。
    progress_queue: 可选，传入 queue.Queue 后，下载过程中会向队列放入进度信息。
    """
    cloud_dir = cloud_dir or CLOUD_SAVE_DIR or DEFAULT_CLOUD_SAVE_DIR
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
        account_folder = _get_account_folder()
        check_paths = []
        if account_folder:
            check_paths.append(os.path.join(DOWNLOAD_DIR, account_folder, full_path.strip("/")))
            check_paths.append(os.path.join(DOWNLOAD_DIR, account_folder, file_name))
        check_paths.append(os.path.join(DOWNLOAD_DIR, file_name))
        for p in check_paths:
            if os.path.exists(p):
                logger.info(f"[baidu_netdisk] file already exists locally: {p}")
                return {"path": p, "name": file_name, "size": os.path.getsize(p), "is_dir": False}

    # 下载（使用 subprocess.Popen 实时读输出，避免 BPCS 卡住）
    _run_bpcs(["config", "set", "-savedir", DOWNLOAD_DIR], timeout=15)
    if DOWNLOAD_TIMEOUT_SECONDS > 0:
        dl_timeout = DOWNLOAD_TIMEOUT_SECONDS
    else:
        dl_timeout = max(600, int(file_size / (1024 * 1024) * 15)) if not is_dir else 3600
    logger.info(f"[baidu_netdisk] download_from_cloud: starting download, dl_timeout={dl_timeout}s, progress_queue={progress_queue is not None}")
    # 把文件信息塞进队列，给 handler 监控文件大小用
    # 如果 file_size 为0但文件非目录，把 0 也塞进去，让 handler 监控时用实际文件大小
    if progress_queue is not None and not is_dir:
        progress_queue.put(("_info", file_name, file_size))
    # 文件夹下载前先记录本地已有文件，下载完成后据此精确归属「本次产生」的文件
    before_files = _snapshot_local_files(DOWNLOAD_DIR) if is_dir else {}
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
        logger.error(f"[baidu_netdisk] download timeout ({dl_timeout}s), killing process")
        # 超时后必须 kill，否则 BPCS 会继续在后台下载
        proc.kill()
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        if progress_queue is not None:
            progress_queue.put(("_error", "下载超时"))
        return {"error": "下载超时"}
    except Exception as e:
        logger.error(f"[baidu_netdisk] download subprocess error: {e}")
        return {"error": f"下载进程异常: {e}"}
    if code != 0 or "失败" in (out or "") or "错误" in (out or ""):
        logger.error(f"[baidu_netdisk] download failed: code={code}, out={(out or '')[:200]}")
        if progress_queue is not None:
            progress_queue.put(("_error", f"下载失败 (code={code})"))
        return {"error": "下载失败"}
    logger.info(f"[baidu_netdisk] download_from_cloud: download completed, code={code}")

    if not DOWNLOAD_DIR:
        return {"error": "下载目录未设置"}

    logger.info(f"[baidu_netdisk] download_from_cloud: searching for downloaded file, DOWNLOAD_DIR={DOWNLOAD_DIR}")
    account_folder = _get_account_folder()
    if is_dir:
        # 优先按「远程路径镜像」定位本地目录（BPCS 会把网盘路径建在 savedir/<账号>/ 下），
        # 目录不存在时退回「下载前后快照差异」；两者都不命中就报错，
        # 不再退回「收集下载目录全部文件」（那会把历史文件与他人的文件一起发出去）
        downloaded = []
        mirror_dir = (os.path.join(DOWNLOAD_DIR, account_folder, full_path.strip("/"))
                      if account_folder else "")
        if mirror_dir and os.path.isdir(mirror_dir):
            downloaded = _collect_files_under(mirror_dir)
        if not downloaded:
            downloaded = _collect_new_files(DOWNLOAD_DIR, before_files, bucket)
        if not downloaded:
            return {"error": "文件夹下载完成但未找到本次下载的文件"}
        _manifest_add([d["path"] for d in downloaded])
        if _auto_delete_cloud:
            _run_bpcs(["rm", full_path], timeout=30)
        return {"path": DOWNLOAD_DIR, "name": file_name, "size": 0, "is_dir": True, "files": downloaded}
    else:
        # 先找账号专属文件夹（BPCS 会按 uid_用户名 创建子目录），并优先按远程路径镜像定位
        search_dirs = []
        if account_folder:
            search_dirs.append(os.path.join(DOWNLOAD_DIR, account_folder, full_path.strip("/")))
            search_dirs.append(os.path.join(DOWNLOAD_DIR, account_folder, file_name))
        search_dirs.append(os.path.join(DOWNLOAD_DIR, file_name))
        local_path = None
        for p in search_dirs:
            if os.path.exists(p):
                local_path = p
                break
        if not local_path and bucket:
            # 兜底一：同名文件只认本次任务转存子目录里的那个
            for root, _, files in os.walk(DOWNLOAD_DIR):
                if bucket in root.replace("\\", "/").split("/") and file_name in files:
                    local_path = os.path.join(root, file_name)
                    break
        if not local_path:
            # 兜底二：目录结构与预期不符时，只认本次下载开始之后新出现的同名文件。
            # 单纯按文件名找会把别人下载的同名文件发出去，所以用创建/修改时间卡住下载起点；
            # 宁可报「未找到文件」，也不把不确定归属的文件发出去
            newest = None
            for root, _, files in os.walk(DOWNLOAD_DIR):
                if file_name not in files:
                    continue
                p = os.path.join(root, file_name)
                try:
                    fresh = max(os.path.getctime(p), os.path.getmtime(p))
                except OSError:
                    continue
                if fresh < dl_started_at:
                    continue
                if newest is None or fresh > newest[0]:
                    newest = (fresh, p)
            if newest:
                local_path = newest[1]
        if not local_path or not os.path.exists(local_path):
            return {"error": "下载完成但未找到文件"}
        _manifest_add([local_path])
        if _auto_delete_cloud:
            _run_bpcs(["rm", full_path], timeout=30)
        return {"path": local_path, "size": os.path.getsize(local_path), "name": file_name}


# 这些扩展名本身已是压缩格式：再 DEFLATE 几乎无收益还白费 CPU/时间，直接 STORED 拼装（速度接近 tar）
# 注意不含 wav/bmp/tif 这类未压缩格式——它们 DEFLATE 收益明显，仍走压缩
_PRECOMPRESSED_EXT = {
    # 视频
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".rmvb", ".mpg", ".mpeg", ".ts", ".vob",
    # 图片
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif",
    # 音频
    ".mp3", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".ape",
    # 压缩包 / 安装包
    ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz", ".zst", ".cab", ".apk", ".ipa",
    # 本质是 zip 容器 / 内部已压缩
    ".pdf", ".docx", ".xlsx", ".pptx",
}


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


def _get_account_folder() -> str:
    """从 BPCS 配置读取当前登录账号的 uid 和 name，返回账号文件夹名（如 620186943_hitomi999）。"""
    cfg_path = _bpcs_config_path()
    if not os.path.exists(cfg_path):
        return ""
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        user = cfg.get("baidu_user_list", [{}])[0]
        uid = user.get("uid", "")
        name = user.get("name", "")
        if uid and name:
            return f"{uid}_{name}"
    except Exception:
        pass
    return ""


def _get_local_file_size(file_name: str) -> int:
    """在 DOWNLOAD_DIR 下查找文件并返回大小，找不到返回 0。"""
    account_folder = _get_account_folder()
    check_paths = []
    if account_folder:
        check_paths.append(os.path.join(DOWNLOAD_DIR, account_folder, file_name))
    check_paths.append(os.path.join(DOWNLOAD_DIR, file_name))
    for p in check_paths:
        if os.path.exists(p):
            try:
                return os.path.getsize(p)
            except OSError:
                return 0
    return 0


@register(
    "astrbot_plugin_baidu_netdisk",
    "XTsat",
    "百度网盘分享文件自动下载发送",
    "2.0.0",
    "https://github.com/XTsat/astrbot_plugin_baidu_netdisk",
)
class BaiduNetdiskPlugin(Star):

    _SHARE_CACHE_MAX_USERS = 50  # 分享缓存最多保留多少个用户，避免长期运行后无限增长

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        global DOWNLOAD_DIR
        custom_dir = str(self.config.get("download_dir", "")).strip()
        if custom_dir:
            DOWNLOAD_DIR = os.path.abspath(custom_dir)
        else:
            DOWNLOAD_DIR = os.path.join(PLUGIN_DATA_DIR, "downloads")
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        except Exception as e:
            logger.warning(f"[baidu_netdisk] 配置的 download_dir ({DOWNLOAD_DIR}) 不可用 ({e})，回退到默认路径")
            DOWNLOAD_DIR = os.path.join(PLUGIN_DATA_DIR, "downloads")
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        logger.info(f"[baidu_netdisk] download dir: {DOWNLOAD_DIR}")

        global CLOUD_SAVE_DIR
        cloud_dir = str(self.config.get("cloud_save_dir", "")).strip()
        if cloud_dir:
            CLOUD_SAVE_DIR = cloud_dir.rstrip("/")
        else:
            CLOUD_SAVE_DIR = DEFAULT_CLOUD_SAVE_DIR
        logger.info(f"[baidu_netdisk] cloud save dir: {CLOUD_SAVE_DIR}")

        # 分享缓存：按用户分桶，谁看谁的链接互不覆盖（多人查看不同链接时不会串台）
        self._share_cache = {}
        # 下载去重锁：dl_key -> 发起用户，同时用于判断「该用户是否有下载进行中」
        self._active_downloads = {}

        # 设置 BaiduPCS-Go 下载目录（exe 可用时才设置）
        if _ensure_bpcs_exe():
            _run_bpcs(["config", "set", "-savedir", DOWNLOAD_DIR], timeout=15)
        else:
            logger.warning(f"[baidu_netdisk] {_BPCS_EXE_NAME} 不可用，首次登录时会自动下载，配置将在下载后生效")

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
        global _auto_delete_cloud
        _auto_delete_cloud = bool(self.config.get("auto_delete_cloud", True))
        logger.info(f"[baidu_netdisk] auto_delete_cloud: {_auto_delete_cloud}")

        # 进度显示配置
        self._progress_enabled = bool(self.config.get("progress_enabled", False))
        self._progress_interval = int(self.config.get("progress_interval", 30))
        if self._progress_enabled:
            logger.info(f"[baidu_netdisk] progress reporting enabled, interval={self._progress_interval}s")

        # 下载超时（秒），-1 = 不超时
        global DOWNLOAD_TIMEOUT_SECONDS
        DOWNLOAD_TIMEOUT_SECONDS = int(self.config.get("timeout_seconds", -1))
        logger.info(f"[baidu_netdisk] download timeout: {DOWNLOAD_TIMEOUT_SECONDS}s")

        # 读取每天定时清理本地文件的小时数
        global _local_cleanup_hour
        cleanup_hour = int(self.config.get("local_cleanup_hour", 3))
        _local_cleanup_hour = cleanup_hour
        _schedule_local_cleanup(cleanup_hour)

        threading.Thread(target=_init_bpcs, daemon=True).start()

        # 可选：配置了 BDUSS 则启动时自动登录
        bduss_val = str(self.config.get("bduuss", "")).strip()
        if bduss_val:
            threading.Thread(target=self._auto_login_bduss, args=(bduss_val,), daemon=True).start()

    def _auto_login_bduss(self, bduss: str):
        time.sleep(2)  # 等待 _init_bpcs 先跑完
        result = login_bduss_bpcs(bduss, "")
        if "error" in result:
            logger.warning(f"[baidu_netdisk] bduuss 配置自动登录失败: {result['error']}")
        else:
            logger.info("[baidu_netdisk] bduuss 配置自动登录成功")

    def _cloud_dir_for(self, user_id: str) -> str:
        """该用户的网盘转存目录：<cloud_save_dir>/<用户哈希>。
        按用户隔离后，下载完成的自动删除只会删掉自己的转存内容。"""
        base = (CLOUD_SAVE_DIR or DEFAULT_CLOUD_SAVE_DIR).rstrip("/")
        return f"{base}/{_user_bucket(user_id)}"

    def _cache_get(self, user_id: str) -> dict:
        """读取该用户的分享缓存（不写入，避免只读操作撑大缓存）。"""
        return self._share_cache.get(user_id) or {"surl": "", "pwd": "", "tree": "", "items": []}

    def _cache_put(self, user_id: str, surl: str, pwd: str, tree: str, items: list):
        """写入该用户的分享缓存；缓存按使用者数量封顶，超出的淘汰最早写入的那个。"""
        self._share_cache.pop(user_id, None)  # 先删再插，保持字典顺序=最近使用
        self._share_cache[user_id] = {"surl": surl, "pwd": pwd, "tree": tree, "items": items}
        while len(self._share_cache) > self._SHARE_CACHE_MAX_USERS:
            self._share_cache.pop(next(iter(self._share_cache)), None)

    def _user_has_active_download(self, user_id: str) -> bool:
        return user_id in self._active_downloads.values()

    def _transfer_and_list(self, surl: str, pwd: str, user_id: str) -> dict:
        """线程安全的"切换分享链接"：API 直连列出分享目录树并更新该用户的缓存。
        列目录不转存、也不删暂存目录（下载改为按需精准转存单个文件/文件夹，
        下载完成后由 auto_delete_cloud 清理），多用户查看不同链接互不影响。"""
        with _share_switch_lock:
            result = list_share_content(surl, pwd)
            if "error" not in result:
                self._cache_put(user_id, surl, pwd, result.get("text", ""), result.get("items", []))
        return result

    def _ensure_transferred_sync(self, cloud_path: str, user_id: str) -> dict:
        """把选中的分享内容（单个文件/文件夹）按 fs_id 精准转存到该用户的暂存目录。
        只转存所需项，避免整包转存撑爆网盘空间（大分享整包动辄上 TB）。
        返回 {"name": 转存后的文件/文件夹名, "is_dir": 是否为文件夹} 或 {"error": ...}。"""
        entry = self._cache_get(user_id)
        surl, pwd = entry["surl"], entry["pwd"]
        if not surl:
            return {"error": "请先查看目录: /pan <链接> [密码]"}
        items = entry["items"] or []
        target = next((it for it in items if it.get("path") == cloud_path), None)
        if not target and items:
            # 路径没命中（树被截断/缓存过期）：重新拉取目录树再找
            refreshed = _list_share_tree_api(surl, pwd)
            if "error" not in refreshed:
                self._cache_put(user_id, surl, pwd, refreshed.get("text", ""),
                                refreshed.get("items", []))
                items = self._cache_get(user_id)["items"]
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
    async def _interruptible_sleep(fut: asyncio.Future, seconds: int):
        """分段休眠，长间隔下也能及时感知下载完成。"""
        for _ in range(max(1, int(seconds))):
            if fut.done():
                return
            await asyncio.sleep(1)

    def _is_blacklisted(self, event: AstrMessageEvent) -> bool:
        if self._get_send_mode() != "onebot":
            return False  # 黑名单仅 OneBot 模式生效
        sid = event.get_sender_id()
        return sid and sid in self._blacklist

    def _get_max_mb(self) -> int:
        # 0 = 不限制；official 模式下硬限 200MB（发了也发不了）
        cap = int(self.config.get("max_file_size", 200))
        if self._get_send_mode() == "official":
            cap = min(cap, FLASH_TASK_LIMIT_MB) if cap > 0 else FLASH_TASK_LIMIT_MB
        return cap

    def _get_send_mode(self) -> str:
        return str(self.config.get("send_mode", "onebot")).strip().lower()

    def _get_zip_max_mb(self) -> int:
        """文件夹打包 zip 的体积上限（MB），0 = 不限制。"""
        try:
            return max(0, int(self.config.get("zip_max_size", _ZIP_MAX_MB_DEFAULT)))
        except (TypeError, ValueError):
            return _ZIP_MAX_MB_DEFAULT

    def _get_send_files_threshold(self) -> int:
        """文件夹内文件数 ≤ 此值时逐个发送（原始文件更易命中秒传），0 = 总是打包。"""
        try:
            return max(0, int(self.config.get("send_files_threshold", _SEND_FILES_THRESHOLD_DEFAULT)))
        except (TypeError, ValueError):
            return _SEND_FILES_THRESHOLD_DEFAULT

    async def _send_file(self, event: AstrMessageEvent, dl: dict, share_url: str = "") -> bool:
        """根据平台类型和文件大小选择发送方式。"""
        file_path = dl["path"]
        file_size = dl["size"]
        size_mb = file_size / (1024 * 1024)
        name = dl["name"]
        logger.info(f"[baidu_netdisk] _send_file: name={name}, size={size_mb:.1f}MB, share_url={share_url[:50] if share_url else 'None'}")

        # 自动检测平台，优先于配置
        platform = getattr(event.platform_meta, "name", "") if event.platform_meta else ""
        platform_id = getattr(event, "platform_identifier", "") or ""
        logger.info(f"[baidu_netdisk] _send_file: platform={platform}, platform_id={platform_id}")
        if platform == "qq_official" or "qq_official" in platform_id or "qq_official" in platform:
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
        try:
            await event.send(MessageChain(chain=[File(name=name, file=file_path)]))
            logger.info(f"[baidu_netdisk] _send_onebot: direct send success")
            return True
        except Exception as e2:
            logger.warning(f"[baidu_netdisk] _send_onebot: direct send failed: {e2}")
            # 兜底：发送链接
            msg = f"⚠️ 文件 {size_mb:.1f}MB 发送失败"
            if share_url:
                msg += f"。请自行下载: {share_url}"
            try:
                await event.send(MessageChain(chain=[Plain(msg)]))
            except Exception:
                pass
            return False

    async def _send_other(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = "") -> bool:
        """其他平台路径：直接发送文件，失败则发送链接"""
        try:
            await event.send(MessageChain(chain=[File(name=name, file=file_path)]))
            return True
        except Exception as e:
            logger.warning(f"[baidu_netdisk] send file failed: {e}")
            msg = "⚠️ 文件发送失败"
            if share_url:
                msg += f"，请自行下载: {share_url}"
            try:
                await event.send(MessageChain(chain=[Plain(msg)]))
            except Exception:
                pass
            return False

    async def _send_official(self, event: AstrMessageEvent, file_path: str, name: str, size_mb: float, share_url: str = "") -> bool:
        """QQ官方机器人路径：<=200MB 走官方API上传，>200MB 只能返回链接"""
        logger.info(f"[baidu_netdisk] _send_official: name={name}, size={size_mb:.1f}MB, limit={FLASH_TASK_LIMIT_MB}MB")
        if size_mb <= FLASH_TASK_LIMIT_MB:
            logger.info(f"[baidu_netdisk] _send_official: file <= limit, sending directly")
            try:
                await event.send(MessageChain(chain=[File(name=name, file=file_path)]))
                return True
            except Exception as e:
                logger.warning(f"[baidu_netdisk] _send_official: send failed: {e}")
                return False

        logger.info(f"[baidu_netdisk] _send_official: file > limit, returning link")
        msg = "⚠️ 文件超过200MB，QQ官方API暂不支持大文件上传"
        if share_url:
            msg += f"，请自行下载: {share_url}"
        try:
            await event.send(MessageChain(chain=[Plain(msg)]))
        except Exception:
            pass
        return False

    async def _send_folder(self, event: AstrMessageEvent, dl: dict, share_url: str = "",
                           label: str = "") -> str:
        """文件夹下载完成后的发送策略（QQ 等平台不能直接发送文件夹）：
        文件数 ≤ 阈值（默认 3，可配置）时逐个发送；否则打包 zip 当单文件发送；
        打包失败或体积超限时降级为「本地路径 + 文件清单」。
        返回给用户的收尾文本。"""
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
        platform_id = getattr(event, "platform_identifier", "") or ""
        is_official = (platform == "qq_official" or "qq_official" in platform_id
                       or "qq_official" in platform or self._get_send_mode() == "official")
        size_text = _format_size(str(total_bytes))
        if is_official and total_bytes > FLASH_TASK_LIMIT_MB * 1024 * 1024:
            return await self._folder_fallback(
                event, dl, f"⚠️ 文件夹约 {size_text}，超过 QQ 官方 {FLASH_TASK_LIMIT_MB}MB 限制，未打包发送", subject)
        zip_max_mb = self._get_zip_max_mb()
        if zip_max_mb > 0 and total_bytes > zip_max_mb * 1024 * 1024:
            return await self._folder_fallback(
                event, dl, f"⚠️ 文件夹约 {size_text}，超过打包上限 {zip_max_mb}MB（配置 zip_max_size），未打包发送", subject)
        # 打包需临时占用等量磁盘（zip 与原始文件并存），空间不足直接降级，避免压到一半失败
        try:
            free = shutil.disk_usage(PLUGIN_DATA_DIR).free
            if free < total_bytes * 1.05:
                return await self._folder_fallback(
                    event, dl,
                    f"⚠️ 打包需约 {_format_size(str(total_bytes))} 临时空间，当前可用 {_format_size(str(free))}，空间不足未打包",
                    subject)
        except Exception as e:
            logger.warning(f"[baidu_netdisk] _send_folder: 磁盘空间检查失败: {e}")

        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", folder_name).strip() or "folder"
        zip_name = f"{safe_name}.zip"
        zip_path = os.path.join(PLUGIN_DATA_DIR, "zips", f"{safe_name}_{uuid.uuid4().hex[:8]}.zip")
        z = await asyncio.to_thread(_zip_files, files, zip_path, folder_name)
        if "error" in z:
            return await self._folder_fallback(event, dl, f"⚠️ 打包失败（{z['error']}），未发送", subject)
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
                pass
        if sent_ok:
            return f"✅ {subject} 下载完成，已打包为 {zip_name} 发送（共 {count} 个文件）"
        return await self._folder_fallback(event, dl, "⚠️ zip 发送失败，未发送", subject)

    async def _folder_fallback(self, event: AstrMessageEvent, dl: dict, extra: str = "",
                               label: str = "") -> str:
        """文件夹无法直接发送时的降级输出：报完成 + 本地路径 + 文件清单。"""
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
        await event.send(event.plain_result(text))
        return text

    @filter.llm_tool(name="pan_list")
    async def pan_list(self, event: AstrMessageEvent, link: str, pwd: str = ""):
        """查看百度网盘分享链接的目录结构。用户说"查一下这个百度网盘链接"、"看看里面有什么文件"时调用。
        特大分享（TB级）也能正常查看（只读目录，不转存、不占网盘空间）。
        超大目录树最多展示3层/约300项，超出部分会在末尾提示；下载更深层的内容时把已知路径传给下载工具即可。

        Args:
            link(string): 百度网盘分享链接，如 https://pan.baidu.com/s/1abc123（也可直接传用户发来的整段分享文案，会自动识别链接与提取码）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return "你没有权限使用该功能。"
        surl, pwd = self._normalize_link(link, pwd)

        if not surl:
            return "无法解析链接"

        uid = event.get_sender_id()
        blocked = self._check_switch_allowed(surl, uid)
        if blocked:
            return blocked

        result = await asyncio.to_thread(self._transfer_and_list, surl, pwd, uid)
        if "error" in result:
            return result["error"]
        return (f"{result['text']}\n\n"
                f"（环境信息：网盘转存目录为 {self._cloud_dir_for(uid)}，本地下载目录为 {DOWNLOAD_DIR}，"
                f"后续转存/下载结果都在这两个目录下）")

    @filter.llm_tool(name="pan_download_dir")
    async def pan_download_dir(self, event: AstrMessageEvent, folder_path: str, link: str = "", pwd: str = ""):
        """下载百度网盘分享中的文件夹或单个文件。用户说"下载xxx文件夹"、"下载xxx里面的xxx文件夹"、"下载xxx这个文件"时调用。
        folder_path 必须与 pan_list 返回的目录树中显示的路径完全一致（从分享根目录第一层开始，如 "初中资料/2025万唯"），不要自创或省略层级。如果还没查看过目录树，先调用 pan_list。
        工具只转存选中项（需要网盘剩余空间大于该项大小）然后下载。若返回"空间不足"等错误，如实告诉用户原因，并建议挑选更小的文件/文件夹分批下载。
        下载完成后文件保存在本地下载目录（后台配置的 download_dir，未配置则为 AstrBot 插件数据目录 data/plugin_data 下的 downloads）下的 <账号uid_用户名>/ 子目录里，实际完整路径以工具返回值为准，回复用户时原样告知。
        下载文件夹时插件会自动发送内容（文件数 ≤ 阈值（默认 3）时逐个发送，更多则打包 zip 发送）；若体积过大发不动，只返回本地路径与文件清单，此时如实告知用户并建议挑选更小的文件下载。

        Args:
            folder_path(string): 目录树中显示的文件夹或文件路径，如 "初中资料" 或 "初中资料/2025万唯"
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供；也可传整段分享文案，会自动识别链接与提取码）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return "你没有权限使用该功能。"

        surl = ""
        if link:
            surl, pwd = self._normalize_link(link, pwd)

        uid = event.get_sender_id()
        if surl:
            blocked = self._check_switch_allowed(surl, uid)
            if blocked:
                return blocked
            result = await asyncio.to_thread(self._transfer_and_list, surl, pwd, uid)
            if "error" in result:
                return result["error"]
        elif not self._cache_get(uid)["surl"]:
            return "请先查看目录: /pan <链接> [密码]"

        dl_key = f"tool:pan_download_dir:{uid}:{folder_path}"
        if dl_key in self._active_downloads:
            logger.warning(f"[baidu_netdisk] pan_download_dir: duplicate blocked: {dl_key}")
            return "⏳ 该文件正在下载中，请稍候..."
        self._active_downloads[dl_key] = uid
        try:
            # 按需精准转存：只转存选中的文件夹，再从暂存目录下载
            tr = await asyncio.to_thread(self._ensure_transferred_sync, folder_path, uid)
            if "error" in tr:
                return tr["error"]
            prog_q = queue.Queue() if self._progress_enabled else None
            dl_future = asyncio.create_task(asyncio.to_thread(
                download_from_cloud, tr["name"], self._get_max_mb(), prog_q,
                self._cloud_dir_for(uid), _user_bucket(uid)))
            if prog_q:
                file_info = None
                while not dl_future.done():
                    try:
                        msg = prog_q.get_nowait()
                        if isinstance(msg, tuple) and msg[0] == "_info":
                            file_info = (msg[1], msg[2])
                        elif isinstance(msg, tuple) and msg[0] == "_error":
                            await event.send(event.plain_result(f"❌ {msg[1]}"))
                        elif isinstance(msg, str):
                            await event.send(event.plain_result(msg))
                    except queue.Empty:
                        pass
                    if file_info and not dl_future.done():
                        fname, total = file_info
                        current = _get_local_file_size(fname)
                        if current > 0 and total > 0:
                            pct = min(current / total * 100, 99.9)
                            await event.send(event.plain_result(f"⏬ 下载中: {pct:.1f}%"))
                    await self._interruptible_sleep(dl_future, self._progress_interval)
            dl = await dl_future
            if "error" in dl:
                return dl["error"]

            if dl.get("is_dir"):
                # 文件夹不能直接发送：≤阈值逐个发，否则打包 zip，超限则降级为路径+清单
                share_url = f"https://pan.baidu.com/s/{self._cache_get(uid)['surl']}"
                return await self._send_folder(event, dl, share_url)
            else:
                share_url = f"https://pan.baidu.com/s/{self._cache_get(uid)['surl']}"
                sent = await self._send_file(event, dl, share_url)
                if sent:
                    return f"文件 '{folder_path}' 下载并发送完成，本地路径: {dl['path']}"
                return f"文件 '{folder_path}' 已下载到 {dl['path']}，但发送失败"
        finally:
            self._active_downloads.pop(dl_key, None)

    @filter.llm_tool(name="pan_download_all")
    async def pan_download_all(self, event: AstrMessageEvent, link: str = "", pwd: str = ""):
        """下载百度网盘分享中的所有文件。用户说"全部下载"、"下载所有文件"、"都下载"时调用。
        注意：需要网盘剩余空间大于分享总大小；特大分享（TB级）会转存失败并返回空间不足提示，此时应引导用户改用 pan_download_dir 挑选需要的文件夹或文件分批下载。
        下载完成后文件保存在本地下载目录（后台配置的 download_dir）下的 <账号uid_用户名>/ 子目录里，实际完整路径以工具返回值为准，回复用户时原样告知。
        下载完成后插件会自动发送内容（文件数 ≤ 阈值（默认 3）时逐个发送，更多则打包 zip 发送）；体积过大发不动时只返回本地路径与文件清单，此时如实告知用户。

        Args:
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供；也可传整段分享文案，会自动识别链接与提取码）
            pwd(string): 提取码（可选）
        """
        if self._is_blacklisted(event):
            return "你没有权限使用该功能。"

        surl = ""
        if link:
            surl, pwd = self._normalize_link(link, pwd)

        uid = event.get_sender_id()
        if surl:
            blocked = self._check_switch_allowed(surl, uid)
            if blocked:
                return blocked
            result = await asyncio.to_thread(self._transfer_and_list, surl, pwd, uid)
            if "error" in result:
                return result["error"]
        elif not self._cache_get(uid)["surl"]:
            return "请先查看目录: /pan <链接> [密码]"

        dl_key = f"tool:pan_download_all:{uid}"
        if dl_key in self._active_downloads:
            logger.warning(f"[baidu_netdisk] pan_download_all: duplicate blocked: {dl_key}")
            return "⏳ 该文件正在下载中，请稍候..."
        self._active_downloads[dl_key] = uid
        try:
            # 先转存分享根目录全部文件到该用户的暂存目录，再整体下载
            # （特大分享会因网盘剩余空间不足失败，错误信息会提示改用按文件下载）
            tr = await asyncio.to_thread(
                _transfer_via_api, self._cache_get(uid)["surl"], self._cache_get(uid)["pwd"],
                self._cloud_dir_for(uid))
            if "error" in tr:
                return tr["error"]
            prog_q = queue.Queue() if self._progress_enabled else None
            dl_future = asyncio.create_task(asyncio.to_thread(
                download_from_cloud, "", self._get_max_mb(), prog_q,
                self._cloud_dir_for(uid), _user_bucket(uid)))
            if prog_q:
                while not dl_future.done():
                    try:
                        msg = prog_q.get_nowait()
                        if isinstance(msg, tuple) and msg[0] == "_error":
                            await event.send(event.plain_result(f"❌ {msg[1]}"))
                        elif isinstance(msg, str):
                            await event.send(event.plain_result(msg))
                    except queue.Empty:
                        pass
                    await self._interruptible_sleep(dl_future, self._progress_interval)
            dl = await dl_future
            if "error" in dl:
                return dl["error"]

            # 全部下载本质也是文件夹：≤阈值逐个发，否则打包 zip，超限则降级为路径+清单
            share_url = f"https://pan.baidu.com/s/{self._cache_get(uid)['surl']}"
            return await self._send_folder(event, dl, share_url, label="全部文件")
        finally:
            self._active_downloads.pop(dl_key, None)

    @filter.llm_tool(name="pan_transfer")
    async def pan_transfer(self, event: AstrMessageEvent, link: str = "", pwd: str = "", path: str = ""):
        """把百度网盘分享中的文件或文件夹转存到自己的网盘（不下载到本地）。用户说"转存一下"、"存到我的网盘"、"先转存"时调用。
        path 传 pan_list 目录树中显示的路径则只转存该项（需要网盘剩余空间大于该项大小）；不传 path 则转存分享根目录的全部内容（特大分享会因空间不足失败）。
        转存结果在网盘的暂存目录下（<配置的转存目录>/<该用户的隔离子目录>），回复用户时把转存到的完整网盘路径一起告诉用户。

        Args:
            link(string): 百度网盘分享链接（可选，如未查看过目录则需提供；也可传整段分享文案，会自动识别链接与提取码）
            pwd(string): 提取码（可选）
            path(string): 要转存的文件夹或文件在目录树中的路径（可选，不传则转存全部）
        """
        if self._is_blacklisted(event):
            return "你没有权限使用该功能。"

        surl = ""
        if link:
            surl, pwd = self._normalize_link(link, pwd)

        uid = event.get_sender_id()
        if surl:
            blocked = self._check_switch_allowed(surl, uid)
            if blocked:
                return blocked
            result = await asyncio.to_thread(self._transfer_and_list, surl, pwd, uid)
            if "error" in result:
                return result["error"]
        elif not self._cache_get(uid)["surl"]:
            return "请先查看目录: /pan <链接> [密码]"

        cloud_dir = self._cloud_dir_for(uid)
        if path:
            tr = await asyncio.to_thread(self._ensure_transferred_sync, path, uid)
            if "error" in tr:
                return tr["error"]
            return f"✅ 已转存「{path}」到网盘 {cloud_dir}/{tr['name']}"
        # 未指定 path：转存分享根目录全部内容
        tr = await asyncio.to_thread(
            _transfer_via_api, self._cache_get(uid)["surl"], self._cache_get(uid)["pwd"], cloud_dir)
        if "error" in tr:
            return tr["error"]
        names = "、".join(tr.get("filenames", [])[:10]) or "全部内容"
        return f"✅ 已转存（{names}）到网盘 {cloud_dir}"

    async def _login_flow(self, event: AstrMessageEvent, username: str, password: str):
        """/pan login 2/pwd 子命令：账密登录，全局生效"""
        if not event.is_private_chat():
            yield event.plain_result("⚠️ 密码会出现在聊天记录中，建议私聊执行以防泄露")
        yield event.plain_result("⏳ 正在登录百度网盘...")
        result = await asyncio.to_thread(login_bpcs, username, password)
        if "error" in result:
            yield event.plain_result(f"❌ {result['error']}")
            return
        yield event.plain_result("✅ 登录成功！BaiduPCS-Go 已保存登录凭证，全局生效。")
        yield event.plain_result("🔐 密码已出现在聊天记录中，建议撤回/删除该消息，防止泄露")

    def _login_menu(self) -> str:
        """/pan login 登录菜单：数字简写或完整指令两种输入都可用"""
        return (
            "百度网盘登录/退出方式（数字简写与完整指令任选其一）:\n"
            "1  扫码登录    /pan login 1  或  /pan login qr\n"
            "2  账密登录    /pan login 2 <账号> <密码>  或  /pan login pwd <账号> <密码>\n"
            "3  BDUSS       /pan login 3 <BDUSS> [STOKEN]  或  /pan login bduss <BDUSS> [STOKEN]\n"
            "4  Cookie      /pan login 4 <完整Cookies>  或  /pan login cookies <完整Cookies>\n"
            "0  退出登录    /pan login 0  或  /pan login out"
        )

    async def _login_dispatch(self, event: AstrMessageEvent, parts: list):
        """/pan login <方式> [参数]：统一登录入口，负责分发到具体登录/退出流程。"""
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
            async for r in self._login_flow(event, parts[2], parts[3]):
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
            yield event.plain_result("⏳ 正在登录...")
            result = await asyncio.to_thread(login_bduss_bpcs, bduss, stoken)
            if "error" in result:
                yield event.plain_result(f"❌ {result['error']}")
            else:
                extra = "（含STOKEN，转存可用）" if stoken else "（无STOKEN，转存可能不可用，仅下载）"
                yield event.plain_result(f"✅ 登录成功！{extra}")
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
            yield event.plain_result("⏳ 正在注入 Cookies...")
            result = await asyncio.to_thread(login_cookies_bpcs, cookie_str)
            if "error" in result:
                yield event.plain_result(f"❌ {result['error']}")
            else:
                yield event.plain_result(f"✅ 登录成功！{result.get('output', '')}")
            return

        if method in ("0", "out", "logout", "off"):
            yield event.plain_result("⏳ 正在退出登录...")
            result = await asyncio.to_thread(logout_bpcs)
            if "error" in result:
                yield event.plain_result(f"❌ {result['error']}")
            else:
                yield event.plain_result("✅ 已退出百度网盘登录，本地凭证已清除。如需重新登录请发送 /pan login 1 扫码登录")
            return

        # 未知方式：提示并展示菜单
        yield event.plain_result(f"未知的登录方式: {parts[1]}")
        yield event.plain_result(self._login_menu())

    async def _qrlogin_flow(self, event: AstrMessageEvent):
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
                except Exception:
                    pass
                # 从 session 的 cookie jar 提取全部 cookie（已含 pan 专用 cookie）
                full_cookie = "; ".join(f"{k}={cv2}" for k, cv2 in sess.cookies.items())
                logger.info(f"[baidu_netdisk] qrlogin cookie keys: {list(sess.cookies.keys())}")
                logger.info(f"[baidu_netdisk] qrlogin full_cookie len: {len(full_cookie)}")
                bduss_m = re.search(r'BDUSS=([^\s;]+)', full_cookie)
                if not bduss_m:
                    yield event.plain_result("❌ 未能提取 BDUSS，登录失败")
                    return
                # Step 4: 用完整 cookie 登录 BaiduPCS-Go
                result = await asyncio.to_thread(login_cookies_bpcs, full_cookie)
                if "error" in result:
                    # 回退到仅 BDUSS+STOKEN 方式
                    stoken_m = re.search(r'STOKEN=([^\s;]+)', full_cookie)
                    bduss_val = bduss_m.group(1)
                    stoken_val = stoken_m.group(1) if stoken_m else ""
                    result2 = await asyncio.to_thread(login_bduss_bpcs, bduss_val, stoken_val)
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
    async def on_pan(self, event: AstrMessageEvent, args: GreedyStr):
        """用法: /pan <链接或surl> [提取码]  |  /pan login 查看登录/退出菜单"""

        if self._is_blacklisted(event):
            return
        uid = event.get_sender_id()
        dl_key = f"{uid}:{str(args).strip()}"
        _lock_acquired = False
        parts = [p for p in str(args).split() if p]
        if not parts or parts[0] == "help":
            help_lines = [
                "用法:",
                "/pan <链接> [密码]  查看目录树",
                "💡 可直接粘贴整段分享文案（含“链接:”与“提取码:”），自动识别链接与提取码",
                "/pan look  重新查看目录树",
                "/pan down <文件或文件夹路径>  下载文件夹或文件（自动识别类型）",
                "/pan login  登录/退出统一入口（发送 /pan login 查看登录菜单）",
                "/pan pcs  下载/更新 BaiduPCS-Go 工具（直连失败时自动挑选可用加速镜像）",
                "/pan pcs git  强制直连 GitHub 官方原始链接（不用代理、不用镜像）",
                "/pan pcs <代理URL>  通过加速代理下载并保存（如 https://gh-proxy.org/）",
                "/pan pcs off  清除已保存的加速代理",
            ]
            yield event.plain_result("\n".join(help_lines))
            sponsor_path = os.path.join(os.path.dirname(__file__), "sponsor.png")
            if os.path.exists(sponsor_path):
                yield event.chain_result([
                    Plain("支持作者↑↑↑"),
                    Image.fromFileSystem(sponsor_path)
                ])
            return

        if parts[0].lower() == "pcs":
            global _auto_dl_failed, _github_proxy
            _auto_dl_failed = False  # 手动下载即重试自动下载标记
            if len(parts) > 1:
                arg2 = parts[1]
                if arg2 == "git":
                    # 强制只走 GitHub 官方原始链接直连：不用代理、也不用加速镜像
                    yield event.plain_result("⏳ 正在从 GitHub 官方 release 直连下载最新版 BaiduPCS-Go...")
                    result = await asyncio.to_thread(_download_github_exe, "", 3, False)
                    if "ok" in result:
                        yield event.plain_result(f"✅ {_BPCS_EXE_NAME} 下载成功！现在可以使用 /pan login 等命令了。")
                    else:
                        yield event.plain_result(
                            f"❌ 下载失败: {result.get('error', '未知错误')}\n"
                            "💡 网络不通时可改用 /pan pcs <加速镜像地址> 通过镜像下载，例如 /pan pcs https://gh-proxy.org/"
                        )
                    return
                if arg2.lower() == "off":
                    # 清除已保存的加速代理，回到「自动挑选可用镜像」的默认下载方式
                    _github_proxy = ""
                    try:
                        os.remove(_GITHUB_PROXY_FILE)
                    except OSError:
                        pass
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
            via = f"加速代理 {_github_proxy} " if _github_proxy else ""
            yield event.plain_result(f"⏳ 正在{via}从 GitHub 官方 release 下载最新版 BaiduPCS-Go...（网络不通时会自动挑选可用加速镜像，可能稍慢）")
            result = await asyncio.to_thread(_download_github_exe)
            if "ok" in result:
                used = f"（via 加速镜像 {result['via']}）" if result.get("via") else ""
                saved = " 加速代理已保存，登录时自动下载也会使用。" if _github_proxy else ""
                yield event.plain_result(f"✅ {_BPCS_EXE_NAME} 下载成功{used}！现在可以使用 /pan login 等命令了。{saved}")
            else:
                yield event.plain_result(f"❌ 下载失败: {result.get('error', '未知错误')}")
            return

        # 登录菜单是纯文本，不需要 BaiduPCS-Go，先于 exe 校验直接展示
        if parts[0] == "login" and (len(parts) < 2 or parts[1].lower() in ("help", "-h")):
            yield event.plain_result(self._login_menu())
            return

        # 以下命令都需要 BaiduPCS-Go.exe；登录类指令在 exe 缺失/损坏时自动下载
        if not _ensure_bpcs_exe():
            if parts[0] != "login":
                yield event.plain_result(f"❌ {_BPCS_EXE_NAME} 缺失或损坏，请使用 /pan help 中提供的命令进行下载或查看readme")
                return
            yield event.plain_result("⏳ BaiduPCS-Go 不存在或不可用，正在自动下载（GitHub 官方 release 优先，直连失败时自动挑选可用加速镜像）...")
            if not await asyncio.to_thread(_auto_download_bpcs):
                yield event.plain_result(
                    f"❌ BaiduPCS-Go 自动下载失败：{_auto_dl_last_error or '未知原因'}。"
                    "请稍后重试（或用 /pan pcs 手动下载）"
                )
                return

        if parts[0] == "look":
            cached_tree = self._cache_get(uid)["tree"]
            if cached_tree:
                yield event.plain_result(cached_tree)
            else:
                yield event.plain_result("请先使用 /pan <链接> [密码] 查看目录")
            return

        if parts[0] == "login":
            async for r in self._login_dispatch(event, parts):
                yield r
            return

        if parts[0] == "down":
            # 下载文件夹或单个文件（自动识别类型）；使用本用户缓存的链接
            if not self._cache_get(uid)["surl"]:
                yield event.plain_result("请先使用 /pan <链接> [密码] 查看目录")
                return
            if len(parts) < 2:
                yield event.plain_result("用法: /pan down <文件或文件夹路径>（自动识别类型）")
                return
            # 路径可能含空格，拼回完整参数
            dl_path = " ".join(parts[1:])
            # 下载锁检查
            if dl_key in self._active_downloads:
                logger.warning(f"[baidu_netdisk] on_pan: duplicate download blocked: {dl_key}")
                yield event.plain_result("⏳ 该文件正在下载中，请稍候...")
                return
            self._active_downloads[dl_key] = uid
            _lock_acquired = True
            try:
                logger.info(f"[baidu_netdisk] on_pan down: dl_path={dl_path}, progress_enabled={self._progress_enabled}")
                tr = await asyncio.to_thread(self._ensure_transferred_sync, dl_path, uid)
                if "error" in tr:
                    yield event.plain_result(f"❌ {tr['error']}")
                    return
                # 自动识别类型（目录树已给出 is_dir）：下载前先告知用户是文件夹还是文件
                kind = "文件夹" if tr.get("is_dir") else "文件"
                yield event.plain_result(f"⏳ 正在下载{kind}: {dl_path} ...")
                prog_q = queue.Queue() if self._progress_enabled else None
                dl_future = asyncio.create_task(asyncio.to_thread(
                    download_from_cloud, tr["name"], self._get_max_mb(), prog_q,
                    self._cloud_dir_for(uid), _user_bucket(uid)))
                if prog_q:
                    file_info = None
                    _last_prog_size = None
                    _last_prog_time = None
                    while not dl_future.done():
                        try:
                            msg = prog_q.get_nowait()
                            if isinstance(msg, tuple) and msg[0] == "_info":
                                file_info = (msg[1], msg[2])  # (name, size)
                            elif isinstance(msg, tuple) and msg[0] == "_error":
                                yield event.plain_result(f"❌ {msg[1]}")
                            elif isinstance(msg, str):
                                yield event.plain_result(msg)
                        except queue.Empty:
                            pass
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
                logger.info(f"[baidu_netdisk] on_pan down: download complete, dl={dl}")
                if "error" in dl:
                    logger.error(f"[baidu_netdisk] on_pan down: download error: {dl['error']}")
                    yield event.plain_result(f"❌ {dl['error']}")
                else:
                    if dl.get("is_dir"):
                        # 文件夹不能直接发送：≤阈值逐个发，否则打包 zip，超限则降级为路径+清单
                        share_url = f"https://pan.baidu.com/s/{self._cache_get(uid)['surl']}"
                        yield event.plain_result(await self._send_folder(event, dl, share_url))
                    else:
                        yield event.chain_result([File(name=dl.get("name", "file"), file=dl["path"])])
            finally:
                if _lock_acquired:
                    self._active_downloads.pop(dl_key, None)
            return

        # 默认: /pan <链接> [密码] → 查看目录树（整串交给 _normalize_link 统一解析）
        surl, pwd = self._normalize_link(" ".join(parts), "")

        if not surl:
            yield event.plain_result("❌ 无法解析链接")
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

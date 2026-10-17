"""工具层：GitHub release 拉取、镜像探测与 BaiduPCS-Go 二进制下载。"""

import hashlib
import os
import platform
import requests as _req
import subprocess
import sys
import threading
import time
import urllib.parse

from astrbot.api import logger

from ..domain.constants import _AUTO_DL_RETRY_COOLDOWN_SECONDS, BPCS_API_URL, _BPCS_EXE_NAME, BPCS_PATH, _GITHUB_DOWNLOAD_HOSTS, _GITHUB_MIRROR_HOSTS, _HOST_RE, _IS_LINUX, _IS_MACOS, _IS_WINDOWS
from ..domain.paths import _GITHUB_PROXY_FILE
from ..domain.state import state
from .fs import _atomic_write


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

_exe_ok_cache = {"sig": None}  # 已验证可用的 exe 的 (mtime, size)，避免每次请求都 spawn 进程

_auto_dl_lock = threading.Lock()  # 串行化登录时的自动下载（同一时间只允许一个下载任务）

def _allowed_proxy_hosts() -> tuple:
    """允许的加速源域名（内置镜像列表 + BAIDU_NETDISK_EXTRA_PROXIES 追加，英文逗号分隔）。"""
    extra = os.environ.get("BAIDU_NETDISK_EXTRA_PROXIES", "")
    hosts = tuple(h.strip().lower() for h in extra.split(",") if h.strip())
    return _GITHUB_MIRROR_HOSTS + hosts

def _host_is_sane(host: str) -> bool:
    """主机名是否可安全用于「后缀白名单」比较。

    urlsplit 按 RFC 3986 解析，而 requests/urllib3 把 `\\` 当路径分隔符：
    `https://evil.com\\.gh-proxy.com/x` 在 urlsplit 里的主机名以 `.gh-proxy.com` 结尾，
    能通过后缀白名单，实际连接的却是 evil.com。含反斜杠等异常字符的主机名一律不受信。"""
    return bool(_HOST_RE.match(host or ""))

def _download_host_allowed(host: str, extra: str = "") -> bool:
    """下载/跳转目标主机是否可信：GitHub 官方域名、已知加速源，外加 extra（当前使用的镜像）本身。"""
    host = (host or "").lower().rstrip(".")
    if not _host_is_sane(host):
        return False
    allowed = [a for a in list(_GITHUB_DOWNLOAD_HOSTS) + list(_allowed_proxy_hosts())
               if _host_is_sane(a)]
    if extra:
        eh = (urllib.parse.urlsplit(extra).hostname or "").lower().rstrip(".")
        if _host_is_sane(eh):
            allowed.append(eh)
    return any(host == a or host.endswith("." + a) for a in allowed)

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
    if parts.scheme not in ("http", "https") or not _host_is_sane(host):
        return "代理地址格式不正确"
    # 明文 http 下载链路可被中间人改写（含 302 到任意主机），只接受 https 加速源
    if parts.scheme != "https":
        return "代理地址必须以 https:// 开头：明文 http 加速源可被中间人篡改下载内容"
    for allowed in _allowed_proxy_hosts():
        if host == allowed or host.endswith("." + allowed):
            return None
    return ("代理只能填 GitHub 加速源，当前地址不在白名单内。可用: "
            + "、".join(_allowed_proxy_hosts()))

def _mirror_candidates() -> tuple:
    """按优先级返回待探测的加速镜像地址（上次探测成功的排最前，避免每次下载都从头探测）。"""
    hosts = list(_GITHUB_MIRROR_HOSTS)
    if state.auto_mirror:
        cached = (urllib.parse.urlsplit(state.auto_mirror).hostname or "").lower()
        if cached in hosts:
            hosts.remove(cached)
            hosts.insert(0, cached)
    return tuple(f"https://{h}" for h in hosts)

try:
    with open(_GITHUB_PROXY_FILE, "r", encoding="utf-8") as f:
        state.github_proxy = (f.read() or "").strip()
except Exception:
    pass  # 首次运行或文件被删，等价于「没保存过代理」

_saved_proxy_err = _validate_github_proxy(state.github_proxy) if state.github_proxy else None

if _saved_proxy_err:
    logger.warning(f"[baidu_netdisk] ⚠️ 已保存的加速代理不可用，已忽略: {state.github_proxy}（{_saved_proxy_err}）")
    state.github_proxy = ""

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

def _auto_download_bpcs(allow_insecure_tls: bool = False) -> bool:
    """确保 BaiduPCS-Go 可用：缺失或损坏时自动下载（GitHub 官方 release 优先，直连失败时自动挑选可用加速镜像）。
    并发安全（同一时间只允许一个下载任务）；下载失败后进入冷却期，冷却期内跳过重试，
    冷却结束后自动再试（避免每次登录都长时间空等，同时保证普通用户能自愈）；
    allow_insecure_tls 来自插件配置（是否允许证书校验失败时退回不校验模式）；
    失败原因记录到 state.auto_dl_last_error 供用户侧提示。"""

    def _in_cooldown() -> bool:
        return bool(state.auto_dl_failed) and (
            time.time() - state.auto_dl_failed_at < _AUTO_DL_RETRY_COOLDOWN_SECONDS)

    if _ensure_bpcs_exe():
        return True
    if _in_cooldown():
        logger.warning(f"[baidu_netdisk] ⚠️ BaiduPCS-Go 不可用且自动下载刚失败过，"
                       f"{_AUTO_DL_RETRY_COOLDOWN_SECONDS // 60} 分钟内跳过重试（管理员可用 /pan pcs 立即手动重试）")
        return False
    with _auto_dl_lock:
        if _ensure_bpcs_exe():
            return True
        if _in_cooldown():
            return False
        logger.info("[baidu_netdisk] BaiduPCS-Go 不存在或不可用，开始自动下载...")
        result = _download_github_exe(allow_insecure_tls=allow_insecure_tls)
        if "error" in result:
            state.auto_dl_failed = True
            state.auto_dl_failed_at = time.time()
            state.auto_dl_last_error = result["error"]
            logger.error(f"[baidu_netdisk] ❌ BaiduPCS-Go 自动下载失败: {state.auto_dl_last_error}")
            return False
        if result.get("via"):
            logger.info(f"[baidu_netdisk] BaiduPCS-Go 已通过加速镜像 {result['via']} 下载完成")
        state.auto_dl_failed = False
        state.auto_dl_last_error = ""
        return _ensure_bpcs_exe()

def _proxied_url(proxy: str, url: str) -> str:
    """给 GitHub 请求地址加上加速代理前缀（gh-proxy 类代理：代理地址 + 原始完整 URL）。proxy 为空时原样返回。"""
    if not proxy:
        return url
    return f"{proxy.rstrip('/')}/{url}"

def _disable_urllib3_warnings() -> None:
    """只在确实要发「不校验证书」请求前调用，免得日志被 InsecureRequestWarning 刷屏。"""
    import urllib3 as _u3
    _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)

def _http_get(url: str, *, headers: dict, timeout: float | int | tuple, stream: bool = False,
              extra_host: str = "",
              allow_insecure_tls: bool = False) -> tuple[_req.Response | None, str]:
    """带跳转校验的 GET：逐跳要求 https 且主机在白名单内（GitHub 官方域名 + 加速源 + extra_host），最多 3 跳。
    返回 (响应, 错误描述)，正常时错误描述为空串、失败时响应为 None。

    证书校验失败**不会**自动降级：只有调用方显式传入 allow_insecure_tls=True
    （对应配置项 allow_insecure_tls）才退回不校验证书，且每次都记 warning 留痕。"""
    hop_limit = 3
    for _hop in range(hop_limit + 1):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https":
            return None, f"拒绝非 https 下载地址：{parts.scheme or '未知'}://{parts.hostname or ''}"
        if not _download_host_allowed(parts.hostname or "", extra_host):
            # 攻击者控制的 302 想把我方下载引到别处，就在这里被拦住
            return None, f"拒绝访问未授权的下载主机：{parts.hostname or url}"
        try:
            resp = _req.get(url, timeout=timeout, headers=headers, stream=stream, allow_redirects=False)
        except _req.exceptions.SSLError as e:
            if not allow_insecure_tls:
                return None, (f"TLS 证书校验失败（{parts.hostname}）：如确认当前网络可信，"
                              f"可在插件配置中开启「允许不校验证书」后重试（{e}）")
            logger.warning(f"[baidu_netdisk] ⚠️ 已按配置关闭证书校验访问 {parts.hostname}，"
                           f"本次下载内容可被中间人篡改")
            _disable_urllib3_warnings()
            try:
                resp = _req.get(url, timeout=timeout, headers=headers, stream=stream,
                                allow_redirects=False, verify=False)
            except Exception as e2:
                return None, f"请求失败：{e2}"
        except Exception as e:
            return None, f"请求失败：{e}"
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            resp.close()
            if not location:
                return None, "跳转响应缺少 Location"
            url = urllib.parse.urljoin(url, location)
            continue
        return resp, ""
    return None, f"跳转次数超过 {hop_limit} 次，已中止"

def _set_github_proxy(proxy: str) -> str | None:
    """校验并持久化 GitHub 加速代理（供 /pan pcs <代理地址> 使用）。
    只接受白名单内的加速源（详见 _validate_github_proxy）。
    成功返回 None，地址非法返回错误描述；写文件失败只记日志（本次会话仍生效）。"""
    proxy = (proxy or "").strip().rstrip("/")
    err = _validate_github_proxy(proxy)
    if err:
        return err
    state.github_proxy = proxy
    try:
        _atomic_write(_GITHUB_PROXY_FILE, proxy)
    except OSError as e:
        logger.warning(f"[baidu_netdisk] ⚠️ 保存 GitHub 加速代理失败（{e}），本次会话内仍生效")
    return None

def _fetch_release_info(proxy: str = "", timeout: int = 30,
                        allow_insecure_tls: bool = False) -> dict | None:
    """从 GitHub API 获取官方 release 信息（下载 URL、文件大小与官方 sha256 都来自官方，不硬编码）。
    proxy 非空时通过加速代理访问 API（如 https://gh-proxy.org/https://api.github.com/...）。"""
    headers = {"User-Agent": "astrbot_plugin_baidu_netdisk", "Accept": "application/vnd.github+json"}
    api_url = _proxied_url(proxy, BPCS_API_URL)
    resp, err = _http_get(api_url, headers=headers, timeout=timeout, extra_host=proxy,
                          allow_insecure_tls=allow_insecure_tls)
    if err:
        state.last_release_error = err
        logger.error(f"[baidu_netdisk] ❌ 获取 GitHub release 信息失败: {err}")
        return None
    try:
        resp.raise_for_status()
        state.last_release_error = ""
        return resp.json()
    except Exception as e:
        state.last_release_error = str(e)
        logger.error(f"[baidu_netdisk] ❌ 获取 GitHub release 信息失败: {e}")
        return None

def _probe_mirror(url: str, timeout: int = 10, extra_host: str = "",
                  allow_insecure_tls: bool = False) -> bool:
    """试探加速镜像能否代理该资产：只读响应前 4 字节，能连上且返回压缩包/可执行文件头才算可用。
    连接失败与「镜像返回 HTML 错误页」都在这里挡掉，避免为不可用的镜像白下整包。"""
    headers = {"User-Agent": "astrbot_plugin_baidu_netdisk"}
    resp, err = _http_get(url, headers=headers, timeout=timeout, stream=True,
                          extra_host=extra_host, allow_insecure_tls=allow_insecure_tls)
    if err or resp is None:
        return False
    try:
        if resp.status_code not in (200, 206):
            return False
        head = next(resp.iter_content(4), b"")
    except Exception:
        return False
    finally:
        resp.close()
    return head[:2] in (b"PK", b"MZ")

def _extract_sha256(asset: dict) -> str:
    """取出 release 资产里的官方 sha256（GitHub 的 digest 字段形如 sha256:<hex>）。
    取不到或格式不认识时返回空串。

    注意：这是**上传的压缩包**的哈希，不是解压后可执行文件的哈希。"""
    raw = str(asset.get("digest") or "").strip().lower()
    if raw.startswith("sha256:"):
        raw = raw[len("sha256:"):]
    if len(raw) == 64 and all(c in "0123456789abcdef" for c in raw):
        return raw
    if raw:
        logger.warning(f"[baidu_netdisk] ⚠️ 无法识别的资产摘要格式，已忽略: {raw[:40]}")
    return ""

def _download_asset(via: str, asset_url: str, tag: str, suffix: str,
                    expected_size: int, max_attempts: int,
                    expected_digest: str = "", require_digest: bool = False,
                    allow_insecure_tls: bool = False) -> str:
    """下载 release 资产、解压出当前平台的可执行文件并写入本地（via 为空表示直连 GitHub）。
    成功返回空串，失败返回错误描述。

    expected_digest 非空时必须与官方 sha256 一致（比对的是压缩包整包，在解压之前）；
    require_digest=True 时缺少摘要直接拒绝——第三方来源拿不到官方完整性背书就不该采信。"""
    import zipfile as _zip, io as _io

    def _get(url: str, timeout: int = 300) -> tuple[bytes | None, str]:
        """返回 (内容, 错误描述)：失败时内容为 None，错误描述直接来自请求层或 HTTP 状态。"""
        headers = {"User-Agent": "astrbot_plugin_baidu_netdisk"}
        # 连接超时 10s、读取超时 300s：直连被墙时 10s 内就让位给加速镜像，慢速大文件仍有足够读取时间
        resp, err = _http_get(url, headers=headers, timeout=(10, timeout), extra_host=via,
                              allow_insecure_tls=allow_insecure_tls)
        if err or resp is None:
            return None, err or "请求失败"
        try:
            resp.raise_for_status()
            return resp.content, ""
        except Exception as e:
            return None, f"HTTP {resp.status_code}: {e}"

    download_url = _proxied_url(via, asset_url)
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            time.sleep(1.0 * (attempt - 1))  # 1s/2s 退避，给瞬时故障留恢复时间
        where = f"加速镜像 {via} " if via else "GitHub 官方 release "
        logger.info(f"[baidu_netdisk] 从{where}下载 BaiduPCS-Go {tag}（{suffix}，第 {attempt}/{max_attempts} 次尝试）...")
        data, get_err = _get(download_url, timeout=300)
        if data is None:
            last_err = f"下载失败（{get_err}）"
            continue
        if len(data) != expected_size:
            last_err = f"下载大小不匹配: 官方API={expected_size}, 实际={len(data)}"
            continue
        # 官方 digest 是「上传的 zip」的 sha256：在解压前校验整包，包内所有文件内容随之被认定
        if expected_digest:
            actual_digest = hashlib.sha256(data).hexdigest()
            if actual_digest != expected_digest:
                last_err = (f"压缩包 sha256 校验失败（官方 {expected_digest[:12]}…，"
                            f"实际 {actual_digest[:12]}…）")
                logger.warning(f"[baidu_netdisk] ⚠️ {where}的压缩包与官方摘要不一致，已丢弃：{last_err}")
                continue
        elif require_digest:
            last_err = "官方未提供 sha256，第三方来源的压缩包无法校验"
            logger.warning(f"[baidu_netdisk] ⚠️ {where}缺少可核对的官方 sha256，按安全策略跳过该来源")
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
                         allow_mirrors: bool = True, allow_insecure_tls: bool = False) -> dict:
    """从 GitHub 官方 release 下载当前平台对应的最新版 BaiduPCS-Go（`/releases/latest`，不固定版本）。
    国内网络下 GitHub 下载经常瞬时失败（超时/连接重置/403），对可重试的失败自动重试，最多 max_attempts 次。
    指定 proxy（或存在已保存的代理）时资产只走该加速源；未配置代理时先直连 GitHub，直连失败后
    依次探测候选加速镜像（见 _GITHUB_MIRROR_HOSTS）并改用可用的那个，各平台通用。
    release 信息（含官方 sha256）始终优先直连 api.github.com，只有直连不通才退到代理/镜像；
    压缩包与官方 sha256 不一致时丢弃并换下一个来源，第三方来源缺少 sha256 时直接跳过。
    返回 {"ok": True, "via": 镜像, "digest_verified": bool, "info_trusted": bool, "info_source": str}
    或 {"error": "..."}；allow_mirrors=False 表示强制直连不走镜像；
    allow_insecure_tls=True（配置项）时证书校验失败才允许退回不校验模式。"""

    if proxy is None:
        proxy = state.github_proxy
    if proxy:
        proxy_err = _validate_github_proxy(proxy)
        if proxy_err:
            return {"error": proxy_err}
    mirrors = _mirror_candidates() if (allow_mirrors and not proxy) else ()

    suffix = _bpcs_asset_suffix()
    if not suffix:
        return {"error": f"当前平台（{sys.platform}/{platform.machine()}）暂不支持自动下载 BaiduPCS-Go，请手动获取对应平台的二进制"}

    info = None
    info_trusted = False
    info_source = ""
    # release 信息（含官方 sha256 摘要）优先直连 api.github.com：摘要只在可信来源手里才有意义，
    # 走代理/镜像拿到的摘要与它提供的载荷同源，无法抵御该来源替换压缩包
    info_sources = [("", True)]
    if proxy:
        info_sources.append((proxy, False))
    else:
        info_sources += [(m, False) for m in mirrors]
    for src, trustworthy in info_sources:
        if src:
            info = _fetch_release_info(src, timeout=10, allow_insecure_tls=allow_insecure_tls)
        else:
            # 有后备来源时直连只等 15 秒：被墙时没必要让用户干等 30 秒
            info = _fetch_release_info("", timeout=15 if len(info_sources) > 1 else 30,
                                       allow_insecure_tls=allow_insecure_tls)
        if info:
            info_trusted, info_source = trustworthy, src
            if trustworthy:
                logger.info("[baidu_netdisk] 已从 GitHub 官方 API 获取 release 信息（含官方 sha256）")
            else:
                logger.warning(f"[baidu_netdisk] ⚠️ GitHub 官方 API 不可用，release 信息（含 sha256）改由 "
                               f"{src} 提供；该来源可同时替换校验信息与压缩包，本次下载无法抵御它")
            break
    if not info:
        # 证书链不完整时只报「检查网络」会让人无从下手：把可操作的那条线索带出来
        hint = ""
        if "证书校验失败" in state.last_release_error:
            hint = "（TLS 证书校验失败：如确认当前网络可信，可在插件配置中开启「允许不校验证书」后重试）"
        return {"error": "GitHub 官方 release 获取失败，请检查网络后重试" + hint}
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
    digest = _extract_sha256(asset)

    # API 都是靠镜像拿到的，说明直连基本不通，直连下载只试一次即可，省得白等重试
    routes = [(proxy, max_attempts if info_trusted else 1, False)]
    routes += [(m, 1, True) for m in mirrors]
    errors = []
    for via, attempts, need_probe in routes:
        if need_probe:
            # 先用几字节探针确认镜像真能代理该资产，再决定是否整包下载（不可用的直接跳过）
            if not _probe_mirror(_proxied_url(via, asset["browser_download_url"]),
                                 extra_host=via, allow_insecure_tls=allow_insecure_tls):
                logger.info(f"[baidu_netdisk] 加速镜像 {via} 探测未通过，尝试下一个")
                errors.append(f"{via}: 不可用")
                continue
            logger.info(f"[baidu_netdisk] 直连下载失败，改用加速镜像 {via}")
        # 第三方来源必须能被官方摘要核对（require_digest），否则宁可不下载
        err = _download_asset(via, asset["browser_download_url"], tag, suffix, asset["size"], attempts,
                              expected_digest=digest, require_digest=bool(via),
                              allow_insecure_tls=allow_insecure_tls)
        if not err:
            if need_probe:
                state.auto_mirror = via  # 记住可用镜像，本次运行后续下载优先复用
            if digest:
                logger.info(f"[baidu_netdisk] 压缩包 sha256 已与 GitHub 官方信息核对一致（{digest[:12]}…）")
            return {"ok": True, "via": via, "digest_verified": bool(digest),
                    "info_trusted": info_trusted, "info_source": info_source}
        errors.append(f"{'直连' if not via else via}: {err}")
    # 直连错误最有诊断价值，始终保留；镜像错误只留最后两条，避免报错糊成一团
    if len(errors) > 3:
        errors = errors[:1] + errors[-2:]
    return {"error": "；".join(errors)}

def _digest_note(result: dict) -> str:
    """把本次下载的完整性校验情况翻译成一句用户可读的说明。"""
    if result.get("digest_verified"):
        return "\n🔒 压缩包 sha256 已与 GitHub 官方信息核对一致"
    if result.get("info_trusted"):
        return "\n⚠️ GitHub 未提供 sha256，本次未校验压缩包（来源为 GitHub 官方直连）"
    return (f"\n⚠️ 本次校验信息由 {result.get('info_source') or '第三方来源'} 提供，"
            f"无法抵御该来源替换文件")

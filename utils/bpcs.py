"""工具层：BaiduPCS-Go 配置、登录/退出与网盘会话构建。"""

import contextlib
import json as _json
import os
import re
import requests as _req
import subprocess
import threading

from astrbot.api import logger

from ..domain.constants import _BPCS_EXE_NAME, BPCS_PATH, _BROWSER_UA, _IS_WINDOWS, _LOGIN_ERR_RE, _LOGIN_ERR_RE_PWD, _MSG_BPCS_UNAVAILABLE
from ..domain.paths import PLUGIN_DATA_DIR
from ..domain.state import state
from .fs import _atomic_write, _atomic_write_json
from .github import _auto_download_bpcs, _ensure_bpcs_exe


_bpcs_lock = threading.Lock()

def _bpcs_env() -> dict:
    """BaiduPCS-Go 子进程的补充环境变量。

    凭证统一放在插件数据目录（见 _bpcs_config_path），但 BPCS 自己启动时只按
    BAIDUPCS_GO_CONFIG_DIR → 系统用户目录的顺序找配置；不强制指向数据目录的话，
    who/login/logout/download 等子进程会各自读写系统目录里的旧配置，登录态就
    落到了 AstrBot 数据卷之外，Docker 重建容器即丢失。"""
    return {**os.environ, "BAIDUPCS_GO_CONFIG_DIR": PLUGIN_DATA_DIR}

def _run_bpcs(args: list, timeout: int = 300) -> tuple:
    try:
        r = subprocess.run(
            [BPCS_PATH] + args, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            env=_bpcs_env(),
        )
        return r.stdout or "", r.stderr or "", r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
    except Exception as e:
        return "", str(e), -2

def _init_bpcs() -> bool:
    """检查本地是否已有可用的登录凭证（直接读数据目录里的 pcs_config.json）。

    此前用 who 命令的输出判断登录态，但 BaiduPCS-Go 的 who 无论是否登录都输出
    「当前帐号 uid: N, ...」且退出码 0（未登录 N=0），按「输出不含 登录/未登录
    即已登录」判断永远命中「已登录」，未登录也会打出误导日志。现在改读配置里
    当前账号的凭证字段，与下载/转存实际使用的判据保持一致。"""
    if state.bpcs_inited:
        return True
    with _bpcs_lock:
        if state.bpcs_inited:
            return True
        if not _ensure_bpcs_exe():
            logger.warning(f"[baidu_netdisk] ⚠️ {_BPCS_EXE_NAME} 不可用，登录时会自动尝试下载（管理员也可用 /pan pcs 手动下载）")
            return False
        user = _first_bpcs_user(_load_bpcs_config(quiet=True) or {})
        if user.get("cookies") or user.get("bduss"):
            state.bpcs_inited = True
            logger.info(f"[baidu_netdisk] BaiduPCS-Go logged in: uid={user.get('uid')}, name={user.get('name')}")
            return True
        logger.info("[baidu_netdisk] BaiduPCS-Go not logged in, use /pan login")
        return False

def _legacy_bpcs_config_paths() -> list[str]:
    """v2.1 及更早版本可能存放 pcs_config.json 的候选位置（一次性迁移用）。

    顺序即优先度：exe 同目录（历史默认）→ BAIDUPCS_GO_CONFIG_DIR 指向的目录
    （README 曾推荐）→ 系统用户目录（Windows %APPDATA% / Linux macOS ~/.config）。"""
    exe_dir = os.path.dirname(os.path.abspath(BPCS_PATH))
    paths = [os.path.join(exe_dir, "pcs_config.json")]
    env = os.environ.get("BAIDUPCS_GO_CONFIG_DIR")
    if env:
        base = env if os.path.isabs(env) else os.path.join(exe_dir, env)
        paths.append(os.path.join(base, "pcs_config.json"))
    if _IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(os.path.join(appdata, "BaiduPCS-Go", "pcs_config.json"))
    else:
        home = os.path.expanduser("~")
        if home and home != "~":
            paths.append(os.path.join(home, ".config", "BaiduPCS-Go", "pcs_config.json"))
    return paths

def _bpcs_config_path() -> str:
    """BaiduPCS-Go 的 pcs_config.json 路径（登录凭证），固定放在插件数据目录。

    历史版本把凭证存到 BPCS 的默认位置（系统用户目录等），不在 AstrBot 的 data
    卷上，Docker 重建容器就丢登录态。现在统一收进插件数据目录（data/plugin_data/
    <插件名>/，随 AstrBot 数据卷持久化）；首次运行时若旧位置已有配置，自动原子
    迁移过来一次，避免升级后要求用户重新登录。

    同时 _bpcs_env() 会给所有 BPCS 子进程注入 BAIDUPCS_GO_CONFIG_DIR 指向本目录，
    保证子进程（who/login/logout/download）与插件读到的是同一份凭证。"""
    new_path = os.path.join(PLUGIN_DATA_DIR, "pcs_config.json")
    if os.path.exists(new_path):
        return new_path
    for old in _legacy_bpcs_config_paths():
        if not os.path.exists(old):
            continue
        try:
            with open(old, "r", encoding="utf-8", errors="replace") as f:
                _atomic_write(new_path, f.read(), mode=0o600)
            logger.info(f"[baidu_netdisk] 已把登录凭证从旧位置迁移到插件数据目录: {old} → {new_path}")
        except OSError as e:
            logger.warning(f"[baidu_netdisk] ⚠️ 登录凭证迁移失败（{old} → {new_path}）: {e}")
        return new_path
    return new_path

def _fetch_uid_by_bduss(bduss: str, stoken: str = "", cookies: str = "") -> tuple[str, str]:
    """查一次网盘账号信息，返回 `(uid, 昵称)`；查不到返回 `("", "")`。

    登录要写进 pcs_config.json 的 uid，BaiduPCS-Go 自己的 login 是调贴吧接口拿的；
    这里换成网盘接口，因为 login 只能从命令行收凭证（同机用户 ps / /proc/<pid>/cmdline 可见），
    而写配置这条路需要我们先把 uid 问出来。请求失败按「无法确认」处理，由调用方回退。"""
    url = "https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo&clienttype=0"
    try:
        sess = _req.Session()
        sess.headers.update({"User-Agent": _BROWSER_UA})
        if cookies:
            _apply_baidu_cookies(sess, cookies)
        else:
            sess.cookies.set("BDUSS", bduss, domain=".baidu.com")
            if stoken:
                sess.cookies.set("STOKEN", stoken, domain=".baidu.com")
        j = sess.get(url, timeout=10).json()
    except Exception as e:
        logger.warning(f"[baidu_netdisk] ⚠️ 查询账号 uid 失败（稍后回退命令行登录）: {e}")
        return "", ""
    uid = str(j.get("uk") or "").strip()
    try:
        errno = int(j.get("errno", -1))
    except (TypeError, ValueError):
        errno = -1
    if errno != 0 or not uid.isdigit():
        logger.warning(f"[baidu_netdisk] ⚠️ 账号 uid 查询未通过: errno={j.get('errno')}")
        return "", ""
    name = str(j.get("netdisk_name") or j.get("baidu_name") or "").strip()
    return uid, name

def _require_bpcs(allow_insecure_tls: bool = False) -> str | None:
    """确保 BaiduPCS-Go 可用；返回错误文案，可用时返回 None。

    四个登录/退出入口的前置守卫：可用性判定与错误文案必须一致，否则
    用户在不同入口会看到不同的失败原因。

    这里只判可用性、不预跑 `who`：四个入口原本都只调 _auto_download_bpcs，
    多跑一次子进程最多让人白等 15 秒，返回值也没被任何判定使用。"""
    if _auto_download_bpcs(allow_insecure_tls):
        return None
    return _MSG_BPCS_UNAVAILABLE

def _login_via_config(bduss: str, stoken: str = "", cookies: str = "") -> dict:
    """直接把凭证写进 pcs_config.json 完成登录；成功返回 `{"success": True}`。

    BaiduPCS-Go 只在 `login -bduss=`/`-cookies=` 里收凭证，凭证因此必然出现在进程命令行里。
    这里改为复刻它官方登录流程的落盘结果（写一条用户记录，并把 baidu_active_uid 指向该 uid，
    BPCS 启动时按 uid 载入当前账号），然后用 who 验证，凭证全程不进命令行。

    任何一步不成立都返回 `{"error": ...}` 并**完整还原原配置**，由调用方回退命令行登录：
    登录是插件的命脉，宁可退到有暴露风险的老路径，也不能留下半登录状态。"""
    uid, name = _fetch_uid_by_bduss(bduss, stoken, cookies)
    if not uid:
        return {"error": "未能确认账号 uid"}
    config_path = _bpcs_config_path()
    if not os.path.exists(config_path):
        # 首次登录时配置文件还不存在：先跑一次 who 让 BPCS 自己生成（字段齐全、权限 0600）
        _run_bpcs(["who"], timeout=15)
    if not os.path.exists(config_path):
        return {"error": f"未找到 {config_path}"}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            original = f.read()
        cfg = _json.loads(original)
        if not isinstance(cfg, dict):
            return {"error": "配置文件格式不正确"}
    except Exception as e:
        return {"error": f"读取配置失败: {e}"}

    entry = {
        "uid": int(uid), "name": name, "sex": "default", "age": 0,
        "bduss": bduss, "ptoken": "", "stoken": stoken, "baiduid": "",
        "sboxtkn": "", "cookies": cookies, "accesstoken": "", "workdir": "/",
    }
    users = cfg.get("baidu_user_list")
    users = [u for u in users if isinstance(u, dict)] if isinstance(users, list) else []
    cfg["baidu_user_list"] = [u for u in users if str(u.get("uid")) != uid] + [entry]
    cfg["baidu_active_uid"] = int(uid)

    try:
        _atomic_write_json(config_path, cfg, indent=1, mode=0o600)
    except OSError as e:
        return {"error": f"写入配置失败: {e}"}

    out, err, _ = _run_bpcs(["who"], timeout=15)
    text = ((out or "") + " " + (err or "")).strip()
    if f"uid: {uid}" not in (out or ""):
        with contextlib.suppress(OSError):
            _atomic_write(config_path, original, mode=0o600)
        return {"error": f"写入配置后校验未通过: {text[:120]}"}
    return {"success": True}

def _bpcs_login_result(out: str, err: str, code: int, log_tag: str, *,
                       err_re: str = "", with_output: bool = False) -> dict:
    """解释 `_run_bpcs(["login", ...])` 的结果，统一成功判定与失败文案。

    成功判据 `code == 0 且输出中没有「失败/错误」` 在三处登录里完全相同；
    失败时用于提取原因的正则各方式不同（账密登录的提示格式更宽），故由参数传入，
    不要归一——归一会让账密登录的错误提示文本变化。"""
    text = (out or "") + chr(10) + (err or "")
    if code == 0 and "失败" not in text and "错误" not in text:
        state.bpcs_inited = True
        logger.info(f"[baidu_netdisk] BaiduPCS-Go logged in via {log_tag}")
        if with_output:
            return {"success": True, "output": text.strip()[:300]}
        return {"success": True}
    m = re.search(err_re or _LOGIN_ERR_RE, text)
    if m:
        return {"error": f"登录失败: {m.group(0).strip()}"}
    return {"error": f"登录失败（退出码 {code}）: {text.strip()[:200]}"}

def login_bduss_bpcs(bduss: str, stoken: str = "", allow_insecure_tls: bool = False) -> dict:
    """用 BDUSS + STOKEN 登录 BaiduPCS-Go。"""
    guard_err = _require_bpcs(allow_insecure_tls)
    if guard_err:
        return {"error": guard_err}
    # 优先写配置文件登录：login 只能从命令行收凭证，而同机任何用户都能读到进程命令行
    via_config = _login_via_config(bduss, stoken)
    if "success" in via_config:
        state.bpcs_inited = True
        logger.info("[baidu_netdisk] BaiduPCS-Go logged in via /pan login 3/bduss（配置写入，凭证未进命令行）")
        return {"success": True}
    logger.info(f"[baidu_netdisk] 写配置登录不可用（{via_config.get('error')}），回退命令行登录")
    args = ["login", f"-bduss={bduss}"]
    if stoken:
        args.append(f"-stoken={stoken}")
    out, err, code = _run_bpcs(args, timeout=30)
    return _bpcs_login_result(out, err, code, "/pan login 3/bduss")

def _patch_config_stoken(stoken: str, bduss: str = "") -> None:
    """手动补写 pcs_config.json 中的 stoken 和 bduss 字段。
    login -cookies= 不会自动填充 stoken 字段，但 transfer 需要它。"""
    config_path = _bpcs_config_path()
    if not os.path.exists(config_path):
        logger.warning(f"[baidu_netdisk] ⚠️ pcs_config.json not found at {config_path}")
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        user = _first_bpcs_user(cfg)
        if user:
            if stoken:
                user["stoken"] = stoken
            if bduss:
                user["bduss"] = bduss
            # 这份配置 BaiduPCS-Go 自己也会读写，保持 indent=4 以免两边来回改格式
            _atomic_write_json(config_path, cfg, indent=4)
            logger.info(f"[baidu_netdisk] patched stoken into config")
    except Exception as e:
        logger.warning(f"[baidu_netdisk] ⚠️ failed to patch config: {e}")

def login_cookies_bpcs(cookie_str: str, allow_insecure_tls: bool = False) -> dict:
    """用完整 Cookie 字符串登录 BaiduPCS-Go（v4.0.1 支持 -cookies 参数）。"""
    guard_err = _require_bpcs(allow_insecure_tls)
    if guard_err:
        return {"error": guard_err}
    # 清洗：去掉可能干扰的空名项（如 =value）和 *_BFESS 字段
    cleaned = "; ".join(
        part.strip() for part in cookie_str.split(";")
        if part.strip() and "=" in part.strip()
        and "_BFESS" not in part
    )
    stoken_m = re.search(r'STOKEN=([^\s;]+)', cleaned)
    bduss_m = re.search(r'BDUSS=([^\s;]+)', cleaned)
    if bduss_m:
        # 同 bduss 登录：优先写配置文件，凭证不进命令行（stoken 也一起写，省掉事后的补写）
        via_config = _login_via_config(bduss_m.group(1),
                                       stoken_m.group(1) if stoken_m else "", cleaned)
        if "success" in via_config:
            state.bpcs_inited = True
            logger.info("[baidu_netdisk] BaiduPCS-Go logged in via cookies（配置写入，凭证未进命令行）")
            return {"success": True}
        logger.info(f"[baidu_netdisk] 写配置登录不可用（{via_config.get('error')}），回退命令行登录")
    out, err, code = _run_bpcs(["login", f"-cookies={cleaned}"], timeout=30)
    result = _bpcs_login_result(out, err, code, "cookies")
    # cookies 登录后 stoken 字段不会自动填充，手动补写；只在登录成功时写
    if stoken_m and "error" not in result:
        _patch_config_stoken(stoken_m.group(1), bduss_m.group(1) if bduss_m else "")
    return result

def login_bpcs(username: str, password: str, allow_insecure_tls: bool = False) -> dict:
    """用百度账号密码登录 BaiduPCS-Go，登录成功后凭证保存在本地，全局生效。
    优先走交互式 stdin 输入，避免密码出现在进程命令行（同机其他进程可见）。"""
    guard_err = _require_bpcs(allow_insecure_tls)
    if guard_err:
        return {"error": guard_err}

    # 方式一：交互式输入（密码不进命令行）
    try:
        proc = subprocess.Popen(
            [BPCS_PATH, "login"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=_bpcs_env(),
        )
        out_bytes, _ = proc.communicate(
            input=f"{username}\n{password}\n".encode("utf-8"), timeout=60
        )
        text = (out_bytes or b"").decode("utf-8", errors="replace")
        # err 传空串：stdin 方式的 stdout/stderr 已合并到同一管道，再传一次
        # 会让 output 变成两份 stdout 拼接，失败原因也会重复
        result = _bpcs_login_result(text, "", proc.returncode,
                                    "/pan login 2/pwd (stdin)", with_output=True)
        if "error" not in result:
            return result
        logger.info(f"[baidu_netdisk] stdin login 未成功，回退参数方式: rc={proc.returncode}")
    except Exception as e:
        logger.warning(f"[baidu_netdisk] ⚠️ stdin login 异常，回退参数方式: {e}")

    # 方式二：参数式（兜底）
    out, err, code = _run_bpcs(
        ["login", f"-username={username}", f"-password={password}"], timeout=60
    )
    return _bpcs_login_result(out, err, code, "/pan login 2/pwd",
                              err_re=_LOGIN_ERR_RE_PWD, with_output=True)

def logout_bpcs(allow_insecure_tls: bool = False) -> dict:
    """退出 BaiduPCS-Go 当前登录的百度帐号，清除本地凭证。"""
    guard_err = _require_bpcs(allow_insecure_tls)
    if guard_err:
        return {"error": guard_err}
    out, err, code = _run_bpcs(["logout", "-y"], timeout=30)
    text = (out or "") + chr(10) + (err or "")
    if code == 0:
        state.bpcs_inited = False
        logger.info("[baidu_netdisk] BaiduPCS-Go logged out via /pan login out")
        return {"success": True}
    return {"error": f"退出失败（退出码 {code}）: {text.strip()[:200]}"}

def _load_bpcs_config(quiet: bool = False) -> dict | None:
    """读取 pcs_config.json 为 dict；文件不存在或格式不对返回 None。

    只合并读侧：各处写回格式不同（缩进/权限），写回必须各留各的。"""
    cfg_path = _bpcs_config_path()
    if not os.path.exists(cfg_path):
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
    except Exception as e:
        if not quiet:
            logger.warning(f"[baidu_netdisk] ⚠️ failed to read config: {e}")
        return None
    return cfg if isinstance(cfg, dict) else None

def _first_bpcs_user(cfg: dict) -> dict:
    """取 pcs_config.json 里的当前账号记录（无则空 dict）。

    以 baidu_active_uid 为准（与 BaiduPCS-Go 的 ActiveUser 一致），找不到再退回
    列表第一项（兼容旧配置）。不能只取 users[0]：每次登录都是「去重同 uid 后
    追加到列表末尾」，先后用两个账号登录后首项是旧账号，API 会话只会用它，
    而 BPCS 子进程用的是 active 账号，两边就各干各的了。"""
    users = cfg.get("baidu_user_list")
    if isinstance(users, list):
        active_uid = cfg.get("baidu_active_uid")
        for u in users:
            if isinstance(u, dict) and str(u.get("uid")) == str(active_uid):
                return u
        for u in users:
            if isinstance(u, dict):
                return u
    return {}

def _apply_baidu_cookies(sess: _req.Session, cookies_str: str) -> None:
    """把 `k=v; k2=v2` 形式的 cookie 串逐个装进 session。

    是否走「单 BDUSS 兜底」的判据由调用方决定，这里只管拆分装配。"""
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            sess.cookies.set(k, v, domain=".baidu.com")

def _build_pan_session() -> object:
    """从 pcs_config.json 读取 cookies，构建已登录的 requests.Session。
    自动访问 pan.baidu.com/disk/main 获取 csrfToken / PANPSC 等 pan 专用 cookie。"""
    cfg = _load_bpcs_config()
    if not cfg:
        logger.warning("[baidu_netdisk] ⚠️ pcs_config.json not found, cannot build pan session")
        return None
    user = _first_bpcs_user(cfg)
    cookies_str = user.get("cookies", "") or user.get("bduss", "")
    if not cookies_str:
        logger.warning("[baidu_netdisk] ⚠️ no cookies/bduss in config")
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
        _apply_baidu_cookies(sess, cookies_str)
    # 访问 pan.baidu.com 获取 pan 专用 cookie（csrfToken / PANPSC）
    try:
        sess.get("https://pan.baidu.com/disk/main", timeout=10)
    except Exception as e:
        # 预热只为拿 pan 专用 cookie，失败不致命：后续请求仍会用已有 cookie 继续尝试
        logger.warning(f"[baidu_netdisk] ⚠️ 预热 pan cookie 失败: {e}")
    return sess

def _get_account_folder() -> str:
    """从 BPCS 配置读取当前登录账号的 uid 和 name，返回账号文件夹名（如 620186943_hitomi999）。"""
    # 读不到就退化成不带名字的隔离目录，不影响下载
    user = _first_bpcs_user(_load_bpcs_config(quiet=True) or {})
    uid = user.get("uid", "")
    name = user.get("name", "")
    if uid and name:
        return f"{uid}_{name}"
    return ""

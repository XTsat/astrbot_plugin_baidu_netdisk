"""核心层：百度网盘 Web API（分享预检、转存、目录树）。"""

import re
import requests as _req
import threading
import time

from astrbot.api import logger

from ..domain.constants import _BPCS_LS_RE, _MSG_NEED_LOGIN, _TREE_DISPLAY_LIMIT_PER_DIR, _TREE_MAX_ITEMS
from ..utils.bpcs import _build_pan_session
from ..utils.size import _format_size


_share_switch_lock = threading.Lock()  # 串行化“切换分享链接”（rm 旧转存 + 转存新链接 + 更新缓存）

def _grab_share_field(html: str, key: str) -> str:
    """从分享页 HTML 中提取单个字段值（兼容引号/数值两种形式）。
    不做整页 JSON 解析，避免正则改写 URL/时间等内容导致解析失败。"""
    m = re.search(rf'["\']{re.escape(key)}["\']\s*:\s*["\']?([A-Za-z0-9_\-.]*)', html or "")
    return m.group(1) if m else ""

def _get_bdstoken(sess: _req.Session) -> str:
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
        logger.warning(f"[baidu_netdisk] ⚠️ gettemplatevariable 失败: {e}")
    return ""

def _resolve_share_via_page(sess: _req.Session, surl: str, share_link: str) -> dict:
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
                # 其余网络异常（连接失败/SSL 等）不能往上抛：会一路冒到命令层，
                # 用户只看到框架的通用报错，这里转成可读提示
                logger.warning(f"[baidu_netdisk] ⚠️ 分享页解析兜底失败: {e}")
                return {"error": f"访问百度网盘失败（{type(e).__name__}），请检查服务器网络后重试"}
            logger.warning(f"[baidu_netdisk] ⚠️ 分享页请求失败，重试: {e}")
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

def _share_api_preflight(sess: _req.Session, surl: str, pwd: str, share_link: str) -> dict:
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
        logger.warning(f"[baidu_netdisk] ⚠️ shorturlinfo 请求失败，回退分享页解析: {e}")
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
        try:
            r2 = sess.post(verify_url, data={
                "pwd": pwd, "vcode": "null", "vcode_str": "null", "bdstoken": bdstoken
            }, headers=headers, timeout=20)
            resp2 = r2.json()
        except (_req.exceptions.RequestException, ValueError) as e:
            logger.warning(f"[baidu_netdisk] ⚠️ 提取码验证请求失败: {e}")
            return {"error": f"提取码验证请求失败（{type(e).__name__}），请检查网络后重试"}
        if resp2.get("errno") != 0:
            if resp2.get("errno") == -9:
                return {"error": "提取码错误"}
            show = resp2.get("show_msg") or resp2.get("errno")
            return {"error": f"密码验证失败: {show}"}
    return {"shareid": shareid, "uk": share_uk, "bdstoken": bdstoken,
            "short": surl[1:] if surl.startswith("1") else surl}

def _api_dir_list_errno(sess: _req.Session, bdstoken: str, dir_path: str) -> int:
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
        logger.warning(f"[baidu_netdisk] ⚠️ api/list 检查目录 {dir_path} 失败: {e}")
        return -1

def _ensure_remote_dir(sess: _req.Session, bdstoken: str, target_path: str) -> str:
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
            logger.warning(f"[baidu_netdisk] ⚠️ 创建网盘目录 {cur} 失败: {e}")
            return f"创建网盘目录 {cur} 失败: {e}"
        if r.get("errno") != 0:
            show = r.get("show_msg") or r.get("errno")
            logger.warning(f"[baidu_netdisk] ⚠️ 创建网盘目录 {cur} 失败: {show}")
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
        return {"error": _MSG_NEED_LOGIN}

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
            logger.warning(f"[baidu_netdisk] ⚠️ transfer errno=2, show_msg={show or '(空)'}")
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

def _parse_ls_line(line: str) -> tuple[str, str, bool] | None:
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
        return {"error": _MSG_NEED_LOGIN}
    share_link = f"https://pan.baidu.com/s/{surl}"
    try:
        pf = _share_api_preflight(sess, surl, pwd, share_link)
    except Exception as e:
        # 前置接口的意外异常不能冒到命令层：否则 /pan <链接> 只抛框架通用错误，用户看不到原因
        logger.error(f"[baidu_netdisk] ❌ 分享前置检查异常: {e}")
        return {"error": f"访问百度网盘失败（{type(e).__name__}），请稍后重试"}
    if "error" in pf:
        return pf
    bdstoken, short = pf["bdstoken"], pf["short"]

    lines, items = [], []
    state = {"truncated": False}

    def _walk(dir_rel: str, api_dir: str, depth: int) -> None:
        if len(items) >= _TREE_MAX_ITEMS:
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
                logger.warning(f"[baidu_netdisk] ⚠️ share/list {dir_rel} errno={j.get('errno')}")
                return
        except Exception as e:
            logger.warning(f"[baidu_netdisk] ⚠️ share/list {dir_rel or '根目录'} 失败: {e}")
            return
        prefix = "  " * depth
        shown = 0
        omitted = 0
        for f in lst:
            name = f.get("server_filename") or ""
            if not name:
                continue
            if len(items) >= _TREE_MAX_ITEMS:
                state["truncated"] = True
                break
            is_dir = str(f.get("isdir")) == "1"
            rel = f"{dir_rel}/{name}" if dir_rel else name
            # 无论是否显示都记入 items：/pan down <路径> 靠这份清单解析路径，
            # 只记显示出来的会让第 61 项之后的文件既看不到也下不了
            items.append({"path": rel, "name": name,
                          "size": "-" if is_dir else str(f.get("size", 0)),
                          "is_dir": is_dir, "fs_id": str(f.get("fs_id"))})
            if shown >= _TREE_DISPLAY_LIMIT_PER_DIR:
                # 超出显示上限：不再显示、也不再递归展开（该项仍可用 /pan down 按路径下载）
                omitted += 1
                continue
            shown += 1
            if is_dir:
                lines.append(f"{prefix}📁 {name}/")
                _walk(rel, f.get("path") or "", depth + 1)
            else:
                lines.append(f"{prefix}📄 {name}  ({_format_size(str(f.get('size', 0)))})")
        if omitted:
            lines.append(f"{prefix}… 本目录另有 {omitted} 项未显示（可用 /pan down 加完整路径下载）")

    _walk("", "", 0)
    if not lines:
        return {"error": "目录为空或获取失败"}
    if state["truncated"]:
        lines.append(f"（目录过多，仅列出前 {_TREE_MAX_ITEMS} 项；其余内容可用 /pan down 加完整路径下载）")
    return {"text": "\n".join(lines), "items": items}

def list_share_content(surl: str, pwd: str = "") -> dict:
    """列出分享目录树（API 直连，不转存、不占网盘空间）。"""
    result = _list_share_tree_api(surl, pwd)
    if "error" in result:
        return result
    return {"text": result["text"], "items": result["items"]}

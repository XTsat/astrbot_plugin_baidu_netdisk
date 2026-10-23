"""领域常量层：不可变常量、用户文案、正则、单位表、host 白名单。

最底层，不依赖本插件其它模块。"""

import os
import re
import sys


_IS_WINDOWS = sys.platform.startswith("win")

_IS_MACOS = sys.platform == "darwin"

_IS_LINUX = sys.platform.startswith("linux")

_BPCS_EXE_NAME = "BaiduPCS-Go.exe" if _IS_WINDOWS else "BaiduPCS-Go"

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 本文件在 <插件根>/domain/

BPCS_PATH = os.path.join(PLUGIN_DIR, _BPCS_EXE_NAME)

BPCS_REPO = "qjfoidnh/BaiduPCS-Go"

BPCS_API_URL = f"https://api.github.com/repos/{BPCS_REPO}/releases/latest"

_MAX_CONCURRENT_DOWNLOADS = 2  # 同时进行的下载任务上限（每个任务一个 BaiduPCS-Go 进程，抢同一份带宽与磁盘）

_PARTIAL_GRACE_SECONDS = 300

_INTERRUPT_POLL_SECONDS = 2

_USER_LOCK_MAX_ENTRIES = 200  # 用户级串行锁最多保留多少条，超出时清理空闲条目

DEFAULT_CLOUD_SAVE_DIR = "/我的资源/AutoTransfer"

DEFAULT_USER_DIR_MODE = "hash"  # 默认用哈希命名用户隔离子目录，不把 QQ 号等原始标识写进网盘目录名

USER_DIR_MODE_RAW = "raw"  # 直接用发送者原始 ID（如 QQ 号）命名，便于在网盘里按用户核查

USER_DIR_MODES = (DEFAULT_USER_DIR_MODE, USER_DIR_MODE_RAW)

FLASH_TASK_LIMIT_MB = 200

_ZIP_MAX_MB_DEFAULT = 10240  # 文件夹打包上限默认值（MB，10GB 对应 QQ 闪传大文件通道）；可在配置中修改，0=不限制

_SEND_FILES_THRESHOLD_DEFAULT = 3  # 文件夹内文件数 ≤ 此值时逐个发送（原始文件更易命中秒传），超过则打包 zip

_DOWNLOAD_HARD_CAP_SECONDS = 12 * 3600

_MSG_BLACKLISTED = "你没有权限使用该功能。"

_MSG_BAD_LINK = "无法解析链接"

_MSG_NEED_LIST = "请先查看目录: /pan <链接> [密码]"

_MSG_BUSY = "⏳ 该文件正在下载中，请稍候..."

_MSG_NEED_LOGIN = "无法获取百度网盘登录凭证，请先使用 /pan login 1 登录"

_MSG_LINK_SWITCHED = "分享链接已切换，本次下载已取消，请重新查看目录后再试"

_MSG_BPCS_UNAVAILABLE = (
    f"{_BPCS_EXE_NAME} 不可用且自动下载失败，请稍后重试（或联系管理员用 /pan pcs 手动下载）"
)

_LOGIN_ERR_RE = r"错误代码:\s*\d+[^\n]{0,60}|[\u4e00-\u9fa5]{2,20}[:：]?\s*[^\n]{0,60}"

# 字符类需含 : 与中文标点：百度错误消息如「错误代码: 50052, 消息: 系统繁忙，请稍候再试(50052)」，
# 缺了会截断在第一个冒号处，用户只看到半句
_LOGIN_ERR_RE_PWD = r"[\u4e00-\u9fa5]{2,20}[:：]?\s*[0-9a-zA-Z\u4e00-\u9fa5 ,.()：:，。；!！?？、]+"

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_AUTO_DL_RETRY_COOLDOWN_SECONDS = 600

_GITHUB_MIRROR_HOSTS = (
    "gh-proxy.com", "gh-proxy.org", "ghfast.top", "ghproxy.net",
    "ghproxy.com", "ghproxy.cc", "ghproxy.link",
    "gh-proxy.net", "ghps.cc", "gh.llkk.cc",
    "gitmirror.com", "hub.gitmirror.com",
)

_GITHUB_DOWNLOAD_HOSTS = (
    "github.com", "api.github.com", "objects.githubusercontent.com",
    "codeload.github.com", "raw.githubusercontent.com", "githubusercontent.com",
)

_HOST_RE = re.compile(r"^[a-z0-9.-]+$")

_SHARE_SURL_RE = re.compile(
    r'(?:https?://)?(?:[A-Za-z0-9-]+\.)*baidu\.com/(?:s/|share/init\?(?:surl|shorturl)=)'
    r'([A-Za-z0-9_-]+)',
    re.IGNORECASE,
)

_PWD_RE = re.compile(
    r'(?<![A-Za-z0-9])(?:pwd|password|提取码|密码|访问码)\s*[:：=]?\s*([A-Za-z0-9]{4,6})',
    re.IGNORECASE,
)

# 目录树序号：允许「3」「第3个」「3.」「#3」等写法；不带尾随内容，
# 避免把「3.mp4」这类数字开头的文件名误当成序号
_SEL_NUM_RE = re.compile(r'^\s*(?:第\s*)?#?\s*(\d+)\s*个?\s*[.、:：]?\s*$')

_SIZE_UNITS: tuple[tuple[str, int], ...] = (
    ("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024),
)

_BPCS_LS_RE = re.compile(
    r'^\s*\d+\s+\d+\s+\d+\s+(\S+)\s+'
    r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+'
    r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+'
    r'(?:[0-9a-fA-F]{32}\s+)?(.+?)\s*$'
)

_TREE_DISPLAY_LIMIT_PER_DIR = 60  # 目录树文本中每个目录最多显示多少项（避免消息过长）

_TREE_MAX_ITEMS = 300  # 目录树最多记录多少项；/pan down <序号或路径> 靠这份清单解析

_TREE_NUM_HINT = "💡 每项前面的数字是序号，下载时可直接用序号选择"

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

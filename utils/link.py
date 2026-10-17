"""工具层：百度网盘分享链接与提取码解析。"""

import urllib.parse

from ..domain.constants import _PWD_RE, _SHARE_SURL_RE


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

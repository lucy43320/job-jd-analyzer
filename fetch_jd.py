"""合规的网页抓取：只在对方允许的前提下取一页 JD 正文。

判断顺序（任何一步不过就停下，并把原因说清楚）：
  1. 只允许 http / https
  2. 明确禁止抓取的站点（工商信息平台）直接拒绝 —— 这类平台的服务条款禁止抓取，
     且已有生效判例，不属于"对方同意"的范围
  3. 读 robots.txt，用标准库 RobotFileParser 按 UA 判断该路径是否允许
  4. 抓取时带真实 UA、限超时、限体积、只取一个页面、不跟到站外
  5. 提取正文，交给用户看一眼再决定要不要用

不做的事：不登录、不绕验证码、不改 UA 伪装浏览器、不并发、不翻页、不存 cookie。
"""

from __future__ import annotations

import gzip
import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from urllib.robotparser import RobotFileParser

UA = "JobToolJDReader/1.0 (personal job analysis; single page; respects robots.txt)"

BUILD = 20261107161500

TIMEOUT = 12           # 秒
MAX_BYTES = 2_500_000  # 2.5MB，超过就停，别把内存撑爆
MAX_TEXT = 20_000      # 提取出的正文上限，再长对分析也没用

# 明确禁止抓取的站点。这些不是"技术上难"，是"条款禁止 + 有判例"，不做。
BLOCKED_HOSTS = {
    "tianyancha.com": "天眼查服务条款禁止抓取，且已有生效判例",
    "qcc.com": "企查查服务条款禁止抓取，且已有生效判例",
    "qixin.com": "启信宝服务条款禁止抓取",
    "tianyancha.com.cn": "天眼查服务条款禁止抓取",
}

# 需要登录才看得到内容的站点：技术上能连，但抓到的是登录墙，直接告诉用户没用
LOGIN_WALLED = {
    "zhipin.com": "BOSS直聘的 JD 需要登录后渲染，抓不到真实内容",
    "liepin.com": "猎聘的 JD 需要登录后渲染",
    "51job.com": "前程无忧的 JD 需要登录后渲染",
    "zhaopin.com": "智联招聘的 JD 需要登录后渲染",
    "lagou.com": "拉勾的 JD 需要登录后渲染",
    "maimai.cn": "脉脉的 JD 需要登录后渲染",
}

_robots_cache: dict[str, RobotFileParser | None] = {}


def _host_root(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _base_domain(host: str) -> str:
    """取主域：zhipin.com / www.zhipin.com / m.zhipin.com 都归到 zhipin.com。"""
    host = host.lower().split(":")[0]
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def check_url(url: str) -> tuple[bool, str]:
    """发请求之前的纯本地校验。返回 (是否通过, 说明)。"""
    url = (url or "").strip()
    if not url:
        return False, "链接是空的。"
    if not re.match(r"^https?://", url, re.I):
        return False, "只支持 http / https 链接。"
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "这个链接里没有域名。"

    root = _base_domain(host)
    for banned, why in BLOCKED_HOSTS.items():
        if root == banned or host.endswith("." + banned):
            return False, f"{why}。这类信息请手动复制文字贴进来。"
    for walled, why in LOGIN_WALLED.items():
        if root == walled or host.endswith("." + walled):
            return False, f"{why}。请在页面上复制 JD 文字粘贴进来（这样也更准）。"
    return True, ""


def _robots_allows(url: str) -> tuple[bool, str]:
    """按 robots.txt 判断。读不到时保守放行但如实告知，由用户自己判断。"""
    root = _host_root(url)
    if root in _robots_cache:
        parser = _robots_cache[root]
    else:
        parser = RobotFileParser()
        parser.set_url(root + "/robots.txt")
        try:
            req = urllib.request.Request(
                root + "/robots.txt",
                headers={"User-Agent": UA, "Accept-Encoding": "identity"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read(200_000).decode("utf-8", "replace")
            parser.parse(raw.splitlines())
        except urllib.error.HTTPError as exc:
            # 404/403 = 站点没有 robots.txt，惯例视为不限制
            parser = None if exc.code in (403, 404) else None
        except (urllib.error.URLError, TimeoutError, OSError):
            parser = None
        _robots_cache[root] = parser

    if parser is None:
        return True, "该站点没有可读的 robots.txt，按惯例视为不限制；请自行确认它的服务条款。"
    try:
        if parser.can_fetch(UA, url):
            return True, "robots.txt 允许抓取这一页。"
        return False, "robots.txt 明确不允许抓取这一页，已停止。请手动复制 JD 文字。"
    except Exception:
        return True, "robots.txt 解析异常，按不限制处理；请自行确认服务条款。"


def _decode(raw: bytes, headers) -> str:
    encoding = (headers.get("Content-Encoding") or "").lower()
    if "gzip" in encoding:
        raw = gzip.decompress(raw)
    elif "deflate" in encoding:
        raw = zlib.decompress(raw)

    charset = None
    ctype = headers.get("Content-Type") or ""
    match = re.search(r"charset=([\w-]+)", ctype, re.I)
    if match:
        charset = match.group(1)
    if not charset:
        match = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.I)
        if match:
            charset = match.group(1).decode("ascii", "ignore")
    for enc in (charset, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return raw.decode(enc, "strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _visible_text(html: str) -> str:
    """去脚本/样式/标签，取可见文字。不追求完美，够模型读懂 JD 就行。"""
    html = re.sub(r"(?is)<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    # 块级标签换行，避免把两段粘成一句
    html = re.sub(
        r"(?i)</?(p|div|br|li|tr|h[1-6]|section|article|header|footer|ul|ol|table)[^>]*>",
        "\n",
        html,
    )
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if len(ln) > 1]
    # 连续重复行（导航、页脚）只留一次
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    return "\n".join(out)[:MAX_TEXT]


def fetch(url: str) -> dict:
    """抓一页。返回 {ok, text, title, url, note} 或 {ok: False, reason, message}。"""
    ok, why = check_url(url)
    if not ok:
        return {"ok": False, "reason": "blocked", "message": why}

    allowed, note = _robots_allows(url)
    if not allowed:
        return {"ok": False, "reason": "robots", "message": note}

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status >= 400:
                return {
                    "ok": False,
                    "reason": "http",
                    "message": f"站点返回 {resp.status}，这一页拿不到。请手动复制 JD。",
                }
            raw = resp.read(MAX_BYTES + 1)
            headers = resp.headers
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "reason": "http",
            "message": f"站点返回 {exc.code}（可能需要登录或被限制访问）。请手动复制 JD 文字。",
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "reason": "network",
            "message": f"连不上：{exc.reason}。检查链接或网络后重试，也可以直接粘贴 JD。",
        }
    except (TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "reason": "network",
            "message": f"请求超时或被中断（{type(exc).__name__}）。请重试或直接粘贴 JD。",
        }

    if len(raw) > MAX_BYTES:
        return {
            "ok": False,
            "reason": "toobig",
            "message": f"页面超过 {MAX_BYTES // 1000}KB，不抓了。请手动复制 JD 正文。",
        }

    try:
        html = _decode(raw, headers)
    except Exception as exc:
        return {"ok": False, "reason": "decode", "message": f"页面解码失败：{exc}"}

    text = _visible_text(html)
    if len(text) < 80:
        return {
            "ok": False,
            "reason": "empty",
            "message": "抓到的是空壳（正文可能靠浏览器脚本渲染，或需要登录）。请手动复制 JD 文字。",
        }

    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""

    # JD 常见的段落标记：命中就只保留这一块，去掉导航和推荐位
    key = re.search(r"(岗位职责|工作职责|职位描述|任职要求|岗位要求|Job Description|Responsibilities)", text, re.I)
    if key and key.start() > 0:
        text = text[key.start():]

    return {
        "ok": True,
        "text": text.strip(),
        "title": title[:120],
        "url": url,
        "note": note,
        "chars": len(text.strip()),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
    }

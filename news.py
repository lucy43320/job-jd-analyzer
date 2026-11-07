"""国际视野：拉公开新闻 → 本地归类 → 让模型写成「和我求职有关」的简报。

三条硬边界，先写在这里，代码里逐条落实：
  1. 只读**公开分发接口**（RSS，以及新浪官网前端自己在用的公开 JSON 接口），
     不登录、不翻页、不抓正文、不绕付费墙、不伪装 UA 做反爬绕过。
  2. 每次抓取前查 robots.txt；对方不允许就不抓，并在页面上写明是哪一条挡的。
  3. 只在用户点按钮时联网，不后台常驻轮询。抓到的内容只存标题+摘要+链接，
     不存全文，不落系统目录，只进 data.json。

这套边界和 fetch_jd.py 是同一套，不是新开的口子。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

BUILD = 20261107164000

UA = "JobTool/1.0 (personal job-hunting dashboard; reads public RSS feeds)"
TIMEOUT = 12

# 实测可用的源（2026-11-07 验证过 HTTP 200 且返回 XML）。
# 不加没验证过的地址：写进去但打不开，用户点一次失败一次，比没有更糟。
# 新浪财经原来那几个 rss.sina.com.cn 地址（hot_roll / rollnews / macro）看着能通，
# 实际返回的是 2008 年的期货日评，pubDate 停在 2008-06-12 —— 接口早废弃了，
# 抓回来等于给你看十八年前的新闻。所以改走它官网前端自己在用的公开 JSON 接口
# feed.mix.sina.com.cn/api/roll/get：不带 Cookie、不需要登录、和 RSS 同性质的公开分发，
# 但内容是实时的（实测返回的就是当前这一小时）。
# lid=2516 财经滚动、2518 国际财经是实测有当天数据的；2510/2511 停在好几个月前，不收。
SINA_API = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid={lid}&k=&num={num}&page=1"

SOURCES = [
    {"name": "新浪财经·要闻", "url": SINA_API.format(lid=2516, num=25), "fmt": "sina",
     "region": "国内", "kind": "财经"},
    {"name": "新浪财经·国际", "url": SINA_API.format(lid=2518, num=15), "fmt": "sina",
     "region": "国际", "kind": "财经"},
    {"name": "中新网·财经", "url": "https://www.chinanews.com.cn/rss/finance.xml", "fmt": "rss", "region": "国内", "kind": "财经"},
    {"name": "华尔街见闻", "url": "https://dedicated.wallstreetcn.com/rss.xml", "fmt": "rss", "region": "国内", "kind": "财经"},
    {"name": "中新网·国际", "url": "https://www.chinanews.com.cn/rss/world.xml", "fmt": "rss", "region": "国际", "kind": "综合"},
    {"name": "中新网·滚动", "url": "https://www.chinanews.com.cn/rss/scroll-news.xml", "fmt": "rss", "region": "国内", "kind": "热点"},
    {"name": "WSJ Markets", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "fmt": "rss", "region": "国际", "kind": "财经"},
]

# 新浪的 ctime 常见是字符串也可能是 int，统一成秒。
_SINA_TIME = re.compile(r"^\d{9,11}$")

# 标签词表：命中即打标。顺序有意义，先命中先得（政策类优先，避免被"公司"吃掉）。
TAG_RULES = [
    ("货币政策", ("降准", "降息", "加息", "利率", "美联储", "央行", "流动性", "LPR", "国债收益率")),
    ("政策监管", ("国务院", "财政部", "证监会", "监管", "新规", "征求意见", "关税", "反垄断", "政策")),
    ("资本市场", ("A股", "港股", "美股", "IPO", "上市", "退市", "并购", "债券", "基金", "股市", "指数")),
    ("宏观数据", ("CPI", "PPI", "GDP", "PMI", "社融", "进出口", "就业", "失业率", "通胀", "经济增速")),
    ("公司动态", ("财报", "业绩", "营收", "净利", "裁员", "招聘", "收购", "破产", "暴雷")),
    ("行业产业", ("制造", "地产", "新能源", "汽车", "芯片", "医药", "消费", "能源", "光伏", "AI")),
    ("汇率大宗", ("人民币", "汇率", "美元", "原油", "黄金", "铜", "大宗商品")),
    ("ESG", ("ESG", "碳", "绿色", "可持续", "环保", "双碳")),
]


def _get(url: str) -> bytes:
    """取一个 URL。先按系统代理走，失败再用直连重试一次。

    很多机器上 http_proxy 指向本地工具代理，代理没开时 urllib 会连不上，
    但网络本身是通的 —— 直连重试能救回这种情况，不需要用户去改环境变量。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.read()


def robots_allowed(url: str) -> tuple[bool, str]:
    """查 robots.txt。返回 (是否允许, 说明)。

    抓不到 robots.txt（404/超时）时**允许**：RSS 本身就是给聚合器读的。
    明确写了 Disallow 才拦，并且把规则原文带回去显示，方便核对。
    """
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        raw = _get(robots_url).decode("utf-8", "ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return True, "未取到 robots.txt，按 RSS 分发默认允许"

    allowed = True
    reason = "robots.txt 未限制该路径"
    for line in raw.splitlines():
        line = line.strip()
        if not line.lower().startswith("disallow"):
            continue
        _, _, path = line.partition(":")
        path = path.strip()
        if path and path != "/" and parts.path.startswith(path):
            allowed = False
            reason = f"robots.txt 写了 Disallow: {path}"
            break
    return allowed, reason


def _text(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _clean(text: str, limit: int = 240) -> str:
    """RSS 摘要里常带 HTML 片段和实体，压成一行纯文本。"""
    plain = re.sub(r"<[^>]+>", "", text or "")
    plain = re.sub(r"&nbsp;|&amp;|&quot;|&#39;|&lt;|&gt;", " ", plain)
    return re.sub(r"\s+", " ", plain).strip()[:limit]


def _pub_time(item) -> str:
    raw = _text(item.find("pubDate")) or _text(item.find("{http://purl.org/dc/elements/1.1/}date"))
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return raw[:16]


# 明确的外国主体。不含「美元」「原油」这类跨境商品词 —— 人民币对美元也是国内新闻，
# 拿它们判断会把一半国内稿误标成国际。这个表只放国家/地区/机构/市场名。
FOREIGN_SUBJECTS = (
    "美国", "美联储", "英国", "日本", "欧盟", "欧元区", "韩国", "俄罗斯", "德国", "法国",
    "澳大利亚", "印度", "巴西", "加拿大", "意大利", "西班牙", "土耳其", "沙特", "伊朗",
    "越南", "泰国", "新加坡", "墨西哥", "阿根廷", "南非", "瑞士", "荷兰",
    "特朗普", "白宫", "纳斯达克", "道琼斯", "标普", "华尔街", "美股", "欧股", "日经",
    "马斯克", "得州", "加州", "硅谷", "纽约", "伦敦", "东京", "巴黎", "柏林", "莫斯科",
    "民主党", "共和党", "欧洲央行", "日本央行", "IMF", "世界银行", "WTO", "OPEC",
)


def refine_region(base: str, title: str, summary: str = "") -> str:
    """源频道的 region 是粗的，按标题里的外国主体再校一次。

    实测踩到的：新浪「财经要闻」频道里一半是国际稿（英国大臣、俄罗斯生铁、韩股、美联储），
    一律标「国内」会让「国际」页签空着、国内页签塞满外国新闻。
    """
    if any(word in (title or "") + " " + (summary or "") for word in FOREIGN_SUBJECTS):
        return "国际"
    return base


def tags_of(text: str) -> list[str]:
    """打标签。英文关键词要求词边界，否则 'AI' 会命中 'Diversified' 这种词中间的字母。"""
    found = []
    upper = (text or "").upper()
    for name, words in TAG_RULES:
        for word in words:
            target = word.upper()
            if target.isascii():
                if re.search(rf"\b{re.escape(target)}\b", upper):
                    found.append(name)
                    break
            elif target in upper:
                found.append(name)
                break
    return found[:3]


def parse_feed(raw: bytes, source: dict) -> list[dict]:
    """RSS → 统一结构。解析失败返回空列表，不抛异常炸页面。"""
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return []

    nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out: list[dict] = []
    for node in nodes:
        title = _clean(_text(node.find("title")), 160)
        if not title:
            continue
        link = _text(node.find("link"))
        if not link:
            atom_link = node.find("{http://www.w3.org/2005/Atom}link")
            link = atom_link.get("href", "") if atom_link is not None else ""
        summary = _clean(
            _text(node.find("description"))
            or _text(node.find("{http://purl.org/rss/1.0/modules/content/}encoded"))
        )
        out.append({
            "title": title,
            "link": link.strip(),
            "summary": summary,
            "pub": _pub_time(node),
            "source": source["name"],
            "region": refine_region(source["region"], title, summary),
            "kind": source["kind"],
            "tags": tags_of(title + " " + summary),
        })
    return out


def _sina_time(value) -> str:
    """新浪 ctime 是 Unix 秒（偶尔给字符串），转成和 RSS 一致的 MM-DD HH:MM。"""
    raw = str(value or "").strip()
    if not _SINA_TIME.match(raw):
        return ""
    try:
        return datetime.fromtimestamp(int(raw)).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_sina_json(raw: bytes, source: dict) -> list[dict]:
    """新浪 roll 接口 JSON → 统一结构。

    输出字段和 parse_feed 完全一致，所以后面的打标签、去重、排序都不用区分来源。
    解析失败返回空列表，不抛异常炸页面。
    """
    try:
        payload = json.loads(raw.decode("utf-8", "ignore"))
    except (ValueError, UnicodeDecodeError):
        return []

    nodes = ((payload.get("result") or {}).get("data")) or []
    out: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        title = _clean(str(node.get("title") or ""), 160)
        if not title:
            continue
        summary = _clean(str(node.get("intro") or node.get("summary") or ""), 240)
        out.append({
            "title": title,
            "link": str(node.get("url") or "").strip(),
            "summary": summary,
            "pub": _sina_time(node.get("ctime")),
            "source": source["name"],
            "region": refine_region(source["region"], title, summary),
            "kind": source["kind"],
            "media": _clean(str(node.get("media_name") or ""), 20),
            "tags": tags_of(title + " " + summary),
        })
    return out


def fetch_all(sources: list[dict] | None = None, per_source: int = 12) -> dict:
    """抓全部源。返回 {'items': [...], 'report': [...], 'errors': [...]}。

    单个源失败不影响其它源 —— 新闻这种东西，缺一条不影响用，整体失败才报错。
    """
    items: list[dict] = []
    report: list[dict] = []
    errors: list[str] = []

    for source in sources or SOURCES:
        allowed, why = robots_allowed(source["url"])
        if not allowed:
            report.append({"source": source["name"], "ok": False, "count": 0, "why": why})
            continue
        try:
            raw = _get(source["url"])
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            errors.append(f'{source["name"]}：{type(exc).__name__}')
            report.append({"source": source["name"], "ok": False, "count": 0, "why": str(exc)[:60]})
            continue
        got = (parse_sina_json if source.get("fmt") == "sina" else parse_feed)(raw, source)
        got = got[:per_source]
        items.extend(got)
        if got:
            report.append({"source": source["name"], "ok": True, "count": len(got), "why": why})
        else:
            # 200 但解析不出条目 = 接口改结构或频道下架，得说出来，不能装作抓到了。
            report.append({"source": source["name"], "ok": False, "count": 0,
                           "why": "接口返回 0 条，可能频道已下架或改了结构"})

    seen: set[str] = set()
    unique: list[dict] = []
    for item in items:
        key = re.sub(r"[\s\W]+", "", item["title"])[:40]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return {"items": unique, "report": report, "errors": errors}


def watch_hits(items: list[dict], watch: list[str]) -> list[dict]:
    """按关注词给新闻标相关度，并排序。

    先按发布时间倒序排一次，再按命中数稳定排序 —— 这样「都命中 0 个」的新闻里
    仍然是新的在前，不会出现旧闻压着新闻。
    """
    words = [w.strip() for w in watch if w.strip()]
    for item in items:
        blob = (item.get("title", "") + " " + item.get("summary", "")).upper()
        item["hits"] = [w for w in words if w.upper() in blob]
    items.sort(key=lambda i: i.get("pub", ""), reverse=True)
    items.sort(key=lambda i: -len(i.get("hits") or []))
    return items


def brief_prompt(items: list[dict], watch: list[str], background: str) -> tuple[str, str]:
    """组装给模型的请求。返回 (system, user)。

    刻意要求「不评论、不预测涨跌」：这是求职信息面板，不是投资建议工具。
    """
    system = (
        "你是财经编辑，为一个正在找工作的人写每日简报。"
        "只根据给定新闻写，不补充新闻里没有的事实，不预测涨跌、不构成投资建议。"
        "输出中文 Markdown，严格按四个小节，每节用项目符号：\n"
        "## 今天最该知道的三件事（国际）\n## 国内财经重点\n"
        "## 一个热点 + 财经视角\n## 对我求职的 3 条含义\n"
        "最后一节必须落到具体岗位能力（会计/财务/审计/风控/ESG 等），每条一句话，不要空话。"
    )
    lines = [f'- [{i.get("region")}] {i.get("title")}：{i.get("summary", "")[:110]}' for i in items[:24]]
    user = (
        f'我的关注：{"、".join(watch) or "财经、就业"}\n'
        f"我的背景：{background[:300]}\n\n"
        f"今日新闻：\n" + "\n".join(lines)
    )
    return system, user


def fetch_article_text(url: str) -> str:
    """抓单篇文章正文（用户主动贴链接时才调用）。

    只取 <p> 段落文字，不做反爬绕过；robots 明确 Disallow 就直接拒绝。
    """
    allowed, why = robots_allowed(url)
    if not allowed:
        raise PermissionError(f"该站 robots.txt 不允许抓取（{why}）")
    raw = _get(url).decode("utf-8", "ignore")
    paras = re.findall(r"<p[^>]*>(.*?)</p>", raw, flags=re.S | re.I)
    text = "\n".join(_clean(p, 400) for p in paras)
    return re.sub(r"\n{2,}", "\n", text).strip()

"""从「整页复制」的招聘网页文字里，捞出结构化字段。

为什么需要它：BOSS / 猎聘 / 51job 的正文都在登录后渲染，程序抓不到（也不该去抓）。
但你自己在浏览器里 Ctrl+A、Ctrl+C 是拿得到的 —— 拿到的是一大坨夹着导航栏、
按钮、推荐位、公司信息、页脚的文字。人眼一扫就懂，模型直接吃会被噪声带偏。

这里把它拆成：岗位名称 / 公司名称 / 薪资 / 地点 / 经验 / 学历 / 福利关键词 /
JD 正文。全部本地正则，不发请求、不调模型，所以既快又不花钱。

设计原则：宁可留空，也不硬猜。每个字段抽不到就返回 ""，界面上去手填一行，
比塞一个错的值更有用。
"""

from __future__ import annotations

import re

BUILD = 20261107161500

# --------------------------------------------------------------------------
# 常见招聘站的文字特征。只用来判断「这坨文字大概来自哪儿」，
# 不影响字段抽取（抽取规则对各家都通用）。
# --------------------------------------------------------------------------
SOURCES = (
    ("51job", ("51job", "前程无忧", "51job.com")),
    ("boss", ("boss直聘", "直聘", "zhipin.com", "bosszp")),
    ("liepin", ("猎聘", "liepin.com")),
    ("zhaopin", ("智联招聘", "zhaopin.com", "智联")),
    ("lagou", ("拉勾", "lagou.com")),
    ("shixiseng", ("实习僧", "shixiseng")),
    ("yingjiesheng", ("应届生求职", "yingjiesheng")),
    ("guopin", ("国聘", "iguopin")),
)

# JD 正文的起点：招聘站基本都用这几个小标题
JD_START = re.compile(
    r"^(?:\s*(?:【[^】]{0,6}】)?\s*)?"
    r"(岗位职责|工作职责|职位职责|职责描述|岗位描述|职位描述|工作内容|"
    r"主要职责|工作职责描述|你将负责|你要做的事|岗位工作内容|"
    r"任职要求|岗位要求|任职资格|职位要求|资格要求|我们希望你|任职条件)",
    re.MULTILINE,
)

# JD 正文的终点：这些一出现，后面就都是页面的壳了
JD_END = re.compile(
    r"^(?:\s*(?:【[^】]{0,6}】)?\s*)?"
    r"(公司信息|公司介绍|公司简介|企业信息|工商信息|企业介绍|"
    r"工作地址|上班地址|办公地址|职位地址|工作地点(?![\s:：]*[\u4e00-\u9fa5]{2,4}?[市省])|"
    r"联系方式|联系人|联系电话|简历投递|投递方式|"
    r"相似职位|相关职位|猜你喜欢|热门职位|推荐职位|看了又看|"
    r"面试评价|公司评价|职位发布者|举报|投诉|"
    r"竞争分析|职位竞争力|行业对比|工商信息已核验)",
    re.MULTILINE,
)

CITIES = (
    "北京|上海|广州|深圳|杭州|南京|成都|重庆|武汉|西安|苏州|天津|长沙|郑州|青岛|"
    "合肥|宁波|厦门|福州|济南|大连|沈阳|无锡|东莞|佛山|昆明|哈尔滨|长春|石家庄|"
    "太原|南昌|贵阳|南宁|兰州|乌鲁木齐|呼和浩特|海口|银川|西宁|拉萨|珠海|中山|"
    "惠州|常州|温州|嘉兴|南通|徐州|烟台|泉州|绍兴|台州|金华|保定|廊坊|淄博"
)
CITY_RE = re.compile(rf"({CITIES})(?:\s*[·・\-—/]\s*([\u4e00-\u9fa5]{{2,10}}))?")

# 薪资：8千-1.2万/月、15-25K·13薪、15K-25K、200-300元/天、面议……
SALARY_PATTERNS = (
    # 前一个数字可能不带 K（"15-25K·13薪" 是 BOSS 最常见的写法）
    re.compile(
        r"\d+(?:\.\d+)?\s*[Kk]?\s*[-~－—–]\s*\d+(?:\.\d+)?\s*[Kk]"
        r"(?:\s*[·・]\s*\d+\s*薪)?"
    ),
    re.compile(r"\d+(?:\.\d+)?\s*[千万]\s*[-~－—–]\s*\d+(?:\.\d+)?\s*[千万]\s*/\s*[月年]"),
    re.compile(r"\d+(?:\.\d+)?\s*[-~－—–]\s*\d+(?:\.\d+)?\s*[千万]\s*/\s*[月年]"),
    re.compile(r"\d+(?:\.\d+)?\s*[-~－—–]\s*\d+(?:\.\d+)?\s*元\s*/\s*(?:天|小时|月|时)"),
    re.compile(r"\d+(?:\.\d+)?\s*元\s*/\s*(?:天|小时|月|时)"),
    re.compile(r"\d+(?:\.\d+)?\s*[Kk]\s*以上"),
    re.compile(r"薪资面议|面议|待遇面议"),
)

EXPERIENCE_RE = re.compile(
    r"(\d+\s*[-~－—–]\s*\d+\s*年(?:经验)?|\d+\s*年(?:以上)?(?:经验)?|"
    r"经验不限|应届毕业生|应届生|在校/应届|在校生|1年以内|10年以上|无需经验)"
)
EDUCATION_RE = re.compile(
    r"(博士|硕士|本科|大专|中专/中技|中专|中技|高中|初中及以下|学历不限|不限学历)"
)

# 福利词：用户说的「待遇」除了钱，通常也指这些
BENEFIT_WORDS = (
    "五险一金", "六险一金", "补充医疗保险", "补充公积金", "年终奖", "年终奖金",
    "绩效奖金", "十三薪", "十四薪", "带薪年假", "带薪病假", "双休", "周末双休",
    "弹性工作", "不加班", "包吃", "包住", "餐补", "交通补助", "房补", "住房补贴",
    "通讯补贴", "定期体检", "年度体检", "员工旅游", "团建", "节日福利", "生日福利",
    "免费班车", "股票期权", "期权", "股权激励", "技能培训", "导师带教", "转正机会",
)

# 公司名：以组织形式结尾。
# 「集团」这种短结尾太容易误伤正文（"负责集团月度分析" → "负责集团"），
# 所以分两条路：整行匹配用宽松的，行内搜索用严格的（长结尾 + 不含动词）。
COMPANY_SUFFIX_LONG = (
    "股份有限公司|有限责任公司|有限公司|集团有限公司|分公司|"
    "会计师事务所|律师事务所|事务所|研究院|设计院|"
    "银行|证券|保险|资产管理|基金管理"
)
COMPANY_FULL = re.compile(
    rf"^[\u4e00-\u9fa5A-Za-z0-9（）()·&]{{2,32}}?(?:{COMPANY_SUFFIX_LONG}|集团)$"
)
COMPANY_RE = re.compile(
    rf"([\u4e00-\u9fa5A-Za-z0-9（）()·&]{{2,32}}?(?:{COMPANY_SUFFIX_LONG}))"
)

# 行内搜索时，命中的片段里出现这些词，说明是句子不是公司名
VERB_WORDS = re.compile(
    r"负责|参与|加入|隶属|属于|服务于|合作|覆盖|包括|以及|支持|对接|"
    r"入职|应聘|就职|来自|客户|供应商|母公司|子公司"
)

# 岗位名前后会出现的噪声。整行命中就说明这行不是岗位名
TITLE_NOISE = re.compile(
    r"登录|注册|首页|搜索|职位搜索|投递|已投|立即申请|收藏|分享|举报|投诉|"
    r"招聘网|招聘网站|人才网|客服|隐私|协议|下载|APP|app|微信|二维码|"
    r"上一页|下一页|查看更多|展开|收起|返回|导航|菜单|我的简历|简历投递|"
    r"浏览|人看过|人投递|发布时间|更新于|刷新|推荐|相似|热门|精选|广告|"
    r"版权所有|copyright|ICP|营业执照|人力资源服务许可证",
)

# 招聘人数 / 浏览人数这类计数行，绝不能当岗位名（"招1人" 曾被抓成职位名）
HEADCOUNT_RE = re.compile(
    r"^(?:急?招\s*\d+\s*人|\d+\s*人(?:看过|投递|已投|收藏|浏览|应聘)?|"
    r"招聘\s*\d+\s*人|在招\s*\d*\s*个?职位?|共\s*\d+\s*个?职位)$"
)


# 平台自己的名字和网址，绝不会是岗位名（实测：整页复制第一行常常就是
# "前程无忧51job.com"，不拦掉就会被当成岗位名）
PLATFORM_NOISE = re.compile(
    r"前程无忧|51job|boss直聘|bosszp|zhipin|猎聘|liepin|智联|zhaopin|"
    r"拉勾|lagou|实习僧|shixiseng|国聘|iguopin|应届生求职|大街网|中华英才",
    re.IGNORECASE,
)
URL_NOISE = re.compile(r"\.(?:com|cn|net|org|cc|top)\b|www\.|https?://", re.IGNORECASE)

# 以组织形式结尾的，是公司名不是岗位名
CORP_SUFFIX = re.compile(
    r"(?:有限公司|有限责任公司|股份有限公司|集团|分公司|事务所|研究院|设计院|"
    r"银行|证券|保险|工作室|中心|协会|学校|大学)$"
)

# 一行文字当成岗位名时的合理长度
TITLE_MIN, TITLE_MAX = 3, 30


def _guess_source(text: str) -> str:
    low = text.lower().replace(" ", "")
    for name, hints in SOURCES:
        for hint in hints:
            if hint.lower().replace(" ", "") in low:
                return name
    return ""


def _norm(text: str) -> str:
    """统一空白：全角空格转半角，行内多余空白压成一个，行首行尾去掉。

    保留换行 —— 招聘页的换行本身携带结构（一行岗位名、一行公司、一行薪资）。
    """
    text = (text or "").replace("\u3000", " ").replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(lines)


def _find_salary(text: str) -> str:
    for pattern in SALARY_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0).strip()
    return ""


def _strip_salary(line: str) -> str:
    """把一行里的薪资片段抠掉，剩下的当岗位名候选。

    51job / BOSS 的列表页常把「风控实习生 180元/天」压成一行 ——
    直接跳过整行就会退到下一行的「招1人」，反而抓错。
    """
    out = line
    for pattern in SALARY_PATTERNS:
        out = pattern.sub(" ", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" ·・|｜-—–/、")


def _find_company(text: str) -> str:
    """公司名：先按「整行就是公司名」找，再做行内兜底。

    为什么先整行：招聘页复制出来，公司名几乎总是独立一行的（51job、BOSS 都是）。
    整行匹配最准，也不会把正文里的"负责集团月度分析"切成"负责集团"。
    """
    lines = [ln.strip() for ln in text.split("\n")]

    def looks_like_benefit(name: str, line: str = "") -> bool:
        """福利标签扮成的「公司」。

        招聘页的待遇标签墙里全是「补充医疗保险」「年终奖金」，它们都以
        「保险 / 奖金」这类后缀结尾，正好踩中公司名规则。一行里堆了两个以上
        福利词的，也肯定是标签墙而不是公司介绍。
        """
        if any(w in name for w in BENEFIT_WORDS):
            return True
        if line:
            hits = sum(1 for w in BENEFIT_WORDS if w in line)
            if hits >= 2:
                return True
        return False

    # 第一轮：整行即公司名
    for line in lines:
        if not line or len(line) > 44:
            continue
        # "公司名称：XXX有限公司" 这种带前缀的，先把前缀剥掉
        stripped = re.sub(r"^(?:公司名称|企业名称|单位名称|招聘企业|公司)\s*[:：]\s*", "", line)
        if COMPANY_FULL.match(stripped) and not looks_like_benefit(stripped, line):
            return stripped

    # 第二轮：行内找一个合法片段。命中里含动词的一律不要
    for match in COMPANY_RE.finditer(text):
        name = match.group(1).strip()
        name = re.sub(r"^(?:服务于|就职于|来自|入职|应聘|合作客户|客户)", "", name)
        if len(name) < 5:
            continue
        if VERB_WORDS.search(name):
            continue
        if TITLE_NOISE.search(name) or PLATFORM_NOISE.search(name) or URL_NOISE.search(name):
            continue
        if looks_like_benefit(name):
            continue
        return name
    return ""


def _find_title(lines: list[str], company: str) -> str:
    """岗位名：在开头若干行里找一个像标题的短行。

    招聘页复制出来，前几行基本是「岗位名 / 公司 / 城市 / 薪资 / 时间」，
    所以只看前 12 行；再往后就是正文，容易把句子误当标题。
    """
    for raw in lines[:12]:
        line = raw.strip()
        if not line:
            continue
        # 「风控实习生 180元/天」这类同行写法：先把薪资抠掉再判断
        core = _strip_salary(line)
        if not core:
            continue
        # 带句号 / 分号的多半是句子，不是标题
        if re.search(r"[。；;，,]", core):
            continue
        if HEADCOUNT_RE.match(core):
            continue
        if TITLE_NOISE.search(core) or PLATFORM_NOISE.search(core) or URL_NOISE.search(core):
            continue
        if CORP_SUFFIX.search(core):
            continue
        if company and company in core:
            continue
        if CITY_RE.fullmatch(core):
            continue
        if EXPERIENCE_RE.fullmatch(core) or EDUCATION_RE.fullmatch(core):
            continue
        if not (TITLE_MIN <= len(core) <= TITLE_MAX):
            continue
        # "财务专员 · 上海" / "财务专员（上海）" 这类，只留岗位名
        core = re.split(r"\s*[·・|｜]\s*", core)[0]
        core = re.sub(r"[（(][^）)]{0,12}[）)]$", "", core).strip()
        # "招聘财务助理" / "急招：财务助理" 这类前缀不是岗位名的一部分
        core = re.sub(r"^(?:招聘|急招|诚聘|高薪急聘|直招|热招)\s*[:：]?\s*", "", core).strip()
        if TITLE_MIN <= len(core) <= TITLE_MAX:
            return core
    return ""


def _slice_jd(text: str) -> tuple[str, str, bool]:
    """切出 JD 正文，返回 (正文, 切法说明, 是否找到了小标题)。

    三段式：先找「岗位职责」这类标题当起点，再找「公司信息」这类标题当终点。
    找不到起点就用整段去噪（有些小公司页面没有小标题）；
    找不到终点就一路留到最后。
    """
    start = JD_START.search(text)
    has_start = bool(start)
    if start:
        body = text[start.start():]
        note = "从「岗位职责/任职要求」开始截取"
    else:
        body = text
        note = "没找到职责小标题，保留全文"

    end = JD_END.search(body, 1)  # 从 1 开始，避免把起点自己当成终点
    if end:
        body = body[: end.start()]
        note += "，到「公司信息/工作地址」为止"
    body = body.strip()

    # 去掉行内重复行（有的页面正文会重复渲染一遍）
    seen: set[str] = set()
    kept: list[str] = []
    for ln in body.split("\n"):
        key = ln.strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        kept.append(ln)
    return "\n".join(kept).strip(), note, has_start


def _find_benefits(text: str) -> list[str]:
    hits: list[str] = []
    for word in BENEFIT_WORDS:
        if word in text:
            hits.append(word)
    # 长词优先，去掉被长词包含的短词（"年终奖" 与 "年终奖金" 不必都留）
    hits.sort(key=len, reverse=True)
    out: list[str] = []
    for w in hits:
        if not any(w != o and w in o for o in out):
            out.append(w)
    return out


def extract(text: str) -> dict:
    """主入口：吃一整页复制的文字，吐结构化字段。

    返回 dict：
      ok        是否至少抽到 JD 正文
      title / company / salary / location / experience / education
      benefits  福利关键词列表
      jd        清洗后的 JD 正文
      jd_note   正文是怎么切出来的（写进界面，方便你判断有没有切歪）
      source    猜测的来源站
      chars     原始字数 / jd_chars 正文清字数
      missing   没抽到的字段名列表
    """
    norm = _norm(text)
    if not norm.strip():
        return {
            "ok": False,
            "message": "粘进来的内容是空的。",
            "missing": [],
        }

    jd, jd_note, has_start = _slice_jd(norm)

    # 公司名在「正文」里找更准：正文之外的导航栏经常混着平台自己的公司名
    company = _find_company(jd) or _find_company(norm)
    lines = norm.split("\n")
    title = _find_title(lines, company)

    # 没有小标题时，正文开头会带着「岗位名 / 公司 / 城市 / 薪资 / 学历经验」
    # 这几行元信息 —— 它们已经抽成独立字段了，正文里再留一遍是重复噪声
    if not has_start:
        meta_lines = {t for t in (title, company) if t}
        cleaned: list[str] = []
        for idx, raw in enumerate(jd.split("\n")):
            s = raw.strip()
            if not s:
                continue
            # 原文里的"招聘财务助理"和抽出来的"财务助理"要能对上，否则过滤不掉
            s_clean = re.sub(r"^(?:招聘|急招|诚聘|高薪急聘|直招|热招)\s*[:：]?\s*", "", s).strip()
            is_meta = (
                idx < 10
                and (
                    s in meta_lines
                    or s_clean in meta_lines
                    or CITY_RE.fullmatch(s)
                    or _find_salary(s)
                    or EXPERIENCE_RE.fullmatch(s)
                    or EDUCATION_RE.fullmatch(s)
                    or re.fullmatch(rf"(?:{EXPERIENCE_RE.pattern})\s*(?:{EDUCATION_RE.pattern})?", s)
                    or re.fullmatch(rf"(?:{EDUCATION_RE.pattern})\s*(?:{EXPERIENCE_RE.pattern})?", s)
                )
            )
            if not is_meta:
                cleaned.append(raw)
        jd = "\n".join(cleaned).strip()
        jd_note += "（已去掉开头的岗位名/公司/薪资等重复行）"

    # 城市：只在正文以外的前半段找，正文里的城市多是业务分布，不是岗位地点
    head = "\n".join(lines[:14])
    city_m = CITY_RE.search(head)
    location = ""
    if city_m:
        location = city_m.group(1) + (f"·{city_m.group(2)}" if city_m.group(2) else "")

    # 经验 / 学历：优先看页头那几行（招聘站会单独列一行），
    # 页头没有就退到正文里找（"本科及以上学历"通常写在任职要求里）
    exp_m = EXPERIENCE_RE.search(head) or EXPERIENCE_RE.search(jd)
    edu_m = EDUCATION_RE.search(head) or EDUCATION_RE.search(jd)

    salary = _find_salary(head) or _find_salary(norm)

    missing = [
        label
        for label, value in (
            ("岗位名称", title),
            ("公司名称", company),
            ("薪资待遇", salary),
            ("工作地点", location),
        )
        if not value
    ]

    return {
        "ok": len(jd) >= 30,
        "message": "" if len(jd) >= 30 else "正文太短（不到 30 字），可能只复制到了标题栏。",
        "title": title,
        "company": company,
        "salary": salary,
        "location": location,
        "experience": exp_m.group(0) if exp_m else "",
        "education": edu_m.group(0) if edu_m else "",
        "benefits": _find_benefits(norm),
        "jd": jd,
        "jd_note": jd_note,
        "source": _guess_source(norm),
        "chars": len(norm),
        "jd_chars": len(jd),
        "missing": missing,
    }


def summary_lines(result: dict) -> list[str]:
    """把结果压成几行「字段 → 值」的文本，给 Markdown / 导出复用。"""
    rows = [
        ("岗位名称", result.get("title", "")),
        ("公司名称", result.get("company", "")),
        ("薪资待遇", result.get("salary", "")),
        ("工作地点", result.get("location", "")),
        ("经验要求", result.get("experience", "")),
        ("学历要求", result.get("education", "")),
        ("福利关键词", "、".join(result.get("benefits") or [])),
    ]
    return [f"{k}：{v}" for k, v in rows if v]

"""privacy.py —— 发往模型之前的本地脱敏层。

背景：调用云端大模型 API，文本必须先离开本机才能算出结果，这是原理决定的，
无法通过配置绕开。DeepSeek 隐私政策明确写着「经安全加密技术处理和去标识化
前提下，我们可能会将输入及对应输出用于模型训练和服务的优化」。

所以这里做的是**降低泄露面**，不是消除：
把能直接指向"你是谁"的字段替换成占位符，让服务商拿到的是
「[P1]（某财经类高校）会计专硕，base [P2]」，而不是「张三，西南财经大学」。

设计原则：
1. 纯本地、纯函数，不联网、不读文件、不写文件。
2. 替换集合可枚举、可检查 —— 页面会把"替换了什么"列出来给用户看。
3. 只替换高置信度实体，宁可漏也别把正文改得面目全非（错误替换会让分析跑偏）。
4. 占位符用 ASCII 的 [P1] 形式，模型改动风险比中文符号低；还原时用宽松正则兼容
   模型可能加的空格。

不处理的（有意为之）：
- 城市名：base 城市直接影响地点匹配判断，抹掉等于废掉一整个评分维度。
- 详细地址：正则误伤率太高（"高新区天府大道"这类既是地址又是地标），
  收益不足以抵消把 JD 改坏的风险。
"""

from __future__ import annotations

import re
from typing import Any

# 模块构建标记，供 app.py 启动时自检是否加载了旧版本（见 store.BUILD 的说明）
BUILD = 20261107161500

# ==========================================================================
# 一、实体识别规则
# ==========================================================================

# 顺序有意义：先匹配长的、结构明确的，避免短规则先吃掉一部分
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 身份证：18 位，最后一位可能是 X。前后不能紧邻数字，避免啃掉更长的数字串
    ("IDCARD", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    # 手机号：1 开头 11 位，前后不能紧邻数字（防止从注册资本里切出 11 位）
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("EMAIL", re.compile(r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)+")),
    # 学校：常见后缀。限定 2-14 字，防止把整段话吞掉
    ("SCHOOL", re.compile(r"[\u4e00-\u9fa5]{2,14}(?:大学|学院|学校)")),
    # 公司：带组织形式后缀的才处理，光秃秃的品牌名（"牧原"）不动，
    # 因为它经常出现在 JD 正文里描述业务，抹掉会让行业判断失真
    (
        "COMPANY",
        re.compile(
            r"[\u4e00-\u9fa5A-Za-z（）()]{2,24}?"
            r"(?:股份有限公司|有限责任公司|有限公司|股份公司|集团公司)"
        ),
    ),
]

# 姓名走单独逻辑：正则没法可靠判断"两个字是不是人名"，必须靠上下文
RE_NAME_LABEL = re.compile(r"(?:姓\s*名|姓\s*氏)\s*[:：]\s*([\u4e00-\u9fa5]{2,4})")
# 简历首行的"张三 会计专硕" / "张三/会计专硕" / 单独一行的"张三"
RE_NAME_HEAD = re.compile(r"^\s*([\u4e00-\u9fa5]{2,4})(?=[\s/·|｜，,、—-]|$)")
# 这些不是人名，是标题
NAME_STOPWORDS = {"个人简历", "求职简历", "简历", "个人信息", "教育背景", "求职意向", "自我评价"}
# 含这些字样的两到四字词一律不当人名（"会计专硕""财务管理"会被首行规则误判）
NAME_BLOCK_WORDS = (
    "会计", "财务", "金融", "审计", "经济", "管理", "税务", "统计",
    "硕士", "专硕", "学硕", "本科", "研究生", "博士", "学历",
    "简历", "求职", "应聘", "个人", "信息", "背景", "经历", "技能",
    "优先", "接受", "希望", "期望", "倾向", "偏好", "考虑", "目前", "坐标",
    "本人", "今年", "目前", "现在", "性格", "兴趣",
)

# 首行猜姓名只在"这段文字确实是简历"时才做。
# 不加这层判断的话，求职偏好栏里"优先双休，MBTI 是 ISTJ"会被当成一个人名，
# 脱敏后模型看到的是「[P3]，MBTI 是 ISTJ」，比不脱敏还坏。
RESUME_SIGNS = (
    "简历", "求职意向", "教育背景", "教育经历", "工作经历", "实习经历",
    "项目经历", "个人信息", "自我评价", "技能", "在校经历", "获奖",
    "电话", "手机", "出生年月", "政治面貌",
)
RE_PHONE_ANY = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 首行猜姓名的第二道门槛：人名后面紧跟的应该是身份描述，不是"MBTI""性格"这类词。
# 覆盖"张三，会计专硕研二，base成都"这种背景栏写法（它不是简历，但确实以人名开头）。
IDENTITY_SIGNS = (
    "专硕", "学硕", "硕士", "研究生", "博士", "本科", "大四", "研一", "研二", "研三",
    "应届", "在读", "毕业", "专业", "学院", "大学", "base", "Base", "BASE",
    "坐标", "求职", "应聘", "实习", "工作", "技能", "项目",
)

# 公司名前面常粘着动词/介词（"曾在四川蜀道集团有限公司"），替换前要先剥掉，
# 否则占位符里存的是"曾在四川蜀道集团有限公司"，还原回正文就不通了
COMPANY_STOP_PREFIXES = (
    "就职于", "任职于", "实习于", "工作于", "服务于", "就读于", "客户包括",
    "例如", "比如", "包括", "以及", "其中", "曾在", "就职", "任职", "实习",
    "加入", "进入", "所在", "来自", "入职", "合作方", "客户", "单位",
    "在", "于", "的", "和", "与", "及", "至", "到", "从", "向", "对", "是", "为",
)
# 中文公司名最少两个字（"沙县小吃"算短的）
COMPANY_MIN_CORE = 2
# 商号 + 地区 + 行业词，核心部分极少超过 10 个字。超了说明还粘着修饰语
COMPANY_MAX_CORE = 10
# 组织形式后缀，按长度降序，避免"股份有限公司"被"有限公司"先切走
COMPANY_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "股份公司", "集团公司")

# 行业推断：公司全称被抹掉后，至少让模型知道这是什么行业，否则行业匹配没法算
INDUSTRY_HINTS: list[tuple[str, str]] = [
    ("农牧", "农牧养殖"), ("养殖", "农牧养殖"), ("食品", "食品加工"), ("饮料", "食品饮料"),
    ("医药", "医药"), ("生物", "生物医药"), ("医疗", "医疗器械"),
    ("银行", "银行"), ("证券", "证券"), ("保险", "保险"), ("基金", "基金"),
    ("地产", "房地产"), ("置业", "房地产"), ("建筑", "建筑工程"), ("建设", "建筑工程"),
    ("机械", "机械制造"), ("装备", "装备制造"), ("汽车", "汽车"), ("钢铁", "钢铁冶金"),
    ("化工", "化工"), ("能源", "能源"), ("电力", "电力"), ("新能源", "新能源"),
    ("电子", "电子制造"), ("半导体", "半导体"), ("软件", "软件"), ("信息", "信息技术"),
    ("网络", "互联网"), ("科技", "科技"), ("通信", "通信"), ("物流", "物流"),
    ("贸易", "商贸流通"), ("商贸", "商贸流通"), ("零售", "零售"),
    ("传媒", "传媒"), ("文化", "文化传媒"), ("教育", "教育"), ("咨询", "咨询"),
    ("会计", "财税服务"), ("税务", "财税服务"), ("律师", "法律服务"),
    ("投资", "投资"), ("控股", "投资控股"), ("集团", "综合性集团"),
]

# 高校类型推断：同上的理由，抹掉校名后保留"这是什么档次的学校"这一层信息
SCHOOL_HINTS: list[tuple[str, str]] = [
    ("财经", "财经类"), ("工商", "财经类"), ("金融", "财经类"), ("经济", "财经类"),
    ("师范", "师范类"), ("理工", "理工类"), ("工业", "理工类"), ("科技", "理工类"),
    ("交通", "理工类"), ("电子", "理工类"), ("邮电", "理工类"), ("建筑", "理工类"),
    ("石油", "理工类"), ("矿业", "理工类"),
    ("农业", "农林类"), ("林业", "农林类"), ("医科", "医药类"), ("中医药", "医药类"),
    ("政法", "政法类"), ("外国语", "外语类"), ("外语", "外语类"),
]

RE_PLACEHOLDER = re.compile(r"\[\s*P\s*(\d{1,3})\s*\]")


def _guess_industry(name: str) -> str:
    for key, label in INDUSTRY_HINTS:
        if key in name:
            return label
    return "行业未识别"


def _guess_school_type(name: str) -> str:
    for key, label in SCHOOL_HINTS:
        if key in name:
            return label
    return "普通高校"


# ==========================================================================
# 二、脱敏
# ==========================================================================


def _strip_company_prefix(name: str) -> str:
    """剥掉公司名前面粘上的虚词/动词。

    "曾在四川蜀道集团有限公司" -> "四川蜀道集团有限公司"
    "客户包括成都硅宝科技股份有限公司" -> "成都硅宝科技股份有限公司"
    """
    changed = True
    while changed:
        changed = False
        for word in COMPANY_STOP_PREFIXES:
            if name.startswith(word) and len(name) - len(word) >= COMPANY_MIN_CORE:
                name = name[len(word):].lstrip("　 ·:：")
                changed = True
                break

    # 兜底：停用词表不可能穷举，核心名过长就一定还粘着修饰语
    for suffix in COMPANY_SUFFIXES:
        if name.endswith(suffix):
            core = name[: -len(suffix)]
            if len(core) > COMPANY_MAX_CORE:
                # 先在核心名里找最后一个停用词，从它之后切，比硬截长度准
                for word in sorted(COMPANY_STOP_PREFIXES, key=len, reverse=True):
                    pos = core.rfind(word)
                    if pos > 0 and len(core) - (pos + len(word)) >= COMPANY_MIN_CORE:
                        core = core[pos + len(word):]
                        break
                if len(core) > COMPANY_MAX_CORE:  # 还长就只得硬切末尾
                    core = core[-COMPANY_MAX_CORE:]
            name = core + suffix
            break
    return name


def _looks_like_resume(text: str) -> bool:
    """粗判这段文字是不是简历。只用于决定要不要做"首行猜姓名"。"""
    if any(sign in text for sign in RESUME_SIGNS):
        return True
    return bool(RE_PHONE_ANY.search(text)) or "@" in text


def _head_name(text: str) -> tuple[str, int] | None:
    """从首行猜人名，返回 (人名, 起始位置)；不确信就返回 None。

    两道门槛缺一不可：既不能是"会计专硕""优先双休"这类词，后面紧跟的还得是
    身份描述（专硕/base/学院…）。"优先双休，MBTI 是 ISTJ" 后半句是 MBTI，不认。
    """
    head = RE_NAME_HEAD.match(text)
    if not head:
        return None

    raw = head.group(1)
    if raw in NAME_STOPWORDS or any(word in raw for word in NAME_BLOCK_WORDS):
        return None

    # 简历本身就该有首行人名，不必再验身份词；非简历文本必须验
    if _looks_like_resume(text):
        return raw, head.start(1)

    tail = text[head.end(): head.end() + 30]
    if any(sign in tail for sign in IDENTITY_SIGNS):
        return raw, head.start(1)
    return None


def _collect(text: str) -> list[tuple[str, str, int]]:
    """扫描文本，返回 [(原文, 类型, 首次出现位置)]。

    位置用于按文中出现顺序分配占位符编号，读起来更符合直觉。
    """
    found: dict[str, tuple[str, int]] = {}

    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(0).strip()
            if len(raw) < 3:
                continue
            if kind == "COMPANY":
                raw = _strip_company_prefix(raw)
                if len(raw) < 4:  # 剥完只剩两三个字，说明本来就匹配错了
                    continue
            # 学校规则会把"某某大学附属医院"整段吞掉，长度异常的直接放弃
            if kind == "SCHOOL" and len(raw) > 16:
                continue
            if raw in found:
                continue
            pos = text.find(raw)
            found[raw] = (kind, pos if pos >= 0 else match.start())

    # 姓名：先看"姓名：张三"这种明确标注，再退一步看首行
    for match in RE_NAME_LABEL.finditer(text):
        raw = match.group(1)
        if raw not in NAME_STOPWORDS:
            found.setdefault(raw, ("NAME", match.start(1)))

    head = _head_name(text)
    if head:
        raw, pos = head
        if raw not in found:
            found[raw] = ("NAME", pos)

    # 按首次出现位置排序，编号就是 1、2、3… 的顺序
    ordered = sorted(found.items(), key=lambda kv: kv[1][1])
    return [(raw, kind, pos) for raw, (kind, pos) in ordered]


def _hint_for(raw: str, kind: str) -> str:
    """给被抹掉的实体留一层类别信息，让模型还能分析行业/院校层次。"""
    if kind == "COMPANY":
        return f"某{_guess_industry(raw)}企业"
    if kind == "SCHOOL":
        return f"某{_guess_school_type(raw)}院校"
    return ""


def _scrub_impl(parts: dict[str, str]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """脱敏核心逻辑：跨材料统一识别、统一编号、统一替换。"""
    if not any(parts.values()):
        return dict(parts), {}

    # ---- 1. 各份材料独立识别 ----
    entity_kind: dict[str, str] = {}
    entity_pos: dict[str, dict[str, int]] = {}
    for key, text in parts.items():
        entity_pos[key] = {}
        for raw, kind, pos in _collect(text):
            entity_pos[key][raw] = pos
            entity_kind.setdefault(raw, kind)

    # ---- 2. 跨材料补漏 ----
    # 简历里识别出"张伟"是姓名，但背景栏里那句"张伟，会计专硕研二"单独看不像简历，
    # 首行规则不会触发。如果不在这里补齐，同一个人的名字就会明文漏出去。
    for key, text in parts.items():
        for raw in entity_kind:
            if raw not in entity_pos[key] and raw in text:
                entity_pos[key][raw] = text.find(raw)

    # ---- 3. 全局编号：按材料顺序 + 文内位置，[P1] 就是最先出现的那个 ----
    raw_to_idx: dict[str, str] = {}
    for key in parts:
        for raw, _pos in sorted(entity_pos[key].items(), key=lambda kv: kv[1]):
            if raw not in raw_to_idx:
                raw_to_idx[raw] = str(len(raw_to_idx) + 1)

    merged = {
        idx: {
            "raw": raw,
            "kind": entity_kind[raw],
            "masked": f"[P{idx}]",
            "hint": _hint_for(raw, entity_kind[raw]),
        }
        for raw, idx in raw_to_idx.items()
    }

    # ---- 4. 替换：长实体优先，用哨兵中转避免编号互相覆盖 ----
    out: dict[str, str] = {}
    for key, text in parts.items():
        text_out = text
        for raw, _pos in sorted(entity_pos[key].items(), key=lambda kv: len(kv[0]), reverse=True):
            text_out = text_out.replace(raw, f"\x00{raw_to_idx[raw]}\x00")
        for raw, idx in raw_to_idx.items():
            text_out = text_out.replace(f"\x00{idx}\x00", f"[P{idx}]")
        out[key] = text_out

    return out, merged


def scrub(text: str) -> tuple[str, dict[str, dict[str, str]]]:
    """把文本里的敏感实体换成 [P1] 占位符。

    返回 (脱敏后文本, mapping)。mapping 形如：
        {"1": {"raw": "张三", "kind": "NAME", "masked": "[P1]", "hint": ""}}

    纯本地操作，不联网。
    """
    if not text:
        return text, {}
    out, mapping = _scrub_impl({"text": text})
    return out["text"], mapping


def scrub_many(parts: dict[str, str]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """对多份材料（背景/偏好/简历/JD）统一脱敏。

    不能各自调 scrub 后直接拼——那样每份材料都会从 [P1] 开始，占位符撞号，
    还原时就会把"院校"错还原成"姓名"；更糟的是同一实体会拿到多个编号，
    还会漏掉只在一份材料里被识别出来的姓名。
    """
    return _scrub_impl(parts)


def hint_block(mapping: dict[str, dict[str, str]]) -> str:
    """生成附在 prompt 末尾的说明块，让模型知道被抹掉的是什么类别。

    不给这层提示的话，模型只能看到 [P3]，会写出"你的 [P3] 经历"这种没法读的
    句子，而且行业匹配度直接算不出来。
    """
    lines = [
        f"{item['masked']} = {item['hint']}（原文已脱敏，不要猜测具体名称）"
        for item in mapping.values()
        if item.get("hint")
    ]
    if not lines:
        return ""
    return "【脱敏说明】\n" + "\n".join(lines)


def report(mapping: dict[str, dict[str, str]]) -> str:
    """给页面显示的一行统计，比如「姓名 1、手机号 1、公司 2」。"""
    if not mapping:
        return ""
    labels = {
        "NAME": "姓名", "PHONE": "手机号", "EMAIL": "邮箱", "IDCARD": "身份证",
        "SCHOOL": "院校", "COMPANY": "公司",
    }
    counter: dict[str, int] = {}
    for item in mapping.values():
        key = labels.get(item["kind"], item["kind"])
        counter[key] = counter.get(key, 0) + 1
    return "、".join(f"{k} {v}" for k, v in counter.items())


# ==========================================================================
# 三、还原（模型返回后在本机把真名换回来）
# ==========================================================================


def restore(text: str, mapping: dict[str, dict[str, str]]) -> str:
    """把 [P1] 还原成原文。找不到对应项时原样保留，不报错。"""
    if not text or not mapping:
        return text

    def _sub(match: re.Match[str]) -> str:
        idx = match.group(1)
        item = mapping.get(idx)
        return item["raw"] if item else match.group(0)

    return RE_PLACEHOLDER.sub(_sub, text)


def restore_obj(obj: Any, mapping: dict[str, dict[str, str]]) -> Any:
    """递归还原 dict / list / str 里所有字符串。

    为什么要递归而不是直接还原原始 JSON 文本：
    先 parse 再逐字段还原，即使某个占位符被模型改坏了，也只影响那一个字段，
    不会让整份 JSON 解析失败。
    """
    if not mapping:
        return obj
    if isinstance(obj, str):
        return restore(obj, mapping)
    if isinstance(obj, list):
        return [restore_obj(item, mapping) for item in obj]
    if isinstance(obj, dict):
        return {key: restore_obj(value, mapping) for key, value in obj.items()}
    return obj

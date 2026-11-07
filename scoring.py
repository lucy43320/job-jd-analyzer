"""硬条件核查：模型只负责抽取，判定交给本地规则。

为什么不让模型直接判：
    同一个岗位问两次，它给的"匹配度"能从 62 漂到 55——因为模型算的是
    "读起来像不像"，不是"第 3 条要求你到底满不满足"。这里把 JD 里明写的
    硬门槛（学历/年限/证书/专业/毕业年份）抽成结构化条目，用可复现的规则
    逐条比对，结论永远一致，而且能说清是卡在哪一条上。

设计原则：
    判得了的就判，判不了的明确说"要你自己确认"，不假装能判。
    硬性要求（must）不过才算"卡住"，软性的只做提示。
"""

from __future__ import annotations

import re

BUILD = 20261107161500

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"

# 学历等级：只用来比大小，不用来评价学校好坏
EDU_RANK = {
    "博士": 4, "硕士": 3, "研究生": 3, "本科": 2, "学士": 2,
    "大专": 1, "专科": 1, "高职": 1, "中专": 0, "高中": 0,
}

# 能明确判定"有没有"的证书。判不出来的一律走 UNKNOWN，不猜。
CERT_WORDS = (
    "初级会计", "中级会计", "高级会计", "注册会计师", "CPA", "ACCA", "CMA",
    "税务师", "CFA", "FRM", "证券从业", "基金从业", "期货从业",
    "法律职业资格", "CET-6", "英语六级", "CET-4", "英语四级",
    "雅思", "托福", "计算机二级",
)

# 财务岗常见的专业限制词
MAJOR_WORDS = (
    "会计", "财务", "审计", "财管", "财务管理", "税务", "财政", "金融",
    "经济", "统计", "管理", "商科", "经管",
)

CITY_WORDS = (
    "北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "武汉",
    "西安", "天津", "重庆", "厦门", "长沙", "青岛", "合肥", "郑州", "济南",
    "宁波", "无锡", "福州", "东莞", "佛山", "珠海", "昆明", "南昌", "沈阳",
)

KIND_ORDER = ["学历", "专业", "年限", "证书", "毕业年份", "语言", "城市", "薪资", "其他"]

VERDICT_CN = {PASS: "过", FAIL: "卡", UNKNOWN: "待确认"}


def _need_edu(req: str) -> int | None:
    """从要求文本里取**最低**学历门槛。

    "本科及以上，硕士优先" → 2（本科），不是 3。
    带"优先/加分"的分句直接跳过，否则会把加分项当成门槛。
    """
    best: int | None = None
    for seg in re.split(r"[，,、;；。]", req):
        if any(w in seg for w in ("优先", "加分", "更佳", "尤佳")):
            continue
        for word in sorted(EDU_RANK, key=len, reverse=True):
            if word in seg:
                rank = EDU_RANK[word]
                best = rank if best is None else min(best, rank)
                break
    return best


def _need_years(req: str) -> int | None:
    """取年限门槛的下限。"3-5 年" → 3，"三年以上"这里判不了，返回 None。"""
    if any(w in req for w in ("不限经验", "无经验", "应届", "经验不限")):
        return 0
    m = re.search(r"(\d+)\s*[-~～到至]\s*(\d+)\s*年", req)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*年(?:以上|及以上|或以上)?", req)
    if m:
        return int(m.group(1))
    return None


def _certs_in(text: str) -> set[str]:
    upper = text.upper()
    return {c for c in CERT_WORDS if c.upper() in upper}


def parse_profile(inputs: dict) -> dict:
    """把求职设置里的信息折算成可比对的量。

    只取能确定的：学历等级、全职年限、已有证书、目标城市、是否应届。
    取不到的留空，交给 judge 标"待确认"，不要瞎猜。
    """
    edu_text = str(inputs.get("education") or "")
    edu_rank: int | None = None
    for word in sorted(EDU_RANK, key=len, reverse=True):
        if word in edu_text:
            edu_rank = EDU_RANK[word]
            break

    material = " ".join(
        str(inputs.get(k) or "") for k in ("background", "resume", "preference", "identity")
    )
    certs = _certs_in(material)

    years = 0
    m = re.search(r"(\d+)\s*年(?:以上)?(?:的)?\s*(?:全职|工作|财务|相关|从业)?\s*经验", material)
    if m:
        years = int(m.group(1))

    fresh = any(w in material for w in ("应届", "在校", "在读", "毕业生", "2025 届", "2026 届", "2027 届", "2025届", "2026届", "2027届"))

    grad_year = None
    m = re.search(r"(20\d{2})\s*届", material)
    if m:
        grad_year = int(m.group(1))

    cities = {c for c in CITY_WORDS if c in material}

    return {
        "edu_rank": edu_rank,
        "edu_text": edu_text,
        "years": years,
        "certs": certs,
        "fresh": fresh,
        "grad_year": grad_year,
        "cities": cities,
        "material": material,
        "identity": str(inputs.get("identity") or ""),
    }


def _judge_one(kind: str, req: str, must: bool, prof: dict) -> tuple[str, str]:
    """判定单条要求，返回 (结论, 一句说明)。"""
    kind = (kind or "").strip()

    if kind == "学历":
        need = _need_edu(req)
        if need is None or prof["edu_rank"] is None:
            return UNKNOWN, "学历门槛没解析出来，或你的最高学历没填"
        if prof["edu_rank"] >= need:
            return PASS, f"要求{req}，你是{prof['edu_text'] or '已满足'}"
        return FAIL, f"要求{req}，你是{prof['edu_text'] or '低于门槛'}，这一条会被机器直接筛掉"

    if kind == "年限":
        need = _need_years(req)
        if need is None:
            return UNKNOWN, "年限要求没写成数字，自己核一下"
        if need == 0:
            return PASS, "对方不卡经验，应届可投"
        if prof["years"] >= need:
            return PASS, f"要求{need}年，你有{prof['years']}年"
        if prof["fresh"]:
            return UNKNOWN, f"要求{need}年经验，你是应届口径——实习算不算年限要问 HR"
        return FAIL, f"要求{need}年，你目前是{prof['years']}年"

    if kind == "证书":
        wanted = _certs_in(req)
        if not wanted:
            return UNKNOWN, "JD 没写具体证书名，自己核一下"
        missing = wanted - prof["certs"]
        if not missing:
            return PASS, f"要求{'/'.join(sorted(wanted))}，你的材料里有"
        if must:
            return FAIL, f"要求{'/'.join(sorted(missing))}，你的背景和简历里都没找到"
        return UNKNOWN, f"提到{'/'.join(sorted(missing))}，但属于加分项，不是必须"

    if kind == "专业":
        hit = {w for w in MAJOR_WORDS if w in req and w in prof["material"]}
        if hit:
            return PASS, f"专业方向对得上（{'/'.join(sorted(hit)[:3])}）"
        return UNKNOWN, f"要求{req}，你没填专业——跨专业能不能投要你自己判断"

    if kind == "毕业年份":
        m = re.search(r"(20\d{2})\s*届", req)
        if not m or prof["grad_year"] is None:
            return UNKNOWN, "届别要求或你的毕业届别没填清楚"
        if int(m.group(1)) == prof["grad_year"]:
            return PASS, f"要求{m.group(1)}届，你是同届"
        return FAIL, f"要求{m.group(1)}届，你是{prof['grad_year']}届"

    if kind == "语言":
        wanted = _certs_in(req) | {w for w in ("英语", "日语", "韩语", "粤语") if w in req}
        have = {w for w in wanted if w.upper() in prof["material"].upper()}
        if have:
            return PASS, f"语言要求（{'/'.join(sorted(have))}）你的材料里有体现"
        return UNKNOWN, f"要求{req}，材料里没找到对应证明，面试可能会问"

    if kind == "城市":
        hit = {c for c in CITY_WORDS if c in req and c in prof["cities"]}
        if hit:
            return PASS, f"{'/'.join(sorted(hit))}在你的目标城市里"
        return UNKNOWN, "工作地点和你的目标城市不一致，要不要去你自己定"

    if kind == "薪资":
        return UNKNOWN, "薪资是否可接受，只能你自己判断"

    return UNKNOWN, "这条要靠你自己核"


def judge(conditions: list[dict], inputs: dict) -> tuple[list[dict], dict]:
    """逐条判定，返回 (明细行, 汇总)。"""
    prof = parse_profile(inputs)
    rows: list[dict] = []

    for item in conditions or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "其他").strip()
        req = str(item.get("requirement") or "").strip()
        if not req:
            continue
        must = item.get("must")
        must = True if must is None else bool(must)
        verdict, note = _judge_one(kind, req, must, prof)
        rows.append({
            "kind": kind,
            "requirement": req,
            "quote": str(item.get("quote") or "").strip(),
            "must": must,
            "verdict": verdict,
            "note": note,
        })

    rows.sort(key=lambda r: (
        KIND_ORDER.index(r["kind"]) if r["kind"] in KIND_ORDER else len(KIND_ORDER),
        {FAIL: 0, UNKNOWN: 1, PASS: 2}[r["verdict"]],
    ))

    return rows, summarize(rows)


def summarize(rows: list[dict]) -> dict:
    passed = [r for r in rows if r["verdict"] == PASS]
    failed = [r for r in rows if r["verdict"] == FAIL]
    unknown = [r for r in rows if r["verdict"] == UNKNOWN]
    blocked = [r for r in failed if r["must"]]

    if not rows:
        gate = "这份记录里没有硬条件数据，重跑一次分析即可生成"
        tone = "muted"
    elif blocked:
        names = "、".join(f"{r['kind']}（{r['requirement']}）" for r in blocked[:3])
        gate = f"卡在 {len(blocked)} 条硬性要求上：{names}"
        tone = "bad"
    elif failed:
        gate = f"{len(failed)} 条不满足，但都属加分项，不一定会被筛掉"
        tone = "warn"
    elif unknown:
        gate = f"没有明显冲突，但有 {len(unknown)} 条要你自己确认"
        tone = "warn"
    else:
        gate = "硬条件全部通过"
        tone = "good"

    judged = len(passed) + len(failed)
    score = round(len(passed) / judged * 5, 1) if judged else None

    return {
        "pass": len(passed),
        "fail": len(failed),
        "unknown": len(unknown),
        "blocked": len(blocked),
        "gate": gate,
        "tone": tone,
        "score": score,
        "rows": len(rows),
    }


def render(rows: list[dict], summ: dict) -> str:
    """硬条件核查表。样式类在 app.py 的 CSS 里（.hc-*）。"""
    import html as _html

    if not rows:
        return (
            '<div class="hc-empty">这份记录早于硬条件核查功能，'
            "在「🗂 历史记录」里重新分析一次就能生成</div>"
        )

    parts = [
        '<div class="hc">',
        '<div class="hc-head">',
        f'<span class="hc-badge {summ["tone"]}">{_html.escape(summ["gate"])}</span>',
        f'<span class="hc-count">{summ["pass"]} 过 / {summ["fail"]} 卡 / '
        f'{summ["unknown"]} 待确认</span>',
        "</div>",
    ]

    for row in rows:
        verdict = row["verdict"]
        mark = {"pass": "✓", "fail": "✕", "unknown": "?"}[verdict]
        req = _html.escape(row["requirement"])
        note = _html.escape(row["note"])
        kind = _html.escape(row["kind"])
        star = '<span class="hc-must">硬性</span>' if row["must"] else ""

        # 把 JD 原句附在结论下面：模型有没有抄错、是不是它自己编的门槛，一眼能核
        note_html = f'<span class="hc-note">{note}'
        if row.get("quote"):
            note_html += (
                f'<span class="hc-quote">JD 原文：{_html.escape(row["quote"])}</span>'
            )
        note_html += "</span>"

        parts.append(
            f'<div class="hc-row {verdict}">'
            f'<span class="hc-mark">{mark}</span>'
            f'<span class="hc-kind">{kind}</span>'
            f'<span class="hc-req">{req}{star}</span>'
            f"{note_html}"
            "</div>"
        )

    parts.append("</div>")
    return "".join(parts)

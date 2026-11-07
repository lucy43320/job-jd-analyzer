"""投递漏斗：用已有数据算转化率，指出问题出在哪个环节。

动机：投了 18 份只拿到 2 个面试，和投了 18 份拿到 12 个面试，
     该改的东西完全不一样。但人自己数不清这个，工具能数清。
     全部本地计算，不调模型，不花钱。
"""

from __future__ import annotations

import html

BUILD = 20261107161500

# 每一级"低转化"对应的真实原因和该做的事
DIAGNOSIS = {
    "已投递": ("这批岗位还没进筛选环节，先等等，或者补一批新投递", "info"),
    "简历筛": ("简历关过得少 → 问题在简历本身：关键词没对上 JD、量化数据缺失", "bad"),
    "笔试": ("笔试通过率偏低 → 财会基础题/行测要单独练，或者投的岗位笔试难度不匹配", "bad"),
    "一面": ("一面通过率低 → 问题在表达：自我介绍、项目讲述、财务实操题没过关", "bad"),
    "二面": ("二面卡住 → 通常输在业务深度和动机，面试官觉得你只会执行", "warn"),
    "HR面": ("HR 面没过 → 稳定性、薪资预期、到岗时间这几项可能有分歧", "warn"),
    "Offer": ("走到最后没成 → 竞争或薪资，属于外部因素，不必全归到自己身上", "info"),
}

STEP_HINT = {
    "简历筛": "简历关",
    "笔试": "笔试关",
    "一面": "一面关",
    "二面": "二面关",
    "HR面": "HR 面",
    "Offer": "终局",
}


def build(jobs: list[dict], stages: list[str]) -> dict:
    """按"当前阶段"倒推每一关的通过人数。

    阶段是"现在停在哪"，不是"过了几关"，所以用 stage_index >= i 累计。
    「未投递」不进漏斗——它还没开始，算进去会把所有转化率稀释得很难看。
    """
    index = {name: i for i, name in enumerate(stages)}
    entered: list[int] = []
    no_stage = 0

    for job in jobs:
        meta = job.get("meta") or {}
        stage = str(meta.get("stage") or "未投递")
        i = index.get(stage)
        if i is None or i == 0:
            no_stage += 1
            continue
        entered.append(i)

    total = len(entered)
    rows: list[dict] = []
    prev = total

    for i, name in enumerate(stages):
        if i == 0:
            continue
        count = sum(1 for v in entered if v >= i)
        rate = (count / prev) if prev else 0.0
        rows.append({
            "stage": name,
            "count": count,
            "rate": rate,
            "overall": (count / total) if total else 0.0,
        })
        prev = count

    return {
        "rows": rows,
        "total": total,
        "not_applied": no_stage,
        "worst": diagnose(rows, total),
    }


def diagnose(rows: list[dict], total: int) -> dict | None:
    """找流失最狠的那一级。样本太少就不下结论，免得误导。"""
    if total < 5:
        return {
            "stage": "",
            "tone": "info",
            "text": f"有效投递只有 {total} 份，样本太少，先攒到 8 份以上再看转化率才有意义。",
        }

    worst = None
    for row in rows:
        if row["count"] == 0 and row["rate"] == 0:
            continue
        if worst is None or row["rate"] < worst["rate"]:
            worst = row

    if worst is None:
        return None

    # 掉得不算狠就别硬找问题
    if worst["rate"] >= 0.5:
        return {
            "stage": worst["stage"],
            "tone": "good",
            "text": "每一关的转化率都在 50% 以上，节奏是健康的，问题只是投得还不够多。",
        }

    text, tone = DIAGNOSIS.get(worst["stage"], ("这一关流失最多，值得单独复盘", "warn"))
    return {
        "stage": worst["stage"],
        "tone": tone,
        "text": f"{STEP_HINT.get(worst['stage'], worst['stage'])}只过了 {worst['rate']:.0%}"
                f"（{worst['count']} / 上一级）。{text}",
    }


def render(funnel: dict) -> str:
    """横向漏斗条 + 一句诊断。样式类在 app.py 的 CSS 里（.fn-*）。"""
    rows = funnel.get("rows") or []
    if not rows or funnel["total"] == 0:
        return (
            '<div class="fn-empty">还没有进入流程的投递。'
            "到「📮 投递追踪」把岗位的阶段往下推一格，这里就开始出数了。</div>"
        )

    total = funnel["total"]
    parts = ['<div class="fn">']

    for row in rows:
        pct = row["overall"] * 100
        width = max(pct, 0.6)
        step = f'{row["rate"]:.0%}' if row["count"] else "—"
        parts.append(
            '<div class="fn-row">'
            f'<span class="fn-name">{html.escape(row["stage"])}</span>'
            '<span class="fn-track">'
            f'<span class="fn-bar" style="width:{width:.1f}%"></span>'
            "</span>"
            f'<span class="fn-num">{row["count"]}</span>'
            f'<span class="fn-step">{step}</span>'
            "</div>"
        )

    parts.append(
        f'<div class="fn-foot">有效投递 <b>{total}</b> 份'
        + (f'，另有 {funnel["not_applied"]} 份还没投' if funnel["not_applied"] else "")
        + "</div>"
    )

    worst = funnel.get("worst")
    if worst and worst.get("text"):
        parts.append(
            f'<div class="fn-diag {worst["tone"]}">{html.escape(worst["text"])}</div>'
        )

    parts.append("</div>")
    return "".join(parts)

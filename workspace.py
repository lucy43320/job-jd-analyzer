"""工作台扩展模块：把「分析一个 JD」之外的全流程管理补齐。

包含：简历中心、Offer 管理、心态日记、能力分析、人脉内推 / 学习计划 / 资料库、
秋招节奏时间线、AI 求职助手、社招板块。

两条硬约束，写代码前先想清楚：
  1. 不新增第三方依赖。图表全部用内联 SVG 画（requirements.txt 里只有
     streamlit / openai / pypdf / python-docx），否则云端部署会多一份装不上的风险。
  2. 不联网。除了 AI 助手页那次用户主动点的模型调用，其余全是本地读写。
     数据统一走 store 的 blob（任意 JSON），落进本机 data.json。
"""

from __future__ import annotations

import csv
import html
import io
import math
from datetime import date, datetime, timedelta

import streamlit as st

import store

BUILD = 20261107160000


# ==========================================================================
# 一、基础工具
# ==========================================================================


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def today() -> date:
    return date.today()


def blob(data: dict, key: str, default):
    """读 blob；没存过就返回 default 的副本，避免调用方改到共享默认值。"""
    value = store.get_blob(data, key, None)
    if value is None:
        if isinstance(default, list):
            return list(default)
        if isinstance(default, dict):
            return dict(default)
        return default
    return value


def put(data: dict, key: str, value) -> None:
    store.set_blob(data, key, value)
    store.save(data)


def num(value, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def days_left(text: str) -> int | None:
    """距某个日期还有几天；解析不了返回 None（不瞎猜）。"""
    raw = str(text or "").strip()[:10]
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return (datetime.strptime(raw, fmt).date() - today()).days
        except ValueError:
            continue
    return None


def statrow(pairs) -> None:
    st.markdown(
        '<div class="statrow">'
        + "".join(
            f'<div class="s"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>'
            for k, v in pairs
        )
        + "</div>",
        unsafe_allow_html=True,
    )


def page_head(icon: str, title: str, desc: str) -> None:
    st.markdown(f"### {icon}　{title}")
    if desc:
        st.caption(desc)


def to_csv(headers: list[str], rows: list[list]) -> str:
    """导出 CSV。带 BOM，否则 Excel 打开中文是乱码。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return "﻿" + buf.getvalue()


# ==========================================================================
# 二、本页专用样式（只在第一次渲染时注入）
# ==========================================================================

WS_CSS = """
.ws-card{border:1px solid var(--line);border-radius:var(--radius);background:var(--card);
  padding:.85rem 1rem;margin:.45rem 0;position:relative;overflow:hidden;}
.ws-card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--acc),var(--acc2));}
.ws-card h5{margin:.1rem 0 .35rem 0;font-size:.98rem;}
.ws-card h5 .ws-tag{margin-left:.45rem;font-weight:500;}
.ws-meta{font-size:.76rem;opacity:.62;line-height:1.7;}
.ws-tag{display:inline-block;font-size:.70rem;padding:.08rem .48rem;margin:.12rem .18rem .12rem 0;
  border-radius:999px;border:1px solid var(--acc-line);background:var(--soft);color:var(--acc2);}
.ws-score{font-size:1.5rem;font-weight:800;color:var(--acc2);line-height:1.1;}
.ws-chart{width:100%;overflow-x:auto;}
.ws-chart svg{width:100%;height:auto;min-width:280px;display:block;}
.ws-note{border:1px dashed var(--acc-line);background:var(--soft);border-radius:12px;
  padding:.6rem .8rem;font-size:.82rem;line-height:1.75;margin:.4rem 0;}
.ws-warn{border-left:3px solid #d9863c;background:rgba(217,134,60,.10);
  border-radius:0 12px 12px 0;padding:.6rem .8rem;font-size:.84rem;line-height:1.75;margin:.4rem 0;}
.ws-ok{border-left:3px solid #4c9a6c;background:rgba(76,154,108,.10);
  border-radius:0 12px 12px 0;padding:.6rem .8rem;font-size:.84rem;line-height:1.75;margin:.4rem 0;}
.ws-tl{border-left:2px solid var(--acc-line);margin-left:.45rem;padding-left:1rem;}
.ws-tl-item{position:relative;padding:.1rem 0 .75rem 0;}
.ws-tl-item:before{content:"";position:absolute;left:-1.36rem;top:.35rem;width:9px;height:9px;
  border-radius:50%;background:var(--acc);box-shadow:0 0 0 3px var(--soft);}
.ws-tl-item.past:before{background:transparent;border:2px solid var(--acc-line);box-shadow:none;}
.ws-tl-date{font-size:.74rem;opacity:.6;}
.ws-funnel-row{display:flex;align-items:center;gap:.5rem;margin:.22rem 0;font-size:.8rem;}
.ws-funnel-label{width:5.2rem;flex:none;opacity:.75;}
.ws-funnel-bar{height:20px;border-radius:6px;background:linear-gradient(90deg,var(--acc),var(--acc2));
  min-width:2px;transition:width .3s;}
.ws-funnel-num{flex:none;font-size:.76rem;opacity:.7;}
.ws-empty{border:1px dashed var(--line);border-radius:12px;padding:1.1rem;text-align:center;
  font-size:.85rem;opacity:.6;margin:.5rem 0;}
"""


def ensure_css() -> None:
    if st.session_state.get("_ws_css"):
        return
    st.markdown(f"<style>{WS_CSS}</style>", unsafe_allow_html=True)
    st.session_state["_ws_css"] = True


def empty(text: str) -> None:
    st.markdown(f'<div class="ws-empty">{esc(text)}</div>', unsafe_allow_html=True)


# ==========================================================================
# 三、纯 SVG 图表（不依赖 plotly / matplotlib）
#    文字用 currentColor，跟随 Streamlit 的深浅色主题，不用硬编码颜色。
# ==========================================================================


def _svg(body: str, width: int, height: int) -> str:
    return (
        f'<div class="ws-chart"><svg viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">{body}</svg></div>'
    )


def _txt(x: float, y: float, text: str, size: int = 11, anchor: str = "middle",
         opacity: float = 0.8, weight: str = "normal") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" '
        f'fill="currentColor" opacity="{opacity}" font-weight="{weight}" '
        f'font-family="inherit">{esc(text)}</text>'
    )


def svg_radar(items: list[dict], size: int = 340, maximum: float = 10.0) -> str:
    """能力雷达：自己 vs 同龄均值。items = [{'name','mine','peer'}]"""
    items = [i for i in items if str(i.get("name") or "").strip()]
    if len(items) < 3:
        return '<div class="ws-empty">雷达图至少需要 3 个能力项，先在下面添加</div>'
    cx = cy = size / 2
    radius = size * 0.31
    n = len(items)
    angles = [-math.pi / 2 + 2 * math.pi * k / n for k in range(n)]

    def point(k: int, ratio: float) -> tuple[float, float]:
        ratio = max(0.0, min(1.0, ratio))
        return cx + radius * ratio * math.cos(angles[k]), cy + radius * ratio * math.sin(angles[k])

    parts: list[str] = []
    for ring in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (point(k, ring) for k in range(n)))
        parts.append(
            f'<polygon points="{pts}" fill="none" stroke="currentColor" '
            f'stroke-opacity="{0.10 if ring < 1 else 0.22}" stroke-width="1"/>'
        )
    for k in range(n):
        x, y = point(k, 1.0)
        parts.append(
            f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
            f'stroke="currentColor" stroke-opacity="0.12"/>'
        )

    for key, color, fill_op in (("peer", "var(--acc2)", 0.10), ("mine", "var(--acc)", 0.22)):
        pts = " ".join(
            f"{x:.1f},{y:.1f}"
            for x, y in (point(k, num(items[k].get(key)) / maximum) for k in range(n))
        )
        parts.append(
            f'<polygon points="{pts}" fill="{color}" fill-opacity="{fill_op}" '
            f'stroke="{color}" stroke-width="2" stroke-linejoin="round"/>'
        )
        for k in range(n):
            x, y = point(k, num(items[k].get(key)) / maximum)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')

    for k, item in enumerate(items):
        lx, ly = point(k, 1.28)
        anchor = "middle"
        if lx < cx - 6:
            anchor = "end"
        elif lx > cx + 6:
            anchor = "start"
        label = f'{item.get("name")} {num(item.get("mine")):g}'
        parts.append(_txt(lx, ly + 4, label, size=12, anchor=anchor, opacity=0.9))

    parts.append(_txt(cx, size - 6, "实心=我的评分　外框=同龄均值", size=11, opacity=0.5))
    return _svg("".join(parts), size, size)


def svg_bars(rows: list[tuple[str, float]], maximum: float | None = None,
             unit: str = "", width: int = 620, row_h: int = 30) -> str:
    """横向条形图：标签在左，数值在右，适合技能打分 / Offer 对比。"""
    if not rows:
        return '<div class="ws-empty">还没有数据</div>'
    top = max((v for _, v in rows), default=0.0)
    maximum = maximum or (top if top > 0 else 1.0)
    maximum = max(maximum, 0.0001)
    height = row_h * len(rows) + 12
    label_w = 132
    bar_w = width - label_w - 74
    parts: list[str] = []
    for i, (label, value) in enumerate(rows):
        y = 8 + i * row_h
        w = max(2.0, bar_w * min(1.0, max(0.0, value) / maximum))
        parts.append(_txt(label_w - 8, y + row_h / 2 + 4, str(label)[:16], size=12, anchor="end"))
        parts.append(
            f'<rect x="{label_w}" y="{y + 5}" width="{bar_w}" height="{row_h - 12}" rx="5" '
            f'fill="currentColor" opacity="0.06"/>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y + 5}" width="{w:.1f}" height="{row_h - 12}" rx="5" '
            f'fill="var(--acc)"/>'
        )
        parts.append(
            _txt(label_w + bar_w + 8, y + row_h / 2 + 4, f"{value:g}{unit}", size=12, anchor="start", weight="bold")
        )
    return _svg("".join(parts), width, height)


def svg_group_bars(categories: list[str], series: list[tuple[str, list[float], str]],
                   width: int = 660, height: int = 250, unit: str = "") -> str:
    """分组柱状图：一个类别多根柱子。series = [(名称, [值...], 颜色)]"""
    if not categories or not series:
        return '<div class="ws-empty">还没有数据</div>'
    left, bottom, top_pad = 44, 42, 26
    plot_w = width - left - 12
    plot_h = height - bottom - top_pad
    peak = max([v for _, vals, _ in series for v in vals] + [1.0])
    parts: list[str] = []
    for step in range(5):
        value = peak * step / 4
        y = top_pad + plot_h - plot_h * step / 4
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - 12}" y2="{y:.1f}" '
            f'stroke="currentColor" stroke-opacity="0.10"/>'
        )
        parts.append(_txt(left - 6, y + 4, f"{value:.0f}", size=10, anchor="end", opacity=0.5))
    group_w = plot_w / len(categories)
    n = len(series)
    bar_w = min(26.0, group_w * 0.72 / n)
    for ci, cat in enumerate(categories):
        gx = left + group_w * ci + group_w / 2
        for si, (_, vals, color) in enumerate(series):
            value = vals[ci] if ci < len(vals) else 0.0
            h = plot_h * (value / peak) if peak else 0
            x = gx - (n * bar_w) / 2 + si * bar_w
            y = top_pad + plot_h - h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w - 3:.1f}" height="{max(h, 1):.1f}" '
                f'rx="3" fill="{color}"><title>{esc(cat)} {value:g}{esc(unit)}</title></rect>'
            )
        parts.append(_txt(gx, height - bottom + 18, str(cat)[:9], size=11))
    lx = left
    for name, _, color in series:
        parts.append(f'<rect x="{lx}" y="6" width="10" height="10" rx="2" fill="{color}"/>')
        parts.append(_txt(lx + 15, 15, name, size=11, anchor="start", opacity=0.8))
        lx += 22 + len(name) * 12
    return _svg("".join(parts), width, height)


def svg_line(series: list[tuple[str, list[float], str]], labels: list[str],
             width: int = 660, height: int = 230, maximum: float | None = None) -> str:
    """折线图：心境曲线 / 投递趋势。支持多条线。"""
    if not labels or not series:
        return '<div class="ws-empty">还没有数据</div>'
    left, bottom, top_pad = 36, 34, 24
    plot_w = width - left - 16
    plot_h = height - bottom - top_pad
    peak = maximum or max([v for _, vals, _ in series for v in vals] + [1.0])
    peak = max(peak, 1.0)
    count = len(labels)
    parts: list[str] = []
    for step in range(5):
        value = peak * step / 4
        y = top_pad + plot_h - plot_h * step / 4
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - 16}" y2="{y:.1f}" '
            f'stroke="currentColor" stroke-opacity="0.10"/>'
        )
        parts.append(_txt(left - 6, y + 4, f"{value:.0f}", size=10, anchor="end", opacity=0.5))

    def xy(index: int, value: float) -> tuple[float, float]:
        x = left + (plot_w * index / (count - 1) if count > 1 else plot_w / 2)
        y = top_pad + plot_h - plot_h * (value / peak)
        return x, y

    for name, vals, color in series:
        pts = [xy(i, vals[i] if i < len(vals) else 0.0) for i in range(count)]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(
            f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2.2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for i, (x, y) in enumerate(pts):
            value = vals[i] if i < len(vals) else 0.0
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}">'
                f'<title>{esc(labels[i])} {esc(name)} {value:g}</title></circle>'
            )
    step = max(1, count // 8)
    for i in range(0, count, step):
        x, _ = xy(i, 0)
        parts.append(_txt(x, height - bottom + 18, str(labels[i]), size=10, opacity=0.6))
    lx = left
    for name, _, color in series:
        parts.append(f'<rect x="{lx}" y="5" width="10" height="10" rx="2" fill="{color}"/>')
        parts.append(_txt(lx + 15, 14, name, size=11, anchor="start", opacity=0.8))
        lx += 22 + len(name) * 12
    return _svg("".join(parts), width, height)


def svg_donut(parts_in: list[tuple[str, float, str]], size: int = 210,
              center: str = "", width: int = 400) -> str:
    """环形图：心情分布。parts = [(标签, 值, 颜色)]"""
    total = sum(v for _, v, _ in parts_in)
    if total <= 0:
        return '<div class="ws-empty">还没有数据</div>'
    cx, cy, radius = size / 2, size / 2, size * 0.34
    thick = size * 0.15
    parts: list[str] = []
    angle = -math.pi / 2
    for label, value, color in parts_in:
        if value <= 0:
            continue
        sweep = 2 * math.pi * value / total
        large = 1 if sweep > math.pi else 0
        x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        angle2 = angle + sweep
        x2, y2 = cx + radius * math.cos(angle2), cy + radius * math.sin(angle2)
        if sweep >= 2 * math.pi - 0.001:
            parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{color}" stroke-width="{thick}"/>'
            )
        else:
            parts.append(
                f'<path d="M{x1:.2f},{y1:.2f} A{radius},{radius} 0 {large} 1 {x2:.2f},{y2:.2f}" '
                f'fill="none" stroke="{color}" stroke-width="{thick}">'
                f'<title>{esc(label)} {value:g}（{value / total * 100:.0f}%）</title></path>'
            )
        angle = angle2
    parts.append(_txt(cx, cy + 5, center or f"{total:g}", size=22, weight="bold"))
    ly = 30
    for label, value, color in parts_in:
        parts.append(f'<rect x="{size + 12}" y="{ly - 9}" width="10" height="10" rx="2" fill="{color}"/>')
        parts.append(
            _txt(size + 28, ly, f"{label}　{value:g} 天（{value / total * 100:.0f}%）", size=12, anchor="start")
        )
        ly += 24
    return _svg("".join(parts), width, size)


def svg_progress(ratio: float, label: str = "", width: int = 620, height: int = 52) -> str:
    """进度条。标签在条上方单独一行：之前把文字画在 y=4，上半截被 viewBox 裁掉了。"""
    ratio = max(0.0, min(1.0, ratio))
    bar_w = width - 76
    body = (
        (_txt(0, 15, label, size=12, anchor="start", opacity=0.8) if label else "")
        + f'<rect x="0" y="26" width="{bar_w}" height="14" rx="7" fill="currentColor" opacity="0.07"/>'
        + f'<rect x="0" y="26" width="{max(3, bar_w * ratio):.1f}" height="14" rx="7" fill="var(--acc)"/>'
        + _txt(bar_w + 10, 38, f"{ratio * 100:.0f}%", size=13, anchor="start", weight="bold")
    )
    return _svg(body, width, height if label else height - 20)


def funnel_html(rows: list[tuple[str, int]]) -> str:
    """转化漏斗：人脉内推用。纯 HTML 横条，比图更省地方。"""
    top = max([v for _, v in rows] + [1])
    out = []
    for label, value in rows:
        out.append(
            f'<div class="ws-funnel-row"><div class="ws-funnel-label">{esc(label)}</div>'
            f'<div class="ws-funnel-bar" style="width:{value / top * 100:.0f}%"></div>'
            f'<div class="ws-funnel-num">{value} 人</div></div>'
        )
    return "".join(out)


# ==========================================================================
# 三·五、首页驾驶舱：KPI + 红色预警 + 今日待办
#    由 app.py 的 render_home 调用，events 用主程序已有的日程归集结果，不重复算。
# ==========================================================================


def render_dashboard(data: dict, events: dict) -> None:
    ensure_css()
    jobs = data.get("jobs", [])
    offers = blob(data, "offers", [])
    plans = blob(data, "learning", [])
    todos = blob(data, "todos", [])

    sent = [j for j in jobs if str(j.get("meta", {}).get("stage") or "") not in ("", "未投递")]
    replied = [j for j in sent if str(j.get("meta", {}).get("stage") or "") not in ("已投递",)]
    statrow([
        ("目标岗位", f"{len(jobs)}"),
        ("已投递", f"{len(sent)}"),
        ("回复率", f"{len(replied) / len(sent) * 100:.0f}%" if sent else "—"),
        ("Offer", f"{len(offers)}"),
        ("进行中任务", f"{sum(1 for p in plans if num(p.get('progress')) < 100)}"),
    ])

    # 红色预警：3 天内的硬节点。超过 3 天不打扰，避免预警疲劳。
    urgent = []
    for day, items in (events or {}).items():
        left = (day - today()).days
        if 0 <= left <= 3:
            for item in items:
                label = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else str(item)
                urgent.append((left, day, label))
    if urgent:
        lines = "".join(
            f'<div>🔴 <b>{esc(label)}</b>　·　{day.isoformat()}　·　'
            f'{"<b>就是今天</b>" if left == 0 else f"还剩 {left} 天"}</div>'
            for left, day, label in sorted(urgent, key=lambda x: x[0])
        )
        st.markdown(f'<div class="ws-warn"><b>⏰ 即将截止，先处理这些</b>{lines}</div>', unsafe_allow_html=True)

    st.markdown("**✅ 今日待办**")
    open_todos = [t for t in todos if not t.get("done")]
    if not open_todos:
        st.caption("待办是空的。可以在下面加一条，或者从系统建议里挑一条。")
    for t in todos:
        tid = t.get("id", "")
        done = st.checkbox(str(t.get("text")), value=bool(t.get("done")), key=f"td_{tid}")
        if done != bool(t.get("done")):
            t["done"] = done
            put(data, "todos", todos)
            st.rerun()

    c1, c2 = st.columns([3, 1])
    new_todo = c1.text_input("新待办", key="td_new", label_visibility="collapsed",
                             placeholder="如：给中建电商补一份自我介绍")
    if c2.button("加待办", use_container_width=True):
        if new_todo.strip():
            todos.append({"id": store.new_id("td"), "text": new_todo.strip(), "done": False})
            put(data, "todos", todos)
            st.session_state["td_new"] = ""
            st.rerun()

    # 系统建议：只列不强制，点一下才进待办，避免首页被自动任务刷屏
    suggestions: list[str] = []
    for p in plans:
        left = days_left(p.get("due"))
        if left is not None and left <= 3 and num(p.get("progress")) < 100:
            suggestions.append(f'学习任务「{p.get("topic")}」{"已逾期" if left < 0 else f"{left} 天后到期"}，当前 {num(p.get("progress")):g}%')
    existing = {str(t.get("text")) for t in todos}
    suggestions = [s for s in suggestions if s not in existing][:3]
    if suggestions:
        st.caption("系统建议（点按钮才会加进待办）：")
        for i, text in enumerate(suggestions):
            if st.button(f"＋ {text}", key=f"td_sug_{i}", use_container_width=True):
                todos.append({"id": store.new_id("td"), "text": text, "done": False})
                put(data, "todos", todos)
                st.rerun()
    st.divider()


# ==========================================================================
# 四、简历中心：多方向版本管理 + 项目经历库
# ==========================================================================


def _resume_stats(items: list[dict]) -> list[tuple[str, str]]:
    applied = sum(int(num(i.get("applied"))) for i in items)
    replies = sum(int(num(i.get("replies"))) for i in items)
    scored = [num(i.get("score")) for i in items if num(i.get("score")) > 0]
    return [
        ("简历版本", f"{len(items)}"),
        ("累计投递", f"{applied}"),
        ("累计回复", f"{replies}"),
        ("整体回复率", f"{replies / applied * 100:.0f}%" if applied else "—"),
        ("平均自评分", f"{sum(scored) / len(scored):.1f}" if scored else "—"),
    ]


def render_resumes_page(data: dict, ai=None) -> None:
    ensure_css()
    page_head("📄", "简历中心", "一个方向一份简历，别再拿同一份海投。投递数和回复率自己回填，才看得出哪份真的有效。")
    resumes = blob(data, "resumes", [])
    projects = blob(data, "projects", [])
    statrow(_resume_stats(resumes))

    tab_list, tab_proj = st.tabs(["🗂 简历版本", "🧱 项目经历库"])

    with tab_list:
        if not resumes:
            empty("还没有简历版本。点下面的「新增简历版本」，先建一份主简历。")
        for item in resumes:
            rid = item.get("id", "")
            rate = ""
            if num(item.get("applied")) > 0:
                rate = f"　·　回复率 {num(item.get('replies')) / num(item.get('applied')) * 100:.0f}%"
            st.markdown(
                f'<div class="ws-card"><h5>{esc(item.get("name"))}'
                f'<span class="ws-tag">{esc(item.get("version"))}</span></h5>'
                f'<div class="ws-meta">{esc(item.get("direction"))}　·　'
                f'{int(num(item.get("pages")))} 页　·　投递 {int(num(item.get("applied")))} 次{rate}'
                f'　·　更新于 {esc(item.get("updated"))}</div>'
                + "".join(f'<span class="ws-tag">{esc(t)}</span>' for t in item.get("tags", []) if t)
                + (f'<div class="ws-meta" style="margin-top:.3rem">{esc(item.get("note"))}</div>' if item.get("note") else "")
                + "</div>",
                unsafe_allow_html=True,
            )
            with st.expander(f"✏️ 编辑 / 回填数据 · {item.get('name')}", expanded=False):
                c1, c2, c3 = st.columns(3)
                name = c1.text_input("版本名称", value=item.get("name", ""), key=f"rs_name_{rid}")
                version = c2.text_input("版本号", value=item.get("version", "v1.0"), key=f"rs_ver_{rid}")
                direction = c3.text_input("投递方向", value=item.get("direction", ""), key=f"rs_dir_{rid}")
                c4, c5, c6, c7 = st.columns(4)
                score = c4.slider("自评分", 0, 100, int(num(item.get("score"), 80)), key=f"rs_score_{rid}")
                pages = c5.number_input("页数", 1, 5, int(num(item.get("pages"), 1)), key=f"rs_pg_{rid}")
                applied = c6.number_input("累计投递", 0, 9999, int(num(item.get("applied"))), key=f"rs_ap_{rid}")
                replies = c7.number_input("累计回复", 0, 9999, int(num(item.get("replies"))), key=f"rs_rp_{rid}")
                tags = st.text_input("标签（逗号分隔）", value="，".join(item.get("tags", [])), key=f"rs_tag_{rid}")
                note = st.text_area("这份简历的定位与取舍", value=item.get("note", ""), height=80, key=f"rs_note_{rid}")
                b1, b2 = st.columns([1, 1])
                if b1.button("💾 保存这个版本", key=f"rs_save_{rid}", use_container_width=True):
                    item.update({
                        "name": name.strip() or item.get("name"), "version": version.strip(),
                        "direction": direction.strip(), "score": score, "pages": int(pages),
                        "applied": int(applied), "replies": int(replies),
                        "tags": [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()],
                        "note": note.strip(), "updated": today().isoformat(),
                    })
                    put(data, "resumes", resumes)
                    st.success("已保存")
                    st.rerun()
                if b2.button("🗑 删除这个版本", key=f"rs_del_{rid}", use_container_width=True):
                    put(data, "resumes", [r for r in resumes if r.get("id") != rid])
                    st.rerun()

        with st.expander("➕ 新增简历版本", expanded=not resumes):
            with st.form("rs_new", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                name = c1.text_input("版本名称 *", placeholder="如：核算会计主简历")
                version = c2.text_input("版本号", value="v1.0")
                direction = c3.text_input("投递方向", placeholder="如：制造业核算 / 事务所审计 / TMT 战略")
                c4, c5 = st.columns(2)
                score = c4.slider("自评分", 0, 100, 80, key="rs_new_score")
                pages = c5.number_input("页数", 1, 5, 1, key="rs_new_pages")
                tags = st.text_input("标签（逗号分隔）", placeholder="核算,制造业,可接受包住")
                note = st.text_area("这份简历的定位与取舍", height=80,
                                    placeholder="突出哪段经历、删掉了什么、为什么这样排")
                if st.form_submit_button("保存简历版本", use_container_width=True):
                    if not name.strip():
                        st.error("版本名称不能为空")
                    else:
                        resumes.insert(0, {
                            "id": store.new_id("rs"), "name": name.strip(), "version": version.strip(),
                            "direction": direction.strip(), "score": score, "pages": int(pages),
                            "applied": 0, "replies": 0,
                            "tags": [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()],
                            "note": note.strip(), "updated": today().isoformat(),
                        })
                        put(data, "resumes", resumes)
                        st.rerun()

    with tab_proj:
        st.caption("项目经历是简历里最能拉开差距的部分。每个项目写清：用了什么、做了什么、量化结果是什么。")
        if not projects:
            empty("还没有项目经历。可以先把你做过的量化研究、课程项目、实习里的专项写进来。")
        for proj in projects:
            pid = proj.get("id", "")
            st.markdown(
                f'<div class="ws-card"><h5>{esc(proj.get("name"))}</h5>'
                + "".join(f'<span class="ws-tag">{esc(t)}</span>' for t in proj.get("stack", []) if t)
                + f'<div class="ws-meta" style="margin-top:.35rem">{esc(proj.get("desc"))}</div>'
                + (f'<div class="ws-ok" style="margin-top:.4rem">📈 量化成果：{esc(proj.get("result"))}</div>'
                   if proj.get("result") else '<div class="ws-warn" style="margin-top:.4rem">还缺量化成果：数字（样本量、提升百分比、节约工时）比形容词有用得多。</div>')
                + "</div>",
                unsafe_allow_html=True,
            )
            c1, c2 = st.columns([1, 1])
            if ai and c1.button("🤖 AI 优化这段描述", key=f"pj_ai_{pid}", use_container_width=True):
                with st.spinner("正在改写…"):
                    try:
                        text = ai(
                            "你是简历顾问，只输出优化后的中文项目描述，不要解释、不要标题。",
                            "把这段项目经历改写成简历上 2-3 条要点，每条以动词开头，"
                            "保留事实，不要编造数据；没有数字的地方用「可补充：xxx」提示。\n\n"
                            f"项目：{proj.get('name')}\n技术/方法：{'、'.join(proj.get('stack', []))}\n"
                            f"原描述：{proj.get('desc')}\n已有成果：{proj.get('result')}",
                        )
                        proj["desc"] = text.strip()
                        put(data, "projects", projects)
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"调用失败：{exc}")
            if c2.button("🗑 删除项目", key=f"pj_del_{pid}", use_container_width=True):
                put(data, "projects", [p for p in projects if p.get("id") != pid])
                st.rerun()

        with st.expander("➕ 新增项目经历", expanded=not projects):
            with st.form("pj_new", clear_on_submit=True):
                name = st.text_input("项目名称 *", placeholder="如：牧原 ESG 熵权法-TOPSIS 量化研究")
                stack = st.text_input("方法 / 工具标签（逗号分隔）", placeholder="Python,熵权法,TOPSIS,Excel")
                desc = st.text_area("核心描述", height=100, placeholder="做了什么、怎么做、你的角色")
                result = st.text_area("量化成果", height=70, placeholder="如：覆盖 5 年 × 30 家样本，输出 3 套权重方案对比")
                if st.form_submit_button("保存项目", use_container_width=True):
                    if not name.strip():
                        st.error("项目名称不能为空")
                    else:
                        projects.insert(0, {
                            "id": store.new_id("pj"), "name": name.strip(),
                            "stack": [t.strip() for t in stack.replace("，", ",").split(",") if t.strip()],
                            "desc": desc.strip(), "result": result.strip(),
                        })
                        put(data, "projects", projects)
                        st.rerun()


# ==========================================================================
# 五、Offer 管理：总包口径统一，多维对比
# ==========================================================================


def offer_total(item: dict) -> float:
    """总包（万）= 月薪(k) × 发薪月数 ÷ 10。口径统一才比得动。"""
    return num(item.get("base")) * num(item.get("months"), 12) / 10


def offer_score(item: dict) -> float:
    """综合评分：薪资 40% + 发展空间 35% + 文化 25%。权重写死，避免每次算出来不一样。"""
    return round(num(item.get("pay")) * 0.4 + num(item.get("growth")) * 0.35 + num(item.get("culture")) * 0.25, 1)


def render_offers_page(data: dict) -> None:
    ensure_css()
    page_head("💼", "Offer 管理", "月薪不是总包。这里统一按「月薪 × 发薪月数」算年包，再看发展、文化、薪资满意度。")
    offers = blob(data, "offers", [])
    if offers:
        statrow([
            ("Offer 数", f"{len(offers)}"),
            ("最高年包", f"{max(offer_total(o) for o in offers):.1f} 万"),
            ("平均年包", f"{sum(offer_total(o) for o in offers) / len(offers):.1f} 万"),
            ("最高综合分", f"{max(offer_score(o) for o in offers):.1f}"),
        ])
    else:
        empty("还没有 Offer。可以先拿「意向 offer / 往年同岗薪资」练手填一份，对比功能立刻能用。")

    for item in offers:
        oid = item.get("id", "")
        left, right = st.columns([3, 1])
        with left:
            st.markdown(
                f'<div class="ws-card"><h5>{esc(item.get("company"))} · {esc(item.get("title"))}</h5>'
                f'<div class="ws-meta">{esc(item.get("city"))}　·　月薪 {num(item.get("base")):g}k × '
                f'{num(item.get("months"), 12):g} 个月　·　年包 <b>{offer_total(item):.1f} 万</b></div>'
                f'<div class="ws-meta">发展 {num(item.get("growth")):g}　文化 {num(item.get("culture")):g}'
                f'　薪资满意 {num(item.get("pay")):g}</div>'
                + (f'<div class="ws-meta" style="margin-top:.3rem">🎁 {esc(item.get("benefits"))}</div>' if item.get("benefits") else "")
                + (f'<div class="ws-meta">📝 {esc(item.get("note"))}</div>' if item.get("note") else "")
                + "</div>",
                unsafe_allow_html=True,
            )
        with right:
            st.markdown(f'<div class="ws-score">{offer_score(item):.1f}</div>', unsafe_allow_html=True)
            st.caption("综合评分 / 10")
        with st.expander("✏️ 编辑 / 删除", expanded=False):
            with st.form(f"of_edit_{oid}"):
                c1, c2, c3 = st.columns(3)
                company = c1.text_input("公司", value=item.get("company", ""))
                title = c2.text_input("岗位", value=item.get("title", ""))
                city = c3.text_input("城市", value=item.get("city", ""))
                c4, c5, c6 = st.columns(3)
                base = c4.number_input("月薪（k）", 0.0, 200.0, num(item.get("base")), step=0.5)
                months = c5.number_input("发薪月数", 1.0, 24.0, num(item.get("months"), 12.0), step=0.5)
                benefits = c6.text_input("福利", value=item.get("benefits", ""))
                c7, c8, c9 = st.columns(3)
                growth = c7.slider("发展空间", 0, 10, int(num(item.get("growth"), 7)))
                culture = c8.slider("文化匹配", 0, 10, int(num(item.get("culture"), 7)))
                pay = c9.slider("薪资满意度", 0, 10, int(num(item.get("pay"), 7)))
                note = st.text_area("备注", value=item.get("note", ""), height=70)
                b1, b2 = st.columns(2)
                saved = b1.form_submit_button("💾 保存", use_container_width=True)
                deleted = b2.form_submit_button("🗑 删除", use_container_width=True)
                if saved:
                    item.update({"company": company.strip(), "title": title.strip(), "city": city.strip(),
                                 "base": base, "months": months, "benefits": benefits.strip(),
                                 "growth": growth, "culture": culture, "pay": pay, "note": note.strip()})
                    put(data, "offers", offers)
                    st.rerun()
                if deleted:
                    put(data, "offers", [o for o in offers if o.get("id") != oid])
                    st.rerun()

    if len(offers) >= 2:
        st.markdown("#### 📊 多维对比")
        names = [f'{o.get("company") or "未命名"}'[:8] for o in offers]
        st.markdown(svg_group_bars(
            names,
            [("年包(万)", [round(offer_total(o), 1) for o in offers], "var(--acc)"),
             ("月薪(k)", [num(o.get("base")) for o in offers], "var(--acc2)")],
            unit="",
        ), unsafe_allow_html=True)
        st.markdown(svg_group_bars(
            names,
            [("发展空间", [num(o.get("growth")) for o in offers], "var(--acc)"),
             ("文化匹配", [num(o.get("culture")) for o in offers], "var(--acc2)"),
             ("综合评分", [offer_score(o) for o in offers], "#8a7ec8")],
        ), unsafe_allow_html=True)
        st.caption("不同指标量纲不一样，上图看绝对值，下图看 0-10 的主观评分，别跨图比高低。")

    if offers:
        st.download_button(
            "⬇️ 导出 Offer 对比 CSV",
            data=to_csv(
                ["公司", "岗位", "城市", "月薪(k)", "发薪月数", "年包(万)", "福利",
                 "发展空间", "文化匹配", "薪资满意度", "综合评分", "备注"],
                [[o.get("company"), o.get("title"), o.get("city"), o.get("base"), o.get("months"),
                  round(offer_total(o), 2), o.get("benefits"), o.get("growth"), o.get("culture"),
                  o.get("pay"), offer_score(o), o.get("note")] for o in offers],
            ),
            file_name=f"offers_{today().isoformat()}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with st.expander("➕ 新增 Offer", expanded=not offers):
        with st.form("of_new", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            company = c1.text_input("公司 *")
            title = c2.text_input("岗位 *")
            city = c3.text_input("城市")
            c4, c5, c6 = st.columns(3)
            base = c4.number_input("月薪（k）", 0.0, 200.0, 10.0, step=0.5)
            months = c5.number_input("发薪月数", 1.0, 24.0, 13.0, step=0.5)
            benefits = c6.text_input("福利", placeholder="五险一金基数、补贴、宿舍、年假")
            c7, c8, c9 = st.columns(3)
            growth = c7.slider("发展空间", 0, 10, 7, key="of_new_g")
            culture = c8.slider("文化匹配", 0, 10, 7, key="of_new_c")
            pay = c9.slider("薪资满意度", 0, 10, 7, key="of_new_p")
            note = st.text_area("备注", height=70, placeholder="加班情况、汇报对象、是否有服务期/违约金")
            if st.form_submit_button("保存 Offer", use_container_width=True):
                if not company.strip() or not title.strip():
                    st.error("公司和岗位都要填")
                else:
                    offers.insert(0, {
                        "id": store.new_id("of"), "company": company.strip(), "title": title.strip(),
                        "city": city.strip(), "base": base, "months": months, "benefits": benefits.strip(),
                        "growth": growth, "culture": culture, "pay": pay, "note": note.strip(),
                        "created": store.now_text(),
                    })
                    put(data, "offers", offers)
                    st.rerun()


# ==========================================================================
# 六、心态日记：求职是消耗战，状态也是数据
# ==========================================================================

MOOD_COLORS = {1: "#c25b5b", 2: "#d9863c", 3: "#c9b458", 4: "#7fa86b", 5: "#4c9a6c"}
MOOD_WORDS = {1: "很低落", 2: "偏低", 3: "一般", 4: "不错", 5: "很好"}


def render_mood_page(data: dict) -> None:
    ensure_css()
    page_head("📝", "心态日记", "每天 30 秒，记心情和能量。连续低谷会在这里被标出来，不用硬扛。")
    moods = blob(data, "moods", [])
    by_date = {m.get("date"): m for m in moods}
    statrow([
        ("记录天数", f"{len(moods)}"),
        ("平均心情", f"{sum(num(m.get('mood')) for m in moods) / len(moods):.1f}" if moods else "—"),
        ("平均能量", f"{sum(num(m.get('energy')) for m in moods) / len(moods):.1f}" if moods else "—"),
        ("低谷天数", f"{sum(1 for m in moods if num(m.get('mood')) <= 2)}"),
    ])

    left, right = st.columns([1, 1.35])
    with left:
        st.markdown("#### ✍️ 今天记一笔")
        picked = st.date_input("日期", value=today(), key="md_date")
        existing = by_date.get(picked.isoformat(), {})
        with st.form("md_form"):
            mood = st.slider("心情（1 很低落 · 5 很好）", 1, 5, int(num(existing.get("mood"), 3)))
            energy = st.slider("能量（1 没电 · 5 满格）", 1, 5, int(num(existing.get("energy"), 3)))
            note = st.text_area("今天发生了什么（可空）", value=existing.get("note", ""), height=90,
                                placeholder="被哪家拒了 / 哪场面试聊得顺 / 什么都没做但很焦虑")
            if st.form_submit_button("保存这一天", use_container_width=True):
                record = {"date": picked.isoformat(), "mood": mood, "energy": energy, "note": note.strip()}
                by_date[picked.isoformat()] = record
                put(data, "moods", sorted(by_date.values(), key=lambda m: m.get("date", "")))
                st.success("已保存，同一天再保存会覆盖")
                st.rerun()

        recent = sorted(moods, key=lambda m: m.get("date", ""), reverse=True)[:5]
        if recent:
            st.markdown("#### 🕘 最近几条")
            for m in recent:
                st.markdown(
                    f'<div class="ws-card"><div class="ws-meta">{esc(m.get("date"))}　'
                    f'心情 {num(m.get("mood")):g}（{MOOD_WORDS.get(int(num(m.get("mood"))), "")}）'
                    f'　能量 {num(m.get("energy")):g}</div>'
                    + (f'<div class="ws-meta">{esc(m.get("note"))}</div>' if m.get("note") else "")
                    + "</div>",
                    unsafe_allow_html=True,
                )

    with right:
        ordered = sorted(moods, key=lambda m: m.get("date", ""))[-30:]
        st.markdown("#### 📈 心境曲线")
        if len(ordered) >= 2:
            st.markdown(svg_line(
                [("心情", [num(m.get("mood")) for m in ordered], "var(--acc)"),
                 ("能量", [num(m.get("energy")) for m in ordered], "var(--acc2)")],
                [str(m.get("date", ""))[5:] for m in ordered], maximum=5,
            ), unsafe_allow_html=True)
        else:
            empty("至少记两天才画得出曲线")
        st.markdown("#### 🍩 心情分布")
        counts = {k: sum(1 for m in moods if int(num(m.get("mood"))) == k) for k in range(1, 6)}
        st.markdown(svg_donut(
            [(f"{k} 分·{MOOD_WORDS[k]}", counts[k], MOOD_COLORS[k]) for k in range(5, 0, -1)],
            center=f"{len(moods)} 天",
        ), unsafe_allow_html=True)

    # 低谷预警：两条独立触发条件，任一命中就提示。只看最近，不拿三个月前的状态吓人。
    if moods:
        ordered_all = sorted(moods, key=lambda m: m.get("date", ""))
        last3 = ordered_all[-3:]
        low_ratio = sum(1 for m in ordered_all[-14:] if num(m.get("mood")) <= 2) / max(1, len(ordered_all[-14:]))
        avg3 = sum(num(m.get("mood")) for m in last3) / len(last3)
        if (len(last3) >= 3 and avg3 <= 2.3) or low_ratio >= 0.4:
            st.markdown(
                '<div class="ws-warn"><b>⚠️ 低谷预警</b><br>'
                f'最近 {len(last3)} 天平均心情 {avg3:.1f} 分，近两周低谷占比 {low_ratio * 100:.0f}%。<br>'
                '可以先做这三件小事：① 把今天的目标砍到只剩一件能完成的事；'
                '② 停一天刷招聘软件，信息刷新不等于机会变多；'
                '③ 把「被拒」和「我不行」分开写——前者是事实，后者是解释。<br>'
                '如果持续两周以上睡不好、吃不下，请找学校心理中心或专业咨询，这不是矫情。</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown('<div class="ws-ok">最近状态在可接受区间，继续保持记录就好。</div>', unsafe_allow_html=True)


# ==========================================================================
# 七、求职能力分析：自己 vs 同龄均值，短板要落到建议
# ==========================================================================

DEFAULT_SKILLS = [
    {"name": "财务核算", "mine": 7, "peer": 6},
    {"name": "审计与内控", "mine": 6, "peer": 6},
    {"name": "税务基础", "mine": 5, "peer": 6},
    {"name": "Excel与数据", "mine": 7, "peer": 6},
    {"name": "系统与工具", "mine": 5, "peer": 5},
    {"name": "表达与协作", "mine": 6, "peer": 6},
]

# 短板建议按关键词命中，命中不了就用通用建议。建议必须是可以动手做的，不写"加强学习"。
SKILL_ADVICE = [
    (("核算", "会计"), "找一家制造业的真实账套（或课程案例）独立做完「凭证→结账→报表」一轮，把卡壳的环节记下来。"),
    (("审计", "内控"), "把审计流程拆成「风险评估→控制测试→实质性程序」三张卡片，各配一个你做过的例子，面试直接能讲。"),
    (("税",), "主攻增值税和企业所得税两个高频考点，各整理 5 个常见调整项，比泛泛看税法教材有效。"),
    (("excel", "数据", "函数"), "练熟透视表、XLOOKUP、SUMIFS 三件套，再补一个 Power Query 清洗案例，简历上能写成具体成果。"),
    (("系统", "sap", "用友", "金蝶", "工具"), "没有真实系统经验就换口径：写「参与过系统上线的 UAT / 数据核对」，别写「精通 SAP」。"),
    (("英语", "口语"), "准备 60 秒英文自我介绍 + 3 个专业术语的英文解释，财务岗常考的就这几句。"),
    (("表达", "协作", "沟通"), "把每段经历压成 STAR 四句话，录音回听一遍，删掉所有「然后」「就是」。"),
]
GENERIC_ADVICE = "把这项拆成一个两周内能交付的小成果（一张表、一段分析、一次讲解），比看课更能提升分数。"


def weekly_trend(jobs: list[dict], weeks: int = 5) -> tuple[list[str], list[int], list[int]]:
    """近 N 周的投递量与有回复量。

    口径说清楚：按岗位记录的创建周归集；「有回复」指阶段已推进到简历筛及以后。
    我们没有每周的独立回复时间戳，所以这是近似值，不假装精确。
    """
    monday = today() - timedelta(days=today().weekday())
    starts = [monday - timedelta(weeks=w) for w in range(weeks - 1, -1, -1)]
    labels = [s.strftime("%m/%d") for s in starts]
    sent = [0] * weeks
    replied = [0] * weeks
    for job in jobs:
        created = str(job.get("created") or job.get("meta", {}).get("time") or "")[:10]
        try:
            day = datetime.strptime(created, "%Y-%m-%d").date()
        except ValueError:
            continue
        for i, start in enumerate(starts):
            if start <= day < start + timedelta(days=7):
                sent[i] += 1
                stage = str(job.get("meta", {}).get("stage") or "")
                if stage and stage != "未投递":
                    replied[i] += 1
                break
    return labels, sent, replied


def render_ability_page(data: dict) -> None:
    ensure_css()
    page_head("📊", "求职能力分析", "分数是你自己打的，同龄均值也可以自己调。重点不是准不准，是短板有没有对应动作。")
    skills = blob(data, "skills", DEFAULT_SKILLS)
    jobs = data.get("jobs", [])

    left, right = st.columns([1.05, 1])
    with left:
        st.markdown("#### 🕸 能力雷达")
        st.markdown(svg_radar(skills), unsafe_allow_html=True)
    with right:
        st.markdown("#### 📏 技能掌握度")
        st.markdown(svg_bars([(s.get("name", ""), num(s.get("mine"))) for s in skills], maximum=10), unsafe_allow_html=True)
        st.caption("0-10 自评。建议每两周重打一次分，分数变化比绝对值更有参考价值。")

    st.markdown("#### 📈 近 5 周投递趋势")
    labels, sent, replied = weekly_trend(jobs)
    st.markdown(svg_line(
        [("投递/记录", sent, "var(--acc)"), ("有推进", replied, "var(--acc2)")], labels,
    ), unsafe_allow_html=True)
    st.caption("口径：按岗位记录的创建周归集；「有推进」= 阶段已不是「未投递」。没有每周独立的回复时间戳，所以是近似值。")

    st.markdown("#### 🩹 能力短板与对应动作")
    gaps = []
    for s in skills:
        mine, peer = num(s.get("mine")), num(s.get("peer"))
        if mine < peer or mine < 6:
            advice = GENERIC_ADVICE
            for keys, text in SKILL_ADVICE:
                if any(k in str(s.get("name", "")).lower() for k in keys):
                    advice = text
                    break
            gaps.append((s.get("name", ""), mine, peer, advice))
    if not gaps:
        st.markdown('<div class="ws-ok">各项自评都不低于同龄均值，且都在 6 分以上。可以把目标从「补短板」换成「把一项拉到 9 分当招牌」。</div>', unsafe_allow_html=True)
    for name, mine, peer, advice in sorted(gaps, key=lambda g: g[1] - g[2]):
        st.markdown(
            f'<div class="ws-card"><h5>{esc(name)}　<span class="ws-tag">我 {mine:g} / 同龄 {peer:g}</span>'
            f'<span class="ws-tag">差 {mine - peer:+.0f}</span></h5>'
            f'<div class="ws-meta">{esc(advice)}</div></div>',
            unsafe_allow_html=True,
        )

    with st.expander("✏️ 调整能力项与分数", expanded=False):
        st.caption("每行一个能力项。改完点保存；新增空行即可加项，名称留空的行会被忽略。")
        rows = list(skills) + [{"name": "", "mine": 5, "peer": 5}]
        edited = []
        for i, s in enumerate(rows):
            c1, c2, c3 = st.columns([2, 1, 1])
            name = c1.text_input("能力项", value=s.get("name", ""), key=f"sk_n_{i}", label_visibility="collapsed",
                                 placeholder="能力项名称")
            mine = c2.slider("我的", 0, 10, int(num(s.get("mine"), 5)), key=f"sk_m_{i}")
            peer = c3.slider("同龄", 0, 10, int(num(s.get("peer"), 5)), key=f"sk_p_{i}")
            edited.append({"name": name.strip(), "mine": mine, "peer": peer})
        if st.button("💾 保存能力评分", use_container_width=True):
            clean = [s for s in edited if s["name"]]
            if len(clean) < 3:
                st.error("至少保留 3 个能力项，否则雷达图画不出来")
            else:
                put(data, "skills", clean)
                st.rerun()


# ==========================================================================
# 八、人脉内推 / 学习计划 / 资料库
# ==========================================================================

CONTACT_STAGES = ["未联系", "已请求", "已内推", "已反馈", "已转化"]
RESOURCE_CATS = ["面经", "笔试题", "公司分析", "岗位要求", "行业报告", "自我介绍", "其他"]


def render_network_page(data: dict) -> None:
    ensure_css()
    page_head("📚", "人脉 · 学习 · 资料库", "内推靠记录不靠记忆；学习计划要能看到进度；资料要能按分类找出来。")
    tab_c, tab_l, tab_r = st.tabs(["🤝 人脉与内推", "🎯 学习提升计划", "🗃 资料库"])

    with tab_c:
        contacts = blob(data, "contacts", [])
        statrow([
            ("联系人", f"{len(contacts)}"),
            ("已请求内推", f"{sum(1 for c in contacts if CONTACT_STAGES.index(c.get('status', '未联系')) >= 1)}"),
            ("已内推", f"{sum(1 for c in contacts if CONTACT_STAGES.index(c.get('status', '未联系')) >= 2)}"),
            ("已转化", f"{sum(1 for c in contacts if c.get('status') == '已转化')}"),
        ])
        st.markdown("**内推转化漏斗**")
        st.markdown(funnel_html([
            (stage, sum(1 for c in contacts if CONTACT_STAGES.index(c.get("status", "未联系")) >= i))
            for i, stage in enumerate(CONTACT_STAGES)
        ]), unsafe_allow_html=True)
        st.caption("每一档统计的是「至少走到这一步」的人数，所以是逐级递减的漏斗，不是互斥分类。")

        if not contacts:
            empty("还没有联系人。校友、实习带教、宣讲会加的 HR 都可以记进来。")
        for c in contacts:
            cid = c.get("id", "")
            st.markdown(
                f'<div class="ws-card"><h5>{esc(c.get("name"))}　<span class="ws-tag">{esc(c.get("status"))}</span></h5>'
                f'<div class="ws-meta">{esc(c.get("company"))}　·　{esc(c.get("title"))}　·　{esc(c.get("channel"))}'
                f'　·　请求于 {esc(c.get("asked"))}</div>'
                + (f'<div class="ws-meta">💬 {esc(c.get("feedback"))}</div>' if c.get("feedback") else "")
                + "</div>",
                unsafe_allow_html=True,
            )
            with st.expander("✏️ 更新状态 / 反馈", expanded=False):
                c1, c2 = st.columns(2)
                status = c1.selectbox("状态", CONTACT_STAGES,
                                      index=CONTACT_STAGES.index(c.get("status", "未联系")), key=f"ct_st_{cid}")
                feedback = c2.text_input("反馈", value=c.get("feedback", ""), key=f"ct_fb_{cid}")
                b1, b2 = st.columns(2)
                if b1.button("💾 保存", key=f"ct_save_{cid}", use_container_width=True):
                    c["status"], c["feedback"] = status, feedback.strip()
                    put(data, "contacts", contacts)
                    st.rerun()
                if b2.button("🗑 删除", key=f"ct_del_{cid}", use_container_width=True):
                    put(data, "contacts", [x for x in contacts if x.get("id") != cid])
                    st.rerun()

        with st.expander("➕ 新增联系人", expanded=not contacts):
            with st.form("ct_new", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                name = c1.text_input("姓名 / 称呼 *")
                company = c2.text_input("公司")
                title = c3.text_input("岗位 / 关系")
                c4, c5 = st.columns(2)
                channel = c4.selectbox("渠道", ["校友", "实习同事", "宣讲会", "领英/脉脉", "老师介绍", "猎头", "其他"])
                asked = c5.text_input("请求日期", value=today().isoformat())
                feedback = st.text_input("备注 / 反馈")
                if st.form_submit_button("保存联系人", use_container_width=True):
                    if not name.strip():
                        st.error("姓名不能为空")
                    else:
                        contacts.insert(0, {
                            "id": store.new_id("ct"), "name": name.strip(), "company": company.strip(),
                            "title": title.strip(), "channel": channel, "asked": asked.strip(),
                            "status": "未联系", "feedback": feedback.strip(),
                        })
                        put(data, "contacts", contacts)
                        st.rerun()

    with tab_l:
        plans = blob(data, "learning", [])
        if plans:
            avg = sum(num(p.get("progress")) for p in plans) / len(plans)
            statrow([("学习任务", f"{len(plans)}"),
                     ("已完成", f"{sum(1 for p in plans if num(p.get('progress')) >= 100)}"),
                     ("平均进度", f"{avg:.0f}%"),
                     ("已逾期", f"{sum(1 for p in plans if (days_left(p.get('due')) or 0) < 0 and num(p.get('progress')) < 100)}")])
        else:
            empty("还没有学习计划。可以直接把「能力分析」页里的短板动作搬过来。")
        for p in plans:
            pid = p.get("id", "")
            left = days_left(p.get("due"))
            due_text = "未设截止" if left is None else (f"已逾期 {-left} 天" if left < 0 else f"剩 {left} 天")
            st.markdown(f"**{p.get('topic')}**　`{p.get('priority')}`　·　{p.get('why') or '未填原因'}")
            st.markdown(svg_progress(num(p.get("progress")) / 100, f"截止 {p.get('due') or '—'}　·　{due_text}"),
                        unsafe_allow_html=True)
            c1, c2, c3 = st.columns([2, 1, 1])
            progress = c1.slider("进度", 0, 100, int(num(p.get("progress"))), key=f"ln_pg_{pid}")
            if c2.button("💾 更新", key=f"ln_save_{pid}", use_container_width=True):
                p["progress"] = progress
                put(data, "learning", plans)
                st.rerun()
            if c3.button("🗑 删除", key=f"ln_del_{pid}", use_container_width=True):
                put(data, "learning", [x for x in plans if x.get("id") != pid])
                st.rerun()
            st.divider()

        with st.expander("➕ 新增学习任务", expanded=not plans):
            with st.form("ln_new", clear_on_submit=True):
                topic = st.text_input("任务 *", placeholder="如：独立做完一套制造业账套的月末结账")
                why = st.text_input("对应短板 / 为什么学", placeholder="如：核算自评 5 分，低于同龄均值")
                c1, c2, c3 = st.columns(3)
                priority = c1.selectbox("优先级", ["高", "中", "低"])
                due = c2.text_input("截止日期", value=(today() + timedelta(days=14)).isoformat())
                progress = c3.slider("初始进度", 0, 100, 0, key="ln_new_pg")
                if st.form_submit_button("保存学习任务", use_container_width=True):
                    if not topic.strip():
                        st.error("任务内容不能为空")
                    else:
                        plans.insert(0, {"id": store.new_id("ln"), "topic": topic.strip(), "why": why.strip(),
                                         "priority": priority, "due": due.strip(), "progress": progress})
                        put(data, "learning", plans)
                        st.rerun()

    with tab_r:
        resources = blob(data, "resources", [])
        c1, c2 = st.columns([1, 1.4])
        cat = c1.selectbox("分类筛选", ["全部"] + RESOURCE_CATS)
        keyword = c2.text_input("关键词", placeholder="搜标题或正文")
        shown = [r for r in resources
                 if (cat == "全部" or r.get("cat") == cat)
                 and (not keyword.strip() or keyword.strip() in (r.get("title", "") + r.get("content", "")))]
        st.caption(f"共 {len(resources)} 条，当前显示 {len(shown)} 条")
        if not shown:
            empty("这个分类下还没有资料")
        for r in shown:
            with st.expander(f"📄 {r.get('title')}　·　{r.get('cat')}", expanded=False):
                if r.get("link"):
                    st.markdown(f"🔗 [{r.get('link')}]({r.get('link')})")
                st.write(r.get("content") or "（只有标题，正文还没补）")
                if st.button("🗑 删除这条资料", key=f"rc_del_{r.get('id')}"):
                    put(data, "resources", [x for x in resources if x.get("id") != r.get("id")])
                    st.rerun()
        with st.expander("➕ 新增资料", expanded=not resources):
            with st.form("rc_new", clear_on_submit=True):
                title = st.text_input("标题 *")
                c1, c2 = st.columns(2)
                rcat = c1.selectbox("分类", RESOURCE_CATS)
                link = c2.text_input("链接（可空）")
                content = st.text_area("正文 / 要点", height=140)
                if st.form_submit_button("保存资料", use_container_width=True):
                    if not title.strip():
                        st.error("标题不能为空")
                    else:
                        resources.insert(0, {"id": store.new_id("rc"), "title": title.strip(), "cat": rcat,
                                             "link": link.strip(), "content": content.strip()})
                        put(data, "resources", resources)
                        st.rerun()


# ==========================================================================
# 九、秋招节奏 & 时间线规划
# ==========================================================================


def default_phases() -> list[dict]:
    """按当前月份推归属年份：7 月之后算当年这一届，之前算上一届启动的周期。"""
    year = today().year if today().month >= 7 else today().year - 1
    return [
        {"name": "提前批", "start": f"{year}-07-01", "end": f"{year}-08-31", "target": 15},
        {"name": "正式批", "start": f"{year}-09-01", "end": f"{year}-11-30", "target": 40},
        {"name": "补录 / 春招", "start": f"{year}-12-01", "end": f"{year + 1}-03-31", "target": 30},
    ]


def _in_range(day_text: str, start: str, end: str) -> bool:
    try:
        day = datetime.strptime(day_text[:10], "%Y-%m-%d").date()
        return date.fromisoformat(start) <= day <= date.fromisoformat(end)
    except (ValueError, TypeError):
        return False


def render_season_page(data: dict) -> None:
    ensure_css()
    page_head("📅", "秋招节奏 & 时间线", "阶段进度看「投了多少」，节点时间线看「接下来不能错过什么」。")
    phases = blob(data, "season_phases", default_phases())
    events = blob(data, "season_events", [])
    jobs = data.get("jobs", [])

    st.markdown("#### 🚩 阶段进度")
    for i, phase in enumerate(phases):
        done = sum(1 for j in jobs if _in_range(str(j.get("created") or j.get("meta", {}).get("time") or ""),
                                               phase.get("start", ""), phase.get("end", "")))
        target = max(1, int(num(phase.get("target"), 1)))
        st.markdown(
            svg_progress(done / target,
                         f'{phase.get("name")}　{phase.get("start")} ~ {phase.get("end")}　·　'
                         f'已记录 {done} / 目标 {target}'),
            unsafe_allow_html=True,
        )
    with st.expander("✏️ 调整阶段名称、起止日期与目标投递数", expanded=False):
        for i, phase in enumerate(phases):
            c1, c2, c3, c4 = st.columns([1.2, 1, 1, 1])
            name = c1.text_input("阶段名", value=phase.get("name", ""), key=f"ph_n_{i}")
            start = c2.text_input("开始", value=phase.get("start", ""), key=f"ph_s_{i}")
            end = c3.text_input("结束", value=phase.get("end", ""), key=f"ph_e_{i}")
            target_in = c4.number_input("目标投递数", 1, 500, max(1, int(num(phase.get("target"), 1))), key=f"ph_t_{i}")
            phase.update({"name": name.strip() or phase.get("name"), "start": start.strip(),
                          "end": end.strip(), "target": int(target_in)})
        if st.button("💾 保存阶段设置", use_container_width=True):
            put(data, "season_phases", phases)
            st.success("已保存")
            st.rerun()
    st.caption("「已记录」按岗位分析的创建日期自动归集，不用手填；改目标投递数才会改变进度条。")

    st.divider()
    left, right = st.columns([1.3, 1])
    with left:
        st.markdown("#### 🧵 关键节点时间线")
        rows = sorted(events, key=lambda e: e.get("date", ""))
        if not rows:
            empty("还没有节点。把投递截止、笔试、面试日期加进来，右侧会自动算倒计时。")
        body = []
        for e in rows:
            left_days = days_left(e.get("date"))
            past = left_days is not None and left_days < 0
            body.append(
                f'<div class="ws-tl-item{" past" if past else ""}">'
                f'<div class="ws-tl-date">{esc(e.get("date"))}　·　{esc(e.get("kind"))}</div>'
                f'<div><b>{esc(e.get("title"))}</b></div>'
                + (f'<div class="ws-meta">{esc(e.get("note"))}</div>' if e.get("note") else "")
                + "</div>"
            )
        if body:
            st.markdown(f'<div class="ws-tl">{"".join(body)}</div>', unsafe_allow_html=True)
        for e in rows:
            if st.button(f"🗑 删除「{e.get('title')}」", key=f"ev_del_{e.get('id')}"):
                put(data, "season_events", [x for x in events if x.get("id") != e.get("id")])
                st.rerun()

    with right:
        st.markdown("#### ⏳ 即将到来")
        upcoming = [(days_left(e.get("date")), e) for e in events]
        upcoming = [(d, e) for d, e in upcoming if d is not None and d >= 0]
        if not upcoming:
            empty("未来没有已登记的节点")
        for d, e in sorted(upcoming, key=lambda x: x[0])[:8]:
            style = "ws-warn" if d <= 7 else "ws-note"
            st.markdown(
                f'<div class="{style}"><b>{esc(e.get("title"))}</b><br>'
                f'{esc(e.get("date"))}　·　{esc(e.get("kind"))}　·　'
                f'{"<b>就是今天</b>" if d == 0 else f"还剩 <b>{d}</b> 天"}</div>',
                unsafe_allow_html=True,
            )

    with st.expander("➕ 新增关键节点", expanded=not events):
        with st.form("ev_new", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            title = c1.text_input("事项 *", placeholder="如：中建电商投递截止")
            day = c2.text_input("日期", value=today().isoformat())
            kind = c3.selectbox("类型", ["投递截止", "笔试", "面试", "测评", "Offer 截止", "其他"])
            note = st.text_input("备注", placeholder="地点 / 形式 / 需要带什么")
            if st.form_submit_button("保存节点", use_container_width=True):
                if not title.strip() or days_left(day) is None:
                    st.error("事项必填，日期要是 YYYY-MM-DD 格式")
                else:
                    events.append({"id": store.new_id("ev"), "title": title.strip(), "date": day.strip(),
                                   "kind": kind, "note": note.strip()})
                    put(data, "season_events", events)
                    st.rerun()


# ==========================================================================
# 十、AI 求职助手：六个场景一个入口
# ==========================================================================

AI_MODES = [
    ("jd", "🔍 JD 分析", "把 JD 贴进来，我拆「硬性门槛 / 实际干什么 / 匹配点 / 风险点」。",
     "你是求职顾问，只输出结构化中文分析，分四节：硬性门槛、实际工作内容、候选人的匹配点、风险与待确认项。不要客套话。",
     "贴一段 JD，或写「分析我最新保存的那个岗位」"),
    ("resume", "📄 简历优化", "贴一段简历原文，改成有动词、有结果的表述。",
     "你是简历顾问。逐条给出「原文 → 改后」，并说明改动理由。不得编造数据和经历，缺数字就标「可补充」。",
     "贴简历里的一段经历"),
    ("mock", "🎤 模拟面试", "给我岗位，我出 5 道题并给追问，你答完我再点评。",
     "你是面试官。先出 5 道中文面试题（含 2 道专业题、2 道行为题、1 道压力题），每题附一个可能的追问。不要立刻给答案。",
     "如：核算会计实习生，制造业，我是会计专硕"),
    ("review", "🪞 面试复盘", "把被问到的题和你怎么答的贴进来，我挑问题。",
     "你是面试复盘教练。指出回答里的三个具体问题（结构、证据、表达），并给出改进版示范。语气直接但不否定人。",
     "贴「问题 + 你当时的回答」"),
    ("offer", "⚖️ Offer 对比", "把几个 offer 的条件贴进来，我按长期收益和风险帮你理一遍。",
     "你是职业顾问。从总包口径、成长空间、行业风险、跳槽溢价四个维度对比，最后给出还需要向 HR 确认的问题清单。不做绝对化推荐。",
     "贴两个 offer 的薪资、城市、岗位、福利"),
    ("salary", "💰 薪资谈判", "告诉我对方开价和你的底线，我给你话术。",
     "你是薪酬谈判顾问。给出：合理报价区间、三段可直接说的话术、以及对方压价时的应对。不得承诺结果。",
     "如：对方给 8k×12，成都，我期望 10k"),
]

AI_SUGGESTIONS = [
    "我是会计专硕，没有事务所实习，怎么把课程项目讲得值钱？",
    "帮我准备一段 60 秒的自我介绍，突出量化研究经历",
    "面试官问「你为什么不去事务所」，怎么答不扣分？",
    "双休和平台哪个更重要？帮我列一个判断清单",
]


def render_ai_page(data: dict, ai=None, context: str = "") -> None:
    ensure_css()
    page_head("🤖", "AI 求职助手", "六个场景共用一个入口。所有输出都是草稿，对外用之前自己先改一遍。")
    if ai is None:
        st.warning("模型还没配置好：请先到「⚙️ 求职设置」里填 API Key 和接口地址，本页功能就能用了。")
    history = blob(data, "ai_chats", [])

    tabs = st.tabs([title for _, title, _, _, _ in AI_MODES])
    for tab, (key, title, desc, system, placeholder) in zip(tabs, AI_MODES):
        name = title  # 标题里已带图标，对话记录里直接用它当场景名
        with tab:
            if key == "jd":
                # JD 分析只在「岗位分析」页做一份完整版；这里再放简版就是两个入口两套结果。
                st.caption(
                    "JD 分析已经升级成完整工作流（提取字段 → 去噪 → 硬条件核查 → 问题预测），"
                    "都只在「🚀 岗位分析」一个页面里。这里不再放重复的简版。"
                )
                if st.button("🚀 进入完整岗位分析", type="primary", use_container_width=True):
                    st.session_state["nav_page"] = "analyze"
                    st.rerun()
                continue
            st.caption(desc)
            suggestions = AI_SUGGESTIONS if key == "mock" else []
            if suggestions:
                st.caption("不知道问什么？点一条直接用：")
                cols = st.columns(2)
                for i, text in enumerate(suggestions):
                    if cols[i % 2].button(text, key=f"ai_sug_{key}_{i}", use_container_width=True):
                        st.session_state[f"ai_in_{key}"] = text
            prompt = st.text_area("输入", value=st.session_state.get(f"ai_in_{key}", ""),
                                  height=130, placeholder=placeholder, key=f"ai_box_{key}")
            if st.button(f"✨ 生成{name}结果", key=f"ai_go_{key}", use_container_width=True, disabled=ai is None):
                if not prompt.strip():
                    st.error("先写点内容再生成")
                else:
                    with st.spinner("模型正在生成…"):
                        try:
                            reply = ai(system, f"我的背景（仅供参考，不要复述）：{context}\n\n我的问题：{prompt.strip()}")
                            history.insert(0, {"ts": store.now_text(), "mode": name,
                                               "prompt": prompt.strip(), "reply": reply})
                            put(data, "ai_chats", history[:60])
                            st.session_state[f"ai_in_{key}"] = ""
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"调用失败：{exc}")

    st.divider()
    st.markdown("#### 🕘 最近对话")
    if not history:
        empty("还没有对话记录")
    for i, chat in enumerate(history[:10]):
        with st.expander(f'{chat.get("ts")}　·　{chat.get("mode")}　·　{str(chat.get("prompt"))[:34]}', expanded=(i == 0)):
            st.markdown("**我问的**")
            st.write(chat.get("prompt"))
            st.markdown("**模型给的草稿**")
            st.write(chat.get("reply"))
    if history:
        st.download_button(
            "⬇️ 导出全部对话（Markdown）",
            data="\n\n---\n\n".join(
                f'## {c.get("ts")} · {c.get("mode")}\n\n**问**\n\n{c.get("prompt")}\n\n**答**\n\n{c.get("reply")}'
                for c in history
            ),
            file_name=f"ai_chats_{today().isoformat()}.md",
            mime="text/markdown",
            use_container_width=True,
        )
        if st.button("🗑 清空对话记录", use_container_width=True):
            put(data, "ai_chats", [])
            st.rerun()


# ==========================================================================
# 十一、社招板块：流程、口径、风险都和校招不一样
# ==========================================================================

SOCIAL_STAGES = ["已收藏", "已投递", "HR电话", "一面", "二面", "终面", "谈薪", "背调", "已入职", "已结束"]
SOCIAL_EXP = ["不限", "应届可投", "1-3年", "3-5年", "5-10年", "10年以上"]
SOCIAL_CHANNELS = ["官网", "BOSS直聘", "前程无忧", "智联", "猎聘", "猎头", "内推", "其他"]
SOCIAL_CHECK = [
    "确认劳动合同主体和实际工作地点，别签成外包或派遣却不知情",
    "问清社保公积金缴纳基数和比例，是按全额还是最低基数",
    "查上一家是否签过竞业限制协议，限制范围、期限、补偿金分别是多少",
    "背调联系人提前打好招呼，避免现单位提前知道你在看机会",
    "谈薪前先确认现薪流水是否需要提供，别在口头阶段就把底线说死",
    "离职证明、离职交接周期、年假折算方式在提离职前确认",
    "试用期时长和试用期工资折扣写进 offer，不接受口头承诺",
]


def render_social_page(data: dict) -> None:
    ensure_css()
    page_head("💼", "社招板块", "社招看的是「来了能不能立刻干活」，流程更短、谈薪空间更大，但背调和竞业是真风险。")
    profile = blob(data, "social_profile", {})
    jobs = blob(data, "social_jobs", [])

    st.markdown(
        '<div class="ws-note"><b>社招和校招的三个关键差别</b><br>'
        '① <b>评价口径</b>：校招看潜力和可塑性，社招看即战力，简历里「独立负责过什么」比「学过什么」重要得多。<br>'
        '② <b>流程</b>：通常没有统一笔试和批次，HR 电话 → 业务面 → 终面 → 谈薪 → 背调，节奏由对方定，可能一周内走完。<br>'
        '③ <b>风险</b>：多了背调、竞业限制、试用期条款三件事，校招基本不会遇到，务必在提离职前查清。</div>',
        unsafe_allow_html=True,
    )

    with st.expander("🧾 我的社招档案（在职状态 / 薪资底线 / 竞业）", expanded=not profile):
        with st.form("sp_form"):
            c1, c2 = st.columns(2)
            status = c1.selectbox("当前状态", ["在职看机会", "已离职", "应届但想投社招岗"],
                                  index=["在职看机会", "已离职", "应届但想投社招岗"].index(
                                      profile.get("status", "应届但想投社招岗"))
                                  if profile.get("status") in ["在职看机会", "已离职", "应届但想投社招岗"] else 2)
            available = c2.text_input("可到岗时间", value=profile.get("available", ""), placeholder="如：随时 / 提离职后 1 个月")
            c3, c4, c5 = st.columns(3)
            current = c3.number_input("当前月薪（k，没有填 0）", 0.0, 200.0, num(profile.get("current")), step=0.5)
            expect = c4.number_input("期望月薪（k）", 0.0, 200.0, num(profile.get("expect"), 10.0), step=0.5)
            floor = c5.number_input("底线月薪（k）", 0.0, 200.0, num(profile.get("floor"), 8.0), step=0.5)
            noncompete = st.text_area("竞业 / 服务期 / 违约金情况", value=profile.get("noncompete", ""), height=70,
                                      placeholder="没签过就写「无」；签过写清限制范围和期限")
            if st.form_submit_button("保存社招档案", use_container_width=True):
                put(data, "social_profile", {"status": status, "available": available.strip(), "current": current,
                                             "expect": expect, "floor": floor, "noncompete": noncompete.strip()})
                st.rerun()

    if jobs:
        statrow([
            ("社招岗位", f"{len(jobs)}"),
            ("已投递", f"{sum(1 for j in jobs if SOCIAL_STAGES.index(j.get('stage', '已收藏')) >= 1)}"),
            ("进入面试", f"{sum(1 for j in jobs if 3 <= SOCIAL_STAGES.index(j.get('stage', '已收藏')) <= 5)}"),
            ("谈薪及以后", f"{sum(1 for j in jobs if SOCIAL_STAGES.index(j.get('stage', '已收藏')) >= 6)}"),
        ])

    c1, c2, c3 = st.columns(3)
    f_stage = c1.selectbox("按阶段筛", ["全部"] + SOCIAL_STAGES)
    f_exp = c2.selectbox("按经验要求筛", ["全部"] + SOCIAL_EXP)
    f_channel = c3.selectbox("按渠道筛", ["全部"] + SOCIAL_CHANNELS)
    shown = [j for j in jobs
             if (f_stage == "全部" or j.get("stage") == f_stage)
             and (f_exp == "全部" or j.get("exp") == f_exp)
             and (f_channel == "全部" or j.get("channel") == f_channel)]
    st.caption(f"当前显示 {len(shown)} / {len(jobs)} 个社招岗位")

    if not shown:
        empty("还没有符合条件的社招岗位，先在下面添加一个")
    for job in shown:
        jid = job.get("id", "")
        idx = SOCIAL_STAGES.index(job.get("stage", "已收藏"))
        st.markdown(
            f'<div class="ws-card"><h5>{esc(job.get("company"))} · {esc(job.get("title"))}'
            f'<span class="ws-tag">{esc(job.get("stage"))}</span></h5>'
            f'<div class="ws-meta">{esc(job.get("city"))}　·　{esc(job.get("salary"))}　·　'
            f'经验 {esc(job.get("exp"))}　·　{esc(job.get("level"))}　·　渠道 {esc(job.get("channel"))}</div>'
            + (f'<div class="ws-meta">📝 {esc(job.get("note"))}</div>' if job.get("note") else "")
            + (f'<div class="ws-meta">🔗 <a href="{esc(job.get("link"))}" target="_blank">查看原帖</a></div>' if job.get("link") else "")
            + "</div>",
            unsafe_allow_html=True,
        )
        c1, c2, c3, c4 = st.columns([1.4, 1, 1, 1])
        new_stage = c1.selectbox("阶段", SOCIAL_STAGES, index=idx, key=f"sj_st_{jid}", label_visibility="collapsed")
        if c2.button("推进到这个阶段", key=f"sj_go_{jid}", use_container_width=True):
            job["stage"] = new_stage
            put(data, "social_jobs", jobs)
            st.rerun()
        note = c3.text_input("备注", value=job.get("note", ""), key=f"sj_note_{jid}", label_visibility="collapsed",
                             placeholder="补一句备注")
        if c4.button("存备注", key=f"sj_nt_{jid}", use_container_width=True):
            job["note"] = note.strip()
            put(data, "social_jobs", jobs)
            st.rerun()
        if st.button("🗑 删除这个岗位", key=f"sj_del_{jid}"):
            put(data, "social_jobs", [j for j in jobs if j.get("id") != jid])
            st.rerun()

    with st.expander("➕ 新增社招岗位", expanded=not jobs):
        with st.form("sj_new", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            company = c1.text_input("公司 *")
            title = c2.text_input("岗位 *")
            city = c3.text_input("城市")
            c4, c5, c6 = st.columns(3)
            salary = c4.text_input("薪资", placeholder="如：10-15k·14薪")
            exp = c5.selectbox("经验要求", SOCIAL_EXP)
            level = c6.text_input("职级", placeholder="如：专员 / 主管 / P5")
            c7, c8 = st.columns(2)
            channel = c7.selectbox("渠道", SOCIAL_CHANNELS)
            link = c8.text_input("原帖链接（可空）")
            note = st.text_area("备注", height=70, placeholder="汇报对象、是否接受应届、是否外包")
            if st.form_submit_button("保存社招岗位", use_container_width=True):
                if not company.strip() or not title.strip():
                    st.error("公司和岗位都要填")
                else:
                    jobs.insert(0, {"id": store.new_id("sj"), "company": company.strip(), "title": title.strip(),
                                    "city": city.strip(), "salary": salary.strip(), "exp": exp,
                                    "level": level.strip(), "channel": channel, "link": link.strip(),
                                    "note": note.strip(), "stage": "已收藏", "created": store.now_text()})
                    put(data, "social_jobs", jobs)
                    st.rerun()

    left, right = st.columns(2)
    with left:
        st.markdown("#### 💰 谈薪报价参考")
        base = num(profile.get("current"))
        expect = num(profile.get("expect"), 10.0)
        floor = num(profile.get("floor"), 8.0)
        if base > 0:
            st.caption(f"按当前月薪 {base:g}k 计算：")
            st.markdown(svg_bars([
                ("平跳（+0%）", round(base, 1)),
                ("常规涨幅（+20%）", round(base * 1.2, 1)),
                ("较好涨幅（+30%）", round(base * 1.3, 1)),
                ("你设的期望", expect),
                ("你设的底线", floor),
            ], unit="k"), unsafe_allow_html=True)
        else:
            st.caption("你还没填当前月薪（应届/无现薪属正常），按期望与底线看：")
            st.markdown(svg_bars([("期望月薪", expect), ("底线月薪", floor),
                                  ("建议首次报价（期望×1.1）", round(expect * 1.1, 1))], unit="k"),
                        unsafe_allow_html=True)
        st.markdown(
            f'<div class="ws-note">建议首次报价 <b>{round((base * 1.25 if base > 0 else expect * 1.1), 1):g}k</b>，'
            f'心理底线 <b>{floor:g}k</b>。首次报价留 10%-20% 让步空间，但底线不要提前说出口。<br>'
            '谈薪时把「月薪」换成「年包 + 缴纳基数 + 试用期折扣」三件事一起谈，只谈月薪容易在最后一步吃亏。</div>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown("#### ✅ 背调与竞业自查清单")
        checked = blob(data, "social_check", [])
        for i, text in enumerate(SOCIAL_CHECK):
            value = st.checkbox(text, value=(i in checked), key=f"sc_{i}")
            if value and i not in checked:
                checked.append(i)
            elif not value and i in checked:
                checked.remove(i)
        put(data, "social_check", checked)
        st.caption(f"已确认 {len(checked)} / {len(SOCIAL_CHECK)} 项。勾选状态会保存在本机。")


# ==========================================================================
# 十二、国际视野：公开 RSS → 每日财经简报
# ==========================================================================

DEFAULT_WATCH = ["会计", "财务", "审计", "风控", "ESG", "就业", "应届生", "制造业"]


def render_news_page(data: dict, ai=None, background: str = "") -> None:
    import news  # 局部导入：这个模块会联网，只在真正打开本页时才加载

    ensure_css()
    page_head("🌍", "国际视野", "读公开新闻源（新浪财经、中新网、华尔街见闻、WSJ），把国内外财经和热点写成「跟我求职有关」的简报。每天点一次，只抓一次。")
    cache = blob(data, "news_cache", {})
    watch = blob(data, "news_watch", DEFAULT_WATCH)
    today_text = today().isoformat()
    fresh = str(cache.get("date") or "") == today_text

    statrow([
        ("今日简报", "已生成" if fresh else "未生成"),
        ("上次更新", (cache.get("generated") or "—")[5:16] if cache.get("generated") else "—"),
        ("抓到条数", f"{len(cache.get('items') or [])}"),
        ("关注词", f"{len([w for w in watch if w.strip()])} 个"),
    ])

    st.markdown(
        '<div class="ws-note"><b>抓取边界</b>：只读公开分发接口（RSS，以及媒体官网前端自己在用的公开数据接口），'
        '每次先查 robots.txt，对方不允许就跳过并写明原因；不登录、不翻页、不抓正文全文、不绕付费墙；'
        '只在点按钮时联网一次，不在后台轮询。</div>',
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns([1.2, 1])
    if c1.button("🔄 抓取今日新闻并生成简报", type="primary", use_container_width=True):
        with st.spinner("正在读公开 RSS…"):
            result = news.fetch_all()
        if not result["items"]:
            st.error("这次没抓到新闻（可能网络不通或源临时不可用）。下面可以手动粘贴新闻。")
            for row in result["report"]:
                st.caption(f'{row["source"]}：{"成功 " + str(row["count"]) + " 条" if row["ok"] else "失败 · " + row["why"]}')
        else:
            items = news.watch_hits(result["items"], watch)
            brief = ""
            if ai:
                with st.spinner("正在写简报…"):
                    try:
                        system, user = news.brief_prompt(items, watch, background)
                        brief = ai(system, user)
                    except Exception as exc:  # noqa: BLE001
                        st.warning(f"新闻抓到了，但模型写简报失败：{exc}。下面仍然按标签列出新闻。")
            else:
                brief = "（未配置模型，只做了抓取和分类。到「⚙️ 求职设置」填好 API Key 后会生成简报。）"
            cache = {
                "date": today_text,
                "generated": store.now_text(),
                "items": items[:60],
                "brief": brief,
                "report": result["report"],
            }
            put(data, "news_cache", cache)
            archive = blob(data, "news_archive", [])
            archive = [a for a in archive if a.get("date") != today_text]
            archive.insert(0, {"date": today_text, "brief": brief,
                              "count": len(items), "titles": [i["title"] for i in items[:8]]})
            put(data, "news_archive", archive[:30])
            st.rerun()
    with c2:
        st.caption("关注词决定「跟我有关」的排序，可以随时改：")
        watch_text = st.text_input("关注词（逗号分隔）", value="，".join(watch), key="news_watch_in",
                                   label_visibility="collapsed")
        if st.button("保存关注词", use_container_width=True):
            put(data, "news_watch", [w.strip() for w in watch_text.replace("，", ",").split(",") if w.strip()])
            st.rerun()

    if not cache:
        empty("今天还没生成简报。点上面按钮抓一次，之后一整天都能直接看。")
    else:
        if not fresh:
            st.caption(f'当前显示的是 {cache.get("date")} 的简报，点按钮可更新到今天。')
        if cache.get("brief"):
            st.markdown("#### 📰 今日简报")
            with st.container(border=True):
                st.markdown(cache["brief"])
        items = cache.get("items") or []
        if items:
            st.markdown("#### 🗂 新闻清单")
            tabs = st.tabs(["全部", "国内", "国际", "热点", "跟我有关"])
            groups = {
                "全部": items,
                "国内": [i for i in items if i.get("region") == "国内"],
                "国际": [i for i in items if i.get("region") == "国际"],
                "热点": [i for i in items if i.get("kind") == "热点"],
                "跟我有关": [i for i in items if i.get("hits")],
            }
            for tab, (label, rows) in zip(tabs, groups.items()):
                with tab:
                    if not rows:
                        empty(f"「{label}」这一类今天没有内容")
                    for item in rows[:30]:
                        tag_html = "".join(f'<span class="ws-tag">{esc(t)}</span>' for t in item.get("tags") or [])
                        hit_html = "".join(f'<span class="ws-tag">✓{esc(h)}</span>' for h in item.get("hits") or [])
                        title = esc(item.get("title"))
                        if item.get("link"):
                            title = f'<a href="{esc(item["link"])}" target="_blank">{title}</a>'
                        st.markdown(
                            f'<div class="ws-card"><h5>{title}</h5>'
                            f'<div class="ws-meta">{esc(item.get("source"))}　·　{esc(item.get("pub"))}'
                            f'　·　{esc(item.get("region"))}</div>'
                            f'{tag_html}{hit_html}'
                            + (f'<div class="ws-meta" style="margin-top:.3rem">{esc(item.get("summary"))}</div>'
                               if item.get("summary") else "")
                            + "</div>",
                            unsafe_allow_html=True,
                        )
            if cache.get("report"):
                with st.expander("这次抓取的源与结果", expanded=False):
                    for row in cache["report"]:
                        st.caption(f'{row["source"]}：{"成功 " + str(row["count"]) + " 条" if row["ok"] else "跳过/失败 · " + row["why"]}')

    st.divider()
    with st.expander("📌 自己贴一条新闻，让模型从财经角度拆一下", expanded=False):
        st.caption("抓取失败、或者你看到一条不在 RSS 里的新闻时用这个入口。只处理你粘贴的文字。")
        pasted = st.text_area("新闻标题 / 正文", height=120, key="news_paste",
                              placeholder="粘贴标题和正文，或只贴标题也可以")
        if st.button("分析这条", key="news_paste_go", use_container_width=True, disabled=ai is None):
            if not pasted.strip():
                st.error("先贴点内容")
            else:
                with st.spinner("正在分析…"):
                    try:
                        reply = ai(
                            "你是财经编辑兼求职顾问。只根据给定新闻写：①这件事的财经含义（3 条）；"
                            "②常见误读（1 条）；③对应到会计/财务/审计/风控岗位，面试里可以怎么用（2 条）。"
                            "不预测涨跌、不给投资建议、不编造数字。",
                            f"我的背景：{background[:300]}\n\n新闻原文：\n{pasted.strip()[:2500]}",
                        )
                        st.markdown(reply)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"调用失败：{exc}")

    archive = blob(data, "news_archive", [])
    if archive:
        with st.expander(f"🗄 往期简报（{len(archive)} 期）", expanded=False):
            for entry in archive:
                st.markdown(f"**{entry.get('date')}**　·　{entry.get('count')} 条新闻")
                if entry.get("brief"):
                    st.markdown(entry["brief"][:1200])
                st.divider()


# @@PAGES@@




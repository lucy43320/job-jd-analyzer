"""公司情报模块：把「用户自己粘贴」的企业信息文字抽取成结构化字段。

为什么不做抓取：
    天眼查 / 企查查 / 爱企查的服务条款都禁止抓取，且已有不正当竞争判例；
    国家企业信用信息公示系统同样无公开接口、有验证码。
    所以这里的取数方式只有一个：用户自己在这些平台搜到公司，把页面文字整段
    复制粘贴进来，本模块只做文本抽取。全程不联网、不访问任何网站。

另外提供两个纯字符串生成器（不依赖 Streamlit）：
    radar_svg   —— 五维评分雷达图
    compare_html —— 多对象并排对比表
"""

from __future__ import annotations

import html
import math
import re
from typing import Any

# 模块构建标记，供 app.py 启动时自检是否加载了旧版本（见 store.BUILD 的说明）
BUILD = 20261107161500

# --------------------------------------------------------------------------
# 一、公司信息抽取
# --------------------------------------------------------------------------

COMPANY_SYSTEM_PROMPT = """你是一个信息抽取器。用户会粘贴一段企业信息页面（来自企查查、天眼查或公司官网）的文字，你要把它抽成字段。

必须遵守：
1. 只抽取原文里出现过的信息。抽不到就返回空字符串 ""，绝对不许推断、估算、凭常识补全。
   例如原文没写社保人数，insured 就必须是 ""，不能按规模猜一个数。
2. 注册资本保留原文写法与单位，例如 "45.2亿元人民币"、"2000万元人民币"。
3. 社保 / 参保人数填数字与单位，例如 "1234人"；原文写"参保人数 1,234"就填 "1234人"。
4. 企业规模优先用原文标签，例如 "1000-4999人"、"大型"。原文没有就留空，不要换算。
5. group 指所属集团、母公司或控股股东；只有原文明确写了才算，别把上市公司自己当成集团。
6. 原文里有多家公司时，只抽最主要的那一家（页面标题对应的公司）。
7. missing 字段列出你没抽到的项，用中文标签，例如 ["注册资本", "社保人数"]。全抽到了就给空数组。
8. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释。"""

COMPANY_SCHEMA = """{
  "name": "公司全称",
  "group": "所属集团 / 母公司 / 控股股东",
  "capital": "注册资本",
  "insured": "社保或参保人数",
  "scale": "企业规模",
  "location": "所在地（省市）",
  "industry": "行业",
  "established": "成立时间",
  "status": "经营状态（存续/在业/注销等）",
  "missing": ["没抽到的字段名"]
}"""

COMPANY_LABELS: list[tuple[str, str]] = [
    ("name", "公司名称"),
    ("group", "所属集团 / 母公司"),
    ("capital", "注册资本"),
    ("insured", "社保 / 参保人数"),
    ("scale", "企业规模"),
    ("location", "所在地"),
    ("industry", "行业"),
    ("established", "成立时间"),
    ("status", "经营状态"),
]

# 抽取时给模型的文本上限，防止有人粘进来一整本书
MAX_SOURCE_CHARS = 6000

# 用户粘贴内容里出现这些字样时提醒：可能粘错了页面
NOISE_HINTS = ("招聘", "岗位职责", "任职要求", "投递简历", "薪资", "面议")


def normalize_company(raw: dict | None) -> dict:
    """把模型返回收敛成固定结构：所有字段都是字符串，多余的丢掉。"""
    out = {key: "" for key, _ in COMPANY_LABELS}
    out["missing"] = []

    if not isinstance(raw, dict):
        return out

    for key, _ in COMPANY_LABELS:
        value = raw.get(key, "")
        if value is None:
            value = ""
        if isinstance(value, (int, float)):
            value = str(value)
        out[key] = str(value).strip()

    missing = raw.get("missing")
    if isinstance(missing, list):
        out["missing"] = [str(m).strip() for m in missing if str(m).strip()]
    elif isinstance(missing, str) and missing.strip():
        out["missing"] = [w.strip() for w in re.split(r"[，,、;；]", missing) if w.strip()]

    # 一个字段都没抽到时，让上层知道这份粘贴没起作用
    if not any(out[key] for key, _ in COMPANY_LABELS):
        out["missing"] = ["全部字段"]
    return out


def build_company_prompt(text: str) -> str:
    trimmed = text.strip()[:MAX_SOURCE_CHARS]
    return f"""【待抽取的企业信息原文】
{trimmed}

请严格按以下 JSON 结构输出，字段名完全一致，抽不到的填空字符串：
{COMPANY_SCHEMA}"""


def extract_company(text: str, config: tuple[str, str, str]) -> dict:
    """调用模型抽取公司字段。异常向上抛，由调用方统一格式化提示。"""
    from openai import OpenAI  # 延迟导入，未安装时页面仍能打开

    api_key, base_url, model = config
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=90, max_retries=1)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": COMPANY_SYSTEM_PROMPT},
            {"role": "user", "content": build_company_prompt(text)},
        ],
        "temperature": 0,          # 抽取任务不要发挥
        "max_tokens": 900,
    }

    try:
        resp = client.chat.completions.create(
            **kwargs, response_format={"type": "json_object"}
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if any(k in msg for k in ("response_format", "400", "invalid", "unsupported")):
            resp = client.chat.completions.create(**kwargs)
        else:
            raise

    raw = (resp.choices[0].message.content or "").strip()
    return normalize_company(parse_json(raw))


def parse_json(raw: str) -> dict | None:
    """从模型返回里抠 JSON，容忍围栏和前后废话。"""
    import json

    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def looks_like_jd(text: str) -> bool:
    """粗略判断用户是不是把 JD 粘到了公司信息框里。"""
    hits = sum(1 for word in NOISE_HINTS if word in text)
    return hits >= 2


# --------------------------------------------------------------------------
# 二、五维评分雷达图（纯 SVG，不引入绘图库）
# --------------------------------------------------------------------------

# 岗位分析输出的五个评分维度，键名与 prompt 里的 schema 保持一致
RADAR_DIMS: list[tuple[str, str]] = [
    ("hard", "硬性门槛"),
    ("skill", "技能匹配"),
    ("experience", "经验匹配"),
    ("location", "地点与节奏"),
    ("potential", "成长空间"),
]


def radar_svg(scores: dict | None, size: int = 262) -> str:
    """生成五维雷达图。分数缺失或全为 0 时返回空串，由上层决定是否跳过。

    颜色用 currentColor 与半透明描边，浅色和深色主题下都能看清。
    """
    values: list[float] = []
    for key, _ in RADAR_DIMS:
        try:
            value = float((scores or {}).get(key, 0))
        except (TypeError, ValueError):
            value = 0.0
        values.append(max(0.0, min(5.0, value)))

    if not any(values):
        return ""

    cx = cy = size / 2
    radius = size * 0.32
    count = len(RADAR_DIMS)

    def point(index: int, ratio: float) -> tuple[float, float]:
        angle = -math.pi / 2 + 2 * math.pi * index / count
        return cx + radius * ratio * math.cos(angle), cy + radius * ratio * math.sin(angle)

    def polygon(ratio: float) -> str:
        pts = [point(i, ratio) for i in range(count)]
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    parts: list[str] = [
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'role="img" aria-label="五维匹配度雷达图" style="max-width:100%;height:auto;">'
    ]

    # 网格：4 层同心五边形
    for ratio in (0.25, 0.5, 0.75, 1.0):
        parts.append(
            f'<polygon points="{polygon(ratio)}" fill="none" '
            f'stroke="currentColor" stroke-opacity="{0.10 + ratio * 0.10:.2f}" stroke-width="1"/>'
        )

    # 轴线
    for i in range(count):
        x, y = point(i, 1.0)
        parts.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
            f'stroke="currentColor" stroke-opacity="0.16" stroke-width="1"/>'
        )

    # 数据面（颜色走 CSS 变量，换主题时雷达图跟着变）
    data_pts = [point(i, values[i] / 5.0) for i in range(count)]
    data_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in data_pts)
    parts.append(
        f'<polygon points="{data_str}" fill="var(--acc,#4a7fb5)" fill-opacity="0.30" '
        f'stroke="var(--acc,#4a7fb5)" stroke-width="2" stroke-linejoin="round"/>'
    )

    # 顶点圆点
    for x, y in data_pts:
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="var(--acc,#4a7fb5)"/>'
        )

    # 轴标签：贴着外圈放，根据方位决定对齐方式
    for i, (_, label) in enumerate(RADAR_DIMS):
        x, y = point(i, 1.28)
        angle = -math.pi / 2 + 2 * math.pi * i / count
        cos = math.cos(angle)
        anchor = "middle" if abs(cos) < 0.25 else ("start" if cos > 0 else "end")
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" font-size="11.5" fill="currentColor" '
            f'fill-opacity="0.78">{html.escape(label)} {values[i]:.0f}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# 三、多对象并排对比表
# --------------------------------------------------------------------------


def compare_html(
    headers: list[str],
    rows: list[list[str]],
    highlight_row: str = "",
) -> str:
    """生成并排对比表。第一列是维度名，后面每个对象一列。

    highlight_row 命中时该行加底色（用来突出"推荐"这类结论行）。
    """
    if not rows:
        return ""

    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body_parts: list[str] = []

    for row in rows:
        if not row:
            continue
        first = str(row[0])
        is_hl = bool(highlight_row) and first == highlight_row
        cls = ' class="hl"' if is_hl else ""
        cells = "".join(
            f"<td>{html.escape(str(cell)) if str(cell) else '<span class=dim>—</span>'}</td>"
            for cell in row[1:]
        )
        body_parts.append(
            f"<tr{cls}><th scope=row>{html.escape(first)}</th>{cells}</tr>"
        )

    return (
        '<div class="cmp-wrap"><table class="cmp">'
        f"<thead><tr><th></th>{head}</tr></thead>"
        f"<tbody>{''.join(body_parts)}</tbody>"
        "</table></div>"
    )

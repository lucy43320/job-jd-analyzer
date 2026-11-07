"""输入预处理：先把 JD 洗干净、把简历切开，再交给模型。

模型读什么决定它写出什么。原始 JD 里一半是"公司简介/五险一金/简历投至
xx@xx.com"这类噪声，原始简历是一整块没有小标题的文字——喂进去的东西
糊成一团，出来的结论也只能是笼统的。

这里做三件事，都不调模型：
  1. clean_jd      剔掉明确是噪声的行，并回报删了什么（你可以核对）
  2. tag_resume    给简历加段落标记（只加标不删字，信息零损失）
  3. similarity    算两份 JD 有多像，用于发现重复投递
"""

from __future__ import annotations

import re

BUILD = 20261107213000

# 整行命中这些词、且该行本身不长 → 判定为噪声。长段落不动，避免误删职责描述。
# 注意：公司简介不是噪声 —— 规模、行业、所在地都能用来判断这岗位值不值得投，
# 删掉反而是损失，所以这里不动它。
NOISE_PATTERNS = (
    (r"福利待遇|薪酬福利|员工福利|我们提供|你能得到|你将获得", "福利清单"),
    (r"五险一金|六险一金|补充医疗|带薪年假|节日福利|定期体检|免费班车|餐补|下午茶|员工旅游|团建", "福利条目"),
    (r"简历投递|投递邮箱|简历发送|发送至|投递方式|hr邮箱|邮箱[:：]|@[\w.-]+\.(com|cn|net)", "联系方式"),
    (r"联系电话|联系方式|联系人[:：]|电话[:：]|微信[:：]|扫码|二维码|长按识别", "联系方式"),
    (r"欢迎.{0,8}(加入|投递|自荐)|期待你的加入|join\s*us", "招募话术"),
    (r"^©|版权所有|copyright|icp备|沪icp|京icp", "页脚"),
    (r"^[-=_*·•]{3,}$", "分隔线"),
)

# 简历常见小标题 → 归一化段名
RESUME_SECTIONS = (
    ("教育背景", r"教育(背景|经历|情况)|学历背景|求学经历"),
    ("实习经历", r"实习(经历|经验|情况)|实践经历"),
    ("工作经历", r"工作(经历|经验|情况)|职业经历"),
    ("项目经历", r"项目(经历|经验|情况|实践)"),
    ("校园经历", r"校园(经历|活动|实践)|学生工作|社团|志愿服务"),
    ("技能证书", r"技能(证书|清单)?|证书|资质|语言能力|专业能力"),
    ("荣誉奖项", r"荣誉(奖项|证书)?|奖项|获奖|奖学金"),
    ("自我评价", r"自我评价|个人总结|自我介绍|兴趣爱好"),
)

MAX_REPORT = 6   # 页面上最多展示几条被删的样例


def _is_noise(line: str) -> str | None:
    """命中的话返回噪声类别名，否则 None。"""
    text = line.strip()
    if not text:
        return "空行"
    low = text.lower()
    for pattern, label in NOISE_PATTERNS:
        if re.search(pattern, low, re.IGNORECASE):
            # 长段落多半是正文夹了关键词，不动它
            if len(text) > 90 and label in ("福利条目", "福利清单"):
                continue
            return label
    return None

def clean_jd(text: str) -> dict:
    """剔除噪声行。

    返回 deleted（删掉的原文，供你核对）与 kept_chars，删了多少一目了然，
    不做静默修改。
    """
    raw_lines = str(text or "").splitlines()
    kept: list[str] = []
    deleted: list[dict] = []
    seen: set[str] = set()

    for line in raw_lines:
        label = _is_noise(line)
        if label:
            if line.strip():
                deleted.append({"label": label, "text": line.strip()[:60]})
            continue
        # 完全重复的行只留一次（JD 里复制粘贴导致的重灾区）
        key = re.sub(r"\s+", "", line)
        if key and key in seen:
            deleted.append({"label": "重复行", "text": line.strip()[:60]})
            continue
        if key:
            seen.add(key)
        kept.append(line.rstrip())

    # 连续空行压成一个
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

    return {
        "text": cleaned,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "before_chars": len(str(text or "")),
        "after_chars": len(cleaned),
        "changed": cleaned != str(text or "").strip(),
    }


def tag_resume(text: str) -> str:
    """给简历加段落标记，只加标不删字。

    模型看到「【实习经历】」这样的边界，引用你的经历时能精确指到某一段，
    而不是笼统地说"你的经历比较匹配"。原文一个字都不动，所以不会丢信息。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    lines = raw.splitlines()
    hits: list[tuple[int, str]] = []

    for idx, line in enumerate(lines):
        probe = line.strip()
        if not probe or len(probe) > 24:
            continue
        for name, pattern in RESUME_SECTIONS:
            if re.fullmatch(rf"[【\[（(]?\s*(?:{pattern})\s*[】\]）)]?[:：]?", probe, re.IGNORECASE):
                hits.append((idx, name))
                break

    if len(hits) < 2:
        # 认不出结构就别乱切，原样返回
        return raw

    marked: list[str] = []
    hit_map = dict(hits)
    for idx, line in enumerate(lines):
        if idx in hit_map:
            marked.append("")
            marked.append(f"【{hit_map[idx]}】")
        else:
            marked.append(line)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(marked)).strip()


def describe_resume(text: str) -> list[str]:
    """回报识别到了哪几段，方便你确认切得对不对。"""
    tagged = tag_resume(text)
    return re.findall(r"【(.+?)】", tagged)


def _shingles(text: str, n: int = 3) -> set[str]:
    dense = re.sub(r"[\s，。、；：（）()\[\]【】]", "", str(text or ""))
    if len(dense) < n:
        return {dense} if dense else set()
    return {dense[i:i + n] for i in range(len(dense) - n + 1)}


def similarity(a: str, b: str) -> float:
    """字符 3-gram Jaccard。中文不需要分词，这个指标够用了。

    实测参考：同一份 JD 改个标题 ≈ 0.75+；同公司不同岗位 ≈ 0.3-0.45；
    完全无关的两个岗位 ≈ 0.05-0.15。
    """
    left, right = _shingles(a), _shingles(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def similar_jobs(job: dict, jobs: list[dict], threshold: float = 0.5) -> list[dict]:
    """找出跟这份岗位高度相似的已有记录，避免同一份 JD 反复分析和反复投。"""
    target = job.get("jd_text") or ""
    if not target:
        return []

    out: list[dict] = []
    for other in jobs:
        if other is job or other.get("id") == job.get("id"):
            continue
        other_text = other.get("jd_text") or ""
        if not other_text:
            continue
        score = similarity(target, other_text)
        if score >= threshold:
            out.append({
                "job": other,
                "score": score,
                "same_company": (
                    str(other.get("company") or "").strip()
                    == str(job.get("company") or "").strip()
                    and bool(str(job.get("company") or "").strip())
                ),
            })

    out.sort(key=lambda x: -x["score"])
    return out


def group_duplicates(jobs: list[dict], threshold: float = 0.55) -> list[list[dict]]:
    """把互相相似的岗位聚成组（并查集）。用于在历史记录里折叠重复项。"""
    parent = list(range(len(jobs)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i in range(len(jobs)):
        for k in range(i + 1, len(jobs)):
            a = jobs[i].get("jd_text") or ""
            b = jobs[k].get("jd_text") or ""
            if a and b and similarity(a, b) >= threshold:
                union(i, k)

    buckets: dict[int, list[dict]] = {}
    for idx, job in enumerate(jobs):
        buckets.setdefault(find(idx), []).append(job)

    return [g for g in buckets.values() if len(g) > 1]


def render_clean_report(report: dict) -> str:
    """去噪结果的小结条。"""
    import html as _html

    if not report.get("changed"):
        return '<div class="cl-note ok">JD 里没发现明显噪声，按原文分析。</div>'

    saved = report["before_chars"] - report["after_chars"]
    samples = "；".join(
        f'{_html.escape(d["label"])}「{_html.escape(d["text"][:24])}」'
        for d in report["deleted"][:MAX_REPORT]
    )
    more = "…" if report["deleted_count"] > MAX_REPORT else ""

    return (
        f'<div class="cl-note">已剔除 {report["deleted_count"]} 行噪声'
        f'（{saved} 字）：{samples}{more}</div>'
    )


# --------------------------------------------------------------------------
# 面试记录解析：把「复制来的一整段」拆成 问题 / 我的回答 / 优化回答
# --------------------------------------------------------------------------
# 用户面完会把聊天记录、手写笔记整段粘进来，格式五花八门：
#   "1、自我介绍  答：我是……"        "Q: 为什么转行？ A: 因为……"
#   "优化答案：可以从……"              "面试官：你还有什么想问的？"
# 这里只做结构切分，不改一个字 —— 拆错了用户能当场看到并改。

_INT_B = re.compile(r"^(?:优化答案|优化回答|优化后的答案|更好的回答|建议回答|参考答案|改进版|优化后)\s*[:：]\s*")
_INT_A = re.compile(r"^(?:答\s*[:：]|A\s*[:：]|我的回答\s*[:：]|我的答案\s*[:：]|你的回答\s*[:：]|回答\s*[:：])\s*")
_INT_Q = re.compile(
    r"^(?:Q\s*\d{0,2}\s*[:：]|问\s*[:：]|面试官\s*[:：]|HR\s*[:：]|"
    r"\d{1,2}\s*[.、)）:：]\s*|[（(]\s*\d{1,2}\s*[)）]\s*)"
)
MAX_Q_LEN = 120   # 再长就不是题目，是一段话

# --------------------------------------------------------------------------
# 录音转写 / AI 总结的整段文本：标记全挤在一行里，还带 Markdown 装饰。
# 例如："Q1 ###✅【面试官问题+你的回答 逐条排列】### Q1: ……？**你的回答**: ……
# 先归一化：去装饰、把行内标记换成真正的换行，再走上面的逐行解析。
# --------------------------------------------------------------------------

# 【面试官问题+你的回答 逐条排列】这类小标题是转写工具加的，不是内容
_INT_TITLE_JUNK = re.compile(
    r"【[^】]{0,30}(?:逐条排列|问题\s*[+＋]\s*你的回答|面试官问题\s*[+＋])[^】]*】\s*"
)
_INT_MD_JUNK = re.compile(r"[*#`]{1,6}")                     # **加粗** / ### 标题 / `代码`
_INT_TITLE_BRACKET = re.compile(r"^\s*【[^】]{0,20}】\s*")    # 行首的独立小标题
_INT_A_INLINE = re.compile(
    r"[【\*（(]?\s*(?:你的回答|我的回答|我的答案|考生回答)\s*[】\*）)]?\s*[:：]?\s*"
)
_INT_B_INLINE = re.compile(
    r"[【\*（(]?\s*(?:优化答案|优化回答|优化后的答案|更好的回答|建议回答|参考答案|改进版)\s*[】\*）)]?\s*[:：]\s*"
)
_INT_Q_INLINE = re.compile(
    r"(?:^|(?<=[。！？!?；;…\s]))((?:Q|问题)\s*\d{0,2}\s*[:：]|面试官\s*[:：])"
)
_INT_NUM_INLINE = re.compile(
    r"([。！？!?；;…])\s*((?:\d{1,2}\s*[、.．])|(?:[（(]\s*\d{1,2}\s*[)）]))"
)


def normalize_interview_log(text: str) -> str:
    """录音转写 / AI 总结的整段 → 带换行的行式文本。

    只做三件事：去掉 Markdown 与转写工具加的小标题装饰；
    把挤在一行里的「你的回答：」「Q2:」等标记前面插入换行；
    中文句号后面的「2、」编号前插入换行。不改任何正文文字。
    """
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _INT_TITLE_JUNK.sub("", t)          # 【面试官问题+你的回答 逐条排列】
    t = _INT_MD_JUNK.sub("", t)             # ** ### `
    t = _INT_TITLE_BRACKET.sub("", t, count=1)  # 开头孤立的小标题，只去一个
    t = _INT_A_INLINE.sub("\n我的回答：", t)     # 统一成行首标记，交给 _INT_A
    t = _INT_B_INLINE.sub("\n优化答案：", t)
    t = _INT_Q_INLINE.sub(lambda m: "\n" + m.group(1), t)
    t = _INT_NUM_INLINE.sub(lambda m: m.group(1) + "\n" + m.group(2), t)
    return t


def parse_interview_log(text: str) -> list[dict]:
    """整段面试记录 → [{'q': 问题, 'my': 我的回答, 'better': 优化回答}]。

    没有标记时的兜底：以问号结尾的短行当题目，其余文字接到上一题下面。
    录音转写 / AI 总结那种标记挤在一行的格式会先经过 normalize_interview_log。
    """
    raw = normalize_interview_log(text)
    lines = [ln.strip() for ln in raw.split("\n")]

    items: list[dict] = []
    cur: dict | None = None
    mode = "q"

    def start(question: str) -> dict:
        item = {"q": question.strip(), "my": "", "better": ""}
        items.append(item)
        return item

    for line in lines:
        if not line:
            continue

        m = _INT_B.match(line)
        if m:
            if cur is None:
                cur = start("（这段没有写问题）")
            body = line[m.end():].strip()
            cur["better"] = (cur["better"] + "\n" + body).strip() if cur["better"] else body
            mode = "b"
            continue

        m = _INT_A.match(line)
        if m:
            if cur is None:
                continue
            body = line[m.end():].strip()
            cur["my"] = (cur["my"] + "\n" + body).strip() if cur["my"] else body
            mode = "a"
            continue

        m = _INT_Q.match(line)
        if m and len(line) <= MAX_Q_LEN + 10:
            cur = start(line[m.end():].strip())
            mode = "q"
            continue

        # 没有编号也没有 Q/A 标记：问号结尾的短句就是题目
        if line.endswith(("？", "?")) and len(line) <= MAX_Q_LEN:
            cur = start(line)
            mode = "q"
            continue

        if cur is None:
            cur = start(line)
            mode = "q"
            continue

        if mode == "q":
            cur["q"] = (cur["q"] + " " + line).strip()
        elif mode == "a":
            cur["my"] = (cur["my"] + "\n" + line).strip()
        else:
            cur["better"] = (cur["better"] + "\n" + line).strip()

    # 首尾装饰残留（如转写工具写的孤零零「Q1 ✅」「###」）不是真问题，滤掉。
    # 判据：把装饰字符全剥掉后一个正文字符都不剩的标题，就是装饰。
    _deco = re.compile(r"[\sQqＱ0-9#＃*＊✅✨☑✔✳❗❓\.。、,:：;；\-—–=_【】\[\]（）()]+")
    return [x for x in items if x["q"].strip() and _deco.sub("", x["q"])]

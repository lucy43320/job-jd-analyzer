"""
会计专硕背景 · 岗位JD智能分析助手
==================================
一个基于 Streamlit + 大模型（OpenAI 兼容接口）的求职 JD 分析工具。

设计边界（已在代码层面硬性落地，不依赖模型自觉）：
  1. 联网只有一处入口：用户主动贴链接并点「抓取」，读那一页 JD。抓取逻辑全在
     fetch_jd.py，进入前必过三道闸 —— 协议白名单、平台黑名单（天眼查/企查查等
     条款禁止且已有判例）、robots.txt。抓到的文字先给用户看，确认才进输入框。
     不做的事：不登录、不绕验证码、不伪装 UA、不翻页、不并发、不留 cookie。
  2. 不读本地文件 —— 除 st.secrets / 环境变量读取 API 配置外，不读任何用户文件，
     不上传、不遍历目录。
  3. 不发邮件 / 不投递 / 不发消息 —— 没有任何 smtplib、邮件或 IM 发送逻辑。
  4. 唯一允许的落地动作是用户自己点击"下载 Markdown / CSV / Word"，文件落在他本机。
  5. 公司工商信息仍然禁止抓取 —— 企查查/天眼查/爱企查的服务条款都不允许，
     国家企业信用信息公示系统也没有公开接口。所以公司信息只接受用户
     自己复制粘贴的文字，抽取逻辑在 intel.py，全程不联网。
  6. 公司库、收藏、历史记录只写在本地 data.json 里，不上传任何服务器。
  7. 隐私脱敏 —— 调用云端模型前，姓名/手机号/邮箱/身份证/院校/公司全称会先在
     本机替换成 [P1] 占位符，返回后再还原。见 privacy.py。这是"降低泄露面"，
     不是"消除"：文本仍然必须离开本机才能被模型处理，这是云端 API 的原理，
     任何配置都绕不开。要彻底不出网只能用本地模型（见「⚙️ 求职设置」里的模型配置说明）。

运行方式：
    pip install -r requirements.txt
    # 配置好 Key（见 README.md），然后：
    streamlit run app.py
"""

from __future__ import annotations

import calendar as calmod
import html
import importlib
import io
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import streamlit as st

import extract   # 从「整页复制的网页文字」抽岗位/公司/薪资/JD 正文（纯本地）
import fetch_jd  # 按 robots.txt 合规抓取一页 JD（唯一会主动联网的模块）
import funnel    # 投递漏斗：用已有 stage 数据算转化率（纯本地）
import intel     # 公司信息抽取 + 雷达图 + 对比表（纯函数，不联网）
import news      # 国际视野：公开 RSS 财经新闻（用户点按钮才联网，先查 robots.txt）
import prep      # 输入预处理：JD 去噪 / 简历分段 / 岗位相似度（纯本地）
import privacy   # 发往模型前的本地脱敏 + 返回后还原（纯函数，不联网）
import scoring   # 硬条件核查：模型只抽取，本地规则判定（纯本地）
import store     # 本机 data.json 读写：公司库 / 岗位记录 / 收藏
import auth     # 多用户：Supabase 登录 + 每人一份云端数据（没配就退回本地单用户）
import workspace # 工作台扩展：简历/Offer/心态/能力/人脉/秋招节奏/AI助手/社招（纯本地 + SVG 图表）

# --------------------------------------------------------------------------
# 热重载自保
# Streamlit 只在文件变化时重跑 app.py，不会重新 import 依赖模块。
# 结果就是：store.py 已经更新，页面却还在调用旧函数，报出看不懂的 KeyError
# （真实踩过：改了 stats() 之后侧边栏报 KeyError: 'interviews'）。
# 这里比对构建标记，不一致就把磁盘上的新代码重新加载进来，用户不用重启服务。
# --------------------------------------------------------------------------
_EXPECTED_BUILD = 20261107161500
_RELOADED: list[str] = []   # 注意：这里不能调 st.*，set_page_config 必须是最早的 st 命令
for _mod in (store, privacy, intel, fetch_jd, prep, scoring, funnel, extract, news, auth):
    if getattr(_mod, "BUILD", 0) != _EXPECTED_BUILD:
        importlib.reload(_mod)
        _RELOADED.append(f"{_mod.__name__}.py")

# workspace 是后加的扩展模块，构建号独立维护，不参与上面那组比对
if getattr(workspace, "BUILD", 0) != 20261107160000:
    importlib.reload(workspace)
    _RELOADED.append("workspace.py")

# ==========================================================================
# 一、常量与配置
# ==========================================================================

# 侧边栏「个人背景」默认值：只放客观事实，不掺偏好
DEFAULT_BACKGROUND = (
    "会计专硕研二，base成都；掌握 Python、Streamlit 网页工具开发；"
    "完成牧原 ESG 熵权法‑TOPSIS 量化研究；"
    "暂无大量全职财务实习经历"
)

# 侧边栏「求职偏好与性格」默认值：偏好部分沿用用户原话；性格与 MBTI 留空由用户自己填
DEFAULT_PREFERENCE = (
    "优先双休；可接受包住制造业；同时关注 TMT 战略类实习"
)

# 身份单独一栏：它决定整份分析的判断口径，混在背景里模型经常读漏
IDENTITY_OPTIONS = [
    "应届生 · 在校（赶秋招/春招）",
    "应届生 · 已毕业（未就业/待业中）",
    "实习生 · 在读（找日常实习）",
    "往届生 · 毕业 1-2 年（走社招/补录）",
    "社招 · 在职（考虑跳槽）",
]
DEFAULT_IDENTITY = IDENTITY_OPTIONS[0]

# 学历单独两栏：很多岗位卡的不是"硕士"而是"本科是否双非/985"，
# 混在一段背景里模型经常只看到"会计专硕"就放过学历关
EDUCATION_OPTIONS = ["硕士", "本科", "专科", "博士"]
DEFAULT_EDUCATION = "硕士"
SCHOOL_TIER_OPTIONS = ["不填", "985", "211 / 双一流", "普通本科（双非）", "海外 / 中外合办"]
DEFAULT_SCHOOL_TIER = "不填"

# 内存占用的粗略提示：常驻进程 + 每次分析的额外开销
MEM_TIP = (
    "运行时约占 150-350MB 内存（Streamlit 常驻进程，含 Python 与依赖）。"
    "历史结果只存磁盘、不常驻内存，所以记录攒多了也不会更吃内存；"
    "真正会临时拉高占用的是「一次贴很长的 JD / 上传大体积 PDF」。"
)

# 所有 AI 产出内容的强制结尾标注
SAFETY_FOOTER = "【AI草稿，必须人工审阅修改后再对外使用】"

# 单次分析的 JD 长度上限，防止超长文本打爆上下文
MAX_JD_CHARS = 8000

# 简历文件限制
MAX_RESUME_BYTES = 10 * 1024 * 1024   # 10MB
MAX_RESUME_CHARS = 6000               # 送进模型的简历文本上限

# 批量对比：一次最多几份 JD（每份都要单独调用一次模型，别让它无限涨）
MAX_BATCH = 3
BATCH_SEP = re.compile(r"(?m)^\s*=+\s*$")   # 用单独一行 === 分隔多份 JD

# 默认走 DeepSeek（OpenAI 兼容协议）。换成任意兼容服务只改配置、不改代码。
DEFAULT_API_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

# 识别 JD 文本里夹带的链接：只做提示，不因为文本里有链接就去访问它。
# 真正的抓取入口只有一个 —— 上方「从招聘页链接导入」，且必须先过 robots.txt。
URL_PATTERN = re.compile(r"(https?://|www\.)", re.IGNORECASE)


# ==========================================================================
# 二、系统提示词：约束模型"只基于给定材料、不编造经历"
# ==========================================================================

SYSTEM_PROMPT = """你是一位深耕财务/会计方向应届生求职的资深职业顾问，熟悉"会计专硕 → 制造业财务 / TMT 战略"这条转型路径，也熟悉招聘方在 JD 里藏话的方式。

你会收到这些材料：【我的身份】【我的学历】【个人背景】【求职偏好与性格】【简历原文（可能为空）】【我标注的岗位名称】【我标注的公司名称】【我自己的面试真题（可能为空）】【目标岗位 JD】。

必须遵守的工作原则：
1. 只依据这些材料判断。严禁虚构候选人的实习经历、项目、证书、成绩、获奖、量化数字。材料里没有的，一律写"材料中未体现"，并在建议里给出补救方向。
2. 不恭维、不美化。短板要具体到"面试官会怎么追问、答不上来会怎样"。
3. 严格区分事实与推断：JD 明确写了的是事实；你根据行业惯例推断的，句首标注"推测："。
4. 筛你的不是一个"招聘方"，而是两批人，两套标准，必须分开拆、分开下结论：
   【公司侧】用人部门负责人 / 直属领导 / 业务面官。他们看的是"这人来了能不能干活"：
   具体技能、行业经验、能不能扛住这个岗位的日常、性格搭不搭团队。
   他们通常不看你简历上学历是不是 985，但会在技术面用具体问题把你问穿。
   【HR 侧】招聘 HR / 简历初筛 / 系统筛选。他们看的是"这人合不合规、好不好推进、会不会白忙"：
   学历层次、专业、工作年限、到岗时间、期望薪资是否在区间内、稳定性（跳槽频率/空窗期）、
   简历关键词能不能被搜到、有没有明显硬伤。
   两批人的判断经常不一致：HR 过了但业务面挂，或者业务想要但 HR 卡学历。
   jd_breakdown 必须按这两批人拆开写，并各自给一句 verdict（这关你过不过）。
   company.must / hr.must 的区别：前者是"不会做就是不会做"，后者是"不符合规则就是不符合规则"。
5. 【我的身份】决定判断口径，必须贯穿全文：
   - 应届生（在校）：按校招时间线评估（秋招/春招批次、三方协议、毕业前能否全职到岗），并留意"能否先实习再转正"。
   - 应届生（已毕业）：重点判断是否还算应届生（择业期、是否已缴社保），以及 JD 是否只收在校生。
   - 实习生（在读）：按实习口径评估，关注一周能到岗几天、能否连做三个月以上。
   - 往届生 / 社招：按可迁移经验评估，关注年限缺口、行业经验、离职原因。
   当 JD 的硬性要求和这个身份冲突时（例如要求 3 年经验、只招 2026 届），必须在 match.gaps 的第一条直接写明"这是硬门槛，投了也过不了简历关"，不要含糊成"有挑战"。
6. 【我的学历】单独判断学历关，不要因为背景里写了"硕士"就默认全过：
   - 先看 JD 的学历要求（本科及以上 / 硕士及以上 / 仅限 985、211 / 不限），明确给出"过线"或"不过线"。
   - JD 要求 985/211 或"名校优先"时，结合【院校层次】判断；院校层次是"不填"就写"材料中未体现院校层次，无法判断这一条"，绝不臆测是哪所学校。
   - 学历不过线的，写进 match.gaps 第一条并说明 HR 会怎么筛（系统筛选还是人工看）；学历过线的，写进 strengths 时要点明"过线"这件事，别空写"学历符合"。
7. 做适配岗位推导时，性格描述与 MBTI 只作为参考信号，不要迷信、不要给人贴标签。要说清"这类性格在哪类工作场景里更省力、在哪类场景里更耗神"，并落到具体岗位方向上；推导必须与【我的身份】【我的学历】一致，别给在校生推要求 3 年经验的岗位，也别给专科推"硕士起步"的岗位。
8. 写简历逐条改写建议时，必须引用简历里的真实原句（超过 30 字就截断），再给改法和理由。绝对不许凭空造出新经历、新数字。简历为空时，rewrites 返回空数组。
9. 语言为简体中文，使用中文标点。每条内容尽量控制在 60 字以内，力求可直接拿去做面试准备，不要空话套话。
10. 你只做文本分析：不访问任何网址、不检索外部信息、不发送任何邮件或消息。
11. scores 是这份 JD 与候选人匹配程度的五维评分，每项给 1-5 的整数，五项必须齐全：
    hard=硬性门槛满足度、skill=技能匹配、experience=经验匹配、
    location=地点与工作节奏契合度、potential=成长空间。
    要有区分度，别五项都填 4。
12. 岗位名称与公司名称：如果【我标注的岗位名称】/【我标注的公司名称】已有值，job_title / company_name 必须原样照抄，一个字都不要改；只有标注为空时，才从 JD 原文里取，原文也没写就返回空字符串，绝不推测。
13. decision 只能取"建议投""谨慎投""别浪费简历"三者之一，且必须和 scores 自洽：
    五维里有两项及以上不超过 2 分、或硬性门槛不过线，就不许给"建议投"。
    next_step 写现在就能做的一件具体事（例如"先补一段成本核算实习再投"），不要写"继续努力"。
14. day_in_life 给 3 条，写具体动作：几点在做什么、和谁对接、交付什么东西。
    禁止写"负责财务核算工作"这种岗位说明书式的空话；第 3 条固定写"最容易消耗你的地方"。
15. 面试题（interview 数组）的取材规则：
    - 材料里有【我自己的面试真题】时：必须覆盖其中 2-3 道，题末标注「（真题）」；
      这几道的参考答案以我记录的「优化过的回答」为准，只有「我当时的回答」时以它为事实边界，
      不许超出我写过的经历去编。
    - 但不要整份都是真题：至少一半题目要你自己出 —— 结合 JD 的职责原文、这个岗位
      在真实面试里最常考的 3 个方向（例如风控岗常考：指标口径、模型解释、异常案例复盘），
      以及我在这份分析里暴露出的短板。新出的题不标注。
    - 完全没有真题材料时，按 JD + 我的短板正常出 5 道题，全部不标注。
16. 只输出一个 JSON 对象，不要 markdown 代码块包裹，不要任何额外解释文字。"""


# JSON 结构说明（要求全字段统一用 title/detail，便于稳定渲染）
OUTPUT_SCHEMA = """{
  "company_name": "招聘公司名称（JD 没写就空字符串）",
  "job_title": "岗位名称（JD 没写就空字符串）",
  "role_summary": "一句话概括这个岗位到底在招什么人（30字内）",
  "decision": {"verdict": "建议投 或 谨慎投 或 别浪费简历", "reason": "一句话说清依据（必须与 scores 自洽）", "next_step": "现在就能做的一件具体事"},
  "day_in_life": ["09:00 到岗后的第一件事：做什么、和谁对接、交付什么", "下午的固定动作 + 经常临时插进来的事", "最容易消耗你的地方"],
  "scores": {"hard": 4, "skill": 5, "experience": 2, "location": 4, "potential": 3},
  "hard_conditions": [
    {"kind": "学历", "requirement": "本科及以上，会计/财务相关专业", "must": true, "quote": "全日制本科及以上学历"}
  ],
  "fit_roles": {
    "profile": "把背景+偏好+性格揉成一句话的求职画像（40字内）",
    "roles": [{"title": "适配的岗位方向（具体到岗位名，不要写'财务类'这种笼统说法）", "detail": "为什么适配：对应哪条背景/性格特征 + 这个岗位日常在做什么"}],
    "avoid": [{"title": "不建议优先投的方向", "detail": "为什么不合适：是能力缺口还是性格消耗"}]
  },
  "jd_breakdown": {
    "company": {
      "must":   [{"title": "公司侧硬要求（不会就是不会：技能/行业经验/能不能独立出活）", "detail": "JD 哪句看出来的 + 业务面会怎么验你"}],
      "expect": [{"title": "公司侧真正想要的（JD 没写全的部分）", "detail": "依据 + 报到第一周在哪暴露出来"}],
      "verdict": "公司这关：能过 / 悬 / 过不了 + 一句话理由"
    },
    "hr": {
      "must":   [{"title": "HR 初筛会卡的硬指标（学历/年限/到岗时间/薪资区间/地点）", "detail": "他们用哪个字段卡你，你会因为什么被刷"}],
      "soft":   [{"title": "HR 面试会重点核的软性点（稳定性/动机/空窗期/离职原因）", "detail": "怎么答才不留疑点"}],
      "verdict": "HR 这关：能过 / 悬 / 过不了 + 一句话理由"
    },
    "screening_order": ["第 1 关：谁在看、看什么、大概多久下结论", "第 2 关：…", "第 3 关：…"],
    "implicit": [{"title": "入职后才会暴露的隐性要求（两批人都没说）", "detail": "从哪句 JD 推出来的"}]
  },
  "match": {
    "verdict_level": "高 或 中 或 低",
    "verdict_reason": "一句话说明匹配度判断依据",
    "strengths": [{"title": "优势匹配点（必须能对应到材料里的具体事实）", "detail": "怎么在面试里举证"}],
    "gaps":      [{"title": "短板或风险点", "detail": "面试官可能的追问 + 建议的应对口径"}]
  },
  "resume": {
    "rewrites": [{"title": "简历里的原句（超30字截断）", "detail": "改成什么 + 为什么这么改（缺数据就指明要补什么数据）"}],
    "reorder":  [{"title": "经历排序建议", "detail": "为什么这个顺序对这类岗位有效"}],
    "keywords": ["需要补进简历的关键词，逐个列出，6-10个"],
    "compress": [{"title": "应该压缩或弱化的内容", "detail": "为什么它会稀释你的竞争力"}]
  },
  "interview": [
    {"question": "高概率面试题原文", "approach": "答题思路：先说什么、用什么事实支撑、收在哪", "pitfall": "这题最容易踩的坑"}
  ]
}"""


# ==========================================================================
# 三、大模型调用层（OpenAI 兼容协议）
# ==========================================================================


def read_config(key: str, default: str = "") -> str:
    """从 st.secrets 或环境变量读取配置（不落盘的静态配置来源）。"""
    try:  # st.secrets 在未创建 secrets.toml 时会抛异常，这里兜住
        value = st.secrets.get(key, "")
        if value:
            return str(value).strip()
    except Exception:
        pass
    return (os.environ.get(key, "") or default).strip()


def resolve_config(ui_key: str, ui_base: str, ui_model: str) -> tuple[str, str, str]:
    """返回最终生效的 (api_key, base_url, model)。

    优先级：页面上的临时输入 > secrets.toml / 环境变量 > 代码默认值。
    """
    api_key = (ui_key or "").strip() or read_config("API_KEY") or read_config("OPENAI_API_KEY")
    base_url = (ui_base or "").strip() or read_config("BASE_URL", DEFAULT_API_BASE)
    model = (ui_model or "").strip() or read_config("MODEL", DEFAULT_MODEL)
    return api_key, base_url, model


def find_bad_chars(text: str) -> tuple[int, str] | None:
    """找出第一个非 ASCII 或控制字符，返回 (下标, 字符)。"""
    for idx, ch in enumerate(text):
        if not ch.isascii() or ch in "\r\n\t":
            return idx, ch
    return None


def validate_config(config: tuple[str, str, str]) -> str | None:
    """发请求前先查输入，返回错误说明；没问题返回 None。

    为什么必须前置：Key 里混入中文时，httpx 在设置请求头那一步就会抛
    UnicodeEncodeError，报错原文是 'ascii' codec can't encode characters…，
    用户根本看不出是自己粘贴时带进了中文。
    """
    api_key, base_url, model = config

    if not api_key:
        return "还没填 API Key。"

    bad = find_bad_chars(api_key)
    if bad:
        idx, ch = bad
        shown = ch if ch.isprintable() and not ch.isspace() else "（不可见字符）"
        return (
            f"**API Key 里混入了非法字符**：第 {idx + 1} 个字符是 {shown}（U+{ord(ch):04X}）。\n\n"
            "HTTP 请求头只接受 ASCII 字符，带上它请求根本发不出去。"
            "常见来源：复制时把中文说明、全角符号或零宽空格一起粘进来了。"
            "请把输入框清空，重新粘贴纯 Key。"
        )
    if api_key != api_key.strip():
        return "API Key 首尾有空格或换行，删掉再试。"

    bad = find_bad_chars(base_url)
    if bad:
        idx, ch = bad
        return (
            f"**Base URL 里有非 ASCII 字符**：第 {idx + 1} 个字符 `{ch}`（U+{ord(ch):04X}）。"
            "请填纯英文地址。"
        )
    if not base_url.startswith(("http://", "https://")):
        return "Base URL 要以 `https://` 开头，例如 `https://api.deepseek.com/v1`。"

    bad = find_bad_chars(model)
    if bad:
        idx, ch = bad
        return (
            f"**模型名里有非 ASCII 字符**：第 {idx + 1} 个字符 `{ch}`（U+{ord(ch):04X}）。"
            "请填英文模型名，例如 `deepseek-chat`。"
        )

    return None


def build_request(
    inputs: dict[str, str],
    jd_text: str,
    scrub: bool = True,
    bank: list[dict] | None = None,
) -> tuple[str, dict[str, dict[str, str]]]:
    """组装发给模型的 user_prompt，返回 (文本, 脱敏映射)。

    单独抽出来是为了「发送前预览」：预览看到的必须就是要发出去的那一段，
    否则预览只是心理安慰。预览和正式发送都走这个函数，文本一定一致。
    """
    if bank:
        bank_lines = []
        for idx, item in enumerate(bank[:12], start=1):
            line = (
                f"{idx}. {item['question']}"
                f"（来源：{item.get('company') or '公司未记'} · {item.get('title') or '岗位未记'}"
                f" · {item.get('date') or '日期未记'}）"
            )
            if item.get("my"):
                line += f"\n   我当时的回答：{item['my'][:300]}"
            if item.get("better"):
                line += f"\n   后来优化过的回答：{item['better'][:300]}"
            bank_lines.append(line)
        bank_block = "\n".join(bank_lines)
    else:
        bank_block = "（没有记录，正常按 JD 推导，不要标注真题）"

    # 先洗再发：去掉福利话术/联系方式/重复行；简历加段落标记但不删字。
    # 这里清洗而不是在外层洗，是为了让「发送前预览」看到的就是真正发出去的文本。
    cleaned = prep.clean_jd(jd_text)
    jd_body = cleaned["text"] or jd_text
    resume_tagged = prep.tag_resume((inputs.get("resume") or "").strip())

    parts = {
        "background": inputs.get("background", ""),
        "preference": inputs.get("preference", "") or "（未填写，仅按背景推导）",
        "resume": resume_tagged,
        "company": (inputs.get("company_name") or "").strip(),
        # 真题里带着公司全称，必须和别的一起脱敏，否则"已脱敏"就是假的
        "bank": bank_block,
        "jd": jd_body,
    }

    mapping: dict[str, dict[str, str]] = {}
    if scrub:
        parts, mapping = privacy.scrub_many(parts)

    resume_block = parts["resume"] if parts["resume"] else "（未提供简历，resume.rewrites 返回空数组）"
    identity = (inputs.get("identity") or "").strip() or "（未填写，按应届生口径判断）"
    title_hint = (inputs.get("job_title") or "").strip()

    edu = (inputs.get("education") or "").strip()
    tier = (inputs.get("school_tier") or "").strip()
    tier_text = tier if tier and tier != "不填" else "未填（不要臆测院校层次，也不要猜是哪所学校）"
    edu_line = f"最高学历：{edu or '未填'}；院校层次：{tier_text}"

    user_prompt = f"""【我的身份】
{identity}

【我的学历】
{edu_line}

【个人背景】
{parts["background"]}

【求职偏好与性格】
{parts["preference"]}

【简历原文】
{resume_block}

【我标注的岗位名称】
{title_hint or "（没填，请从 JD 原文里取；原文也没写就返回空字符串）"}

【我标注的公司名称】
{parts["company"] or "（没填，请从 JD 原文里取；原文也没写就返回空字符串）"}

【我自己的面试真题】
{parts["bank"]}

【目标岗位 JD 原文】
{parts["jd"]}

请严格按以下 JSON 结构输出，字段名必须完全一致，数组元素数量按内容需要给足：
fit_roles.roles 3-5 条、fit_roles.avoid 2-3 条、
jd_breakdown 里 company.must / company.expect / hr.must / hr.soft 各 2-4 条，
screening_order 3-4 条、implicit 2-4 条，两批人各给一句 verdict、
match 优劣各 3-5 条、resume.rewrites 3-6 条（未提供简历则给空数组）、interview 恰好 5 条、
day_in_life 恰好 3 条。
scores 的五个键必须齐全，值取 1-5 的整数；decision 必须与 scores 自洽。

hard_conditions 是**抽取**任务，不是判断任务：只把 JD 里明确写出来的门槛抄成条目，
kind 只能取 学历/专业/年限/证书/毕业年份/语言/城市/薪资/其他 之一；
must=true 表示"不满足就会被直接筛掉"，优先/加分/更佳 这类写成 must=false；
requirement 用 JD 的原话，不要改写成"要求具备良好的…"这种模糊表述；
JD 没写门槛就给空数组，不要编。你不需要判断求职者是否满足，那一步在本地做。
{OUTPUT_SCHEMA}"""

    # 告诉模型 [P1] 分别是什么类别，否则它只能写"你的 [P3] 经历"，行业匹配也没法判断
    hints = privacy.hint_block(mapping)
    if hints:
        user_prompt += f"\n\n{hints}"
    return user_prompt, mapping


def call_llm(
    inputs: dict[str, str],
    jd_text: str,
    config: tuple[str, str, str],
    scrub: bool = True,
    bank: list[dict] | None = None,
) -> tuple[str, dict[str, dict[str, str]]]:
    """调用大模型，返回 (原始文本, 脱敏映射)。异常向上抛，由调用方渲染友好提示。

    scrub=True 时，材料（背景/偏好/简历/公司/JD）会先在本机脱敏再发出，
    调用方拿到返回值后用 privacy.restore_obj 把 [P1] 换回真名。
    """
    from openai import OpenAI  # 延迟导入：未安装时页面仍可打开并显示配置指引

    problem = validate_config(config)  # 兜底：任何入口漏了校验都在这里拦住
    if problem:
        raise ValueError(problem)

    api_key, base_url, model = config
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=180, max_retries=1)

    user_prompt, mapping = build_request(inputs, jd_text, scrub=scrub, bank=bank)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    base_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4000,
    }

    # 部分兼容服务不支持 response_format，失败时自动降级重试一次
    try:
        resp = client.chat.completions.create(
            **base_kwargs, response_format={"type": "json_object"}
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if any(k in msg for k in ("response_format", "400", "invalid", "unsupported")):
            resp = client.chat.completions.create(**base_kwargs)
        else:
            raise

    return (resp.choices[0].message.content or "").strip(), mapping


# 异常类型 -> 大白话排查建议
ERROR_HINTS: dict[str, str] = {
    "AuthenticationError": "Key 无效或已失效。检查是否复制完整、首尾有没有空格；确认 Key 和 Base URL 属于同一家服务商（别把 A 家的 Key 填到 B 家的地址上）。",
    "PermissionDeniedError": "这个 Key 没有调用该模型的权限，或账号未开通该模型。",
    "NotFoundError": "接口地址或模型名不存在。检查 Base URL 是否以 /v1 结尾、模型名有没有拼错。",
    "BadRequestError": "请求被拒。常见原因：该模型不支持 response_format 参数、max_tokens 超出上限、或模型名与接口不匹配。",
    "RateLimitError": "请求太频繁被限流（429）。等几秒再试；若持续出现，去控制台看调用频率限制。",
    "APITimeoutError": "请求超时。网络到该接口不稳定，或 JD 实在过长。",
    "APIConnectionError": "连不上接口。检查网络、系统代理设置，以及 Base URL 域名是否拼错。",
    "InternalServerError": "模型服务端出错（5xx）。可能是服务商临时故障，稍后重试即可。",
    "UnicodeEncodeError": "请求里含中文或全角字符，HTTP 协议不接受。请检查 API Key、Base URL、模型名有没有混入中文、全角符号或零宽字符。",
}

# 余额不足单独拎出来：跟网络、Key、模型名都无关，只跟账户里有没有钱有关
INSUFFICIENT_BALANCE_HINT = (
    "**账户余额不足（402），你的配置本身是对的。**\n\n"
    "Key、地址、模型名都正确，是账户里没钱了。DeepSeek 为预付费模式，余额为 0 时任何模型都调不通。\n\n"
    "**怎么办：** 登录服务商控制台充值（DeepSeek 充值入口在「充值」页，10 元起，"
    "按这个工具每次约 2000 tokens 的用量，10 元能跑很多次）。"
    "充值后不用改代码、不用重启，直接再点一次分析即可。\n\n"
    "不想充钱的话，也可以把 Base URL 和模型名换成另一家还有免费额度的服务，"
    "Key 也要一起换。"
)


def format_api_error(exc: Exception) -> str:
    """把 SDK 异常翻译成能照着排查的中文提示。"""
    # validate_config 抛出的纯 ValueError 本身就是给人看的中文，直接透传。
    # 注意：UnicodeEncodeError 也是 ValueError 的子类，必须用精确类型判断先排除它，
    # 否则它会被透传成 'ascii' codec can't encode… 这种没人看得懂的原文。
    if type(exc) is ValueError:
        return str(exc)

    name = type(exc).__name__
    detail = str(exc).strip().replace("\n", " ")
    if len(detail) > 400:
        detail = detail[:400] + "…"
    low = detail.lower()

    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = None

    # 1) 有 HTTP 状态码就优先按状态码判断，比异常类型名精确
    hint = ""
    if (
        status == 402
        or "insufficient balance" in low
        or "insufficient_quota" in low
        or "payment required" in low
    ):
        hint = INSUFFICIENT_BALANCE_HINT
    elif status == 401:
        hint = ERROR_HINTS["AuthenticationError"]
    elif status == 403:
        hint = ERROR_HINTS["PermissionDeniedError"]
    elif status == 404:
        hint = ERROR_HINTS["NotFoundError"]
    elif status == 429:
        hint = ERROR_HINTS["RateLimitError"]
    elif status is not None and status >= 500:
        hint = ERROR_HINTS["InternalServerError"]

    # 2) 没有状态码时按异常类型名
    if not hint:
        hint = ERROR_HINTS.get(name, "")

    # 3) 还识别不出就按关键词兜底
    if not hint:
        if "ascii" in low and ("encode" in low or "codec" in low):
            hint = ERROR_HINTS["UnicodeEncodeError"]
        elif "402" in low or "余额" in low:
            hint = INSUFFICIENT_BALANCE_HINT
        elif "401" in low or "invalid_api_key" in low or "unauthorized" in low or "authentication" in low:
            hint = ERROR_HINTS["AuthenticationError"]
        elif "404" in low or "model_not_found" in low:
            hint = ERROR_HINTS["NotFoundError"]
        elif "429" in low or "rate limit" in low:
            hint = ERROR_HINTS["RateLimitError"]
        elif "timeout" in low or "timed out" in low:
            hint = ERROR_HINTS["APITimeoutError"]
        elif "connection" in low:
            hint = ERROR_HINTS["APIConnectionError"]
        else:
            hint = "未识别的错误，请对照下面原始信息排查，或把这段发给开发者。"

    text = f"**调用失败：{name}**\n\n{hint}"
    if detail:
        text += f"\n\n原始信息：`{detail}`"
    return text


def test_connection(config: tuple[str, str, str]) -> tuple[bool, str]:
    """发一个极小请求，验证 Key / Base URL / 模型名三者是否匹配。"""
    from openai import OpenAI

    api_key, base_url, model = config
    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=0)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
        )
        return True, f"连接成功　·　{base_url}　·　{model} 可以正常调用，去主页面粘贴 JD 吧。"
    except Exception as exc:  # noqa: BLE001
        return False, format_api_error(exc)


def parse_json_response(raw: str) -> dict | None:
    """从模型返回里抠出 JSON 对象，容忍 ```json 围栏和前后多余文字。"""
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


# ==========================================================================
# 四、数据归一化：模型偶尔不按 schema 给值，这里统一成 (标题, 说明) 列表
# ==========================================================================

TITLE_KEYS = (
    "title", "item", "point", "risk", "question", "name",
    "suggestion", "keyword", "text", "标题", "要点",
)


def normalize_items(value: Any) -> list[tuple[str, str]]:
    """把任意形状的字段归一化成 [(标题, 说明), ...]。"""
    if value is None:
        return []
    seq = value if isinstance(value, list) else [value]

    items: list[tuple[str, str]] = []
    for entry in seq:
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                items.append((text, ""))
            continue
        if not isinstance(entry, dict):
            continue

        flat = {
            str(k): str(v).strip()
            for k, v in entry.items()
            if isinstance(v, (str, int, float)) and str(v).strip()
        }
        if not flat:
            continue

        title = ""
        for pref in TITLE_KEYS:
            for key, val in flat.items():
                if key.lower() == pref:
                    title = val
                    break
            if title:
                break
        if not title:
            title = next(iter(flat.values()))

        detail = " ".join(v for v in flat.values() if v != title)
        items.append((title, detail))
    return items


def normalize_interview(value: Any) -> list[tuple[str, str, str]]:
    """归一化面试题，返回 [(题目, 答题思路, 易踩的坑), ...]。

    单独处理，避免把"思路"和"坑"拼成一坨，丢掉结构。
    """
    if not value:
        return []
    seq = value if isinstance(value, list) else [value]

    out: list[tuple[str, str, str]] = []
    for entry in seq:
        if isinstance(entry, str):
            if entry.strip():
                out.append((entry.strip(), "", ""))
            continue
        if not isinstance(entry, dict):
            continue

        question = str(entry.get("question") or entry.get("题目") or "").strip()
        approach = str(entry.get("approach") or entry.get("答题思路") or "").strip()
        pitfall = str(entry.get("pitfall") or entry.get("易踩的坑") or "").strip()

        if not question:  # 字段名被模型换掉时的兜底
            vals = [str(v).strip() for v in entry.values() if isinstance(v, str) and v.strip()]
            if not vals:
                continue
            question = vals[0]
            approach = approach or " ".join(vals[1:-1])
            pitfall = pitfall or (vals[-1] if len(vals) > 2 else "")

        out.append((question, approach, pitfall))
    return out


def normalize_keywords(value: Any) -> list[str]:
    """关键词展示，接受字符串或对象列表。"""
    out: list[str] = []
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                out.append(entry.strip())
            elif isinstance(entry, dict):
                for val in entry.values():
                    if isinstance(val, str) and val.strip():
                        out.append(val.strip())
                        break
    elif isinstance(value, str) and value.strip():
        out = [w.strip() for w in re.split(r"[，,、;；]", value) if w.strip()]
    # 去重保序
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def normalize_breakdown(value: Any) -> dict[str, Any]:
    """把 jd_breakdown 统一成「公司侧 / HR 侧」两批人的结构。

    新结构：company{must,expect,verdict} + hr{must,soft,verdict} + screening_order + implicit
    旧结构（历史记录）：hard / implicit / hr_focus —— 老数据不能白丢，
    映射成 company_must / implicit / hr_must 继续显示，并在页面上标一句"旧版结果"。
    """
    src = value if isinstance(value, dict) else {}
    company = src.get("company") if isinstance(src.get("company"), dict) else {}
    hr = src.get("hr") if isinstance(src.get("hr"), dict) else {}

    legacy = bool(src.get("hard") or src.get("hr_focus")) and not (company or hr)

    company_must = normalize_items(company.get("must"))
    company_expect = normalize_items(company.get("expect"))
    hr_must = normalize_items(hr.get("must"))
    hr_soft = normalize_items(hr.get("soft"))

    if legacy:  # 老结果兜底
        company_must = normalize_items(src.get("hard"))
        hr_must = normalize_items(src.get("hr_focus"))

    order_raw = src.get("screening_order")
    order: list[str] = []
    if isinstance(order_raw, list):
        order = [str(x).strip() for x in order_raw if str(x).strip()]

    return {
        "company_must": company_must,
        "company_expect": company_expect,
        "company_verdict": str(company.get("verdict") or "").strip(),
        "hr_must": hr_must,
        "hr_soft": hr_soft,
        "hr_verdict": str(hr.get("verdict") or "").strip(),
        "order": order,
        "implicit": normalize_items(src.get("implicit")),
        "legacy": legacy,
    }


def verdict_tone(text: str) -> tuple[str, str]:
    """把「能过 / 悬 / 过不了」判成颜色。注意先判"过不了"，否则会被"能过"误伤。"""
    if "过不了" in text or "不过" in text:
        return "rgba(180,60,60,.13)", "#a33"
    if "能过" in text or "可以过" in text:
        return "rgba(27,127,90,.13)", "#1b7f5a"
    if "悬" in text or "勉强" in text or "有风险" in text:
        return "rgba(190,140,30,.15)", "#8a6412"
    return "rgba(128,128,128,.10)", "inherit"


# ==========================================================================
# 五、简历文件解析（只解析用户在页面上主动上传的那一份文件）
# ==========================================================================


def extract_resume_text(uploaded) -> tuple[str, str]:
    """从用户主动上传的简历里提取纯文本，返回 (文本, 提示信息)。

    边界：只处理传进来的这一个文件对象，不遍历目录、不按路径读盘、不落盘缓存。
    uploaded 为空时返回 ("", "")。
    """
    if uploaded is None:
        return "", ""

    name = (getattr(uploaded, "name", "") or "").lower()

    try:
        raw = uploaded.getvalue()
    except Exception as exc:  # noqa: BLE001
        return "", f"读取文件失败：{type(exc).__name__}"

    if not raw:
        return "", "文件是空的。"
    if len(raw) > MAX_RESUME_BYTES:
        return "", f"文件超过 {MAX_RESUME_BYTES // (1024 * 1024)}MB，请导出精简版后再传。"

    import io

    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif name.endswith(".docx"):
            from docx import Document

            doc = Document(io.BytesIO(raw))
            parts = [p.text for p in doc.paragraphs]
            # 简历常用表格排版，表格里的文字也必须取出来
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            parts.append(cell.text)
            text = "\n".join(parts)
        elif name.endswith(".doc"):
            return "", "不支持旧版 .doc，请用 Word 另存为 .docx 后再上传，或直接粘贴文本。"
        elif name.endswith((".txt", ".md")):
            text = raw.decode("utf-8", errors="ignore")
        else:
            return "", "只支持 PDF / .docx / .txt，其它格式请另存，或直接粘贴文本。"
    except ImportError as exc:
        return "", f"缺少解析库 {getattr(exc, 'name', '')}，请运行 pip install pypdf python-docx。"
    except Exception as exc:  # noqa: BLE001
        return "", f"解析失败：{type(exc).__name__}。文件可能损坏或加密，建议改用粘贴文本。"

    # 清理 PDF 提取常带来的碎空格与连续空行
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()

    if len(text) < 30:
        return "", "没提取到文字。扫描件（图片型 PDF）会这样，请改用「直接粘贴简历内容」。"
    if len(text) > MAX_RESUME_CHARS:
        return text[:MAX_RESUME_CHARS], f"简历偏长，已截断到前 {MAX_RESUME_CHARS} 字送分析。"
    return text, ""


# ==========================================================================
# 六、渲染层
# ==========================================================================

# --------------------------------------------------------------------------
# 配色：整站只靠三个变量驱动（主色 / 深色 / 浅底），换主题只替换这几行
# --------------------------------------------------------------------------
THEMES: dict[str, dict[str, str]] = {
    "雾蓝": {"acc": "#4a7fb5", "acc2": "#20456b", "soft": "rgba(74,127,181,.13)",
             "line": "rgba(74,127,181,.38)"},
    "墨绿": {"acc": "#2f8a72", "acc2": "#17463b", "soft": "rgba(47,138,114,.13)",
             "line": "rgba(47,138,114,.38)"},
    "绛紫": {"acc": "#7b5ea7", "acc2": "#3f2f63", "soft": "rgba(123,94,167,.13)",
             "line": "rgba(123,94,167,.38)"},
    "暖橙": {"acc": "#cc7a3c", "acc2": "#8a4a18", "soft": "rgba(204,122,60,.15)",
             "line": "rgba(204,122,60,.40)"},
    "石墨": {"acc": "#5f7185", "acc2": "#2a323c", "soft": "rgba(95,113,133,.15)",
             "line": "rgba(95,113,133,.40)"},
}
DEFAULT_THEME = "雾蓝"

CSS_TEMPLATE = """
<style>
:root {
  --acc: __ACC__; --acc2: __ACC2__; --soft: __SOFT__; --acc-line: __LINE__;
  --radius: 14px; --line: rgba(128,128,128,.20);
  --card: rgba(255,255,255,.62);
}
@media (prefers-color-scheme: dark) { :root { --card: rgba(255,255,255,.035); } }

/* ---------- 顶部标题横幅 ---------- */
.hero { position:relative; border-radius:18px; padding:1.05rem 1.3rem 1rem;
        margin-bottom:1rem; overflow:hidden;
        background:linear-gradient(135deg, var(--soft), transparent 65%),
                   linear-gradient(180deg, var(--card), transparent);
        border:1px solid var(--acc-line);
        box-shadow:0 8px 26px var(--soft); }
.hero::after { content:""; position:absolute; right:-40px; top:-60px; width:220px; height:220px;
        border-radius:50%; background:var(--soft); filter:blur(6px); }
.hero-t { font-size:1.45rem; font-weight:800; letter-spacing:.5px; line-height:1.35; }
.hero-t em { font-style:normal;
        background:linear-gradient(120deg, var(--acc), var(--acc2));
        -webkit-background-clip:text; background-clip:text; color:transparent; }
.hero-s { font-size:.90rem; opacity:.72; margin-top:.28rem; line-height:1.55; }
.hero-tags { display:flex; flex-wrap:wrap; gap:.4rem; margin-top:.7rem; }
.hero-tags span { font-size:.76rem; padding:.16rem .6rem; border-radius:999px;
        border:1px solid var(--acc-line); background:var(--card); font-weight:600;
        color:var(--acc2); }

/* ---------- 页面底色：两团主色光晕，比纯白/纯黑有层次 ---------- */
[data-testid="stAppDeployButton"] { display:none; }   /* 按钮在这儿没意义，藏掉更干净 */
.stApp {
  background:
    radial-gradient(1100px 520px at 12% -14%, var(--soft), transparent 62%),
    radial-gradient(820px 460px at 96% 2%, var(--soft), transparent 58%);
}
.block-container { padding-top: 2.6rem; padding-bottom: 3rem; max-width: 1580px; }
h1, h2, h3, h4, h5 { letter-spacing: .2px; }
h4 { border-left: 3px solid var(--acc); padding-left: .55rem; }

/* ---------- 侧边栏 ---------- */
section[data-testid="stSidebar"] {
  background: linear-gradient(180deg, var(--soft), transparent 46%);
  border-right: 1px solid var(--line);
}
section[data-testid="stSidebar"] .block-container { padding-top: 1.2rem; }

/* ---------- 卡片：圆角 + 细边 + 轻阴影，hover 微微抬起 ---------- */
[data-testid="stVerticalBlockBorderWrapper"] > div {
  border-radius: var(--radius) !important;
  border: 1px solid var(--line) !important;
  background: var(--card) !important;
  box-shadow: 0 4px 16px rgba(30,50,80,.06);
  transition: box-shadow .2s ease, transform .2s ease;
}
[data-testid="stVerticalBlockBorderWrapper"] > div:hover {
  box-shadow: 0 8px 26px rgba(30,50,80,.11);
}

/* ---------- 页签 ---------- */
.stTabs [data-baseweb="tab-list"] { gap: .3rem; border-bottom: 1px solid var(--line); }
.stTabs [data-baseweb="tab"] {
  border-radius: 10px 10px 0 0; padding: .45rem 1rem; font-weight: 600; opacity: .72;
  transition: background .15s ease, opacity .15s ease;
}
.stTabs [data-baseweb="tab"]:hover { opacity: 1; background: rgba(128,128,128,.06); }
.stTabs [aria-selected="true"] { opacity: 1; background: var(--soft); }
.stTabs [data-baseweb="tab-highlight"] { background: var(--acc); height: 3px; border-radius: 3px; }

/* ---------- 按钮 ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
  border-radius: 10px; font-weight: 600; transition: .18s ease;
}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
  transform: translateY(-1px); box-shadow: 0 6px 18px rgba(30,50,80,.14);
}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background: linear-gradient(135deg, var(--acc), var(--acc2));
  border: none; color: #fff;
}

/* ---------- 输入框聚焦 ---------- */
.stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus {
  border-color: var(--acc) !important;
  box-shadow: 0 0 0 3px var(--soft) !important;
}

/* ---------- 折叠面板 ---------- */
[data-testid="stExpander"] details {
  border-radius: 12px !important; border: 1px solid var(--line) !important;
}
[data-testid="stExpander"] summary:hover { color: var(--acc); }

/* ---------- 板块标题 ---------- */
.sec { display:flex; align-items:center; gap:.6rem; font-size:1.10rem;
       font-weight:700; margin:.1rem 0 .75rem 0; letter-spacing:.2px; }
.sec-num { display:inline-flex; width:1.65rem; height:1.65rem; border-radius:.5rem;
           background:linear-gradient(135deg,var(--acc),var(--acc2)); color:#fff;
           font-size:.85rem; font-weight:700; align-items:center;
           justify-content:center; flex:none;
           box-shadow:0 3px 10px var(--soft); }
.sub { font-size:.92rem; font-weight:700; opacity:.88; margin:.5rem 0 .4rem 0; }
.item { margin:0 0 .6rem 0; line-height:1.62; padding-left:.6rem;
        border-left:2px solid var(--acc-line); }
.detail { opacity:.70; font-size:.86rem; margin-top:.12rem; line-height:1.55; }
.pill { display:inline-block; padding:.13rem .6rem; margin:.15rem .3rem .15rem 0;
        border-radius:999px; font-size:.82rem;
        border:1px solid var(--line); background:var(--soft); }
.badge { display:inline-block; padding:.22rem .75rem; border-radius:999px;
         font-size:.85rem; font-weight:700; }
.summary { font-size:.98rem; line-height:1.6; opacity:.9; }

/* 岗位名片 */
.jobcard { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:.55rem .9rem; margin:.15rem 0 .55rem 0; }
.jobcard .cell { border-left:3px solid var(--acc); padding-left:.6rem; }
.jobcard .k { font-size:.70rem; opacity:.55; letter-spacing:.6px; margin-bottom:.1rem; }
.jobcard .v { font-size:.92rem; font-weight:700; line-height:1.45; word-break:break-word; }
.jobcard .v.big { font-size:1.05rem; }

/* 投递决策条 */
.decision { border-left:4px solid; border-radius:.55rem; padding:.55rem .85rem;
            margin:.6rem 0 .2rem 0; font-size:.90rem; line-height:1.55; }
.decision b { font-size:.98rem; }

/* 两批人的过关结论条 */
.verdict { border-left:4px solid; border-radius:.55rem; padding:.42rem .72rem;
           margin:.35rem 0 .5rem 0; font-size:.88rem; line-height:1.5; }

/* 入职后典型一天 / 筛选顺序 */
.tl { margin:.2rem 0 .3rem 0; }
.tl .row { display:flex; gap:.6rem; padding:.26rem 0; line-height:1.6; font-size:.88rem; }
.tl .n { flex:none; width:1.5rem; height:1.5rem; border-radius:50%; font-size:.72rem;
         display:flex; align-items:center; justify-content:center;
         background:var(--soft); color:var(--acc2); font-weight:700; }

.foot { margin-top:1.2rem; padding:.7rem .9rem; border-radius:.55rem;
        background:rgba(200,120,30,.10); border-left:4px solid #c8781e;
        font-size:.88rem; font-weight:700; }

/* 公司卡片（右栏窄） */
.card-name { font-weight:700; font-size:.94rem; line-height:1.35; margin-bottom:.15rem; }
.card-meta { font-size:.80rem; opacity:.72; line-height:1.6; }

/* 对比表 */
.cmp-wrap { overflow-x:auto; margin:.5rem 0 .2rem 0; border-radius:10px; }
table.cmp { border-collapse:collapse; width:100%; font-size:.85rem; }
table.cmp th, table.cmp td { border:1px solid var(--line);
        padding:.42rem .55rem; text-align:left; vertical-align:top; line-height:1.5; }
table.cmp thead th { background:var(--soft); font-weight:700; }
table.cmp tbody th { background:rgba(128,128,128,.07); font-weight:600; white-space:nowrap; }
table.cmp tr.hl td, table.cmp tr.hl th { background:rgba(190,140,30,.15); }
table.cmp .dim { opacity:.40; }

/* 分析原理：五步小标签 */
.steps { display:flex; flex-wrap:wrap; gap:.4rem; margin:.15rem 0 .1rem 0; }
.steps .st1 { display:inline-flex; align-items:center; gap:.35rem; font-size:.80rem;
              padding:.22rem .62rem; border-radius:999px; font-weight:600;
              color:var(--acc2);
              border:1px solid var(--acc-line); background:var(--soft); white-space:nowrap; }
.steps .st1 b { color:var(--acc2); font-weight:700; }
.steps .arrow { opacity:.35; align-self:center; font-size:.8rem; }

/* 真题统计条 */
.statrow { display:flex; flex-wrap:wrap; gap:.55rem; margin:.2rem 0 .6rem 0; }
.statrow .s { border:1px solid var(--line); border-left:3px solid var(--acc);
              background:var(--card); border-radius:11px;
              padding:.4rem .75rem; min-width:92px; }
.statrow .s .k { font-size:.70rem; opacity:.55; letter-spacing:.4px; }
.statrow .s .v { font-size:1.02rem; font-weight:700; color:var(--acc2); }

/* ---------- 面试题库：拆出来的问答预览 ---------- */
.iv-prev-box { border:1px solid var(--line); border-left:3px solid var(--acc);
  border-radius:12px; background:var(--card); padding:.55rem .8rem; margin:.15rem 0 .5rem; }
.iv-prev-head { font-size:.78rem; font-weight:800; opacity:.72; margin-bottom:.3rem; }
.iv-prev { font-size:.82rem; line-height:1.55; padding:.16rem 0; border-top:1px dashed var(--line); }
.iv-prev:first-of-type { border-top:none; }
.iv-prev b { color:var(--acc2); margin-right:.15rem; }
.iv-q { font-weight:750; font-size:.94rem; line-height:1.5; margin:.55rem 0 .1rem;
  padding-left:.55rem; border-left:3px solid var(--acc); }

/* ---------- 推荐岗位：可左右滑的卡片 ---------- */
.pool-links { display:flex; flex-wrap:wrap; gap:.55rem; margin:.15rem 0 .4rem; }
.pool-link { display:flex; flex-direction:column; gap:.1rem; text-decoration:none;
  padding:.5rem .85rem; border-radius:12px; border:1px solid var(--acc-line);
  background:var(--soft); transition:transform .16s ease, box-shadow .16s ease; }
.pool-link:hover { transform:translateY(-2px); box-shadow:0 8px 20px var(--soft); }
.pool-link .pl-name { font-weight:800; font-size:.86rem; color:var(--acc2); }
.pool-link .pl-kw { font-size:.7rem; opacity:.62; }
.pool-strip { display:flex; gap:.65rem; overflow-x:auto; padding:.25rem .15rem .8rem;
  scroll-snap-type:x mandatory; }
.pool-strip::-webkit-scrollbar { height:7px; }
.pool-card { flex:0 0 200px; scroll-snap-align:start; display:block; text-decoration:none;
  color:inherit; border:1px solid var(--line); border-left:3px solid var(--acc);
  border-radius:14px; background:var(--card); padding:.7rem .8rem .55rem;
  transition:transform .18s ease, box-shadow .18s ease, border-color .18s ease; }
.pool-card:hover { transform:translateY(-3px); box-shadow:0 10px 24px rgba(30,50,80,.13);
  border-color:var(--acc-line); border-left-color:var(--acc2); }
.pool-card .p-sticker { font-size:1.35rem; line-height:1; margin-bottom:.3rem; }
.pool-card .p-title { font-weight:800; font-size:.92rem; line-height:1.3; margin-bottom:.18rem; }
.pool-card .p-meta { font-size:.7rem; opacity:.6; line-height:1.5; min-height:1.9rem; }
.pool-card .p-score { font-size:.72rem; opacity:.75; margin-top:.28rem; }
.pool-card .p-score span { font-size:1.05rem; font-weight:800; color:var(--acc2); }
.pool-hero { display:flex; align-items:center; gap:.8rem; border:1px solid var(--acc-line);
  border-radius:16px; background:var(--card); padding:.75rem 1rem; margin:.2rem 0 .7rem; }
.pool-hero .ph-sticker { font-size:1.9rem; }
.pool-hero .ph-title { font-size:1.2rem; font-weight:800; }
.pool-hero .ph-meta { font-size:.78rem; opacity:.6; margin-top:.12rem; }

/* ---------- 自定义流程阶段：一排小标签 ---------- */
.stage-view { display:flex; flex-wrap:wrap; gap:.35rem; margin:.1rem 0 .55rem; }
.stage-chip { display:inline-flex; align-items:center; gap:.3rem; font-size:.76rem;
  font-weight:650; padding:.2rem .55rem .2rem .25rem; border-radius:999px;
  border:1px solid var(--acc-line); background:var(--soft); color:var(--acc2); }
.stage-chip b { display:inline-flex; align-items:center; justify-content:center;
  width:1.15rem; height:1.15rem; border-radius:50%; font-size:.66rem;
  background:var(--acc); color:#fff; }

/* ---------- 左侧导航窗格（把侧边栏当菜单用） ---------- */
.nav-brand { display:flex; align-items:center; gap:.6rem; padding:.1rem 0 .9rem; }
.nav-brand .logo { width:2.15rem; height:2.15rem; flex:none; border-radius:.7rem;
  display:flex; align-items:center; justify-content:center; font-size:1.05rem;
  background:linear-gradient(135deg,var(--acc),var(--acc2)); color:#fff;
  box-shadow:0 4px 14px var(--soft); }
.nav-brand .t { font-weight:800; font-size:.95rem; line-height:1.25; }
.nav-brand .s { font-size:.68rem; opacity:.5; letter-spacing:.5px; }
.nav-group { font-size:.66rem; letter-spacing:.16em; opacity:.42; font-weight:800;
  margin:.85rem 0 .25rem .15rem; }
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap:.10rem; }
section[data-testid="stSidebar"] .stButton > button {
  justify-content:flex-start; text-align:left; background:transparent;
  border:1px solid transparent; box-shadow:none; padding:.40rem .60rem; font-weight:600; }
section[data-testid="stSidebar"] .stButton > button p { width:100%; text-align:left; font-size:.89rem; }
section[data-testid="stSidebar"] .stButton > button:hover {
  background:var(--soft); transform:none; box-shadow:none; border-color:var(--acc-line); }
section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
  background:linear-gradient(135deg,var(--acc),var(--acc2)); border:none;
  box-shadow:0 5px 16px var(--soft); }
section[data-testid="stSidebar"] .stButton > button[kind="primary"] p { color:#fff; }

/* ---------- 指标卡 ---------- */
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(104px,1fr));
  gap:.5rem; margin:.15rem 0 .2rem; }
.kpi { border:1px solid var(--line); border-radius:14px; background:var(--card); padding:.55rem .7rem; }
.kpi .v { font-size:1.32rem; font-weight:800; line-height:1.15; color:var(--acc2); }
.kpi .k { font-size:.73rem; opacity:.6; margin-top:.08rem; }
.kpi .h { font-size:.67rem; opacity:.42; margin-top:.12rem; }
.kpi.hot { border-color:var(--acc-line); background:var(--soft); }

/* ---------- 日历 ---------- */
.cal-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:.3rem; }
.cal-title { font-weight:800; font-size:.95rem; letter-spacing:.3px; }
table.cal { width:100%; border-collapse:separate; border-spacing:2px; table-layout:fixed; }
table.cal th { font-size:.64rem; opacity:.42; font-weight:800; padding:0 0 .12rem; }
table.cal td.cal-cell { vertical-align:top; height:58px; border-radius:8px;
  border:1px solid rgba(128,128,128,.10); padding:2px 3px; }
table.cal td.cal-cell.void { border:none; background:transparent; }
table.cal td.cal-cell .d { font-size:.72rem; opacity:.6; line-height:1.1; }
table.cal td.cal-cell.has { border-color:var(--line); background:rgba(128,128,128,.055); }
table.cal td.cal-cell.today { background:var(--soft); border-color:var(--acc-line); }
table.cal td.cal-cell.today .d { color:var(--acc2); font-weight:800; opacity:1; }
.cal-ev { font-size:.58rem; line-height:1.3; margin-top:1px; padding:1px 3px; border-radius:4px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:700; }
.cal-ev.dl { background:rgba(200,120,30,.17); color:#9a5810; }
.cal-ev.iv { background:var(--soft); color:var(--acc2); }
.cal-ev.ap { background:rgba(47,138,114,.17); color:#1f6b57; }
.cal-ev.more { background:transparent; opacity:.5; font-weight:600; }
@media (prefers-color-scheme: dark) {
  .cal-ev.dl { color:#e8ad6c; } .cal-ev.ap { color:#72c8ae; }
}
.cal-leg { display:flex; flex-wrap:wrap; gap:.55rem; margin-top:.3rem; font-size:.66rem; opacity:.6; }
.cal-leg i { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:3px; }
.cal-leg i.dl { background:#c8781e; } .cal-leg i.iv { background:var(--acc); }
.cal-leg i.ap { background:#2f8a72; }
.cal-mini table.cal { border-spacing:1px; }
.cal-mini table.cal td.cal-cell { height:30px; text-align:center; }
.cal-mini table.cal td.cal-cell .d { font-size:.68rem; }
.cal-dots { display:flex; gap:2px; justify-content:center; margin-top:2px; height:4px; }
.cal-dots i { width:4px; height:4px; border-radius:50%; }
.cal-dots i.dl { background:#c8781e; } .cal-dots i.iv { background:var(--acc); }
.cal-dots i.ap { background:#2f8a72; }

/* ---------- 投递阶段进度条 ---------- */
.pipe { display:flex; margin:.65rem 0 .1rem; }
.pipe .pnode { flex:1; position:relative; text-align:center; font-size:.68rem;
  opacity:.45; padding-top:16px; white-space:nowrap; }
.pipe .pnode::before { content:""; position:absolute; top:6px; left:0; right:0;
  height:2px; background:rgba(128,128,128,.26); }
.pipe .pnode:first-child::before { left:50%; }
.pipe .pnode:last-child::before { right:50%; }
.pipe .pnode .dot { position:absolute; top:0; left:50%; transform:translateX(-50%);
  width:13px; height:13px; border-radius:50%; background:var(--card);
  border:2px solid rgba(128,128,128,.35); box-sizing:border-box; }
.pipe .pnode.done, .pipe .pnode.cur { opacity:1; font-weight:700; }
.pipe .pnode.done .dot { background:var(--acc); border-color:var(--acc); }
.pipe .pnode.done::before { background:var(--acc); }
.pipe .pnode.cur .dot { border-color:var(--acc); box-shadow:0 0 0 3px var(--soft); }
.pipe .pnode.cur { color:var(--acc2); }

/* ---------- 硬条件核查表 ---------- */
.hc { margin:.35rem 0 .2rem; }
.hc-head { display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; margin-bottom:.5rem; }
.hc-badge { font-size:.74rem; font-weight:700; padding:.22rem .58rem; border-radius:999px; }
.hc-badge.good { background:rgba(29,158,117,.14); color:#1D9E75; }
.hc-badge.bad { background:rgba(216,90,48,.14); color:#D85A30; }
.hc-badge.warn { background:rgba(186,117,23,.15); color:#BA7517; }
.hc-badge.muted { background:rgba(128,128,128,.12); opacity:.72; }
.hc-count { font-size:.7rem; opacity:.5; margin-left:auto; }
.hc-row { display:grid; grid-template-columns:20px 50px minmax(110px,.95fr) minmax(150px,1.45fr);
  gap:.55rem; align-items:baseline; padding:.42rem .6rem; border-radius:9px;
  margin-bottom:.3rem; border:1px solid var(--line); background:var(--card); font-size:.79rem; }
.hc-row .hc-mark { font-weight:800; text-align:center; }
.hc-row .hc-kind { font-size:.7rem; opacity:.55; }
.hc-row .hc-req { font-weight:600; }
.hc-row .hc-note { font-size:.72rem; opacity:.62; line-height:1.5; }
.hc-quote { display:block; font-size:.68rem; opacity:.5; margin-top:.16rem;
  padding-left:.5rem; border-left:2px solid rgba(128,128,128,.22); }
.hc-row.pass .hc-mark { color:#1D9E75; }
.hc-row.fail { border-color:rgba(216,90,48,.42); background:rgba(216,90,48,.05); }
.hc-row.fail .hc-mark { color:#D85A30; }
.hc-row.unknown .hc-mark { color:#BA7517; opacity:.85; }
.hc-must { font-size:.6rem; font-weight:600; padding:.04rem .3rem; border-radius:4px;
  background:rgba(128,128,128,.14); color:inherit; opacity:.55;
  margin-left:.32rem; vertical-align:middle; }
.hc-row.fail .hc-must { background:rgba(216,90,48,.16); color:#D85A30; opacity:1; }
.hc-empty { font-size:.74rem; opacity:.55; padding:.4rem 0; }

/* ---------- 投递漏斗 ---------- */
.fn { margin:.3rem 0 .1rem; }
.fn-row { display:grid; grid-template-columns:62px 1fr 32px 44px; gap:.5rem;
  align-items:center; margin-bottom:.34rem; font-size:.76rem; }
.fn-name { opacity:.72; white-space:nowrap; }
.fn-track { height:15px; border-radius:5px; background:rgba(128,128,128,.1); overflow:hidden; }
.fn-bar { display:block; height:100%; border-radius:5px; background:var(--acc); }
.fn-num { text-align:right; font-weight:800; }
.fn-step { text-align:right; font-size:.7rem; opacity:.5; }
.fn-foot { font-size:.7rem; opacity:.5; margin-top:.15rem; }
.fn-diag { margin-top:.6rem; padding:.5rem .68rem; border-radius:9px; font-size:.76rem;
  line-height:1.6; border:1px solid var(--line); background:rgba(128,128,128,.045); }
.fn-diag.bad { border-color:rgba(216,90,48,.36); background:rgba(216,90,48,.055); }
.fn-diag.warn { border-color:rgba(186,117,23,.36); background:rgba(186,117,23,.055); }
.fn-diag.good { border-color:rgba(29,158,117,.36); background:rgba(29,158,117,.055); }
.fn-empty { font-size:.74rem; opacity:.55; line-height:1.6; }

/* ---------- 输入清洗提示 ---------- */
.cl-note { font-size:.72rem; opacity:.6; padding:.3rem .1rem; line-height:1.6; }
.cl-note.ok { opacity:.45; }

/* ---------- 岗位行 / 标签 ---------- */
.jrow { border:1px solid var(--line); border-left:3px solid var(--acc-line);
  border-radius:14px; background:var(--card);
  padding:.65rem .85rem .5rem; margin-bottom:.5rem; }
.jrow .top, .jhead { display:flex; flex-wrap:wrap; align-items:baseline; gap:.45rem; }
.jrow .co, .jhead .co { font-weight:800; font-size:.96rem; max-width:58%; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.jrow .ti, .jhead .ti { font-size:.86rem; opacity:.78; max-width:46%; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.jrow .meta, .jhead .meta { font-size:.73rem; opacity:.52; margin-left:auto; }
.tag { display:inline-block; font-size:.67rem; font-weight:700; padding:.08rem .45rem;
  border-radius:999px; border:1px solid var(--acc-line); color:var(--acc2);
  background:var(--soft); white-space:nowrap; }
.tag.warn { border-color:rgba(200,120,30,.45); color:#9a5810; background:rgba(200,120,30,.14); }
.tag.ok { border-color:rgba(47,138,114,.45); color:#1f6b57; background:rgba(47,138,114,.14); }
.tag.plain { border-color:var(--line); color:inherit; background:rgba(128,128,128,.08); opacity:.8; }
.tag.money { border-color:var(--acc-line); color:var(--acc2); background:var(--soft);
  font-variant-numeric:tabular-nums; }
.tag.star { border-color:rgba(190,150,40,.45); color:#8a6a12; background:rgba(190,150,40,.15); }
@media (prefers-color-scheme: dark) {
  .tag.warn { color:#e8ad6c; } .tag.ok { color:#72c8ae; }
}
.empty-hint { border:1px dashed var(--acc-line); border-radius:14px; padding:1rem 1.1rem;
  font-size:.86rem; opacity:.75; line-height:1.7; background:var(--card); }

/* ---------- 首页：顶部状态条（代替大标语） ---------- */
.tbar { display:flex; flex-wrap:wrap; align-items:center; gap:1rem;
  border:1px solid var(--line); border-radius:18px; background:var(--card);
  padding:.85rem 1.15rem; margin-bottom:.7rem;
  box-shadow:0 4px 18px rgba(30,50,80,.06); }
.tbar-main { flex:1 1 320px; min-width:240px; }
.tbar-date { font-size:.71rem; letter-spacing:.14em; font-weight:800; opacity:.5; }
.tbar-say { font-size:1.0rem; font-weight:700; margin-top:.24rem; line-height:1.5; }
.tbar-figs { display:flex; gap:1.25rem; }
.tbar-figs div { text-align:center; }
.tbar-figs b { display:block; font-size:1.26rem; font-weight:800; color:var(--acc2); line-height:1.1;
  font-variant-numeric:tabular-nums; }
.tbar-figs span { font-size:.67rem; opacity:.55; }

/* ---------- 首页：未来七天 ---------- */
.wstrip { display:flex; gap:.4rem; margin-bottom:.2rem; }
.wd { flex:1; min-width:0; border:1px solid var(--line); border-radius:12px;
  background:var(--card); padding:.4rem .45rem .5rem;
  transition:border-color .18s ease, transform .18s ease, box-shadow .18s ease; }
.wd:hover { transform:translateY(-2px); box-shadow:0 6px 18px rgba(30,50,80,.10); }
.wd.has { border-color:var(--acc-line); }
.wd.today { border-color:var(--acc); background:var(--soft); }
.wd-h { display:flex; align-items:baseline; gap:.28rem; margin-bottom:.28rem; }
.wd-h b { font-size:1rem; font-weight:800; }
.wd-h span { font-size:.61rem; opacity:.55; }
.wc { font-size:.63rem; line-height:1.4; padding:.08rem .3rem; border-radius:5px;
  margin-bottom:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.wc.none { opacity:.3; }
.wc.more { opacity:.55; font-weight:700; }
.wc.dl, .cal-ev.dl { background:rgba(200,120,30,.17); color:#9a5810; }
.wc.iv, .cal-ev.iv { background:var(--soft); color:var(--acc2); }
.wc.ap, .cal-ev.ap { background:rgba(47,138,114,.17); color:#1f6b57; }
.wc.ex, .cal-ev.ex { background:rgba(120,90,190,.16); color:#5a3fa0; }
.wc.tk, .cal-ev.tk { background:rgba(190,150,40,.18); color:#8a6a12; }
.wc.td, .cal-ev.td { background:rgba(90,120,160,.16); color:#3c5a80; }
.wc.my, .cal-ev.my { background:rgba(128,128,128,.14); color:inherit; }

/* ---------- 首页：阶段分布 ---------- */
.sbar { display:flex; height:14px; border-radius:999px; overflow:hidden;
  background:rgba(128,128,128,.10); margin:.2rem 0 .6rem; }
.sbar i { display:block; height:100%; background:var(--acc); }
.sgrid { display:grid; grid-template-columns:repeat(8,1fr); gap:.3rem; }
.sd { text-align:center; border:1px solid var(--line); border-radius:9px; padding:.28rem .1rem;
  background:var(--card); }
.sd b { display:block; font-size:.95rem; font-weight:800; color:var(--acc2);
  font-variant-numeric:tabular-nums; }
.sd span { font-size:.61rem; opacity:.6; }
.sd.zero { opacity:.38; }

/* ---------- 批量操作条 ---------- */
.bulk-head { display:flex; align-items:center; gap:.55rem; font-weight:700; font-size:.92rem; }
.bulk-hint { font-weight:400; font-size:.7rem; opacity:.5; margin-left:auto; }
.ev-note { display:block; font-size:.68rem; opacity:.55; line-height:1.4; }

/* 日历新增类型的颜色 */
.cal-leg i.ex { background:#785abE; } .cal-leg i.tk { background:#be9628; }
.cal-leg i.td { background:#5a78a0; } .cal-leg i.my { background:#8a8a8a; }
.cal-dots i.ex { background:#785abE; } .cal-dots i.tk { background:#be9628; }
.cal-dots i.td { background:#5a78a0; } .cal-dots i.my { background:#8a8a8a; }

/* ---------- 鼠标：能点的地方要认得出来 ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button,
[data-testid="stExpander"] summary,
[data-testid="stCheckbox"] label, [data-testid="stRadio"] label,
[data-testid="stSelectbox"] [data-baseweb="select"],
[data-testid="stFileUploader"] label, [data-testid="stTabs"] button,
[data-testid="stSidebar"] button, a, .wd, .jrow,
table.cal td.cal-cell { cursor:pointer; }

/* hover 时整块抬起来 + 主色描边：鼠标划到哪，哪里就亮 */
.stButton > button:hover, .stDownloadButton > button:hover,
.stFormSubmitButton > button:hover { border-color:var(--acc-line) !important; }
.stButton > button:active, .stDownloadButton > button:active,
.stFormSubmitButton > button:active { transform:translateY(0) scale(.98); }

[data-testid="stVerticalBlockBorderWrapper"] > div { transition:border-color .18s ease,
  box-shadow .2s ease, transform .2s ease; }
[data-testid="stVerticalBlockBorderWrapper"]:hover > div {
  border-color:var(--acc-line) !important;
  box-shadow:0 8px 26px rgba(30,50,80,.12);
}

/* 岗位行、日历格子这类信息块，鼠标扫过去也有回应 */
.jrow { transition:border-color .18s ease, box-shadow .18s ease, transform .18s ease,
  border-left-color .18s ease; }
.jrow:hover { border-color:var(--acc-line); border-left-color:var(--acc);
  transform:translateY(-1px); box-shadow:0 6px 20px rgba(30,50,80,.10); }
table.cal td.cal-cell { transition:background .15s ease, border-color .15s ease; }
table.cal td.cal-cell:hover { background:var(--soft); border-color:var(--acc-line); }

/* 输入类控件 hover 也提前给个边框提示，别等点下去才变 */
[data-baseweb="select"]:hover > div,
.stTextInput input:hover, .stTextArea textarea:hover,
.stNumberInput input:hover, [data-testid="stDateInput"] input:hover {
  border-color:var(--acc-line) !important;
}
[data-testid="stCheckbox"] label:hover span,
[data-testid="stRadio"] label:hover { color:var(--acc2); }

/* 键盘也能看出来焦点在哪 */
button:focus-visible, a:focus-visible { outline:2px solid var(--acc); outline-offset:2px; }

@media (prefers-color-scheme: dark) {
  .wc.dl, .cal-ev.dl { color:#e8ad6c; }
  .wc.ap, .cal-ev.ap { color:#72c8ae; }
  .wc.ex, .cal-ev.ex { color:#b6a2e8; }
  .wc.tk, .cal-ev.tk { color:#e2c46a; }
  .wc.td, .cal-ev.td { color:#9cb6d8; }
  .tag.money { color:var(--acc2); }
}

/* 滚动条 */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--soft); border-radius: 8px; }
::-webkit-scrollbar-thumb:hover { background: var(--acc); }
</style>
"""


def _rgb(hex_color: str) -> tuple[int, int, int] | None:
    """#4a7fb5 → (74,127,181)。不是标准六位色就返回 None。"""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(hex_color or "").strip())
    if not m:
        return None
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _shade(hex_color: str, factor: float) -> str:
    """同色系加深 / 变浅，用来从一个主色推出深色标题色。"""
    rgb = _rgb(hex_color)
    if not rgb:
        return hex_color
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, int(c * factor))) for c in rgb
    )


def _alpha(hex_color: str, alpha: float) -> str:
    rgb = _rgb(hex_color)
    if not rgb:
        return "rgba(0,0,0,0)"
    return f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{alpha})"


def build_css(theme: str | None = None, accent: str = "") -> str:
    """按当前主题生成 CSS。主题名对不上就退回默认，不会报错。

    accent 是你自己挑的主色：给了就用它推整套配色（深色 + 半透明底），
    这样"我想要这个绿"不会被五个预设卡住。
    """
    conf = dict(THEMES.get(theme or "", THEMES[DEFAULT_THEME]))
    rgb = _rgb(accent)
    if rgb:
        base = "#%02x%02x%02x" % rgb
        conf["acc"] = base
        conf["acc2"] = _shade(base, 0.60)
        conf["soft"] = _alpha(base, 0.13)
        conf["line"] = _alpha(base, 0.38)
    return (
        CSS_TEMPLATE.replace("__ACC__", conf["acc"])
        .replace("__ACC2__", conf["acc2"])
        .replace("__SOFT__", conf["soft"])
        .replace("__LINE__", conf["line"])
    )


CSS = build_css()


def section_header(num: int, title: str, hint: str = "") -> None:
    """带序号色块的板块标题。"""
    st.markdown(
        f'<div class="sec"><span class="sec-num">{num}</span><span>{html.escape(title)}</span></div>',
        unsafe_allow_html=True,
    )
    if hint:
        st.caption(hint)


def sub_header(text: str) -> None:
    st.markdown(f'<div class="sub">{html.escape(text)}</div>', unsafe_allow_html=True)


def render_items(items: list[tuple[str, str]], empty_text: str = "（模型未返回该维度内容）") -> None:
    """渲染 (标题, 说明) 列表；全部经过 html.escape，杜绝模型输出注入 HTML。"""
    if not items:
        st.caption(empty_text)
        return
    blocks = []
    for title, detail in items:
        block = f'<div class="item"><b>{html.escape(title)}</b>'
        if detail:
            block += f'<div class="detail">{html.escape(detail)}</div>'
        block += "</div>"
        blocks.append(block)
    st.markdown("".join(blocks), unsafe_allow_html=True)


def render_keywords(words: list[str]) -> None:
    if not words:
        st.caption("（未返回关键词）")
        return
    pills = "".join(f'<span class="pill">{html.escape(w)}</span>' for w in words)
    st.markdown(pills, unsafe_allow_html=True)


VERDICT_STYLE = {
    "高": ("rgba(27,127,90,.14)", "#1b7f5a"),
    "中": ("rgba(190,140,30,.16)", "#8a6412"),
    "低": ("rgba(180,60,60,.14)", "#a33"),
}

# 投递决策三档：颜色即结论，不用读字也知道该不该投
DECISION_STYLE = {
    "建议投": ("rgba(27,127,90,.13)", "#1b7f5a"),
    "谨慎投": ("rgba(190,140,30,.15)", "#8a6412"),
    "别浪费简历": ("rgba(180,60,60,.13)", "#a33"),
}


def render_timeline(items: list[str]) -> None:
    """入职后典型一天：带序号的时间线，比一串无标题段落好读。"""
    rows = "".join(
        f'<div class="row"><div class="n">{idx}</div><div>{html.escape(text)}</div></div>'
        for idx, text in enumerate(items, start=1)
    )
    st.markdown(f'<div class="tl">{rows}</div>', unsafe_allow_html=True)


def render_verdict(text: str, label: str) -> None:
    """两批人各自的过关结论：能过绿、悬黄、过不了红。"""
    text = (text or "").strip()
    if not text:
        return
    bg, fg = verdict_tone(text)
    st.markdown(
        f'<div class="verdict" style="background:{bg};border-left-color:{fg};">'
        f'<span style="color:{fg};font-weight:700;">{html.escape(label)}：</span>'
        f'<span style="opacity:.92;">{html.escape(text)}</span></div>',
        unsafe_allow_html=True,
    )


def render_rewrites(items: list[tuple[str, str]]) -> None:
    """简历逐条改写：原句在上、改法在下，视觉上区分得开。"""
    if not items:
        st.caption("未上传简历，这一节跳过。上传简历后可获得逐句改写建议。")
        return
    blocks = []
    for original, advice in items:
        block = (
            '<div class="item">'
            '<span style="opacity:.55;font-size:.78rem;letter-spacing:.5px;">原文</span><br>'
            f"<b>{html.escape(original)}</b>"
        )
        if advice:
            block += f'<div class="detail">→ {html.escape(advice)}</div>'
        block += "</div>"
        blocks.append(block)
    st.markdown("".join(blocks), unsafe_allow_html=True)


def render_job_card(result: dict, meta: dict, title_override: str = "", company_override: str = "") -> None:
    """岗位名片：岗位名称 / 公司 / 身份 / 时间 各占一格，不挤成一串。

    岗位名称优先用用户在输入框里标注的值（input 已随 meta 快照存下），
    其次用模型从 JD 原文里取到的值。
    """
    snap = meta.get("inputs") or {}
    title = (title_override or str(result.get("job_title") or "")).strip()
    company = (company_override or str(result.get("company_name") or "")).strip()
    identity = str(meta.get("identity") or snap.get("identity") or "").strip()
    education = str(meta.get("education") or snap.get("education") or "").strip()
    school_tier = str(meta.get("school_tier") or snap.get("school_tier") or "").strip()
    edu_text = education or "未填"
    if school_tier and school_tier != "不填":
        edu_text = f"{edu_text} · {school_tier}"
    # 薪资：你手填的 > 模型从 JD 里抽的
    salary = str(meta.get("salary") or "").strip() or str(result.get("salary") or "").strip()
    role_summary = str(result.get("role_summary") or "").strip()
    match = result.get("match") or {}
    reason = str(match.get("verdict_reason") or "").strip()
    level = str(match.get("verdict_level") or "").strip()[:1] or "中"
    bg, fg = VERDICT_STYLE.get(level, VERDICT_STYLE["中"])

    cells = [
        ("岗位名称", title or "（JD 未写，也没标注）", True),
        ("公司", company or "（未写）", False),
        ("薪资待遇", salary or "（未写）", False),
        ("我的身份", identity or "未设置", False),
        ("学历 / 院校", edu_text, False),
        ("分析时间", str(meta.get("time") or "-"), False),
    ]
    grid = "".join(
        f'<div class="cell"><div class="k">{html.escape(k)}</div>'
        f'<div class="v{" big" if big else ""}">{html.escape(v)}</div></div>'
        for k, v, big in cells
    )

    with st.container(border=True):
        left, right = st.columns([2.4, 1])
        with left:
            st.markdown(f'<div class="jobcard">{grid}</div>', unsafe_allow_html=True)
            if role_summary:
                st.markdown(
                    f'<div class="summary"><b>岗位画像：</b>{html.escape(role_summary)}</div>',
                    unsafe_allow_html=True,
                )
            if reason:
                st.markdown(
                    f'<div class="detail">判断依据：{html.escape(reason)}</div>',
                    unsafe_allow_html=True,
                )
        with right:
            # 五维得分雷达图。模型没给 scores 时这里留空，不显示半张图
            svg = intel.radar_svg(result.get("scores"))
            if svg:
                st.markdown(
                    f'<div style="display:flex;justify-content:center;">{svg}</div>',
                    unsafe_allow_html=True,
                )
            st.markdown(
                f'<div style="text-align:center;"><span class="badge" '
                f'style="background:{bg};color:{fg};">整体匹配度 · {html.escape(level)}</span></div>',
                unsafe_allow_html=True,
            )
        # 投递决策条：把"分数"翻译成"到底投不投"
        decision = result.get("decision") or {}
        verdict = str(decision.get("verdict") or "").strip()
        if verdict:
            key = next((k for k in DECISION_STYLE if verdict.startswith(k[:2])), "谨慎投")
            d_bg, d_fg = DECISION_STYLE[key]
            body = f"<b>{html.escape(verdict)}</b>"
            d_reason = str(decision.get("reason") or "").strip()
            if d_reason:
                body += f"　{html.escape(d_reason)}"
            d_next = str(decision.get("next_step") or "").strip()
            if d_next:
                body += f'<div class="detail">下一步：{html.escape(d_next)}</div>'
            st.markdown(
                f'<div class="decision" style="background:{d_bg};border-color:{d_fg};'
                f'color:{d_fg};">{body}</div>',
                unsafe_allow_html=True,
            )

        st.caption(
            f"输入快照 #{meta.get('bg_version', '-')}"
            "（背景/偏好/身份/学历/简历已快照保存，之后修改侧边栏不影响这份结果）"
        )


def render_how_it_works() -> None:
    """一行五步 + 折叠细节。占位两行，不点开不挡视线。"""
    steps = [
        ("①", "读材料", "身份·学历·背景·偏好·简历·JD·你的真题"),
        ("②", "拆筛子", "公司侧 + HR 侧两批人，各一套标准"),
        ("③", "对号入座", "五维打分 → 投 / 谨慎投 / 别投"),
        ("④", "改简历", "逐句对照，只改不编"),
        ("⑤", "押题", "5 道，优先覆盖你记过的真题"),
    ]
    pills = '<span class="arrow">→</span>'.join(
        f'<span class="st1"><b>{n}</b>{name}</span>' for n, name, _ in steps
    )
    st.markdown(f'<div class="steps">{pills}</div>', unsafe_allow_html=True)

    with st.expander("🔍 这套分析是怎么跑出来的（点开看细节）"):
        st.markdown(
            "**1. 读材料**　身份、最高学历与院校层次、个人背景、求职偏好与性格、"
            "简历原文、这份 JD、以及你自己记过的面试真题 —— 全部在本机拼成一段请求。"
            "开了脱敏的话，姓名/手机/邮箱/身份证/院校/公司全称在发出去之前已经换成 `[P1]`。"
        )
        st.markdown(
            "**2. 拆筛子（核心）**　筛你的不是一个「招聘方」，是两批人：\n\n"
            "- **公司侧**（用人部门 / 直属领导 / 业务面官）看的是「这人来了能不能干活」——"
            "具体技能、行业经验、扛不扛得住日常、和团队搭不搭。他们不看你学历是不是 985，"
            "但会在业务面用具体问题把你问穿。\n"
            "- **HR 侧**（招聘 HR / 简历初筛 / 系统筛选）看的是「合不合规、好不好推进、"
            "会不会白忙」——学历层次、专业、年限、到岗时间、期望薪资是否在区间内、"
            "稳定性（跳槽频率 / 空窗期）、简历关键词能不能被搜到。\n\n"
            "两批人的结论经常不一致：HR 过了但业务面挂，或者业务想要但 HR 卡学历。"
            "所以结果是分开给的，还额外排了一个「你会先被谁筛」的顺序 —— 死在第几步，"
            "比「匹配度 60 分」有用得多。"
        )
        st.markdown(
            "**3. 对号入座**　五维打分（硬性门槛 / 技能 / 经验 / 地点与节奏 / 成长空间），"
            "每项 1-5 分。**这不是公式算出来的，是模型照着你写下的材料判断的**，"
            "所以材料写得越具体，分数越可信；没写的经历它不许猜，只会写「材料中未体现」。"
            "分数只说明匹配程度，投不投由旁边那条决策结论回答。"
        )
        st.markdown(
            "**4. 改简历**　只对着你简历里的原句改，引用原句 → 给改法 → 说为什么。"
            "缺数据它会指出来让你补，不会替你编一段实习。"
        )
        st.markdown(
            "**5. 押题**　按 JD + 你的短板出 5 道。如果你在「面试真题」里记过这家公司或同类岗位的题，"
            "会优先覆盖那些题并标「（真题）」。"
        )
        st.markdown(
            "**它会联网做什么**　只做一件：你把招聘页链接贴进来、点了抓取，它才去读那一页 —— "
            "而且先查对方的 `robots.txt`，对方不允许就停手；天眼查/企查查这类条款禁止、已有判例的平台直接不碰；"
            "需要登录才看得到正文的招聘站会明确告诉你抓不到，让你手动复制。"
            "抓回来的文字先摊给你看，你点「填入」才进输入框。"
        )
        st.markdown(
            "**它不做什么**　不读你没上传的文件、不发邮件、不自动投递、不翻页批量抓、"
            "不登录、不绕验证码、不伪装成浏览器；"
            "除了你点分析时把材料发给你自己配置的模型服务商、以及你点抓取时读那一页，没有别处外发动作。"
        )


def render_interview_log(company: str, title: str, job_id: str = "") -> None:
    """面完之后就地录题：不用切页签，填完入库，下次分析自动带上。"""
    data = st.session_state.get("data") or store.load()
    matched = store.matched_questions(data, company, title)
    ver = int(st.session_state.get("quick_iv_ver", 0))

    with st.expander(
        f"📝 面完了？把这场的题记进来（已有 {len(matched)} 道同类真题会被优先覆盖）"
        if matched
        else "📝 面完了？把这场的题记进来（下次分析同类岗位会优先覆盖这些题）"
    ):
        st.caption(
            "一行一道题，尽量记原话。存进本机真题库，只在你下次点分析时随材料发出去一次。"
        )
        # 预填公司/岗位，省得手打；用 setdefault 而不是 value，避免控件状态互相覆盖
        st.session_state.setdefault(f"qiv_company_{ver}", company)
        st.session_state.setdefault(f"qiv_title_{ver}", title)

        with st.form(f"quick_iv_form_{ver}"):
            c1, c2, c3, c4 = st.columns([1.4, 1.4, 1, 1])
            q_company = c1.text_input("公司", key=f"qiv_company_{ver}")
            q_title = c2.text_input("岗位", key=f"qiv_title_{ver}")
            q_round = c3.selectbox(
                "轮次", ["一面", "二面", "三面", "HR 面", "终面", "群面", "笔试"],
                key=f"qiv_round_{ver}",
            )
            q_outcome = c4.selectbox(
                "结果", ["还没出", "进下一轮", "挂了", "拿到 offer"], key=f"qiv_outcome_{ver}"
            )
            q_text = st.text_area(
                "被问到的题（一行一道）",
                height=130,
                key=f"qiv_q_{ver}",
                placeholder="自我介绍\n为什么从会计转成本核算？\n你在实习里具体做过哪张表？\n期望薪资多少？",
            )
            q_feel = st.text_input(
                "哪道题卡住了（可留空）", key=f"qiv_feel_{ver}",
                placeholder="比如：存货跌价准备答得不清楚",
            )
            submitted = st.form_submit_button("保存到真题库", type="primary")

        if submitted:
            if not (q_text or "").strip():
                st.error("至少记一道题。")
            else:
                rec = store.add_interview(
                    data,
                    {
                        "company": q_company,
                        "title": q_title,
                        "round": q_round,
                        "outcome": q_outcome,
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "questions": q_text,
                        "feeling": q_feel,
                        "job_id": job_id,
                    },
                )
                persist_data()
                st.session_state["quick_iv_ver"] = ver + 1
                flash(
                    f"已记下 {len(rec['questions'])} 道题。下次分析这家公司或同类岗位时，"
                    "预测会优先覆盖它们并标「真题」。"
                )
                st.rerun()


def render_result(
    result: dict,
    meta: dict,
    quiz_mode: bool = True,
    title_override: str = "",
    company_override: str = "",
) -> None:
    """按四个板块渲染这份 JD 的分析结果。

    版式：顶部「岗位名片」→ ① JD 拆解（整宽，两批人两套筛法）
    → ② 匹配评估 → ③ 简历建议 → ④ 面试预测。
    「适配岗位画像」不在这里 —— 它是"我这个人的方向"，与手里这份 JD 无关，
    已经移到左侧求职设置里，改完背景点一下就能重算，不用重新贴 JD。
    quiz_mode=True 时，面试题折叠成卡片：先只给题目，自己答完再展开对照。
    """
    breakdown = result.get("jd_breakdown") or {}
    match = result.get("match") or {}
    resume = result.get("resume") or {}
    interview = normalize_interview(result.get("interview"))

    # ---------- 岗位名片 ----------
    render_job_card(result, meta, title_override, company_override)

    st.write("")

    # ---------- ① JD 拆解：两批人，两套筛法 ----------
    with st.container(border=True):
        section_header(
            1,
            "JD 拆解 · 两批人两套筛法",
            "公司侧看你能不能干活，HR 侧看你合不合规——现实中常常一个过一个不过",
        )
        bd = normalize_breakdown(breakdown)
        if bd["legacy"]:
            st.caption("⚠️ 这是旧版结果（当时还没拆两批人），重新分析一次可以得到完整判断。")

        jd_clean = meta.get("jd_clean") or {}
        if jd_clean.get("deleted"):
            st.caption(
                f"✂️ 送进模型前已剔除 {jd_clean['deleted']} 行噪声"
                f"（福利话术 / 联系方式 / 重复行），"
                f"{jd_clean.get('before', 0)} 字 → {jd_clean.get('after', 0)} 字。"
                "原文没存改动记录，想核对可以拿原始 JD 比一遍。"
            )

        day = [str(x).strip() for x in (result.get("day_in_life") or []) if str(x).strip()]
        if day:
            sub_header("🕘 入职后典型一天")
            st.caption("⚠️ 按岗位类型推的典型节奏，不是你入职后的实际安排。")
            render_timeline(day[:3])
            st.divider()

        if bd["order"]:
            sub_header("🚦 你会先被谁筛（顺序决定你死在第几步）")
            st.caption("⚠️ 从 JD 的段落顺序和措辞反推的，不同公司实际流程有差异。")
            render_timeline(bd["order"][:4])
            st.divider()

        # ── 第一批：公司侧 ──
        sub_header("🏢 公司侧 · 用人部门 / 直属领导 / 业务面官")
        render_verdict(bd["company_verdict"], "公司这关")
        st.caption("不会就是不会：技能、行业经验、能不能独立出活")
        render_items(bd["company_must"], "（未返回公司侧硬要求）")
        st.write("")
        st.caption("JD 没写全、但报到第一周就会暴露的期待")
        render_items(bd["company_expect"], "（未返回公司侧潜在期待）")

        st.divider()

        # ── 第二批：HR 侧 ──
        sub_header("🧑‍💼 HR 侧 · 招聘 HR / 简历初筛 / 系统筛选")
        render_verdict(bd["hr_verdict"], "HR 这关")
        st.caption("不符合规则就是不符合规则：学历、年限、到岗时间、薪资区间、关键词")
        render_items(bd["hr_must"], "（未返回 HR 侧硬指标）")
        st.write("")
        st.caption("HR 会重点核的软性点（稳定性 / 动机 / 空窗期 / 离职原因）")
        render_items(bd["hr_soft"], "（未返回 HR 侧软性关注点）")

        st.divider()
        sub_header("🕵️ 两批人都没说、但入职后会暴露的隐性要求")
        st.caption("⚠️ 全部是从 JD 措辞反推的，不是明文条款，当真之前先找人问一句。")
        render_items(bd["implicit"])

    st.write("")

    # ---------- ③ 个人匹配评估 ----------
    with st.container(border=True):
        identity = str(
            meta.get("identity") or (meta.get("inputs") or {}).get("identity") or ""
        ).strip()
        section_header(
            2,
            "个人匹配评估",
            (f"按「{identity}」的校招/社招口径评估；" if identity else "")
            + "只对照你写下的材料，材料里没有的经历不会替你编",
        )
        # 硬条件核查放在最前面：先看会不会被机器直接筛掉，再看软性匹配
        conditions = result.get("hard_conditions") or []
        hc_rows, hc_sum = scoring.judge(conditions, meta.get("inputs") or {})
        if hc_rows:
            sub_header("🧱 硬条件核查（逐条过，不看感觉）")
            st.markdown(scoring.render(hc_rows, hc_sum), unsafe_allow_html=True)
            st.caption(
                "判定全在本地做：模型只负责从 JD 里抄出条件，"
                "过不过由规则比对，所以同一份 JD 每次结论都一样。"
            )
            st.divider()

        left, right = st.columns(2)
        with left:
            sub_header("✅ 优势匹配点")
            render_items(normalize_items(match.get("strengths")))
        with right:
            sub_header("⚠️ 短板 / 风险点")
            render_items(normalize_items(match.get("gaps")))

    st.write("")

    # ---------- ③ 简历修改建议 ----------
    with st.container(border=True):
        section_header(
            3,
            "简历修改建议",
            "逐条对着简历原句改；没提供的简历不会替你编",
        )
        sub_header("✍️ 逐条改写（原句 → 改法 → 为什么）")
        render_rewrites(normalize_items(resume.get("rewrites")))
        st.divider()
        sub_header("🔀 经历排序")
        render_items(normalize_items(resume.get("reorder")))
        st.divider()
        sub_header("🔑 关键词补充")
        render_keywords(normalize_keywords(resume.get("keywords")))
        st.divider()
        sub_header("✂️ 需要压缩或弱化的内容")
        render_items(normalize_items(resume.get("compress")))

    st.write("")

    # ---------- ④ 高频面试预测 ----------
    with st.container(border=True):
        section_header(4, "高频面试预测", "5 道高概率题目 + 答题思路 + 易踩的坑")
        if any("真题" in q for q, _, _ in interview):
            st.caption("标「（真题）」的是你自己记录过、确实被问到的题，优先级最高。")
        if not interview:
            st.caption("（模型未返回面试题）")
        elif quiz_mode:
            # 自测模式：题目在外，答案折在里面。先自己答，再点开对照
            st.caption("自测模式：先在心里答一遍，再展开对照思路。想直接看答案就把上面的开关关掉。")
            for idx, (question, approach, pitfall) in enumerate(interview[:5], start=1):
                with st.expander(f"Q{idx}. {question}", expanded=False):
                    if approach:
                        st.markdown(f"**答题思路：** {approach}")
                    if pitfall:
                        st.markdown(f"**易踩的坑：** {pitfall}")
        else:
            for idx, (question, approach, pitfall) in enumerate(interview[:5], start=1):
                st.markdown(f"**Q{idx}. {question}**")
                if approach:
                    st.markdown(f"　**答题思路：** {approach}")
                if pitfall:
                    st.markdown(f"　**易踩的坑：** {pitfall}")
                if idx != min(len(interview), 5):
                    st.divider()

        # 面试回来的入口就放在这一块底部：刚面完的人一定是停在这里的
        st.divider()
        render_interview_log(
            str(company_override or meta.get("company") or result.get("company_name") or ""),
            str(title_override or meta.get("title") or result.get("job_title") or ""),
            job_id=str(meta.get("job_id") or ""),
        )

    # ---------- 强制结尾标注 ----------
    st.markdown(f'<div class="foot">⚠️ {html.escape(SAFETY_FOOTER)}</div>', unsafe_allow_html=True)


def build_markdown(result: dict, meta: dict) -> str:
    """把结构化结果导出成 Markdown 文本（供本地下载，末尾同样带强制标注）。"""
    fit = result.get("fit_roles") or {}
    breakdown = result.get("jd_breakdown") or {}
    match = result.get("match") or {}
    resume = result.get("resume") or {}

    def lines(items: list[tuple[str, str]]) -> list[str]:
        return [f"- **{t}**　{d}" if d else f"- {t}" for t, d in items] or ["- （无）"]

    scores = result.get("scores") or {}
    score_lines = [
        f"- {label}：{scores.get(key)} / 5"
        for key, label in intel.RADAR_DIMS
        if scores.get(key) not in (None, "")
    ]

    day = [str(x).strip() for x in (result.get("day_in_life") or []) if str(x).strip()]
    day_lines = [f"- {d}" for d in day] or ["- （模型未返回）"]

    bd = normalize_breakdown(breakdown)

    out = [
        "# 岗位JD智能分析报告",
        "",
        f"- 分析时间：{meta.get('time', '-')}",
        f"- 岗位名称：{meta.get('title') or result.get('job_title') or '-'}",
        f"- 公司：{meta.get('company') or result.get('company_name') or '-'}",
        f"- 我的身份：{meta.get('identity') or (meta.get('inputs') or {}).get('identity') or '-'}",
        f"- 学历 / 院校：{meta.get('education') or '-'} / {meta.get('school_tier') or '不填'}",
        f"- 岗位画像：{result.get('role_summary', '-')}",
        f"- 投递建议：{(result.get('decision') or {}).get('verdict', '-')}"
        f"（{(result.get('decision') or {}).get('reason', '-')}）",
        f"- 整体匹配度：{match.get('verdict_level', '-')}",
        *score_lines,
        "",
        "## 一、JD 拆解（两批人两套筛法）",
        "",
        "### 入职后典型一天",
        *day_lines,
        "",
        "### 筛选顺序",
        *([f"- {x}" for x in bd["order"]] if bd["order"] else ["- （未返回）"]),
        "",
        "### 公司侧（用人部门 / 直属领导 / 业务面官）",
        f"**结论：** {bd['company_verdict'] or '（未返回）'}",
        "",
        "#### 不会就是不会",
        *lines(bd["company_must"]),
        "",
        "#### JD 没写全的期待",
        *lines(bd["company_expect"]),
        "",
        "### HR 侧（招聘 HR / 简历初筛 / 系统筛选）",
        f"**结论：** {bd['hr_verdict'] or '（未返回）'}",
        "",
        "#### 会卡的硬指标",
        *lines(bd["hr_must"]),
        "",
        "#### 会重点核的软性点",
        *lines(bd["hr_soft"]),
        "",
        "### 两批人都没说、入职后会暴露的要求",
        *lines(bd["implicit"]),
        "",
        "## 二、个人匹配评估",
        "",
        "### 优势匹配点",
        *lines(normalize_items(match.get("strengths"))),
        "",
        "### 短板 / 风险点",
        *lines(normalize_items(match.get("gaps"))),
        "",
        "## 三、简历修改建议",
        "",
        "### 逐条改写",
        *lines(normalize_items(resume.get("rewrites"))),
        "",
        "### 经历排序",
        *lines(normalize_items(resume.get("reorder"))),
        "",
        "### 关键词补充",
        "、".join(normalize_keywords(resume.get("keywords"))) or "（无）",
        "",
        "### 需要压缩或弱化的内容",
        *lines(normalize_items(resume.get("compress"))),
        "",
        "## 四、高频面试预测",
        "",
    ]
    for idx, (question, approach, pitfall) in enumerate(
        normalize_interview(result.get("interview"))[:5], start=1
    ):
        out.append(f"**Q{idx}. {question}**")
        if approach:
            out.append(f"- 答题思路：{approach}")
        if pitfall:
            out.append(f"- 易踩的坑：{pitfall}")
        out.append("")

    # 画像已经移到「求职设置」，不属于这份 JD 的结论，所以放到附录，不占正文章节
    if fit.get("profile") or fit.get("roles"):
        out += ["", "---", "", "## 附录：适配岗位画像（来自求职设置，与这份 JD 无关）", ""]
        if fit.get("profile"):
            out += [f"**求职画像：** {fit['profile']}", ""]
        out += ["### 适配的岗位方向", *lines(normalize_items(fit.get("roles"))), ""]
        out += ["### 不建议优先投的方向", *lines(normalize_items(fit.get("avoid"))), ""]

    out += ["---", "", SAFETY_FOOTER]
    return "\n".join(out)


# ---------- 单板块重生成 / 追问 / Word 导出 ----------

SECTION_LABELS = {
    # 画像不在这里：它已经移到侧边栏「求职设置」，用那边自己的按钮重算，
    # 免得同一个东西有两个重算入口、结果还互相覆盖
    "jd_breakdown": "① JD 拆解",
    "match": "② 个人匹配评估",
    "resume": "③ 简历修改建议",
    "interview": "④ 高频面试预测",
    "decision": "投递决策 + 入职后典型一天",
}

# 每个板块单独一份极简 schema：让模型只吐这一块，输出短、便宜、不容易截断
SECTION_SCHEMA = {
    "fit_roles": '{"fit_roles": {"profile": "…", "roles": [{"title": "…", "detail": "…"}], "avoid": [{"title": "…", "detail": "…"}]}}',
    "jd_breakdown": '{"jd_breakdown": {"company": {"must": [{"title": "…", "detail": "…"}], "expect": [{"title": "…", "detail": "…"}], "verdict": "公司这关：能过/悬/过不了 + 理由"}, "hr": {"must": [{"title": "…", "detail": "…"}], "soft": [{"title": "…", "detail": "…"}], "verdict": "HR 这关：能过/悬/过不了 + 理由"}, "screening_order": ["第 1 关…", "第 2 关…", "第 3 关…"], "implicit": [{"title": "…", "detail": "…"}]}}',
    "match": '{"match": {"verdict_level": "高 或 中 或 低", "verdict_reason": "…", "strengths": [{"title": "…", "detail": "…"}], "gaps": [{"title": "…", "detail": "…"}]}}',
    "resume": '{"resume": {"rewrites": [{"title": "简历里的原句", "detail": "改成什么 + 为什么"}], "reorder": [{"title": "…", "detail": "…"}], "keywords": ["…"], "compress": [{"title": "…", "detail": "…"}]}}',
    "interview": '{"interview": [{"question": "…", "approach": "…", "pitfall": "…"}]}',
    "decision": '{"decision": {"verdict": "建议投 或 谨慎投 或 别浪费简历", "reason": "…", "next_step": "…"}, "day_in_life": ["…", "…", "…"]}',
}


def chat_once(system: str, user: str, config, max_tokens: int = 2500, json_mode: bool = True) -> str:
    """一次单轮调用。局部重生成和追问共用，避免两份客户端代码走偏。"""
    from openai import OpenAI  # 延迟导入，和 call_llm 保持一致

    problem = validate_config(config)
    if problem:
        raise ValueError(problem)

    api_key, base_url, model = config
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=180, max_retries=1)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    if json_mode:
        try:
            resp = client.chat.completions.create(**kwargs, response_format={"type": "json_object"})
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001  不支持 response_format 的服务降级重试
            msg = str(exc).lower()
            if not any(k in msg for k in ("response_format", "400", "invalid", "unsupported")):
                raise
    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def regenerate_section(job: dict, key: str, config) -> tuple[bool, str]:
    """只重跑某一个板块并就地合并进结果，不重算整份。"""
    inputs = (job.get("meta") or {}).get("inputs") or {}
    jd_text = job.get("jd_text") or ""
    if not jd_text:
        return False, "这条记录没保存 JD 原文，只能重跑一次完整分析。"
    if key not in SECTION_SCHEMA:
        return False, "不支持的板块。"

    prompt, mapping = build_request(inputs, jd_text, scrub=scrub_enabled())
    result = job.get("result") or {}
    summary = {
        "岗位画像": result.get("role_summary", ""),
        "整体匹配度": (result.get("match") or {}).get("verdict_level", ""),
        "投递建议": (result.get("decision") or {}).get("verdict", ""),
    }
    prompt += (
        f"\n\n【已有结论摘要】\n{json.dumps(summary, ensure_ascii=False)}\n\n"
        f"【本次任务】\n材料已经给全，只重做「{SECTION_LABELS[key]}」这一块。"
        "只输出下面这一个 JSON 对象，不要带其它字段，不要任何解释：\n"
        f"{SECTION_SCHEMA[key]}"
    )

    try:
        raw = chat_once(SYSTEM_PROMPT, prompt, config, max_tokens=2500, json_mode=True)
    except Exception as exc:  # noqa: BLE001
        return False, format_api_error(exc)

    parsed = parse_json_response(raw)
    if not parsed:
        return False, "模型返回的不是合法 JSON，重试一次或换个模型。"
    parsed = privacy.restore_obj(parsed, mapping)

    if key == "decision":
        job["result"]["decision"] = parsed.get("decision") or {}
        job["result"]["day_in_life"] = parsed.get("day_in_life") or []
    elif key in parsed:
        job["result"][key] = parsed[key]
    else:
        return False, f"模型没有返回 {key} 字段，重试一次。"
    return True, f"已重生成「{SECTION_LABELS[key]}」。"


def ask_followup(job: dict, question: str, config) -> tuple[bool, str]:
    """就这份结果继续追问。带上原始材料 + 已有结论，答案才不会飘。"""
    inputs = (job.get("meta") or {}).get("inputs") or {}
    jd_text = job.get("jd_text") or ""
    prompt, mapping = build_request(inputs, jd_text, scrub=scrub_enabled())
    result = job.get("result") or {}
    digest = {
        "岗位画像": result.get("role_summary", ""),
        "投递建议": result.get("decision") or {},
        "匹配度": (result.get("match") or {}).get("verdict_level", ""),
        "主要短板": [t for t, _ in normalize_items((result.get("match") or {}).get("gaps"))][:3],
    }
    prompt += (
        f"\n\n【已有分析结论】\n{json.dumps(digest, ensure_ascii=False)}\n\n"
        f"【我的追问】\n{question}\n\n"
        "要求：直接回答，别重复上面已经讲过的内容；250 字以内；材料里没有的就写「材料里没体现，"
        "建议你自己确认」，绝对不许编经历或数字。"
    )
    try:
        text = chat_once(SYSTEM_PROMPT, prompt, config, max_tokens=1200, json_mode=False)
    except Exception as exc:  # noqa: BLE001
        return False, format_api_error(exc)
    return True, privacy.restore(text, mapping)


def build_docx(result: dict, meta: dict) -> bytes:
    """导出 Word。用默认样式，不引模板，能直接发给别人看。"""
    from docx import Document

    doc = Document()
    doc.add_heading("岗位 JD 智能分析报告", level=0)

    head = [
        ("岗位名称", meta.get("title") or result.get("job_title") or "-"),
        ("公司", meta.get("company") or result.get("company_name") or "-"),
        ("我的身份", meta.get("identity") or "-"),
        ("学历 / 院校", f"{meta.get('education') or '-'} / {meta.get('school_tier') or '不填'}"),
        ("分析时间", meta.get("time") or "-"),
        ("岗位画像", result.get("role_summary") or "-"),
    ]
    for key, value in head:
        para = doc.add_paragraph()
        para.add_run(f"{key}：").bold = True
        para.add_run(str(value))

    decision = result.get("decision") or {}
    if decision.get("verdict"):
        para = doc.add_paragraph()
        para.add_run(f"投递建议：{decision.get('verdict')}").bold = True
        if decision.get("reason"):
            para.add_run(f"　{decision['reason']}")
        if decision.get("next_step"):
            doc.add_paragraph(f"下一步：{decision['next_step']}")

    def add_items(items: list[tuple[str, str]]) -> None:
        for title, detail in items:
            para = doc.add_paragraph(style="List Bullet")
            para.add_run(title).bold = True
            if detail:
                para.add_run(f"　{detail}")

    fit = result.get("fit_roles") or {}
    breakdown = result.get("jd_breakdown") or {}
    match = result.get("match") or {}
    resume = result.get("resume") or {}

    doc.add_heading("一、JD 拆解（两批人两套筛法）", level=1)
    bd = normalize_breakdown(breakdown)
    day = [str(x).strip() for x in (result.get("day_in_life") or []) if str(x).strip()]
    if day:
        doc.add_paragraph("入职后典型一天").runs[0].bold = True
        for item in day:
            doc.add_paragraph(item, style="List Number")
    if bd["order"]:
        doc.add_paragraph("筛选顺序").runs[0].bold = True
        for item in bd["order"]:
            doc.add_paragraph(item, style="List Number")
    for label, items, verdict in (
        ("公司侧 · 硬要求（不会就是不会）", bd["company_must"], bd["company_verdict"]),
        ("公司侧 · JD 没写全的期待", bd["company_expect"], ""),
        ("HR 侧 · 会卡的硬指标", bd["hr_must"], bd["hr_verdict"]),
        ("HR 侧 · 会重点核的软性点", bd["hr_soft"], ""),
        ("两批人都没说的隐性要求", bd["implicit"], ""),
    ):
        doc.add_paragraph(label).runs[0].bold = True
        if verdict:
            doc.add_paragraph(f"结论：{verdict}")
        add_items(items)

    doc.add_heading("二、个人匹配评估", level=1)
    doc.add_paragraph(
        f"整体匹配度：{match.get('verdict_level', '-')}　{match.get('verdict_reason', '')}".strip()
    )
    doc.add_paragraph("优势匹配点").runs[0].bold = True
    add_items(normalize_items(match.get("strengths")))
    doc.add_paragraph("短板 / 风险点").runs[0].bold = True
    add_items(normalize_items(match.get("gaps")))

    doc.add_heading("三、简历修改建议", level=1)
    doc.add_paragraph("逐条改写（原句 → 改法）").runs[0].bold = True
    add_items(normalize_items(resume.get("rewrites")))
    doc.add_paragraph("经历排序").runs[0].bold = True
    add_items(normalize_items(resume.get("reorder")))
    doc.add_paragraph("关键词补充").runs[0].bold = True
    doc.add_paragraph("、".join(normalize_keywords(resume.get("keywords"))) or "（无）")
    doc.add_paragraph("需要压缩或弱化的内容").runs[0].bold = True
    add_items(normalize_items(resume.get("compress")))

    doc.add_heading("四、高频面试预测", level=1)
    for idx, (question, approach, pitfall) in enumerate(
        normalize_interview(result.get("interview"))[:5], start=1
    ):
        doc.add_paragraph(f"Q{idx}. {question}").runs[0].bold = True
        if approach:
            doc.add_paragraph(f"答题思路：{approach}")
        if pitfall:
            doc.add_paragraph(f"易踩的坑：{pitfall}")

    # 画像不属于这份 JD 的结论，挪到附录，正文只讲这份岗位
    if fit.get("profile") or fit.get("roles"):
        doc.add_page_break()
        doc.add_heading("附录：适配岗位画像（来自求职设置，与这份 JD 无关）", level=1)
        if fit.get("profile"):
            doc.add_paragraph(f"求职画像：{fit['profile']}")
        doc.add_paragraph("适配的岗位方向").runs[0].bold = True
        add_items(normalize_items(fit.get("roles")))
        doc.add_paragraph("不建议优先投的方向").runs[0].bold = True
        add_items(normalize_items(fit.get("avoid")))

    doc.add_paragraph()
    doc.add_paragraph(SAFETY_FOOTER).runs[0].bold = True

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ==========================================================================
# 六、页面：侧边栏
# ==========================================================================


# ==========================================================================
# 适配岗位画像：放在侧边栏「求职设置」这一侧
# 它是"我这个人的方向"，不依赖任何一份 JD，所以不该混在结果里；
# 改完背景/偏好点一下就能重算，不用重新贴 JD。
# ==========================================================================

PROFILE_SYSTEM = """你是资深职业顾问，只做一件事：根据候选人的身份、学历、背景、偏好与性格，推导他适合投什么岗位。

规则：
1. 只依据材料判断。材料里没有的经历、证书、成绩一律不编，缺的写"材料中未体现"。
2. 性格与 MBTI 只作参考信号，不贴标签；要说清这类性格在哪类场景更省力、哪类场景更耗神。
3. 推导必须与身份一致：在校生不要推要求 3 年经验的岗位；已毕业要看择业期是否还在。
4. roles 给 4 条，title 是岗位方向（写得具体，如"制造业成本核算"而不是"财务"），detail 说明为什么适合 + 现实门槛。
5. avoid 给 2 条，说清为什么不建议优先投，要落到具体原因。
6. profile 是一句话画像，40 字以内，说清"你是个什么样的人、该往哪个方向使劲"。
7. 语言为简体中文，使用中文标点。
8. 只输出一个 JSON 对象，不要 markdown 代码块包裹，不要任何额外解释。"""

PROFILE_SCHEMA = '{"profile": "一句话画像", "roles": [{"title": "岗位方向", "detail": "为什么适合 + 现实门槛"}], "avoid": [{"title": "不建议方向", "detail": "为什么"}]}'


def derive_profile(inputs: dict[str, str], config) -> tuple[dict, str]:
    """只推导画像，不看 JD、不看简历。返回 (画像字典, 错误说明)。"""
    parts = {
        "identity": (inputs.get("identity") or "").strip() or "（未填，按应届生口径）",
        "education": f'{inputs.get("education") or "未填"} / {inputs.get("school_tier") or "不填"}',
        "background": (inputs.get("background") or "").strip() or "（未填）",
        "preference": (inputs.get("preference") or "").strip() or "（未填，仅按背景推导）",
    }

    mapping: dict[str, dict[str, str]] = {}
    if scrub_enabled():
        parts, mapping = privacy.scrub_many(parts)

    prompt = f"""【我的身份】
{parts["identity"]}

【我的学历 / 院校层次】
{parts["education"]}

【个人背景】
{parts["background"]}

【求职偏好与性格】
{parts["preference"]}

请严格按以下 JSON 结构输出，字段名必须完全一致：
{PROFILE_SCHEMA}"""

    blocks = privacy.hint_block(mapping)
    if blocks:
        prompt += f"\n\n{blocks}"

    try:
        raw = chat_once(PROFILE_SYSTEM, prompt, config, max_tokens=1400)
    except ValueError as exc:
        return {}, str(exc)
    except Exception as exc:  # noqa: BLE001  网络/额度问题由 format_api_error 翻成人话
        return {}, format_api_error(exc)

    parsed = parse_json_response(raw)
    if not parsed:
        return {}, "模型返回的不是合法 JSON，重试一次即可。"

    parsed = privacy.restore_obj(parsed, mapping)
    roles = normalize_items(parsed.get("roles"))
    avoid = normalize_items(parsed.get("avoid"))
    if not roles:
        return {}, "模型没返回可用的岗位方向，重试一次。"

    return (
        {
            "profile": str(parsed.get("profile") or "").strip(),
            "roles": [{"title": t, "detail": d} for t, d in roles],
            "avoid": [{"title": t, "detail": d} for t, d in avoid],
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": "手动推导",
            "inputs": {k: v for k, v in inputs.items() if k in ("identity", "education", "school_tier", "background", "preference")},
        },
        "",
    )


def render_profile_panel(inputs: dict[str, str], config) -> None:
    """画像块：只展示结论，细节折叠，避免把设置页撑得太长。"""
    data = st.session_state.setdefault("data", store.load())
    prof = data.get("profile") or {}

    st.markdown("##### 🎯 适配岗位画像")
    st.caption(
        "只看身份、学历、背景、偏好推导适合投什么，**与具体 JD 无关**，"
        "所以放在求职设置这一页，不用贴 JD 就能重算。"
    )

    if prof:
        summary = str(prof.get("profile") or "").strip()
        if summary:
            st.markdown(
                f'<div class="summary" style="font-size:.86rem;">{html.escape(summary)}</div>',
                unsafe_allow_html=True,
            )
        roles = prof.get("roles") or []
        avoid = prof.get("avoid") or []
        st.caption(f"适配方向 {len(roles)} 条　·　不建议 {len(avoid)} 条　·　{prof.get('updated', '-')}")

        with st.expander("展开看完整画像"):
            st.markdown("**🎯 适配的岗位方向**")
            for item in roles:
                title = str(item.get("title") or "").strip()
                detail = str(item.get("detail") or "").strip()
                st.markdown(f"- **{html.escape(title)}**")
                if detail:
                    st.caption(f"　{detail}")
            if avoid:
                st.markdown("**🚫 不建议优先投**")
                for item in avoid:
                    title = str(item.get("title") or "").strip()
                    detail = str(item.get("detail") or "").strip()
                    st.markdown(f"- {html.escape(title)}")
                    if detail:
                        st.caption(f"　{detail}")
    else:
        st.caption("还没有画像。点下面按钮推导一次，不需要贴 JD。")

    if st.button(
        "🔄 按当前设置重新推导" if prof else "✨ 推导我的适配画像",
        key="derive_profile_btn",
        use_container_width=True,
    ):
        if not config[0]:
            st.error("还没填 API Key，先在本页下方「⚙️ 模型配置」里填好。")
        else:
            with st.spinner("正在推导画像…"):
                new_prof, err = derive_profile(inputs, config)
            if err:
                st.error(err)
            else:
                data["profile"] = new_prof
                if persist_data():
                    st.success(
                        f"画像已更新（{len(new_prof.get('roles') or [])} 个方向）。"
                        "分析 JD 时不会再重复推导这一块。"
                    )


NAV_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("工作台", [
        ("home", "🏠　首页"),
        ("analyze", "🚀　岗位分析"),
        ("track", "📮　投递追踪"),
        ("calendar", "🗓　日程日历"),
        ("season", "📅　秋招节奏"),
    ]),
    ("岗位池", [
        ("pool", "🎯　推荐岗位"),
        ("intern", "🎒　实习岗"),
        ("social", "💼　社招板块"),
        ("fav", "⭐　收藏夹"),
        ("compare", "⚖️　对比分析"),
        ("history", "🗂　历史记录"),
    ]),
    ("成长与素材", [
        ("resumes", "📄　简历中心"),
        ("ability", "📊　能力分析"),
        ("network", "📚　人脉·学习·资料"),
        ("news", "🌍　国际视野"),
        ("interview", "🎤　面试真题"),
        ("intel", "🏢　公司情报"),
    ]),
    ("收获与状态", [
        ("offers", "💰　Offer 管理"),
        ("mood", "📝　心态日记"),
        ("ai", "🤖　AI 求职助手"),
    ]),
    ("我的", [
        ("settings", "⚙️　求职设置"),
    ]),
]


def restore_payload(raw: bytes) -> tuple[dict | None, str]:
    """校验上传的备份文件。能还原就返回数据，否则返回 (None, 原因)。

    不信任文件内容：用户可能选错文件，备份也可能被手改过。只认结构，不做猜测。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "这个文件不是 UTF-8 文本，可能选错了文件。"
    try:
        payload = json.loads(text)
    except ValueError:
        return None, "不是有效的 JSON，可能选错了文件。"
    if not isinstance(payload, dict):
        return None, "格式不对：备份文件的顶层应该是一个键值对象。"
    known = {"jobs", "companies", "interviews", "settings", "resumes", "offers"}
    if not known & set(payload.keys()):
        return None, "文件里找不到岗位 / 公司 / 面试这些字段，不像是本程序导出的备份。"
    return payload, ""


def render_backup_box() -> None:
    """侧边栏的备份 / 恢复区。

    为什么需要：Streamlit 社区云的容器文件系统是临时的，官方明确说不保证持久化 ——
    重启、重新部署、平台迁移都会让 data.json 回到初始状态。给一个手动出口最实在：
    录完点下载存到本机，换环境或数据被清空时上传还原。
    """
    current = st.session_state.get("data") or store.load()
    blob = json.dumps(current, ensure_ascii=False, indent=1).encode("utf-8")
    jobs = len(current.get("jobs") or [])
    interviews = len(current.get("interviews") or [])

    with st.expander("💾 备份 / 恢复", expanded=False):
        st.caption(
            f"当前 {len(blob) / 1024:.0f} KB · 岗位 {jobs} 条 · 面试 {interviews} 场"
        )
        st.download_button(
            "⬇️ 下载备份到电脑",
            data=blob,
            file_name=f"jobtool-backup-{datetime.now():%Y%m%d-%H%M}.json",
            mime="application/json",
            use_container_width=True,
            key="backup_download",
        )
        st.caption("云端重启会清空数据，建议每天录完点一次。文件含你的记录，别传到公开仓库。")

        up = st.file_uploader("⬆️ 上传备份还原", type=["json"], key="backup_restore_file")
        if up is not None:
            payload, why = restore_payload(up.getvalue())
            if payload is None:
                st.error(why)
            else:
                incoming = len(payload.get("jobs") or [])
                st.warning(
                    f"将用这份备份覆盖当前数据：备份里 {incoming} 条岗位，"
                    f"当前 {jobs} 条。覆盖后现有的会没，建议先下载一份当前备份。"
                )
                if st.button("确认覆盖", key="backup_restore_go", use_container_width=True):
                    ok, msg = store.save(payload)
                    if ok:
                        st.session_state["data"] = payload
                        st.success("已还原。")
                        st.rerun()
                    else:
                        st.error(f"写入失败：{msg}")


# ==========================================================================
# 多用户：登录门 + 按用户挂载云端数据
# ==========================================================================
# 没配 SUPABASE_URL / SUPABASE_ANON_KEY 时下面整段不生效，应用仍是原来那个
# 单用户本地版（读本机 data.json）—— 改造不能把现有用法弄坏，这是底线。
LOGIN_CSS = """
<style>
.login-wrap{max-width:520px;margin:2.5rem auto 1.2rem;text-align:center;}
.login-wrap .logo{font-size:2.4rem;line-height:1;}
.login-wrap h2{margin:.5rem 0 .3rem 0;font-size:1.5rem;}
.login-wrap p{margin:0;opacity:.68;font-size:.88rem;line-height:1.6;}
.login-note{margin-top:1.1rem;padding:.7rem .9rem;border-radius:.7rem;font-size:.78rem;
  line-height:1.6;background:var(--soft);opacity:.9;}
</style>
"""


def render_login_page(cfg: dict) -> None:
    """未登录时看到的页。登录 / 注册两个标签页。"""
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="login-wrap"><div class="logo">🧭</div>'
        "<h2>求职工作台</h2>"
        "<p>登录后，岗位记录、面试真题、投递进度只有你自己能看到。<br>"
        "同一个链接，每个人看到的是自己那一份。</p></div>",
        unsafe_allow_html=True,
    )
    if st.session_state.pop("auth_expired", False):
        st.warning("登录状态已过期，请重新登录。")

    _left, mid, _right = st.columns([1, 1.6, 1])
    with mid:
        tab_in, tab_up = st.tabs(["登录", "注册"])

        with tab_in, st.form("login_form"):
            email = st.text_input("邮箱", key="login_email_in", placeholder="you@example.com")
            pwd = st.text_input("密码", type="password", key="login_pwd_in")
            if st.form_submit_button("登录", type="primary", use_container_width=True):
                clean = (email or "").strip().lower()
                if not clean or not pwd:
                    st.error("邮箱和密码都要填。")
                else:
                    try:
                        session, why = auth.sign_in(cfg, clean, pwd)
                    except auth.CloudError as exc:
                        st.error(str(exc))
                    else:
                        if session:
                            st.session_state["auth_session"] = session
                            st.rerun()
                        else:
                            st.error(why or "登录失败。")

        with tab_up, st.form("signup_form"):
            email2 = st.text_input("邮箱", key="signup_email", placeholder="you@example.com")
            pwd2 = st.text_input("密码（至少 6 位）", type="password", key="signup_pwd")
            pwd3 = st.text_input("再输一次密码", type="password", key="signup_pwd2")
            if st.form_submit_button("注册", type="primary", use_container_width=True):
                clean2 = (email2 or "").strip().lower()
                if not clean2 or not pwd2:
                    st.error("邮箱和密码都要填。")
                elif len(pwd2) < 6:
                    st.error("密码至少 6 位。")
                elif pwd2 != pwd3:
                    st.error("两次输入的密码不一样。")
                else:
                    try:
                        session, why = auth.sign_up(cfg, clean2, pwd2)
                    except auth.CloudError as exc:
                        st.error(str(exc))
                    else:
                        if session:
                            st.session_state["auth_session"] = session
                            st.rerun()
                        else:
                            st.info(why or "注册完成，去邮箱确认后登录。")

        st.markdown(
            '<div class="login-note">数据存在云端数据库，每人一行，'
            "数据库权限策略锁死「只能读写自己那一行」，别人拿到接口地址也翻不到你的记录。<br>"
            "录完记得在左下角「💾 备份 / 恢复」下载一份到电脑。</div>",
            unsafe_allow_html=True,
        )


def ensure_login(cfg: dict) -> dict:
    """云模式的登录门。已登录返回会话；没登录就画登录页并停下。"""
    session = st.session_state.get("auth_session")
    if session and not auth.still_valid(session):
        fresh = auth.refresh(cfg, session)
        if fresh:
            st.session_state["auth_session"] = session = fresh
        else:
            session = None
            st.session_state.pop("auth_session", None)
            st.session_state["auth_expired"] = True
    if session:
        return session

    render_login_page(cfg)
    st.stop()
    return {}  # 走不到这里，给类型检查留个返回值


def activate_cloud_data(cfg: dict) -> None:
    """把 store 的读写挂到当前登录用户名下。

    这里不在启动时固定一份 token：token 一小时后会过期，闭包里存死了就会在
    「用了一小时突然存不进去」时报错。每次读写都走 _live() 现取现续。
    """

    def _live() -> dict:
        s = st.session_state.get("auth_session")
        if not s:
            raise auth.CloudError("登录状态已失效，请刷新页面重新登录。")
        if not auth.still_valid(s):
            fresh = auth.refresh(cfg, s)
            if not fresh:
                raise auth.CloudError("登录已过期，请刷新页面重新登录。")
            st.session_state["auth_session"] = fresh
            s = fresh
        return s

    store.use_cloud_backend(
        lambda: auth.fetch_data(cfg, _live()),
        lambda data: auth.save_data(cfg, _live(), data),
    )


def render_account_box(session: dict) -> None:
    """侧边栏的账号信息 + 退出登录。"""
    with st.expander(f"👤 {session.get('email') or '已登录'}", expanded=False):
        st.caption("数据存在云端数据库，只有你自己能看到。")
        if st.button("退出登录", use_container_width=True, key="logout_go"):
            st.session_state.pop("auth_session", None)
            st.session_state.pop("data", None)
            st.session_state.pop("data_user", None)
            store.reset_cloud_backend()
            st.rerun()


def render_nav() -> str:
    """左侧导航窗格。

    用按钮而不是 radio：Streamlit 的 radio 圆点在各个版本里 DOM 结构不一样，
    想稳定地藏掉很费劲；按钮改成左对齐 + 选中态填色，效果一样且不会随版本坏掉。
    """
    with st.sidebar:
        st.markdown(
            '<div class="nav-brand">'
            '<div class="logo">🧭</div>'
            '<div><div class="t">求职工作台</div>'
            '<div class="s">JD ANALYZER</div></div>'
            "</div>",
            unsafe_allow_html=True,
        )
        current = st.session_state.get("nav_page", "home")
        for title, items in NAV_GROUPS:
            st.markdown(f'<div class="nav-group">{title}</div>', unsafe_allow_html=True)
            for key, label in items:
                if st.button(
                    label,
                    key=f"nav_{key}",
                    use_container_width=True,
                    type="primary" if key == current else "secondary",
                ):
                    goto(key)

        st.divider()
        _acct = st.session_state.get("auth_session")
        if _acct:
            render_account_box(_acct)
        render_backup_box()
        info = store.stats()
        st.caption(
            f"📦 岗位 {info.get('jobs', 0)} 条　·　真题 {info.get('interviews', 0)} 场"
        )
        if store.cloud_backend_active():
            st.caption("☁️ 数据在云端，只有当前账号能看到。")
        else:
            st.caption(f"数据文件：`{info.get('path', '-')}`")
        mem = process_mem_mb()
        if mem:
            st.caption(f"进程内存约 {mem:.0f} MB。占内存的主要是简历解析和页面缓存，关掉浏览器会释放。")
        if info.get("on_system_drive") and not store.cloud_backend_active():
            st.warning("数据还在 C 盘，建议到「⚙️ 求职设置」换到 D 盘。")
    return st.session_state.get("nav_page", "home")


# ==========================================================================
# 草稿仓：让「粘了一半的 JD」「填了一半的 API Key」跨页面、跨刷新都不丢
# ==========================================================================
# 背景：Streamlit 有个容易踩空的行为 —— 某个 widget 在本次 rerun 里没有被创建，
# 它挂在 session_state 里的值就会被清掉。于是真实发生过：
#   在「岗位分析」粘好 JD → 跳到「求职设置」填 API Key → 切回来，JD 没了；
#   反过来那个 API Key 也没了。用户实测踩到，不是理论风险。
#
# 解法：给这些输入配一层独立草稿（__drafts）。它不属于任何 widget，
# Streamlit 不会清它。控件渲染前从草稿恢复，渲染后把值写回草稿。
# JD 正文 / 模型地址还会落到 data.json，连刷新页面、重启进程都保得住。
# ==========================================================================
# --------------------------------------------------------------------------
# 校招 / 实习：两套独立的求职口径
# 实习看日薪、每周能去几天、有没有转正名额；校招看起薪、批次、培养路径。
# 混成一套填，模型就只能拿同一个标准去评所有岗位，判断必然偏一头。
# --------------------------------------------------------------------------
PREF_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "campus": [
        ("pref_campus_city", "目标城市", "成都 / 重庆 / 西安，可接受出差"),
        ("pref_campus_salary", "期望薪资", "8-12K，低于 7K 不考虑"),
        ("pref_campus_time", "批次与时间", "2027 届秋招，明年 7 月可全职"),
    ],
    "intern": [
        ("pref_intern_city", "目标城市", "成都，也看线上远程"),
        ("pref_intern_salary", "日薪期望", "150-250 元/天"),
        ("pref_intern_time", "可到岗方式", "每周 4 天，能连做 6 个月，希望有转正名额"),
    ],
}
PREF_LABEL = {"campus": "校招", "intern": "实习"}

DRAFT_DEFAULTS = {
    # 岗位分析页
    "jd_input": "",
    "jd_input_multi": "",
    "jd_title_input": "",
    "jd_company_input": "",
    "jd_salary_input": "",
    "jd_mode": "单份分析",
    "jd_import_url": "",
    "page_extract_raw": "",
    # 求职设置页
    "identity_input": DEFAULT_IDENTITY,
    "education_input": DEFAULT_EDUCATION,
    "school_tier_input": DEFAULT_SCHOOL_TIER,
    "background_input": DEFAULT_BACKGROUND,
    "preference_input": DEFAULT_PREFERENCE,
    "pref_campus_city": "",
    "pref_campus_salary": "",
    "pref_campus_time": "",
    "pref_intern_city": "",
    "pref_intern_salary": "",
    "pref_intern_time": "",
    "resume_paste": "",
    "resume_text": "",
    "cfg_api_key": "",
    "cfg_base_url": DEFAULT_API_BASE,
    "cfg_model": DEFAULT_MODEL,
}

# 不写进 data.json 的字段：
#   cfg_api_key      等同密码，只在本次打开的内存里留着（切页不丢，刷新要重填）
#   page_extract_raw 整页网页原文只是中间产物，拆完就落成结构化字段了，
#                    没必要每打一次字就把它整个写回磁盘
DRAFT_NEVER_SAVE = {"cfg_api_key", "page_extract_raw"}


def _drafts() -> dict:
    """草稿仓本体。启动时从 data.json 灌过一次，之后就是内存里的活数据。"""
    return st.session_state.setdefault("__drafts", {})


def draft_restore(key: str) -> None:
    """widget 渲染前调用：值被 Streamlit 清掉了，就从草稿恢复。"""
    if key in st.session_state:
        return
    value = _drafts().get(key, DRAFT_DEFAULTS.get(key, ""))
    if value not in ("", None):
        st.session_state[key] = value


def draft_keep(key: str) -> None:
    """widget 渲染后调用：把当前值写回草稿，并标记「需要落盘」。"""
    value = st.session_state.get(key)
    if value is None:
        return
    if _drafts().get(key) != value:
        _drafts()[key] = value
        st.session_state["__drafts_dirty"] = True


def load_drafts(data: dict) -> None:
    """启动时把上次的草稿灌进来（刷新页面、重启进程后内容还在）。"""
    if st.session_state.get("__drafts_loaded"):
        return
    st.session_state["__drafts_loaded"] = True
    saved = store.get_blob(data, "drafts", {})
    if not isinstance(saved, dict):
        return
    drafts = _drafts()
    for key, value in saved.items():
        if key in DRAFT_DEFAULTS and key not in DRAFT_NEVER_SAVE:
            drafts.setdefault(key, value)


def persist_drafts(data: dict) -> None:
    """把草稿落到 data.json（API Key 除外）。只在真变了的时候写一次。"""
    if not st.session_state.pop("__drafts_dirty", False):
        return
    keep = {k: v for k, v in _drafts().items() if k not in DRAFT_NEVER_SAVE}
    # 空白字段不占地方：清空输入就是「不要了」，没必要一直存着
    keep = {k: v for k, v in keep.items() if v not in ("", None)}
    store.set_blob(data, "drafts", keep)
    try:
        store.save(data)
    except OSError:
        pass


def draft_value(key: str, default: str | None = None) -> str:
    """取一个输入字段的当前值：本次渲染的 widget → 草稿 → 默认值。

    default 传了就用调用方给的（比如 secrets.toml 里的配置），
    没传才回落到 DRAFT_DEFAULTS 里登记的那个默认。
    """
    value = st.session_state.get(key)
    if value in (None, ""):
        value = _drafts().get(key)
    if value in (None, ""):
        value = DRAFT_DEFAULTS.get(key, "") if default is None else default
    return str(value if value is not None else "")


def compose_preference(inputs: dict, kind: str = "") -> str:
    """按「校招 / 实习」里对应的那套口径，拼成一段给模型看的偏好文字。

    两套都没填就只回落到自由文本那一栏，行为跟以前一致。
    """
    key = "intern" if str(kind or "").strip() == "实习" else "campus"
    bits = []
    for field, label, _ in PREF_FIELDS[key]:
        value = str(inputs.get(field) or "").strip()
        if value:
            bits.append(f"{label}：{value}")
    head = f"【{PREF_LABEL[key]}口径】" + "；".join(bits) if bits else ""
    base = str(inputs.get("preference") or "").strip()
    return "\n".join(x for x in (head, base) if x)


def collect_inputs() -> dict[str, str]:
    """从草稿仓读求职背景。

    输入控件只在「求职设置」页渲染，但值同时留在草稿里，
    所以其它页照样能拿到 —— 切页不会丢，也不用为了拿值去渲染那些控件。
    """
    result = {
        "background": draft_value("background_input").strip(),
        "preference": draft_value("preference_input").strip(),
        "resume": draft_value("resume_text"),
        "identity": draft_value("identity_input") or DEFAULT_IDENTITY,
        "education": draft_value("education_input") or DEFAULT_EDUCATION,
        "school_tier": draft_value("school_tier_input") or DEFAULT_SCHOOL_TIER,
    }
    # 校招 / 实习两套偏好的原始字段，一并带出去，分析时按 JD 类型选
    for fields in PREF_FIELDS.values():
        for field, _, _ in fields:
            result[field] = draft_value(field).strip()
    return result


def collect_config() -> tuple[str, str, str]:
    """当前生效的模型配置。

    取值顺序：界面上填过的（草稿里有）> secrets.toml > 环境变量 > 内置默认。
    以前这里直接读 session_state，一旦切页被 Streamlit 清掉就退回文件值，
    用户手填的 Key 会「莫名消失」，所以改成经草稿。
    """
    return resolve_config(
        draft_value("cfg_api_key", read_config("API_KEY") or read_config("OPENAI_API_KEY")),
        draft_value("cfg_base_url", read_config("BASE_URL", DEFAULT_API_BASE)),
        draft_value("cfg_model", read_config("MODEL", DEFAULT_MODEL)),
    )


def render_settings_page(data: dict) -> tuple[dict[str, str], tuple[str, str, str]]:
    """求职设置页，返回 ({background, preference, resume, identity, education, school_tier},
    (api_key, base_url, model))。

    控件只在设置页里创建，但所有值都写在 session_state，别的页面通过
    collect_inputs() / collect_config() 读，所以换页不影响分析口径。
    """
    st.markdown("### ⚙️ 求职设置")
    st.caption(
        "身份、学历、背景、简历、模型和数据位置都在这一页。改完直接换页就生效，"
        "已经分析过的历史结果不会跟着变。"
    )
    # 单列收窄：设置页如果拉满整个屏幕，输入框会宽到不好读
    with st.columns([1, 2.7, 1])[1]:
        st.markdown("#### 🎯 我的背景与方向")

        # ---------- 〇、身份：单独一栏，因为它决定整份分析的判断口径 ----------
        # 注意：这里刻意不传 value / index —— 初值统一由草稿仓给。
        # 同时传 value 和已经在 session_state 里的 key，Streamlit 会弹黄框警告。
        draft_restore("identity_input")
        identity = st.selectbox(
            "我的身份",
            IDENTITY_OPTIONS,
            key="identity_input",
            help="身份决定判断口径：应届生按校招时间线看（秋招/春招批次、三方、毕业前能否全职到岗），"
                 "社招按可迁移经验看。JD 要求和身份冲突时，会直接标成硬门槛，不含糊成「有挑战」。",
        )
        draft_keep("identity_input")

        # ---------- 〇、学历：单独两栏，学历关和院校关分开判 ----------
        draft_restore("education_input")
        education = st.selectbox(
            "最高学历",
            EDUCATION_OPTIONS,
            key="education_input",
        )
        draft_keep("education_input")

        draft_restore("school_tier_input")
        school_tier = st.selectbox(
            "院校层次",
            SCHOOL_TIER_OPTIONS,
            key="school_tier_input",
            help="很多岗位卡的不是「硕士」而是「本科是否双非/985」。填了分析里会明确判过没过线；不填就不让模型猜。",
        )
        draft_keep("school_tier_input")

        # ---------- 一、个人背景 ----------
        draft_restore("background_input")
        background = st.text_area(
            "个人背景",
            height=170,
            key="background_input",
            help="只写客观事实：学历专业、城市、技能、做过的项目、已有的短板。偏好和性格写到下一栏。",
        )
        draft_keep("background_input")

        # ---------- 二、求职偏好与性格 ----------
        draft_restore("preference_input")
        preference = st.text_area(
            "求职偏好与性格",
            height=140,
            key="preference_input",
            placeholder="可以写一段话，也可以只写性格或 MBTI，例如：偏内向、喜欢有明确标准的工作，MBTI 是 ISTJ",
            help="写清城市/薪资/工作节奏偏好，也可以写性格特点或 MBTI。下面「适配岗位画像」就是根据这一栏推导的。",
        )
        draft_keep("preference_input")

        # ---------- 二·补：校招 / 实习 两套口径分开填 ----------
        st.markdown("##### 🎯 校招 / 实习 分开设置")
        st.caption(
            "各填各的。分析时先看这份 JD 属于实习还是校招，再挑对应那套去比对 —— "
            "不会拿校招的薪资期待去评一份日薪 180 的实习，也不会拿「每周能去几天」去卡校招。"
        )
        tab_campus, tab_intern = st.tabs(["🎓 校招", "🎒 实习"])
        for tab, group in ((tab_campus, "campus"), (tab_intern, "intern")):
            with tab:
                fields = PREF_FIELDS[group]
                cols = st.columns(2)
                for i, (field, label, placeholder) in enumerate(fields):
                    target = cols[i] if i < 2 else None
                    if target is None:
                        draft_restore(field)
                        st.text_input(label, key=field, placeholder=placeholder)
                        draft_keep(field)
                    else:
                        with target:
                            draft_restore(field)
                            st.text_input(label, key=field, placeholder=placeholder)
                            draft_keep(field)
                st.caption(
                    "不填也行 —— 空着的项模型不会拿去硬卡你，只会写「未提供」。"
                )

        st.caption(
            "这里可以随时修改你的背景、求职方向、地点偏好，"
            "修改后新的分析才会生效，历史结果不受影响。"
        )

        st.divider()

        # ---------- 适配岗位画像：与具体 JD 无关，所以归在求职设置里 ----------
        render_profile_panel(
            {
                "identity": identity,
                "education": education,
                "school_tier": school_tier,
                "background": background,
                "preference": preference,
            },
            # 模型配置在下面还没渲染，但 collect_config 会从草稿 / secrets 里取，
            # 所以这里拿到的就是真实生效的那套配置
            collect_config(),
        )

        st.divider()

        # ---------- 三、简历 ----------
        st.markdown("##### 📄 简历")
        uploaded = st.file_uploader(
            "上传简历（PDF / Word / txt）",
            type=["pdf", "docx", "txt", "md"],
            key="resume_file",
            help="只解析你主动上传的这一份文件，不会扫描目录，也不读取其它本地文件。",
        )
        draft_restore("resume_paste")
        pasted_resume = st.text_area(
            "或直接粘贴简历内容",
            height=130,
            key="resume_paste",
            placeholder="把简历正文粘到这里，效果与上传相同",
        )
        draft_keep("resume_paste")

        resume_text, resume_msg = extract_resume_text(uploaded)
        if uploaded is not None:
            if resume_text:
                extra = f" · {resume_msg}" if resume_msg else ""
                st.caption(f"✅ 已读取 {uploaded.name} · {len(resume_text)} 字{extra}")
            else:
                st.error(resume_msg or "解析失败")
                if pasted_resume.strip():
                    resume_text = pasted_resume.strip()
                    st.caption("已改用下方粘贴的简历内容")
        elif pasted_resume.strip():
            resume_text = pasted_resume.strip()
            st.caption(f"✅ 使用粘贴内容 · {len(resume_text)} 字")
        else:
            # 两个输入框都空着，不等于「你没有简历」——很可能是从别的页面切回来、
            # widget 的值被 Streamlit 清掉了。这里保留上次读到的内容，别抹掉它。
            resume_text = draft_value("resume_text")
            if resume_text:
                st.caption(f"✅ 沿用上次读取的简历 · {len(resume_text)} 字（要重传就选个文件或重新粘贴）")
            else:
                st.caption("未提供简历 → 结果里会跳过「逐条改写」，其余板块照常分析")

        # 存一份给其它页面用：简历控件只在设置页渲染，但分析页随时要读它
        st.session_state["resume_text"] = resume_text
        draft_keep("resume_text")

        st.divider()

        # ---------- 隐私脱敏 ----------
        st.markdown("##### 🔒 隐私脱敏")
        st.session_state.setdefault("privacy_scrub", True)
        st.toggle(
            "发送前脱敏（推荐开启）",
            key="privacy_scrub",
            help="姓名、手机号、邮箱、身份证、院校、公司全称会在本机替换成 [P1] 占位符再发出，"
                 "返回后自动还原成真名。城市名保留，因为地点匹配要用。",
        )
        if st.session_state.get("privacy_scrub", True):
            st.caption(
                "已开启：模型收到的是「[P1]（某财经类院校）会计专硕，base 成都」这类文本，"
                "看不到你的真名和联系方式。"
            )
        else:
            st.caption(
                "⚠️ 已关闭：简历原文直接发给你配置的模型服务商。"
                "这等同于你接受该服务商的数据政策，请自行确认。"
            )

        st.toggle(
            "发送前先给我看一遍",
            key="preview_send",
            help="开启后点「开始分析」不会立刻发请求，而是先把要发出去的那段文本（已脱敏）"
                 "完整摊给你看，你点确认才真正调用模型。",
        )
        if st.session_state.get("preview_send"):
            st.caption("已开启：每次分析都会先停一步，让你核对将要发出的内容。")

        st.divider()

        # ---------- 模型配置（折叠，不干扰演示） ----------
        saved_key = read_config("API_KEY") or read_config("OPENAI_API_KEY")
        saved_base = read_config("BASE_URL", DEFAULT_API_BASE)
        saved_model = read_config("MODEL", DEFAULT_MODEL)

        # 初值来源的优先级：上次填过的草稿 > secrets.toml / 环境变量。
        # 先塞进草稿仓，下面的 draft_restore 会按需落到 session_state。
        _drafts().setdefault("cfg_base_url", saved_base)
        _drafts().setdefault("cfg_model", saved_model)
        _drafts().setdefault("cfg_api_key", saved_key or "")

        with st.expander(
            "⚙️ 模型配置" + ("　✅ 已接好" if saved_key else ""),
            expanded=not saved_key,
        ):
            # 这三个也不能用裸 key：切到别的页面再回来，Streamlit 会把值清掉，
            # 表现就是「去分析页粘完 JD 回来，API Key 不见了」。
            draft_restore("cfg_api_key")
            ui_key = st.text_input(
                "API Key", type="password", key="cfg_api_key",
                placeholder="sk-...（切页不丢；刷新页面需重填，不写盘）",
            )
            draft_keep("cfg_api_key")

            draft_restore("cfg_base_url")
            ui_base = st.text_input(
                "Base URL", key="cfg_base_url",
                help="任何 OpenAI 兼容服务均可：DeepSeek / 通义 / 智谱 / Kimi / 硅基流动…",
            )
            draft_keep("cfg_base_url")

            draft_restore("cfg_model")
            ui_model = st.text_input(
                "模型名", key="cfg_model",
                help="例如 deepseek-chat、qwen-plus、glm-4-plus",
            )

            # 把当前真正生效的三项摊开显示，配错了一眼就能看出来
            probe_key, probe_base, probe_model = resolve_config(ui_key, ui_base, ui_model)
            if probe_key:
                masked = (
                    f"{probe_key[:6]}…{probe_key[-4:]}"
                    if len(probe_key) > 12
                    else "已填写"
                )
            else:
                masked = "**未填写**"
            st.caption(f"当前生效 → 地址 `{probe_base}`　模型 `{probe_model}`　Key {masked}")

            # 一键自检：不用粘贴 JD 就能判断配置对不对
            if st.button("🔍 测试连接", key="btn_test_conn", use_container_width=True):
                problem = validate_config((probe_key, probe_base, probe_model))
                if problem:
                    st.error(problem)
                else:
                    with st.spinner("正在请求模型…"):
                        ok, msg = test_connection((probe_key, probe_base, probe_model))
                    if ok:
                        st.success(msg)
                    else:
                        st.error(msg)

            st.caption(
                "也可写入 `.streamlit/secrets.toml`（API_KEY / BASE_URL / MODEL）或同名环境变量，"
                "文件中已配置时此处留空即用文件值。"
            )

        st.divider()

        # ---------- 数据存在哪 / 占多少内存 ----------
        st.markdown("##### 📦 数据与内存")
        notice = st.session_state.pop("data_notice", "")
        if notice:
            st.success(notice)

        info = store.stats()
        # 全部用 .get 兜底：就算运行中的 store 是旧版本，也只会显示 0，不会整页崩掉
        st.caption(f"数据文件：`{info.get('path', '-')}`")
        st.caption(
            f"当前占用 {info.get('size', 0) / 1024:.1f} KB　·　公司 {info.get('companies', 0)} 家"
            f"　·　岗位记录 {info.get('jobs', 0)} 条　·　真题 {info.get('interviews', 0)} 场"
        )
        if info.get("on_system_drive"):
            st.warning(
                "数据还在 C 盘。记录多了会把系统盘写满、拖慢整机，"
                "建议在下面换到 D 盘。"
            )

        mem = process_mem_mb()
        st.caption(
            (f"进程内存占用约 {mem:.0f} MB。" if mem else "") + MEM_TIP
        )

        with st.expander("📁 换个存放位置"):
            new_dir = st.text_input(
                "数据目录",
                value=info["dir"],
                key="data_dir_input",
                help="填目录，不是文件。例如 D:/JobToolData。默认已经自动挑好非系统盘。",
            )
            if st.button(
                "切换并把现有数据复制过去", key="btn_move_dir", use_container_width=True
            ):
                ok_move, move_msg = store.set_data_dir(new_dir)
                if ok_move:
                    st.session_state["data_notice"] = f"✅ {move_msg}"
                    st.session_state["data"] = store.load()  # 从新位置重新读一遍
                    st.rerun()
                else:
                    st.error(move_msg)
            st.caption("只复制、不删除：原位置的文件会留着，确认没问题后你自己删。")

        st.divider()

        # ---------- 首页模块：留哪几块由你定 ----------
        with st.expander("🏠 首页模块"):
            st.caption(
                "关掉的只是不在首页显示，数据一条都不删。想清静就把月历也收起来。"
            )
            for key, label in HOME_MODULES:
                saved = store.get_setting(data, key, "1") == "1"
                st.session_state.setdefault(key, saved)
                value = st.toggle(label, key=key)
                if value != saved:
                    store.set_setting(data, key, "1" if value else "0")
                    persist_data()

        st.divider()

        # ---------- 外观：配色可换，选择会记在 data.json 里 ----------
        current_theme = st.session_state.get("theme", DEFAULT_THEME)
        with st.expander(f"🎨 外观 · 当前「{current_theme}」"):
            options = list(THEMES)
            idx = options.index(current_theme) if current_theme in options else 0
            picked = st.radio(
                "配色",
                options,
                index=idx,
                horizontal=True,
                key="theme_radio",
                label_visibility="collapsed",
                help="只影响界面颜色，不影响任何分析结果。",
            )
            swatch = "".join(
                f'<span style="display:inline-block;width:16px;height:16px;border-radius:50%;'
                f'margin-right:6px;background:{c["acc"]};"></span>'
                for c in THEMES.values()
            )
            st.markdown(swatch, unsafe_allow_html=True)
            st.caption("五套配色：雾蓝 / 墨绿 / 绛紫 / 暖橙 / 石墨。选完立即生效，重启后保留。")

            # 自定义主色：预设选不中就直接挑一个，整套配色由它推出来
            cur_accent = st.session_state.get("accent", "") or ""
            a1, a2 = st.columns([1, 2.2])
            custom = a1.color_picker(
                "自己挑一个主色", value=cur_accent or THEMES[current_theme]["acc"],
                key="accent_pick", help="选完会覆盖上面预设的配色。",
            )
            if a2.button("用这个颜色", key="accent_apply", use_container_width=True):
                st.session_state["accent"] = custom
                store.set_setting(st.session_state["data"], "accent", custom)
                persist_data()
                st.rerun()
            if cur_accent:
                if a2.button("↩️ 回到预设配色", key="accent_clear", use_container_width=True):
                    st.session_state["accent"] = ""
                    store.set_setting(st.session_state["data"], "accent", "")
                    persist_data()
                    st.rerun()
                st.caption(f"当前用的是自定义主色 `{cur_accent}`。")

            if picked != current_theme:
                st.session_state["theme"] = picked
                store.set_setting(st.session_state["data"], "theme", picked)
                persist_data()
                st.rerun()

        # ---------- 流程阶段：自己加、自己调顺序 ----------
        with st.expander(
            "🧩 我的投递流程 · 当前 " + " → ".join(STAGES[:5]) + ("…" if len(STAGES) > 5 else "")
        ):
            st.caption(
                "默认八段是「未投递 → 已投递 → 简历筛 → 笔试 → 一面 → 二面 → HR面 → Offer」。"
                "群面、测评、背调这些可以自己插进来，顺序也能调 —— 追踪页、漏斗、批量改阶段都按你这份排。"
            )
            # 先执行上一轮按钮留下的操作，再渲染控件。
            # 为什么要绕这一步：按钮在控件之后渲染，回调里直接改 selectbox 的
            # session 值 Streamlit 会报"已实例化不能再改"，把操作挪到下一轮最稳。
            stage_list = st.session_state.get("stage_edit")
            if not isinstance(stage_list, list) or not stage_list:
                stage_list = list(STAGES)
            pending_stage = st.session_state.pop("stage_pending", None)
            if isinstance(pending_stage, tuple) and pending_stage:
                op = pending_stage[0]
                idx = int(pending_stage[1])
                if op == "up" and 1 <= idx < len(stage_list):
                    stage_list[idx - 1], stage_list[idx] = stage_list[idx], stage_list[idx - 1]
                    st.session_state[f"stage_pick_{len(stage_list)}"] = idx - 1
                elif op == "down" and 0 <= idx < len(stage_list) - 1:
                    stage_list[idx + 1], stage_list[idx] = stage_list[idx], stage_list[idx + 1]
                    st.session_state[f"stage_pick_{len(stage_list)}"] = idx + 1
                elif op == "rename" and len(pending_stage) > 2 and 0 <= idx < len(stage_list):
                    stage_list[idx] = str(pending_stage[2])
                elif op == "del" and 0 <= idx < len(stage_list) and len(stage_list) > 2:
                    stage_list.pop(idx)
            st.session_state["stage_edit"] = stage_list

            st.markdown(
                '<div class="stage-view">'
                + "".join(
                    f'<span class="stage-chip"><b>{i + 1}</b>{html.escape(name)}</span>'
                    for i, name in enumerate(stage_list)
                )
                + "</div>",
                unsafe_allow_html=True,
            )

            def _stage_label(i: int) -> str:
                # 删段之后旧选中值可能已经越界，这里必须兜住，否则整页崩
                if isinstance(i, int) and 0 <= i < len(stage_list):
                    return f"{i + 1}. {stage_list[i]}"
                return f"{i + 1}. （已删除）"

            # key 里带长度：增删一段时控件重建，旧的越界选中值就不会被沿用
            pick_key = f"stage_pick_{len(stage_list)}"
            place = st.selectbox(
                "选中一段来操作",
                list(range(len(stage_list))),
                format_func=_stage_label,
                key=pick_key,
            )
            place = max(0, min(int(place or 0), len(stage_list) - 1))

            new_name = st.text_input(
                "改个名", value=stage_list[place], key=f"stage_newname_{place}"
            )
            m1, m2, m3 = st.columns(3)
            if m1.button("✏️ 应用改名", key="stage_rename", use_container_width=True):
                fixed = (new_name or "").strip()
                if not fixed:
                    st.error("名字不能空着。")
                elif fixed != stage_list[place] and fixed in stage_list:
                    st.error("已经有同名阶段了，换一个。")
                else:
                    st.session_state["stage_pending"] = ("rename", place, fixed)
                    st.session_state.pop(f"stage_newname_{place}", None)
                    st.rerun()
            if m2.button("↑ 上移", key="stage_up", use_container_width=True, disabled=place == 0):
                st.session_state["stage_pending"] = ("up", place)
                st.rerun()
            if m3.button(
                "↓ 下移", key="stage_down", use_container_width=True,
                disabled=place == len(stage_list) - 1,
            ):
                st.session_state["stage_pending"] = ("down", place)
                st.rerun()

            a1, a2, a3 = st.columns([2, 1, 1])
            new_stage = a1.text_input(
                "新阶段", key="stage_new", placeholder="如：群面 / 测评 / 背调",
                label_visibility="collapsed",
            )
            if a2.button("➕ 加到末尾", key="stage_add", use_container_width=True):
                name = (new_stage or "").strip()
                if not name:
                    st.error("先写个名字。")
                elif name in stage_list:
                    st.error("这个阶段已经有了。")
                else:
                    stage_list.append(name)
                    st.session_state["stage_edit"] = stage_list
                    st.session_state.pop("stage_new", None)
                    st.rerun()
            if a3.button("🗑 删掉选中的", key="stage_del", use_container_width=True):
                if len(stage_list) <= 2:
                    st.error("至少留两段。")
                else:
                    st.session_state["stage_pending"] = ("del", place)
                    st.rerun()

            s1, s2 = st.columns([1, 1.4])
            if s1.button("💾 保存流程", key="stage_save", type="primary", use_container_width=True):
                clean = [str(x).strip() for x in stage_list if str(x).strip()]
                if len(clean) < 2:
                    st.error("至少留两段，不然没有「推进」可言。")
                elif len(set(clean)) != len(clean):
                    st.error("有重复的阶段名，改一下再存 —— 重名会让漏斗算错。")
                else:
                    store.set_blob(st.session_state["data"], "stages", clean)
                    persist_data()
                    st.session_state.pop("stage_edit", None)
                    flash(f"已保存 {len(clean)} 段流程。")
                    st.rerun()
            if s2.button("↩️ 恢复默认八段", key="stage_reset", use_container_width=True):
                store.set_blob(st.session_state["data"], "stages", [])
                persist_data()
                st.session_state.pop("stage_edit", None)
                flash("已恢复默认阶段。")
                st.rerun()


        st.divider()

        # ---------- 安全边界声明 ----------
        st.markdown("##### 🔐 安全边界")
        st.caption(
            "· 联网只有一种情况：你贴链接并点「抓取」，读你点的那一页；先过 robots.txt，对方不允许就停\n\n"
            "· 不登录、不绕验证码、不伪装 UA、不翻页批量抓；天眼查/企查查这类条款禁止、已有判例的平台直接不碰\n\n"
            "· 需要登录才渲染正文的招聘站（BOSS/猎聘/前程无忧等）会提示你手动复制，抓了也是空壳\n\n"
            "· 公司工商信息仍靠你粘贴 —— 那类平台的服务条款不允许抓取\n\n"
            "· 只解析你主动上传的这一份简历，不扫描目录、不读取其它本地文件\n\n"
            "· 不发邮件、不自动投递、不向任何第三方发消息\n\n"
            "· 公司库/收藏/历史只写在本目录 data.json，不上传\n\n"
            "· 数据仍会发给你配置的模型服务商一次 —— 云端 API 绕不开这一点。"
            "开启上面的脱敏后，发出去的内容里没有你的真名、手机、邮箱、身份证"
        )

    return (
        {
            "background": (background or "").strip(),
            "preference": (preference or "").strip(),
            "resume": resume_text,
            "identity": identity or DEFAULT_IDENTITY,
            "education": education or DEFAULT_EDUCATION,
            "school_tier": school_tier or DEFAULT_SCHOOL_TIER,
        },
        resolve_config(ui_key, ui_base, ui_model),
    )


# ==========================================================================
# 七、业务助手：把分析结果与公司信息整理成可持久化的数据结构
# ==========================================================================


def scrub_enabled() -> bool:
    """脱敏开关状态。侧边栏没渲染到（理论不会）时按开启处理，宁可多脱一次。

    单独抽成函数是为了让所有调用大模型的地方都读同一个来源，
    不会出现"分析脱敏了、对比没脱敏"这种漏掉一条路径的情况。
    """
    return bool(st.session_state.get("privacy_scrub", True))


def persist_data() -> bool:
    """把 session_state 里的数据写回 data.json，失败时在页面上直接报错。"""
    data = st.session_state.get("data") or store.load()
    ok, msg = store.save(data)
    if not ok:
        st.error(msg)
    return ok


def process_mem_mb() -> float | None:
    """当前 Python 进程的常驻内存（MB）。量不出来就返回 None，不抛异常。

    优先用 psutil（装了更准）；没装时用 Windows 自带的 API 兜底，
    所以不额外加依赖也能显示数字。非 Windows 且没有 psutil 时返回 None。
    """
    try:
        import psutil  # 可选依赖，缺失不影响功能

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001  —— 没装 / 拿不到都走兜底
        pass

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return None
    return None


def flash(message: str) -> None:
    """存一条提示，下一轮渲染时显示。

    为什么不用 st.success 直接显示：保存后要 st.rerun() 才能刷新表单和列表，
    而 rerun 会把这一轮的所有提示清掉，用户就看不到"保存成功"了。
    """
    st.session_state["flash_msg"] = message


def show_flash() -> None:
    message = st.session_state.pop("flash_msg", "")
    if message:
        st.success(message)


def bump_company_form() -> None:
    """换一组新的控件 key。

    Streamlit 的输入框一旦有了 session_state 记录，就会一直用旧值。
    改 key 版本号等于重建控件，这样"编辑某家公司"时表单才会真正填进新值。
    """
    st.session_state["company_form_ver"] = st.session_state.get("company_form_ver", 0) + 1


def company_form_source(data: dict) -> dict:
    """表单要填的内容：正在编辑的公司 > 刚抽取的结果 > 空白。"""
    editing = st.session_state.get("editing_company_id", "")
    if editing:
        found = store.get_company(data, editing)
        if found:
            return {**found, "_cid": editing}
    extracted = st.session_state.get("extracted_company") or {}
    if extracted:
        return {**extracted, "_cid": ""}
    return {**store.blank_company(), "_cid": ""}


def job_label(job: dict, limit: int = 22) -> str:
    """岗位的可读标签：岗位名称打头，再跟公司；都没有就用岗位画像兜底。

    岗位名称放前面，是因为同一个公司常投好几个岗位，名字比公司更能区分记录。
    """
    parts = [p for p in (job.get("title", ""), job.get("company", "")) if p]
    text = " · ".join(parts) or str(job.get("role_summary") or "未命名岗位")
    return text[:limit]


def _age_days(job: dict) -> int:
    """记录距今多少天。时间解析不出来返回 -1，这样它永远不会被清理掉。"""
    raw = str(job.get("created") or "")[:16]
    try:
        when = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return -1
    return (datetime.now() - when).days


def split_jds(text: str, limit: int = MAX_BATCH) -> list[str]:
    """把多份 JD 按单独一行的 === 拆开，最多取 limit 份。"""
    if not (text or "").strip():
        return []
    parts = [p.strip() for p in BATCH_SEP.split(text) if p.strip()]
    return parts[:limit]


def build_job_record(
    parsed: dict,
    trimmed_jd: str,
    inputs: dict,
    data: dict,
    clean_report: dict | None = None,
) -> dict:
    """把一次分析结果组装成可持久化的岗位记录（含输入快照）。"""
    match = parsed.get("match") or {}
    raw_scores = parsed.get("scores") or {}

    clean_scores: dict[str, int] = {}
    for key, _ in intel.RADAR_DIMS:
        try:
            clean_scores[key] = max(0, min(5, int(round(float(raw_scores.get(key, 0))))))
        except (TypeError, ValueError):
            clean_scores[key] = 0

    st.session_state["bg_version"] = st.session_state.get("bg_version", 0) + 1

    # 用户手填的名称优先级最高：模型从 JD 里猜的名字经常带后缀、缩写或直接写错
    company = (str(inputs.get("company_name") or "").strip()
               or str(parsed.get("company_name") or "").strip())
    title = (str(inputs.get("job_title") or "").strip()
             or str(parsed.get("job_title") or "").strip())
    identity = str(inputs.get("identity") or "").strip()
    education = str(inputs.get("education") or "").strip()
    school_tier = str(inputs.get("school_tier") or "").strip()
    # 薪资：手填的优先，没填就用模型从 JD 里抽的
    salary = (
        str(inputs.get("salary") or "").strip()
        or str(parsed.get("salary") or "").strip()
    )

    # 自动关联到公司库里已有的公司，对比时就能把岗位和公司串起来
    company_id = ""
    if company:
        for comp in data.get("companies", []):
            name = str(comp.get("name") or "").strip()
            if name and (company in name or name in company):
                company_id = comp.get("id", "")
                break

    return {
        "company": company,
        "title": title,
        "identity": identity,
        "education": education,
        "school_tier": school_tier,
        "company_id": company_id,
        "salary": salary,
        "level": str(match.get("verdict_level") or "").strip()[:1] or "中",
        "scores": clean_scores,
        "role_summary": str(parsed.get("role_summary") or "").strip(),
        "jd_head": trimmed_jd[:80].replace("\n", " "),
        "jd_text": trimmed_jd[:2000],
        "meta": {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "bg_version": st.session_state["bg_version"],
            "inputs": dict(inputs),
            "title": title,
            "company": company,
            "identity": identity,
            "education": education,
            "school_tier": school_tier,
            "salary": salary,
            "jd_head": trimmed_jd[:80].replace("\n", " "),
            "jd_clean": (
                {
                    "deleted": clean_report["deleted_count"],
                    "before": clean_report["before_chars"],
                    "after": clean_report["after_chars"],
                }
                if clean_report
                else None
            ),
        },
        "result": parsed,
        "star": False,
    }


# ==========================================================================
# 八、右侧公司情报栏（粘贴 → AI 抽取 → 存公司库）
# ==========================================================================


def render_company_card(data: dict, comp: dict) -> None:
    """公司库里的单张卡片。收藏 / 编辑 / 删除都在卡片内完成。"""
    cid = comp.get("id", "")
    with st.container(border=True):
        star = " ⭐" if comp.get("star") else ""
        st.markdown(
            f'<div class="card-name">{html.escape(comp.get("name") or "未命名公司")}{star}</div>',
            unsafe_allow_html=True,
        )

        meta_bits = [
            f"集团：{comp['group']}" if comp.get("group") else "",
            f"注册资本：{comp['capital']}" if comp.get("capital") else "",
            f"社保：{comp['insured']}" if comp.get("insured") else "",
            f"规模：{comp['scale']}" if comp.get("scale") else "",
            f"地点：{comp['location']}" if comp.get("location") else "",
        ]
        meta_bits = [m for m in meta_bits if m]
        if meta_bits:
            st.markdown(
                '<div class="card-meta">' + "<br>".join(html.escape(m) for m in meta_bits) + "</div>",
                unsafe_allow_html=True,
            )
        if comp.get("note"):
            st.markdown(
                f'<div class="card-meta">备注：{html.escape(comp["note"])}</div>',
                unsafe_allow_html=True,
            )

        b1, b2, b3 = st.columns(3)
        if b1.button(
            "⭐" if comp.get("star") else "☆",
            key=f"star_c_{cid}",
            help="收藏 / 取消收藏",
            use_container_width=True,
        ):
            store.toggle_star(data, "company", cid)
            persist_data()
            st.rerun()

        if b2.button("✏️", key=f"edit_c_{cid}", help="修改这家公司", use_container_width=True):
            st.session_state["editing_company_id"] = cid
            st.session_state["extracted_company"] = {}
            bump_company_form()
            st.rerun()

        if b3.button("🗑", key=f"del_c_{cid}", help="删除", use_container_width=True):
            st.session_state["pending_delete"] = f"company:{cid}"

        # 删除必须二次确认：这是不可恢复的操作
        if st.session_state.get("pending_delete") == f"company:{cid}":
            st.warning("确认删除这家公司？删除后不可恢复。")
            d1, d2 = st.columns(2)
            if d1.button("确认删除", key=f"okdel_c_{cid}", use_container_width=True):
                store.remove_company(data, cid)
                if st.session_state.get("editing_company_id") == cid:
                    st.session_state["editing_company_id"] = ""
                st.session_state["pending_delete"] = ""
                persist_data()
                st.rerun()
            if d2.button("取消", key=f"nodel_c_{cid}", use_container_width=True):
                st.session_state["pending_delete"] = ""
                st.rerun()


def render_intel_panel(data: dict, llm_config: tuple[str, str, str]) -> None:
    """右侧栏：公司情报。整个面板不做任何抓取，只吃用户粘贴的文字。"""
    st.markdown("#### 🏢 公司情报")
    st.caption(
        "把企查查 / 天眼查页面上的工商信息整段复制进来，AI 抽成结构化卡片。"
        "**本工具不做抓取**，也不会访问这些网站。"
    )
    show_flash()

    with st.container(border=True):
        raw = st.text_area(
            "粘贴企业信息",
            height=110,
            key="company_raw_text",
            label_visibility="collapsed",
            placeholder="粘贴公司简介 / 工商信息那一整块：注册资本、参保人数、成立时间、注册地址……",
        )
        if intel.looks_like_jd(raw):
            st.caption("⚠️ 这段看着像岗位 JD。公司信息请粘工商信息那部分，JD 粘到左边分析区。")

        c1, c2 = st.columns([1.35, 1])
        extract_clicked = c1.button(
            "🤖 AI 抽取字段", key="btn_extract_company", use_container_width=True
        )
        if c2.button("清空", key="btn_clear_company", use_container_width=True):
            st.session_state["extracted_company"] = {}
            st.session_state["editing_company_id"] = ""
            bump_company_form()
            st.rerun()

        if extract_clicked:
            text = (raw or "").strip()
            if len(text) < 20:
                st.error("先粘贴企业信息文字，太短了抽不出东西。")
            elif not llm_config[0]:
                st.error("还没配 API Key，去「⚙️ 求职设置 → 模型配置」填。")
            else:
                problem = validate_config(llm_config)
                if problem:
                    st.error(problem)
                else:
                    with st.spinner("正在抽取公司字段…"):
                        try:
                            got = intel.extract_company(text, llm_config)
                        except Exception as exc:  # noqa: BLE001
                            got = None
                            st.error(format_api_error(exc))

                    if got:
                        if got["missing"] == ["全部字段"]:
                            st.warning(
                                "这段文字里没找到工商信息。建议把注册资本、参保人数那几行一起复制。"
                            )
                        else:
                            st.session_state["extracted_company"] = got
                            st.session_state["editing_company_id"] = ""
                            bump_company_form()
                            if got.get("missing"):
                                st.caption("未抽到：" + "、".join(got["missing"]) + "（可手动补填）")

    # ---------- 字段确认表单 ----------
    src = company_form_source(data)
    ver = st.session_state.get("company_form_ver", 0)
    is_edit = bool(src.get("_cid"))

    with st.form(f"company_form_{ver}", clear_on_submit=False):
        st.markdown("**✏️ 修改公司信息**" if is_edit else "**📝 字段确认（可改）**")
        vals: dict[str, str] = {}
        for key, label in intel.COMPANY_LABELS[:4]:
            vals[key] = st.text_input(label, value=src.get(key, ""), key=f"cf_{key}_{ver}")
        with st.expander("更多字段（规模 / 地点 / 行业 / 成立 / 状态 / 备注）"):
            for key, label in intel.COMPANY_LABELS[4:]:
                vals[key] = st.text_input(label, value=src.get(key, ""), key=f"cf_{key}_{ver}")
            vals["note"] = st.text_input("我的备注", value=src.get("note", ""), key=f"cf_note_{ver}")

        submitted = st.form_submit_button(
            "💾 保存到公司库", type="primary", use_container_width=True
        )

    if submitted:
        if not (vals.get("name") or "").strip():
            st.error("公司名称不能为空。")
        else:
            vals["source"] = "ai" if st.session_state.get("extracted_company") else "manual"
            rec = store.upsert_company(data, vals, cid=src.get("_cid", ""))
            if persist_data():
                st.session_state["extracted_company"] = {}
                st.session_state["editing_company_id"] = ""
                # 注意：不能给 company_raw_text 赋值 —— 它已经作为 widget 实例化，
                # 运行中途改它的 session_state 会直接抛 StreamlitAPIException
                bump_company_form()
                flash(f"已保存：{rec.get('name')}")
                st.rerun()

    # ---------- 公司库列表 ----------
    companies = data.get("companies", [])
    star_count = sum(1 for c in companies if c.get("star"))
    st.markdown(f"**公司库**　{len(companies)} 家　·　收藏 {star_count} 家")

    if not companies:
        st.caption("还没有公司。上面粘一段工商信息，点「AI 抽取字段」试试。")
        return

    with st.container(height=430):
        for comp in companies:
            render_company_card(data, comp)

    st.download_button(
        "⬇️ 导出公司库（CSV）",
        data=store.companies_to_csv(data),
        file_name="公司库.csv",
        mime="text/csv",
        use_container_width=True,
        key="dl_companies_csv",
    )


# ==========================================================================
# 九、主区各标签页
# ==========================================================================


# --------------------------------------------------------------------------
# 分析任务：丢给后台线程跑，页面随便点、随便切，不会被打断
# --------------------------------------------------------------------------
# 为什么必须这么做：Streamlit 每点一次控件就把整个脚本重跑一遍，正在等模型
# 返回的那次调用会被直接掐断。用户只是想去看一眼日历、点个收藏，分析就白跑了
# （token 照扣）。所以请求本身放在线程里，进度写进下面这个模块级字典，
# 页面每一轮只负责"读进度、显示进度"。
JOB_KEY = "analysis_job"


def current_job() -> dict | None:
    """当前后台任务的状态。

    状态存 session_state，不是模块级变量：AppTest / 热重载会重建模块，
    模块级 dict 一重建，正在跑的任务就"消失"了（真实踩过：点了没反应）。
    线程拿到的是这个 dict 的引用，直接往里写，主线程每轮读同一份。
    """
    job = st.session_state.get(JOB_KEY)
    return job if isinstance(job, dict) and job else None


def analysis_running() -> bool:
    job = current_job()
    return bool(job and job.get("status") == "running")


def _analysis_worker(
    job: dict,
    blocks: list[str],
    inputs: dict[str, str],
    llm_config: tuple[str, str, str],
    data: dict,
    bank: list[dict],
    scrub: bool,
    batch: bool,
) -> None:
    """线程体。这里一个 st.* 都不能调 —— 线程里没有 ScriptRunContext。

    job 是主线程建好的同一个 dict，线程只往里写进度，不碰 session_state。
    """

    total = len(blocks)
    try:
        for idx, block in enumerate(blocks, start=1):
            trimmed = block[:MAX_JD_CHARS]
            clean_report = prep.clean_jd(trimmed)

            # 实习 / 校招两套口径：这一份 JD 属于哪类，就挑哪套偏好去比对
            block_inputs = dict(inputs)
            block_inputs["preference"] = compose_preference(inputs, detect_kind(trimmed))

            job["label"] = (
                f"第 {idx}/{total} 份：正在等模型返回（通常 30-90 秒）"
                if total > 1
                else "正在拆解 JD、比对背景、生成简历与面试建议"
            )
            raw, mapping = call_llm(block_inputs, trimmed, llm_config, scrub=scrub, bank=bank)

            parsed = None
            if raw:
                try:
                    parsed = privacy.restore_obj(parse_json_response(raw), mapping)
                except Exception:  # noqa: BLE001 —— 解析失败只记这一份，不毁掉整批
                    parsed = None
                raw = privacy.restore(raw, mapping) or ""

            if mapping:
                note = privacy.report(mapping)
                if note and note not in job["scrub_notes"]:
                    job["scrub_notes"].append(note)

            if parsed:
                record = build_job_record(parsed, trimmed, block_inputs, data, clean_report)
                job["created"].append(store.add_job(data, record))
                # 每份单独落盘：后面几份翻车，前面已完成的不跟着丢
                store.save(data)
            elif raw:
                job["bad_raw"].append(raw[:1500])
            job["done"] = idx
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["errors"].append(format_api_error(exc))
        job["status"] = "error"
    finally:
        job["ended"] = time.time()


def run_analysis(
    blocks: list[str],
    inputs: dict[str, str],
    llm_config: tuple[str, str, str],
    data: dict,
    batch: bool = False,
) -> None:
    """校验输入，然后把实际请求交给后台线程。"""
    if not inputs["background"]:
        st.error("左侧【个人背景】是空的，先补上你的背景再分析。")
        return
    if not blocks:
        st.error("还没粘贴 JD，请把招聘 JD 原文粘贴到上面输入框。")
        return

    too_short = [b for b in blocks if len(b) < 30]
    if too_short:
        st.error(f"有 {len(too_short)} 份 JD 少于 30 字，请粘贴完整的岗位职责与任职要求。")
        return

    if not llm_config[0]:
        st.warning(
            "还没配置 API Key。请在左侧 **⚙️ 模型配置** 里填入，"
            "或写入 `.streamlit/secrets.toml`（参考 README.md）。"
        )
        return

    problem = validate_config(llm_config)
    if problem:
        # 粘贴事故（中文、空格、全角符号）在本地就拦住，不浪费请求
        st.error(problem)
        return

    if analysis_running():
        st.warning(
            "上一次分析还在跑（进度条在下面）。等它跑完再点 —— "
            "同时发两份不会更快，只会两边都慢、而且都计费。"
        )
        return

    # 同公司/同类岗位的真题，随请求一起发给模型，让它优先覆盖真被问过的题
    bank = store.matched_questions(
        data,
        inputs.get("company_name", ""),
        inputs.get("job_title", ""),
    )

    # 查重提示：这轮放不进线程，先存起来，下一轮渲染时显示
    dupes = prep.similar_jobs(
        {"jd_text": blocks[0][:2000], "company": inputs.get("company_name", "")},
        data.get("jobs", []),
        threshold=0.55,
    )
    if dupes:
        best = dupes[0]
        note = (
            f"⚠️ 这份 JD 和你已记录的「{job_label(best['job'])}」"
            f"相似度 {best['score']:.0%}"
        )
        if best["same_company"]:
            note += "（同一家公司）"
        note += "。可能是同一岗位重发，也可能是改个标题的复制粘贴版 —— 要我继续分析也行，只是别重复投。"
        st.session_state["analysis_notice"] = note

    job = {
        "status": "running",
        "total": len(blocks),
        "done": 0,
        "created": [],
        "errors": [],
        "bad_raw": [],
        "scrub_notes": [],
        "bank": len(bank),
        "label": "正在准备请求…",
        "batch": batch,
        "started": time.time(),
        "ended": 0.0,
    }
    st.session_state[JOB_KEY] = job
    threading.Thread(
        target=_analysis_worker,
        args=(job, list(blocks), dict(inputs), llm_config, data, bank, scrub_enabled(), batch),
        daemon=True,
        name="jd-analysis",
    ).start()
    st.rerun()


@st.fragment(run_every=2.0)
def render_analysis_progress(data: dict) -> None:
    """只刷这一小块，不去打断整页输入。

    run_every 让它在任务进行中每 2 秒自己刷一次；任务结束后把结果交接给
    页面状态，再整页重跑一次，把分析结果渲染出来。
    """
    job = current_job()
    if not job:
        return

    status = job.get("status")
    if status == "running":
        total = max(1, int(job.get("total") or 1))
        done = int(job.get("done") or 0)
        st.progress(
            min(done / total, 1.0),
            text=f"⏳ {job.get('label', '分析中…')}　·　已完成 {done}/{total}",
        )
        st.caption(
            "任务在后台跑：可以切到日历、收藏、设置随便点，回来结果还在，不会中断。"
        )
        return

    # ---- 已结束：把结果交接出去，然后整页重跑 ----
    created = job.get("created") or []
    if created:
        first_id = created[0].get("id")
        if first_id:
            st.session_state["latest_job_id"] = first_id
        st.session_state["batch_compare_ids"] = (
            [j.get("id") for j in created] if len(created) > 1 else []
        )
        st.session_state["latest_job_fresh"] = True
        flash(f"✅ 完成 {len(created)} 份分析，已存入历史记录。")

    for msg in job.get("errors") or []:
        st.error(msg)
        st.caption("可以先去「⚙️ 求职设置」里的 **模型配置 → 🔍 测试连接** 单独验证配置。")

    for raw in job.get("bad_raw") or []:
        st.error("有一份模型返回的内容不是合法 JSON，原始返回贴在下面。")
        with st.expander("查看原始返回", expanded=False):
            st.text(raw[:1500])

    if job.get("scrub_notes"):
        st.caption(
            f"🔒 发送前已在本地脱敏：{'；'.join(job['scrub_notes'])}。"
            "返回后已在本机还原，页面显示的是真名。"
        )

    # 收尾前留一份结论：切页回来、或者出了问题，都还能看到上一轮到底怎么了
    st.session_state["analysis_last"] = {
        "status": status,
        "errors": list(job.get("errors") or []),
        "created": len(created),
        "ended": job.get("ended") or 0.0,
    }
    st.session_state.pop(JOB_KEY, None)
    st.rerun(scope="app")

def render_job_compare(jobs: list[dict]) -> None:
    """岗位并排对比表。数据全部来自已存结果，不再调模型。"""
    jobs = [j for j in jobs if j]
    if len(jobs) < 2:
        st.caption("至少 2 个岗位才能对比。")
        return

    headers = ["维度"] + [job_label(j, 24) for j in jobs]
    # 先把"这是哪个岗位、投的时候我是什么身份"摊清楚，再比分数
    rows: list[list[str]] = [
        ["岗位名称"] + [j.get("title") or "—" for j in jobs],
        ["公司"] + [j.get("company") or "—" for j in jobs],
        ["我的身份"] + [j.get("identity") or "—" for j in jobs],
        ["整体匹配度"] + [j.get("level") or "-" for j in jobs],
    ]

    for key, label in intel.RADAR_DIMS:
        rows.append([label] + [f"{(j.get('scores') or {}).get(key, 0)} / 5" for j in jobs])

    rows.append(["岗位画像"] + [j.get("role_summary") or "—" for j in jobs])
    rows.append(
        ["硬性要求条数"]
        + [
            str(len((j.get("result", {}).get("jd_breakdown") or {}).get("hard") or []))
            for j in jobs
        ]
    )
    rows.append(
        ["首要短板"]
        + [
            " / ".join(
                t for t, _ in normalize_items(
                    (j.get("result", {}).get("match") or {}).get("gaps")
                )[:1]
            )
            or "—"
            for j in jobs
        ]
    )
    rows.append(["分析时间"] + [j.get("created") or "-" for j in jobs])

    st.markdown(intel.compare_html(headers, rows), unsafe_allow_html=True)


def render_latest_job(data: dict, llm_config: tuple[str, str, str]) -> None:
    """渲染最近一次分析结果（从 data.json 里取，不重算）。

    结果下方挂着四件常用的事：改名字、只重生成一块、就结果追问、导出。
    这些都不重新读 JD 之外的任何文件，也不往外部发东西（追问除外，会调一次模型）。
    """
    jid = st.session_state.get("latest_job_id", "")
    job = store.get_job(data, jid) if jid else None
    if not job:
        return

    st.divider()
    st.markdown(f"### 📊 分析结果　·　{job_label(job, 40)}")
    # 初始化时已经 setdefault 过 quiz_mode，这里只绑 key 不再传 value，
    # 否则会触发「widget 同时有默认值和 session_state」的告警
    quiz = st.toggle("🎯 面试自测模式（先只看题，自己答完再展开思路）", key="quiz_mode")
    render_result(
        job["result"],
        job["meta"],
        quiz_mode=quiz,
        title_override=job.get("title", ""),
        company_override=job.get("company", ""),
    )

    stamp = (job.get("created") or "").replace(":", "").replace(" ", "_")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button(
            "⬇️ 导出 Markdown",
            data=build_markdown(job["result"], job["meta"]),
            file_name=f"JD分析_{stamp}.md",
            mime="text/markdown",
            use_container_width=True,
            key=f"dl_job_{job['id']}",
        )
    with c2:
        try:
            docx_bytes = build_docx(job["result"], job["meta"])
        except Exception:  # noqa: BLE001  缺 python-docx 或写入异常都不该阻断页面
            docx_bytes = b""
        if docx_bytes:
            st.download_button(
                "⬇️ 导出 Word",
                data=docx_bytes,
                file_name=f"JD分析_{stamp}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
                key=f"dl_docx_{job['id']}",
            )
        else:
            st.button("导出 Word（缺 python-docx）", disabled=True, use_container_width=True)
    with c3:
        starred = bool(job.get("star"))
        if st.button(
            "⭐ 已收藏" if starred else "☆ 收藏这个岗位",
            key=f"star_latest_{job['id']}",
            use_container_width=True,
        ):
            store.toggle_star(data, "job", job["id"])
            persist_data()
            st.rerun()

    # ---------- 改名字（模型猜错时不用重跑） ----------
    with st.expander("✏️ 改岗位名称 / 公司（不用重跑分析）"):
        e1, e2 = st.columns(2)
        new_title = e1.text_input(
            "岗位名称", value=job.get("title", ""), key=f"edit_title_{job['id']}"
        )
        new_comp = e2.text_input(
            "公司", value=job.get("company", ""), key=f"edit_comp_{job['id']}"
        )
        if st.button("保存名字", key=f"save_name_{job['id']}"):
            job["title"] = (new_title or "").strip()
            job["company"] = (new_comp or "").strip()
            job["meta"]["title"] = job["title"]
            job["meta"]["company"] = job["company"]
            persist_data()
            flash("✅ 名称已更新")
            st.rerun()

    # ---------- 只重生成一块 ----------
    with st.expander("🔁 只重生成某一个板块（比重跑整份省一半以上）"):
        pick = st.selectbox(
            "要重做的板块",
            list(SECTION_LABELS),
            format_func=lambda k: SECTION_LABELS[k],
            key=f"regen_pick_{job['id']}",
        )
        st.caption(
            "用保存的 JD 原文和当时的输入快照重算这一块，其它板块保持不动。"
            "适合「简历改完了想重新给建议」「只想要更多面试题」这种场景。"
        )
        if st.button("重生成这一块", key=f"regen_btn_{job['id']}", use_container_width=True):
            if not llm_config[0]:
                st.warning("还没配置 API Key，去「⚙️ 求职设置 → 模型配置」填。")
            elif validate_config(llm_config):
                st.error(validate_config(llm_config))
            else:
                with st.spinner(f"正在重生成「{SECTION_LABELS[pick]}」…"):
                    ok, msg = regenerate_section(job, pick, llm_config)
                if ok:
                    persist_data()
                    flash(f"✅ {msg}")
                    st.rerun()
                else:
                    st.error(msg)

    # ---------- 就结果追问 ----------
    with st.expander("💬 就这份结果继续问（每次 1 次模型调用）"):
        chat_key = f"chat_{job['id']}"
        history = st.session_state.setdefault(chat_key, [])
        for role, text in history:
            if role == "user":
                st.markdown(f"**你：** {text}")
            else:
                st.markdown(text)
                st.caption(SAFETY_FOOTER)
        question = st.text_input(
            "问点什么",
            key=f"q_{job['id']}",
            placeholder="例如：这条短板面试被问到时怎么答？／薪资怎么谈？",
        )
        if st.button("发送", key=f"ask_{job['id']}"):
            if not question.strip():
                st.caption("先写个问题。")
            elif not llm_config[0]:
                st.warning("还没配置 API Key。")
            elif validate_config(llm_config):
                st.error(validate_config(llm_config))
            else:
                with st.spinner("正在回答…"):
                    ok, msg = ask_followup(job, question.strip(), llm_config)
                if ok:
                    history.append(("user", question.strip()))
                    history.append(("assistant", msg))
                    st.session_state[chat_key] = history
                    st.rerun()
                else:
                    st.error(msg)

    with st.expander("查看本次分析使用的输入快照（改侧边栏不影响这份结果）"):
        snap = job["meta"].get("inputs") or {}
        st.caption("我的身份 / 学历")
        st.text(
            f"{snap.get('identity') or '（未设置）'}　·　"
            f"{snap.get('education') or '未填'}　·　{snap.get('school_tier') or '不填'}"
        )
        st.caption("岗位名称 / 公司（本次填写的）")
        st.text(f"{job.get('title') or '（未填）'} / {job.get('company') or '（未填）'}")
        st.caption("个人背景")
        st.text(snap.get("background", "") or "（空）")
        st.caption("求职偏好与性格")
        st.text(snap.get("preference", "") or "（未填写）")
        resume_snap = snap.get("resume") or ""
        st.caption(f"简历（{len(resume_snap)} 字）")
        st.text(resume_snap[:1500] or "（未提供）")
    st.caption("下载只落到你的本机，不会上传；追问会调用一次你自己配置的模型。")

    # 批量分析刚跑完时，顺手把这几份并排比一下
    batch_ids = st.session_state.get("batch_compare_ids") or []
    if len(batch_ids) > 1:
        jobs = [store.get_job(data, i) for i in batch_ids]
        jobs = [j for j in jobs if j]
        if len(jobs) > 1:
            st.divider()
            st.markdown("### ⚖️ 这几份 JD 放在一起看")
            render_job_compare(jobs)
            st.caption("想看更多组合，去「⚖️ 对比分析」页自由挑选。")


def render_jd_import(mode: str) -> None:
    """从招聘页链接导入 JD。

    顺序刻意做成「先查 → 再抓 → 给你看 → 你确认才填」：
    不替用户决定要不要用某一页的内容，也不偷偷把抓来的文字直接喂给模型。
    """
    target = "jd_input" if mode == "单份分析" else "jd_input_multi"

    with st.expander("🔗 从招聘页链接导入 JD（先查对方 robots.txt）"):
        st.caption(
            "只抓对方放行的那一页。天眼查 / 企查查这类条款明确禁止、且已有判例的平台会直接拒绝；"
            "BOSS直聘、猎聘等要登录才渲染出正文的站也会提前告诉你，让你手动复制 —— 那样反而更准。"
        )

        col_url, col_btn = st.columns([3, 1])
        with col_url:
            draft_restore("jd_import_url")
            url = st.text_input(
                "招聘页链接",
                key="jd_import_url",
                label_visibility="collapsed",
                placeholder="粘贴招聘详情页的链接，例如 https://company.com/careers/123",
            )
            draft_keep("jd_import_url")
        with col_btn:
            go = st.button("抓取", key="jd_import_btn", use_container_width=True)

        if go:
            if not url.strip():
                st.warning("先粘一个链接进来。")
            else:
                with st.spinner("正在读 robots.txt 并抓取这一页…"):
                    st.session_state["jd_import_result"] = fetch_jd.fetch(url.strip())

        res = st.session_state.get("jd_import_result")
        if not res:
            return

        if res.get("ok"):
            st.success(f"抓到 {res.get('chars', 0)} 字　·　{res.get('note', '')}")
            if res.get("title"):
                st.caption(f"页面标题：{res['title']}")
            st.text_area(
                "抓到的正文（先看一眼，确认是这份 JD 再填）",
                value=res.get("text", ""),
                height=200,
                disabled=True,
            )
            if st.button("⬇️ 填入上面的 JD 输入框", key="jd_import_fill", type="primary"):
                old = draft_value(target).strip()
                filled = (
                    f"{old}\n=====\n{res['text']}" if old and target == "jd_input_multi" else res["text"]
                )
                st.session_state[target] = filled
                _drafts()[target] = filled
                st.session_state["__drafts_dirty"] = True
                st.session_state.pop("jd_import_result", None)
                st.rerun()
        else:
            st.error(res.get("message", "这一页没抓到。"))
            st.caption(
                "抓不到很正常 —— 这类站正文要登录后才渲染。"
                "往下用「📋 从网页文字提取」：在页面上全选复制，粘进去就能自动拆出岗位、公司、薪资和正文。"
            )


def render_page_extract(mode: str) -> None:
    """从「整页复制的网页文字」里拆字段。

    为什么需要它：BOSS / 猎聘 / 51job 的正文都在登录后渲染，程序抓回来是空壳
    （也不该去绕）。但你自己在浏览器里全选复制是拿得到的 —— 只是那一坨里夹着
    导航栏、按钮、推荐位、公司信息、页脚，直接喂给模型会把噪声一起带进去。

    这里用本地正则先拆成「岗位名称 / 公司名称 / 薪资 / 地点 / 学历经验 / 福利 /
    JD 正文」，你核对一眼再填进输入框。省掉手填岗位名和手动划 JD 段落。

    必须放在下面那几个输入框**渲染之前** —— 点了「填入」要改它们的
    session_state，widget 已经实例化之后再改会直接报错。
    """
    target = "jd_input" if mode == "单份分析" else "jd_input_multi"
    has_jd = bool(draft_value(target).strip())

    with st.expander("📋 从网页文字提取（整页复制，自动拆出岗位/公司/薪资/正文）", expanded=not has_jd):
        st.caption(
            "在招聘页按 `Ctrl+A` 全选、`Ctrl+C` 复制，整段粘进来。"
            "岗位名称、公司名称、薪资、地点、学历经验、JD 正文会自动拆开，你核对后再填 —— "
            "全部本地正则，不发请求、不调模型，也不用登录。"
        )

        draft_restore("page_extract_raw")
        raw = st.text_area(
            "粘贴整页文字",
            height=160,
            key="page_extract_raw",
            placeholder="把招聘页上全选复制的内容整段粘到这里（夹带的导航栏、按钮、页脚不用管）",
        )
        draft_keep("page_extract_raw")

        c_go, c_clear = st.columns([2, 1])
        with c_go:
            go = st.button(
                "🔍 提取字段", key="page_extract_btn", type="primary", use_container_width=True
            )
        with c_clear:
            if st.button("🧹 清空重来", key="page_extract_clear", use_container_width=True):
                st.session_state["page_extract_raw"] = ""
                st.session_state["page_extract_jd_edit"] = ""
                _drafts().pop("page_extract_raw", None)
                st.session_state["__drafts_dirty"] = True
                st.session_state.pop("page_extract_result", None)
                st.rerun()

        if go:
            if not raw.strip():
                st.warning("先把网页文字粘进来。")
            else:
                with st.spinner("正在拆字段…"):
                    st.session_state["page_extract_result"] = extract.extract(raw)
                # 重新提取时，旧的可编辑副本要丢掉，否则用户会看到上一次的正文
                st.session_state.pop("page_extract_jd_edit", None)

        result = st.session_state.get("page_extract_result")
        if not result:
            return

        if not result.get("ok"):
            st.warning(result.get("message") or "没提取到正文。")
            st.caption(
                "多半是只复制到了标题栏。回到招聘页，从岗位名一直拖到页面底部再复制；"
                "实在不行就直接把 JD 正文粘到下面的输入框，其余字段手填两行。"
            )
            return

        # ---------- 拆出来的字段 ----------
        bits = []
        if result.get("title"):
            bits.append(f"**岗位名称**　{result['title']}")
        if result.get("company"):
            bits.append(f"**公司名称**　{result['company']}")
        if result.get("salary"):
            bits.append(f"**薪资待遇**　{result['salary']}")
        if result.get("location"):
            bits.append(f"**工作地点**　{result['location']}")
        if result.get("experience"):
            bits.append(f"**经验要求**　{result['experience']}")
        if result.get("education"):
            bits.append(f"**学历要求**　{result['education']}")
        if result.get("benefits"):
            bits.append(f"**福利**　{'、'.join(result['benefits'])}")

        if bits:
            st.success(f"拆出 {len(bits)} 项，核对一下再填：")
            st.markdown("　·　".join(bits))
        else:
            st.warning("这几项都没拆出来，只能把正文填进去。")

        if result.get("missing"):
            st.caption(
                f"没拆到：{'、'.join(result['missing'])}"
                "　（拆不到就手填，或者让模型从正文里自己读 —— 别硬塞错的值）"
            )

        st.caption(
            f"原文 {result.get('chars', 0)} 字 → JD 正文 {result.get('jd_chars', 0)} 字"
            f"　·　{result.get('jd_note', '')}"
        )

        # 正文给成可编辑的，抽歪了你当场就能改
        if "page_extract_jd_edit" not in st.session_state:
            st.session_state["page_extract_jd_edit"] = result.get("jd", "")
        jd_edit = st.text_area(
            "提取到的 JD 正文（可以直接改）",
            height=180,
            key="page_extract_jd_edit",
        )

        if st.button("⬇️ 填入下面的输入框", key="page_extract_fill", type="primary"):
            body = (jd_edit or "").strip()
            if not body:
                st.warning("正文是空的，没法填。")
                return
            if target == "jd_input_multi":
                old = draft_value(target).strip()
                filled = f"{old}\n=====\n{body}" if old else body
            else:
                filled = body
            st.session_state[target] = filled
            _drafts()[target] = filled
            # 三个元信息框：这次抽到什么就写什么，抽不到就清空。
            # 早先做过"手填值不覆盖"的保护，结果更糟：草稿里留着上一家的公司名，
            # 新 JD 明明没写公司，框里却挂着旧公司（实测把福利词「补充医疗保险」当了公司）。
            # 点「填入」本身就是"我要用这次结果"的明确动作，所以这里一律覆盖。
            overwritten = []
            for key, value in (
                ("jd_title_input", result.get("title")),
                ("jd_company_input", result.get("company")),
                ("jd_salary_input", result.get("salary")),
            ):
                if mode != "单份分析":
                    continue
                current = str(st.session_state.get(key) or "").strip()
                fresh = str(value or "").strip()
                if current and current != fresh:
                    overwritten.append(key)
                st.session_state[key] = fresh
                _drafts()[key] = fresh
            st.session_state["__drafts_dirty"] = True
            if overwritten:
                st.session_state["extract_overwrite_note"] = (
                    f"已按这次的提取结果更新 {len(overwritten)} 个字段"
                    "（岗位/公司/薪资以这次为准；要保留原来手填的，下次先别点「填入」）。"
                )
            st.session_state.pop("page_extract_result", None)
            st.success("已填入下方的岗位名称 / 公司名称 / JD 输入框。")
            st.rerun()


def render_analyze_tab(inputs: dict[str, str], llm_config, data: dict) -> None:
    """岗位分析页：单份 / 多份两种模式。"""
    st.markdown("#### 📄 粘贴岗位 JD")

    draft_restore("jd_mode")
    mode = st.radio(
        "分析模式",
        ["单份分析", "多份对比"],
        horizontal=True,
        key="jd_mode",
        label_visibility="collapsed",
        help="多份对比：一次粘 2-3 份 JD，逐份分析后自动生成并排对比",
    )
    draft_keep("jd_mode")

    analysis_inputs = dict(inputs)

    # 这两个面板都放在输入框之前：此时输入框还没实例化，
    # 才能安全地写它们的 session_state（实例化后再改会直接报错）
    render_jd_import(mode)
    render_page_extract(mode)

    if mode == "单份分析":
        # 岗位名称 / 公司名称单独列出来：模型从 JD 里猜的名字经常带后缀或直接写错，
        # 填了就以你写的为准，历史记录和收藏里也更好认
        # 岗位名称 / 公司名称 / 薪资 各自一栏。薪资单独拎出来，是因为它最常被漏标，
        # 又最影响判断 —— 日薪 180 和 8-12K·14 薪根本不是一个决策
        name_col, comp_col, pay_col = st.columns([1.2, 1.2, 0.9])
        with name_col:
            draft_restore("jd_title_input")
            title_in = st.text_input(
                "岗位名称（选填）",
                key="jd_title_input",
                placeholder="如：财务管培生 / 战略投资分析实习生",
                help="填了就用你写的，模型不再自己猜；留空则从 JD 原文取，原文没写就标「未写」。",
            )
            draft_keep("jd_title_input")
        with comp_col:
            draft_restore("jd_company_input")
            company_in = st.text_input(
                "公司名称（选填）",
                key="jd_company_input",
                placeholder="如：四川某某新能源股份有限公司",
                help="填了会写进记录，并自动对上右侧公司库里已有的公司。",
            )
            draft_keep("jd_company_input")
        with pay_col:
            draft_restore("jd_salary_input")
            salary_in = st.text_input(
                "薪资待遇（选填）",
                key="jd_salary_input",
                placeholder="8-12K·14薪 / 180 元/天",
                help="填了就用你写的，盖掉模型从 JD 里猜的；显示在岗位卡和投递追踪里。"
                     "留空则用 JD 原文写的那个。",
            )
            draft_keep("jd_salary_input")
        analysis_inputs.update(
            {
                "job_title": (title_in or "").strip(),
                "company_name": (company_in or "").strip(),
                "salary": (salary_in or "").strip(),
            }
        )

        draft_restore("jd_input")
        jd_text = st.text_area(
            "岗位JD原文",
            height=220,
            placeholder="粘贴完整招聘JD原文",
            label_visibility="collapsed",
            key="jd_input",
        )
        draft_keep("jd_input")
        blocks = [jd_text.strip()] if jd_text.strip() else []
    else:
        draft_restore("jd_input_multi")
        jd_text = st.text_area(
            "多份JD原文",
            height=260,
            label_visibility="collapsed",
            key="jd_input_multi",
            placeholder=(
                "把几份 JD 依次粘进来，每份之间放一行 ===== 作为分隔（最多 3 份）\n\n"
                "第一份 JD 全文……\n"
                "=====\n"
                "第二份 JD 全文……"
            ),
        )
        draft_keep("jd_input_multi")
        blocks = split_jds(jd_text)
        total_parts = len([p for p in BATCH_SEP.split(jd_text) if p.strip()])
        if total_parts > MAX_BATCH:
            st.caption(
                f"⚠️ 你贴了 {total_parts} 份，这里只分析前 {MAX_BATCH} 份；"
                "剩下的删掉已分析的再来一轮。"
            )

    char_count = sum(len(b) for b in blocks)
    est_tokens = max(1, int(char_count / 1.5)) if char_count else 0
    info_col, btn_col = st.columns([3, 1])
    with info_col:
        if mode == "单份分析":
            st.caption(
                f"当前 {char_count} 字（约 {est_tokens} tokens）　·　上限 {MAX_JD_CHARS} 字　·　"
                "支持岗位职责 + 任职要求 + 公司简介全文粘贴"
            )
        else:
            st.caption(
                f"识别到 {len(blocks)} 份 JD　·　合计 {char_count} 字（约 {est_tokens} tokens）　·　"
                f"最多 {MAX_BATCH} 份　·　会调用 {len(blocks)} 次模型（每份都单独分析）"
            )
        if char_count > MAX_JD_CHARS:
            st.caption(f"⚠️ 超出 {MAX_JD_CHARS} 字上限，只分析前 {MAX_JD_CHARS} 字，后面的会被截掉。")
        elif char_count > 3000:
            st.caption(
                "文本偏长：单次请求的上下文变大，模型要 30-90 秒才返回，"
                "这段时间页面在转圈，别重复点（每次点击都是一次真实计费调用）。"
            )
        if URL_PATTERN.search(jd_text):
            st.caption(
                "⚠️ 文本里夹带了链接。这里只分析你粘贴的文字，不会因为文中有链接就去访问它；"
                "想把那一页抓下来，用上面的「🔗 从招聘页链接导入」。"
            )
    with btn_col:
        analyze_clicked = st.button(
            "🚀 开始分析岗位", key="analyze_go", type="primary", use_container_width=True
        )

    # 分析原理：一行五步，细节折叠。放在按钮下方，不抢输入区的位置
    render_how_it_works()

    is_batch = mode != "单份分析"

    if analyze_clicked:
        if not blocks:
            st.error("还没粘贴 JD，请把招聘 JD 原文粘贴到上面输入框。")
        elif st.session_state.get("preview_send"):
            # 预览模式：先组装好要发的文本（含脱敏），停下来让用户核对，确认后才真发
            bank_preview = store.matched_questions(
                data,
                analysis_inputs.get("company_name", ""),
                analysis_inputs.get("job_title", ""),
            )
            # 预览必须和实发一致：这里也要按 JD 类型挑校招 / 实习口径，
            # 否则「预览看到的是校招口径，真发出去的是实习口径」
            preview_inputs = dict(analysis_inputs)
            preview_inputs["preference"] = compose_preference(
                analysis_inputs, detect_kind(blocks[0])
            )
            prompt, _ = build_request(
                preview_inputs,
                blocks[0][:MAX_JD_CHARS],
                scrub=scrub_enabled(),
                bank=bank_preview,
            )
            st.session_state["pending_send"] = {
                "prompt": prompt,
                "blocks": [b[:MAX_JD_CHARS] for b in blocks],
                "inputs": dict(analysis_inputs),
                "batch": is_batch,
            }
            st.rerun()
        else:
            run_analysis(blocks, analysis_inputs, llm_config, data, batch=is_batch)

    pending = st.session_state.get("pending_send")
    if pending:
        n_pending = len(pending["blocks"])
        head = "🔍 将要发送给模型的内容（已脱敏）"
        if n_pending > 1:
            head += f"　·　共 {n_pending} 份，下面展示第 1 份"
        with st.expander(head, expanded=True):
            st.code(pending["prompt"], language="text")
            st.caption(
                "上面这段就是请求里属于你的全部内容。系统提示词是固定的分析规则，不含你的信息。"
                "真名 / 手机 / 邮箱 / 身份证 / 院校 / 公司全称已在本机替换成 [P1] 这类占位符，"
                "模型返回后自动还原 —— 你能看到真名，模型看不到。"
                "确认没问题再点发送；不放心就取消，改用本地模型。"
            )
        cc1, cc2 = st.columns(2)
        if cc1.button(
            "✅ 核对无误，发送", type="primary", key="btn_confirm_send", use_container_width=True
        ):
            send_blocks = list(pending["blocks"])
            send_inputs = dict(pending["inputs"])
            send_batch = bool(pending.get("batch"))
            st.session_state["pending_send"] = None
            run_analysis(send_blocks, send_inputs, llm_config, data, batch=send_batch)
        if cc2.button("取消，先不发", key="btn_cancel_send", use_container_width=True):
            st.session_state["pending_send"] = None
            st.rerun()

    # 后台分析任务的进度条 / 结果交接。没有任务时它什么都不画
    render_analysis_progress(data)

    notice = st.session_state.pop("analysis_notice", "")
    if notice:
        st.warning(notice)

    if analysis_running():
        # 正在跑的时候不摆"三步开始"的空状态，免得让人以为没点成功
        return

    if not st.session_state.get("latest_job_id"):
        st.divider()
        st.info(
            "三步开始：① 上面粘贴 JD 原文（岗位名称选填，填了更准）　→　"
            "② 点「🚀 开始分析岗位」　→　③ 五个板块的结果出现在这里，并自动存进历史记录。"
        )
        return

    render_latest_job(data, llm_config)


def render_favorites_tab(data: dict) -> None:
    """收藏夹：公司、岗位分两栏。"""
    comps = [c for c in data.get("companies", []) if c.get("star")]
    jobs = [j for j in data.get("jobs", []) if j.get("star")]

    st.markdown(f"### ⭐ 收藏夹　·　公司 {len(comps)} 家　·　岗位 {len(jobs)} 个")
    st.caption("收藏只存在本机 data.json 里，不上传、不联网。")

    left, right = st.columns(2)

    with left:
        sub_header("🏢 收藏的公司")
        if not comps:
            st.caption("还没有收藏的公司。在右侧公司情报栏点 ☆ 即可。")
        for comp in comps[:20]:
            with st.container(border=True):
                st.markdown(
                    f'<div class="card-name">{html.escape(comp.get("name") or "未命名")}</div>',
                    unsafe_allow_html=True,
                )
                line = "　·　".join(
                    x for x in [comp.get("capital"), comp.get("insured"), comp.get("location")] if x
                )
                if line:
                    st.caption(line)
                if st.button(
                    "取消收藏",
                    key=f"unstar_c_{comp['id']}",
                    use_container_width=True,
                ):
                    store.toggle_star(data, "company", comp["id"])
                    persist_data()
                    st.rerun()

    with right:
        sub_header("💼 收藏的岗位")
        if not jobs:
            st.caption("还没有收藏的岗位。分析完点「☆ 收藏这个岗位」即可。")
        for job in jobs[:20]:
            with st.container(border=True):
                st.markdown(
                    f'<div class="card-name">{html.escape(job_label(job, 34))}</div>',
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"匹配度 {job.get('level') or '-'}　·　{job.get('created') or '-'}"
                    + (f"　·　{job.get('identity')}" if job.get("identity") else "")
                )
                c1, c2 = st.columns(2)
                if c1.button("查看结果", key=f"fav_view_{job['id']}", use_container_width=True):
                    st.session_state["view_job_id"] = job["id"]
                    st.session_state["view_origin"] = "fav"
                    st.rerun()
                if c2.button("取消收藏", key=f"fav_unstar_{job['id']}", use_container_width=True):
                    store.toggle_star(data, "job", job["id"])
                    persist_data()
                    st.rerun()

    if comps or jobs:
        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "⬇️ 导出收藏的公司（CSV）",
                data=store.companies_to_csv(data, only_star=True),
                file_name="收藏的公司.csv",
                mime="text/csv",
                use_container_width=True,
                key="dl_fav_companies",
            )
        with c2:
            st.download_button(
                "⬇️ 导出收藏的岗位（CSV）",
                data=store.jobs_to_csv(data, only_star=True),
                file_name="收藏的岗位.csv",
                mime="text/csv",
                use_container_width=True,
                key="dl_fav_jobs",
            )

    render_view_slot(data, "fav")


COMPARE_SYSTEM_PROMPT = (
    "你是财务 / 战略方向的求职顾问，正在帮候选人在几个机会之间做选择。\n\n"
    "必须遵守：\n"
    "1. 只依据给定材料判断。不要编造公司的财务数据、排名、口碑、融资情况。\n"
    "2. 先给结论（选哪个，或什么条件下选哪个），再给 3-5 条关键差异，每条 40 字以内。\n"
    "3. 每个选项都要指出一条关键风险。\n"
    "4. 最后给一句下一步该做什么（具体到动作）。\n"
    "5. 简体中文，用 markdown 小标题和列表，不要 JSON。\n"
    "6. 结尾必须单独一行写上：" + SAFETY_FOOTER
)


def build_company_material(comps: list[dict]) -> str:
    blocks = []
    for idx, comp in enumerate(comps, start=1):
        lines = [f"### 选项 {idx}：{comp.get('name') or '未命名'}"]
        for key, label in intel.COMPANY_LABELS[1:]:
            if comp.get(key):
                lines.append(f"- {label}：{comp[key]}")
        if comp.get("note"):
            lines.append(f"- 我的备注：{comp['note']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_job_material(jobs: list[dict]) -> str:
    blocks = []
    for idx, job in enumerate(jobs, start=1):
        result = job.get("result") or {}
        match = result.get("match") or {}
        lines = [f"### 选项 {idx}：{job_label(job, 40)}"]
        lines.append(f"- 整体匹配度：{job.get('level') or '-'}")
        scores = job.get("scores") or {}
        score_text = "、".join(
            f"{label} {scores.get(key, 0)}/5" for key, label in intel.RADAR_DIMS
        )
        lines.append(f"- 五维评分：{score_text}")
        if job.get("role_summary"):
            lines.append(f"- 岗位画像：{job['role_summary']}")
        if match.get("verdict_reason"):
            lines.append(f"- 匹配判断：{match['verdict_reason']}")
        gaps = normalize_items(match.get("gaps"))[:2]
        if gaps:
            lines.append("- 主要短板：" + "；".join(t for t, _ in gaps))
        if job.get("jd_text"):
            lines.append(f"- JD 摘要：{job['jd_text'][:300]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def call_compare(
    subject: str,
    material: str,
    inputs: dict[str, str],
    config: tuple[str, str, str],
    scrub: bool = True,
) -> tuple[str, dict[str, dict[str, str]]]:
    """让模型给对比结论（自由文本，不是 JSON）。返回 (文本, 脱敏映射)。"""
    from openai import OpenAI

    api_key, base_url, model = config
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120, max_retries=1)

    parts = {
        "background": inputs.get("background", ""),
        "preference": inputs.get("preference", "") or "（未填写）",
        "material": material,
    }
    mapping: dict[str, dict[str, str]] = {}
    if scrub:
        parts, mapping = privacy.scrub_many(parts)

    user_prompt = (
        f"【我的背景】\n{parts['background']}\n\n"
        f"【我的求职偏好与性格】\n{parts['preference']}\n\n"
        f"【待比较的{subject}】\n{parts['material']}\n\n"
        f"请比较这些{subject}，告诉我该优先考虑哪个、为什么，以及各自的风险。"
    )
    blocks = privacy.hint_block(mapping)
    if blocks:
        user_prompt += f"\n\n{blocks}"

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": COMPARE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=1200,
    )
    return (resp.choices[0].message.content or "").strip(), mapping


def render_compare_tab(data: dict, llm_config, inputs: dict[str, str]) -> None:
    """对比分析页：公司之间、岗位之间都能并排比较。"""
    st.markdown("### ⚖️ 对比分析")
    st.caption("表格来自你自己录入和已保存的数据，不联网。AI 结论是可选的额外一步。")

    kind = st.radio(
        "对比对象", ["公司", "岗位"], horizontal=True,
        key="cmp_kind", label_visibility="collapsed",
    )

    material = ""
    subject = ""

    if kind == "公司":
        pool = data.get("companies", [])
        if len(pool) < 2:
            st.info("公司库里至少要有 2 家公司才能对比。先去右侧「🏢 公司情报」加两家。")
            return
        labels = {c["id"]: (c.get("name") or "未命名") for c in pool}
        picked = st.multiselect(
            "选 2-4 家公司",
            list(labels.keys()),
            format_func=lambda cid: labels.get(cid, cid),
            key="cmp_companies",
            max_selections=4,
        )
        if len(picked) < 2:
            st.caption("至少选 2 家。")
            return

        comps = [store.get_company(data, cid) for cid in picked]
        comps = [c for c in comps if c]
        if len(comps) < 2:
            return

        headers = ["维度"] + [c.get("name") or "未命名" for c in comps]
        rows: list[list[str]] = []
        for key, label in intel.COMPANY_LABELS[1:]:  # 名字已经在表头，跳过
            rows.append([label] + [c.get(key, "") for c in comps])
        rows.append(
            ["关联岗位数"]
            + [
                str(sum(1 for j in data.get("jobs", []) if j.get("company_id") == c["id"]))
                for c in comps
            ]
        )
        rows.append(["已收藏"] + ["⭐" if c.get("star") else "" for c in comps])

        st.markdown(intel.compare_html(headers, rows), unsafe_allow_html=True)
        material = build_company_material(comps)
        subject = "公司"

    else:
        jobs_all = data.get("jobs", [])
        if len(jobs_all) < 2:
            st.info("至少要有 2 条岗位分析记录才能对比。先去左边分析两份 JD。")
            return
        labels = {j["id"]: f"{j.get('created', '')[:10]} · {job_label(j, 26)}" for j in jobs_all}
        picked = st.multiselect(
            "选 2-4 个岗位",
            list(labels.keys()),
            format_func=lambda jid: labels.get(jid, jid),
            key="cmp_jobs",
            max_selections=4,
        )
        if len(picked) < 2:
            st.caption("至少选 2 个。")
            return

        jobs = [store.get_job(data, jid) for jid in picked]
        jobs = [j for j in jobs if j]
        render_job_compare(jobs)
        material = build_job_material(jobs)
        subject = "岗位"

    # ---------- 可选的 AI 对比结论 ----------
    st.divider()
    st.markdown("**🤖 AI 对比结论（可选）**")
    st.caption("表格是硬数据；AI 结论是主观判断，会消耗一次模型调用，且需你自己复核。")

    if st.button("生成 AI 对比结论", key="btn_cmp_ai", use_container_width=False):
        if not llm_config[0]:
            st.warning("还没配置 API Key，去「⚙️ 求职设置 → 模型配置」填。")
        else:
            problem = validate_config(llm_config)
            if problem:
                st.error(problem)
            else:
                with st.spinner(f"正在比较这些{subject}…"):
                    try:
                        text, mapping = call_compare(
                            subject, material, inputs, llm_config,
                            scrub=scrub_enabled(),
                        )
                        st.session_state["compare_ai_text"] = privacy.restore(text, mapping)
                    except Exception as exc:  # noqa: BLE001
                        st.error(format_api_error(exc))

    ai_text = st.session_state.get("compare_ai_text") or ""
    if ai_text:
        st.markdown(ai_text)
        if st.button("清空结论", key="btn_cmp_clear"):
            st.session_state["compare_ai_text"] = ""
            st.rerun()


def render_view_slot(data: dict, origin: str) -> None:
    """在列表下方展开某条岗位的完整结果（只渲染一条，避免一次算很多遍）。"""
    if st.session_state.get("view_origin") != origin:
        return
    jid = st.session_state.get("view_job_id", "")
    job = store.get_job(data, jid) if jid else None
    if not job:
        return

    st.divider()
    st.markdown(f"#### 📄 {job_label(job, 40)}")
    if st.button("收起", key=f"close_view_{origin}"):
        st.session_state["view_job_id"] = ""
        st.session_state["view_origin"] = ""
        st.rerun()
    render_result(
        job["result"],
        job["meta"],
        quiz_mode=st.session_state.get("quiz_mode", True),
        title_override=job.get("title", ""),
        company_override=job.get("company", ""),
    )


def render_history_tab(data: dict) -> None:
    """历史记录：持久化在 data.json，支持关键词 / 匹配度 / 收藏筛选。"""
    jobs = data.get("jobs", [])
    st.markdown(f"### 🗂 历史记录　·　共 {len(jobs)} 条")
    st.caption("记录存在本机 data.json，关掉浏览器再打开也还在。")

    if not jobs:
        st.caption("还没有记录。上面的分析结果会自动存到这里。")
        return

    c1, c2, c3 = st.columns([2, 1, 1])
    kw = c1.text_input("关键词", key="hist_kw", placeholder="公司 / 岗位 / 画像关键词")
    level = c2.selectbox("匹配度", ["全部", "高", "中", "低"], key="hist_level")
    only_star = c3.checkbox("只看收藏", key="hist_star")

    def hit(job: dict) -> bool:
        if level != "全部" and job.get("level") != level:
            return False
        if only_star and not job.get("star"):
            return False
        if kw.strip():
            blob = " ".join(
                [
                    job.get("company", ""),
                    job.get("title", ""),
                    job.get("role_summary", ""),
                    job.get("jd_head", ""),
                ]
            )
            if kw.strip() not in blob:
                return False
        return True

    shown = [j for j in jobs if hit(j)]
    total = len(shown)
    per_page = 20
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, int(st.session_state.get("hist_page", 1) or 1)), pages)
    st.session_state["hist_page"] = page

    nav1, nav2, nav3 = st.columns([1, 2, 1])
    if nav1.button("← 上一页", disabled=page <= 1, key="hist_prev", use_container_width=True):
        st.session_state["hist_page"] = page - 1
        st.rerun()
    nav2.caption(f"共 {total} 条　·　第 {page}/{pages} 页　·　每页 {per_page} 条")
    if nav3.button("下一页 →", disabled=page >= pages, key="hist_next", use_container_width=True):
        st.session_state["hist_page"] = page + 1
        st.rerun()

    start = (page - 1) * per_page
    for job in shown[start:start + per_page]:
        title = (
            f"{job.get('created') or '-'}　·　{job_label(job, 30)}　·　"
            f"匹配度 {job.get('level') or '-'}"
            + ("　⭐" if job.get("star") else "")
        )
        with st.expander(title):
            meta_line = "　·　".join(
                x for x in (job.get("company"), job.get("identity")) if x
            )
            if meta_line:
                st.caption(meta_line)
            st.caption(f"岗位画像：{job.get('role_summary') or '（无）'}")
            b1, b2, b3 = st.columns(3)
            if b1.button("查看完整结果", key=f"view_{job['id']}", use_container_width=True):
                st.session_state["view_job_id"] = job["id"]
                st.session_state["view_origin"] = "hist"
                st.rerun()
            if b2.button(
                "取消收藏" if job.get("star") else "收藏",
                key=f"hist_star_{job['id']}",
                use_container_width=True,
            ):
                store.toggle_star(data, "job", job["id"])
                persist_data()
                st.rerun()
            if b3.button("删除", key=f"del_{job['id']}", use_container_width=True):
                st.session_state["pending_delete"] = f"job:{job['id']}"

            if st.session_state.get("pending_delete") == f"job:{job['id']}":
                st.warning("确认删除这条记录？删除后不可恢复。")
                d1, d2 = st.columns(2)
                if d1.button("确认删除", key=f"ok_{job['id']}", use_container_width=True):
                    store.remove_job(data, job["id"])
                    if st.session_state.get("latest_job_id") == job["id"]:
                        st.session_state["latest_job_id"] = ""
                    st.session_state["pending_delete"] = ""
                    persist_data()
                    st.rerun()
                if d2.button("取消", key=f"no_{job['id']}", use_container_width=True):
                    st.session_state["pending_delete"] = ""
                    st.rerun()

    if len(jobs) > 1:
        st.download_button(
            "⬇️ 导出全部记录（CSV）",
            data=store.jobs_to_csv(data),
            file_name="岗位分析记录.csv",
            mime="text/csv",
            key="dl_history_csv",
        )

    # ---------- 清理旧记录：只动岗位记录，公司库与真题库不受影响 ----------
    with st.expander("🧹 清理旧记录（只删岗位记录，公司库和真题不动）"):
        keep_days = st.selectbox(
            "只保留最近",
            [30, 60, 90, 180, 365],
            index=2,
            format_func=lambda d: f"{d} 天",
            key="clean_days",
        )
        old_jobs = [j for j in jobs if _age_days(j) > keep_days and not j.get("star")]
        st.caption(
            f"按当前设置会删掉 {len(old_jobs)} 条（共 {len(jobs)} 条）。"
            "已收藏的会保留，公司库和真题库不受影响。删了不可恢复，要留档先导出 CSV。"
        )
        if st.button(
            "删除这些记录",
            key="btn_clean_preview",
            disabled=not old_jobs,
            use_container_width=True,
        ):
            st.session_state["pending_clean"] = keep_days

        if st.session_state.get("pending_clean") == keep_days and old_jobs:
            st.warning(f"确认删除 {len(old_jobs)} 条早于 {keep_days} 天的记录？不可恢复。")
            d1, d2 = st.columns(2)
            if d1.button("确认删除", key="ok_clean", use_container_width=True):
                removed_ids = {j["id"] for j in old_jobs}
                for jid_rm in removed_ids:
                    store.remove_job(data, jid_rm)
                if st.session_state.get("latest_job_id") in removed_ids:
                    st.session_state["latest_job_id"] = (
                        data["jobs"][0]["id"] if data.get("jobs") else ""
                    )
                st.session_state["pending_clean"] = None
                persist_data()
                flash(f"已删除 {len(removed_ids)} 条旧记录。")
                st.rerun()
            if d2.button("取消", key="no_clean", use_container_width=True):
                st.session_state["pending_clean"] = None
                st.rerun()

    render_view_slot(data, "hist")


def improve_interview_answer(
    question: str,
    my_answer: str,
    title: str,
    company: str,
    config,
) -> tuple[bool, str]:
    """把一条面试回答改好。只用你写下来的事实，缺的地方明确让你补，不许编。"""
    system = (
        "你是面试教练，专门帮会计 / 财务 / 风控方向的应届生把回答改得更有说服力。"
        "你的改法是：先点明面试官在考什么，再给一版能直接背的口语化回答。"
        "绝对不许替候选人编造经历、数字、证书或项目。"
    )
    user = (
        f"应聘岗位：{title or '没写'}\n"
        f"公司：{company or '没写'}\n\n"
        f"面试题：{question}\n\n"
        f"我当时的回答（原话）：\n{my_answer or '（当时没记下来）'}\n\n"
        "请输出三行，不要多余的话：\n"
        "考察点：这题面试官真正想看什么（一句话）\n"
        "优化回答：第一人称、口语、3-5 句，能直接背；只用上面出现过的事实\n"
        "需要补充：我回答里缺的具体经历或数字（没有就写「不用补」）"
    )
    try:
        text = chat_once(system, user, config, max_tokens=900, json_mode=False)
    except Exception as exc:  # noqa: BLE001
        return False, format_api_error(exc)
    return True, (text or "").strip()


def render_interview_tab(data: dict, llm_config=("", "", "")) -> None:
    """面试真题库：整段粘贴自动拆成问答对，按岗位筛，配「我的答案 / 优化答案」。

    边界不变：这里不训练任何模型。你的题只在你点分析时作为材料发出去一次。
    """
    items = data.get("interviews", [])
    all_pairs = [(item, store.interview_items(item)) for item in items]
    total_q = sum(len(pairs) for _, pairs in all_pairs)
    with_ans = sum(1 for _, pairs in all_pairs for x in pairs if x.get("my"))
    with_better = sum(1 for _, pairs in all_pairs for x in pairs if x.get("better"))
    dates = sorted((str(i.get("date") or "") for i in items if i.get("date")), reverse=True)

    st.markdown("### 🎤 面试真题题库")
    st.caption(
        "整段粘进来会自动拆成「问题 + 我的回答 + 优化回答」。下次分析同一岗位时，"
        "这些题会随请求发出去，面试预测会优先覆盖它们并标「（真题）」，其余由模型结合 JD 补新题。"
    )

    st.markdown(
        '<div class="statrow">'
        + "".join(
            f'<div class="s"><div class="k">{k}</div><div class="v">{v}</div></div>'
            for k, v in (
                ("场次", f"{len(items)}"),
                ("题目", f"{total_q}"),
                ("有我的答案", f"{with_ans}"),
                ("有优化答案", f"{with_better}"),
                ("最近一次", dates[0][5:] if dates else "—"),
            )
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    ver = st.session_state.get("interview_form_ver", 0)

    # ---------- ① 整段粘贴：拆开给你看，确认了再存 ----------
    st.markdown("#### ➕ 记一场面试")
    st.caption(
        "把聊天记录或笔记整段粘进来就行，这些写法都认：`1、自我介绍` / `Q: …` / "
        "`答：…` / `优化答案：…` / 直接以问号结尾的问句。保存前会先告诉你拆成了几组。"
    )
    iv_log = st.text_area(
        "面试记录原文",
        key=f"iv_log_{ver}",
        height=190,
        label_visibility="collapsed",
        placeholder=(
            "1、自我介绍\n答：我是会计专硕，做过两段审计实习……\n"
            "2、为什么从会计转风控？\n答：……\n优化答案：可以从审计发现的内控问题切入……\n"
            "3、你还有什么想问我的？"
        ),
    )

    preview = prep.parse_interview_log(iv_log) if (iv_log or "").strip() else []
    if preview:
        rows = []
        for i, pair in enumerate(preview, 1):
            tags = []
            if pair.get("my"):
                tags.append(f'<span class="tag ok">我的答案 {len(pair["my"])} 字</span>')
            if pair.get("better"):
                tags.append('<span class="tag">含优化答案</span>')
            if not tags:
                tags.append('<span class="tag plain">只有题目</span>')
            rows.append(
                f'<div class="iv-prev"><b>Q{i}</b>　{html.escape(pair["q"][:60])}'
                f'　{"".join(tags)}</div>'
            )
        st.markdown(
            f'<div class="iv-prev-box"><div class="iv-prev-head">👀 会拆成 {len(preview)} 组问答</div>'
            + "".join(rows)
            + "</div>",
            unsafe_allow_html=True,
        )

    with st.form(f"interview_form_{ver}"):
        c1, c2 = st.columns(2)
        iv_company = c1.text_input("公司", key=f"iv_company_{ver}", placeholder="如：某某银行")
        iv_title = c2.text_input(
            "岗位", key=f"iv_title_{ver}", placeholder="如：风控实习生",
            help="题库按这个字段筛，尽量写成一个统一的叫法",
        )
        c3, c4, c5 = st.columns(3)
        iv_round = c3.selectbox(
            "轮次", ["一面", "二面", "三面", "HR 面", "终面", "群面", "笔试"],
            key=f"iv_round_{ver}",
        )
        iv_outcome = c4.selectbox(
            "结果", ["还没出", "进下一轮", "挂了", "拿到 offer"], key=f"iv_outcome_{ver}"
        )
        iv_date = c5.text_input(
            "面试日期", value=datetime.now().strftime("%Y-%m-%d"), key=f"iv_date_{ver}"
        )
        iv_feeling = st.text_area(
            "现场感受（哪里卡住了，下次怎么改）", height=70, key=f"iv_feel_{ver}"
        )
        submitted = st.form_submit_button("保存到真题库", type="primary")

    if submitted:
        pairs = preview
        if not pairs:
            st.error("没拆出题目。至少写一行问题（一行一题也行，不用写编号）。")
        else:
            rec = store.add_interview(
                data,
                {
                    "company": iv_company,
                    "title": iv_title,
                    "round": iv_round,
                    "outcome": iv_outcome,
                    "date": iv_date,
                    "items": pairs,
                    "feeling": iv_feeling,
                    "job_id": st.session_state.get("latest_job_id", ""),
                },
            )
            persist_data()
            st.session_state["interview_form_ver"] = ver + 1
            flash(f"已记下 {len(rec['questions'])} 道题，下次分析同类岗位会带上。")
            st.rerun()

    if not items:
        st.caption("还没有记录。先记一场，题库就起来了。")
        return

    # ---------- ② 按岗位筛 ----------
    st.markdown("#### 🔍 按岗位翻题")
    titles = store.interview_titles(data)
    comps = sorted({str(i.get("company") or "").strip() for i in items if i.get("company")})
    f1, f2, f3 = st.columns([1.4, 1.2, 1.6])
    pick_title = f1.selectbox("岗位", ["全部岗位"] + titles, key="iv_pick_title")
    pick_comp = f2.selectbox("公司", ["全部公司"] + comps, key="iv_pick_comp")
    keyword = f3.text_input("搜题目或答案", key="iv_kw", placeholder="如：自我介绍 / 存货")

    shown: list[dict] = []
    for item, pairs in all_pairs:
        if pick_title != "全部岗位" and (item.get("title") or "").strip() != pick_title:
            continue
        if pick_comp != "全部公司" and (item.get("company") or "").strip() != pick_comp:
            continue
        if keyword.strip():
            hay = " ".join(
                [str(item.get("company") or ""), str(item.get("title") or ""),
                 str(item.get("feeling") or "")]
                + [f"{x.get('q','')} {x.get('my','')} {x.get('better','')}" for x in pairs]
            )
            if keyword.strip() not in hay:
                continue
        shown.append(item)

    shown_q = sum(len(store.interview_items(i)) for i in shown)
    st.caption(f"显示 {len(shown)} 场 · {shown_q} 道题")

    if not shown:
        st.info("这个筛选条件下没有记录。换个岗位，或者点「全部岗位」。")
        return

    # ---------- ③ 每场展开：题目 / 我的答案 / 优化答案 ----------
    for item in shown[:30]:
        head = "　·　".join(
            x for x in (
                item.get("date") or "-",
                item.get("company") or "未记公司",
                item.get("title") or "未记岗位",
                item.get("round") or "",
                item.get("outcome") or "",
            ) if x
        )
        pairs = store.interview_items(item)
        with st.expander(head, expanded=(len(shown) == 1)):
            edited: list[dict] = []
            for idx, pair in enumerate(pairs):
                st.markdown(
                    f'<div class="iv-q">Q{idx + 1}　{html.escape(pair["q"])}</div>',
                    unsafe_allow_html=True,
                )
                k_my = f"ivmy_{item['id']}_{idx}"
                k_bt = f"ivbt_{item['id']}_{idx}"
                my_now = st.text_area(
                    "我的回答", value=pair.get("my", ""), key=k_my, height=76,
                    placeholder="当时怎么答的，记多少写多少",
                )
                bt_now = st.text_area(
                    "优化回答", value=pair.get("better", ""), key=k_bt, height=76,
                    placeholder="留空就点下面的按钮，让模型改一版",
                )
                g1, _g2 = st.columns([1, 2])
                if g1.button("🤖 生成优化回答", key=f"gen_{item['id']}_{idx}"):
                    if not llm_config[0]:
                        st.warning("还没配 API Key，去「⚙️ 求职设置 → 模型配置」填一下。")
                    else:
                        with st.spinner("正在改这一条…"):
                            ok, text = improve_interview_answer(
                                pair["q"], my_now, item.get("title", ""),
                                item.get("company", ""), llm_config,
                            )
                        if ok:
                            pairs[idx]["better"] = text
                            item["items"] = pairs
                            item["questions"] = [x["q"] for x in pairs]
                            persist_data()
                            st.session_state.pop(k_bt, None)
                            flash("优化回答已生成，记得看一眼再背。")
                            st.rerun()
                        else:
                            st.error(text)
                edited.append(
                    {"q": pair["q"], "my": (my_now or "").strip(), "better": (bt_now or "").strip()}
                )

            s1, s2 = st.columns([1, 1])
            if s1.button("💾 保存这一场的修改", key=f"saveiv_{item['id']}", type="primary",
                         use_container_width=True):
                item["items"] = edited
                item["questions"] = [x["q"] for x in edited]
                persist_data()
                for idx in range(len(edited)):
                    st.session_state.pop(f"ivmy_{item['id']}_{idx}", None)
                    st.session_state.pop(f"ivbt_{item['id']}_{idx}", None)
                flash("这一场已更新。")
                st.rerun()
            if s2.button("🗑 删除这一场", key=f"del_iv_{item['id']}", use_container_width=True):
                st.session_state["pending_delete"] = f"iv:{item['id']}"

            if item.get("feeling"):
                st.caption(f"现场感受：{item['feeling']}")

            if st.session_state.get("pending_delete") == f"iv:{item['id']}":
                st.warning("确认删除这场面试记录？不可恢复。")
                d1, d2 = st.columns(2)
                if d1.button("确认删除", key=f"ok_iv_{item['id']}", use_container_width=True):
                    store.remove_interview(data, item["id"])
                    st.session_state["pending_delete"] = ""
                    persist_data()
                    flash("已删除该场记录。")
                    st.rerun()
                if d2.button("取消", key=f"no_iv_{item['id']}", use_container_width=True):
                    st.session_state["pending_delete"] = ""
                    st.rerun()

    if len(shown) > 30:
        st.caption(f"只列前 30 场，一共 {len(shown)} 场。筛一下岗位能看全。")

    st.download_button(
        "⬇️ 导出真题库（CSV，含答案）",
        data=store.interviews_to_csv(data),
        file_name="面试真题库.csv",
        mime="text/csv",
        key="dl_interviews_csv",
    )


# ==========================================================================

# 九点五、工作台：首页 / 投递追踪 / 实习岗 / 日历
# ==========================================================================
#
# 这一层只做「记录与节奏」，不碰分析逻辑：
#   · 阶段（投递 → 笔试 → 面试 → Offer）存在 job["meta"]["stage"]
#   · 截止 / 面试时间存在 job["meta"]["deadline"] / ["interview_at"]
#   · 岗位类型（校招 / 实习 / 社招）优先用手动值 job["meta"]["kind"]，
#     没手动改过就从 JD 文本自动判断
# 全部落在本机 data.json，不上传，也不触发任何联网。

# 阶段：默认八段，用户可以在设置页改名、加段、上下移动。
# STAGES 是"当前生效的那一份"，每轮渲染前由 apply_custom_stages() 重置成
# 默认值再套上用户的自定义，所以别处直接读 STAGES 就行，不用到处传参。
BASE_STAGES = ["未投递", "已投递", "简历筛", "笔试", "一面", "二面", "HR面", "Offer"]
STAGES = list(BASE_STAGES)


def apply_custom_stages(data: dict) -> None:
    """把用户自定义的阶段套进 STAGES。每轮都调用，避免上一轮的值残留。"""
    # 阶段是个 list，不能用 get_setting（那个只认字符串），要用 get_blob
    custom = store.get_blob(data, "stages", [])
    clean = [str(x).strip() for x in (custom if isinstance(custom, list) else []) if str(x).strip()]
    # 至少两段才有"推进"的意义；数据坏了就退回默认，不让页面炸掉
    STAGES[:] = clean if len(clean) >= 2 else BASE_STAGES


def _drop_stage_keys() -> None:
    """清掉阶段输入框的 session 缓存。

    这些输入框的 key 是按位置编号的（stage_name_0/1/2…）。调过顺序之后，
    位置没变、含义变了，Streamlit 会把旧位置的值留给新内容 —— 看着就像
    "点了上移，名字没动"。清掉缓存让它重新按 value 渲染最省事。
    """
    for i in range(40):
        st.session_state.pop(f"stage_name_{i}", None)

KIND_OPTIONS = ["校招", "实习", "社招"]
WEEK_CN = ["一", "二", "三", "四", "五", "六", "日"]
# 日历事件配色：前三个是岗位自动带上来的，后几个是用户自己加的
EV_STYLE = {
    "截止": "dl", "面试": "iv", "投递": "ap",
    "笔试": "ex", "宣讲": "tk", "待办": "td",
}

# 自定义标签的类型 → (日历上的短前缀, 配色 class)。用户可以自由填名称，这里只管配色
CUSTOM_TYPES: dict[str, str] = {
    "面试": "iv", "笔试": "ex", "截止": "dl",
    "宣讲会": "tk", "待办": "td", "其他": "my",
}


def goto(page_key: str) -> None:
    """主区换页。导航状态唯一来源就是 session_state["nav_page"]。"""
    st.session_state["nav_page"] = page_key
    st.rerun()


def job_meta(job: dict) -> dict:
    """取 meta，结构不对就地补成 dict（老记录可能没有这一层）。"""
    meta = job.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        job["meta"] = meta
    return meta


def detect_kind(text: str) -> str:
    """从 JD 里判断是不是实习岗。只用于「用户没手动指定」的情况。"""
    text = (text or "").lower()
    if any(w in text for w in ("实习", "intern", "暑期", "日常实习")):
        return "实习"
    return "校招"


def job_kind(job: dict) -> str:
    manual = str(job_meta(job).get("kind") or "").strip()
    if manual in KIND_OPTIONS:
        return manual
    blob = " ".join(
        [str(job.get("title") or ""), str(job.get("jd_text") or ""), str(job.get("jd_head") or "")]
    )
    return detect_kind(blob)


def job_stage(job: dict) -> str:
    stage = str(job_meta(job).get("stage") or "").strip()
    return stage if stage in STAGES else "未投递"


def job_salary(job: dict) -> str:
    """薪资：你手填的 > 模型从 JD 里抽的。两处都没有就返回空。"""
    manual = str(job_meta(job).get("salary") or "").strip()
    if manual:
        return manual
    manual = str(job.get("salary") or "").strip()
    if manual:
        return manual
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    return str(result.get("salary") or "").strip()


def job_closed(job: dict) -> bool:
    return bool(job_meta(job).get("closed"))


def job_is_active(job: dict) -> bool:
    """在投 = 已经投出去、还没到 Offer、也没标结束。"""
    return (not job_closed(job)) and job_stage(job) not in ("未投递", "Offer")


def parse_day(value: Any) -> date | None:
    """宽松解析日期。解析不出来返回 None，不抛异常 —— 手填的日期经常写错。"""
    raw = str(value or "").strip().replace("/", "-").replace(".", "-")[:10]
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def save_job_meta(job: dict, **fields) -> None:
    job_meta(job).update(fields)
    persist_data()


def short_job_name(job: dict, limit: int = 10) -> str:
    """日历格子很窄，只留最认得出的那几个字：岗位名优先，没有就用公司名。"""
    name = str(job.get("title") or "").strip() or str(job.get("company") or "").strip()
    name = name or "未命名岗位"
    return name[:limit]


def collect_events(data: dict) -> dict[date, list[tuple[str, str, str]]]:
    """把所有能标到日历上的事情收成一个 {日期: [(类型, 文案, 样式)]}。

    来源有四类：岗位的截止 / 面试 / 实际投递日、面试真题库、以及用户自己加的标签。
    """
    events: dict[date, list[tuple[str, str, str]]] = {}

    def add(day: date | None, kind: str, label: str, style: str | None = None) -> None:
        if day:
            events.setdefault(day, []).append(
                (kind, label, style or EV_STYLE.get(kind, "ap"))
            )

    for job in data.get("jobs", []):
        meta = job.get("meta") or {}
        label = short_job_name(job)
        add(parse_day(meta.get("deadline")), "截止", f"截止 · {label}")
        add(parse_day(meta.get("interview_at")), "面试", f"面试 · {label}")
        add(parse_day(meta.get("applied_at")), "投递", f"投递 · {label}")

    for item in data.get("interviews", []):
        who = " · ".join(x for x in (item.get("company"), item.get("title"), item.get("round")) if x)
        add(parse_day(item.get("date")), "面试", f"真题 · {who or '一场面试'}")

    # 用户自己加的标签：名称原样显示，不额外加前缀，否则格子里放不下
    for ev in data.get("events", []):
        label = str(ev.get("label") or "").strip()
        if not label:
            continue
        kind = str(ev.get("type") or "其他").strip() or "其他"
        add(parse_day(ev.get("date")), kind, label, CUSTOM_TYPES.get(kind, "my"))
    return events


def custom_event_list(data: dict) -> list[dict]:
    """自定义标签按日期排好，供日历页列出和删除。"""
    items = []
    for ev in data.get("events", []):
        day = parse_day(ev.get("date"))
        if day and str(ev.get("label") or "").strip():
            items.append({**ev, "_day": day})
    return sorted(items, key=lambda x: (x["_day"], str(x.get("label") or "")))


def new_event_id(data: dict) -> str:
    """给自定义标签生成一个短 id，只要求在本机数据里不重复。"""
    used = {str(ev.get("id") or "") for ev in data.get("events", [])}
    n = 1
    while f"ev{n}" in used:
        n += 1
    return f"ev{n}"


def _month_cells(year: int, month: int) -> list[list[date | None]]:
    """按周一开头排成整周的格子，首尾补齐 None。"""
    lead = date(year, month, 1).weekday()
    days = calmod.monthrange(year, month)[1]
    cells: list[date | None] = [None] * lead + [
        date(year, month, d) for d in range(1, days + 1)
    ]
    while len(cells) % 7:
        cells.append(None)
    return [cells[i:i + 7] for i in range(0, len(cells), 7)]


def calendar_html(events: dict, year: int, month: int, compact: bool = False) -> str:
    """纯 HTML 月历。紧凑模式只显示小圆点（首页右侧那块），完整模式显示事件标签。"""
    today = date.today()
    head = "".join(f"<th>{w}</th>" for w in WEEK_CN)
    body = []
    for week in _month_cells(year, month):
        tds = []
        for day in week:
            if day is None:
                tds.append('<td class="cal-cell void"></td>')
                continue
            evs = events.get(day, [])
            cls = "cal-cell" + (" has" if evs else "") + (" today" if day == today else "")
            title = "&#10;".join(label for _, label, _ in evs)
            attr = f' title="{html.escape(title, quote=True)}"' if title else ""
            if compact:
                dots = "".join(f'<i class="{style}"></i>' for _, _, style in evs[:3])
                inner = f'<div class="d">{day.day}</div><div class="cal-dots">{dots}</div>'
            else:
                inner = f'<div class="d">{day.day}</div>'
                for _, label, style in evs[:2]:
                    inner += f'<div class="cal-ev {style}">{html.escape(label)}</div>'
                if len(evs) > 2:
                    inner += f'<div class="cal-ev more">+{len(evs) - 2}</div>'
            tds.append(f"<td class='{cls}'{attr}>{inner}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (
        f'<table class="cal"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def render_calendar(data: dict, key_prefix: str, compact: bool = False) -> None:
    """带月份切换的日历。左右箭头 + 今天，月份记在 session_state 里。"""
    today = date.today()
    state_key = f"{key_prefix}_month"
    raw = st.session_state.get(state_key) or today.strftime("%Y-%m")
    try:
        year, month = (int(x) for x in str(raw).split("-")[:2])
    except (ValueError, TypeError):
        year, month = today.year, today.month
    if not (1 <= month <= 12):
        year, month = today.year, today.month

    prev_col, title_col, next_col = st.columns([0.7, 6, 0.7])
    if prev_col.button("‹", key=f"{key_prefix}_prev", use_container_width=True, help="上个月"):
        prev_day = date(year, month, 1) - timedelta(days=1)
        st.session_state[state_key] = prev_day.strftime("%Y-%m")
        st.rerun()
    if next_col.button("›", key=f"{key_prefix}_next", use_container_width=True, help="下个月"):
        next_day = (date(year, month, 28) + timedelta(days=5)).replace(day=1)
        st.session_state[state_key] = next_day.strftime("%Y-%m")
        st.rerun()
    title_col.markdown(
        f'<div class="cal-title" style="text-align:center">{year} 年 {month} 月'
        f'{"　·　今天 " + str(today.day) + " 日" if (year, month) == (today.year, today.month) else ""}'
        "</div>",
        unsafe_allow_html=True,
    )

    events = collect_events(data)
    wrapper = "cal-mini" if compact else ""
    st.markdown(
        f'<div class="{wrapper}">{calendar_html(events, year, month, compact=compact)}</div>',
        unsafe_allow_html=True,
    )
    # 图例跟着实际出现的类型走：你自己加过「宣讲会」，这里才会多出那一格
    legend_names = {"dl": "投递截止", "iv": "面试", "ap": "已投递",
                    "ex": "笔试", "tk": "宣讲", "td": "待办", "my": "自定义"}
    used = [style for evs in events.values() for _, _, style in evs]
    shown_legend = [s for s in legend_names if s in used] or ["dl", "iv", "ap"]
    st.markdown(
        '<div class="cal-leg">'
        + "".join(
            f'<span><i class="{s}"></i>{legend_names[s]}</span>' for s in shown_legend
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    if compact:
        month_events = [
            (day, evs)
            for day, evs in sorted(events.items())
            if day.year == year and day.month == month and day >= today
        ]
        if month_events:
            st.caption(
                "最近："
                + "；".join(
                    f"{day.month}/{day.day} {evs[0][1].split(' · ')[0]}"
                    for day, evs in month_events[:3]
                )
            )
        return

    # 完整模式：本月日程清单
    month_events = [
        (day, evs)
        for day, evs in sorted(events.items())
        if day.year == year and day.month == month
    ]
    st.divider()
    st.markdown(f"**本月日程　{len(month_events)} 天有安排**")
    if not month_events:
        st.caption("这个月还没有标注。到「📮 投递追踪」里给岗位填上截止 / 面试时间就会出现在这里。")
        return
    for day, evs in month_events:
        mark = "　·　今天" if day == today else ""
        st.markdown(
            f"**{day.month} 月 {day.day} 日**{mark}　"
            + "　".join(
                f'<span class="tag {"warn" if style == "dl" else ("ok" if style == "ap" else "")}">'
                f"{html.escape(label)}</span>"
                for _, label, style in evs
            ),
            unsafe_allow_html=True,
        )


def pipe_html(stage: str) -> str:
    """阶段进度条：走过的实心，当前的空心带光晕。"""
    idx = STAGES.index(stage) if stage in STAGES else 0
    cells = []
    for i, name in enumerate(STAGES):
        cls = "done" if i < idx else ("cur" if i == idx else "")
        cells.append(
            f'<div class="pnode {cls}"><span class="dot"></span>{html.escape(name)}</div>'
        )
    return '<div class="pipe">' + "".join(cells) + "</div>"


def job_counts(jobs: list[dict]) -> dict[str, int]:
    today = date.today()
    soon = today + timedelta(days=7)
    counts = {
        "total": len(jobs),
        "intern": 0,
        "active": 0,
        "interview": 0,
        "offer": 0,
        "due_soon": 0,
        "star": 0,
    }
    for job in jobs:
        meta = job.get("meta") or {}
        if job_kind(job) == "实习":
            counts["intern"] += 1
        if job_is_active(job):
            counts["active"] += 1
        if job_stage(job) == "Offer":
            counts["offer"] += 1
        if job.get("star"):
            counts["star"] += 1
        iv = parse_day(meta.get("interview_at"))
        if iv and iv >= today and not job_closed(job):
            counts["interview"] += 1
        dl = parse_day(meta.get("deadline"))
        if dl and today <= dl <= soon and not job_closed(job):
            counts["due_soon"] += 1
    return counts


def kpi_strip(items: list[tuple[str, str, str]], hot: set[str] | None = None) -> None:
    """一排指标卡：(数值, 名称, 小字说明)。hot 里放要高亮的序号（字符串）。"""
    hot = hot or set()
    cards = "".join(
        f'<div class="kpi{" hot" if str(i) in hot else ""}">'
        f'<div class="v">{html.escape(str(value))}</div>'
        f'<div class="k">{html.escape(label)}</div>'
        f'<div class="h">{html.escape(hint)}</div></div>'
        for i, (value, label, hint) in enumerate(items)
    )
    st.markdown(f'<div class="kpis">{cards}</div>', unsafe_allow_html=True)


HOME_MODULES: list[tuple[str, str]] = [
    ("home_show_week", "📅 七日安排"),
    ("home_show_stage", "📊 阶段分布"),
    ("home_show_recent", "🕘 最近分析"),
    ("home_show_cal", "🗓 迷你月历"),
]


def home_flag(data: dict, key: str) -> bool:
    """首页模块开关：本次会话里点过的优先，其次读本机设置，默认全开。"""
    value = st.session_state.get(key)
    if isinstance(value, bool):
        return value
    return store.get_setting(data, key, "1") == "1"


def stage_distribution(jobs: list[dict]) -> dict[str, int]:
    dist = {name: 0 for name in STAGES}
    for job in jobs:
        stage = job_stage(job)
        dist[stage] = dist.get(stage, 0) + 1
    return dist


def stage_strip_html(jobs: list[dict]) -> str:
    """一条按人数分段的堆叠条 + 八个阶段小格。

    横条看的是「人堆在哪一关」，小格看的是具体数字。
    越往后的阶段颜色越实，视觉上就有一条往前推的方向感。
    """
    dist = stage_distribution(jobs)
    total = sum(dist.values())
    if not total:
        return '<div class="empty-hint">还没有岗位记录，阶段分布自然是空的。</div>'
    segs = []
    for i, name in enumerate(STAGES):
        n = dist.get(name, 0)
        if not n:
            continue
        opacity = 0.26 + 0.1 * i
        segs.append(
            f'<i style="flex:{n};opacity:{opacity:.2f}" title="{html.escape(name)} {n} 条"></i>'
        )
    cells = "".join(
        f'<div class="sd{" zero" if not dist.get(name) else ""}">'
        f"<b>{dist.get(name, 0)}</b><span>{html.escape(name)}</span></div>"
        for name in STAGES
    )
    return f'<div class="sbar">{"".join(segs)}</div><div class="sgrid">{cells}</div>'


def week_strip_html(events: dict, today: date) -> str:
    """未来七天横着排。格子窄，每格最多两件，多的写 +N。"""
    cells = []
    for offset in range(7):
        day = today + timedelta(days=offset)
        evs = events.get(day, [])
        cls = "wd today" if offset == 0 else ("wd has" if evs else "wd")
        head = "今天" if offset == 0 else "周" + WEEK_CN[day.weekday()]
        if evs:
            chips = "".join(
                f'<div class="wc {html.escape(style)}">{html.escape(txt)}</div>'
                for _, txt, style in evs[:2]
            )
            if len(evs) > 2:
                chips += f'<div class="wc more">+{len(evs) - 2}</div>'
        else:
            chips = '<div class="wc none">—</div>'
        cells.append(
            f'<div class="{cls}"><div class="wd-h"><b>{day.day}</b>'
            f"<span>{head}</span></div>{chips}</div>"
        )
    return f'<div class="wstrip">{"".join(cells)}</div>'


def render_home(data: dict, inputs: dict[str, str], llm_config) -> None:
    """首页：不摆大标语，先回答「今天该干什么」。"""
    jobs = data.get("jobs", [])
    counts = job_counts(jobs)
    today = date.today()
    events = collect_events(data)
    week_end = today + timedelta(days=7)
    this_week = [(d, e) for d, e in sorted(events.items()) if today <= d <= week_end]
    today_evs = events.get(today) or []

    # 一句话说清状况：今天有事就说今天，今天没事就往前找最近的一件
    if today_evs:
        first = today_evs[0][1]
        say = f"今天 {len(today_evs)} 件安排，第一件是「{first}」"
    elif this_week:
        day, evs = this_week[0]
        delta = (day - today).days
        when = "明天" if delta == 1 else f"{delta} 天后"
        say = f"今天没有安排　·　{when}有「{evs[0][1]}」"
    else:
        say = "接下来七天没有硬安排 —— 正好可以多拆几份 JD，或者去补一补短板"

    st.markdown(
        f"""
<div class="tbar">
  <div class="tbar-main">
    <div class="tbar-date">{today.month} 月 {today.day} 日　·　周{WEEK_CN[today.weekday()]}</div>
    <div class="tbar-say">{html.escape(say)}</div>
  </div>
  <div class="tbar-figs">
    <div><b>{counts['total']}</b><span>在册</span></div>
    <div><b>{counts['active']}</b><span>在投</span></div>
    <div><b>{counts['interview']}</b><span>待面试</span></div>
    <div><b>{counts['due_soon']}</b><span>7 天内截止</span></div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    # 驾驶舱：KPI + 3 天内红色预警 + 今日待办（放在快捷按钮之前，先看清楚再动手）
    workspace.render_dashboard(data, events)

    q1, q2, q3, _q4 = st.columns([1, 1, 1, 2.2])
    if q1.button("🚀 分析一份 JD", type="primary", use_container_width=True, key="home_go_analyze"):
        goto("analyze")
    if q2.button("📮 更新进度", use_container_width=True, key="home_go_track"):
        goto("track")
    if q3.button("➕ 加个日程", use_container_width=True, key="home_go_cal"):
        goto("calendar")

    if home_flag(data, "home_show_week"):
        st.markdown('<div class="sec"><span>📅</span><span>未来七天</span></div>', unsafe_allow_html=True)
        st.markdown(week_strip_html(events, today), unsafe_allow_html=True)
        st.caption("格子里的标签来自投递追踪的截止 / 面试时间，以及你在日历里自己加的标签。")

    show_left = home_flag(data, "home_show_stage") or home_flag(data, "home_show_recent")
    show_right = home_flag(data, "home_show_cal")
    if not (show_left or show_right):
        st.info("首页模块都被你关掉了。到「⚙️ 求职设置 → 🏠 首页模块」里重新打开即可。")
        return

    if show_left and show_right:
        left, right = st.columns([1.85, 1], gap="large")
    elif show_left:
        left, right = st.container(), None
    else:
        left, right = None, st.container()

    if left is not None:
        with left:
            if home_flag(data, "home_show_stage"):
                st.markdown('<div class="sec"><span>📊</span><span>投递都停在哪儿</span></div>', unsafe_allow_html=True)
                st.markdown(stage_strip_html(jobs), unsafe_allow_html=True)
                if counts["total"]:
                    stuck = max(
                        ((n, s) for s, n in stage_distribution(jobs).items()),
                        default=(0, ""),
                    )
                    if stuck[0]:
                        st.caption(
                            f"人最多的一关是「{stuck[1]}」，{stuck[0]} 条。"
                            "同一步堆太多，通常说明上一关的通过率出了问题，而不是你不努力。"
                        )

            if home_flag(data, "home_show_recent"):
                st.markdown('<div class="sec"><span>🕘</span><span>最近分析</span></div>', unsafe_allow_html=True)
                if not jobs:
                    st.markdown(
                        '<div class="empty-hint">还没有记录。点上面「🚀 分析一份 JD」，'
                        "贴一份 JD 进来，结果会自动存成一条岗位记录，之后就能在这里追进度。</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    for job in jobs[:4]:
                        meta = job.get("meta") or {}
                        stage = job_stage(job)
                        kind = job_kind(job)
                        tag_cls = "ok" if stage == "Offer" else ("warn" if job_closed(job) else "plain")
                        stage_tag = (
                            f'<span class="tag {tag_cls}">'
                            f'{html.escape("已结束" if job_closed(job) else stage)}</span>'
                        )
                        due = parse_day(meta.get("deadline"))
                        due_text = f"截止 {due.month}/{due.day}" if due else ""
                        pay = job_salary(job)
                        pay_tag = f'<span class="tag money">{html.escape(pay)}</span>' if pay else ""
                        st.markdown(
                            f'<div class="jrow"><div class="top">'
                            f'<span class="co">{html.escape(job.get("company") or "未填公司")}</span>'
                            f'<span class="ti">{html.escape(job.get("title") or job.get("role_summary") or "")}</span>'
                            f'<span class="tag">{html.escape(kind)}</span>{pay_tag}{stage_tag}'
                            f'<span class="meta">{html.escape(due_text)}</span></div>'
                            f"{pipe_html(stage)}</div>",
                            unsafe_allow_html=True,
                        )
                    if len(jobs) > 4:
                        if st.button(f"看全部 {len(jobs)} 条 →", key="home_all_jobs", use_container_width=True):
                            goto("track")

    if right is not None:
        with right:
            if home_flag(data, "home_show_cal"):
                with st.container(border=True):
                    render_calendar(data, key_prefix="home_cal", compact=True)


def render_track_card(data: dict, job: dict, show_kind: bool = True, selectable: bool = False) -> None:
    """一个岗位一张追踪卡：阶段条 + 阶段切换 + 截止/面试时间。"""
    jid = job.get("id", "")
    meta = job_meta(job)
    stage = job_stage(job)
    stage_key = f"trk_stage_{jid}"
    picked = st.session_state.get(stage_key, stage)
    if picked != stage:
        if picked in STAGES[1:] and not meta.get("applied_at"):
            meta["applied_at"] = date.today().strftime("%Y-%m-%d")
        meta["stage"] = picked
        persist_data()
        stage = picked

    deadline = parse_day(meta.get("deadline"))
    interview_at = parse_day(meta.get("interview_at"))
    today = date.today()
    bits = []
    if deadline:
        left_days = (deadline - today).days
        hint = "今天截止" if left_days == 0 else (f"还有 {left_days} 天" if left_days > 0 else f"已过 {abs(left_days)} 天")
        bits.append(f'<span class="tag warn">截止 {deadline.month}/{deadline.day} · {hint}</span>')
    if interview_at:
        bits.append(f'<span class="tag ok">面试 {interview_at.month}/{interview_at.day}</span>')
    if job_closed(job):
        bits.append('<span class="tag plain">已结束</span>')
    if job.get("level"):
        bits.append(f'<span class="tag plain">匹配 {html.escape(str(job.get("level")))}</span>')
    pay = job_salary(job)
    if pay:
        bits.append(f'<span class="tag money">{html.escape(pay)}</span>')
    if job.get("star"):
        bits.append('<span class="tag star">★ 收藏</span>')

    with st.container(border=True):
        st.markdown(
            f'<div class="jhead">'
            f'<span class="co">{html.escape(job.get("company") or "未填公司")}</span>'
            f'<span class="ti">{html.escape(job.get("title") or job.get("role_summary") or "未命名岗位")}</span>'
            f'{" ".join(bits)}'
            f'<span class="meta">{html.escape(str(job.get("created") or "")[:10])}</span></div>'
            + pipe_html(stage),
            unsafe_allow_html=True,
        )

        if selectable:
            c0, c1, c2, c3, c4 = st.columns([0.36, 1.15, 1.05, 0.95, 1.35])
            # 勾上就是「选中这一条」，批量操作在列表顶部统一处理
            c0.checkbox("选中", key=f"trk_sel_{jid}", label_visibility="collapsed",
                        help="勾选后可批量收藏 / 改阶段 / 删除")
        else:
            c1, c2, c3, c4 = st.columns([1.2, 1.1, 1, 1.4])
        c1.selectbox("阶段", STAGES, index=STAGES.index(stage), key=stage_key, label_visibility="collapsed")
        closed_now = c2.checkbox("已结束", value=job_closed(job), key=f"trk_closed_{jid}")
        if closed_now != job_closed(job):
            meta["closed"] = closed_now
            persist_data()
            if closed_now:
                flash(f"已把「{job_label(job, 18)}」标记为结束。")
            st.rerun()
        if show_kind:
            kind = job_kind(job)
            other = "校招" if kind == "实习" else "实习"
            if c3.button(f"转为{other}", key=f"trk_kind_{jid}", use_container_width=True,
                         help="自动识别可能不准，这里可以手动纠正岗位类型"):
                meta["kind"] = other
                persist_data()
                st.rerun()
        if c4.button("查看分析 / 设置时间", key=f"trk_open_{jid}", use_container_width=True):
            st.session_state["view_job_id"] = jid
            st.session_state["view_origin"] = "track"
            st.session_state["nav_page"] = "history"
            st.rerun()

        with st.expander("⏰ 设置截止 / 面试时间（会标到日历上）"):
            d1, d2 = st.columns(2)
            dl_text = d1.text_input(
                "投递截止 YYYY-MM-DD", value=str(meta.get("deadline") or ""),
                key=f"trk_dl_{jid}", placeholder="2026-11-20",
            )
            iv_text = d2.text_input(
                "面试时间 YYYY-MM-DD", value=str(meta.get("interview_at") or ""),
                key=f"trk_iv_{jid}", placeholder="2026-11-18",
            )
            if st.button("保存这两项", key=f"trk_save_dt_{jid}", use_container_width=True):
                bad = [x for x in (dl_text.strip(), iv_text.strip()) if x and not parse_day(x)]
                if bad:
                    st.error("日期要写成 2026-11-20 这种格式。")
                else:
                    meta["deadline"] = dl_text.strip()
                    meta["interview_at"] = iv_text.strip()
                    persist_data()
                    flash("已保存，日历上会跟着变。")
                    st.rerun()


def bulk_apply(data: dict, ids: list[str], action: str, value: str = "") -> str:
    """批量操作统一出口，返回一句给人看的提示。"""
    wanted = {str(x) for x in ids if x}
    targets = [j for j in data.get("jobs", []) if str(j.get("id") or "") in wanted]
    if not targets:
        return "没有选中任何记录。"
    today = date.today().strftime("%Y-%m-%d")
    if action == "star":
        for job in targets:
            job["star"] = True
    elif action == "unstar":
        for job in targets:
            job["star"] = False
    elif action == "stage":
        for job in targets:
            meta = job_meta(job)
            # 推进到「已投递」及之后，顺手记下实际投递日，日历才有东西可标
            if value in STAGES[1:] and not meta.get("applied_at"):
                meta["applied_at"] = today
            meta["stage"] = value
            # 卡片里的阶段下拉自己也留着一份状态。这里把它清掉，让下一轮
            # 渲染时按新阶段重新取值 —— 否则旧值会把刚改的写回去，表现为
            # 「点了几次都没反应」。卡片此刻还没渲染，清它的 state 是安全的。
            st.session_state.pop(f"trk_stage_{job.get('id')}", None)
    elif action == "close":
        for job in targets:
            job_meta(job)["closed"] = True
    elif action == "delete":
        data["jobs"] = [j for j in data.get("jobs", []) if str(j.get("id") or "") not in wanted]
    persist_data()
    done = {
        "star": "标为收藏", "unstar": "取消收藏", "stage": f"阶段改为「{value}」",
        "close": "标记为已结束", "delete": "删除",
    }.get(action, action)
    return f"已把 {len(targets)} 条记录{done}。"


def render_bulk_bar(data: dict, shown: list[dict]) -> None:
    """列表顶部的批量操作条。勾选在每张卡片里，这里只负责「一起干什么」。"""
    def selected_ids() -> list[str]:
        return [
            str(j.get("id") or "")
            for j in shown
            if st.session_state.get(f"trk_sel_{j.get('id')}")
        ]

    ids = selected_ids()
    with st.container(border=True):
        top_l, top_r = st.columns([2.2, 1])
        top_l.markdown(
            '<div class="bulk-head">☑️ 批量操作　'
            f'<span class="tag {"ok" if ids else "plain"}">{len(ids)} 条已选</span>'
            f'<span class="bulk-hint">共 {len(shown)} 条在当前筛选里</span></div>',
            unsafe_allow_html=True,
        )
        # 全选 / 清空会在卡片渲染前写 session_state，此时 checkbox 还没实例化，是安全的
        a1, a2 = top_r.columns(2)
        if a1.button("全选", key="trk_all", use_container_width=True,
                     help="选中当前筛选下的全部记录"):
            for job in shown:
                st.session_state[f"trk_sel_{job.get('id')}"] = True
            st.rerun()
        if a2.button("清空", key="trk_none", use_container_width=True):
            for job in shown:
                st.session_state[f"trk_sel_{job.get('id')}"] = False
            st.rerun()

        # 一行到底：收藏 / 取消收藏 / 阶段 / 改阶段 / 删除
        b1, b2, b3, b4, b5 = st.columns([1, 1.05, 1.45, 1.35, 1.05])
        disabled = not ids
        if b1.button("★ 收藏", key="trk_bulk_star", use_container_width=True, disabled=disabled):
            flash(bulk_apply(data, ids, "star"))
            st.rerun()
        if b2.button("☆ 取消收藏", key="trk_bulk_unstar", use_container_width=True, disabled=disabled):
            flash(bulk_apply(data, ids, "unstar"))
            st.rerun()
        new_stage = b3.selectbox(
            "批量改阶段", STAGES, key="trk_bulk_stage_pick",
            label_visibility="collapsed", disabled=disabled,
        )
        if b4.button("改成这个阶段", key="trk_bulk_stage",
                     use_container_width=True, disabled=disabled,
                     help="把勾选的记录一起推到上面选中的阶段"):
            flash(bulk_apply(data, ids, "stage", new_stage))
            st.rerun()
        if b5.button("🗑 删除", key="trk_bulk_del", use_container_width=True, disabled=disabled):
            st.session_state["trk_del_pending"] = ids
            st.rerun()

    # 删除必须二次确认：这是本机数据里唯一不可撤销的操作
    pending = st.session_state.get("trk_del_pending") or []
    if pending:
        st.warning(f"确认删除这 {len(pending)} 条记录？删除后不可恢复（本机 data.json 里会一并移除）。")
        d1, d2, d3 = st.columns([1, 1, 2.2])
        if d1.button("确认删除", key="trk_del_ok", type="primary", use_container_width=True):
            msg = bulk_apply(data, pending, "delete")
            for jid in pending:
                st.session_state.pop(f"trk_sel_{jid}", None)
            st.session_state.pop("trk_del_pending", None)
            flash(msg)
            st.rerun()
        if d2.button("取消", key="trk_del_no", use_container_width=True):
            st.session_state.pop("trk_del_pending", None)
            st.rerun()


def parse_pasted_applications(text: str) -> list[dict]:
    """从招聘网站复制来的「我的投递记录」文字里解析出一条条投递。

    允许的格式很宽松：一行一条，行里能认出公司/岗位/日期/阶段就行。
    认不出的字段留空，导入后你可以在列表里改。不联网、不登录，
    只解析你主动粘贴的文字 —— 和 JD 提取是同一条合规边界。
    """
    out: list[dict] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or len(line) < 2:
            continue
        item = {"company": "", "title": "", "date": "", "stage": "", "note": ""}

        dm = re.search(
            r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?|(\d{4}-\d{1,2}-\d{1,2})", line
        )
        if dm:
            if dm.group(4):
                item["date"] = dm.group(4)
            else:
                year = dm.group(1) or str(date.today().year)
                item["date"] = f"{year}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
            line = (line[:dm.start()] + " " + line[dm.end():]).strip()

        for stage in ("Offer", "已终止", "不匹配", "已投递", "已查看", "已读", "待查看", "笔试", "面试"):
            if stage in line:
                item["stage"] = "已投递" if stage in ("已查看", "已读", "待查看") else stage
                line = line.replace(stage, " ")
                break

        for platform in ("BOSS直聘", "智联招聘", "猎聘", "实习僧", "前程无忧", "牛客", "国聘"):
            if platform in line:
                item["note"] = platform
                line = line.replace(platform, " ")
                break

        parts = [p for p in re.split(r"\s+[|·—\-–]+\s+|\s{2,}|[\t]", line) if p.strip()]
        if len(parts) == 1:
            parts = [p.strip() for p in re.split(r"\s+", parts[0], maxsplit=1)]
        if parts:
            item["company"] = parts[0].strip("【】·- ").strip()
            item["title"] = "".join(parts[1:]).strip() if len(parts) > 1 else ""
            item["title"] = item["title"].strip("·|—–- \t").strip()
        if item["company"] or item["title"]:
            out.append(item)
    return out


def render_paste_sync(data: dict) -> None:
    """投递追踪页的粘贴同步入口。"""
    with st.expander("📋 从招聘网站粘贴同步投递记录（不用 MCP，不碰你电脑文件）", expanded=False):
        st.caption(
            "在 BOSS / 智联 / 猎聘 / 实习僧 / 前程无忧打开「我的投递记录」，全选复制，贴到下面。"
            "一行一条即可，认得出的公司、岗位、日期、阶段会自动分列，其余留空给你改。"
            "只在你浏览器里处理这段文字，不联网、不登录、不写任何系统文件。"
        )
        pasted = st.text_area(
            "粘贴投递记录", height=130, key="paste_sync_in",
            placeholder="例：\n汇丰银行 风控实习生 11-05 已投递\n中建电商云筑网 · 财务实习生 · 11-03",
        )
        default_stage = st.selectbox("没写阶段的按这个算", ["已投递", "未投递"], key="paste_sync_stage")
        if st.button("解析并导入", key="paste_sync_go", use_container_width=True):
            rows = parse_pasted_applications(pasted)
            if not rows:
                st.warning("没解析出有效行。检查一下每行是不是至少有公司或岗位名。")
            else:
                jobs = data.get("jobs", [])
                existing = {
                    (str(j.get("company") or "").strip(), str(j.get("title") or "").strip())
                    for j in jobs
                }
                added, skipped = 0, 0
                for row in rows:
                    key_pair = (row["company"], row["title"])
                    if key_pair in existing and row["company"]:
                        skipped += 1
                        continue
                    existing.add(key_pair)
                    meta = {"stage": row["stage"] or default_stage, "time": store.now_text()}
                    if row["date"]:
                        meta["deadline"] = row["date"]
                    if row["note"]:
                        meta["source"] = row["note"]
                    store.add_job(data, {
                        "company": row["company"], "title": row["title"],
                        "role_summary": "（粘贴同步的投递记录，暂无 JD 原文）",
                        "jd_head": "", "jd_text": "", "level": "中", "scores": {},
                        "meta": meta,
                    })
                    added += 1
                store.save(data)
                st.success(f"导入 {added} 条，跳过重复 {skipped} 条。上面列表已更新。")
                if added:
                    st.rerun()


def render_track_page(data: dict) -> None:
    """投递追踪：一批人（公司/HR）按阶段推进，日历数据也从这里来。"""
    jobs = data.get("jobs", [])
    counts = job_counts(jobs)
    st.markdown("### 📮 投递追踪")
    st.caption(
        "从投递到 Offer，每家公司一行进度。阶段、截止、面试时间都记在本机，"
        "切到「🗓 日程日历」就是一张排期表。"
    )
    render_paste_sync(data)
    if not jobs:
        st.markdown(
            '<div class="empty-hint">还没有岗位记录。先到「🚀 岗位分析」贴一份 JD，'
            "分析完会自动生成一条记录，回到这里就能推进度。</div>",
            unsafe_allow_html=True,
        )
        if st.button("去分析一份 JD", type="primary", key="track_go_analyze"):
            goto("analyze")
        return

    kpi_strip(
        [
            (counts["total"], "全部岗位", ""),
            (counts["active"], "在投", "已投未出结果"),
            (counts["interview"], "待面试", "有面试时间"),
            (counts["offer"], "Offer", ""),
            (counts["due_soon"], "7 天内截止", ""),
        ],
        hot={"2"} if counts["active"] else set(),
    )

    f1, f2, f3 = st.columns([1.2, 1, 1])
    scope = f1.selectbox(
        "看哪一批", ["全部", "在投中", "实习岗", "已结束"], key="trk_scope",
        help="在投中 = 已经投出去、还没出结果；实习岗按 JD 自动识别，可手动改。",
    )
    keyword = f2.text_input("关键词", key="trk_kw", placeholder="公司 / 岗位")
    only_star = f3.checkbox("只看收藏", key="trk_star")

    def keep(job: dict) -> bool:
        if scope == "在投中" and not job_is_active(job):
            return False
        if scope == "实习岗" and job_kind(job) != "实习":
            return False
        if scope == "已结束" and not job_closed(job):
            return False
        if only_star and not job.get("star"):
            return False
        if keyword.strip():
            blob = " ".join(
                str(job.get(k) or "") for k in ("company", "title", "role_summary", "jd_head")
            )
            if keyword.strip() not in blob:
                return False
        return True

    shown = [j for j in jobs if keep(j)]

    # 漏斗跟着筛选走：切到「实习岗」就能单看实习这条线的转化率
    with st.container(border=True):
        st.markdown("**投递漏斗**")
        st.caption("每一关相对上一级的通过率。掉得最狠的那一关，才是该改的地方。")
        st.markdown(funnel.render(funnel.build(shown, STAGES)), unsafe_allow_html=True)

    render_bulk_bar(data, shown)

    st.caption(f"当前显示 {len(shown)} / {len(jobs)} 条　·　勾选左侧方框可以批量处理")
    for job in shown:
        render_track_card(data, job, selectable=True)
    if not shown:
        st.caption("这个筛选下没有记录，换个条件试试。")


POOL_STICKERS = ["🎯", "🌱", "🔥", "💼", "🧭", "🍀", "⭐", "🚀", "🧊", "🌙", "🍑", "🐳"]


def pool_score(job: dict) -> int:
    """把五维评分压成一个百分数，用于卡片排序。没有评分就是 0。"""
    scores = job.get("scores") or {}
    vals = [v for v in scores.values() if isinstance(v, (int, float)) and v]
    if not vals:
        return 0
    return int(round(sum(vals) / len(vals) / 5 * 100))


def pool_sticker(job: dict) -> str:
    return str(job_meta(job).get("sticker") or "")


def render_pool_card(job: dict) -> str:
    """一张可以点进去的岗位卡。用 <a> 带 query 参数，点一下就切到详情。"""
    score = pool_score(job)
    sticker = pool_sticker(job) or ("🎯" if score >= 60 else "🌱")
    stage = job_stage(job)
    salary = str(job.get("salary") or "").strip()
    meta_bits = "　·　".join(
        x for x in (job.get("company") or "", salary, stage) if x
    )
    return (
        f'<a class="pool-card" href="?job={html.escape(str(job.get("id") or ""))}">'
        f'<div class="p-sticker">{html.escape(sticker)}</div>'
        f'<div class="p-title">{html.escape(str(job.get("title") or "未命名岗位")[:16])}</div>'
        f'<div class="p-meta">{html.escape(meta_bits)}</div>'
        f'<div class="p-score"><span>{score}</span> 匹配</div>'
        f"</a>"
    )


def render_pool_page(data: dict) -> None:
    """推荐岗位：把库里值得投的挑出来排成卡片，右侧给招聘站搜索直达。

    边界照旧：不抓招聘网站。这里只做两件事 —— 从你自己分析过的岗位里排序推荐，
    以及拼一个搜索链接让你在浏览器里自己打开看。
    """
    jobs = [j for j in data.get("jobs", []) if not job_closed(j)]
    st.markdown("### 🎯 推荐岗位")

    # ---------- 详情：卡片链接带 ?job=id 进来 ----------
    qid = ""
    try:
        qid = str(st.query_params.get("job") or "")
    except Exception:  # noqa: BLE001  —— 老版本没有 query_params，退化成列表
        qid = ""
    if qid:
        job = next((j for j in jobs if str(j.get("id")) == qid), None)
        if job:
            render_pool_detail(data, job)
            return

    st.caption(
        "这里排的是**你已经分析过的岗位**，按匹配度从高到低，还没投的排前面。"
        "卡片可以左右滑；点进去看详细画像。想找新岗位，用右边的搜索直达 —— "
        "链接只是替你把关键词填进招聘站，页面由你自己打开看，程序不抓取、不登录。"
    )

    # ---------- 搜索直达 ----------
    pref_city = ""
    for key in ("pref_campus_city", "pref_intern_city"):
        pref_city = pref_city or str(st.session_state.get(key) or "")
    recent_title = ""
    for job in sorted(jobs, key=lambda j: str(j.get("created") or ""), reverse=True):
        if job.get("title"):
            recent_title = str(job["title"])
            break

    from urllib.parse import quote

    keyword = (recent_title or "财务 实习生").replace(" ", "")
    city = pref_city or "成都"
    links = (
        ("前程无忧", f"https://we.51job.com/pc/search?keyword={quote(keyword)}&jobArea=000000"),
        ("BOSS直聘", f"https://www.zhipin.com/web/geek/job?query={quote(keyword)}&city=100010000"),
        ("智联招聘", f"https://sou.zhaopin.com/?kw={quote(keyword)}"),
        ("猎聘", f"https://www.liepin.com/zhaopin/?key={quote(keyword)}"),
        ("实习僧", f"https://www.shixiseng.com/interns?keyword={quote(keyword)}"),
    )
    st.markdown("**🔗 去招聘站找新的**")
    st.markdown(
        '<div class="pool-links">'
        + "".join(
            f'<a class="pool-link" href="{url}" target="_blank" rel="noopener">'
            f'<span class="pl-name">{name}</span>'
            f'<span class="pl-kw">搜「{html.escape(keyword)}」</span></a>'
            for name, url in links
        )
        + "</div>",
        unsafe_allow_html=True,
    )
    st.caption(f"关键词取自你最近分析的岗位「{keyword}」；城市默认 {city}，可在设置里改偏好城市。")

    if not jobs:
        st.markdown(
            '<div class="empty-hint">还没有可推荐的岗位。<br>'
            "先去「🚀 岗位分析」贴一份 JD，分析完它会自动进这里。</div>",
            unsafe_allow_html=True,
        )
        if st.button("去分析一份 JD", type="primary", key="pool_go_analyze"):
            goto("analyze")
        return

    # ---------- 划动卡片 ----------
    scored = sorted(
        jobs,
        key=lambda j: (
            job_stage(j) != "未投递",   # 还没投的排前面
            -pool_score(j),
            str(j.get("created") or ""),
        ),
    )
    st.markdown("#### 值得先看的几份")
    st.markdown(
        '<div class="pool-strip">' + "".join(render_pool_card(j) for j in scored[:20]) + "</div>",
        unsafe_allow_html=True,
    )
    st.caption(f"共 {len(scored)} 个在库岗位 · 点卡片看详情 · 左右滑动查看更多")

    # ---------- 贴纸：给卡片换个表情 ----------
    with st.expander("🎨 给卡片贴个表情"):
        st.caption("贴纸只影响这里卡片的显示，存在本机 data.json 里。")
        ids = [str(j.get("id")) for j in scored[:20]]
        labels = {str(j.get("id")): job_label(j, 22) for j in scored[:20]}
        pick = st.selectbox(
            "挑一个岗位", ids, format_func=lambda i: labels.get(i, i), key="pool_sticker_pick"
        ) if ids else None
        stickers = ["（不贴）"] + POOL_STICKERS
        chosen = st.radio("选贴纸", stickers, horizontal=True, key="pool_sticker_choice")
        if pick and st.button("保存贴纸", key="pool_sticker_save", type="primary"):
            job = next((j for j in jobs if str(j.get("id")) == pick), None)
            if job:
                job_meta(job)["sticker"] = "" if chosen == "（不贴）" else chosen
                persist_data()
                flash("卡片表情换好了。")
                st.rerun()


def render_pool_detail(data: dict, job: dict) -> None:
    """卡片点进来之后的岗位详情。数据全部来自已存结果，不重新调模型。"""
    result = job.get("result") or {}
    if st.button("← 回到推荐列表", key="pool_back"):
        try:
            st.query_params.clear()
        except Exception:  # noqa: BLE001
            pass
        st.rerun()

    sticker = pool_sticker(job) or "🎯"
    st.markdown(
        f'<div class="pool-hero"><div class="ph-sticker">{html.escape(sticker)}</div>'
        f'<div><div class="ph-title">{html.escape(str(job.get("title") or "未命名岗位"))}</div>'
        f'<div class="ph-meta">{html.escape("　·　".join(x for x in (job.get("company") or "", job.get("salary") or "", job_stage(job)) if x))}</div>'
        f"</div></div>",
        unsafe_allow_html=True,
    )

    kpi_strip(
        [
            (f"{pool_score(job)}", "匹配度", "五维平均"),
            (job_stage(job), "当前阶段", ""),
            (str(job.get("identity") or "—"), "我的身份", ""),
            (str(job.get("created") or "—")[:10], "分析日期", ""),
        ]
    )

    if result.get("role_summary"):
        st.markdown(f"**岗位画像**　{result['role_summary']}")

    decision = result.get("decision") or {}
    if decision.get("verdict"):
        st.markdown(f"**投递建议**　{decision.get('verdict')}")
        if decision.get("reason"):
            st.caption(decision["reason"])

    hard = (result.get("jd_breakdown") or {}).get("hard") or []
    if hard:
        sub_header("硬性条件")
        render_items(normalize_items(hard))

    gaps = (result.get("match") or {}).get("gaps") or []
    if gaps:
        sub_header("主要短板")
        render_items(normalize_items(gaps))

    if result.get("day_in_life"):
        sub_header("一天大概在做什么")
        render_items(normalize_items(result["day_in_life"]))

    st.divider()
    b1, b2 = st.columns(2)
    if b1.button("📮 去投递追踪里推进", key="pool_to_track", use_container_width=True):
        st.session_state["latest_job_id"] = job.get("id")
        goto("track")
    if b2.button("🎤 看这个岗位的面试真题", key="pool_to_iv", use_container_width=True):
        st.session_state["iv_pick_title"] = str(job.get("title") or "")
        goto("interview")


def render_intern_page(data: dict) -> None:
    """实习岗单独一页：这类岗位节奏和校招不一样，混在一起看容易漏。"""
    jobs = data.get("jobs", [])
    interns = [j for j in jobs if job_kind(j) == "实习"]
    counts = job_counts(interns)
    st.markdown("### 🎒 实习岗")
    st.caption(
        "按 JD 里的「实习 / Intern / 暑期 / 日常实习」自动归类，判错可以手动改。"
        "实习岗单独看，是因为它看重的是转正机会和时间窗口，和校招不是一套节奏。"
    )
    kpi_strip(
        [
            (len(interns), "实习岗", "共识别到"),
            (counts["active"], "在投", "已投未出结果"),
            (counts["interview"], "待面试", ""),
            (counts["offer"], "Offer", ""),
            (counts["due_soon"], "7 天内截止", ""),
        ],
        hot={"1"} if counts["active"] else set(),
    )

    if not interns:
        st.markdown(
            '<div class="empty-hint">还没有识别到实习岗。<br>'
            "在「🚀 岗位分析」里贴一份带「实习」「Intern」「暑期」字样的 JD，"
            "分析完会自动归到这里；如果 JD 里没写，也可以在「📮 投递追踪」里手动把某个岗位标成实习。</div>",
            unsafe_allow_html=True,
        )
        if st.button("去分析一份实习 JD", type="primary", key="intern_go_analyze"):
            goto("analyze")
        return

    for job in interns:
        render_track_card(data, job)

    st.divider()
    st.caption(
        "实习岗的判断只看 JD 文本，不联网、不查招聘站。想加一条别的岗位，"
        "去「📮 投递追踪」点「转为实习」即可。"
    )


def render_calendar_page(data: dict) -> None:
    st.markdown("### 🗓 日程日历")
    st.caption(
        "自动标上来的有三种：投递截止、面试时间（在「📮 投递追踪」里填）、实际投递日"
        "（阶段推进到「已投递」那天自动记）。剩下的空白格，你自己往上面加标签。"
    )
    events = collect_events(data)
    today = date.today()
    month_total = sum(
        1 for day in events if day.year == today.year and day.month == today.month
    )
    mine = len(data.get("events", []))
    st.caption(
        f"本月共 {month_total} 天有安排　·　全部事件 {sum(len(v) for v in events.values())} 条"
        + (f"　·　其中你手动加的 {mine} 条" if mine else "")
    )

    # 日程页给足宽度：格子窄了事件标签会被截成「投递 · 未命名…」，反而看不清
    render_calendar(data, key_prefix="cal_page", compact=False)

    st.divider()
    tab_job, tab_free = st.tabs(["📌 给岗位标截止 / 面试", "🏷️ 自己加一个标签"])

    # ---------------- ① 岗位日期 ----------------
    with tab_job:
        jobs = data.get("jobs", [])
        if not jobs:
            st.caption("还没有岗位记录。先到「🚀 岗位分析」贴一份 JD，再回来标截止时间。")
        else:
            ids = [j.get("id", "") for j in jobs]
            by_id = {j.get("id", ""): j for j in jobs}
            # 记录被删掉后，上次选中的 id 可能已经不存在了；先纠正再建控件
            if st.session_state.get("cal_pick_job") not in ids:
                st.session_state["cal_pick_job"] = ids[0]

            def label_of(jid: str) -> str:
                job = by_id.get(jid) or {}
                return job_label(job, 26)

            picked = st.selectbox("给哪个岗位标", ids, format_func=label_of, key="cal_pick_job")
            job = by_id.get(picked)
            if job:
                meta = job_meta(job)
                c1, c2, c3 = st.columns([1, 1, 1])
                dl_text = c1.text_input(
                    "投递截止 YYYY-MM-DD",
                    value=str(meta.get("deadline") or ""),
                    key=f"cal_dl_{picked}",
                    placeholder="2026-11-20",
                )
                iv_text = c2.text_input(
                    "面试时间 YYYY-MM-DD",
                    value=str(meta.get("interview_at") or ""),
                    key=f"cal_iv_{picked}",
                    placeholder="2026-11-18",
                )
                if c3.button("保存到日历", key=f"cal_save_{picked}",
                             use_container_width=True, type="primary"):
                    bad = [x for x in (dl_text.strip(), iv_text.strip()) if x and not parse_day(x)]
                    if bad:
                        st.error("日期要写成 2026-11-20 这种格式。")
                    else:
                        meta["deadline"] = dl_text.strip()
                        meta["interview_at"] = iv_text.strip()
                        persist_data()
                        flash("已保存，日历上会跟着变。")
                        st.rerun()
                st.caption("要清空某一项，把对应输入框删空再保存。")

    # ---------------- ② 自定义标签 ----------------
    with tab_free:
        st.caption(
            "宣讲会、交材料、模拟面试、笔试…… 名称随你写，类型只决定颜色。"
            "这些只写在本机，不会同步到任何日历服务。"
        )
        with st.form("cal_add_event", clear_on_submit=False):
            f1, f2, f3 = st.columns([1, 1.7, 1])
            day = f1.date_input("日期", value=today, key="ev_date")
            label = f2.text_input(
                "标签名称", max_chars=20, placeholder="如：模拟面试 / 交就业推荐表",
                key="ev_label",
            )
            kind = f3.selectbox("类型（决定颜色）", list(CUSTOM_TYPES), key="ev_type")
            note = st.text_input(
                "备注（选填）", max_chars=60, placeholder="如：10:00 行政楼，带三份简历",
                key="ev_note",
            )
            submitted = st.form_submit_button("加到日历", type="primary")
        if submitted:
            name = (label or "").strip()
            if not name:
                st.error("标签名称不能空着 —— 换行的空标签在格子里看不出是干什么的。")
            else:
                data.setdefault("events", []).append(
                    {
                        "id": new_event_id(data),
                        "date": day.strftime("%Y-%m-%d"),
                        "label": name,
                        "type": kind,
                        "note": (note or "").strip(),
                        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }
                )
                persist_data()
                flash(f"已加「{name}」，日历上能看到。")
                st.rerun()

        items = custom_event_list(data)
        if not items:
            st.caption("还没有自己加的标签。上面填一条就能出现在月历格子里。")
        else:
            st.markdown("**我加过的标签**")
            for ev in items[::-1][:12]:
                c_time, c_name, c_type, c_del = st.columns([0.9, 2.6, 1, 0.6])
                c_time.markdown(f"**{ev['_day'].month}/{ev['_day'].day}**")
                name_html = html.escape(str(ev.get("label") or ""))
                note_txt = str(ev.get("note") or "").strip()
                if note_txt:
                    name_html += f'<span class="ev-note">{html.escape(note_txt)}</span>'
                c_name.markdown(name_html, unsafe_allow_html=True)
                c_type.caption(str(ev.get("type") or "其他"))
                if c_del.button("删", key=f"del_ev_{ev.get('id')}", help="删掉这个标签"):
                    st.session_state["ev_del"] = ev.get("id")
                    st.rerun()
            if len(items) > 12:
                st.caption(f"只列最近 12 条，一共 {len(items)} 条。")

        pending = st.session_state.get("ev_del")
        if pending:
            ev = next(
                (x for x in data.get("events", []) if str(x.get("id")) == str(pending)), None
            )
            if ev:
                st.warning(f"确认删掉「{ev.get('label')}」这个标签？删了就没了。")
                d1, d2, _d3 = st.columns([1, 1, 2.2])
                if d1.button("确认删除", key="ev_del_ok", type="primary", use_container_width=True):
                    data["events"] = [
                        x for x in data.get("events", [])
                        if str(x.get("id")) != str(pending)
                    ]
                    persist_data()
                    st.session_state.pop("ev_del", None)
                    flash("已删掉这个标签。")
                    st.rerun()
                if d2.button("取消", key="ev_del_no", use_container_width=True):
                    st.session_state.pop("ev_del", None)
                    st.rerun()


# ==========================================================================
# 十、主流程
# ==========================================================================

# 页面基础配置必须是第一个 Streamlit 调用
st.set_page_config(
    page_title="会计专硕背景 · 岗位JD智能分析助手",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)
# 数据与状态先就位，CSS 要用到存下来的主题
if "data_checked" not in st.session_state:
    st.session_state["data_checked"] = True
    st.session_state["data_notice"] = store.migrate_if_needed()

# ---- 多用户：配了云端就先登录，再谈数据 ----
# 位置很关键：必须在下面 data 载入之前，因为 store.load() 在云端模式下要按登录用户去取。
# 没配 Supabase 时 _cloud_cfg 为 None，整段跳过，行为和原来完全一致。
_cloud_cfg = auth.config()
_auth_session: dict | None = None
if _cloud_cfg:
    _auth_session = ensure_login(_cloud_cfg)
    activate_cloud_data(_cloud_cfg)

_current_uid = (_auth_session or {}).get("user_id")
if "data" not in st.session_state or st.session_state.get("data_user") != _current_uid:
    st.session_state["data"] = store.load()
    st.session_state["data_user"] = _current_uid
data = st.session_state["data"]

# 自定义阶段要在任何页面渲染之前套进去（填岗位、追踪、批量条都读 STAGES）
apply_custom_stages(data)

# 上次没写完的草稿（粘了一半的 JD、填好的模型地址）灌回来。
# 放在这里是因为它要用到 data，而且必须在任何页面渲染之前 ——
# 各页的 draft_restore 会从这里取值。
load_drafts(data)

# 主题：存在 data.json 里，重启后还是上次选的那套
if "theme" not in st.session_state:
    st.session_state["theme"] = store.get_setting(data, "theme", DEFAULT_THEME)
if "accent" not in st.session_state:
    st.session_state["accent"] = store.get_setting(data, "accent", "")

st.markdown(
    build_css(st.session_state["theme"], st.session_state["accent"]),
    unsafe_allow_html=True,
)

# 热重载提示：只在真的发生过 reload 的那一次运行里出现
if _RELOADED:
    st.caption(
        f"♻️ 检测到 {'、'.join(_RELOADED)} 已更新，已自动加载最新代码"
        f"（这次不用重启服务）。"
    )

# 背景版本号接着历史记录往下排，避免重启后版本号归零
if "bg_version" not in st.session_state:
    st.session_state["bg_version"] = max(
        [int((j.get("meta") or {}).get("bg_version", 0)) for j in data.get("jobs", [])],
        default=0,
    )

for key, default in (
    ("company_form_ver", 0),
    ("extracted_company", {}),
    ("editing_company_id", ""),
    ("pending_delete", ""),
    ("latest_job_id", ""),
    ("view_job_id", ""),
    ("view_origin", ""),
    ("compare_ai_text", ""),
    ("batch_compare_ids", []),
    ("quiz_mode", True),
    ("pending_send", None),
    ("pending_clean", None),
    ("interview_form_ver", 0),
):
    st.session_state.setdefault(key, default)

# 重开浏览器时没有「最近一次」，就退回历史里最新的一条，
# 否则历史有记录、主区却空着，看着像数据丢了
if not st.session_state.get("latest_job_id") and data.get("jobs"):
    st.session_state["latest_job_id"] = data["jobs"][0]["id"]

# 导航状态：默认落在首页
st.session_state.setdefault("nav_page", "home")

page = render_nav()
if page not in {k for _, items in NAV_GROUPS for k, _ in items}:
    page = st.session_state["nav_page"] = "home"

st.session_state["data"] = data
if page == "settings":
    inputs, llm_config = render_settings_page(data)
else:
    inputs, llm_config = collect_inputs(), collect_config()

# 跨 rerun 的提示（保存成功、已标记结束等）在这里统一显示一次
show_flash()

# 设置页里可能刚换过数据目录 / 主题，这里把最新的 data 取回来
data = st.session_state["data"]


def _ai_caller(llm_config):
    """扩展页共用的单轮对话入口。

    没配好模型时返回 None，让页面自己显示「先去设置填 Key」，
    而不是在每个按钮里各写一遍报错。
    """
    if validate_config(llm_config):
        return None

    def _call(system: str, user: str) -> str:
        return chat_once(system, user, llm_config, max_tokens=2500, json_mode=False)

    return _call


def _ai_context(inputs: dict[str, str]) -> str:
    """给 AI 助手的背景摘要。截断是刻意的：每次调用都带整份简历太浪费 token。"""
    parts = [
        str(inputs.get("background") or "")[:400],
        str(inputs.get("preference") or "")[:200],
        str(inputs.get("resume") or "")[:800],
    ]
    return "\n".join(p for p in parts if p.strip())


def render_page(page_key: str) -> None:
    """主区路由：一个导航项对应一个页面函数，不再用标签页。"""
    if page_key == "home":
        render_home(data, inputs, llm_config)
    elif page_key == "analyze":
        render_analyze_tab(inputs, llm_config, data)
    elif page_key == "track":
        render_track_page(data)
    elif page_key == "calendar":
        render_calendar_page(data)
    elif page_key == "season":
        workspace.render_season_page(data)
    elif page_key == "social":
        workspace.render_social_page(data)
    elif page_key == "resumes":
        workspace.render_resumes_page(data, ai=_ai_caller(llm_config))
    elif page_key == "ability":
        workspace.render_ability_page(data)
    elif page_key == "network":
        workspace.render_network_page(data)
    elif page_key == "news":
        workspace.render_news_page(data, ai=_ai_caller(llm_config), background=_ai_context(inputs))
    elif page_key == "offers":
        workspace.render_offers_page(data)
    elif page_key == "mood":
        workspace.render_mood_page(data)
    elif page_key == "ai":
        workspace.render_ai_page(data, ai=_ai_caller(llm_config), context=_ai_context(inputs))
    elif page_key == "pool":
        render_pool_page(data)
    elif page_key == "intern":
        render_intern_page(data)
    elif page_key == "fav":
        render_favorites_tab(data)
    elif page_key == "compare":
        render_compare_tab(data, llm_config, inputs)
    elif page_key == "history":
        render_history_tab(data)
    elif page_key == "interview":
        render_interview_tab(data, llm_config)
    elif page_key == "intel":
        render_intel_panel(data, llm_config)
    elif page_key == "settings":
        st.divider()
        st.markdown("#### 🔐 使用边界（已写进代码逻辑，不是口头约定）")
        st.caption(
            "① 联网只有你点「抓取」的时候：读你贴的那一页，先过 robots.txt，对方不允许就停；"
            "不登录、不绕验证码、不翻页批量抓。天眼查/企查查这类条款禁止、已有判例的平台直接不碰。\n\n"
            "② 只解析你主动上传的那一份简历，不扫描目录、不发邮件、不自动投递。\n\n"
            "③ 公司库 / 收藏 / 历史只写在本机 data.json（默认落 D 盘，可在本页「📦 数据与内存」切换），不上传。\n\n"
            "④ 姓名 / 手机 / 邮箱 / 身份证 / 院校 / 公司全称在发送前于本机脱敏（本页可开关，也可开启「发送前先看一遍」）。\n\n"
            "⑤ 所有 AI 输出均为草稿，必须人工审阅修改后再对外使用。"
        )


render_page(page)

# 草稿落盘：只在这次做过修改时写一次，避免每轮 rerun 都动 data.json
persist_drafts(data)

# ---- 页脚 ----
st.divider()
st.caption(
    f"{SAFETY_FOOTER}　｜　本工具不构成任何求职承诺，最终判断请结合真实信息与人岗沟通。"
)

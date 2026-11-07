"""本机数据持久化：公司库 / 岗位分析记录 / 收藏标记。

全部数据只存在一个文件里：data.json。

存在哪儿（按优先级）：
  1. 环境变量 JOBTOOL_DATA_DIR 指定的目录（最高优先级，适合固定到某块盘）
  2. 项目内的 .data_dir 指针文件（页面上点「切换目录」时写入，只有一行路径，几十字节）
  3. 自动挑一块非系统盘：Windows 上依次看 D:/E:/F:/G:/H:，用第一块能写的，
     落到 <盘>:/JobToolData；都没有才退回项目目录
  4. 环境变量 JOBTOOL_DATA_FILE 可指定完整文件路径（测试用，覆盖以上全部）

为什么默认不落 C 盘：C 盘通常就是系统盘，写满以后整机都卡；数据文件会随
岗位记录 / 公司库一直长。放 D 盘这类数据盘，既快又不影响系统盘。

设计边界（写进代码，不靠自觉）：
  · 只碰 data.json 这一个文件，不遍历目录、不读用户其它文件。
  · 全程不联网、不上传。公司库和收藏只留在本机。
  · 写入用「临时文件 + 原子替换」，避免写到一半中断留下半截坏文件。
  · 迁移只做「复制」，从不删除旧文件 —— 删不删由用户自己决定。
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import shutil
import string
from datetime import datetime
from pathlib import Path

# 项目目录（store.py 所在目录）：旧数据位置 + 指针文件位置
_PROJECT_DIR = Path(__file__).resolve().parent
_LEGACY_FILE = _PROJECT_DIR / "data.json"      # 以前版本的默认位置（在 C 盘）
_LOCATOR = _PROJECT_DIR / ".data_dir"          # 记录用户自选目录，只有一行
_APP_FOLDER = "JobToolData"                    # 非系统盘下建的文件夹名
_DRIVE_LETTERS = "DEFGH"                       # 依次尝试的非系统盘

SCHEMA_VERSION = 2

# 模块构建标记。改了这个文件就把数字加一，app.py 启动时比对：
# 不一致说明运行中的进程还在用旧的 store 模块（Streamlit 改依赖后只重跑 app.py，
# 不会重新 import store），这时 app.py 会强制 reload 一次，避免出现
# "代码已经更新、页面却按旧函数取值" 的 KeyError。
BUILD = 20261107161500

# 公司卡片的字段顺序（表格、CSV、对比都按这个顺序，保证各处一致）
COMPANY_FIELDS: list[tuple[str, str]] = [
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

COMPANY_KEYS = [k for k, _ in COMPANY_FIELDS]


# --------------------------------------------------------------------------
# 基础读写
# --------------------------------------------------------------------------


def _writable(folder: Path) -> bool:
    """真的试写一次：只看 exists() 不够，U 盘只读、权限不足都会在写入时才炸。"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_probe"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# 自动挑选的结果缓存：data_dir() 每一轮 rerun 都会被调用，如果每次都去试写
# 探测文件，一页刷几次就会在数据盘上反复建/删同名文件（杀软会当可疑行为拦）。
# 目录在本进程生命周期内不会变，挑一次记住就够；用户手动换目录时清空。
_PICKED_DIR: Path | None = None


def _pick_dir() -> Path:
    """自动挑一块非系统盘；非 Windows 或没有其它盘时退回项目目录。"""
    global _PICKED_DIR
    if _PICKED_DIR is not None:
        return _PICKED_DIR
    picked: Path | None = None
    if os.name == "nt":
        for letter in _DRIVE_LETTERS:
            root = Path(f"{letter}:/")
            if not root.exists():
                continue
            candidate = root / _APP_FOLDER
            if _writable(candidate):
                picked = candidate
                break
    _PICKED_DIR = picked or _PROJECT_DIR
    return _PICKED_DIR


def data_dir() -> Path:
    """数据目录。环境变量 > 指针文件 > 自动挑选。"""
    env = os.environ.get("JOBTOOL_DATA_DIR", "").strip()
    if env:
        return Path(env)

    try:
        if _LOCATOR.exists():
            saved = _LOCATOR.read_text(encoding="utf-8").strip()
            if saved:
                return Path(saved)
    except OSError:
        pass
    return _pick_dir()


def is_on_system_drive() -> bool:
    """数据是否落在系统盘（Windows 的 C 盘）。非 Windows 一律返回 False。"""
    if os.name != "nt":
        return False
    try:
        return Path(data_dir()).drive.upper() == "C:"
    except (IndexError, ValueError):
        return False


def data_path() -> Path:
    """当前生效的数据文件完整路径。"""
    override = os.environ.get("JOBTOOL_DATA_FILE", "").strip()
    return Path(override) if override else data_dir() / "data.json"


def migrate_if_needed() -> str:
    """把项目目录里的旧 data.json 复制到新位置，返回要展示给用户的一句话。

    只复制、不删除：旧文件留在原处当备份，什么时候删由用户自己决定。
    新位置已经有数据时不覆盖，避免把新记录冲掉。
    """
    target = data_path()
    if target.exists():
        return ""
    if not _LEGACY_FILE.exists():
        return ""
    if target.parent.resolve() == _LEGACY_FILE.parent.resolve():
        return ""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_LEGACY_FILE, target)
        return (
            f"已把项目目录里的旧数据复制到 {target}。"
            f"旧文件仍留在 {_LEGACY_FILE}，确认数据没丢后可以自己删掉。"
        )
    except OSError as exc:
        return f"旧数据搬家失败：{type(exc).__name__} - {exc}"


def set_data_dir(folder: str) -> tuple[bool, str]:
    """切换数据目录：先把现有数据复制过去，成功后才改指针。"""
    global _PICKED_DIR
    raw = (folder or "").strip().strip('"').strip("'")
    if not raw:
        return False, "路径不能为空。"

    target = Path(raw)
    if target.suffix.lower() == ".json":
        target = target.parent
    if os.name == "nt" and target.drive.upper() == "C:":
        return False, "C 盘正是要避开的那个，换 D 盘或其它数据盘。"

    if not _writable(target):
        return False, f"这个目录不可写（可能不存在、没权限或是只读盘）：{target}"

    ok, msg = save(load(), path=target / "data.json")
    if not ok:
        return False, f"把现有数据复制过去失败：{msg}"

    try:
        _LOCATOR.write_text(str(target), encoding="utf-8")
    except OSError as exc:
        return False, f"目录已可写，但记录新位置失败：{exc}"
    _PICKED_DIR = target
    return True, f"已切换到 {target}（原数据已复制，旧位置的文件没动）"


def stats() -> dict:
    """页面「数据与资源」面板用：路径 / 体积 / 记录条数。"""
    path = data_path()
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    data = load()
    return {
        "path": str(path),
        "dir": str(path.parent),
        "size": size,
        "companies": len(data.get("companies", [])),
        "jobs": len(data.get("jobs", [])),
        "interviews": len(data.get("interviews", [])),
        "on_system_drive": is_on_system_drive(),
    }


def _empty() -> dict:
    return {
        "version": SCHEMA_VERSION,
        "companies": [],
        "jobs": [],
        "interviews": [],
        "events": [],
        "settings": {},
    }


def get_setting(data: dict, key: str, default: str = "") -> str:
    """读一个界面设置（目前只有主题配色）。结构不对就当没存过。"""
    settings = data.get("settings")
    if isinstance(settings, dict):
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def set_setting(data: dict, key: str, value: str) -> None:
    """写一个界面设置。只改内存里的 dict，落盘由调用方统一 persist。"""
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    data["settings"][key] = value


def get_blob(data: dict, key: str, default=None):
    """读任意 JSON 片段（比 get_setting 宽松，dict / list / 数字都能存）。

    用途：界面草稿（粘了一半的 JD、填了一半的表单）要跨页面、跨刷新保留，
    这些不是"一条设置"，而是一坨结构，所以单独给个不挑类型的入口。
    """
    settings = data.get("settings")
    if isinstance(settings, dict) and key in settings:
        return settings[key]
    return default


def set_blob(data: dict, key: str, value) -> None:
    """写任意 JSON 片段。同样只改内存，落盘由调用方决定。"""
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    data["settings"][key] = value


def load() -> dict:
    """读数据文件。文件不存在、损坏、结构不对，都退回空结构，不让页面崩。"""
    path = data_path()
    if not path.exists():
        return _empty()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return _empty()

    if not isinstance(data, dict):
        return _empty()
    data.setdefault("version", SCHEMA_VERSION)
    # 老版本没有 interviews / settings / events 这几层，这里补齐，旧文件不用手动改
    for key in ("companies", "jobs", "interviews", "events"):
        if not isinstance(data.get(key), list):
            data[key] = []
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    return data


def save(data: dict, path: Path | None = None) -> tuple[bool, str]:
    """原子写入，返回 (是否成功, 提示)。path 不传就用当前生效的数据文件。"""
    path = data_path() if path is None else path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 注意：不能用 path.with_suffix(".json.tmp")，后缀里再带点会抛 ValueError
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)  # 原子替换：要么旧文件，要么新文件，不会有半截
        return True, ""
    except OSError as exc:
        return False, f"写入失败：{type(exc).__name__} - {exc}"


def new_id(prefix: str) -> str:
    """生成短 id：时间戳 + 4 位随机，够用且可读。"""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    tail = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{prefix}_{stamp}_{tail}"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# 公司库
# --------------------------------------------------------------------------


def blank_company() -> dict:
    item = {key: "" for key in COMPANY_KEYS}
    item.update({"id": "", "note": "", "star": False, "created": now_text(), "source": ""})
    return item


def clean_company(payload: dict) -> dict:
    """把外部传来的字段收敛成固定结构，多余字段丢掉，缺的补空串。"""
    item = blank_company()
    for key in COMPANY_KEYS + ["id", "note", "created", "source"]:
        value = payload.get(key, "")
        value = "" if value is None else str(value).strip()
        # created 没传时保留 blank_company 里的当前时间，别覆盖成空串
        if key == "created" and not value:
            continue
        item[key] = value
    item["star"] = bool(payload.get("star", False))
    return item


def upsert_company(data: dict, payload: dict, cid: str = "") -> dict:
    """新增或更新一家公司。cid 为空表示新增。返回保存后的记录。"""
    companies = data.setdefault("companies", [])
    record = clean_company(payload)

    if cid:
        for idx, item in enumerate(companies):
            if item.get("id") == cid:
                record["id"] = cid
                record["created"] = item.get("created") or record["created"]
                record["star"] = item.get("star", record["star"])
                companies[idx] = record
                return record
        # cid 给了但没找到，退化为新增

    record["id"] = new_id("c")
    companies.insert(0, record)
    return record


def get_company(data: dict, cid: str) -> dict | None:
    for item in data.get("companies", []):
        if item.get("id") == cid:
            return item
    return None


def remove_company(data: dict, cid: str) -> bool:
    companies = data.get("companies", [])
    keep = [c for c in companies if c.get("id") != cid]
    if len(keep) == len(companies):
        return False
    data["companies"] = keep
    # 岗位引用了这家公司的，把引用清掉，避免悬空
    for job in data.get("jobs", []):
        if job.get("company_id") == cid:
            job["company_id"] = ""
    return True


# --------------------------------------------------------------------------
# 岗位记录
# --------------------------------------------------------------------------


def add_job(data: dict, job: dict) -> dict:
    """写入一条岗位分析记录（放最前面）。"""
    record = dict(job)
    record.setdefault("id", new_id("j"))
    record.setdefault("created", now_text())
    record.setdefault("star", False)
    data.setdefault("jobs", []).insert(0, record)
    return record


def get_job(data: dict, jid: str) -> dict | None:
    for item in data.get("jobs", []):
        if item.get("id") == jid:
            return item
    return None


def remove_job(data: dict, jid: str) -> bool:
    jobs = data.get("jobs", [])
    keep = [j for j in jobs if j.get("id") != jid]
    if len(keep) == len(jobs):
        return False
    data["jobs"] = keep
    return True


# --------------------------------------------------------------------------
# 收藏标记
# --------------------------------------------------------------------------


def set_star(data: dict, kind: str, oid: str, value: bool) -> bool:
    """kind 取 'company' 或 'job'。返回是否找到并改到。"""
    bucket = "companies" if kind == "company" else "jobs"
    for item in data.get(bucket, []):
        if item.get("id") == oid:
            item["star"] = bool(value)
            return True
    return False


def toggle_star(data: dict, kind: str, oid: str) -> bool:
    bucket = "companies" if kind == "company" else "jobs"
    for item in data.get(bucket, []):
        if item.get("id") == oid:
            item["star"] = not bool(item.get("star"))
            return True
    return False


# --------------------------------------------------------------------------
# 面试真题库（用户面完自己记下来，下次分析同类岗位时注入提示词）
# --------------------------------------------------------------------------

INTERVIEW_FIELDS: list[tuple[str, str]] = [
    ("date", "面试日期"),
    ("company", "公司"),
    ("title", "岗位"),
    ("round", "轮次"),
    ("outcome", "结果"),
    ("feeling", "现场感受"),
]


def blank_interview() -> dict:
    return {
        "id": "",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "company": "",
        "title": "",
        "round": "",
        "outcome": "",
        "feeling": "",
        "questions": [],
        "items": [],      # [{q, my, better}]：问题 / 我的回答 / 优化回答，一一对应
        "job_id": "",
        "created": now_text(),
    }


def clean_interview(payload: dict) -> dict:
    item = blank_interview()
    for key in ("company", "title", "round", "outcome", "feeling", "job_id", "date"):
        value = payload.get(key, "")
        item[key] = "" if value is None else str(value).strip()

    # 新版：带答案的问答对。缺字段补空串，保证每条结构一致
    items: list[dict] = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        q = str(raw.get("q") or "").strip()
        if not q:
            continue
        items.append(
            {
                "q": q,
                "my": str(raw.get("my") or "").strip(),
                "better": str(raw.get("better") or "").strip(),
            }
        )
    item["items"] = items

    # questions 是旧字段，也是"只有题目"时的简写。有 items 就由 items 派生，
    # 两者永远保持一致，避免一个地方改了另一个还是老样子。
    if items:
        item["questions"] = [x["q"] for x in items]
    else:
        questions = payload.get("questions") or []
        if isinstance(questions, str):
            questions = [q.strip() for q in questions.splitlines()]
        item["questions"] = [str(q).strip() for q in questions if str(q).strip()]
        item["items"] = [{"q": q, "my": "", "better": ""} for q in item["questions"]]
    return item


def interview_items(item: dict) -> list[dict]:
    """统一读出问答对。老记录只有 questions，这里补成同样结构。"""
    items = item.get("items")
    if isinstance(items, list) and items:
        out = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            q = str(raw.get("q") or "").strip()
            if not q:
                continue
            out.append(
                {
                    "q": q,
                    "my": str(raw.get("my") or "").strip(),
                    "better": str(raw.get("better") or "").strip(),
                }
            )
        if out:
            return out
    return [
        {"q": str(q).strip(), "my": "", "better": ""}
        for q in (item.get("questions") or [])
        if str(q).strip()
    ]


def interview_titles(data: dict) -> list[str]:
    """题库里出现过的岗位名，按出现次数从多到少排。筛选用。"""
    counts: dict[str, int] = {}
    for item in data.get("interviews", []):
        title = str(item.get("title") or "").strip()
        if title:
            counts[title] = counts.get(title, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def add_interview(data: dict, payload: dict) -> dict:
    record = clean_interview(payload)
    record["id"] = new_id("i")
    record["created"] = now_text()
    data.setdefault("interviews", []).insert(0, record)
    return record


def get_interview(data: dict, iid: str) -> dict | None:
    for item in data.get("interviews", []):
        if item.get("id") == iid:
            return item
    return None


def remove_interview(data: dict, iid: str) -> bool:
    items = data.get("interviews", [])
    keep = [x for x in items if x.get("id") != iid]
    if len(keep) == len(items):
        return False
    data["interviews"] = keep
    return True


def _core_name(text: str) -> str:
    """把公司/岗位名压成可比较的核心词：去掉常见后缀与空白。"""
    raw = "".join(str(text or "").split())
    for suffix in ("股份有限公司", "有限责任公司", "有限公司", "集团", "公司", "科技"):
        raw = raw.replace(suffix, "")
    return raw


def matched_questions(data: dict, company: str = "", title: str = "", limit: int = 12) -> list[dict]:
    """找出同一家公司或同类岗位的真题，返回 [{question, company, title, date}]。

    匹配规则故意宽松：公司名互相包含就算同一家（"牧原食品" vs "牧原食品股份有限公司"）；
    岗位名只要有一方包含另一方就算同类（"财务管培生" vs "财务管培生（成本核算）"）。
    宽一点的好处是真题能被用上，坏处是会混进别的岗位的题——所以界面上会标来源。
    """
    comp = _core_name(company)
    post = _core_name(title)
    hits: list[dict] = []
    for item in data.get("interviews", []):
        item_comp = _core_name(item.get("company", ""))
        item_post = _core_name(item.get("title", ""))
        same_company = bool(comp and item_comp and (comp in item_comp or item_comp in comp))
        same_post = bool(post and item_post and (post in item_post or item_post in post))
        if not (same_company or same_post):
            continue
        for pair in interview_items(item):
            hits.append(
                {
                    "question": pair["q"],
                    "my": pair.get("my", ""),
                    "better": pair.get("better", ""),
                    "company": item.get("company", ""),
                    "title": item.get("title", ""),
                    "date": item.get("date", ""),
                }
            )
            if len(hits) >= limit:
                return hits
    return hits


def interviews_to_csv(data: dict) -> str:
    """真题库导出：一行一道题，含我的回答和优化回答。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["面试日期", "公司", "岗位", "轮次", "结果", "题目", "我的回答", "优化回答", "现场感受"]
    )
    for item in data.get("interviews", []):
        pairs = interview_items(item) or [{"q": "", "my": "", "better": ""}]
        for pair in pairs:
            writer.writerow(
                [
                    item.get("date", ""),
                    item.get("company", ""),
                    item.get("title", ""),
                    item.get("round", ""),
                    item.get("outcome", ""),
                    pair.get("q", ""),
                    pair.get("my", ""),
                    pair.get("better", ""),
                    item.get("feeling", ""),
                ]
            )
    return "\ufeff" + buf.getvalue()


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------


def companies_to_csv(data: dict, only_star: bool = False) -> str:
    """公司库导出 CSV 文本（带 BOM，Excel 打开不乱码）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in COMPANY_FIELDS] + ["我的备注", "已收藏"])

    for item in data.get("companies", []):
        if only_star and not item.get("star"):
            continue
        writer.writerow(
            [item.get(key, "") for key in COMPANY_KEYS]
            + [item.get("note", ""), "是" if item.get("star") else ""]
        )
    return "\ufeff" + buf.getvalue()


def jobs_to_csv(data: dict, only_star: bool = False) -> str:
    """岗位记录导出 CSV。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["分析时间", "公司", "岗位", "我的身份", "最高学历", "院校层次", "整体匹配度",
         "硬性门槛", "技能匹配", "经验匹配", "地点与节奏", "成长空间", "岗位画像", "已收藏"]
    )
    for job in data.get("jobs", []):
        if only_star and not job.get("star"):
            continue
        scores = job.get("scores") or {}
        writer.writerow(
            [
                job.get("created", ""),
                job.get("company", ""),
                job.get("title", ""),
                job.get("identity", ""),
                job.get("education", ""),
                job.get("school_tier", ""),
                job.get("level", ""),
                scores.get("hard", ""),
                scores.get("skill", ""),
                scores.get("experience", ""),
                scores.get("location", ""),
                scores.get("potential", ""),
                job.get("role_summary", ""),
                "是" if job.get("star") else "",
            ]
        )
    return "\ufeff" + buf.getvalue()

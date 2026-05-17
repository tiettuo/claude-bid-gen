#!/usr/bin/env python3
"""
bid_common.py - /bid-gen 技能公共模块

集中提供跨脚本复用的工具函数与常量，消除原本散落在
bid_extract / bid_fingerprint / bid_assemble / bid_compliance /
bid_writer / bid_reviewer / bid_similarity / bid_variants 中的重复实现。

所有脚本应只从本模块导入以下接口：
- setup_logger        底层日志构造（独立 log + 可选全局 logs/bid.log）
- make_script_logger  脚本级 logger（自动推导 tender_dir，推荐入口）
- parse_chapter_plan  解析 chapter_plan.md 的章节规划表
- preferred_rules_path 优先返回 authoring_rules.md（不存在则 disqualification_rules.md）
- detect_blind_bid    解析 disqualification_rules.md 判断暗标
- scan_blind_bid_violations  扫描章节 md 中的暗标禁用信息（唯一真值）
- BLIND_PATTERNS      暗标禁用词正则（唯一真值）
- NAME_SUFFIX         人名后缀模式
- sanitize_decorative_chars  暗标装饰字符清洗（【】〖〗★◆■等）
- collect_chapter_files 按数字前缀排序的章节 md 列表
- extract_keywords    从文本抽取 2~6 字中文关键词
- read_project_name   从 project_brain.md 读取项目名
- vary_chapter_title  per-variant H1 标题微变化（反结构收敛）
- derive_variant_skeleton  生成 per-variant H2/H3 章节骨架（反结构收敛）
- extract_headings    Markdown 标题保序抽取（reviewer / similarity 共用）
- strip_prompt_leaks  清除 LLM 回显的 prompt 指令词（如"数据指纹"）
"""

from __future__ import annotations

import logging
import random
import re
import sys
from pathlib import Path


# ─── 统一日志 ─────────────────────────────────────────────────────────

def make_script_logger(name: str, log_path, verbose: bool = False) -> logging.Logger:
    """脚本级 logger：自动推导 tender_dir，挂 tender/logs/bid.log 全局 handler。

    log_path 可以位于 tender/output/、tender/output/VXX/、tender/extracted/ 等任意深度，
    向上查找 output/extracted 父目录即 tender_dir；找不到则退回 log_path.parent.parent。

    用法（替代每脚本自建 setup_logger 的样板）：
        from bid_common import make_script_logger
        log = make_script_logger("bid_xxx", log_path)
    """
    p = Path(log_path)
    cur = p.parent
    while cur != cur.parent and cur.name not in ("output", "extracted"):
        cur = cur.parent
    tender = cur.parent if cur.name in ("output", "extracted") else p.parent.parent
    return setup_logger(name, p, root_log_path=tender / "logs" / "bid.log", verbose=verbose)


def setup_logger(
    name: str,
    log_path: Path,
    root_log_path: Path | None = None,
    verbose: bool = False,
) -> logging.Logger:
    """创建并返回命名 logger。

    Args:
        name: logger 名，同时作为全局日志前缀（如 bid_extract）。
        log_path: 脚本自身的独立日志文件（append 模式）。
        root_log_path: 可选的全局时间线日志文件；传入时额外挂一个 handler
                       共写，条目带 `[name]` 前缀便于跨脚本阅读。
        verbose: True 时终端输出降到 DEBUG。
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    file_fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_fmt = logging.Formatter(
        fmt=f"%(asctime)s [{name}] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(file_fmt)
    logger.addHandler(fh)

    if root_log_path is not None:
        root_log_path = Path(root_log_path)
        root_log_path.parent.mkdir(parents=True, exist_ok=True)
        rh = logging.FileHandler(root_log_path, encoding="utf-8", mode="a")
        rh.setLevel(logging.DEBUG)
        rh.setFormatter(root_fmt)
        logger.addHandler(rh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(file_fmt)
    logger.addHandler(sh)

    return logger


# ─── rules 文件路径优先级（authoring > disqualification）──────────────

def preferred_rules_path(extracted_dir) -> Path:
    """返回优先使用的规则文件路径。

    Phase 3 改造后,bid-gen 同时输出两份文件:
    - authoring_rules.md (方案规则,writer/assemble/charts/plan 应优先读)
    - disqualification_rules.md (合规审计,compliance/reviewer 读)

    本函数返回 authoring_rules.md(存在时)或 disqualification_rules.md(回退)。
    两份文件共享 format / density / blind / must_include 等 YAML 块,
    确保旧消费方代码不改也能继续工作。
    """
    extracted_dir = Path(extracted_dir)
    authoring = extracted_dir / "authoring_rules.md"
    if authoring.exists():
        return authoring
    return extracted_dir / "disqualification_rules.md"


# ─── chapter_plan.md 解析 ─────────────────────────────────────────────

def parse_chapter_plan(plan_path: Path) -> list[dict]:
    """解析章节规划表（兼容旧 7 列与新 10 列格式）。

    旧格式：| 章节号 | 文件名 | 标题 | 评分项 | 分值 | 目标字数 | 写作要点 |
    新格式：| 章节号 | 文件名 | 标题 | 评分项 | 分值 | 子项数 | 目标字数 | 字数带 | 图表数 | 页数估 |

    容错：字段缺失时以 0 占位，target_chars 兜底 max(1500, score*300)。

    Returns:
        list[{num, filename, title, score_item, score, subitem_count,
              target_chars, chart_count, est_pages, notes}]
    """
    plan_path = Path(plan_path)
    if not plan_path.exists():
        return []

    def _num(s: str, typ=int) -> float:
        s = re.sub(r"[^\d.]", "", s or "")
        if not s:
            return 0
        try:
            return typ(float(s))
        except ValueError:
            return 0

    rows: list[dict] = []
    for line in plan_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        if not re.match(r"^\d{1,3}$", cells[0]):
            continue

        score = _num(cells[4], float)
        if len(cells) >= 10:
            # 新 10 列格式
            subitem_count = _num(cells[5])
            target_chars = _num(cells[6])
            chart_count = _num(cells[8])
            est_pages = _num(cells[9], float)
            notes = ""
        else:
            # 旧 7 列格式
            subitem_count = 0
            target_chars = _num(cells[5])
            chart_count = 0
            est_pages = 0.0
            notes = cells[6] if len(cells) > 6 else ""

        rows.append({
            "num": cells[0],
            "filename": cells[1],
            "title": cells[2],
            "score_item": cells[3],
            "score": score,
            "subitem_count": int(subitem_count),
            "target_chars": target_chars or int(max(1500, score * 300)),
            "chart_count": int(chart_count),
            "est_pages": float(est_pages),
            "notes": notes,
        })
    return rows


def parse_plan_budget(plan_path: Path) -> dict:
    """解析 chapter_plan.md 开头的"总量预算"段，抓关键数值。

    返回字段（缺失则为 None）:
      target_pages, target_total_chars, chars_per_page, figure_ratio
    """
    plan_path = Path(plan_path)
    if not plan_path.exists():
        return {}
    text = plan_path.read_text(encoding="utf-8")
    budget = {"target_pages": None, "target_total_chars": None,
              "chars_per_page": None, "figure_ratio": None}
    patterns = {
        "target_pages": r"(?:建议)?目标(?:总)?页数[：:\s*]*(?:\*\*)?([\d.]+)",
        "target_total_chars": r"目标总字数[：:\s*]*[^\n]*?(?:≈|=)\s*\*?\*?([\d,，]{4,})",
        "figure_ratio": r"图表占比[^\d]{0,10}(?:\*\*)?([\d.]+)\s*%",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            raw = m.group(1).replace(",", "").replace("，", "")
            try:
                val = float(raw)
                if key == "figure_ratio":
                    val /= 100.0
                budget[key] = val
            except ValueError:
                pass

    # chars_per_page：优先 "实际 N 字/页"；否则取所有 "N 字/页" 中的最小值
    # （实际文字密度 < 满版理论密度，取小更保险）
    m = re.search(r"实际[^\n]{0,30}?([\d,]+)\s*字\s*/\s*页", text)
    if m:
        budget["chars_per_page"] = float(m.group(1).replace(",", ""))
    else:
        vals = re.findall(r"([\d,]+)\s*字\s*/\s*页", text)
        nums = []
        for v in vals:
            try:
                nums.append(float(v.replace(",", "")))
            except ValueError:
                pass
        if nums:
            budget["chars_per_page"] = min(nums)

    return budget


def parse_plan_subitems(plan_path: Path) -> dict[str, list[dict]]:
    """解析"子项分配"段，返回 {章节号: [{title, chars}, ...]}。

    识别形如 `### 01 xxx（...）` 的章节头 + 其后的 `- X.Y 标题：N 字` 列表项。
    title 自带 "X.Y " 序号前缀(让下游 writer prompt 注入 H2 时直接带号)。
    """
    plan_path = Path(plan_path)
    if not plan_path.exists():
        return {}
    text = plan_path.read_text(encoding="utf-8")
    m = re.search(r"##\s*子项分配[^\n]*\n(.*?)(?=\n##\s|\Z)", text, re.S)
    if not m:
        return {}
    section = m.group(1)
    result: dict[str, list[dict]] = {}
    current: str | None = None
    for line in section.splitlines():
        line_s = line.strip()
        h = re.match(r"^###\s*(\d{1,3})\b", line_s)
        if h:
            current = h.group(1)
            result[current] = []
            continue
        if current is None:
            continue
        # 把 N.M 序号保留进 title("1.1 编制依据与目标")
        li = re.match(r"^[-*]\s*((?:\d+(?:\.\d+)*\s+)?.+?)[：:]\s*([\d,，]+)\s*字", line_s)
        if li:
            chars = int(li.group(2).replace(",", "").replace("，", ""))
            result[current].append({"title": li.group(1).strip(), "chars": chars})
    return result


# ─── 印刷排版常量(TYPOGRAPHY,2026-05-11 Phase 3.2 收敛)─────────────
# 中文字号名 → 磅值、纸张尺寸、Word 实测字数密度
# bid_assemble.py / bid_derive.py 都通过此模块单一引用,避免重复硬编。

def safe_import(name: str, *, log=None, optional: bool = True):
    """统一的可选依赖加载入口。返回模块对象或 None。

    用法:
        jieba = safe_import("jieba", log=log)
        if jieba is not None:
            tokens = list(jieba.cut(text))

    optional=False 时再失败会 raise ImportError(给真正必需的依赖用)。
    log=None 时静默,log=logger 时 DEBUG 级别记录。
    """
    import importlib
    try:
        return importlib.import_module(name)
    except ImportError as e:
        if log is not None:
            log.debug(f"safe_import({name!r}) 失败: {e};走降级路径")
        if not optional:
            raise
        return None


def get_bid_gen_home() -> Path:
    """返回 bid-gen 项目根目录。

    优先用 $BID_GEN_HOME,否则用 scripts/ 的父目录(脚本相对位置)。
    各脚本应统一通过本函数取根目录,避免散落硬编 fallback。
    """
    import os as _os
    env = _os.environ.get("BID_GEN_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


CN_FONT_SIZE_MAP = {
    "初号": 42, "小初": 36,
    "一号": 26, "小一": 24,
    "二号": 22, "小二": 18,
    "三号": 16, "小三": 15,
    "四号": 14, "小四": 12,
    "五号": 10.5, "小五": 9,
}

PAGE_SIZE_CM = {
    "A4": (21.0, 29.7),
    "A3": (29.7, 42.0),
    "LETTER": (21.59, 27.94),
    "B5": (17.6, 25.0),
}


# ─── 章节字数浮动带(圈层理论 ±15%)─────────────────────────────────
# writer 按此生成、reviewer / compliance 按此判定;任一偏离即报 chars_short。
# 其他脚本(bid_expand / bid_plan_verify / bid_writer)内部同值但独立硬编码,
# 若未来要统一收敛,集中改这里 + 替换各处即可。
CHAPTER_CHARS_FLOOR_RATIO = 0.85


def chapter_chars_floor(target: int) -> int:
    """章节字数下限（≥ 85% 目标即通过）。"""
    if not target:
        return 0
    return int(target * CHAPTER_CHARS_FLOOR_RATIO)


# ─── 套话检测器（v3）────────────────────────────────────
_BOILERPLATE_PATTERNS = [
    (r"成立.{0,6}(领导|工作|管理|指挥)小组", "组织套话"),
    (r"建立健?全.{0,10}(体系|制度|机制|网络)", "体系套话"),
    (r"严格(执行|遵守|按照).{0,10}(规定|标准|规范|制度)", "制度套话"),
    (r"加强.{0,6}(管理|监督|检查|考核|教育|培训)", "管理套话"),
    (r"确保.{0,6}(质量|安全|进度|工期|文明)", "承诺套话"),
    (r"坚持.{0,4}(预防为主|安全第一|质量第一|以人为本)", "方针套话"),
    (r"(PDCA|三检制|三级教育|三级交底|三同时|四不放过)", "术语堆砌"),
]
_BOILERPLATE_WEIGHTS = {
    "组织套话": 0.15, "体系套话": 0.15, "制度套话": 0.10,
    "管理套话": 0.10, "承诺套话": 0.08, "方针套话": 0.05, "术语堆砌": 0.12,
}
# ─── 项目指纹（v3: 防跨项目数据串用）───────────────
def project_fingerprint(ext_dir: "Path") -> dict:
    import hashlib
    brain = ext_dir / "project_brain.md"
    if not brain.exists(): return {}
    t = brain.read_text(encoding="utf-8")
    fp = {}
    for k, pat in [("name",r"项目名称[：:\s]*([^\n]+)"),("code",r"(?:招标|项目)编号[：:\s]*([^\n]+)"),("price",r"控制价[：:\s]*([\d,.]+)"),("location",r"建设地点[：:\s]*([^\n]+)")]:
        m = __import__("re").search(pat,t)
        if m:
            v = m.group(1).strip()
            if k=="price":
                try: fp[k]=float(v.replace(",","").replace("，",""))
                except: fp[k]=v
            else: fp[k]=v
    fp["hash"]=hashlib.sha256((fp.get("name","")+fp.get("code","")+str(fp.get("price",""))).encode()).hexdigest()[:12]
    return fp

def detect_boilerplate(text: str) -> dict:
    if not text: return {"ratio":0.0,"hits":[],"verdict":"ok"}
    total = sum(1 for c in text if "\u4e00"<=c<="\u9fff")
    if total<100: return {"ratio":0.0,"hits":[],"verdict":"ok"}
    score=0.0; hits=[]
    for pat,label in _BOILERPLATE_PATTERNS:
        n=len(__import__("re").findall(pat,text))
        if n>0:
            w=_BOILERPLATE_WEIGHTS.get(label,0.1)
            score+=min(1.0,n*w)*(1.0-score*0.5)
            hits.append({"label":label,"count":n,"weight":w})
    r=min(1.0,score)
    v="rewrite" if r>0.35 else ("review" if r>0.20 else "ok")
    return {"ratio":round(r,3),"hits":hits,"verdict":v}

# ─── 清单→章节映射（v3）─────────────────────────
def check_boq_chapter_coverage(ext_dir: "Path", chapter_titles: list[str]) -> dict:
    import json as _json
    bp = ext_dir / "boq.json"
    if not bp.exists(): return {"covered":[],"missing":[],"ratio":1.0}
    try: boq = _json.loads(bp.read_text(encoding="utf-8"))
    except: return {"covered":[],"missing":[],"ratio":1.0}
    cats = boq.get("categories",[]) or []
    if not cats: return {"covered":[],"missing":[],"ratio":1.0}
    cov=[]; mis=[]
    for c in cats:
        n = c.get("name","") if isinstance(c,dict) else str(c)
        if not n: continue
        (cov if any(n in (t or"") or (t or"") in n for t in chapter_titles) else mis).append(n)
    return {"covered":cov,"missing":mis,"ratio":round(len(cov)/len(cats),3) if cats else 1.0}

def count_chinese_chars(text: str) -> int:
    """仅汉字字数（不含标点/空格/ASCII/markdown 符号）。

    chapter_plan 的"目标字数"是语义性字数，writer prompt 明确"图表不计字数"。
    reviewer / compliance 统一用此函数，保证两处报告数字一致、可直接对比。
    """
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff")


# ─── 暗标检测 ─────────────────────────────────────────────────────────

_BOOL_TRUE = ("true", "yes", "y", "是", "1", "on")

# 暗标文本表述正则 — bid_common / bid_assemble 共享
BLIND_TEXT_REGEX = r"(是.{0,4}暗标|技术暗标|采用暗标|暗标.{0,4}[:：是])"

# 暗标敏感场景会触发扫描/清洗的结构化键
BLIND_TRIGGER_KEYS = ("blind_bid", "no_company_info")


def _coerce_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in _BOOL_TRUE


def _parse_blind_flags_from_text(text: str) -> dict:
    """从 disqualification_rules.md 正文解析暗标相关标志。

    Returns: {"blind_bid": bool|None, "no_company_info": bool|None, "text_blind": bool}
    结构化键未出现时返回 None（便于上层区分"显式 False"与"未声明"）。
    """
    flags: dict = {"blind_bid": None, "no_company_info": None, "text_blind": False}
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.DOTALL):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*[：:]\s*(.+)", line)
            if m:
                key = m.group(1).strip()
                if key in BLIND_TRIGGER_KEYS:
                    flags[key] = _coerce_bool(m.group(2).strip().rstrip(",;"))
    flags["text_blind"] = bool(re.search(BLIND_TEXT_REGEX, text))
    return flags


def detect_blind_bid(extracted_dir: Path) -> bool:
    """窄义判定：该项目是否被声明为"暗标"。

    判断顺序：
      1. 结构化参数块中的 blind_bid
      2. 正文出现"是暗标 / 技术暗标 / 采用暗标"等表述

    ⚠ 不含 no_company_info —— 只判"是否暗标"本身。要判"是否该跑公司名扫描/装饰清洗"
    请改用 needs_blind_scan()。
    """
    extracted_dir = Path(extracted_dir)
    path = preferred_rules_path(extracted_dir)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    flags = _parse_blind_flags_from_text(text)
    if flags["blind_bid"] is not None:
        return flags["blind_bid"]
    return flags["text_blind"]


def needs_blind_scan(extracted_dir: Path) -> bool:
    """宽义判定：是否应启用暗标敏感扫描（公司名/签章/装饰字符等）。

    满足任一即 True：
      - 结构化 blind_bid=true
      - 结构化 no_company_info=true（等价暗标扫描效应）
      - 正文出现暗标表述

    这是 reviewer / compliance / assemble 三处共享的唯一决策函数，避免各自判定漂移。
    """
    extracted_dir = Path(extracted_dir)
    path = preferred_rules_path(extracted_dir)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    flags = _parse_blind_flags_from_text(text)
    # 任一显式 True → 启用扫描
    if flags["blind_bid"] or flags["no_company_info"]:
        return True
    # 两者均显式 False → 用户明确非暗标，禁用扫描
    if flags["blind_bid"] is False and flags["no_company_info"] is False:
        return False
    # 其他（至少一个未声明）→ 以正文暗标表述兜底
    return flags["text_blind"]


# ─── 暗标禁用词模式（唯一真值）──────────────────────────────────────

NAME_SUFFIX = r"[\u4e00-\u9fff]{2,4}(?=[,，。；;、\s\n]|$)"

BLIND_PATTERNS: list[tuple[str, str]] = [
    (
        r"[\u4e00-\u9fff]{2,}(?:有限公司|有限责任公司|股份有限公司|集团公司|建设集团|工程公司)",
        "公司全称",
    ),
    (rf"投\s*标\s*人[:：]\s*{NAME_SUFFIX}", "具名投标人"),
    (rf"法定代表人[:：]\s*{NAME_SUFFIX}", "具名法定代表人"),
    (rf"项目经理[:：]\s*{NAME_SUFFIX}", "具名项目经理"),
    (rf"技术负责人[:：]\s*{NAME_SUFFIX}", "具名技术负责人"),
    (r"加盖.{0,6}(?:公章|单位章|印章|法人章)", "盖章动作"),
    (r"(?:公司盖章|单位盖章|法人签字|代表签字|签章确认|骑缝章)", "签章/盖章"),
    # —— 变相身份信息(2026-05-11 R-blind 扩展)——
    # 第一人称自指(常见于 LLM 写施组时无意识带入)
    (r"(?:^|[,，。;；\s])(?:我[们司方公]|我公司|我集团|我方|敝公司|敝单位)", "第一人称自指"),
    (r"(?:^|[,，。;；\s])(?:本公司|本集团|本单位|本企业|本所|本院|本中心)", "本单位自指"),
    # 具名岗位 + 同志/先生/女士(变相署名;名字后直接跟尊称无分隔符,用 {2,4} 不带 lookahead)
    (r"(?:项目经理|技术负责人|总工程师|安全员|质检员|班组长)\s*[一-鿿]{2,4}\s*(?:同志|先生|女士|师傅|工程师)",
     "具名岗位+尊称"),
    # 简历式陈述(名字+动词,只在句首/标点后触发以减少误报)
    (r"(?:^|[,，。;；\s])[一-鿿]{2,4}\s*(?:曾经|历任|现任|担任|主持完成|负责完成|负责实施|曾参与|曾负责)",
     "简历式具名陈述"),
    # 资质证书编号/营业执照(常带公司可识别信息)
    (r"建造师注册编号\s*[:：]?\s*[A-Z0-9]{8,}", "建造师注册编号"),
    (r"统一社会信用代码\s*[:：]?\s*[0-9A-Z]{15,18}", "统一社会信用代码"),
    (r"营业执照\s*(?:号)?\s*[:：]?\s*[0-9A-Z]{15,18}", "营业执照号"),
    (r"安全生产许可证\s*(?:编号)?\s*[:：]?\s*(?:[A-Z]{1,3}[（(]?[A-Z]?[）)]?[0-9-]{6,}|[0-9-]{6,})",
     "安全生产许可证编号"),
    # 我司/本公司+成就陈述(常出现在"业绩介绍"段)
    (r"(?:我[公集司方]|本(?:公司|集团|单位))\s*(?:荣获|获得|取得|拥有|具有|具备|完成|实施|承建|承担)",
     "我司+成就陈述"),
]


# ─── 装饰字符清洗（唯一真值）────────────────────────────────────
# 投标文档通行规范（不限暗标）：装饰符号被视为"做标记/不专业"，应剥除。
# - 剥壳保留内容：【xxx】〖xxx〗 → xxx
# - 直接删除：几何装饰符 ★◆■●◎▲△▼▽◇□☆※•➤
# - 直接删除：勾选符号 ✓ ✅ ✔ ☑ ✗ ☒（Claude 常在"自我核查"段自发打勾）
# 注意：→ ← 不自动删，语义承载（因果/流程），留给 prompt 禁用 + reviewer 警示。
# 注意：· — — · 等合法符号（单位分隔/破折号）不动。

_DECO_BRACKETS_RE = re.compile(r"【([^【】\n]{0,80})】")
_DECO_BRACKETS2_RE = re.compile(r"〖([^〖〗\n]{0,80})〗")
DECO_STRIP_CHARS = "★◆■●◎▲△▼▽◇□☆※•➤✓✅✔☑✗☒"
_DECO_STRIP_TABLE = str.maketrans("", "", DECO_STRIP_CHARS)

# reviewer 用：不清洗但要警示的符号（会破坏语义，靠 prompt 禁用 + 人工复核）
DECO_WARN_CHARS = "→←⇒⇐"


# ─── 输出格式约束（唯一真值源）─────────────────────────────────────────
# bid_writer (_build_block4_output) + bid_rules_synthesize (authoring_constraints) +
# bid_reviewer (deco_warn 检查) 共享。任一处要改请改这里。

OUTPUT_FORMAT_RULES = {
    "table_max_columns": 6,             # Markdown 表格列数硬上限
    "table_remark_max_chars": 15,       # 表格"备注/说明"列字数上限
    "italic_forbidden": True,           # 禁用 Markdown 斜体 *text* / _text_
    "mermaid_forbidden": True,          # 禁用 Mermaid 代码块（docx 不渲染）
    "ascii_art_forbidden": True,        # 禁用 ASCII 字符画（█░ 等）
    "json_block_restricted_to": "进度计划章节",  # JSON 代码块的允许章节范围
}


def render_output_format_constraints_lines(is_subitem: bool = False) -> list[str]:
    """从 OUTPUT_FORMAT_RULES + DECO_STRIP_CHARS + DECO_WARN_CHARS +
    bid_charts._RENDERER_SCHEMAS 派生 prompt 用的 Block 4 / authoring_constraints 文本行。

    单一真值源:任何脚本要列"输出格式约束"都调本函数,不要再硬编码。
    """
    rules = OUTPUT_FORMAT_RULES
    out: list[str] = []

    if is_subitem:
        out.append("输出以 `## 子节标题` 起头，随后是正文；不得输出 `#` 一级标题")
    else:
        out.append("输出以 `# 章节标题` 起头，H1 必须与给定字面一致；H2/H3 顺序/字面/层级一字不动；不得新增/删除 H2")

    out.append("纯 Markdown 输出，不要任何元注释或解释（如 \"以下是...\" 之类）")

    if rules.get("mermaid_forbidden"):
        out.append("禁用 Mermaid 代码块（docx 渲染不出来，组装阶段会被剥离）")

    # 动态 chart types(从 bid_charts._RENDERER_SCHEMAS 派生)
    try:
        from bid_charts import get_chart_type_brief_list  # 延迟导入
        types = get_chart_type_brief_list()
    except Exception:
        types = []
    if types:
        out.append("JSON 图表数据块：文末可附 ```json``` 代码块(组装阶段渲染为 PNG)。**顶层必须有 `\"type\"` 字段(字符串值,非 key 名)**,支持以下类型：")
        for t, brief in types:
            out.append(f"  - `{t}`：{brief}")
        out.append("正确示例(顶层 type 字段)：`{\"type\": \"org_chart\", \"root\": {...}}` ✓")
        out.append("错误示例(把表名当 type)：`{\"施工部署表\": {...}}` ✗ — 会被组装阶段剥离,图缺失")
        out.append("**不能输出 7 种 type 之一时,请用 Markdown 表格代替,严禁输出无 type 字段的 JSON 块**")

    cols = rules.get("table_max_columns", 6)
    remark = rules.get("table_remark_max_chars", 15)
    ascii_part = "；禁 ASCII 字符画（█░ 等）" if rules.get("ascii_art_forbidden") else ""
    out.append(f"Markdown 表格列数 ≤ {cols}；超过 {cols} 列拆成多张小表；备注/说明列 ≤ {remark} 字{ascii_part}")

    if rules.get("italic_forbidden"):
        out.append("禁用 Markdown 斜体 `*text*` / `_text_`，强调一律用 `**粗体**`")

    # 装饰字符黑名单(取自 DECO_STRIP_CHARS + DECO_WARN_CHARS)
    deco_strip = " ".join(DECO_STRIP_CHARS)
    deco_warn = " ".join(DECO_WARN_CHARS)
    out.append(f"禁用装饰字符：【】〖〗 {deco_strip} {deco_warn} —— 流程/因果用文字（导致/依次/则）")

    return out


def scan_blind_bid_violations(
    md_files,
    *,
    company_name: str = "",
    custom_terms=(),
) -> list[dict]:
    """扫描章节 md 中的暗标禁用信息。

    默认规则 = BLIND_PATTERNS；可选追加 company_name（本单位公司名）与 custom_terms（自定义禁用词）。
    是否"仅暗标启用"由调用方判断（本函数无副作用、无日志）。

    Returns:
        list[{file, line, kind, match, context}]
    """
    patterns: list[tuple[str, str]] = list(BLIND_PATTERNS)
    if company_name:
        patterns.append((re.escape(company_name), "本单位公司名"))
    for t in custom_terms or ():
        if t:
            patterns.append((t, "自定义禁用词"))

    hits: list[dict] = []
    for md in md_files:
        try:
            text = Path(md).read_text(encoding="utf-8")
        except OSError:
            continue
        for pat, kind in patterns:
            for m in re.finditer(pat, text):
                line_no = text.count("\n", 0, m.start()) + 1
                ctx = text[max(0, m.start() - 20): m.end() + 20].replace("\n", " ")
                hits.append({
                    "file": Path(md).name, "line": line_no,
                    "kind": kind, "match": m.group(), "context": ctx,
                })
    return hits


def sanitize_decorative_chars(md_files, log: logging.Logger) -> None:
    """就地清洗 md 文件中的装饰性字符（所有投标项目默认执行，非仅暗标）。

    - 【xxx】 / 〖xxx〗 → 剥壳保留内容
    - DECO_STRIP_CHARS 中字符 → 直接删除（几何装饰 + 勾选类）
    - → ← 不动（语义承载，由 prompt 禁用 + reviewer 警示）
    仅当内容有变化时回写；记录每个文件的替换次数。
    """
    total_files_modified = 0
    total_replacements = 0
    for md in md_files:
        try:
            orig = md.read_text(encoding="utf-8")
        except OSError as e:
            log.warning(f"  清洗跳过（读取失败）: {md.name}  {e}")
            continue

        text, n_br1 = _DECO_BRACKETS_RE.subn(r"\1", orig)
        text, n_br2 = _DECO_BRACKETS2_RE.subn(r"\1", text)
        cleaned = text.translate(_DECO_STRIP_TABLE)
        n_deco = len(text) - len(cleaned)

        if cleaned != orig:
            md.write_text(cleaned, encoding="utf-8")
            total_files_modified += 1
            total_replacements += n_br1 + n_br2 + n_deco
            log.info(f"  装饰字符清洗: {md.name}  剥壳 {n_br1 + n_br2} 处 / 删除装饰符 {n_deco} 处")

    if total_replacements:
        log.info(f"装饰字符清洗完成：{total_files_modified} 个文件共 {total_replacements} 处替换（防暗标做标记判定）")
    else:
        log.info("装饰字符清洗：未发现需清洗的字符 ✓")


# ─── prompt 回显清洗 ──────────────────────────────────────────────────

_PROMPT_LEAK_RE = re.compile(
    r"(?:本章)?数据指纹"
    r"|差异化技术参数"
    r"|差异化参数"
    r"|必须体现.*?区别于其他投标"
    r"|融入正文.*?严禁.*?指纹"
)

_PROMPT_LEAK_LINE_RE = re.compile(
    r"^[^\S\n]*(?:[#*\->\d.）)]*\s*)*"
    r"(?:本章数据指纹|数据指纹|差异化技术参数|差异化参数)"
    r"[^\n]*$",
    re.MULTILINE,
)


def strip_prompt_leaks(md_files, log: logging.Logger) -> None:
    """清除 LLM 回显的 prompt 指令词（如"数据指纹"）。"""
    total_files = 0
    total_hits = 0
    for md in md_files:
        try:
            orig = md.read_text(encoding="utf-8")
        except OSError:
            continue
        text = _PROMPT_LEAK_LINE_RE.sub("", orig)
        text = _PROMPT_LEAK_RE.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        n = len(orig) - len(text)
        if n > 0:
            md.write_text(text, encoding="utf-8")
            total_files += 1
            total_hits += n
            log.info(f"  prompt 回显清洗: {md.name}  删除 {n} 字符")
    if total_hits:
        log.info(f"prompt 回显清洗完成：{total_files} 个文件共 {total_hits} 字符")
    else:
        log.info("prompt 回显清洗：未发现泄露 ✓")


# ─── 章节文件收集 ─────────────────────────────────────────────────────

def collect_chapter_files(chapter_dir: Path) -> list[Path]:
    """按数字前缀稳健排序章节 md 文件（兼容 >= 10 章）。

    过滤规则：
      - 文件名以数字开头
      - 排除"报告"/"检查"字样（避免把合规报告当章节）
    """
    chapter_dir = Path(chapter_dir)

    def sort_key(p: Path):
        m = re.match(r"^(\d+)", p.stem)
        return (0, int(m.group(1))) if m else (1, p.stem)

    candidates = [
        p for p in chapter_dir.glob("*.md")
        if re.match(r"^\d", p.stem) and "报告" not in p.name and "检查" not in p.name
    ]
    return sorted(candidates, key=sort_key)


# ─── 关键词抽取 ───────────────────────────────────────────────────────

def extract_keywords(text: str) -> list[str]:
    """从中文文本抽取 2~6 字关键词（去重，顺序不保证稳定）。"""
    if not text:
        return []
    return list({w for w in re.findall(r"[\u4e00-\u9fff]{2,6}", text) if len(w) >= 2})


# ─── 项目名读取 ───────────────────────────────────────────────────────

def read_project_name(extracted_dir: Path, fallback: str = "施工组织设计") -> str:
    """从 project_brain.md 的"## 一、项目基本信息"段内读取项目名。"""
    extracted_dir = Path(extracted_dir)
    brain = extracted_dir / "project_brain.md"
    if not brain.exists():
        return fallback
    text = brain.read_text(encoding="utf-8")
    info_section = re.search(
        r"##\s*一、?\s*项目基本信息[^\n]*\n(.*?)(?=\n##\s|$)",
        text, flags=re.DOTALL,
    )
    scope = info_section.group(1) if info_section else text[:1500]
    # 兼容多种 markdown 写法:
    #   - 项目名称:xxx
    #   - **项目名称**:xxx
    #   * **项目名称**:xxx
    #     项目名称:xxx (无前缀)
    m = re.search(
        r"^[\s\-\*]*\*{0,2}\s*项目名称\s*\*{0,2}\s*[：:]\s*(.+?)\s*$",
        scope, flags=re.MULTILINE,
    )
    if not m:
        return fallback
    name = m.group(1).strip()
    # 去掉值末尾可能残留的 markdown 加粗符号
    name = re.sub(r"\*{1,2}$", "", name).strip()
    return name or fallback


# ─── H1 标题 per-variant 微变化 ───────────────────────────────────────

_H1_CONJ_SWAP = {"与": ["与", "及", "和"], "及": ["与", "及", "和"], "和": ["与", "及", "和"]}


def vary_chapter_title(base_title: str, variant: dict | None) -> str:
    """per-variant 章节 H1 标题微变化，避免 6 份方案完全一致的结构指纹。

    变换规则（确定性，同 variant+title → 同结果）：
    1. 连词互换：与 ↔ 及 ↔ 和
    2. 可选前缀：加"工程"或"施工"
    3. "措施"后缀变体：措施 / 保障措施 / 保证措施
    约 30% 概率保持原标题不变。
    """
    if variant is None or "section_order_seed" not in variant:
        return base_title

    title_hash = sum(ord(c) * (i + 1) for i, c in enumerate(base_title)) & 0xFFFFFFFF
    seed = int(variant["section_order_seed"]) ^ title_hash ^ 0x48314449
    rng = random.Random(seed)

    if rng.random() < 0.3:
        return base_title

    title = base_title

    for old_conj, alternatives in _H1_CONJ_SWAP.items():
        if old_conj in title:
            title = title.replace(old_conj, rng.choice(alternatives), 1)
            break

    prefix = rng.choice([None, None, None, "工程", "施工"])
    if prefix and not title.startswith(prefix) and prefix not in title:
        title = prefix + title

    if title.endswith("与措施") or title.endswith("及措施") or title.endswith("和措施"):
        suffix = rng.choice(["措施", "措施", "保障措施", "保证措施"])
        if suffix != "措施":
            title = title[:-2] + suffix
    elif title.endswith("体系措施"):
        conj = rng.choice(["与", "及", "和", ""])
        if conj:
            title = title[:-2] + conj + "措施"

    return title


# ─── 章节骨架 per-variant 多样化（反结构收敛）────────────────────────

# 原子要点切分：顿号、逗号、分号、顿分句、"和"、斜杠
_NOTES_SPLIT_RE = re.compile(r"[、，,；;/]|\s和\s|\s与\s")

# H2 命名模板（seed 决定选哪一套）
_H2_TEMPLATES: list[callable] = [
    lambda i, t: f"2.{i} {t}",
    lambda i, t: f"{'一二三四五六七八九十'[i-1] if i <= 10 else str(i)}、{t}",
    lambda i, t: f"{i}. {t}",
    lambda i, t: f"（{'一二三四五六七八九十'[i-1] if i <= 10 else str(i)}）{t}",
]

# phase 风格的外壳候选
_PHASE_WRAPPERS = [
    ["前期策划", "过程实施", "后期验收"],
    ["总体策略", "专项措施", "保障机制"],
    ["编制依据", "实施方案", "监督检查"],
    ["组织层面", "技术层面", "管理层面"],
]


def _split_notes(notes: str) -> list[str]:
    """把 chapter['notes'] 切成原子要点，丢空、去头尾空白。"""
    if not notes:
        return []
    parts = [p.strip(" 　…·-") for p in _NOTES_SPLIT_RE.split(notes)]
    return [p for p in parts if p and len(p) >= 2]


def _apply_template(points: list[str], tpl: callable) -> list[str]:
    return [tpl(i + 1, p) for i, p in enumerate(points)]


def derive_variant_skeleton(chapter: dict, variant: dict | None) -> list[dict]:
    """生成本 (variant, chapter) 的 H2/H3 章节骨架。

    Returns:
        list[{level:int, title:str, children:list[str]}]
        children 里是若干 H3 标题字符串（可空）。

    单份模式（variant is None 或缺 section_order_seed）行为：
      原子要点原顺序 → 每个一个 H2，无 H3。与旧 writer 的默认结构等价。

    批量模式：按 section_order_seed 与 chapter['num'] 混合出的 PRNG 做：
      1) 原子要点 shuffle
      2) 三种结构风格之一：flat / grouped / phase
      3) 四种 H2 命名模板之一
    """
    points = _split_notes(chapter.get("notes", ""))
    # notes 为空时兜底一个通用骨架，避免 writer 彻底无结构可用
    if not points:
        points = ["总体策略", "关键措施", "实施要点", "过程监控"]

    if variant is None or "section_order_seed" not in variant:
        return [{"level": 2, "title": p, "children": []} for p in points]

    try:
        ch_num = int(re.sub(r"\D", "", str(chapter.get("num", "0"))) or "0")
    except ValueError:
        ch_num = 0
    seed = int(variant["section_order_seed"]) ^ (ch_num * 2654435761 & 0xFFFFFFFF)
    rng = random.Random(seed)

    shuffled = points[:]
    rng.shuffle(shuffled)

    style = rng.choice(["flat", "grouped", "phase"]) if len(shuffled) >= 3 else "flat"
    tpl = rng.choice(_H2_TEMPLATES)

    if style == "flat":
        titles = _apply_template(shuffled, tpl)
        return [{"level": 2, "title": t, "children": []} for t in titles]

    if style == "grouped":
        # 每 H2 下挂 2~3 个原子要点作为 H3
        group_size = rng.choice([2, 3])
        groups: list[list[str]] = []
        for i in range(0, len(shuffled), group_size):
            groups.append(shuffled[i:i + group_size])
        # H2 标题用首个要点的前几个字或"第 N 组要点"作概括
        h2_titles = []
        for idx, g in enumerate(groups):
            stem = g[0][:6] if len(g[0]) >= 2 else f"要点组{idx+1}"
            h2_titles.append(f"{stem}等")
        h2_titles = _apply_template(h2_titles, tpl)
        return [
            {"level": 2, "title": h2_titles[i], "children": groups[i]}
            for i in range(len(groups))
        ]

    # phase
    wrapper = rng.choice(_PHASE_WRAPPERS)
    # 把 shuffled 按顺序分配到 3 个 wrapper
    buckets: list[list[str]] = [[], [], []]
    for i, p in enumerate(shuffled):
        buckets[i % 3].append(p)
    h2_titles = _apply_template(wrapper, tpl)
    return [
        {"level": 2, "title": h2_titles[i], "children": buckets[i]}
        for i in range(3)
    ]


def render_skeleton_for_prompt(skeleton: list[dict]) -> str:
    """把 skeleton 渲染成 writer prompt 用的嵌套列表文本。"""
    lines = []
    for i, node in enumerate(skeleton, 1):
        lines.append(f"{i}. ## {node['title']}")
        for j, child in enumerate(node.get("children") or [], 1):
            lines.append(f"   {i}.{j} ### {child}")
    return "\n".join(lines)


def extract_headings(text: str, levels: tuple[int, ...] = (2, 3, 4)) -> list[tuple[int, str]]:
    """按出现顺序抽取 Markdown 标题。levels 指定要抓的层级。"""
    result: list[tuple[int, str]] = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not m:
            continue
        lvl = len(m.group(1))
        if lvl in levels:
            result.append((lvl, m.group(2).strip()))
    return result


# ─── 自检入口 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="bid_common 自检")
    p.add_argument("tender_dir", nargs="?", help="可选：对某投标目录做解析自检")
    args = p.parse_args()

    if args.tender_dir:
        d = Path(args.tender_dir).expanduser().resolve()
        ext = d / "extracted"
        print(f"tender_dir: {d}")
        print(f"blind_bid: {detect_blind_bid(ext)}")
        print(f"project_name: {read_project_name(ext)}")
        plan = parse_chapter_plan(ext / "chapter_plan.md")
        print(f"chapter_plan: {len(plan)} 章")
        for r in plan[:3]:
            print(f"  {r['num']} | {r['filename']} | {r['title'][:20]} | {r['score']}")
    else:
        print("bid_common ok. 接口：")
        for name in ("setup_logger", "make_script_logger", "parse_chapter_plan",
                     "detect_blind_bid", "needs_blind_scan", "scan_blind_bid_violations",
                     "collect_chapter_files", "sanitize_decorative_chars",
                     "extract_keywords", "read_project_name"):
            print(f"  - {name}")
        print(f"  - BLIND_PATTERNS: {len(BLIND_PATTERNS)} 条")

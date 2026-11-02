#!/usr/bin/env python3
"""
bid_compliance.py - 投标技术文件自动合规扫描
扫描 output/ 下所有 .md 章节文件，交叉比对 disqualification_rules.md 中的要求，
输出机械可检项的结果（供 Claude 做 Step 5 合规检查时引用）。

检查项：
  1. 暗标禁用信息（公司名/具名岗位/盖章动作等）
  2. 关键响应词是否出现（工期日历天、质量标准、强制响应项）
  3. 章节字数统计（对照 chapter_plan 目标字数）
  4. 章节-评分表对齐（章节数量/标题/分值与 chapter_plan.md 一致）

用法: python bid_compliance.py /path/to/tender/
运行日志: extracted/run.log
"""

from __future__ import annotations

import sys
import re
import json
from bid_common import get_config_dir  # noqa: E402
import logging
from datetime import date, datetime as _dt, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import (  # noqa: E402
    make_script_logger,
    parse_chapter_plan,
    collect_chapter_files,
    scan_blind_bid_violations,
    needs_blind_scan,
    chapter_chars_floor,
    count_chinese_chars,
)
from bid_plan_verify import topic_covered  # noqa: E402  复用语义匹配

# 关键响应词提取规则：从 disqualification_rules.md 自动挖出数字+单位组合
RESPONSE_RULES = [
    # (label, 正则-从规则文件中提取, 是否必须在 output 中出现)
    # 工期响应:优先取结构化字段 schedule_milestones.total_duration_days(见 check_response)。
    # 正则必须收紧到「工期」语境——裸 `(\d{2,4}\s*日历天)` 会先命中废标摘要里的
    # 「投标有效期：90 日历天」,把投标有效期当工期响应词 → 稳定误报「工期未响应」。
    ("工期响应", r"(?:总?工期|计划工期)[^\n。；]{0,12}?(\d{2,4})\s*日历天", True),
    ("暗标响应", r"(技术暗标|暗标格式)", False),
    ("双代号网络图", r"(双代号网络图)", False),
]


def _normalize_heading_title(title: str) -> str:
    """章节标题比较口径：忽略开头展示标记和空白，但不吞掉真实标题差异。"""
    title = re.sub(r"^[\s★☆⭐\ufe0f]+", "", title or "")
    return re.sub(r"\s+", "", title)



# ─── 标准编造检测（P0.6）───────────────────────────────────────────────
_STANDARD_RE = re.compile(
    r"(GB(?:\s*/\s*T)?|JGJ(?:\s*/\s*T)?|CJJ|DB54(?:\s*/\s*T)?)\s*(\d+)(?:[.\-](\d{4}))?"
)

def load_standards_truth(config_dir: Path) -> dict:
    """加载标准版本真值表"""
    truth_path = config_dir / "standards_truth.json"
    if truth_path.exists():
        with open(truth_path) as f:
            data = json.load(f)
        return data.get("standards", {})
    return {}


def normalize_standard_key(prefix: str, num: str) -> str:
    """归一化标准编号键:去空格后拼接前缀与编号。如 ("GB / T","50502")→"GB/T50502"。"""
    return f"{(prefix or '').replace(' ', '')}{(num or '').strip()}"


def standard_lookup_candidates(prefix: str, num: str) -> list[str]:
    """返回标准在真值表中的候选键:精确写法优先,再补 GB↔GB/T 变体。

    招标方案常把 GB/T 标准误写成 GB(或反之),真值表只存一种写法,
    故两种写法都要查。
    """
    key = normalize_standard_key(prefix, num)
    if "/T" in key:
        return [key, key.replace("/T", "")]
    variant = re.sub(r"^(GB|JGJ|CJJ|DB54)", r"\1/T", key)
    return [key, variant] if variant != key else [key]


def check_standards_fabrication(md_files: list[Path], truth: dict, log: logging.Logger) -> dict:
    """扫描章节文件中的标准引用，检测编造的年份版本。
    
    Returns: {"total": int, "fabricated": int, "details": list[dict]}
    """
    total = 0
    fabricated = 0
    details = []
    
    for f in md_files:
        text = f.read_text(encoding="utf-8")
        for m in _STANDARD_RE.finditer(text):
            prefix = m.group(1)
            num = m.group(2)
            year = m.group(3)
            total += 1

            true_year = None
            matched_key = None
            for k in standard_lookup_candidates(prefix, num):
                if k in truth:
                    true_year = truth[k]
                    matched_key = k
                    break

            if true_year and year and year != true_year:
                fabricated += 1
                details.append({
                    "file": f.name,
                    "standard": matched_key,
                    "cited_year": year,
                    "true_year": true_year,
                    "text": m.group(0),
                })
    result = {"total": total, "fabricated": fabricated, "details": details}
    if fabricated:
        log.warning(f"标准编造检测: {fabricated}/{total} 条年份编造")
    else:
        log.info(f"标准编造检测: {total} 条引用，无编造")
    return result


# ─── 工期废标门（网络图关键线路 / 进度跨度 / 声明总工期 须 ≤ 招标工期）──────────
def _aoa_critical_path(activities: list) -> int:
    """双代号网络图最长路径(关键线路)天数。activities:[{from,to,duration,...}]。"""
    edges: dict[int, list] = {}
    nodes: set[int] = set()
    for a in activities or []:
        try:
            fr = int(a.get("from")); to = int(a.get("to"))
            dur = float(a.get("duration", 0) or 0)
        except (TypeError, ValueError):
            continue
        edges.setdefault(fr, []).append((to, dur))
        nodes.add(fr); nodes.add(to)
    if not nodes:
        return 0
    best = {n: 0.0 for n in nodes}  # 事件编号即拓扑序(from<to),DP 求最长路径
    for n in sorted(nodes):
        for to, dur in edges.get(n, []):
            if best[n] + dur > best.get(to, 0.0):
                best[to] = best[n] + dur
    return int(round(max(best.values())))


def _schedule_span(tasks: list) -> int:
    """进度横道图跨度(天)= max(end) - min(start) + 1。"""
    from datetime import datetime as _dt
    ds = []
    for t in tasks or []:
        for k in ("start", "end"):
            v = t.get(k)
            if not v:
                continue
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
                try:
                    ds.append(_dt.strptime(str(v).strip(), fmt)); break
                except ValueError:
                    continue
    return (max(ds) - min(ds)).days + 1 if len(ds) >= 2 else 0


def _parse_abs_date(value) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return _dt.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    m = re.match(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日?", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _schedule_absolute_dates(data: dict) -> list[tuple[str, date]]:
    hits: list[tuple[str, date]] = []
    for key in ("startDate", "endDate", "start_date", "end_date", "baseline_date"):
        d = _parse_abs_date(data.get(key))
        if d:
            hits.append((key, d))
    project = data.get("project") or {}
    if isinstance(project, dict):
        for key in ("start_date", "end_date", "baseline_date"):
            d = _parse_abs_date(project.get(key))
            if d:
                hits.append((f"project.{key}", d))
    for idx, task in enumerate(data.get("tasks") or data.get("schedule") or [], 1):
        if not isinstance(task, dict):
            continue
        for key in ("start", "end", "start_date", "end_date", "begin", "finish"):
            d = _parse_abs_date(task.get(key))
            if d:
                hits.append((f"tasks[{idx}].{key}", d))
    for idx, ms in enumerate(data.get("milestones") or [], 1):
        if not isinstance(ms, dict):
            continue
        d = _parse_abs_date(ms.get("date"))
        if d:
            hits.append((f"milestones[{idx}].date", d))
    return hits


# ── 正文禁日期豁免（全部按【日期级】判断，禁止整行豁免——整行豁免会漏报混排行如
#    「本规范自2024-08-01起实施。计划2026-09-01开工。」和「开工令于2026-08-01发布」）──
# 引用来源类：投标截止/公告/发布时间/获取/造价/保险等引用日期（如「招标公告发布日期」）。
# 「投标」收窄为「投标截止」——「本工程投标后于X日开工」是工期语义，不得豁免；
# 「发布」收窄为「发布时间」——「开工令于X日发布」是工期语义（无「时间」二字），不得豁免。
# 只扫日期左侧上下文：引用来源词必然在日期前（「公告发布日期为X」），右侧「X开工，详见公告」是工期语义。
# ★ 「公告」必须收紧成「公告发布/刊登/挂网」:「招标公告明确开工日期为2026年8月1日」
#   里的「公告」只是普通主语,左侧 12 字窗口含它会把真实工期日期整条豁免掉。
_REFERENCE_CTX_RE = re.compile(
    r"(投标截止|开标(?:时间|日期)?|递交截止|提交截止|公示(?:期|日期|时间)?|评标(?:日期|时间)?"
    r"|报名截止|公告(?:发布|刊登|挂网)|发布日期|发布时间|获取|领取|养老保险|造价|信息价"
    # 证件有效期类事实「安全生产许可证有效期至2027年6月30日」不是排期日期
    r"|有效期|届满|到期)")
# 引用词与日期之间只允许“日期/时间/为/是”这类连接字——
# 「获取开工许可后于X开工」「信息价采集完成后于X进场」夹着实词,不得豁免。
_REF_GLUE_RE = re.compile(r"^[日时期间为之是编号至到：:\s]{0,5}$")
# 规范生效类：规范类词后紧跟（最多 1 个标点）「自/从 + 日期 + 起实施/施行/生效」结构，
# 如「本规范自2024年8月1日起实施」。只豁免被该结构匹配覆盖的那个日期，其余日期照报。
# 规范类词不含「规定」——「按规定实施X」是施工语义。
# 规范生效类：规范类词后紧跟（最多 1 个标点）「自/从 + 日期 + 起实施/施行/生效」结构，
# 如「本规范自2024年8月1日起实施」。只豁免被该结构匹配覆盖的那个日期，其余日期照报。
# 规范类词不含「规定」——「按规定实施X」是施工语义。
_STANDARD_EFFECT_RE = re.compile(
    # 允许标准名后紧跟书名号/引号收尾符(「《建设工程质量管理条例》自…施行」),
    # 但不允许其他实词插入——「《验收统一标准》，本工程自…实施」仍必须被报出。
    # ★ 「法」必须入表:「《安全生产法》自2021年9月1日起施行」是法规引用,
    #   漏了它会被判成工期日期泄漏 → 无谓重写 + 重写后仍命中 → 合规门禁 abort。
    #   (?<![方做手技工]) 避开「方法/做法/手法/技法/工法」这类普通名词
    #   (「施工工法自…实施」不是法规引用,不得豁免)。
    r"(规范|标准|办法|规程|条例|规则|指南|文件|(?<![方做手技工])法)[》」”’)\]】]*"
    # 法规发文字号括号:「《建设工程质量管理条例》(国务院令第279号)自…施行」
    # 中括号内必须含号/令/批准等发文字号特征词,避免把「标准》，本工程…」一并豁免。
    # 括号内允许发文字号(国务院令第279号)与规范编号(GB/T 50502-2009 / JGJ 59-2011)
    r"(?:[（(][^）)]{0,30}(?:令|号|批准|发布|修正|修订|[A-Z]{2,4}(?:\s*/?\s*T)?\s*\d{2,6})[^）)]{0,30}[）)])?"
    r"[，、。]?(?:自|从)\s*"
    r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?(?:起|之日起)?(?:实施|施行|生效)"
)
# 历史事实类（获奖/公司成立/业绩等）：这些日期必然是过去的事实，不可能是计划工期。
# 「成立」仅限「公司/企业/单位/我司…成立」模式——「于2026-08-01成立项目部」是排期里程碑，不得豁免。
_HISTORICAL_CTX_RE = re.compile(
    r"(获奖|荣获|获[^。，；\n]{0,8}奖|荣誉|沿革|历史|业绩|职称|招生|开学|搬迁"
    # 既成事实类日期(不可能是计划工期):体系认证/取证/贯标/颁证/授牌。
    # ★ 不得加入 竣工/验收/备案/许可/资质 等词:「本工程于2027-10-01竣工」
    #   「于2026-10-02取得施工许可」都是真实计划工期日期,加了会把它们整条放行。
    r"|认证|取证|贯标|颁证|授牌"
    r"|(?:公司|企业|单位|我司|本司)[^。，；\n]{0,6}成立)"
)
# 流程类（送检/检测等）：日期紧邻这些词才是「已发生事实」。
# 不含「试验」——「于X日进行试验段施工」是排期里程碑，不得豁免。
# ★ 豁免必须限在同一小句内且距离近:±12 字窗口里夹着句末标点时,
#   「本工程计划于2026年8月1日开工。混凝土试块送检合格后方可浇筑。」会把真实工期日期放行。
_PROCESS_CTX_RE = re.compile(r"(送检|检测|校准|复检|见证)")
# 小句边界(含顿号/逗号):历史事实与流程词的豁免都要求与日期同一小句内,
# 「计划于X日开工，本公司业绩优良」跨逗号后不得再豁免。
_CLAUSE_BREAK_RE = re.compile(r"[。；！？;!?，、\n]")
_TEXT_DATE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}年\s*\d{1,2}月\s*\d{1,2}日")


def _parse_cn_date(s: str):
    """解析「2026年10月2日 / 2026-10-2 / 2026/10/2」→ date;失败返回 None。"""
    m = re.match(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _prose_abs_dates(text: str, json_ranges: list[tuple[int, int]],
                     baseline=None) -> list[tuple[int, str, str]]:
    """散文正文中的绝对日历日期（跳过 ```json 图数据块）。

    正文一律禁止具体日历日期（工期用「总工期 N 日历天 / 开工后第 N 天」相对表达），
    具体日期只允许出现在进度图 JSON 数据块内。返回 [(行号, 日期原文, 行片段)]。
    豁免全部按日期级判断：引用来源类/历史事实类/检测送检流程类取日期前后 12 字符上下文；
    规范生效类仅豁免被结构匹配覆盖的那个日期——不做整行豁免，避免漏报真实工期日期。
    baseline(计划开工基准)传入时,早于它的日期一律视为历史事实豁免(业绩/认证/既往竣工
    等只可能是过去事实),这条比词表更可靠——「2020年8月10日竣工验收」与
    「本工程于2027年10月1日竣工」靠词表无法区分,靠基准日可以。
    """
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(s <= lineno <= e for s, e in json_ranges):
            continue
        std_spans = [m.span() for m in _STANDARD_EFFECT_RE.finditer(line)]
        for m in _TEXT_DATE_RE.finditer(line):
            lo = max(0, m.start() - 12)
            hi = min(len(line), m.end() + 12)
            ctx = line[lo:hi]
            # 引用来源类只扫日期左侧（引用词必在日期前），右侧不豁免，避免漏报真实工期日期。
            left_ctx = line[max(0, m.start() - 12):m.start()]
            # 取“离日期最近的那个可豁免匹配”:只查首个匹配时,
            # 「造价信息发布日期为X」会先命中「造价」而整条不豁免。
            if any(_REF_GLUE_RE.match(left_ctx[_r.end():])
                   for _r in _REFERENCE_CTX_RE.finditer(left_ctx)):
                continue
            if baseline is not None:
                _pd = _parse_cn_date(m.group(0))
                # 只有「远早于基准(>1 年)」才当历史事实:基准前 1 年内的日期仍可能是
                # 本项目写错的计划日期(如「计划于2026-07-01开工」而基准是 2026-07-23),
                # 必须照常报出——否则该错日期两条检查都不报,静默放行。
                if _pd is not None and _pd < baseline - timedelta(days=365):
                    continue
            if _HISTORICAL_CTX_RE.search(ctx):
                # ★ 历史事实豁免也需同句:±12 字窗口跨句时,
                #   「计划于2026年8月1日开工，本公司业绩优良」会被「业绩」放行真实排期日期。
                _hm = _HISTORICAL_CTX_RE.search(ctx)
                _hstart = lo + _hm.start()
                _hend = lo + _hm.end()
                _between = (line[_hend:m.start()] if _hend <= m.start()
                            else line[m.end():_hstart])
                if not _CLAUSE_BREAK_RE.search(_between):
                    continue
            if _PROCESS_CTX_RE.search(ctx):
                # 流程词与日期同句且距离 ±8 字内才豁免(「试块于X日送检」);
                # 隔句/远距离的「送检」不得放行真实排期日期。
                # 句读只在“日期与流程词之间”检查(扫整个短窗会连句末句号一起命中,
                # 把「已于X日送检合格。」这种正常句子误弃)。
                _pl = line[max(0, m.start() - 8):m.start()]
                _pr = line[m.end():min(len(line), m.end() + 8)]
                _ml = _PROCESS_CTX_RE.search(_pl)
                _mr = _PROCESS_CTX_RE.search(_pr)
                if ((_ml and not _CLAUSE_BREAK_RE.search(_pl[_ml.end():]))
                        or (_mr and not _CLAUSE_BREAK_RE.search(_pr[:_mr.start()]))):
                    continue
            if any(s <= m.start() and m.end() <= e for s, e in std_spans):
                continue  # 该日期被「规范…自…起实施」结构覆盖 → 规范生效日期，豁免
            hits.append((lineno, m.group(0), line.strip()[:120]))
    return hits


def check_schedule_duration(
    md_files: list[Path], tender_days: int, log: logging.Logger,
    earliest_start: date | None = None,
) -> dict:
    """工期废标门。评委按双代号网络图算关键线路,activities 最长路径 > 招标工期即超期废标。
    同时查 schedule 横道跨度、散文声明的"总工期 N 天"。"""
    result = {
        "tender_days": tender_days,
        "earliest_start_date": earliest_start.isoformat() if earliest_start else None,
        "violations": [],
        "warnings": [],
        "checked": 0,
    }
    has_tender_days = bool(tender_days and tender_days > 0)
    if not has_tender_days:
        log.info("  工期门:招标工期未知,仅检查绝对开工基准")
    json_re = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="ignore")
        json_ranges: list[tuple[int, int]] = []
        for m in json_re.finditer(text):
            try:
                d = json.loads(m.group(1))
            except (ValueError, json.JSONDecodeError):
                # 解析失败的 ```json 块不豁免：块内日期按正文泄漏处理，
                # 避免同时逃过「正文日期泄漏」与「计划开工基准」两项检查。
                continue
            json_ranges.append((text[:m.start()].count("\n") + 1,
                                text[:m.end()].count("\n") + 1))
            if d.get("type") == "aoa_network":
                cp = _aoa_critical_path(d.get("activities") or [])
                result["checked"] += 1
                if has_tender_days and cp > tender_days:
                    result["violations"].append({
                        "file": md.name, "kind": "网络图关键线路", "value": cp, "limit": tender_days,
                        "detail": f"{md.name} 双代号网络图关键线路累加 {cp} 天 > 招标工期 {tender_days} 天(超期废标)"})
            elif d.get("type") == "schedule":
                span = _schedule_span(d.get("tasks") or [])
                if span:
                    result["checked"] += 1
                    if has_tender_days and span > tender_days:
                        result["violations"].append({
                            "file": md.name, "kind": "进度横道跨度", "value": span, "limit": tender_days,
                            "detail": f"{md.name} 进度横道图跨度 {span} 天 > 招标工期 {tender_days} 天"})
                if earliest_start:
                    for loc, abs_date in _schedule_absolute_dates(d):
                        if abs_date < earliest_start:
                            result["violations"].append({
                                "file": md.name,
                                "kind": "计划开工基准",
                                "value": abs_date.isoformat(),
                                "limit": earliest_start.isoformat(),
                                "detail": (
                                    f"{md.name} 进度图 {loc}={abs_date.isoformat()} "
                                    f"早于投标截止后第5个工作日 {earliest_start.isoformat()}"
                                ),
                            })
        # 正文日期泄漏：散文正文一律禁止具体日历日期（仅进度图 JSON 允许）
        for lineno, date_str, snippet in _prose_abs_dates(text, json_ranges,
                                                        baseline=earliest_start):
            result["violations"].append({
                "file": md.name,
                "kind": "正文日期泄漏",
                "value": date_str,
                "limit": "无（正文禁止具体日期）",
                "detail": (
                    f"{md.name}:{lineno} 正文出现具体日历日期 {date_str}：{snippet} "
                    "—— 工期一律用相对表达（总工期 N 日历天 / 开工后第 N 天 / 第 N 阶段），"
                    "具体日期只允许出现在进度图 JSON 数据块内"
                ),
            })
        for pm in re.finditer(r"(?:总工期|计算工期)[^。\d]{0,6}(\d{2,3})\s*(?:日历)?天", text):
            val = int(pm.group(1))
            if has_tender_days and val > tender_days:
                result["violations"].append({
                    "file": md.name, "kind": "声明总工期", "value": val, "limit": tender_days,
                    "detail": f"{md.name} 声明总工期 {val} 天 > 招标工期 {tender_days} 天"})
        for pm in re.finditer(r"合计\s*(\d{2,3})\s*天", text):
            val = int(pm.group(1))
            if has_tender_days and val > tender_days:
                result["warnings"].append(
                    f"{md.name} 出现'合计{val}天'(>{tender_days}),确认是否为交叉作业压缩前的累加值,"
                    f"建议改述以免评委误读为关键线路超期")
    nv = len(result["violations"])
    if nv:
        log.error(f"  工期门:{nv} 处工期超招标 {tender_days} 天(超期废标风险)")
        for v in result["violations"]:
            log.error(f"    - {v['detail']}")
    else:
        msg = f"  工期门:已检 {result['checked']} 张进度图"
        if has_tender_days:
            msg += f",关键线路/跨度均 ≤ {tender_days} 天"
        if earliest_start:
            msg += f",绝对日期均不早于 {earliest_start.isoformat()}"
        log.info(msg + " ✓")
    for w in result["warnings"]:
        log.warning(f"  工期门(提示): {w}")
    return result


def scan_blind(md_files: list[Path], is_blind: bool, log: logging.Logger) -> list[dict]:
    """扫描章节文件中的暗标禁用信息（薄封装：委托 bid_common + 日志）

    仅在 is_blind=True 时执行；非暗标项目公司名合法，扫描会产生误报。
    """
    if not is_blind:
        log.info("暗标扫描 — 非暗标项目，已跳过")
        return []
    hits = scan_blind_bid_violations(md_files)
    if hits:
        log.warning(f"暗标扫描命中 {len(hits)} 处")
    else:
        log.info("暗标扫描 ✓ 无禁用信息")
    return hits


def check_response(rules_text: str, all_md_text: str, log: logging.Logger) -> list[dict]:
    """检查关键响应词是否在章节中出现"""
    results = []
    # 结构化真值(schedule_milestones.total_duration_days)优先于正则:
    # 规则文件里可能同时存在「投标有效期 90 日历天」等多个 N 日历天,只有该字段是工期。
    m_dur = re.search(r"^\s*total_duration_days\s*:\s*(\d{2,4})\s*$", rules_text, re.M)
    duration_kw = f"{m_dur.group(1)} 日历天" if m_dur else ""
    for label, pat, required in RESPONSE_RULES:
        m_rule = re.search(pat, rules_text)
        if label == "工期响应" and duration_kw:
            keyword = duration_kw
        elif m_rule:
            # 正则只捕获数字,必须补回单位——否则正文出现任意同号数字即判「已响应」(假阴性)
            keyword = (f"{m_rule.group(1)} 日历天" if label == "工期响应"
                       else m_rule.group(1))
        else:
            continue
        # 比对去空白:正文写「450日历天」与规则「450 日历天」必须等价,
        # 否则合规门反复误判未响应并触发无意义的章节重写(与 reviewer 的去空白口径一致)。
        present = (keyword in all_md_text
                   or re.sub(r"\s+", "", keyword) in re.sub(r"\s+", "", all_md_text))
        results.append({
            "label": label, "keyword": keyword,
            "required": required, "present": present,
        })
        status = "✓" if present else ("✗" if required else "⚠")
        log.info(f"  [{status}] {label}: 规则要求 '{keyword}' — {'已响应' if present else '未出现'}")
    return results


def count_words(md_files: list[Path]) -> dict[str, int]:
    """统计各章节汉字字数（委托 bid_common.count_chinese_chars，与 reviewer 同口径）"""
    stats = {}
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        stats[md.name] = count_chinese_chars(text)
    return stats


def parse_must_include_sections(rules_text: str) -> dict:
    """从 disqualification_rules.md 解析 must_include_sections yaml 块（regex 实现，零依赖）。

    返回 {"source": str, "enforcement": str, "sections": [str, ...], "required_attachments": [str, ...]}.
    若字段不存在或解析不到返回各字段默认值（空字符串/空数组）。
    """
    result = {"source": "", "enforcement": "", "sections": [], "required_attachments": []}

    # 找到 must_include_sections: 起点（行首允许有空白，兼容嵌入 yaml fence）
    m_start = re.search(r"^\s*must_include_sections\s*:\s*$", rules_text, re.M)
    if not m_start:
        return result
    start_pos = m_start.end()

    # 截取后续缩进块（缩进 >= 2 空格的连续行；遇到顶级 key 或 fence 结束）
    tail = rules_text[start_pos:]
    block_lines: list[str] = []
    for line in tail.splitlines():
        if not line.strip():
            block_lines.append(line)
            continue
        if line.startswith("```"):
            break
        # 顶级 key（无前导空白且含冒号）→ 块结束
        if re.match(r"^[A-Za-z_]\w*\s*:", line):
            break
        block_lines.append(line)
    block = "\n".join(block_lines)

    # 标量字段
    m_src = re.search(r'^\s*source\s*:\s*"?(.*?)"?\s*$', block, re.M)
    if m_src:
        result["source"] = m_src.group(1).strip()
    m_enf = re.search(r'^\s*enforcement\s*:\s*"?(.*?)"?\s*$', block, re.M)
    if m_enf:
        result["enforcement"] = m_enf.group(1).strip()

    # 列表字段：sections / required_attachments
    def collect_list(key: str) -> list[str]:
        items: list[str] = []
        m_k = re.search(rf"^\s*{key}\s*:\s*$", block, re.M)
        if not m_k:
            # 也兼容 inline 写法 sections: []
            m_inline = re.search(rf"^\s*{key}\s*:\s*\[(.*?)\]\s*$", block, re.M)
            if m_inline:
                raw = m_inline.group(1).strip()
                if raw:
                    items = [s.strip().strip('"').strip("'") for s in raw.split(",") if s.strip()]
            return items
        sub_start = m_k.end()
        for line in block[sub_start:].splitlines():
            if not line.strip():
                continue
            m_item = re.match(r"^\s+-\s+(.+?)\s*$", line)
            if m_item:
                items.append(m_item.group(1).strip().strip('"').strip("'"))
                continue
            # 同级或更外层的 key → 列表结束
            if re.match(r"^\s*[A-Za-z_]\w*\s*:", line):
                break
        return items

    result["sections"] = collect_list("sections")
    result["required_attachments"] = collect_list("required_attachments")
    return result


def check_chart_coverage(
    extracted: Path, chapter_dir: Path, md_files: list[Path],
    log: logging.Logger,
) -> dict:
    """检查 chart_specs.json 中 required=true 的图是否在章节中落地。

    每个 spec 命中规则(任一即视为有图):
      1. 章节 .md 含 ```json``` 块且 type 字段命中 spec.type(或 classify_json 推回该 type)
      2. 章节 .md 含 ![](../charts/...png) 引用
      3. charts/ 目录有 `<chapter>_<...>_<type>.png` 文件

    返回 {
      "loaded": bool, "total_required": int, "satisfied": int,
      "missing": list[dict], "advisory": list[dict]
    }
    """
    out = {
        "loaded": False,
        "total_required": 0,
        "satisfied": 0,
        "missing": [],
        "advisory": [],
    }
    spec_path = extracted / "chart_specs.json"
    if not spec_path.exists():
        log.info("chart_specs.json 不存在 — 跳过图表覆盖检查(向后兼容)")
        return out
    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"chart_specs.json 解析失败: {e}")
        return out

    out["loaded"] = True
    specs = payload.get("specs", [])
    if not specs:
        log.info("chart_specs.json 为空 — 跳过图表覆盖检查")
        return out

    # 索引 md_files 按 chapter num
    md_by_chapter: dict[str, Path] = {}
    for md in md_files:
        m = re.match(r"^(\d{1,3})[_-]", md.name)
        if m:
            md_by_chapter[m.group(1).zfill(2)] = md

    charts_dir = chapter_dir / "charts"
    chart_pngs = list(charts_dir.glob("*.png")) if charts_dir.exists() else []

    # 延迟导入分类器
    classify = None
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from bid_charts import classify_json
        classify = classify_json
    except ImportError:
        pass

    fence_re = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)

    for spec in specs:
        ch = str(spec.get("chapter", "")).zfill(2)
        ctype = spec.get("type", "")
        required = bool(spec.get("required", False))
        section = spec.get("section", "")
        hint = spec.get("data_hint", "")

        present = False
        evidence = ""

        # 1. 检查 md 中的 ```json``` 块
        md = md_by_chapter.get(ch)
        if md and md.exists():
            text = md.read_text(encoding="utf-8")
            for blk in fence_re.findall(text):
                try:
                    data = json.loads(blk)
                except Exception:
                    continue
                if isinstance(data, dict):
                    declared = str(data.get("type", "")).lower()
                    if ctype in declared or (classify and classify(data) == ctype):
                        present = True
                        evidence = f"md JSON 块 type={declared or ctype}"
                        break

            # 2. 检查图片引用
            if not present and re.search(rf"!\[[^\]]*\]\([^)]*charts/[^)]*\.png", text):
                present = True
                evidence = "md 含 charts/ PNG 引用"

        # 3. 检查 charts/ 目录有匹配 PNG
        if not present:
            for png in chart_pngs:
                if png.stem.startswith(f"{ch}_") and ctype in png.stem:
                    present = True
                    evidence = f"PNG: {png.name}"
                    break

        # 4. required 图全变体兜底:chart_specs 用基准章号,变体章节数≠基准时
        #    指定章号可能不存在,招标硬性图只要全文任一章命中即视为已落地。
        if not present and required:
            for any_md in md_files:
                try:
                    atext = any_md.read_text(encoding="utf-8")
                except Exception:
                    continue
                for blk in fence_re.findall(atext):
                    try:
                        data = json.loads(blk)
                    except Exception:
                        continue
                    if isinstance(data, dict):
                        declared = str(data.get("type", "")).lower()
                        if ctype in declared or (classify and classify(data) == ctype):
                            present = True
                            evidence = f"全文扫描命中 {any_md.name} type={declared or ctype}"
                            break
                if present:
                    break

        item = {
            "chapter": ch,
            "section": section,
            "type": ctype,
            "required": required,
            "data_hint": hint,
            "present": present,
            "evidence": evidence,
        }
        if required:
            out["total_required"] += 1
            if present:
                out["satisfied"] += 1
                log.info(f"  ✓ 图 [{ch}.{section} {ctype}] 已落地: {evidence}")
            else:
                out["missing"].append(item)
                log.warning(f"  ✗ 图 [{ch}.{section} {ctype}] ★招标硬性要求 — 未在章节中找到")
        else:
            out["advisory"].append(item)

    log.info(f"图表覆盖: {out['satisfied']}/{out['total_required']} 张 required 图已落地;"
             f" {sum(1 for a in out['advisory'] if a['present'])}/{len(out['advisory'])} 张 advisory 已落地")
    return out


def check_outline_coverage(
    must: dict, md_files: list[Path], log: logging.Logger,
) -> dict:
    """检查 output 章节文件是否覆盖 must_include_sections.sections。

    返回 {"sections": [...], "covered": [...], "missing": [...], "attachments_referenced": {...}}.
    匹配口径：章节文件的一级标题 (#) + 二级标题 (##)，与 sections 做 topic_covered 语义匹配。
    """
    sections = must.get("sections", [])
    attachments = must.get("required_attachments", [])
    out = {
        "sections": sections,
        "covered": [],
        "missing": [],
        "attachments_referenced": {},
    }
    if not sections and not attachments:
        log.info("must_include_sections 为空，跳过章节锚点核查")
        return out

    # 收集所有章节标题（一级 + 二级）
    titles: list[str] = []
    full_text = ""
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        full_text += "\n" + text
        for m in re.finditer(r"^(#{1,2})\s+(.+?)\s*$", text, re.M):
            titles.append(m.group(2).strip())

    log.info(f"章节锚点核查 — 大纲主题 {len(sections)} 项，附表 {len(attachments)} 项")
    for topic in sections:
        if topic_covered(topic, titles):
            out["covered"].append(topic)
        else:
            out["missing"].append(topic)
    if out["missing"]:
        log.warning(f"  缺失大纲主题 {len(out['missing'])} 项: {out['missing']}")
    else:
        log.info("  ✓ 大纲主题全部命中")

    # 附表只做软核查（出现于正文任意位置即算命中）
    # 正文同样按 CJK 过滤后比对,避免附表名含顿号/标点时(如"计划开、竣工日期")永远搜不到
    cjk_full_text = "".join(c for c in full_text if "一" <= c <= "鿿")
    for att in attachments:
        # 取关键词前 4 个汉字做包含搜索
        key_chars = "".join(c for c in att if "一" <= c <= "鿿")[:4]
        if key_chars and (key_chars in full_text or key_chars in cjk_full_text):
            out["attachments_referenced"][att] = True
        else:
            out["attachments_referenced"][att] = False
            log.warning(f"  附表未在章节中引用: {att}")
    return out


def check_plan_alignment(
    plan_rows: list[dict], md_files: list[Path],
    word_stats: dict[str, int], log: logging.Logger,
) -> list[dict]:
    """核查实际章节文件与 chapter_plan 的对齐情况。

    逐项检查：文件是否存在、一级标题是否与 plan 标题一致、字数是否达标。
    """
    md_map = {md.name: md for md in md_files}
    results = []
    for row in plan_rows:
        fname = row["filename"]
        md = md_map.get(fname)
        item = {
            "num": row["num"],
            "expected_filename": fname,
            "expected_title": row["title"],
            "score": row["score"],
            "target_chars": row["target_chars"],
            "file_exists": md is not None,
            "title_match": False,
            "actual_title": "",
            "actual_chars": word_stats.get(fname, 0),
            "chars_meet_target": False,
        }
        if md is not None:
            text = md.read_text(encoding="utf-8")
            m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
            if m:
                item["actual_title"] = m.group(1).strip()
                # 字面一致判断：去掉空白和展示星标后比较
                norm_a = _normalize_heading_title(item["actual_title"])
                norm_b = _normalize_heading_title(row["title"])
                item["title_match"] = (norm_a == norm_b)
            # 字数下限：共享 bid_common.chapter_chars_floor（圈层理论 ±15%）
            threshold = chapter_chars_floor(row["target_chars"])
            item["chars_threshold"] = threshold
            item["chars_meet_target"] = (
                item["actual_chars"] >= threshold if row["target_chars"] else True
            )
        results.append(item)

        if not item["file_exists"]:
            log.error(f"  ✗ 章节 {row['num']} 缺失：{fname}")
        elif not item["title_match"]:
            log.warning(
                f"  ⚠ 章节 {row['num']} 标题不一致：期望「{row['title']}」实际「{item['actual_title']}」"
            )
        elif not item["chars_meet_target"]:
            log.warning(
                f"  ⚠ 章节 {row['num']} 字数不足：{item['actual_chars']} < {item['chars_threshold']}"
                f"（目标 {row['target_chars']}，下限 85%）"
            )
        else:
            log.info(f"  ✓ 章节 {row['num']} 对齐 ({item['actual_chars']}字)")
    # 多余章节（不在 plan 中）
    planned = {r["filename"] for r in plan_rows}
    extras = [md.name for md in md_files if md.name not in planned]
    if extras:
        log.warning(f"发现计划外章节文件: {extras}")
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="投标文件合规扫描")
    parser.add_argument("tender_dir", help="投标根目录")
    parser.add_argument("--variant", help="仅扫描指定 variant 子目录（如 V01）")
    parser.add_argument("--all-variants", action="store_true",
                        help="批量扫描 output/V*/ 所有 variant")
    args = parser.parse_args()

    root = Path(args.tender_dir).resolve()
    if not root.exists():
        print(f"目录不存在: {root}")
        sys.exit(1)

    extracted = root / "extracted"
    output = root / "output"
    if not output.exists():
        print(f"output 目录不存在: {output}")
        sys.exit(1)

    # ── 批量模式：对所有 variant 独立扫描 ──
    if args.all_variants:
        variant_dirs = sorted([d for d in output.iterdir()
                                if d.is_dir() and re.match(r"^V\d+$", d.name)])
        if not variant_dirs:
            print("未发现 V01/ V02/ ... 子目录")
            sys.exit(2)
        failed = []
        for v_dir in variant_dirs:
            print(f"\n━━━ 合规扫描 {v_dir.name} ━━━")
            code = _run_single(root, v_dir, v_dir.name)
            if code != 0:
                failed.append(v_dir.name)
        print(f"\n汇总：{len(variant_dirs) - len(failed)}/{len(variant_dirs)} 通过")
        sys.exit(0 if not failed else 1)

    # ── 单 variant 模式 ──
    if args.variant:
        chapter_dir = output / args.variant
        if not chapter_dir.is_dir():
            print(f"variant 目录不存在: {chapter_dir}")
            sys.exit(2)
        sys.exit(_run_single(root, chapter_dir, args.variant))

    # ── 默认：扫描 output/ 根目录（单份模式）──
    sys.exit(_run_single(root, output, ""))


def _run_single(root: Path, chapter_dir: Path, variant_id: str) -> int:
    """对某个章节目录做合规扫描。chapter_dir 是章节 md 所在位置。"""
    extracted = root / "extracted"
    # 报告固定写到 chapter_dir（单份=output/、批量=output/VXX/）
    output = chapter_dir

    (output / "logs").mkdir(exist_ok=True)
    (output / "reports").mkdir(exist_ok=True)
    log = make_script_logger("bid_compliance", output / "logs" / "compliance.log")
    log.info("=" * 60)
    log.info(f"bid_compliance 启动  章节目录: {chapter_dir}  variant={variant_id or '单份'}")
    log.info("=" * 60)

    rules_path = extracted / "disqualification_rules.md"
    rules_text = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
    if not rules_text:
        log.warning("disqualification_rules.md 不存在或为空，响应词检查将跳过")

    md_files = collect_chapter_files(chapter_dir)
    if not md_files:
        log.error("未找到章节 .md 文件")
        return 1
    log.info(f"扫描章节文件 {len(md_files)} 个")

    all_text = "\n".join(md.read_text(encoding="utf-8") for md in md_files)

    is_blind = needs_blind_scan(extracted)
    log.info(f"暗标判定: is_blind={is_blind}  (来源：bid_common.needs_blind_scan)")
    blind_hits = scan_blind(md_files, is_blind, log)
    log.info("关键响应词检查:")
    responses = check_response(rules_text, all_text, log)
    word_stats = count_words(md_files)

    log.info("章节字数统计:")
    for fname, cn in word_stats.items():
        log.info(f"  {fname}: {cn:,} 汉字")
    log.info(f"  合计: {sum(word_stats.values()):,} 汉字")

    log.info("章节-评分表对齐核查:")
    plan_path = extracted / "chapter_plan.md"
    v_plan = chapter_dir / "chapter_plan_variant.md"
    if variant_id and v_plan.exists() and parse_chapter_plan(v_plan):
        plan_path = v_plan
        log.info(f"批量模式:改用变体计划 {v_plan.name}(结构独立)")
    plan_rows = parse_chapter_plan(plan_path)
    if not plan_rows and not plan_path.exists():
        log.warning(f"chapter_plan.md 不存在: {plan_path}")
    else:
        log.info(f"chapter_plan 解析到 {len(plan_rows)} 章")
    alignment = check_plan_alignment(plan_rows, md_files, word_stats, log) if plan_rows else []
    planned_names = {r["filename"] for r in plan_rows}
    extras = [md.name for md in md_files if md.name not in planned_names] if plan_rows else []

    log.info("章节锚点-施组大纲覆盖核查:")
    must = parse_must_include_sections(rules_text)
    outline_coverage = check_outline_coverage(must, md_files, log)

    log.info("图表覆盖核查(chart_specs.json):")
    chart_coverage = check_chart_coverage(extracted, chapter_dir, md_files, log)

    log.info("DOCX 格式实测层审计:")
    format_audit_result = _run_format_audit(root, output, log)

    log.info("地区暗标专项检测:")
    blind_profile_result = _run_blind_profile_checks(root, chapter_dir, log)

    log.info("标准引用编造检测:")
    config_dir = get_config_dir()
    truth = load_standards_truth(config_dir)
    std_result = check_standards_fabrication(md_files, truth, log)

    log.info("工期废标门(网络图关键线路/进度跨度 ≤ 招标工期):")
    tender_days = 0
    facts_p = extracted / "project_facts.json"
    if facts_p.exists():
        try:
            _fj = json.loads(facts_p.read_text(encoding="utf-8"))
            _f = _fj.get("facts", _fj)
            _dd = _f.get("duration_days")
            _dd = _dd.get("value") if isinstance(_dd, dict) else _dd
            tender_days = int(float(_dd or 0))
        except (ValueError, json.JSONDecodeError, TypeError, AttributeError):
            tender_days = 0
    if not tender_days:
        m = re.search(r"total_duration_days:\s*(\d+)", rules_text)
        if m:
            tender_days = int(m.group(1))
    earliest_start = None
    derived_p = extracted / "project_derived.json"
    if derived_p.exists():
        try:
            _dj = json.loads(derived_p.read_text(encoding="utf-8"))
            _sc = _dj.get("schedule_constraints") or {}
            earliest_start = _parse_abs_date(_sc.get("earliest_start_date") or _dj.get("schedule_start_date"))
        except (json.JSONDecodeError, OSError, TypeError):
            earliest_start = None
    schedule_check = check_schedule_duration(md_files, tender_days, log, earliest_start=earliest_start)

    # 生成机读报告（JSON，供 Claude 读取）+ 人读报告（MD）
    reports_dir = output / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_json = reports_dir / "compliance_scan.json"
    report_json.write_text(json.dumps({
        "is_blind": is_blind,
        "blind_violations": blind_hits,
        "response_check": responses,
        "word_stats": word_stats,
        "total_cn_chars": sum(word_stats.values()),
        "plan_alignment": alignment,
        "extra_files": extras,
        "plan_loaded": bool(plan_rows),
        "outline_coverage": outline_coverage,
        "must_include_sections": must,
        "chart_coverage": chart_coverage,
        "format_audit": format_audit_result,
        "blind_profile_check": blind_profile_result,
        "standards_check": std_result,
        "schedule_check": schedule_check,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"机读报告: {report_json}")

    md_lines = [
        "# 自动合规扫描结果",
        "",
        "> 本文件由 `bid_compliance.py` 自动生成，供 Claude 做废标合规检查时参考。",
        "",
        f"**项目类型**：{'暗标' if is_blind else '非暗标'}（判定来源：bid_common.needs_blind_scan）",
        "",
        "## 1. 暗标禁用信息扫描",
        "",
    ]
    if not is_blind:
        md_lines.append("⏭ 非暗标项目，已跳过（公司名/签章等信息合法，扫描不适用）")
    elif blind_hits:
        md_lines.append(f"❌ 命中 {len(blind_hits)} 处，请逐条核查：")
        md_lines.append("")
        md_lines.append("| 文件 | 行 | 类别 | 命中 | 上下文 |")
        md_lines.append("|------|----|------|------|--------|")
        for h in blind_hits:
            md_lines.append(f"| {h['file']} | {h['line']} | {h['kind']} | `{h['match']}` | …{h['context']}… |")
    else:
        md_lines.append("✓ 未发现禁用信息")
    md_lines.append("")

    md_lines.append("## 2. 关键响应词检查")
    md_lines.append("")
    md_lines.append("| 项 | 规则关键词 | 是否响应 | 强制 |")
    md_lines.append("|----|-----------|---------|------|")
    for r in responses:
        md_lines.append(f"| {r['label']} | `{r['keyword']}` | {'✓' if r['present'] else '✗'} | {'是' if r['required'] else '否'} |")
    md_lines.append("")

    md_lines.append("## 3. 章节字数")
    md_lines.append("")
    md_lines.append("| 章节 | 汉字数 |")
    md_lines.append("|------|--------|")
    for fname, cn in word_stats.items():
        md_lines.append(f"| {fname} | {cn:,} |")
    md_lines.append(f"| **合计** | **{sum(word_stats.values()):,}** |")
    md_lines.append("")

    md_lines.append("## 4. 章节-评分表对齐核查")
    md_lines.append("")
    if not plan_rows:
        md_lines.append("⚠ 未找到 `extracted/chapter_plan.md`，跳过对齐核查。请在 Step 2 生成该文件。")
    else:
        md_lines.append("| 章 | 期望文件 | 期望标题 | 分值 | 实际标题 | 文件存在 | 标题一致 | 实际字数/目标 | 达标 |")
        md_lines.append("|----|---------|---------|------|---------|---------|---------|---------------|------|")
        for a in alignment:
            md_lines.append(
                f"| {a['num']} | {a['expected_filename']} | {a['expected_title']} | {a['score']} | "
                f"{a['actual_title'] or '—'} | {'✓' if a['file_exists'] else '✗'} | "
                f"{'✓' if a['title_match'] else '✗'} | "
                f"{a['actual_chars']}/{a['target_chars']} | "
                f"{'✓' if a['chars_meet_target'] else '✗'} |"
            )
        if extras:
            md_lines.append("")
            md_lines.append(f"⚠ 计划外的章节文件：{', '.join(extras)}")
    md_lines.append("")

    md_lines.append("## 5. 章节锚点-施组编制大纲覆盖核查")
    md_lines.append("")
    if not must.get("sections") and not must.get("required_attachments"):
        md_lines.append("⏭ `disqualification_rules.md` 未声明 `must_include_sections.sections`，跳过此核查。")
        md_lines.append("（合格制项目 / 招标文件含施组大纲时建议在 brain.md 第七节同步该字段。）")
    else:
        if must.get("source"):
            md_lines.append(f"**来源**：{must['source']}")
            md_lines.append("")
        if must.get("sections"):
            covered = outline_coverage["covered"]
            missing = outline_coverage["missing"]
            md_lines.append(f"大纲一级主题 {len(must['sections'])} 项，已覆盖 {len(covered)}，缺失 {len(missing)}")
            md_lines.append("")
            md_lines.append("| 主题 | 状态 |")
            md_lines.append("|------|------|")
            for topic in must["sections"]:
                ok = topic in covered
                md_lines.append(f"| {topic} | {'✓ 已覆盖' if ok else '✗ 缺失'} |")
            md_lines.append("")
        if must.get("required_attachments"):
            md_lines.append("### 附图附表引用核查（关键词搜索口径，仅供参考）")
            md_lines.append("")
            md_lines.append("| 附表名 | 是否在章节正文中提及 |")
            md_lines.append("|--------|-------------------|")
            for att, hit in outline_coverage["attachments_referenced"].items():
                md_lines.append(f"| {att} | {'✓' if hit else '✗'} |")
    md_lines.append("")

    md_lines.append("## 6. 地区暗标专项检测")
    md_lines.append("")
    if not blind_profile_result.get("active"):
        md_lines.append("⏭ 未激活地区暗标 profile（非暗标项目或地区未命中），跳过。")
    else:
        md_lines.append(f"**Profile**：{blind_profile_result.get('profile')}")
        if blind_profile_result.get("penalty_note"):
            md_lines.append("")
            md_lines.append(f"**违规后果**：{blind_profile_result['penalty_note']}")
        md_lines.append("")
        bp_violations = blind_profile_result.get("violations", [])
        if bp_violations:
            md_lines.append(f"❌ 命中 {len(bp_violations)} 处违规：")
            md_lines.append("")
            md_lines.append("| 规则 | 位置 | 详情 |")
            md_lines.append("|------|------|------|")
            for v in bp_violations:
                md_lines.append(f"| {v['rule']} | {v['location']} | {v['detail']} |")
        else:
            md_lines.append("✓ 专项检测通过（加粗/彩色文字/页码/彩色图/白页倒页均未检出）")
        bp_warnings = blind_profile_result.get("warnings", [])
        if bp_warnings:
            md_lines.append("")
            md_lines.append("⚠ 提示（不影响退出码）：")
            for w in bp_warnings:
                md_lines.append(f"- {w}")
    md_lines.append("")

    report_md = reports_dir / "compliance_scan.md"
    report_md.write_text("\n".join(md_lines), encoding="utf-8")
    log.info(f"人读报告: {report_md}")

    # 退出码：暗标违规 / 强制响应未命中 / 章节缺失或标题不一致 / 格式硬违规 → 1
    has_align_violation = any(
        (not a["file_exists"]) or (not a["title_match"])
        for a in alignment
    )
    has_chart_violation = chart_coverage.get("loaded") and bool(chart_coverage.get("missing"))
    has_format_severe = format_audit_result.get("exit_code", 0) >= 2
    has_std_violation = std_result.get("fabricated", 0) > 0
    has_schedule_violation = bool(schedule_check.get("violations"))
    # 地区暗标专项违规与 blind_violation 同级 → exit 1；其 warnings（含 200 页 advisory）永不影响
    has_blind_profile_violation = bool(blind_profile_result.get("violations"))
    has_violation = (
        bool(blind_hits)
        or any(r["required"] and not r["present"] for r in responses)
        or has_align_violation
        or has_chart_violation
        or has_format_severe
        or has_std_violation
        or has_schedule_violation
        or has_blind_profile_violation
    )
    log.info("=" * 60)
    if has_violation:
        if has_chart_violation:
            n_miss = len(chart_coverage["missing"])
            log.error(f"招标硬性要求的 {n_miss} 张图未落地:")
            for m in chart_coverage["missing"]:
                log.error(f"    - 章 {m['chapter']}.{m['section']} type={m['type']} ({m['data_hint']})")
        if has_format_severe:
            n_v = len(format_audit_result.get("violations", []))
            n_sv = len(format_audit_result.get("style_violations", []))
            log.error(f"DOCX 格式实测层硬违规：{n_v} 段落字号/字体偏离 + {n_sv} 样式偏离")
        if has_schedule_violation:
            log.error("工期/正文日期硬违规："
                      f"{len(schedule_check['violations'])} 处（超期废标 或 正文出现日历日期）")
            for v in schedule_check["violations"]:
                log.error(f"    - {v['detail']}")
        if has_blind_profile_violation:
            log.error(f"地区暗标专项（{blind_profile_result.get('profile')}）违规 "
                      f"{len(blind_profile_result['violations'])} 处"
                      f"（{blind_profile_result.get('penalty_note', '')}）")
        log.error("存在合规风险，退出码 1")
        sys.exit(1)
    log.info("合规扫描通过 ✓")
    return 0


def _run_format_audit(root, chapter_dir, log):
    """调用 bid_format_audit 实测 DOCX；缺 DOCX 时返回 skipped。"""
    # 单份模式 chapter_dir=output/(docx 同目录);批量模式 chapter_dir=output/V01/(docx 在父级)
    candidates = sorted(chapter_dir.glob("*_施工组织设计.docx"))
    if not candidates and chapter_dir.name.startswith("V"):
        # 批量模式:docx 在 output/ 顶层,文件名含 variant id
        candidates = sorted(chapter_dir.parent.glob(f"{chapter_dir.name}_*_施工组织设计.docx"))
    if not candidates:
        log.info("  跳过：未找到 *_施工组织设计.docx（先跑 bid_assemble）")
        return {"skipped": True, "reason": "no docx"}
    try:
        import bid_format_audit
        code, result = bid_format_audit.audit(root, candidates[0])
        v = len(result.get("violations", []))
        sv = len(result.get("style_violations", []))
        log.info(f"  实测违规 {v} 段落 + {sv} 样式  退出码 {code}")
        if v or sv:
            for vio in result.get("violations", [])[:5]:
                log.warning(f"  - [{vio.get('region','?')}] 段{vio['para_idx']} 「{vio['snippet']}」"
                            f" 实际 {vio.get('actual')} 期望 {vio.get('expected')}")
        return result
    except Exception as e:
        log.warning(f"  格式审计调用失败：{e}")
        return {"error": str(e), "skipped": True}


def _find_docx_candidates(chapter_dir: Path) -> list[Path]:
    """定位组装 DOCX（与 _run_format_audit 同口径 + 通用 *.docx 兜底）。"""
    candidates = sorted(chapter_dir.glob("*_施工组织设计.docx"))
    if not candidates and chapter_dir.name.startswith("V"):
        candidates = sorted(chapter_dir.parent.glob(f"{chapter_dir.name}_*_施工组织设计.docx"))
    if not candidates:
        candidates = sorted(p for p in chapter_dir.glob("*.docx")
                            if not p.name.startswith("~$"))
    return candidates


def _run_blind_profile_checks(root: Path, chapter_dir: Path, log) -> dict:
    """地区暗标专项检测（如重庆技术暗标）。profile 未激活 → {"skipped": True}。

    违规（加粗/彩色文字/页码/彩色图/白页/倒页）记入 violations → 与 blind_violation
    同级影响退出码；200 页 advisory 与检测跳过只记 warnings，永不影响退出码。
    """
    extracted = root / "extracted"
    try:
        from bid_blind_profile import (
            detect_blind_profile, load_profile,
            check_bold_runs, check_color_runs, check_page_numbers,
            check_color_images, check_blank_and_rotated_pages,
            check_advisory_page_limit, pymupdf_available,
        )
    except ImportError as e:
        log.warning(f"  bid_blind_profile 不可用：{e}")
        return {"skipped": True, "reason": f"import error: {e}"}

    name = detect_blind_profile(extracted)
    if not name:
        log.info("  未激活地区暗标 profile（非暗标或地区未命中）— 跳过")
        return {"skipped": True, "active": False}
    profile = load_profile(name) or {}
    result = {
        "active": True,
        "profile": name,
        "penalty_note": profile.get("penalty_note", ""),
        "violations": [],
        "warnings": [],
    }
    log.info(f"  地区暗标 profile 激活：{name}")
    if profile.get("penalty_note"):
        log.info(f"  违规后果：{profile['penalty_note']}")

    docxs = _find_docx_candidates(chapter_dir)
    if not docxs:
        msg = "未找到组装 DOCX，DOCX 级检测跳过（先跑 bid_assemble）"
        log.warning(f"  {msg}")
        result["warnings"].append(msg)
        # 无 docx 时兜底直扫 charts 目录 PNG
        charts_dir = chapter_dir / "charts"
        if charts_dir.is_dir():
            result["violations"] += check_color_images(charts_dir, log=log)
        return result

    for dx in docxs:
        for fn in (check_bold_runs, check_color_runs, check_page_numbers, check_color_images):
            try:
                hits = fn(dx, log=log)
                result["violations"] += hits
                log.info(f"  {fn.__name__}({dx.name}): {len(hits)} 处违规"
                         + (" ✓" if not hits else ""))
            except Exception as e:  # noqa: BLE001  degrade-safe：单 check 失败不 fail
                log.warning(f"  {fn.__name__}({dx.name}) 执行失败，跳过：{e}")
                result["warnings"].append(f"{fn.__name__} 执行失败：{e}")

        # 白页/倒页 + 页数 advisory：PDF 来源 = docx 同名 .pdf（调用方负责导出）
        pdf = dx.with_suffix(".pdf")
        if pdf.exists():
            result["violations"] += check_blank_and_rotated_pages(pdf, log=log)
            if pymupdf_available():
                try:
                    import fitz
                    with fitz.open(str(pdf)) as _d:
                        result["warnings"] += [
                            h["detail"] for h in
                            check_advisory_page_limit(_d.page_count, profile)
                        ]
                except Exception as e:  # noqa: BLE001
                    result["warnings"].append(f"页数 advisory 读取失败：{e}")
        else:
            msg = (f"未找到 {pdf.name}，白页/倒页与页数 advisory 跳过"
                   f"（可用 Word AppleScript 导出 PDF 后重跑）")
            log.warning(f"  {msg}")
            result["warnings"].append(msg)

    n_v = len(result["violations"])
    if n_v:
        log.error(f"  地区暗标专项（{name}）：{n_v} 处违规（{profile.get('penalty_note', '')}）")
        for v in result["violations"][:10]:
            log.error(f"    - [{v['rule']}] {v['location']}：{v['detail']}")
    else:
        log.info(f"  地区暗标专项（{name}）：未发现违规 ✓")
    return result


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
bid_regroup.py - 合格制批量:目录"指纹族"的第三层 —— 二级标题数量/层级差异化

为什么需要(memory/feedback_bid_page_budget_and_fingerprint.md):
    三家投标文件如果连"二级标题数量"都一一对应(15/5/5/4…),是最容易被判
    "投标文件异常一致"(围标/串标)的结构指纹。措辞不同、数量相同照样危险。

做法(大纲条目一条不丢 = 合格制覆盖底线):
    V01 平铺     : 每个大纲条目 = 一个二级节 (##)
    V02 每2条并1节: 条目降为三级节 (###), 组标题由 LLM 拟
    V03 每3条并1节: 同上
    编号体系同步轮换: 1.1 / 一、+（1） / （一）+1.

落点: bid_run.py STEPS 的 coherence_pass 之后、assemble 之前。
    写手侧不需要改(仍是"N 个二级子项"平铺生成),本步在组装前重排标题层级。

用法:
    python3 bid_regroup.py <tender_dir> [--variant V02] [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger  # noqa: E402

CN = "一二三四五六七八九十"
# 内层编号:必须带分隔符(、/./空格),否则「3号桥台施工」「5S现场管理」会被误剥
_NUM_PREFIX_RE = re.compile(
    r"^(?:第[一二三四五六七八九十]+[节部分章]|\d+(?:\.\d+)+|\d+[、.．]\s*|\d+\s+"
    r"|[一二三四五六七八九十]+[、.．]\s*|（[一二三四五六七八九十]+）)\s*")
# 单变体的分组粒度(按 variants.json 顺序;超出后 2/3 轮转)
GROUP_SIZES = [1, 2, 3]
GROUP_SIZE_CYCLE = [2, 3]


def cn_num(i: int) -> str:
    if i <= 10:
        return CN[i - 1]
    if i < 20:
        return "十" + CN[i - 11]
    return str(i)


def strip_num(t: str) -> str:
    t = (t or "").strip()
    for _ in range(3):
        n = _NUM_PREFIX_RE.sub("", t).strip()
        if n == t:
            break
        t = n or t
    return t


def group_size_for(idx: int) -> int:
    if idx < len(GROUP_SIZES):
        return GROUP_SIZES[idx]
    return GROUP_SIZE_CYCLE[(idx - len(GROUP_SIZES)) % len(GROUP_SIZE_CYCLE)]


def head_group(idx: int, i: int, ch_num: str) -> str:
    style = idx % 3
    if style == 0:
        return f"{int(ch_num)}.{i} "
    return f"{cn_num(i)}、" if style == 1 else f"（{cn_num(i)}）"


def head_item(idx: int, i: int, gi: int = 0, ch_num: str = "") -> str:
    """三级(条目)编号;风格与组号配对:7.1→7.1.1 / 一、→（一） / （一）→1.。"""
    style = idx % 3
    if style == 0:
        return f"{int(ch_num)}.{gi}.{i} " if gi and ch_num else ""
    return f"（{i}）" if style == 1 else f"{i}. "


def parse_chapter_lines(plan_text: str) -> dict[str, list[tuple[str, int]]]:
    """从变体计划解析 {章号: [(标题, 字数)]}。"""
    out: dict[str, list[tuple[str, int]]] = {}
    num = None
    for ln in plan_text.split("\n"):
        h = re.match(r"^###\s+(\d{2})\s", ln)
        if h:
            num = h.group(1)
        m = re.match(r"^-\s+(.+?):\s*([\d,]+)\s*字", ln)
        if m and num:
            out.setdefault(num, []).append((strip_num(m.group(1)), int(m.group(2).replace(",", ""))))
    return out


def chunk(items: list, size: int) -> list[list[int]]:
    """确定性顺序分组,返回每组的 1-based 下标。**无重叠、无遗漏**。

    历史 bug: end 写成 min(i+size, n)+1(多带一条)→ 组间重叠、条目重复。
    """
    return [list(range(i, min(i + size - 1, len(items)) + 1))
            for i in range(1, len(items) + 1, size)]


def name_groups(client, model: str, members: list[list[str]], log) -> list[str]:
    """LLM 拟组名;失败/不合规则回退"<首条>等"。"""
    # 回退名带"等N项":既自然又不易与其他家的原文标题逐字相同(反串标)
    fallback = [f"{m[0]}等{len(m)}项" if len(m) > 1 else m[0] for m in members]
    if client is None:
        return fallback
    lines = [f"{k+1}. " + "；".join(m) for k, m in enumerate(members)]
    prompt = (
        "下面每行是一组小节,请为每组拟一个 4-14 字的施工组织设计二级标题主题名"
        "(如「项目基本情况」「施工条件与场地准备」「质量责任与验收控制」)。要求:\n"
        "① 概括该组全部成员的共同主题,不得空洞(禁用「其他」「相关内容」「综合说明」);\n"
        "② 同一批内组名不得重复;\n"
        '③ 只输出 JSON: {"titles": ["...", ...]},条数与顺序一致。\n\n' + "\n".join(lines)
    )
    try:
        r = client.chat(model_key=model, system="你是施工组织设计目录结构设计专家。",
                        user=prompt, temperature=0.7)
        txt = r.text
        names = [str(x).strip()[:16] for x in
                 json.loads(txt[txt.find("{"): txt.rfind("}") + 1]).get("titles", [])]
        if len(names) != len(members):
            log.warning(f"  组名条数不符({len(names)}/{len(members)}),回退默认名")
            return fallback
        if len(set(names)) != len(names):
            log.warning("  组名有重复,回退默认名")
            return fallback
        return [n if len(n) >= 4 else fb for n, fb in zip(names, fallback)]
    except Exception as e:  # noqa: BLE001
        log.warning(f"  组名生成失败({e}),回退默认名")
        return fallback


def han(s: str) -> int:
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff")


def regroup_variant(tender_dir: Path, vid: str, idx: int, log, dry_run: bool = False,
                    client=None, force: bool = False) -> dict:
    """重排某变体的二级/三级标题。返回报告 dict(status: ok/skip/fail)。"""
    vdir = tender_dir / "output" / vid
    plan_p = vdir / "chapter_plan_variant.md"
    if not plan_p.exists():
        return {"variant": vid, "status": "fail", "reason": "缺 chapter_plan_variant.md"}
    mds = {p.name[:2]: p for p in sorted(vdir.glob("[0-9][0-9]_*.md"))}
    if not mds:
        return {"variant": vid, "status": "fail", "reason": "未找到章节 md"}

    # 幂等:双判据 —— 计划里的结构标记(抗报告丢失) + 报告文件。
    # 不能只数正文的 ###:写手正文可能自带三级小标题,会误判为"已分组"。
    _plan_txt = plan_p.read_text(encoding="utf-8")
    _marked = bool(re.search(r"^>\s*bid_regroup:\s*k=", _plan_txt, re.M))
    _report_p = vdir / "regroup_report.json"
    if _marked and not force:
        _m = re.search(r"^>\s*bid_regroup:\s*k=(\d+),\s*items=(\d+),\s*groups=(\d+)", _plan_txt, re.M)
        log.info(f"  {vid}: 计划已带分组标记({_m.group(0) if _m else ''}),跳过")
        return {"variant": vid, "status": "skip", "reason": "already_grouped"}
    if force and _marked:
        # 已分组状态下重建会把"二级节"当条目二次嵌套 → 拒绝(需先恢复平铺结构)
        return {"variant": vid, "status": "fail",
                "reason": "计划已带分组标记,--force 会二次嵌套;请先恢复到平铺(未分组)章节与计划"}
    if _report_p.exists() and not force:
        try:
            _prev = json.loads(_report_p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _prev = {}
        if _prev.get("status") == "ok":
            log.info(f"  {vid}: 已有分组报告({_prev.get('items')} 条 → {_prev.get('groups')} 节),跳过")
            return {"variant": vid, "status": "skip", "reason": "already_grouped",
                    "items": _prev.get("items"), "groups": _prev.get("groups")}

    size = group_size_for(idx)
    if size <= 1:
        log.info(f"  {vid}: 粒度 {size}(平铺),无需分组")
        return {"variant": vid, "status": "skip", "reason": "flat_by_design"}

    plan_text = plan_p.read_text(encoding="utf-8")
    subs = parse_chapter_lines(plan_text)

    # ① 分组 + 拟名
    chapters: dict[str, list[tuple[str, list[int]]]] = {}
    members_for_naming: list[list[str]] = []
    for num in sorted(subs):
        titles = [t for t, _ in subs[num]]
        groups = chunk(titles, size)
        chapters[num] = [("", g) for g in groups]
        members_for_naming += [[titles[i - 1] for i in g] for g in groups]
    names = name_groups(client, "deepseek_chat", members_for_naming, log)
    k = 0
    for num in sorted(subs):
        for gi in range(len(chapters[num])):
            chapters[num][gi] = (names[k], chapters[num][gi][1])
            k += 1

    # ② 不变量:无重叠无遗漏
    for num in sorted(subs):
        n = len(subs[num])
        used = [i for _, g in chapters[num] for i in g]
        if sorted(used) != list(range(1, n + 1)):
            return {"variant": vid, "status": "fail",
                    "reason": f"章 {num} 分组有重叠/遗漏 (used={len(used)} n={n})"}

    # ③ 重排 md(**先在内存全部构建并校验,最后统一写盘** —— 逐章写盘会在后续章失败时
    #    留下半成品,且计划未更新 → heads≠titles 重跑再 fail,变体永久卡死)
    report_chapters = []
    pending: list[tuple[Path, str]] = []
    for num, path in sorted(mds.items()):
        if num not in chapters:
            continue
        orig = path.read_text(encoding="utf-8")
        parts = re.split(r"^## .+$", orig, flags=re.M)
        heads = re.findall(r"^## .+$", orig, flags=re.M)
        titles = [t for t, _ in subs[num]]
        if len(heads) != len(titles):
            return {"variant": vid, "status": "fail",
                    "reason": f"章 {num} 二级标题数 {len(heads)} ≠ 计划条目数 {len(titles)}"}
        bodies = parts[1:]
        out = [parts[0].rstrip(), ""]
        for gi, (gtitle, items) in enumerate(chapters[num], 1):
            out += [f"## {head_group(idx, gi, num)}{gtitle}", ""]
            for kk, item_i in enumerate(items, 1):
                out += [f"### {head_item(idx, kk, gi, num)}{titles[item_i - 1]}"]
                body = bodies[item_i - 1].strip("\n")
                if body:
                    # 写手正文自带的三级小标题降为四级,避免与"条目降级来的 ###"同级
                    body = re.sub(r"^###\s+", "#### ", body, flags=re.M)
                    out.append(body)
                out += [""]
        new = "\n".join(out).rstrip() + "\n"
        # 结构不变量:内容不得丢(组名是新标题,允许小幅增长;严禁减少)
        _h0, _h1 = han(orig), han(new)
        if _h1 < _h0 - max(30, _h0 * 0.01) or _h1 > _h0 + max(60, _h0 * 0.08):
            return {"variant": vid, "status": "fail",
                    "reason": f"章 {num} 汉字数异常 {_h0}→{_h1}(只允许因新增组名小幅上升)"}
        if len(re.findall(r"```json", new)) != len(re.findall(r"```json", orig)):
            return {"variant": vid, "status": "fail", "reason": f"章 {num} json 块数变化"}
        if len(re.findall(r"^### ", new, re.M)) != len(titles):
            return {"variant": vid, "status": "fail",
                    "reason": f"章 {num} 三级标题数 ≠ 计划条目数"}
        pending.append((path, new))
        report_chapters.append({"num": num, "items": len(titles),
                                "groups": len(chapters[num]), "group_size": size})

    # ④ 计划子项清单 = 分组(否则 reviewer 的 H2 1:1 核查会 mismatch)
    if not dry_run:
        cur = parse_chapter_lines(plan_text)
        out, num, skip = [], None, False
        for ln in plan_text.split("\n"):
            h = re.match(r"^###\s+(\d{2})\s", ln)
            if h:
                num, skip = h.group(1), True
                out.append(ln)
                if num in chapters:
                    for gi, (gtitle, items) in enumerate(chapters[num], 1):
                        total = sum(cur[num][i - 1][1] for i in items)
                        out.append(f"- {head_group(idx, gi, num)}{gtitle}: {total} 字")
                continue
            if skip and re.match(r"^-\s", ln):
                continue
            if re.match(r"^##\s", ln):
                skip = False
            out.append(ln)
        _mark = (f"> bid_regroup: k={size}, items={sum(c['items'] for c in report_chapters)}, "
                 f"groups={sum(c['groups'] for c in report_chapters)}")
        plan_p.write_text("\n".join([out[0], _mark] + out[1:]), encoding="utf-8")

    if not dry_run:
        for _p, _txt in pending:
            _p.write_text(_txt, encoding="utf-8")
        (vdir / "regroup_report.json").write_text(
            json.dumps({"variant": vid, "status": "ok", "group_size": size,
                        "items": sum(c["items"] for c in report_chapters),
                        "groups": sum(c["groups"] for c in report_chapters),
                        "chapters": report_chapters}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    total_items = sum(c["items"] for c in report_chapters)
    total_groups = sum(c["groups"] for c in report_chapters)
    log.info(f"  ✓ {vid}: {total_items} 条大纲条目 → {total_groups} 个二级节(每 {size} 条并一组),"
             f"条目降为三级标题")
    return {"variant": vid, "status": "ok", "group_size": size,
            "items": total_items, "groups": total_groups, "chapters": report_chapters}


def main() -> int:
    p = argparse.ArgumentParser(description="合格制批量:二级标题数量/层级差异化(目录指纹族第三层)")
    p.add_argument("tender_dir")
    p.add_argument("--variant", default=None, help="仅处理指定变体(如 V02)")
    p.add_argument("--force", action="store_true",
                   help="忽略幂等报告重建(仅当章节仍为平铺结构;已分组时会拒绝以免二次嵌套)")
    p.add_argument("--dry-run", action="store_true", help="只校验不改文件")
    args = p.parse_args()

    tender = Path(args.tender_dir)
    log = make_script_logger("regroup", tender / "output" / "logs" / "bid_regroup.log")
    log.info("=" * 60)
    log.info(f"bid_regroup 启动: {tender}")

    vp = tender / "extracted" / "variants.json"
    if not vp.exists():
        log.info("  非批量模式(缺 variants.json),跳过目录重组")
        return 0
    try:
        payload = json.loads(vp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.error(f"variants.json 解析失败: {e}")
        return 2
    variants = payload.get("variants") if isinstance(payload, dict) else payload
    if not isinstance(variants, list) or not variants:
        log.info("  variants 为空,跳过")
        return 0
    # 单变体无差异化对象:目录指纹族(二级标题数量/层级)只用于多投标人反串标,
    # 且 chapter_plan_variant.md 仅由 plan_variants 产生——单份模式不会跑那步。
    # 不在这里早退的话,单份项目会稳定报“缺 chapter_plan_variant.md”失败
    # (bid_run 侧降级为警告不阻断,但日志噪声大且误导排查)。
    if len(variants) < 2 and not args.variant:
        log.info(f"  仅 {len(variants)} 家投标,无反串标差异化需求,跳过目录重组")
        return 0
    order = {str(v.get("id", "")): i for i, v in enumerate(variants)}
    targets = [v for v in variants
               if not args.variant or str(v.get("id", "")).upper() == args.variant.upper()]

    client = None
    try:
        from bid_llm_client import LLMClient
        client = LLMClient.load(log=log)
    except Exception as e:  # noqa: BLE001
        log.warning(f"  LLM 客户端不可用({e}),组名将用默认名(首条+等)")

    reports, fails = [], 0
    for v in targets:
        vid = str(v.get("id", ""))
        r = regroup_variant(tender, vid, order.get(vid, 0), log, args.dry_run, client, args.force)
        reports.append(r)
        if r["status"] == "fail":
            fails += 1
            log.error(f"  ✗ {vid} 重组失败: {r.get('reason')}")

    # 跨变体指纹校验:两两标题同名率应为 0
    h2 = {}
    for v in variants:
        vid = str(v.get("id", ""))
        pl = tender / "output" / vid / "chapter_plan_variant.md"
        if pl.exists():
            h2[vid] = [strip_num(t) for t, _ in
                       [x for num in parse_chapter_lines(pl.read_text(encoding="utf-8")).values() for x in num]]
    ids = sorted(h2)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = h2[ids[i]], h2[ids[j]]
            same = len(set(a) & set(b))
            flag = "✓" if same == 0 else "⚠"
            log.info(f"  指纹核对 {ids[i]}↔{ids[j]}: 二级标题同名 {same} 条 {flag}"
                     f" ({len(a)} vs {len(b)} 条)")
    out = tender / "output" / "regroup_summary.json"
    out.write_text(json.dumps({"reports": reports}, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info(f"汇总: {out}")
    if fails:
        log.error(f"{fails} 个变体重组失败(未写盘),请查上文")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

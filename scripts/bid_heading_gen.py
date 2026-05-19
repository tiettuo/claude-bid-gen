#!/usr/bin/env python3
"""bid_heading_gen.py — 为所有变体生成章节标题结构（不写正文）。

合格制模式：LLM 自由组织 H2/H3 结构。
评分制模式：从 chapter_plan_variant.md 读取预设标题。

用法: python bid_heading_gen.py <tender_dir>
"""

import json
import re
import sys
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from bid_common import make_script_logger, parse_chapter_plan, parse_plan_subitems
from bid_llm_client import LLMClient


_HEADING_PROMPT_PASSFAIL = """\
你是投标文件章节规划专家。请为以下章节设计标题结构。

项目简介：
{project_brief}

章节号：第 {ch_num} 章
章节标题：{ch_title}
本章目标字数：{target} 字

要求：
1. 自行决定分几个二级标题 ##（建议 3-7 个）
2. 每个二级标题下可按需添加三级标题 ###（0-3 个）
3. 标题必须紧扣章节主题和项目实际（高原牧业基础设施）
4. 严禁使用空洞模板化表述（如"编制依据/实施方案/保障措施/应急响应"）
5. 每个二级标题后标注目标字数（总和 = {target}）

必须覆盖的关键内容（自然融入各小节）：
{coverage}

严格按以下 JSON 格式输出，不要输出其他内容：
{{
  "headings": [
    {{"title": "...", "chars": N, "children": [{{"title": "..."}}, ...]}},
    ...
  ]
}}"""


def load_llm_client(log):
    llm_cfg = {}
    cfg_path = ROOT / "configs" / "bid_llm_config.json"
    if cfg_path.exists():
        llm_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return LLMClient(llm_cfg, log)


def generate_headings_passfail(
    client, ch, variant, project_brief, coverage, log
) -> dict:
    """合格制：LLM 自由生成标题结构。"""
    prompt = _HEADING_PROMPT_PASSFAIL.format(
        project_brief=project_brief[:500],
        ch_num=ch["num"],
        ch_title=ch["title"],
        target=ch.get("target_chars", 1950),
        coverage=coverage or "（无特定关键词）",
    )
    try:
        resp = client.chat(
            model_key=variant["model"],
            system="你是投标文件章节结构设计专家，擅长针对具体项目设计专业、差异化的章节结构。",
            user=prompt,
            temperature=variant.get("temperature", 0.7),
        )
        text = resp.text.strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            log.warning(f"[{variant['id']}] 章 {ch['num']} 标题生成失败，返回无法解析")
            return {"headings": []}
        return json.loads(m.group())
    except Exception as e:
        log.warning(f"[{variant['id']}] 章 {ch['num']} 标题生成异常: {e}")
        return {"headings": []}


def generate_headings_scoring(subitems: list[dict]) -> dict:
    """评分制：直接用预设标题。"""
    headings = []
    for s in subitems:
        h = {"title": s["title"], "chars": s.get("chars", 0), "children": []}
        headings.append(h)
    return {"headings": headings}


def main():
    if len(sys.argv) < 2:
        print("用法: python bid_heading_gen.py <tender_dir>")
        sys.exit(1)

    tender_dir = Path(sys.argv[1])
    extracted = tender_dir / "extracted"
    output = tender_dir / "output"
    log = make_script_logger("heading_gen", output / "logs" / "heading_gen.log")

    # 加载输入
    plan_path = extracted / "chapter_plan.md"
    variants_path = extracted / "variants.json"
    brain_path = extracted / "project_brain.md"
    authoring_path = extracted / "authoring_rules.md"

    chapters = parse_chapter_plan(plan_path)
    variants = json.loads(variants_path.read_text(encoding="utf-8"))
    if isinstance(variants, dict):
        variants = variants.get("variants", [variants])

    project_brief = ""
    if brain_path.exists():
        project_brief = brain_path.read_text(encoding="utf-8")[:1000]

    # 评标模式
    eval_mode = "passfail"
    if authoring_path.exists():
        m = re.search(r"evaluation_mode:\s*(\w+)", authoring_path.read_text(encoding="utf-8"))
        if m:
            eval_mode = m.group(1)
    is_scoring = eval_mode == "scoring"
    log.info(f"评标模式: {eval_mode} ({'评分制-严格对齐' if is_scoring else '合格制-自由发挥'})")

    client = load_llm_client(log)

    # 构建 tasks
    tasks = []
    for v in variants:
        vid = v["id"]
        v_dir = output / vid
        v_dir.mkdir(parents=True, exist_ok=True)

        # 评分制时读取 variant plan
        v_subitems_map = {}
        if is_scoring:
            vp = v_dir / "chapter_plan_variant.md"
            if vp.exists():
                v_subitems_map = parse_plan_subitems(vp)

        for ch in chapters:
            out_path = v_dir / f"headings_{str(ch['num']).zfill(2)}.json"
            # 跳过已存在
            if out_path.exists() and "--force" not in sys.argv:
                continue
            tasks.append((v, ch, out_path, v_subitems_map))

    log.info(f"待生成: {len(tasks)} 个标题结构 ({len(variants)} 变体 × {len(chapters)} 章)")

    # 覆盖关键词
    def get_coverage(ch):
        notes = ch.get("notes", "")
        return "、".join(notes.split(",")[:10]) if notes else ""

    def run_one(v, ch, out_path, v_subitems_map):
        vid = v["id"]
        ch_num = ch["num"]
        coverage = get_coverage(ch)

        if is_scoring:
            subitems = v_subitems_map.get(ch_num, [])
            result = generate_headings_scoring(subitems)
        else:
            result = generate_headings_passfail(client, ch, v, project_brief, coverage, log)

        result["variant_id"] = vid
        result["chapter_num"] = ch_num
        result["chapter_title"] = ch["title"]
        result["eval_mode"] = eval_mode

        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        n_h2 = len(result.get("headings", []))
        n_h3 = sum(len(h.get("children", [])) for h in result.get("headings", []))
        log.info(f"  {vid} 章 {ch_num}: {n_h2} H2 + {n_h3} H3")
        return vid, ch_num

    # 并行
    workers = 4
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(run_one, v, ch, p, sm): (v["id"], ch["num"]) for v, ch, p, sm in tasks}
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                vid, ch_num = futures[f]
                log.error(f"  {vid} 章 {ch_num} 失败: {e}")

    # 生成汇总 HTML
    _generate_html(output, variants, chapters, eval_mode, log)
    log.info(f"完成！{len(tasks)} 个标题结构已生成")


def _generate_html(output, variants, chapters, eval_mode, log):
    """生成标题对比 HTML。"""
    ch_names = {
        '01':'工程概况','02':'编制依据','03':'总体施工部署','04':'项目组织管理机构',
        '05':'施工总进度计划','06':'施工总平面布置','07':'施工方案与技术措施',
        '08':'质量管理体系与措施','09':'工期保证技术组织措施','10':'安全文明施工体系与措施',
        '11':'绿色施工管理体系与措施','12':'季节性施工措施',
        '13':'地下管线及设施保护','14':'民工工资保证措施','15':'附图及附表','16':'其他施工组织总设计'
    }
    colors = ['#fff3e0','#e8f5e9','#e3f2fd','#fce4ec','#f3e5f5','#e0f7fa','#fff8e1','#efebe9','#e8eaf6','#e2f7e1']

    # 加载所有标题数据
    all_data = {}
    for v in variants:
        vid = v["id"]
        all_data[vid] = {}
        for ch in chapters:
            ch_num = str(ch["num"]).zfill(2)
            fp = output / vid / f"headings_{ch_num}.json"
            if fp.exists():
                all_data[vid][ch_num] = json.loads(fp.read_text(encoding="utf-8"))
            else:
                all_data[vid][ch_num] = {"headings": []}

    mode_label = "评分制（严格对齐）" if eval_mode == "scoring" else "合格制（自由发挥）"

    html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>10份方案标题结构对比</title><style>
body{{font-family:"Microsoft YaHei","PingFang SC",sans-serif;margin:20px;background:#fafafa}}
h1{{color:#333;border-bottom:2px solid #1976d2;padding-bottom:8px}}
h2{{color:#1565c0;margin-top:30px;cursor:pointer}}
table{{border-collapse:collapse;width:100%;margin:8px 0 20px;font-size:13px}}
th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left;vertical-align:top}}
th{{background:#1976d2;color:#fff;position:sticky;top:0}}
td:first-child{{background:#f5f5f5;font-weight:bold;text-align:center;min-width:30px}}
.h3{{color:#666;font-size:12px;margin-left:12px}}
.stats{{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}}
.stat-card{{background:#fff;border-radius:8px;padding:12px 16px;box-shadow:0 1px 3px rgba(0,0,0,.12);min-width:110px}}
.stat-card .num{{font-size:20px;font-weight:bold;color:#1976d2}}
.stat-card .label{{font-size:11px;color:#666}}
.badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;color:#fff;margin-left:6px}}
.badge.passfail{{background:#4caf50}}.badge.scoring{{background:#ff9800}}
</style></head><body>
<h1>10份方案标题结构对比 <span class="badge {eval_mode}">{mode_label}</span></h1>
<p>浪卡子县普玛江塘乡高原特色牧业基础设施建设项目</p>'''

    # 统计卡片
    html += '<div class="stats">'
    for ch in chapters:
        ch_num = str(ch["num"]).zfill(2)
        # 收集标题结构
        title_sets = set()
        h2_counts = []
        for v in variants:
            vid = v["id"]
            headings = all_data.get(vid, {}).get(ch_num, {}).get("headings", [])
            titles = tuple(h["title"] for h in headings)
            title_sets.add(titles)
            h2_counts.append(len(headings))
        unique = len(title_sets)
        cnt_range = f"{min(h2_counts)}-{max(h2_counts)}" if h2_counts else "0"
        icon = "✅" if unique >= 8 else ("📌" if ch_num in ("01","15") else "⚠️")
        html += f'<div class="stat-card"><div class="num">{icon} {unique}/10</div><div class="label">第{ch_num}章 {ch_names.get(ch_num,"")}<br/>H2: {cnt_range}</div></div>'
    html += '</div>'

    # 各章节表格
    for ch in chapters:
        ch_num = str(ch["num"]).zfill(2)
        max_h2 = max(
            len(all_data.get(v["id"], {}).get(ch_num, {}).get("headings", []))
            for v in variants
        )
        html += f'<h2 onclick="toggleSection(this)">▶ 第{ch_num}章 {ch_names.get(ch_num,"")}</h2>'
        html += '<div style="display:none"><table><tr><th>#</th>'
        for vi, v in enumerate(variants):
            headings = all_data.get(v["id"], {}).get(ch_num, {}).get("headings", [])
            html += f'<th>{v["id"]}({len(headings)}项)</th>'
        html += '</tr>'

        for i in range(max_h2):
            html += f'<tr><td>{i+1}</td>'
            for vi, v in enumerate(variants):
                vid = v["id"]
                headings = all_data.get(vid, {}).get(ch_num, {}).get("headings", [])
                if i < len(headings):
                    h = headings[i]
                    title = h["title"]
                    chars = h.get("chars", "")
                    children = h.get("children", [])
                    chars_str = f' <span style="color:#999;font-size:11px">({chars}字)</span>' if chars else ""
                    h3_str = ""
                    if children:
                        h3_str = '<div class="h3">' + "<br/>".join(f"├ {c['title']}" for c in children) + '</div>'
                    html += f'<td style="background:{colors[vi]}">{title}{chars_str}{h3_str}</td>'
                else:
                    html += f'<td style="background:{colors[vi]};color:#ccc">—</td>'
            html += '</tr>'
        html += '</table></div>'

    html += '''<script>
function toggleSection(el){var c=el.nextElementSibling;c.style.display=c.style.display==="none"?"block":"none";el.textContent=el.textContent.includes("▶")?el.textContent.replace("▶","▼"):el.textContent.replace("▼","▶")}
</script></body></html>'''

    out = output / "H2_diff_table.html"
    out.write_text(html, encoding="utf-8")
    log.info(f"HTML 已保存: {out}")


if __name__ == "__main__":
    main()

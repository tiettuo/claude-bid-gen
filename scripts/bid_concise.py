#!/usr/bin/env python3
"""超页降页工具:对章节做无损 LLM 精简(保留标题/数字/json图块,表格≤6列)。

实测一轮即饱和(feedback_bid_page_cap_overrun),verify 门禁对 page_over_cap 只调一轮。
精简后必须重跑 bid_assemble(docx 已 stale)。

用法:
  python bid_concise.py <tender_dir> --variant V01 --ratio 0.85   # 批量某变体
  python bid_concise.py <tender_dir> --ratio 0.9                  # 单份(章节在 output/ 根)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bid_llm_client import LLMClient  # noqa: E402
from bid_common import make_script_logger, count_chinese_chars  # noqa: E402

SYS = ("你是资深施工组织设计精简专家。这次任务是**降页数**——必须把本章压到给定字数"
       "上限以内,允许删减信息量(合并条目、去掉铺垫与例证),而不是只做措辞润色。")


def concise(client: LLMClient, path: Path, target: int) -> tuple[int, int, int]:
    """精简单章到约 target 字;返回 (原字符数, 新字符数, 保护的 json 块数)。"""
    txt = path.read_text(encoding="utf-8")
    # 保护 json 图块:抽出占位,精简后还原
    blocks: list[str] = []

    def _stash(m):
        blocks.append(m.group(0))
        return f"@@JSONBLOCK{len(blocks)-1}@@"

    body = re.sub(r'```json\s*\n.*?\n```', _stash, txt, flags=re.DOTALL)
    rules = f"""把下面这一章施工组织设计压缩到 **不超过 {target} 字**(纯正文汉字数,不含标题)。
★ 这是硬上限:输出超过 {target} 字即判定失败,必须继续删减。

【可以且应当删减的】
A. 并列长列举:同类措施/工序/工种的长串列举,只保留 3-4 个代表项 + "等",其余删除
B. 铺垫与过渡句、意义阐述、口号式表态("为确保…""为进一步…""高度重视…"、综上所述等)
C. 规范条文的逐条复述:只需保留规范号与名称("符合 GB/T 50502 规定"),不抄条文内容
D. 同一要点的重复表述(不同小节重复说同一件事时只留一处)
E. 可由通用规范推知的操作细节、示例性数字(如示意性的"3 人/5 台"示例值)
F. 表格中"备注/进场时间/制造年份/退场时间"等次要列(表格列数必须 ≤6)

【必须保留】
1. 所有 # / ## / ### 标题一字不改、顺序不变(这是招标大纲锚点)
2. 本项目核心数据不可改不可删:工程量(面积/体积/长度/数量)、招标工期天数、
   计划开工/竣工基准、质量目标、控制价、温度/海拔等硬数据,出现时原样保留
3. 表格列数 ≤6 列;超 6 列按上条 F 压缩
4. 保留所有 @@JSONBLOCK数字@@ 占位符原样不动
5. 不得出现具体公司名/单位名;主体用"本项目部"
6. 直接输出压缩后的完整 markdown,不要代码围栏、不要解释

输出前自检:估算正文汉字数,若仍 > {target} 字,回到 A-F 继续删减直到达标。
原文:
"""
    resp = client.chat("deepseek_chat", SYS, rules + body, temperature=0.3, max_tokens=8000)
    out = resp.text.strip()
    out = re.sub(r'^```\w*\s*\n', '', out)
    out = re.sub(r'\n```\s*$', '', out)
    for i, b in enumerate(blocks):
        out = out.replace(f"@@JSONBLOCK{i}@@", b)
    path.write_text(out, encoding="utf-8")
    return len(txt), len(out), len(blocks)


def chapter_mds(chapter_dir: Path) -> list[Path]:
    """章节 md 固定 NN_ 前缀命名;报告类 md 不会命中。"""
    return sorted(p for p in chapter_dir.glob("*.md") if re.match(r"\d{2}_", p.name))


def main() -> int:
    ap = argparse.ArgumentParser(description="超页降页:章节 LLM 无损精简")
    ap.add_argument("tender_dir", help="投标根目录(含 output/)")
    ap.add_argument("--variant", default="", help="变体 ID(如 V01);缺省=单份,章节在 output/ 根")
    ap.add_argument("--ratio", type=float, default=0.85,
                    help="目标字数比例(精简到原文汉字数的 R 倍,默认 0.85)")
    ap.add_argument("--min-chars", type=int, default=2500,
                    help="汉字数低于此值的章节跳过(短章压缩收益低)")
    args = ap.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    output = tender_dir / "output"
    target_dir = output / args.variant.upper() if args.variant else output
    log = make_script_logger("bid_concise", output / "logs" / "concise.log")

    mds = chapter_mds(target_dir)
    if not mds:
        log.error(f"未找到章节 md: {target_dir}")
        return 2
    if not (0.3 <= args.ratio < 1.0):
        log.error(f"--ratio {args.ratio} 不在 [0.3, 1.0) 区间")
        return 2

    client = LLMClient.load(log=log)
    log.info(f"精简 {target_dir.name or 'output'}: {len(mds)} 章 ratio={args.ratio}")
    done = failed = 0
    for md in mds:
        cn_chars = count_chinese_chars(md.read_text(encoding="utf-8"))
        if cn_chars < args.min_chars:
            log.info(f"  跳过 {md.name}: {cn_chars} 字 < {args.min_chars}")
            continue
        target = int(cn_chars * args.ratio)
        try:
            a, b, nb = concise(client, md, target)
            log.info(f"  {md.name}: {a}→{b} 字符 (目标{target}字, json块保护={nb})")
            done += 1
        except Exception as e:
            log.error(f"  {md.name} 精简失败: {e}")
            failed += 1
    # 预算追踪:落盘累计 token(与 writer 同一文件,不同 label 分桶)
    client.flush_stats_to_json(output / "logs" / "llm_stats.json", "concise")
    log.info(f"完成: 精简 {done} 章, 失败 {failed} 章")
    return 0 if failed == 0 and done > 0 else (1 if done else 2)


if __name__ == "__main__":
    sys.exit(main())

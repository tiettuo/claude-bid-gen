#!/usr/bin/env python3
"""章节扩写：逐 H2 小节送给指定下游大模型扩写，原文保留，追加技术细节到达标字数。

用法:
    python bid_expand.py <项目目录> --chapter 02 --model deepseek --target 28203
    python bid_expand.py <项目目录> --all  # 按 chapter_plan 自动补到 85% 下限
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_llm_client import LLMClient  # type: ignore


SYS_PROMPT = """你是资深投标文件编写工程师，擅长把施工组织设计写得翔实、专业、贴合具体工程。
规则（必须严格遵守）:
1. 保留用户提供的"原文"一字不改（包括所有 Markdown 标题、表格、列表）。
2. 在原文基础上**扩充内容**：增加技术参数、工艺步骤、质量控制要点、计算数据、规范条款引用（GB/JGJ/CJJ）、针对性应对措施。
3. 只能在原文段落之间插入新段落，或在段落末尾追加内容；不得新增 H1/H2 标题，不得改动既有标题文字。
4. 【暗标硬约束】禁止出现任何公司名、人名、LOGO、项目业绩甲方、获奖证书编号；禁止使用装饰字符【】★◆■●※☆；禁止 Markdown 斜体 *text*；禁止破折号 — 和连字符 –，需要列举用"、"，需要范围用"至"。
5. 强调一律用 **粗体**。不使用 emoji。
6. 输出纯 Markdown 正文，不要包 ``` 代码块，不要加前言/后记，不要说"以下是扩写后的内容"之类的话。
7. 扩写后字数应为原文的 1.8 至 2.5 倍。内容必须与本章主题紧密相关，不得用通用套话灌水。"""


def split_sections(md: str) -> list[tuple[str, str]]:
    """按 H2 切分。返回 [(header_line, body), ...]，第一段可能是 H1 + 前言。"""
    lines = md.splitlines(keepends=True)
    sections: list[tuple[str, str]] = []
    current_header = ""
    current_body: list[str] = []
    for ln in lines:
        if re.match(r"^##\s", ln):
            if current_header or current_body:
                sections.append((current_header, "".join(current_body)))
            current_header = ln
            current_body = []
        else:
            current_body.append(ln)
    sections.append((current_header, "".join(current_body)))
    return sections


def expand_section(client: LLMClient, model: str, project_context: str,
                   chapter_title: str, header: str, body: str,
                   log: logging.Logger) -> str:
    if len(body.strip()) < 50:
        return body  # 太短（只是标题间的空行）不扩写
    user_msg = f"""【项目背景】
{project_context}

【当前章节】
{chapter_title}

【本节标题】
{header.strip() or '(前言段)'}

【本节原文】
{body}

【任务】
在保留原文一字不动的前提下，扩充本节内容到 {int(len(body) * 2.0)} 字左右（原文的 2 倍），新增技术细节、工艺参数、规范引用、具体应对措施。遵守系统规则。直接输出扩写后的完整 Markdown。"""
    resp = client.chat(
        model_key=model,
        system=SYS_PROMPT,
        user=user_msg,
        temperature=0.7,
    )
    out = resp.text.strip()
    # 剥掉可能的围栏
    out = re.sub(r"^```(?:markdown|md)?\n", "", out)
    out = re.sub(r"\n```$", "", out)
    # 暗标硬清洗
    out = out.replace("—", "、").replace("–", "至")
    out = re.sub(r"([\u4e00-\u9fff])-([\u4e00-\u9fff])", r"\1、\2", out)
    out = re.sub(r"[【】★◆■●※☆]", "", out)
    log.info(f"  section expanded: {len(body)} -> {len(out)} chars (model={resp.model})")
    return out


def expand_chapter(project: Path, chapter_num: str, model: str, target: int,
                   log: logging.Logger) -> None:
    client = LLMClient.load(log=log)
    # 找章节文件
    md_files = sorted(project.glob(f"output/{chapter_num}_*.md"))
    if not md_files:
        raise FileNotFoundError(f"未找到 {chapter_num}_*.md")
    md_path = md_files[0]
    orig = md_path.read_text(encoding="utf-8")
    # 提取章节标题（第一个 H1）
    m = re.search(r"^#\s+(.+)$", orig, re.MULTILINE)
    chapter_title = m.group(1).strip() if m else md_path.stem
    # 项目背景（brain 前 2000 字）
    brain = (project / "extracted/project_brain.md").read_text(encoding="utf-8")
    project_context = brain[:2000]

    log.info(f"扩写 {md_path.name}  当前={len(orig)} 字  目标={target} 字  模型={model}")

    sections = split_sections(orig)
    new_parts: list[str] = []
    for header, body in sections:
        expanded_body = expand_section(
            client, model, project_context, chapter_title, header, body, log
        )
        if header:
            new_parts.append(header + expanded_body if not expanded_body.startswith("#") else expanded_body)
        else:
            new_parts.append(expanded_body)
        # 中途字数够就停（省 token）
        current_len = sum(len(p) for p in new_parts) + sum(
            len(h) + len(b) for h, b in sections[len(new_parts):]
        )
        if current_len >= target:
            # 把剩余未扩写的段落原样追加
            for h, b in sections[len(new_parts):]:
                new_parts.append(h + b if h else b)
            break

    final = "".join(new_parts)
    # 备份
    backup = md_path.with_suffix(".md.bak")
    if not backup.exists():
        backup.write_text(orig, encoding="utf-8")
    md_path.write_text(final, encoding="utf-8")
    log.info(f"✓ {md_path.name}  {len(orig)} -> {len(final)} 字")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("project", type=Path)
    p.add_argument("--chapter", help="章节编号，如 02")
    p.add_argument("--model", default="deepseek")
    p.add_argument("--target", type=int, default=0, help="目标字数；0 则按 chapter_plan 85%%")
    p.add_argument("--all", action="store_true", help="扩所有未达标章节")
    args = p.parse_args()

    log = logging.getLogger("expand")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    # 解析 chapter_plan 取目标字数
    plan = (args.project / "extracted/chapter_plan.md").read_text(encoding="utf-8")
    targets: dict[str, int] = {}
    for ln in plan.splitlines():
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) < 9:
            continue
        if re.fullmatch(r"\d{2}", cells[1] if cells[0] == "" else cells[0]):
            row = cells[1:] if cells[0] == "" else cells
            num = row[0]
            # target is col index 6 (章节号,文件,标题,评分项,分值,子项数,目标字数)
            try:
                targets[num] = int(row[6].replace(",", ""))
            except (ValueError, IndexError):
                pass

    if args.all:
        for num in sorted(targets):
            md_files = sorted(args.project.glob(f"output/{num}_*.md"))
            if not md_files:
                continue
            cur = len(md_files[0].read_text(encoding="utf-8"))
            floor = int(targets[num] * 0.85)
            if cur < floor:
                try:
                    expand_chapter(args.project, num, args.model, floor, log)
                except Exception as e:
                    log.error(f"✗ 章 {num} 失败: {e}")
    else:
        if not args.chapter:
            p.error("需要 --chapter 或 --all")
        target = args.target or int(targets.get(args.chapter, 14000) * 0.85)
        expand_chapter(args.project, args.chapter, args.model, target, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())

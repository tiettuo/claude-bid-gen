#!/usr/bin/env python3
"""临时驱动:安全精简章节 —— 自己抽出 ```json 块,压缩后按标题原位回填。

bid_concise.concise() 依赖模型原样保留 @@JSONBLOCKn@@ 占位符,实测 glm/deepseek
会把占位符改写成 JSON 本体并注入杂字符("children":3[ / 安全@员),JSON 直接失效。
本驱动改为外部保管 JSON 块,模型完全看不到它们。

用法: python3 bid_concise_safe.py <tender_dir> <V01|""> "01:1069,02:690,..."
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bid_common import count_chinese_chars, make_script_logger  # noqa: E402
from bid_concise import concise  # noqa: E402
from bid_llm_client import LLMClient  # noqa: E402

HEAD = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")


def split_sections(text: str):
    """→ [(heading_or_None, body)];body 不含 heading 行。"""
    marks = [(m.start(), m.end(), m.group(0)) for m in HEAD.finditer(text)]
    if not marks:
        return [(None, text)]
    out = []
    if marks[0][0] > 0:
        out.append((None, text[: marks[0][0]]))
    for i, (s, e, h) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((h.strip(), text[e:nxt]))
    return out


def main() -> int:
    tender_dir = Path(sys.argv[1]).expanduser().resolve()
    variant = sys.argv[2].strip()
    spec = sys.argv[3]
    targets = {k: int(v) for k, v in (x.split(":") for x in spec.split(","))}

    target_dir = tender_dir / "output" / variant if variant else tender_dir / "output"
    log = make_script_logger("concise_safe", tender_dir / "output" / "logs" / "concise_safe.log")
    client = LLMClient.load(log=log)

    for num, tgt in targets.items():
        cand = sorted(target_dir.glob(f"{num}_*.md"))
        if not cand:
            log.warning(f"  章 {num} 未找到,跳过")
            continue
        p = cand[0]
        raw = p.read_text(encoding="utf-8")
        sections = split_sections(raw)

        # 1) 抽出 json 块(连同所属 heading)
        holder: list[tuple[str | None, list[str]]] = []
        stripped = []
        for head, body in sections:
            jsons = re.findall(r"```json\s*\n.*?\n```", body, flags=re.DOTALL)
            if jsons:
                holder.append((head, jsons))
                for j in jsons:
                    body = body.replace(j, "")
            stripped.append((head, body))
        tmp_text = "".join((h + "\n" if h else "") + b for h, b in stripped)
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(tmp_text, encoding="utf-8")
        before = count_chinese_chars(tmp_text)

        # 2) 压缩
        try:
            _, _, _ = concise(client, tmp, tgt)
        except Exception as e:  # noqa: BLE001
            log.error(f"  {p.name} 精简失败: {e}")
            tmp.unlink(missing_ok=True)
            continue
        new_text = tmp.read_text(encoding="utf-8")
        tmp.unlink(missing_ok=True)

        # 3) 按 heading 原位回填 json 块
        now = split_sections(new_text)
        idx = {h: i for i, (h, _) in enumerate(now) if h}
        out = []
        for head, body in now:
            out.append((head, body))
        for head, jsons in holder:
            i = idx.get(head)
            if i is None:
                log.warning(f"  {p.name}: 回填锚点标题丢失 {head!r},JSON 追加章末")
                out[-1] = (out[-1][0], out[-1][1] + "\n" + "\n\n".join(jsons) + "\n")
                continue
            out[i] = (out[i][0], out[i][1].rstrip() + "\n\n" + "\n\n".join(jsons) + "\n")

        final = "".join((h + "\n" if h else "") + b for h, b in out)
        # 4) JSON 有效性自检
        bad = 0
        for j in re.findall(r"```json\s*\n(.*?)\n```", final, flags=re.DOTALL):
            try:
                import json as _j

                _j.loads(j)
            except Exception:  # noqa: BLE001
                bad += 1
        after = count_chinese_chars(final)
        p.write_text(final, encoding="utf-8")
        log.info(f"  {p.name}: {before}→{after} 字 (目标{tgt}) JSON块={sum(len(x[1]) for x in holder)} 无效={bad}")
    client.flush_stats_to_json(tender_dir / "output" / "logs" / "llm_stats.json", "concise_safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())

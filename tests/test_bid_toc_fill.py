"""bid_toc_fill 回归测试。

覆盖 2026-09 某藏北乡镇派出所项目踩到的两个真实缺陷:

1. `toc_entries_in_order` 用 `rfind("<w:p ")` 定位段落 — 只认**带属性**的段落,
   而 python-docx 生成的普通段落是 `<w:p>`。于是回看会跳到文档里最后一个带属性的
   段落(常是节属性段),把从封面到该条目的整篇目录粘成**一条** entry →
   112 条 entry 全部匹配失败 → 目录页码整体落成「—」。
2. 目录条目必须按文档顺序、逐条返回,且不得把页码占位符本身的数字带进标题文本。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from bid_toc_fill import TOKEN_RE, toc_entries_in_order  # noqa: E402


def _p(texts: list[str], attrs: str = "") -> str:
    runs = "".join(f"<w:r><w:t>{t}</w:t></w:r>" for t in texts)
    return f"<w:p{attrs}>{runs}</w:p>"


def _docxml(paras: list[str], trailing: str = "") -> str:
    body = "".join(paras)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="x"><w:body>'
        + body
        + f'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
        + trailing
        + "</w:body></w:document>"
    )


def test_entries_split_when_paragraph_has_no_attributes():
    """核心回归:无属性的 `<w:p>` 也必须被识别为段落边界。"""
    xml = _docxml([
        _p(["封面标题"]),
        _p(["目　录"]),
        _p(["第一章 工程概况", "@@TOCPG1@@"]),
        _p(["1.1 项目主要情况", "@@TOCPG2@@"]),
        _p(["1.2 项目主要施工条件", "@@TOCPG3@@"]),
    ])
    entries = toc_entries_in_order(xml)

    assert len(entries) == 3, f"应逐条拆出 3 条,实际 {len(entries)}: {entries}"
    assert entries == [
        "第一章 工程概况",
        "1.1 项目主要情况",
        "1.2 项目主要施工条件",
    ]


def test_entries_ok_when_paragraph_has_attributes():
    """旧写法能过的场景(带属性段落)不能被新写法弄坏。"""
    xml = _docxml([
        _p(["目　录"]),
        _p(["第一章 工程概况", "@@TOCPG1@@"], attrs=' w:rsidR="00AB12"'),
        _p(["第二章 编制依据", "@@TOCPG2@@"], attrs=' w:rsidR="00AB12"'),
    ])
    assert toc_entries_in_order(xml) == ["第一章 工程概况", "第二章 编制依据"]


def test_entry_text_excludes_page_number_token():
    """页码占位符本身不能被当成标题文本的一部分。"""
    xml = _docxml([_p(["第三章 总体施工部署", "@@TOCPG17@@"])])
    entries = toc_entries_in_order(xml)
    assert entries == ["第三章 总体施工部署"]
    assert all("@@TOCPG" not in e for e in entries)


def test_entries_count_matches_token_count():
    """entry 数必须等于 @@TOCPGn@@ 占位符数 —— 少了就是段落边界又判错了。"""
    xml = _docxml([_p([f"章 {i}", f"@@TOCPG{i}@@"]) for i in range(1, 21)])
    entries = toc_entries_in_order(xml)
    assert len(entries) == len(TOKEN_RE.findall(xml)) == 20


def test_no_tokens_returns_empty():
    assert toc_entries_in_order(_docxml([_p(["无目录占位符"])])) == []

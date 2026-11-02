# -*- coding: utf-8 -*-
"""bid_regroup 不变量测试:分组无重叠/无遗漏、组数=⌈n/k⌉、编号体系配对。

历史 bug(memory/feedback_bid_page_budget_and_fingerprint.md):
    chunk() 末尾写成 min(i+size, n)+1 → 每组多吃 1 条 → 组间重叠、条目重复,
    而当时只校验了"组数",没校验"无重叠无遗漏",漏到产物里才发现。
"""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("bid_regroup", SCRIPTS / "bid_regroup.py")
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)


def test_chunk_no_overlap_no_gap():
    for n in range(1, 30):
        for size in (2, 3, 4):
            groups = br.chunk(list(range(n)), size)
            used = [i for g in groups for i in g]
            assert sorted(used) == list(range(1, n + 1)), f"n={n} size={size} 有重叠/遗漏: {groups}"
            assert len(groups) == (n + size - 1) // size, f"n={n} size={size} 组数不对: {groups}"


def test_group_size_by_variant_index():
    assert br.group_size_for(0) == 1        # 第 1 家平铺
    assert br.group_size_for(1) == 2        # 第 2 家每 2 条并 1 节
    assert br.group_size_for(2) == 3        # 第 3 家每 3 条并 1 节
    assert br.group_size_for(3) == 2        # 之后 2/3 轮转
    assert br.group_size_for(4) == 3


def test_numbering_styles_paired():
    # 组号与条目号必须成对且三家不同(反串标:编号体系也是指纹之一)
    assert br.head_group(0, 1, "07") == "7.1 "
    assert br.head_group(1, 1, "07") == "一、"
    assert br.head_group(2, 1, "07") == "（一）"
    assert br.head_item(0, 1, 1, "07") == "7.1.1 "
    assert br.head_item(1, 1) == "（1）"
    assert br.head_item(2, 1) == "1. "


def test_strip_num_keeps_real_leading_digits():
    # 「3号桥台施工」不能被当成内层编号剥掉
    assert br.strip_num("3号桥台施工") == "3号桥台施工"
    assert br.strip_num("1、土石方开挖") == "土石方开挖"
    assert br.strip_num("第一节 建筑工程") == "建筑工程"
    assert br.strip_num("（一）项目概况") == "项目概况"
    assert br.strip_num("7.1 建筑工程") == "建筑工程"

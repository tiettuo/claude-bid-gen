"""投标截止时间抽取回归测试。"""

from bid_facts_extract import extract_facts


class _NullLog:
    def info(self, *a, **k):
        pass

    warning = debug = error = info


def test_extract_bid_deadline_from_submission_window():
    text = """
# 第一章 比选公告

5.1  投标递交时间2026年7月16日9时00分至2026年7月16日9时30分；竞争性比选开始时间2026年7月16日9时30分。
2.5 工期要求：60日历天
"""
    facts = extract_facts(text, "招标文件.txt", _NullLog())
    assert facts["bid_deadline"]["value"] == "2026-07-16 09:30"
    assert facts["duration_days"]["value"] == 60

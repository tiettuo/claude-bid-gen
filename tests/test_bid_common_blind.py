"""测试 bid_common.BLIND_PATTERNS 的变相身份扫描。"""
import tempfile
from pathlib import Path

import pytest

from bid_common import BLIND_PATTERNS, scan_blind_bid_violations


def _scan(text: str) -> list[dict]:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
        tf.write(text)
        path = Path(tf.name)
    return scan_blind_bid_violations([path])


def test_blind_first_person():
    hits = _scan("我公司将严格按照设计要求施工。")
    assert any(h["kind"] == "第一人称自指" for h in hits)


def test_blind_unit_self():
    hits = _scan("本单位拥有市政工程一级资质。")
    assert any(h["kind"] in ("本单位自指", "我司+成就陈述") for h in hits)


def test_blind_named_role_honorific():
    hits = _scan("项目经理张三同志将驻场管理。")
    assert any(h["kind"] == "具名岗位+尊称" for h in hits)


def test_blind_resume_chen_shu():
    hits = _scan("李四曾负责类似工程的施工。")
    assert any(h["kind"] == "简历式具名陈述" for h in hits)


def test_blind_credential_number():
    hits = _scan("建造师注册编号: A0123456789")
    assert any(h["kind"] == "建造师注册编号" for h in hits)


def test_blind_credit_code():
    hits = _scan("统一社会信用代码: 91110105MA01ABCDXX")
    assert any(h["kind"] == "统一社会信用代码" for h in hits)


def test_blind_company_full_name():
    hits = _scan("由北京某建筑工程有限公司承担本项目。")
    assert any(h["kind"] == "公司全称" for h in hits)


def test_blind_no_false_positive_on_clean_text():
    """普通技术段落不应触发。"""
    text = """
项目部应按照设计图纸和规范要求,做好土方开挖与回填的施工组织。
基坑支护采用钢板桩,深度按地勘报告确定。
"""
    hits = _scan(text)
    assert len(hits) == 0


def test_blind_patterns_count():
    """确保扩展后总数 16 条(防止意外回退)"""
    assert len(BLIND_PATTERNS) == 16

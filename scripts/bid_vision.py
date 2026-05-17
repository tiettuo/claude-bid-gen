#!/usr/bin/env python3
"""
bid_vision.py - 视觉大模型 OCR 调度模块

对 bid_extract.py 提供三项能力：
1. load_config()            读取 $BID_GEN_HOME/configs/bid_vision_config.json
2. classify_page(text)      按关键词分类页面，返回命中的类别集合
3. should_use_vision(...)   判定某页是否应走视觉模型（强制类 / 扫描页+弱类）
4. vision_extract(img,...)  调用视觉模型抽取该页，主力 qwen-vl → 兜底 glm-ocr

调用规范与 bid_llm_client 保持一致：从 bid_vision_config.json 读模型池，
API Key 从环境变量获取，失败按 fallback_order 降级。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from bid_common import get_bid_gen_home as _get_bid_gen_home  # noqa: E402
_BID_GEN_HOME = _get_bid_gen_home()
CONFIG_PATH = _BID_GEN_HOME / "configs" / "bid_vision_config.json"


# ---------- 配置加载 ----------

_config_cache: dict | None = None


def load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(f"视觉配置文件不存在: {CONFIG_PATH}")
    _config_cache = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _config_cache


# ---------- 关键词分类 ----------

@dataclass
class PageClassification:
    """某页命中的关键词情况"""
    categories: set[str]           # 命中的类别名集合
    hits: dict[str, list[str]]     # 类别 -> 命中词列表（去重后）

    def is_empty(self) -> bool:
        return not self.categories


def classify_page(page_text: str, config: dict | None = None) -> PageClassification:
    """按类别关键词扫描页面文本，返回命中情况。

    每个类别支持两种字段：
    - strong_terms: 单独命中即触发该类别
    - weak_terms: 单独命中不触发；只有同页存在 strong 命中时才记入 hits
    旧字段 `terms` 视作 strong_terms，向后兼容。
    """
    cfg = config or load_config()
    keywords = cfg.get("keywords", {})
    text = page_text or ""
    if not text.strip():
        return PageClassification(categories=set(), hits={})

    cats: set[str] = set()
    hits: dict[str, list[str]] = {}
    for cat_name, cat_def in keywords.items():
        strong_terms = cat_def.get("strong_terms") or cat_def.get("terms", [])
        weak_terms = cat_def.get("weak_terms", [])

        strong_matched: list[str] = []
        for term in strong_terms:
            if term and term in text and term not in strong_matched:
                strong_matched.append(term)

        if not strong_matched:
            continue

        matched = list(strong_matched)
        for term in weak_terms:
            if term and term in text and term not in matched:
                matched.append(term)

        cats.add(cat_name)
        hits[cat_name] = matched
    return PageClassification(categories=cats, hits=hits)


def should_use_vision(
    classification: PageClassification,
    is_scanned: bool,
    config: dict | None = None,
) -> tuple[bool, str]:
    """根据分类结果 + 是否扫描页，判定是否走视觉模型

    返回 (触发, 原因字符串) —— 原因用于日志与 manifest 标注
    """
    cfg = config or load_config()
    policy = cfg.get("trigger_policy", {})
    force = set(policy.get("force_categories", []))
    scanned_only = set(policy.get("scanned_only_categories", []))

    if is_scanned:
        # 扫描页一律走视觉，原因附带关键词类别
        reason_cats = ",".join(sorted(classification.categories)) or "none"
        return True, f"scanned_page(kw:{reason_cats})"

    forced_hit = classification.categories & force
    if forced_hit:
        return True, f"force_kw:{','.join(sorted(forced_hit))}"

    # 弱类别在非扫描页不触发
    return False, ""


# ---------- 视觉模型调用 ----------

def _get_api_key(model_cfg: dict) -> str:
    key = os.environ.get(model_cfg.get("api_key_env", ""), "")
    if not key:
        alt = model_cfg.get("api_key_env_alt", "")
        if alt:
            key = os.environ.get(alt, "")
    return key


def _call_qwen_vl(
    image_bytes: bytes,
    prompt: str,
    model_cfg: dict,
    log: logging.Logger,
    timeout: int,
) -> str:
    """通义千问 VL 系列：走 OpenAI 兼容接口的 multimodal messages"""
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("  openai SDK 未安装，跳过 Qwen-VL")
        return ""

    api_key = _get_api_key(model_cfg)
    if not api_key:
        log.warning(f"  {model_cfg.get('display_name')} 未配置 API Key，跳过")
        return ""

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_uri = f"data:image/png;base64,{b64}"

    client = OpenAI(api_key=api_key, base_url=model_cfg["endpoint"], timeout=timeout)
    resp = client.chat.completions.create(
        model=model_cfg["model_name"],
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        max_tokens=model_cfg.get("max_tokens", 4000),
    )
    if resp and resp.choices:
        return (resp.choices[0].message.content or "").strip()
    return ""


def _call_glm_ocr(
    image_bytes: bytes,
    prompt: str,
    model_cfg: dict,
    log: logging.Logger,
    timeout: int,
) -> str:
    """智谱 GLM-OCR：走 zai SDK 的 layout_parsing 端点（不支持 prompt 定制，忽略）"""
    try:
        from zai import ZhipuAiClient
    except ImportError:
        log.warning("  zai SDK 未安装，跳过 GLM-OCR")
        return ""

    api_key = _get_api_key(model_cfg)
    if not api_key:
        log.warning(f"  {model_cfg.get('display_name')} 未配置 API Key，跳过")
        return ""

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_uri = f"data:image/png;base64,{b64}"

    client = ZhipuAiClient(api_key=api_key, timeout=timeout)
    resp = client.layout_parsing.create(
        model=model_cfg["model_name"],
        file=data_uri,
    )
    md = getattr(resp, "md_results", None) or ""
    return md.strip()


_DISPATCH = {
    "qwen_vl": _call_qwen_vl,
    "glm_ocr": _call_glm_ocr,
}


_TABLE_DENSE_CATEGORIES = {"scoring", "blind_format", "disqualify", "response"}
_TEXT_DENSE_CATEGORIES = {"tech_core", "key_difficult", "performance", "outline"}


def pick_preferred_model(
    categories: set[str] | None, config: dict | None = None
) -> str | None:
    """根据页面命中的关键词类别推荐主力模型。

    - 表格密集（评分/暗标/废标/响应）→ glm_ocr（SOTA 中文表格 94.6 / 实测 97%）
    - 文字段（技术核心/重难点/业绩/大纲）→ qwen_vl（速度优势）
    - 无类别 / 多类别冲突 → None，走默认 fallback_order
    """
    if not categories:
        return None
    cfg = config or load_config()
    routing = cfg.get("category_routing") or {}
    table_pref = routing.get("table_dense", "glm_ocr")
    text_pref = routing.get("text_dense", "qwen_vl")

    has_table = bool(categories & _TABLE_DENSE_CATEGORIES)
    has_text = bool(categories & _TEXT_DENSE_CATEGORIES)
    if has_table and not has_text:
        return table_pref
    if has_text and not has_table:
        return text_pref
    return None


def vision_extract(
    image_bytes: bytes,
    log: logging.Logger,
    prefer: str | None = None,
    prompt: str | None = None,
    config: dict | None = None,
    categories: set[str] | None = None,
) -> tuple[str, str]:
    """调用视觉模型抽取一页图像，返回 (markdown_text, model_used)

    prefer: 显式指定优先模型别名（覆盖一切）
    categories: 该页命中的关键词类别集合，用于按 Tier 自动选主力模型
                （表格密集 → glm_ocr，文字段 → qwen_vl）
    失败返回 ("", "")
    """
    cfg = config or load_config()
    models = cfg.get("models", {})
    order = list(cfg.get("fallback_order", ["qwen_vl", "glm_ocr"]))

    auto_prefer = pick_preferred_model(categories, cfg) if not prefer else None
    effective_prefer = prefer or auto_prefer
    if effective_prefer and effective_prefer in order:
        order.remove(effective_prefer)
        order.insert(0, effective_prefer)
        if auto_prefer and not prefer:
            log.debug(f"  [route] categories={categories} → 主力 {auto_prefer}")

    retry = cfg.get("retry", {})
    max_attempts = int(retry.get("max_attempts", 2))
    backoff = float(retry.get("backoff_seconds", 3))
    timeout = int(retry.get("timeout_seconds", 120))

    vision_prompt = prompt or cfg.get("vision_prompt", "请将这一页识别为 Markdown。")

    for model_key in order:
        model_cfg = models.get(model_key)
        if not model_cfg:
            continue
        dispatcher = _DISPATCH.get(model_key)
        if not dispatcher:
            log.warning(f"  未知视觉模型类型: {model_key}，跳过")
            continue

        for attempt in range(1, max_attempts + 1):
            try:
                text = dispatcher(image_bytes, vision_prompt, model_cfg, log, timeout)
                if text and text.strip():
                    log.info(
                        f"  视觉识别成功 [{model_cfg.get('display_name')}] "
                        f"({len(text)} 字符)"
                    )
                    return text, model_key
            except Exception as e:
                log.warning(
                    f"  视觉模型 {model_key} 第 {attempt}/{max_attempts} 次失败: {e}"
                )
            if attempt < max_attempts:
                time.sleep(backoff)
        log.warning(f"  视觉模型 {model_key} 全部重试失败，降级下一家")

    return "", ""


# ---------- 辅助：类别聚合 ----------

def summarize_hits(hits_list: Iterable[dict[str, list[str]]]) -> dict[str, int]:
    """把多页的 hits 聚合为 {类别: 命中页数}"""
    agg: dict[str, int] = {}
    for hits in hits_list:
        for cat in hits:
            agg[cat] = agg.get(cat, 0) + 1
    return agg

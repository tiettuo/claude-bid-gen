#!/usr/bin/env python3
"""
bid_charts.py - 章节 md 预处理：剥离 JSON/mermaid 代码块，渲染进度甘特图

职责（由 bid_assemble.py 在 pandoc 之前调用）：
1. 扫描每个 .md 里的围栏代码块（```json / ```mermaid / 无语言）
2. ```json 且含 tasks/milestones 字段 → matplotlib 渲染横道图 PNG，原块替换为 ![图](charts/xxx.png)
3. 其他 ```json / ```mermaid → 直接剥离，备份到 charts_data/ 以便后续重渲染
4. 写临时 md 到 <output>/VXX/.assemble_tmp/（pandoc 读这里，原 md 保持不动）

用法（通常由 bid_assemble.py 自动调用，也可独立跑）:
  python bid_charts.py <chapter_dir>          # 处理单个目录下所有 md
  python bid_charts.py <chapter_dir> --dry    # 只打印会做什么，不写盘
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import make_script_logger  # noqa: E402


# ─── 围栏代码块识别 ───────────────────────────────────────────────────

# 匹配 ```lang\n...\n``` ；lang 可空
FENCE_RE = re.compile(r"(^|\n)```([^\n`]*)\n(.*?)\n```", re.DOTALL)


@dataclass
class FenceBlock:
    """一个围栏代码块的元数据。"""
    lang: str
    content: str
    start: int      # 在原文中的起始偏移（含前导换行）
    end: int        # 在原文中的结束偏移（含结尾 ```）
    full_match: str # 原样替换用


def iter_fences(md_text: str):
    for m in FENCE_RE.finditer(md_text):
        lang = (m.group(2) or "").strip().lower()
        yield FenceBlock(
            lang=lang,
            content=m.group(3),
            start=m.start(),
            end=m.end(),
            full_match=m.group(0),
        )


# ─── JSON 类型判定 ────────────────────────────────────────────────────

_TYPE_ALIASES: dict[str, str] = {}

def _init_type_aliases():
    """构建 type 别名 → 标准类型的映射表。"""
    mappings = {
        "schedule": [
            "schedule", "gantt_chart", "gantt", "construction_schedule",
            "construction_phases_timeline", "schedule_table",
        ],
        "org_chart": [
            "org_chart", "organization_chart", "organizational_chart",
        ],
        "hierarchy_flow": ["hierarchy_flow"],
        "process_flow": [
            "process_flow", "construction_logic_flow", "diagram",
        ],
        "resource_curve": [
            "resource_curve", "line_chart", "bar_chart",
        ],
        "aoa_network": ["aoa_network"],
        "site_layout": ["site_layout"],
        "table": ["table"],
    }
    cn_keywords = {
        "schedule": ["进度", "横道", "甘特", "工期", "工序时间"],
        "org_chart": ["机构", "组织"],
        "process_flow": ["流程", "工艺"],
        "hierarchy_flow": ["体系"],
        "resource_curve": ["曲线", "劳动力投入", "资金使用"],
        "table": ["表", "清单", "汇总", "一览"],
    }
    for canonical, aliases in mappings.items():
        for alias in aliases:
            _TYPE_ALIASES[alias] = canonical
    for canonical, keywords in cn_keywords.items():
        _TYPE_ALIASES[f"__cn__{canonical}"] = (canonical, keywords)

_init_type_aliases()


def _unwrap_data(data: dict) -> dict:
    """如果数据被包在 data 字段里，解包返回合并后的 dict。"""
    inner = data.get("data")
    if inner is None:
        return data
    if isinstance(inner, dict):
        merged = dict(inner)
        for k, v in data.items():
            if k != "data" and k not in merged:
                merged[k] = v
        return merged
    if isinstance(inner, list):
        return {"_items": inner, **{k: v for k, v in data.items() if k != "data"}}
    return data


def classify_json(data) -> str:
    """判断 JSON 图表类型。返回类型标识符。"""
    if not isinstance(data, dict):
        return "other"

    t = str(data.get("type", "")).strip()

    # 1. 精确匹配（含英文别名）
    if t.lower() in _TYPE_ALIASES:
        return _TYPE_ALIASES[t.lower()]

    # 2. 中文关键词模糊匹配 type 字段
    if t:
        for key, entry in _TYPE_ALIASES.items():
            if not key.startswith("__cn__"):
                continue
            canonical, keywords = entry
            if any(kw in t for kw in keywords):
                return canonical

    # 3. 解包 data 后按结构探测
    d = _unwrap_data(data)

    if any(k in d for k in ("tasks", "milestones", "schedule")):
        return "schedule"
    items = d.get("_items") or d.get("items") or d.get("phases")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        sample = items[0]
        if any(k in sample for k in ("start", "start_day", "duration", "end", "end_day")):
            return "schedule"
    if "root" in d and isinstance(d.get("root"), dict):
        return "org_chart"
    if any(k.startswith("level_") for k in d if isinstance(k, str)):
        return "org_chart"
    content = data.get("content") or data.get("data")
    if isinstance(content, dict) and any(
            isinstance(k, str) and k.startswith("level_") for k in content):
        return "org_chart"
    if "layers" in d and "connections" in d:
        return "hierarchy_flow"
    if "nodes" in d and "edges" in d:
        return "process_flow"
    if isinstance(d.get("nodes"), list) or isinstance(d.get("steps"), list):
        return "process_flow"
    if "periods" in d and "series" in d:
        return "resource_curve"
    if isinstance(d.get("categories"), list) and isinstance(d.get("values"), list):
        return "resource_curve"

    # 4. 表格探测
    if _detect_table(data):
        return "table"

    return "other"


def _detect_table(data: dict) -> bool:
    """探测 JSON 是否为表格数据。"""
    inner = data.get("data", data)
    if isinstance(inner, dict):
        if (inner.get("headers") or inner.get("columns") or inner.get("列")) and \
           (inner.get("rows") or inner.get("行")):
            return True
    if isinstance(inner, list) and len(inner) >= 1:
        if len(inner) >= 2 and all(isinstance(r, list) for r in inner[:5]):
            return True
        if all(isinstance(r, dict) for r in inner[:5]):
            sample = inner[0]
            time_keys = {"start", "end", "start_day", "end_day", "duration",
                         "begin", "finish", "start_date", "end_date"}
            if not (time_keys & set(sample.keys())):
                return True
    if (data.get("headers") or data.get("columns") or data.get("列")) and \
       (data.get("rows") or data.get("行")):
        return True
    if "cols" in data and isinstance(data.get("data"), list):
        return True
    return False


def _parse_json_safe(content: str):
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # 容错：去掉尾随逗号
        cleaned = re.sub(r",\s*([}\]])", r"\1", content)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


# ─── 共用常量与工具 ───────────────────────────────────────────────────

# A4 版心尺寸（单位 cm），与 bid_assemble.py DEFAULT_FORMAT 一致
_PAGE_W_CM = 21.0
_PAGE_H_CM = 29.7
_MARGIN_L_CM = 2.8
_MARGIN_R_CM = 2.4
_MARGIN_T_CM = 2.6
_MARGIN_B_CM = 2.5

_CONTENT_W_CM = _PAGE_W_CM - _MARGIN_L_CM - _MARGIN_R_CM   # 15.8
_CONTENT_H_CM = _PAGE_H_CM - _MARGIN_T_CM - _MARGIN_B_CM   # 24.6
_CONTENT_W_LAND_CM = _PAGE_H_CM - _MARGIN_L_CM - _MARGIN_R_CM  # 24.5
_CONTENT_H_LAND_CM = _PAGE_W_CM - _MARGIN_T_CM - _MARGIN_B_CM  # 15.9

_CONTENT_W_IN = _CONTENT_W_CM / 2.54     # ~6.22 inches
_CONTENT_H_IN = _CONTENT_H_CM / 2.54     # ~9.69 inches
_CONTENT_W_LAND_IN = _CONTENT_W_LAND_CM / 2.54   # ~9.65 inches
_CONTENT_H_LAND_IN = _CONTENT_H_LAND_CM / 2.54   # ~6.26 inches

_RENDER_DPI = 150


def _figsize_for_content(n_items: int, item_height_in: float,
                         prefer_landscape: bool = False,
                         max_height_ratio: float = 0.9) -> tuple[float, float]:
    """根据内容量计算 matplotlib figsize，确保输出适配 A4 版心。

    返回 (width_inches, height_inches)。
    """
    if prefer_landscape:
        w = _CONTENT_W_LAND_IN
        max_h = _CONTENT_H_LAND_IN * max_height_ratio
    else:
        w = _CONTENT_W_IN
        max_h = _CONTENT_H_IN * max_height_ratio

    natural_h = max(2.5, n_items * item_height_in + 1.5)
    h = min(natural_h, max_h)
    return (w, h)


def _graphviz_size_str(prefer_landscape: bool = False) -> str:
    """返回 graphviz 的 size 属性字符串 'W,H'（单位 inches），限制输出尺寸。"""
    if prefer_landscape:
        return f"{_CONTENT_W_LAND_IN:.1f},{_CONTENT_H_LAND_IN:.1f}"
    return f"{_CONTENT_W_IN:.1f},{_CONTENT_H_IN:.1f}"


_DEFAULT_HATCHES = ["///", "...", "|||", "xxx", "\\\\\\", "+++", "---", "ooo", "***"]

_CN_FONT_CANDIDATES = (
    "PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS",
    "Noto Sans CJK SC", "Source Han Sans SC", "SimHei", "Microsoft YaHei",
)

def _setup_cn_font(plt):
    import matplotlib
    for font in _CN_FONT_CANDIDATES:
        try:
            matplotlib.font_manager.findfont(font, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [font]
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False


# ─── 资源曲线渲染 ─────────────────────────────────────────────────────

def render_resource_curve(data: dict, out_png: Path, title: str,
                          log: logging.Logger, style: dict | None = None) -> bool:
    """把 resource_curve JSON 渲染成堆叠柱状图 + 合计折线 PNG。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        log.error("matplotlib 未安装。请运行：pip3 install matplotlib")
        return False

    style = style or {}
    _setup_cn_font(plt)

    periods = data.get("periods") or []
    series_list = data.get("series") or []
    if not periods or not series_list:
        log.warning(f"  resource_curve 无有效数据，跳过 {out_png.name}")
        return False

    n = len(periods)
    hatches = style.get("resource_hatches", _DEFAULT_HATCHES)
    bar_w = style.get("resource_bar_width", 0.6)
    line_w = style.get("line_width", 1.0)
    fs_off = style.get("font_size_offset", 0)

    x = np.arange(n)
    bottoms = np.zeros(n)
    cscale = style.get("chart_scale", 1.0)
    fw, fh = _figsize_for_content(n, 0.35 * cscale, prefer_landscape=True)
    rdpi = max(100, int(_RENDER_DPI * cscale))
    fig, ax = plt.subplots(figsize=(fw, fh), dpi=rdpi)

    for i, s in enumerate(series_list):
        vals = s.get("values") or []
        if len(vals) < n:
            vals = vals + [0] * (n - len(vals))
        vals = np.array(vals[:n], dtype=float)
        h = hatches[i % len(hatches)]
        ax.bar(x, vals, bottom=bottoms, width=bar_w, label=s.get("name", f"系列{i+1}"),
               color="white", edgecolor="black", linewidth=0.6, hatch=h)
        bottoms += vals

    if data.get("show_total_line", True):
        ax.plot(x, bottoms, color="black", linewidth=line_w + 0.4,
                marker="o", markersize=3, label="合计")

    ax.set_xticks(x)
    ax.set_xticklabels(periods, fontsize=8 + fs_off, rotation=30 if n > 8 else 0)
    ax.set_ylabel(data.get("y_label", ""), fontsize=9 + fs_off)
    ax.set_xlabel(data.get("x_label", ""), fontsize=9 + fs_off)
    ax.set_title(title, fontsize=12 + fs_off, pad=10)
    ax.legend(fontsize=7 + fs_off, loc="upper right", framealpha=0.9)
    grid_style = style.get("gantt_grid", "dashed")
    if grid_style != "none":
        ax.grid(axis="y", linestyle="--" if grid_style == "dashed" else ":",
                linewidth=0.4, alpha=0.5)
    ax.set_axisbelow(True)

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    log.info(f"  ✓ 渲染资源曲线: {out_png.name}  系列={len(series_list)}  期数={n}")
    return True


# ─── 图表视觉主题（反串标：变体间颜色/填充/连线差异化） ──────────────────

def _theme_from_style(style: dict) -> dict:
    """从变体 chart_style 解出图表视觉主题。

    旧实现所有变体的流程图都白底黑框 SimHei,肉眼一个样。这里读出每变体专属的
    描边/填充色、是否填充、是否圆角、连线走线方式,在渲染器里套用。
    """
    accent = style.get("chart_accent", "#1A1A1A")
    fill = style.get("chart_node_fill", "white")
    filled = bool(style.get("chart_node_filled", False))
    rounded = bool(style.get("chart_node_rounded", False))
    spline = str(style.get("chart_edge_spline", "") or "")
    parts = []
    if rounded:
        parts.append("rounded")
    if filled:
        parts.append("filled")
    return {"accent": accent, "fill": fill, "filled": filled,
            "node_style": ",".join(parts), "spline": spline}


def _wrap_cjk(text: str, width: int = 14) -> str:
    """长标签每 width 字插入换行(graphviz \\n 转义),避免节点过宽把整图压扁。"""
    text = str(text or "")
    if len(text) <= width:
        return text
    return "\\n".join(text[i:i + width] for i in range(0, len(text), width))


def _rotate_png_if_wide(out_png: "Path", log,
                        lo: float = 2.0, hi: float = 5.5) -> None:
    """真正二维的宽图(宽/高 ∈ [lo,hi])旋转 90° 转竖向。

    竖排页面里宽图缩到页宽后会很矮很小、内容看不清;旋转成竖向后可用整页
    高度铺满,内容放大约 1.5×(阅读时把文档转 90°)。
    比值 > hi 的是单排链条/塌扁图,旋转只会变竖丝带,不旋转。
    """
    try:
        from PIL import Image
    except ImportError:
        return
    try:
        with Image.open(out_png) as im:
            w, h = im.size
            r = w / h if h else 0
            if lo <= r <= hi:
                im.rotate(-90, expand=True).save(out_png)  # 顺时针 90°
                log.info(f"  ↻ 图过宽({w}x{h}),旋转 90° 转竖向: {out_png.name}")
    except Exception as e:
        log.warning(f"  图旋转失败(非致命): {e}")


# ─── 组织机构图渲染 ───────────────────────────────────────────────────

def _add_org_node(g, node: dict, parent_id: str | None, counter: list,
                  style: dict, shuffle_rng):
    nid = f"n{counter[0]}"
    counter[0] += 1
    # 节点文字:优先 label(实际数据用这个字段),回退 role/name/title/text。
    # 旧实现只读 role/name → 数据用 label 时取不到 → 渲染成空框。
    label = str(node.get("label") or node.get("title") or node.get("text") or "").strip()
    if not label:
        role = str(node.get("role", "") or "").strip()
        name = str(node.get("name", "") or "").strip()
        label = f"{role}\\n{name}" if (role and name) else (role or name)
    label = _wrap_cjk(label, 12) or "（未命名）"
    shape = style.get("org_box_shape", "box")
    theme = _theme_from_style(style)
    sty = []
    if shape == "rounded":
        sty.append("rounded")
    if theme["filled"]:
        sty.append("filled")
    node_kw = {"shape": "box"}
    if sty:
        node_kw["style"] = ",".join(sty)
        node_kw["fillcolor"] = theme["fill"]
    if shape == "doubleframe":
        node_kw["peripheries"] = "2"
    g.node(nid, label=label, **node_kw)
    if parent_id is not None:
        g.edge(parent_id, nid)
    children = node.get("children") or []
    if children and shuffle_rng:
        indices = list(range(len(children)))
        shuffle_rng.shuffle(indices)
        children = [children[i] for i in indices]
    for ch in children:
        _add_org_node(g, ch, nid, counter, style, shuffle_rng)


def render_org_chart(data: dict, out_png: Path, title: str,
                     log: logging.Logger, style: dict | None = None) -> bool:
    """把 org_chart JSON 渲染成组织机构图 PNG。"""
    try:
        import graphviz
    except ImportError:
        log.error("graphviz 未安装。请运行：pip3 install graphviz && brew install graphviz")
        return False

    import random as _random

    style = style or {}
    root = data.get("root")
    if not root:
        log.warning(f"  org_chart 无 root 节点，跳过 {out_png.name}")
        return False

    direction = style.get("org_direction", "TB")
    line_w = str(style.get("line_width", 0.8))
    fs = str(10 + style.get("font_size_offset", 0))
    spacing = style.get("spacing_factor", 1.0)

    shuffle_seed = style.get("sibling_shuffle_seed")
    shuffle_rng = _random.Random(shuffle_seed) if shuffle_seed else None

    theme = _theme_from_style(style)
    gv_size = _graphviz_size_str(prefer_landscape=False)
    gdpi = max(100, int(_RENDER_DPI * style.get("chart_scale", 1.0)))
    g = graphviz.Digraph(format="png")
    g.attr(rankdir=direction, dpi=str(gdpi), bgcolor="white",
           size=gv_size,
           ranksep=str(round(0.6 * spacing, 2)),
           nodesep=str(round(0.4 * spacing, 2)))
    if theme["spline"]:
        g.attr(splines=theme["spline"])
    g.attr("node", fontname="SimHei", fontsize=fs,
           penwidth=line_w, margin="0.15,0.08",
           color=theme["accent"], fontcolor=theme["accent"])
    g.attr("edge", penwidth=line_w, arrowsize="0.7", color=theme["accent"])

    if title:
        g.attr(label=title, labelloc="t", fontname="SimHei", fontsize=str(int(fs) + 2))

    counter = [0]
    _add_org_node(g, root, None, counter, style, shuffle_rng)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    stem = str(out_png.with_suffix(""))
    g.render(stem, cleanup=True)
    _rotate_png_if_wide(out_png, log)
    log.info(f"  ✓ 渲染组织机构图: {out_png.name}  节点={counter[0]}")
    return True


# ─── 层级体系图渲染（质量/安全管理体系） ──────────────────────────────

def render_hierarchy_flow(data: dict, out_png: Path, title: str,
                          log: logging.Logger, style: dict | None = None) -> bool:
    """把 hierarchy_flow JSON 渲染成分层体系图 PNG。"""
    try:
        import graphviz
    except ImportError:
        log.error("graphviz 未安装")
        return False

    style = style or {}
    layers = data.get("layers") or []
    connections = data.get("connections") or []
    if not layers:
        log.warning(f"  hierarchy_flow 无 layers，跳过 {out_png.name}")
        return False

    direction = style.get("flow_direction", "TB")
    line_w = str(style.get("line_width", 0.8))
    fs = str(10 + style.get("font_size_offset", 0))
    spacing = style.get("spacing_factor", 1.0)
    theme = _theme_from_style(style)

    gv_size = _graphviz_size_str(prefer_landscape=False)
    g = graphviz.Digraph(format="png")
    gdpi = max(100, int(_RENDER_DPI * style.get("chart_scale", 1.0)))
    g.attr(rankdir=direction, dpi=str(gdpi), bgcolor="white",
           size=gv_size,
           ranksep=str(round(0.7 * spacing, 2)),
           nodesep=str(round(0.5 * spacing, 2)))
    if theme["spline"]:
        g.attr(splines=theme["spline"])
    node_kw = dict(fontname="SimHei", fontsize=fs, shape="box",
                   penwidth=line_w, margin="0.15,0.08",
                   color=theme["accent"], fontcolor=theme["accent"])
    if theme["node_style"]:
        node_kw["style"] = theme["node_style"]
        node_kw["fillcolor"] = theme["fill"]
    g.attr("node", **node_kw)
    g.attr("edge", penwidth=line_w, arrowsize="0.7", color=theme["accent"])

    if title:
        g.attr(label=title, labelloc="t", fontname="SimHei", fontsize=str(int(fs) + 2))

    node_ids = {}
    layer_first = []   # 每层一个代表节点,用于强制层间纵向排序
    for li, layer in enumerate(layers):
        first_in_layer = None
        with g.subgraph() as s:
            s.attr(rank="same")
            layer_name = layer.get("name", "")
            if layer_name:
                invis_id = f"_layer_{li}"
                s.node(invis_id, label=layer_name, shape="plaintext",
                       fontname="SimHei", fontsize=str(int(fs) - 1))
                first_in_layer = invis_id
            for node_name in (layer.get("nodes") or []):
                nid = f"L{li}_{node_name}"
                s.node(nid, label=_wrap_cjk(node_name))
                node_ids[node_name] = nid
                if first_in_layer is None:
                    first_in_layer = nid
        layer_first.append(first_in_layer)

    # 强制层间纵向堆叠:相邻层代表节点连不可见边。否则 connections 缺失或
    # 格式不符时,各层 rank=same 子图会塌成一行(渲染出 929x20 的横丝带)。
    for a, b in zip(layer_first, layer_first[1:]):
        if a and b:
            g.edge(a, b, style="invis")

    def _resolve_node(name):
        """连接端点名 → 节点 id;端点常用简称,支持精确 + 子串匹配。"""
        name = str(name or "").strip()
        if not name:
            return None
        if name in node_ids:
            return node_ids[name]
        for k, v in node_ids.items():
            if name in k or k in name:
                return v
        return None

    for conn in connections:
        # connections 兼容两种格式:{"from","to","label"} 或 ["from","to"]
        if isinstance(conn, dict):
            fn, tn, lbl = conn.get("from", ""), conn.get("to", ""), conn.get("label", "")
        elif isinstance(conn, (list, tuple)) and len(conn) >= 2:
            fn, tn = conn[0], conn[1]
            lbl = conn[2] if len(conn) > 2 else ""
        else:
            continue
        f, t = _resolve_node(fn), _resolve_node(tn)
        if f and t:
            g.edge(f, t, label=str(lbl), fontname="SimHei", fontsize=str(int(fs) - 2))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    stem = str(out_png.with_suffix(""))
    g.render(stem, cleanup=True)
    _rotate_png_if_wide(out_png, log)
    log.info(f"  ✓ 渲染层级体系图: {out_png.name}  层数={len(layers)}")
    return True


# ─── 工艺流程图渲染 ───────────────────────────────────────────────────

def render_process_flow(data: dict, out_png: Path, title: str,
                        log: logging.Logger, style: dict | None = None) -> bool:
    """把 process_flow JSON 渲染成工艺流程图 PNG。"""
    try:
        import graphviz
    except ImportError:
        log.error("graphviz 未安装")
        return False

    style = style or {}
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    if not nodes:
        log.warning(f"  process_flow 无 nodes，跳过 {out_png.name}")
        return False

    default_dir = "LR" if len(nodes) >= 4 else "TB"
    direction = style.get("flow_direction", default_dir)
    line_w = str(style.get("line_width", 0.8))
    fs = str(10 + style.get("font_size_offset", 0))
    spacing = style.get("spacing_factor", 1.0)

    shape_map = {"box": "box", "diamond": "diamond", "oval": "ellipse",
                 "start": "ellipse", "end": "ellipse", "process": "box",
                 "decision": "diamond"}

    theme = _theme_from_style(style)
    prefer_land = direction == "LR"
    gv_size = _graphviz_size_str(prefer_landscape=prefer_land)
    g = graphviz.Digraph(format="png")
    gdpi = max(100, int(_RENDER_DPI * style.get("chart_scale", 1.0)))
    g.attr(rankdir=direction, dpi=str(gdpi), bgcolor="white",
           size=gv_size,
           ranksep=str(round(0.6 * spacing, 2)),
           nodesep=str(round(0.4 * spacing, 2)))
    if theme["spline"]:
        g.attr(splines=theme["spline"])
    node_kw = dict(fontname="SimHei", fontsize=fs, penwidth=line_w,
                   margin="0.12,0.06",
                   color=theme["accent"], fontcolor=theme["accent"])
    if theme["node_style"]:
        node_kw["style"] = theme["node_style"]
        node_kw["fillcolor"] = theme["fill"]
    g.attr("node", **node_kw)
    g.attr("edge", fontname="SimHei", fontsize=str(int(fs) - 2),
           penwidth=line_w, arrowsize="0.7", color=theme["accent"])

    if title:
        g.attr(label=title, labelloc="t", fontname="SimHei", fontsize=str(int(fs) + 2))

    for nd in nodes:
        nid = str(nd.get("id", ""))
        label = str(nd.get("label", nid))
        shape = shape_map.get(nd.get("shape", "box"), "box")
        g.node(nid, label=_wrap_cjk(label, 16), shape=shape)

    for ed in edges:
        g.edge(str(ed.get("from", "")), str(ed.get("to", "")), label=str(ed.get("label", "")))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    stem = str(out_png.with_suffix(""))
    g.render(stem, cleanup=True)
    _rotate_png_if_wide(out_png, log)
    log.info(f"  ✓ 渲染工艺流程图: {out_png.name}  节点={len(nodes)}")
    return True


# ─── 双代号网络图渲染（AOA / CPM） ───────────────────────────────────

def _topo_layers(events: list[int], activities: list[dict]) -> dict[int, int]:
    """拓扑排序，返回 {event_id: layer}。layer 0 = 起始事件。"""
    from collections import defaultdict, deque
    adj: dict[int, list[int]] = defaultdict(list)
    indeg: dict[int, int] = {e: 0 for e in events}
    for a in activities:
        adj[a["from"]].append(a["to"])
        indeg.setdefault(a["to"], 0)
        indeg[a["to"]] = indeg.get(a["to"], 0) + 1
    q = deque(e for e in events if indeg.get(e, 0) == 0)
    layer = {}
    while q:
        n = q.popleft()
        if n not in layer:
            layer[n] = 0
        for nb in adj[n]:
            layer[nb] = max(layer.get(nb, 0), layer[n] + 1)
            indeg[nb] -= 1
            if indeg[nb] == 0:
                q.append(nb)
    return layer


def _critical_path(events: list[int], activities: list[dict]) -> set[tuple[int, int]]:
    """正推+反推算关键路径，返回关键活动的 (from, to) 集合。"""
    from collections import defaultdict
    fwd: dict[int, list[dict]] = defaultdict(list)
    bwd: dict[int, list[dict]] = defaultdict(list)
    for a in activities:
        fwd[a["from"]].append(a)
        bwd[a["to"]].append(a)
    es = {e: 0 for e in events}
    topo = sorted(events, key=lambda e: _topo_layers(events, activities).get(e, 0))
    for n in topo:
        for a in fwd[n]:
            es[a["to"]] = max(es[a["to"]], es[n] + a.get("duration", 0))
    total = max(es.values()) if es else 0
    lf = {e: total for e in events}
    for n in reversed(topo):
        for a in fwd[n]:
            lf[n] = min(lf[n], lf[a["to"]] - a.get("duration", 0))
    crit = set()
    for a in activities:
        if es[a["from"]] == lf[a["from"]] and es[a["to"]] == lf[a["to"]]:
            if es[a["to"]] - es[a["from"]] == a.get("duration", 0):
                crit.add((a["from"], a["to"]))
    return crit


def render_aoa_network(data: dict, out_png: Path, title: str,
                       log: logging.Logger, style: dict | None = None) -> bool:
    """把 aoa_network JSON 渲染成双代号网络图 PNG。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyArrowPatch, Circle
    except ImportError:
        log.error("matplotlib 未安装")
        return False

    style = style or {}
    _setup_cn_font(plt)

    activities = data.get("activities") or []
    event_set = set()
    for a in activities:
        event_set.add(a["from"])
        event_set.add(a["to"])
    events_from_data = data.get("events") or sorted(event_set)
    events = sorted(set(events_from_data) | event_set)

    if not activities or len(events) < 2:
        log.warning(f"  aoa_network 数据不足，跳过 {out_png.name}")
        return False

    layers = _topo_layers(events, activities)
    crit = _critical_path(events, activities)

    node_r = style.get("network_node_radius", 18)
    label_pos = style.get("network_label_pos", "above")
    line_w = style.get("line_width", 0.8)
    fs_off = style.get("font_size_offset", 0)
    spacing = style.get("spacing_factor", 1.0)

    from collections import defaultdict
    layer_groups: dict[int, list[int]] = defaultdict(list)
    for e in events:
        layer_groups[layers.get(e, 0)].append(e)

    max_layer = max(layers.values()) if layers else 0
    max_group = max(len(g) for g in layer_groups.values()) if layer_groups else 1

    x_gap = 120 * spacing
    y_gap = 80 * spacing
    pos: dict[int, tuple[float, float]] = {}
    for lay, group in layer_groups.items():
        group.sort()
        n = len(group)
        for i, e in enumerate(group):
            x = lay * x_gap
            y = (i - (n - 1) / 2) * y_gap
            pos[e] = (x, y)

    fw, fh = _figsize_for_content(max_group, 1.2 * spacing, prefer_landscape=True)
    fig, ax = plt.subplots(figsize=(fw, fh), dpi=_RENDER_DPI)
    ax.set_aspect("equal")
    ax.axis("off")

    if title:
        ax.set_title(title, fontsize=12 + fs_off, pad=15)

    r_plot = node_r * 0.012

    for a in activities:
        x0, y0 = pos.get(a["from"], (0, 0))
        x1, y1 = pos.get(a["to"], (0, 0))
        is_crit = (a["from"], a["to"]) in crit
        is_dummy = a.get("dummy", False) or a.get("duration", 1) == 0 and not a.get("name")
        lw = (line_w + 0.8) if is_crit else line_w
        ls = ":" if is_dummy else "-"

        dx = x1 - x0
        dy = y1 - y0
        dist = (dx**2 + dy**2)**0.5 or 1
        ux, uy = dx / dist, dy / dist
        sx = x0 + ux * r_plot * 60
        sy = y0 + uy * r_plot * 60
        ex = x1 - ux * r_plot * 60
        ey = y1 - uy * r_plot * 60

        ax.annotate("", xy=(ex, ey), xytext=(sx, sy),
                    arrowprops=dict(arrowstyle="-|>", lw=lw, linestyle=ls,
                                    color="black", shrinkA=0, shrinkB=0))

        mx = (sx + ex) / 2
        my = (sy + ey) / 2
        name = a.get("name", "")
        dur = a.get("duration", "")
        offset_y = 8 if label_pos == "above" else -8
        if name:
            ax.annotate(name, (mx, my), textcoords="offset points",
                        xytext=(0, offset_y), fontsize=8 + fs_off,
                        ha="center", va="bottom" if label_pos == "above" else "top")
        if dur and not is_dummy:
            ax.annotate(str(dur), (mx, my), textcoords="offset points",
                        xytext=(0, -offset_y), fontsize=7 + fs_off,
                        ha="center", va="top" if label_pos == "above" else "bottom",
                        color="#333333")

    for e in events:
        x, y = pos[e]
        circle = Circle((x, y), r_plot * 55, fill=True, facecolor="white",
                         edgecolor="black", linewidth=line_w + 0.2, zorder=5)
        ax.add_patch(circle)
        ax.text(x, y, str(e), ha="center", va="center",
                fontsize=10 + fs_off, fontweight="bold", zorder=6)

    all_x = [p[0] for p in pos.values()]
    all_y = [p[1] for p in pos.values()]
    margin = x_gap * 0.4
    ax.set_xlim(min(all_x) - margin, max(all_x) + margin)
    ax.set_ylim(min(all_y) - margin, max(all_y) + margin)

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    log.info(f"  ✓ 渲染双代号网络图: {out_png.name}  事件={len(events)}  活动={len(activities)}  关键路径={len(crit)}条")
    return True


# ─── 施工平面布置图渲染（模板方案） ──────────────────────────────────

_SITE_TEMPLATES = {
    "rectangular_center": {
        "zones": {
            "north": {"x": 10, "y": 70, "w": 80, "h": 15},
            "south": {"x": 10, "y": 5, "w": 80, "h": 15},
            "east":  {"x": 75, "y": 25, "w": 20, "h": 40},
            "west":  {"x": 5, "y": 25, "w": 20, "h": 40},
        },
        "building": {"x": 30, "y": 30, "w": 35, "h": 30},
        "gate": {"x": 5, "y": 2, "w": 8, "h": 4},
        "road_spine": [(5, 22), (95, 22)],
        "fence": [(2, 2), (98, 2), (98, 90), (2, 90), (2, 2)],
    },
    "L_shape": {
        "zones": {
            "north": {"x": 10, "y": 70, "w": 50, "h": 15},
            "south": {"x": 10, "y": 5, "w": 80, "h": 15},
            "east":  {"x": 65, "y": 25, "w": 25, "h": 55},
            "west":  {"x": 5, "y": 25, "w": 20, "h": 40},
        },
        "building": {"x": 25, "y": 30, "w": 35, "h": 35},
        "gate": {"x": 10, "y": 2, "w": 8, "h": 4},
        "road_spine": [(10, 22), (90, 22)],
        "fence": [(2, 2), (98, 2), (98, 90), (65, 90), (65, 60), (2, 60), (2, 2)],
    },
    "U_shape": {
        "zones": {
            "north": {"x": 25, "y": 72, "w": 50, "h": 15},
            "south": {"x": 10, "y": 5, "w": 80, "h": 15},
            "east":  {"x": 75, "y": 25, "w": 18, "h": 45},
            "west":  {"x": 5, "y": 25, "w": 18, "h": 45},
        },
        "building": {"x": 28, "y": 28, "w": 40, "h": 38},
        "gate": {"x": 40, "y": 2, "w": 8, "h": 4},
        "road_spine": [(40, 22), (95, 22)],
        "fence": [(2, 2), (98, 2), (98, 90), (2, 90), (2, 2)],
    },
}

_ZONE_HATCHES = {"north": "///", "south": "...", "east": "|||", "west": "xxx",
                 "northeast": "\\\\\\", "northwest": "+++", "center": ""}

_ZONE_NAMES_CN = {"north": "北区", "south": "南区", "east": "东区", "west": "西区",
                  "northeast": "东北区", "northwest": "西北区", "center": "中央区"}


def _wrap_text(text: str, max_chars: int) -> str:
    """中文文本按字符数硬换行,但保护"数字+单位"不被切开。

    例:"供水池 5m³" max_chars=4 → ["供水池", "5m³"](不会切成"供水池 5"和"m³")
    """
    if not text or len(text) <= max_chars:
        return text
    # 优先在空格处断行
    if " " in text:
        words = text.split(" ")
        lines: list[str] = []
        cur = ""
        for w in words:
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= max_chars:
                cur = f"{cur} {w}"
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return "\n".join(lines)
    # 无空格则按字符硬切
    return "\n".join(text[i:i + max_chars] for i in range(0, len(text), max_chars))


def render_site_layout(data: dict, out_png: Path, title: str,
                       log: logging.Logger, style: dict | None = None) -> bool:
    """把 site_layout JSON 渲染成专业施工总平面布置图 PNG。

    新版要素(2026-05-04 升级):
    - 图签栏(右下):图名/比例/图号/日期
    - 图例框(左下):4 种填充图案对应北/南/东/西区
    - 比例尺(底部中央)
    - 指北针(右上,带圆环)
    - 主建筑显示名称 + 面积(若提供)
    - 设施名按格子尺寸自动换行,不溢出
    - 围墙加粗 + 大门用缺口标识
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle, Circle, Polygon
    except ImportError:
        log.error("matplotlib 未安装")
        return False

    style = style or {}
    _setup_cn_font(plt)

    template_name = data.get("template", "rectangular_center")
    tpl = _SITE_TEMPLATES.get(template_name, _SITE_TEMPLATES["rectangular_center"])

    line_w = style.get("line_width", 0.8)
    fs_off = style.get("font_size_offset", 0)

    # 画布布局:
    #   正图区          0~95 高
    #   比例尺通道      -4 ~ -1 (3 单位)
    #   图例 / 图签栏   -16 ~ -5 (并排在底部,11 单位高)
    fw, fh = _figsize_for_content(8, 1.15, prefer_landscape=True)
    fig, ax = plt.subplots(figsize=(fw, fh), dpi=_RENDER_DPI)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_xlim(-3, 103)
    ax.set_ylim(-18, 96)

    # 图标题(顶部)
    if title:
        ax.text(50, 94, title, ha="center", va="top",
                fontsize=12 + fs_off, fontweight="bold")

    # ── 围墙 ────────────────────────────────────────
    fence_pts = tpl.get("fence", [])
    if fence_pts:
        xs = [p[0] for p in fence_pts]
        ys = [p[1] for p in fence_pts]
        ax.plot(xs, ys, color="black", linewidth=line_w + 1.0,
                linestyle="-", solid_capstyle="round", zorder=2)

    # ── 各功能区设施(先画区,再画建筑) ─────────────
    zones_data = data.get("zones") or {}
    used_zones: list[str] = []  # 用于图例,顺序保留

    for zone_name, zone_rect in tpl["zones"].items():
        items = zones_data.get(zone_name, [])
        if not items:
            continue
        used_zones.append(zone_name)
        zx, zy, zw, zh = zone_rect["x"], zone_rect["y"], zone_rect["w"], zone_rect["h"]
        n = len(items)

        # 整个 zone 用统一填充图案做背景(图例对应)
        bg = Rectangle((zx, zy), zw, zh, linewidth=line_w * 0.8,
                       edgecolor="#666666", facecolor="white",
                       hatch=_ZONE_HATCHES.get(zone_name, ""),
                       zorder=1)
        ax.add_patch(bg)

        # 切格子放设施名
        sub_w = zw / max(n, 1)
        for i, item_label in enumerate(items):
            ix = zx + i * sub_w + 0.5
            iw = sub_w - 1
            iy = zy + 0.5
            ih = zh - 1
            r = Rectangle((ix, iy), iw, ih, linewidth=line_w,
                          edgecolor="black", facecolor="white", zorder=2)
            ax.add_patch(r)
            # 文字按格子宽度自动换行(中文每字 ~1.0 单位宽)
            wrap_chars = max(2, int(iw / 1.6))
            wrapped = _wrap_text(str(item_label), wrap_chars)
            ax.text(ix + iw / 2, iy + ih / 2, wrapped,
                    ha="center", va="center", fontsize=7 + fs_off, zorder=3)

    # ── 主体建筑 ────────────────────────────────────
    b = tpl["building"]
    main_label = data.get("main_building", "主体建筑")
    main_area = data.get("main_building_area") or data.get("main_area") or ""
    main_type = data.get("main_building_type") or data.get("main_type") or ""

    rect = Rectangle((b["x"], b["y"]), b["w"], b["h"], linewidth=line_w + 1.2,
                     edgecolor="black", facecolor="#DDDDDD", zorder=4)
    ax.add_patch(rect)

    # 主建筑文字(主名 + 副信息)
    cy_text = b["y"] + b["h"] / 2
    if main_type or main_area:
        ax.text(b["x"] + b["w"] / 2, cy_text + 2.5, main_label,
                ha="center", va="center", fontsize=10 + fs_off,
                fontweight="bold", zorder=5)
        sub = " ".join(s for s in (main_type, main_area) if s)
        ax.text(b["x"] + b["w"] / 2, cy_text - 2.5, sub,
                ha="center", va="center", fontsize=8 + fs_off, zorder=5)
    else:
        ax.text(b["x"] + b["w"] / 2, cy_text, main_label,
                ha="center", va="center", fontsize=10 + fs_off,
                fontweight="bold", zorder=5)

    # ── 施工道路 ────────────────────────────────────
    road = tpl.get("road_spine", [])
    if road:
        rx = [p[0] for p in road]
        ry = [p[1] for p in road]
        # 双线表达硬化路面
        ax.plot(rx, ry, color="black", linewidth=line_w * 4, alpha=0.15, zorder=1)
        ax.plot(rx, ry, color="black", linewidth=line_w * 0.5,
                linestyle="--", zorder=2, dashes=(4, 3))

    # ── 大门(用围墙缺口 + 折线表达) ────────────────
    gt = tpl.get("gate", {})
    if gt:
        gx, gy, gw, gh = gt["x"], gt["y"], gt["w"], gt["h"]
        # 用白色矩形遮挡围墙(模拟开口)
        ax.add_patch(Rectangle((gx, gy - 0.3), gw, gh + 0.6,
                               linewidth=0, edgecolor="none",
                               facecolor="white", zorder=3))
        # 两侧门柱
        ax.plot([gx, gx], [gy, gy + gh], color="black",
                linewidth=line_w + 0.8, zorder=4)
        ax.plot([gx + gw, gx + gw], [gy, gy + gh], color="black",
                linewidth=line_w + 0.8, zorder=4)
        # 门标识
        ax.text(gx + gw / 2, gy + gh + 1.5, "施工大门",
                ha="center", va="bottom", fontsize=7 + fs_off,
                fontweight="bold", zorder=4)

    # ── 塔吊/汽车吊回转半径 ───────────────────────
    cranes = data.get("cranes") or []
    n_cranes = len(cranes)
    for idx, cr in enumerate(cranes):
        pos_hint = str(cr.get("position", "northeast_of_building"))
        bld = tpl["building"]
        # 默认放在主建筑内部偏上,这样回转圈不会冲出北区设施
        cx = bld["x"] + bld["w"] * 0.75
        cy = bld["y"] + bld["h"] * 0.65
        if "northwest" in pos_hint:
            cx = bld["x"] + bld["w"] * 0.25
            cy = bld["y"] + bld["h"] * 0.65
        elif "southeast" in pos_hint:
            cx = bld["x"] + bld["w"] * 0.75
            cy = bld["y"] + bld["h"] * 0.35
        elif "southwest" in pos_hint:
            cx = bld["x"] + bld["w"] * 0.25
            cy = bld["y"] + bld["h"] * 0.35
        elif "south" in pos_hint:
            cy = bld["y"] + bld["h"] * 0.3
        if n_cranes > 1:
            cx += (idx - (n_cranes - 1) / 2) * 6
        radius = cr.get("radius", 16)
        # 回转半径警戒圈
        circle = Circle((cx, cy), radius, fill=False, edgecolor="black",
                        linewidth=line_w, linestyle="--", zorder=3)
        ax.add_patch(circle)
        # 塔吊本体(小圆+十字)
        ax.add_patch(Circle((cx, cy), 1.5, facecolor="black",
                            edgecolor="black", zorder=5))
        ax.plot([cx - 2.5, cx + 2.5], [cy, cy], color="black",
                linewidth=line_w + 0.5, zorder=5)
        ax.plot([cx, cx], [cy - 2.5, cy + 2.5], color="black",
                linewidth=line_w + 0.5, zorder=5)
        # 标签放塔吊本体右下,贴近主建筑内,避免穿到北区
        label = cr.get("label", "塔吊")
        ax.text(cx + 3, cy - 1, label, ha="left", va="top",
                fontsize=7 + fs_off, fontweight="bold", zorder=5)

    # ── 指北针(右上角,带圆环) ───────────────────
    n_cx, n_cy, n_r = 93, 80, 4
    ax.add_patch(Circle((n_cx, n_cy), n_r, fill=False,
                        edgecolor="black", linewidth=line_w + 0.3, zorder=4))
    # N 箭头(实心三角)
    arrow = Polygon([(n_cx, n_cy + n_r - 0.5), (n_cx - 1.2, n_cy),
                     (n_cx + 1.2, n_cy)],
                    closed=True, facecolor="black", edgecolor="black", zorder=5)
    ax.add_patch(arrow)
    # S 端用空心三角
    arrow_s = Polygon([(n_cx, n_cy - n_r + 0.5), (n_cx - 1.2, n_cy),
                       (n_cx + 1.2, n_cy)],
                      closed=True, facecolor="white", edgecolor="black", zorder=5)
    ax.add_patch(arrow_s)
    ax.text(n_cx, n_cy + n_r + 0.8, "N", ha="center", va="bottom",
            fontsize=10 + fs_off, fontweight="bold", zorder=5)

    # ── 比例尺(图例与图签上方的横向通道 y=-4~-1) ─────────
    scale_str = str(data.get("scale") or "1:200")
    sx0, sy0 = 38, -2.6  # 标尺主体 y=-2.6 高 0.7
    seg = 5  # 单段图上长度
    for i in range(4):
        x0 = sx0 + i * seg
        ax.add_patch(Rectangle((x0, sy0), seg, 0.7,
                               linewidth=line_w * 0.8,
                               edgecolor="black",
                               facecolor="black" if i % 2 == 0 else "white",
                               zorder=4))
    # 比例尺刻度小竖线
    for i in range(5):
        x0 = sx0 + i * seg
        ax.plot([x0, x0], [sy0 - 0.3, sy0 + 1.0], color="black",
                linewidth=line_w * 0.6, zorder=5)
    # 刻度值
    try:
        ratio = int(str(scale_str).split(":")[1]) if ":" in scale_str else 200
    except (ValueError, IndexError):
        ratio = 200
    step_m = max(1, int(seg * ratio / 100))
    for i in range(5):
        ax.text(sx0 + i * seg, sy0 + 1.2, f"{i * step_m}m",
                ha="center", va="bottom", fontsize=6 + fs_off, zorder=5)
    # 比例尺标题放在标尺左侧(避免遮挡刻度数字)
    ax.text(sx0 - 1.5, sy0 + 0.35, f"比例尺  {scale_str}",
            ha="right", va="center", fontsize=7 + fs_off,
            fontweight="bold", zorder=5)

    # ── 图例框(左下) ──────────────────────────
    if used_zones:
        leg_x, leg_y = 2, -16
        leg_w, leg_h = 32, 11
        ax.add_patch(Rectangle((leg_x, leg_y), leg_w, leg_h,
                               linewidth=line_w + 0.3,
                               edgecolor="black",
                               facecolor="white", zorder=3))
        ax.text(leg_x + leg_w / 2, leg_y + leg_h - 0.8, "图  例",
                ha="center", va="top", fontsize=8 + fs_off,
                fontweight="bold", zorder=4)
        # 内部条目
        item_h = (leg_h - 2.5) / max(len(used_zones), 1)
        for i, zn in enumerate(used_zones):
            ix = leg_x + 1.5
            iy = leg_y + leg_h - 2.5 - (i + 0.7) * item_h
            # 填充示意小方块
            ax.add_patch(Rectangle((ix, iy), 4, item_h * 0.6,
                                   linewidth=line_w * 0.7,
                                   edgecolor="black", facecolor="white",
                                   hatch=_ZONE_HATCHES.get(zn, ""),
                                   zorder=4))
            label = _ZONE_NAMES_CN.get(zn, zn)
            ax.text(ix + 5, iy + item_h * 0.3, label,
                    ha="left", va="center", fontsize=7 + fs_off, zorder=4)

    # ── 图签栏(右下) ──────────────────────────
    sig_x, sig_y = 60, -16
    sig_w, sig_h = 38, 11
    ax.add_patch(Rectangle((sig_x, sig_y), sig_w, sig_h,
                           linewidth=line_w + 0.5,
                           edgecolor="black", facecolor="white", zorder=3))

    # 内部分行
    drawing_name = str(data.get("drawing_name")
                       or data.get("title")
                       or title
                       or "施工总平面布置图")
    drawing_no = str(data.get("drawing_no") or "PJ-S-01")
    drawn_date = str(data.get("date")
                     or data.get("drawn_date")
                     or datetime.now().strftime("%Y-%m"))
    rows = [
        ("图  名", drawing_name),
        ("比  例", scale_str),
        ("图  号", drawing_no),
        ("绘制日期", drawn_date),
    ]
    row_h = sig_h / len(rows)
    for i, (k, v) in enumerate(rows):
        y_top = sig_y + sig_h - i * row_h
        # 行分隔线
        if i > 0:
            ax.plot([sig_x, sig_x + sig_w], [y_top, y_top],
                    color="black", linewidth=line_w * 0.6, zorder=4)
        # 列分隔线(键|值约 1:3)
        col_split = sig_x + sig_w * 0.28
        if i == 0:
            ax.plot([col_split, col_split], [sig_y, sig_y + sig_h],
                    color="black", linewidth=line_w * 0.6, zorder=4)
        # 键
        ax.text(sig_x + sig_w * 0.14, y_top - row_h / 2, k,
                ha="center", va="center", fontsize=7 + fs_off, zorder=4)
        # 值
        max_chars = 18
        v_show = v if len(v) <= max_chars else v[: max_chars - 1] + "…"
        ax.text(col_split + (sig_x + sig_w - col_split) / 2,
                y_top - row_h / 2, v_show,
                ha="center", va="center", fontsize=7 + fs_off, zorder=4)

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    log.info(f"  ✓ 渲染施工平面布置图: {out_png.name}  "
             f"模板={template_name}  区域={len(used_zones)}  塔吊={len(cranes)}")
    return True


# ─── 甘特图渲染 ───────────────────────────────────────────────────────

_FALLBACK_GANTT_GRAYS = ["#000000", "#333333", "#555555", "#777777", "#999999", "#BBBBBB"]


_NAME_STRIP_RE = re.compile(r"[=★☆⭐\ufe0f]+")


def _clean_label(s):
    if not s:
        return s
    return _NAME_STRIP_RE.sub("", str(s)).strip()


def _parse_date(s):
    """解析绝对日期字符串。不支持数字（数字由 _resolve_day 处理为相对日偏移）。"""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return None  # 数字交给调用方按相对日处理
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # 纯数字字符串也当相对日交给上游
    return None


def _as_day_offset(v):
    """尝试把值解析为相对日偏移（整数，D1 起算）。返回 None 表示不是相对日。"""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if re.fullmatch(r"[Dd]?\d+", s):
        return int(re.sub(r"^[Dd]", "", s))
    return None


def _resolve_baseline(data: dict):
    """从 JSON 里推断基准日（D1 对应的绝对日期）。找不到就用今天。"""
    proj = data.get("project") or {}
    for key in ("start_date", "baseline_date", "baseline", "kickoff"):
        d = _parse_date(proj.get(key))
        if d:
            return d
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def render_schedule_png(data: dict, out_png: Path, title: str,
                        log: logging.Logger, style: dict | None = None) -> bool:
    """把 schedule JSON 渲染成横道图 PNG。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
        import matplotlib.dates as mdates
    except ImportError:
        log.error("matplotlib 未安装。请运行：pip3 install matplotlib")
        return False

    style = style or {}
    _setup_cn_font(plt)

    tasks = data.get("tasks") or data.get("schedule") or []
    milestones = data.get("milestones") or []
    baseline = _resolve_baseline(data)
    total_days = (data.get("project") or {}).get("total_duration_days")

    def _resolve_dt(val_date, val_day):
        """优先绝对日期，其次相对日偏移；都没有返回 None。"""
        d = _parse_date(val_date)
        if d is not None:
            return d
        off = _as_day_offset(val_day if val_day is not None else val_date)
        if off is not None:
            return baseline + timedelta(days=off - 1)  # D1 对应 baseline
        return None

    parsed_tasks = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        start_raw = t.get("start") if t.get("start") is not None else t.get("start_date") or t.get("begin") or t.get("start_day")
        end_raw = t.get("end") if t.get("end") is not None else t.get("end_date") or t.get("finish") or t.get("finish_day")
        start = _resolve_dt(start_raw, start_raw)
        end = _resolve_dt(end_raw, end_raw)
        if start is None or end is None or end < start:
            continue
        parsed_tasks.append({
            "name": _clean_label(t.get("name") or t.get("task") or "(未命名)"),
            "start": start,
            "end": end,
            "phase": t.get("phase") or t.get("stage") or ("关键线路" if t.get("critical") else ""),
        })

    if not parsed_tasks:
        log.warning(f"  schedule JSON 无有效 tasks，跳过渲染 {out_png.name}")
        return False

    # 分配灰度色（暗标只能黑白，用灰度梯度 + hatch 区分）
    gantt_grays = style.get("gantt_grays") or _FALLBACK_GANTT_GRAYS
    bar_hatch = style.get("gantt_bar_hatch", "")
    ms_linestyle = style.get("gantt_milestone_style", ":")

    phases = []
    for t in parsed_tasks:
        if t["phase"] and t["phase"] not in phases:
            phases.append(t["phase"])
    phase_color = {ph: gantt_grays[i % len(gantt_grays)] for i, ph in enumerate(phases)}
    default_color = gantt_grays[0]

    parsed_tasks = list(reversed(parsed_tasks))

    bar_h = style.get("gantt_bar_height", 0.55)
    date_fmt = style.get("gantt_date_fmt", "%Y-%m")
    grid_style = style.get("gantt_grid", "dashed")
    line_w = style.get("line_width", 0.8)
    fs_off = style.get("font_size_offset", 0)

    n = len(parsed_tasks)
    cscale = style.get("chart_scale", 1.0)
    fw, fh = _figsize_for_content(n, 0.38 * cscale, prefer_landscape=True)
    rdpi = max(100, int(_RENDER_DPI * cscale))
    fig, ax = plt.subplots(figsize=(fw, fh), dpi=rdpi)

    for i, t in enumerate(parsed_tasks):
        color = phase_color.get(t["phase"], default_color)
        if not phases:
            color = gantt_grays[i % len(gantt_grays)]
        dur = (t["end"] - t["start"]).days or 1
        bar_kw = dict(height=bar_h, color=color, edgecolor="#333333", linewidth=line_w * 0.6)
        if bar_hatch:
            bar_kw["hatch"] = bar_hatch
        ax.barh(i, dur, left=mdates.date2num(t["start"]), **bar_kw)

    ax.set_yticks(range(n))
    ax.set_yticklabels([t["name"] for t in parsed_tasks], fontsize=9 + fs_off)
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
    ax.tick_params(axis="x", labelsize=8 + fs_off, rotation=30)
    if grid_style != "none":
        ax.grid(axis="x", linestyle="--" if grid_style == "dashed" else ":",
                linewidth=0.4, alpha=0.5)
    ax.set_axisbelow(True)
    ax.set_title(title, fontsize=12 + fs_off, pad=10)

    # milestones：竖虚线 + 标注
    for ms in milestones:
        if not isinstance(ms, dict):
            continue
        date_raw = ms.get("date") or ms.get("deadline")
        day_raw = ms.get("day") if ms.get("day") is not None else ms.get("day_offset")
        d = _resolve_dt(date_raw, day_raw if day_raw is not None else date_raw)
        if d is None:
            continue
        ax.axvline(mdates.date2num(d), color="#333333", linestyle=ms_linestyle, linewidth=0.9, alpha=0.8)
        ax.annotate(
            _clean_label(ms.get("name", "")),
            xy=(mdates.date2num(d), n - 0.5),
            xytext=(2, -6), textcoords="offset points",
            fontsize=7.5, color="#333333", rotation=0, ha="left", va="top",
        )

    if phases:
        handles = [Patch(facecolor=phase_color[ph], edgecolor="#333333", label=_clean_label(ph)) for ph in phases]
        ax.legend(handles=handles, loc="lower right", fontsize=8, framealpha=0.9)

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    log.info(f"  ✓ 渲染横道图: {out_png.name}  任务={n}  里程碑={len(milestones)}")
    return True


# ─── JSON 表格 → Markdown 表格转换 ──────────────────────────────────────

def json_table_to_markdown(data: dict) -> str:
    """把 JSON 表格转为 markdown 表格语法。返回空串表示无法转换。"""
    inner = data.get("data", data)

    headers: list[str] = []
    rows: list[list[str]] = []

    # 格式 1: {headers:[...], rows:[[...],...]}
    if isinstance(inner, dict):
        hdrs = inner.get("headers") or inner.get("columns") or inner.get("列")
        rws = inner.get("rows") or inner.get("行")
        if hdrs and rws and isinstance(hdrs, list):
            if isinstance(hdrs[0], dict):
                headers = [str(h.get("name", "") or h.get("key", "")) for h in hdrs]
            else:
                headers = [str(h) for h in hdrs]
            for r in rws:
                if isinstance(r, list):
                    rows.append([str(c).replace("\n", " ").replace("|", "｜") for c in r])
                elif isinstance(r, dict):
                    rows.append([str(r.get(h, "")).replace("\n", " ").replace("|", "｜") for h in headers])

    # 格式 2: {data: [[row0_as_header], [row1], ...]}  首行当表头
    if not headers and isinstance(inner, list) and inner and isinstance(inner[0], list):
        headers = [str(c).replace("|", "｜") for c in inner[0]]
        for r in inner[1:]:
            if isinstance(r, list):
                rows.append([str(c).replace("\n", " ").replace("|", "｜") for c in r])

    # 格式 3: {data: [{k:v,...}, ...]}  用 keys 做表头
    if not headers and isinstance(inner, list) and inner and isinstance(inner[0], dict):
        headers = [str(k) for k in inner[0].keys()]
        for item in inner:
            if isinstance(item, dict):
                rows.append([str(item.get(h, "")).replace("\n", " ").replace("|", "｜") for h in headers])

    # 格式 4: 顶层 headers + data（非 dict 包装）
    if not headers:
        hdrs = data.get("headers") or data.get("columns") or data.get("列")
        rws = data.get("rows") or data.get("行")
        if hdrs and rws and isinstance(hdrs, list):
            if isinstance(hdrs[0], dict):
                headers = [str(h.get("name", "")) for h in hdrs]
            else:
                headers = [str(h) for h in hdrs]
            for r in rws:
                if isinstance(r, list):
                    rows.append([str(c).replace("\n", " ").replace("|", "｜") for c in r])

    # 格式 5: cols + data[[...]]
    if not headers and "cols" in data and isinstance(data.get("data"), list):
        raw = data["data"]
        if raw and isinstance(raw[0], list):
            headers = [str(c).replace("|", "｜") for c in raw[0]]
            for r in raw[1:]:
                if isinstance(r, list):
                    rows.append([str(c).replace("\n", " ").replace("|", "｜") for c in r])

    if not headers or not rows:
        return ""

    # 对齐列数
    ncols = len(headers)
    for i, r in enumerate(rows):
        if len(r) < ncols:
            rows[i] = r + [""] * (ncols - len(r))
        elif len(r) > ncols:
            rows[i] = r[:ncols]

    title = data.get("title") or data.get("caption") or ""
    lines = []
    if title:
        lines.append(f"\n**{title}**\n")
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * ncols) + "|")
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    lines.append("")
    return "\n".join(lines)


# ─── 图表数据适配（解包 + 结构转换） ────────────────────────────────────

def _adapt_schedule_data(data: dict) -> dict:
    """把各种进度 JSON 格式统一为 render_schedule_png 能接受的格式。"""
    d = _unwrap_data(data)
    if "tasks" in d or "schedule" in d:
        return d

    # LLM 常写 data: {header: [...], rows: [{name, start, end, color}]}
    # (_unwrap_data 已将 data 解包，所以 rows 在顶层)
    rows = d.get("rows") or []
    if rows:
        tasks = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name") or row.get("task") or ""
            start = row.get("start")
            end = row.get("end")
            dur = row.get("duration") or row.get("days")
            if end is None and dur is not None and start is not None:
                try:
                    end = int(start) + int(dur)
                except (ValueError, TypeError):
                    pass
            if name and start is not None and end is not None:
                tasks.append({
                    "name": name, "start": start, "end": end,
                    "color": row.get("color", ""),
                    "phase": row.get("phase", "") or row.get("stage", ""),
                })
        if tasks:
            result = {k: v for k, v in d.items() if k != "rows"}
            result["tasks"] = tasks
            return result

    items = d.get("_items") or d.get("items") or d.get("phases") or []
    if not items:
        return d

    tasks = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("task") or item.get("phase") or ""
        start = item.get("start") if item.get("start") is not None else \
                item.get("start_day") or item.get("begin")
        end = item.get("end") if item.get("end") is not None else \
              item.get("end_day") or item.get("finish")
        dur = item.get("duration") or item.get("days")
        if end is None and dur is not None and start is not None:
            try:
                end = int(start) + int(dur)
            except (ValueError, TypeError):
                pass
        if name and start is not None and end is not None:
            tasks.append({
                "name": name, "start": start, "end": end,
                "phase": item.get("phase", "") or item.get("stage", ""),
            })
    if tasks:
        result = {k: v for k, v in d.items() if k not in ("_items", "items", "phases")}
        result["tasks"] = tasks
        return result
    return d


def _adapt_org_chart_data(data: dict) -> dict:
    """把 level_N 扁平结构转为 root/children 递归结构。"""
    d = _unwrap_data(data)
    content = d if "root" not in d else d
    if "root" in d and isinstance(d["root"], dict):
        return d

    # 收集 level_N
    levels: dict[int, list[str]] = {}
    source = data.get("content") or data.get("data") or data
    if not isinstance(source, dict):
        return d
    for k, v in source.items():
        if not isinstance(k, str) or not k.startswith("level_"):
            continue
        try:
            n = int(k.split("_")[1])
        except (IndexError, ValueError):
            continue
        if isinstance(v, str):
            levels[n] = [v]
        elif isinstance(v, list):
            levels[n] = [str(x) for x in v]
        elif isinstance(v, dict):
            flat = []
            for sv in v.values():
                if isinstance(sv, list):
                    flat.extend(str(x) for x in sv)
                elif isinstance(sv, str):
                    flat.append(sv)
            levels[n] = flat

    if not levels:
        return d

    sorted_lvs = sorted(levels.keys())

    def _build_tree(li: int) -> list[dict]:
        if li >= len(sorted_lvs):
            return []
        items = levels[sorted_lvs[li]]
        children = _build_tree(li + 1)
        nodes = [{"role": item, "children": []} for item in items]
        if children and nodes:
            per = max(1, len(children) // len(nodes))
            for i, nd in enumerate(nodes):
                s = i * per
                e = s + per if i < len(nodes) - 1 else len(children)
                nd["children"] = children[s:e]
        return nodes

    tree = _build_tree(0)
    if not tree:
        return d
    root = tree[0] if len(tree) == 1 else {"role": "组织机构", "children": tree}
    return {"root": root, "title": data.get("title", "")}


def _normalize_edges(edges, default_label: str = "") -> list[dict]:
    """把 edges 各种写法统一为 list of {from, to, label} dict。

    支持:
    - [{"from":"a","to":"b","label":"x"}]    已规整
    - [["a","b"], ["b","c"]]                  list-of-list
    - [("a","b","x")]                         tuple 含 label
    - {"a":"b","b":"c"}                       dict 邻接
    """
    if not edges:
        return []
    out = []
    if isinstance(edges, dict):
        for k, v in edges.items():
            out.append({"from": str(k), "to": str(v), "label": default_label})
        return out
    for e in edges:
        if isinstance(e, dict):
            out.append({
                "from": str(e.get("from") or e.get("source") or e.get("src") or ""),
                "to": str(e.get("to") or e.get("target") or e.get("dst") or ""),
                "label": str(e.get("label", default_label)),
            })
        elif isinstance(e, (list, tuple)) and len(e) >= 2:
            out.append({
                "from": str(e[0]),
                "to": str(e[1]),
                "label": str(e[2]) if len(e) >= 3 else default_label,
            })
    return out


def _adapt_process_flow_data(data: dict) -> dict:
    """把各种流程图 JSON 格式统一为 render_process_flow 能接受的格式。"""
    d = _unwrap_data(data)

    # nodes 归一化:list-of-string → list-of-dict
    if "nodes" in d:
        nodes = d["nodes"]
        if nodes and isinstance(nodes[0], str):
            d["nodes"] = [{"id": f"s{i}", "label": n} for i, n in enumerate(nodes)]
        elif nodes and isinstance(nodes[0], dict):
            # 缺 id 自动补
            for i, n in enumerate(nodes):
                if "id" not in n:
                    n["id"] = f"s{i}"
                if "label" not in n:
                    n["label"] = n.get("name", n.get("id"))

    # edges/connections 归一化
    raw_edges = d.get("edges") or d.get("connections") or []
    d["edges"] = _normalize_edges(raw_edges)

    if "nodes" in d:
        if not d.get("edges") and len(d["nodes"]) > 1:
            # 顺序连
            d["edges"] = [{"from": d["nodes"][i]["id"], "to": d["nodes"][i + 1]["id"], "label": ""}
                          for i in range(len(d["nodes"]) - 1)]
        return d

    # 从 steps/_items 推 nodes
    steps = d.get("steps") or d.get("_items")
    if isinstance(steps, list) and steps:
        nodes = []
        for i, s in enumerate(steps):
            if isinstance(s, str):
                nodes.append({"id": f"s{i}", "label": s})
            elif isinstance(s, dict):
                nodes.append({"id": s.get("id", f"s{i}"),
                              "label": s.get("name", s.get("label", str(s)))})
        edges = _normalize_edges(d.get("edges") or d.get("connections") or [])
        if not edges:
            edges = [{"from": nodes[i]["id"], "to": nodes[i + 1]["id"], "label": ""}
                     for i in range(len(nodes) - 1)]
        return {"nodes": nodes, "edges": edges, "title": d.get("title", "")}
    return d


def _adapt_resource_curve_data(data: dict) -> dict:
    """把柱状图/曲线变体统一为 render_resource_curve 能接受的格式。

    renderer 期望 series = list of {name, values}。
    LLM 常写:
    - series = {name1:[..], name2:[..]}    dict 形式
    - series = [{name, values}]             list 形式(已规整)
    - 或 categories+values(单系列)
    """
    d = _unwrap_data(data)

    # series 归一化
    if "series" in d:
        s = d["series"]
        if isinstance(s, dict):
            d["series"] = [{"name": str(k), "values": list(v) if isinstance(v, (list, tuple)) else []}
                           for k, v in s.items()]
        elif isinstance(s, list):
            normed = []
            for item in s:
                if isinstance(item, dict):
                    normed.append({
                        "name": str(item.get("name", item.get("label", ""))),
                        "values": list(item.get("values") or item.get("data") or []),
                    })
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    normed.append({"name": str(item[0]),
                                   "values": list(item[1]) if isinstance(item[1], (list, tuple)) else []})
            d["series"] = normed

    if "periods" in d and "series" in d:
        return d

    # LLM 常写 data: [{month:1, 工种A:15, 工种B:5}, ...] 交叉表格式
    # (_unwrap_data 将 list 解包到 _items)
    rows_data = d.get("_items")
    if not rows_data:
        rows_data = d.get("data") if isinstance(d.get("data"), list) else None
    if rows_data and len(rows_data) > 0 and isinstance(rows_data[0], dict):
        key_names = [k for k in rows_data[0].keys()
                     if k not in ("month", "period") and k[0] != "_"]
        if "month" in rows_data[0] or "period" in rows_data[0]:
            pk = "month" if "month" in rows_data[0] else "period"
            periods = [str(r.get(pk, "")) for r in rows_data]
            if key_names:
                series = []
                for kn in key_names:
                    series.append({"name": kn, "values": [r.get(kn, 0) or 0 for r in rows_data]})
                return {
                    "periods": periods,
                    "series": series,
                    "x_label": d.get("x_label", ""),
                    "y_label": d.get("y_label", ""),
                    "title": d.get("title", ""),
                }

    cats = d.get("categories") or d.get("periods")
    vals = d.get("values")
    if isinstance(cats, list) and isinstance(vals, list):
        return {
            "periods": cats,
            "series": [{"name": d.get("title", "数量"), "values": vals}],
            "y_label": d.get("unit", ""),
            "title": d.get("title", ""),
        }
    return d


def _adapt_hierarchy_flow_data(data: dict) -> dict:
    """把 hierarchy_flow 各种写法统一为 layers=list-of-dict + connections=list-of-dict。

    renderer 期望:
    - layers: [{"name": "决策层", "nodes": ["项目经理"]}, ...]
    - connections: [{"from":"项目经理","to":"技术负责人","label":""}, ...]

    兼容 LLM 常见写法:
    - layers: [["a"], ["b","c"]]              list-of-list (无层名)
    - layers: [{"层名":"决策","成员":[..]}]    中文键
    - connections: [["a","b"]] / [["a","b","label"]]
    - root: {name, children: [...]}           树形结构 → 自动铺层
    """
    d = _unwrap_data(data)

    # LLM 常写 root.children 树形结构，转成 layers + connections
    root = d.get("root")
    if root and isinstance(root, dict) and "children" in root:
        layers = []
        connections = []

        def _walk(node, depth, parent_name=None):
            while len(layers) <= depth:
                layers.append({"name": "", "nodes": []})
            name = node.get("name", "")
            if name:
                layers[depth]["nodes"].append(name)
            if parent_name and name:
                connections.append({"from": parent_name, "to": name, "label": ""})
            for child in node.get("children") or []:
                _walk(child, depth + 1, name)

        _walk(root, 0)
        d["layers"] = layers
        d["connections"] = connections
        return d

    # layers 归一化
    raw = d.get("layers") or []
    norm_layers = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name") or item.get("层名") or item.get("title") or ""
            nodes = item.get("nodes") or item.get("members") or item.get("成员") or []
            if isinstance(nodes, str):
                nodes = [nodes]
            norm_layers.append({"name": str(name), "nodes": [str(n) for n in nodes]})
        elif isinstance(item, list):
            norm_layers.append({"name": "", "nodes": [str(n) for n in item]})
        elif isinstance(item, str):
            norm_layers.append({"name": "", "nodes": [item]})
    d["layers"] = norm_layers

    # connections 归一化
    raw_c = d.get("connections") or d.get("edges") or []
    d["connections"] = _normalize_edges(raw_c)

    return d


def _adapt_aoa_network_data(data: dict) -> dict:
    """把双代号网络图各种写法统一为 activities=list-of-dict。

    renderer 期望:
    - activities: [{"from":1,"to":2,"name":"准备","duration":5}, ...]
    - events: 可选,list of int

    兼容:
    - edges 替代 activities
    - activity 缺 duration 时默认 1
    - from/to 是字符串时尝试映射为整数 id
    """
    d = _unwrap_data(data)

    raw = d.get("activities") or d.get("edges") or []
    activities = []
    str_to_id: dict[str, int] = {}
    next_id = [1]

    def _eid(v):
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v)
        if s.isdigit():
            return int(s)
        if s not in str_to_id:
            str_to_id[s] = next_id[0]
            next_id[0] += 1
        return str_to_id[s]

    for a in raw:
        if isinstance(a, dict):
            f = a.get("from") if a.get("from") is not None else a.get("source") or a.get("src")
            t = a.get("to") if a.get("to") is not None else a.get("target") or a.get("dst")
            if f is None or t is None:
                continue
            activities.append({
                "from": _eid(f),
                "to": _eid(t),
                "name": str(a.get("name", a.get("label", ""))),
                "duration": a.get("duration", a.get("days", 1)),
                "dummy": bool(a.get("dummy", False)),
            })
        elif isinstance(a, (list, tuple)) and len(a) >= 2:
            activities.append({
                "from": _eid(a[0]),
                "to": _eid(a[1]),
                "name": str(a[2]) if len(a) >= 3 else "",
                "duration": a[3] if len(a) >= 4 else 1,
                "dummy": False,
            })

    out = {k: v for k, v in d.items() if k not in ("activities", "edges")}
    out["activities"] = activities
    if "events" not in out:
        es = sorted({a["from"] for a in activities} | {a["to"] for a in activities})
        if es:
            out["events"] = es
    return out


# 站点平面图:LLM 常写 list of {x,y,w,h,name},按位置推方向
_SITE_DIRECTIONS = ("north", "south", "east", "west")


def _infer_zone_direction(x: float, y: float, w: float, h: float,
                          site_w: float, site_h: float, building) -> str:
    """根据坐标相对场地中心(或主体建筑)推断方向。"""
    cx = x + w / 2
    cy = y + h / 2
    bx = building.get("x", site_w / 2) + building.get("w", 0) / 2 if building else site_w / 2
    by = building.get("y", site_h / 2) + building.get("h", 0) / 2 if building else site_h / 2
    dx = cx - bx
    dy = cy - by
    if abs(dy) >= abs(dx):
        return "north" if dy > 0 else "south"
    return "east" if dx > 0 else "west"


def _adapt_site_layout_data(data: dict) -> dict:
    """把 site_layout 各种写法统一为 zones=dict{north/south/east/west: list-of-string}。

    renderer 期望:
    - zones: {"north": ["材料堆场"], "south": [..], ...}
    - main_building: 主体建筑名称
    - cranes: [{"position":"northeast_of_building","label":"汽车吊25t"}]

    兼容:
    - zones 是 list of {x,y,w,h,name} → 按位置推方向
    - zones 是 list of string → 平均分配到 4 方向
    - facilities 字段(平铺) → 按位置归并到 zones
    """
    d = _unwrap_data(data)

    # main_building 归一化
    if "main_building" not in d:
        b = d.get("buildings")
        if isinstance(b, list) and b and isinstance(b[0], dict):
            d["main_building"] = str(b[0].get("name") or b[0].get("label") or "主体建筑")
        else:
            d["main_building"] = "主体建筑"

    # template 默认
    if "template" not in d:
        d["template"] = "rectangular_center"

    raw_zones = d.get("zones")

    # 已是 dict 形式且 key 是方向
    if isinstance(raw_zones, dict):
        normed = {dir_: [] for dir_ in _SITE_DIRECTIONS}
        for k, v in raw_zones.items():
            kn = str(k).lower()
            # 中文方向映射
            if "北" in str(k) or "north" in kn:
                key = "north"
            elif "南" in str(k) or "south" in kn:
                key = "south"
            elif "东" in str(k) or "east" in kn:
                key = "east"
            elif "西" in str(k) or "west" in kn:
                key = "west"
            else:
                # 未知 key,丢到 north
                key = "north"
            if isinstance(v, str):
                normed[key].append(v)
            elif isinstance(v, list):
                normed[key].extend([str(x.get("name", x.get("label", str(x)))) if isinstance(x, dict) else str(x)
                                    for x in v])
        d["zones"] = normed
        return d

    # list 形式
    if isinstance(raw_zones, list):
        site = d.get("site") or {}
        site_w = site.get("width_m", 100) if isinstance(site, dict) else 100
        site_h = site.get("height_m", 100) if isinstance(site, dict) else 100
        building = (d.get("buildings") or [{}])[0] if isinstance(d.get("buildings"), list) else {}

        normed = {dir_: [] for dir_ in _SITE_DIRECTIONS}
        for i, z in enumerate(raw_zones):
            if isinstance(z, str):
                # 平均分配
                normed[_SITE_DIRECTIONS[i % 4]].append(z)
            elif isinstance(z, dict):
                name = str(z.get("name") or z.get("label") or z.get("zone")
                           or z.get("symbol") or f"区域{i+1}")
                # 优先看用户显式声明的 direction
                explicit = z.get("direction") or z.get("方位")
                if explicit:
                    en = str(explicit).lower()
                    for d_ in _SITE_DIRECTIONS:
                        if d_ in en or {"north":"北","south":"南","east":"东","west":"西"}[d_] in str(explicit):
                            normed[d_].append(name)
                            break
                    else:
                        normed["north"].append(name)
                elif all(k in z for k in ("x", "y")):
                    direction = _infer_zone_direction(
                        z.get("x", 0), z.get("y", 0),
                        z.get("w", 1), z.get("h", 1),
                        site_w, site_h, building,
                    )
                    normed[direction].append(name)
                else:
                    normed[_SITE_DIRECTIONS[i % 4]].append(name)
        d["zones"] = normed
        return d

    # 通用兜底:writer 的 site_layout schema 不统一(elements / legend字典 /
    # zonning_areas / facilities …),从任意字段搜刮设施名,轮转均分到 4 区。
    # 各变体设施清单不同 → 渲染结果天然互不雷同(反串标)。
    _road_kw = ("道路", "干道", "次干道", "主干道", "支路", "通道", "围挡", "围墙",
                "消防通道", "运输路", "环路")
    facilities: list[str] = []

    def _add_fac(name) -> None:
        name = str(name or "").strip()
        if (name and name not in facilities
                and not any(k in name for k in _road_kw)):
            facilities.append(name)

    raw_elements = d.get("elements")
    if isinstance(raw_elements, list):
        for e in raw_elements:
            if isinstance(e, dict):
                _add_fac(e.get("label") or e.get("name"))
    leg = d.get("legend")
    if isinstance(leg, dict):
        items = leg.get("items")
        if isinstance(items, list):
            for it in items:
                _add_fac((it.get("label") or it.get("symbol") or it.get("name"))
                         if isinstance(it, dict) else it)
        else:
            for k, v in leg.items():
                if isinstance(v, str):  # {设施名: 描述}
                    _add_fac(k)
    elif isinstance(leg, list):
        for it in leg:
            _add_fac((it.get("label") or it.get("symbol") or it.get("name"))
                     if isinstance(it, dict) else it)
    for fld in ("zonning_areas", "zoning_areas", "facilities",
                "buildings", "main_buildings", "areas"):
        val = d.get(fld)
        if isinstance(val, list):
            for it in val:
                _add_fac(it.get("name") or it.get("label") if isinstance(it, dict) else it)

    if facilities:
        d["main_building"] = next(
            (f for f in facilities if any(k in f for k in ("鸡舍", "主体", "拟建", "厂房"))),
            "主体建筑",
        )
        rest = [f for f in facilities if f != d["main_building"]]
        normed = {dir_: [] for dir_ in _SITE_DIRECTIONS}
        for i, name in enumerate(rest[:16]):
            normed[_SITE_DIRECTIONS[i % 4]].append(name)
        d["zones"] = normed
        d["template"] = "rectangular_center"
        return d

    # 没 zones,塞个空
    d["zones"] = {dir_: [] for dir_ in _SITE_DIRECTIONS}
    return d


_CHART_ADAPTERS = {
    "schedule": _adapt_schedule_data,
    "org_chart": _adapt_org_chart_data,
    "process_flow": _adapt_process_flow_data,
    "resource_curve": _adapt_resource_curve_data,
    "hierarchy_flow": _adapt_hierarchy_flow_data,
    "aoa_network": _adapt_aoa_network_data,
    "site_layout": _adapt_site_layout_data,
}


# 各图表类型经 adapter 规范化后,renderer 真正画得出内容所需的最小字段。
_CHART_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "schedule": ("tasks",),
    "org_chart": ("root",),
    "process_flow": ("nodes",),
    "hierarchy_flow": ("layers",),
    "resource_curve": ("series",),
    "aoa_network": ("activities",),
    # site_layout 的 zones 是 dict,单独判,见下。
}


def validate_chart_data(ctype: str, data: dict) -> tuple[bool, str]:
    """检查(经 adapter 规范化后的)图表数据是否含可渲染内容。

    返回 (ok, reason)。ok=False 表示按当前数据渲染只会得到空图/通用兜底图
    —— 调用方应据此明确告警,而非静默出一张空图(养殖类项目 site_layout 踩过)。
    """
    d = data if isinstance(data, dict) else {}
    if ctype == "site_layout":
        zones = d.get("zones") or {}
        n = sum(len(v) for v in zones.values() if isinstance(v, list))
        if n == 0 and not d.get("elements"):
            return False, "zones 全空且无 elements(无任何设施)"
        return True, ""
    required = _CHART_REQUIRED_KEYS.get(ctype)
    if not required:
        return True, ""  # 未登记类型不拦
    if any(d.get(k) for k in required):
        return True, ""
    return False, f"缺 {'/'.join(required)}(无可渲染内容)"


# ─── ASCII 字符画检测 ─────────────────────────────────────────────────

_BOXART_CHARS = set("█░▓▒│─┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬━┃┏┓┗┛")


# ─── 中文数字（章序号） ───────────────────────────────────────────────

_CN_NUM = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]


def _cn_numeral(n: int) -> str:
    """1-99 的中文数字（投标章节够用）。"""
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_NUM[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_NUM[n - 10]
    tens, ones = divmod(n, 10)
    return _CN_NUM[tens] + "十" + (_CN_NUM[ones] if ones else "")


def _prepend_chapter_number(text: str, chapter_idx: int) -> str:
    """给首个 H1（# xxx）加上"第N章 "前缀。已有则不动。"""
    if chapter_idx <= 0:
        return text
    prefix = f"第{_cn_numeral(chapter_idx)}章"
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            if title.startswith("第") and "章" in title[:6]:
                return text  # 已有章号
            lines[i] = f"# {prefix} {title}"
            return "\n".join(lines)
    return text


# ── 标题编号风格 ─────────────────────────────────────────────
# 12 种常用编号体系，组装时随机分配给变体

_CN_NUMERALS = "一二三四五六七八九十"
_ROMAN = ["Ⅰ","Ⅱ","Ⅲ","Ⅳ","Ⅴ","Ⅵ","Ⅶ","Ⅷ","Ⅸ","Ⅹ","Ⅺ","Ⅻ"]

def _cn_num(n: int) -> str:
    """整数转中文数字：1→一, 10→十, 12→十二"""
    if n <= 0:
        return ""
    if n <= 10:
        return _CN_NUMERALS[n - 1]
    if n < 20:
        return "十" + ("" if n == 10 else _CN_NUMERALS[n - 11])
    if n < 100:
        tens, ones = divmod(n, 10)
        s = _CN_NUMERALS[tens - 1] + "十"
        if ones:
            s += _CN_NUMERALS[ones - 1]
        return s
    return str(n)

_HEADING_STYLES = {
    # id: (name, h2_fmt, h3_fmt, h4_fmt)
    # 格式函数签名: (ch_str, h2_n, h3_n, h4_n) -> prefix_str
    "A": ("数字分节",
          lambda c, h2, h3, h4: f"{int(c)}.{h2}",
          lambda c, h2, h3, h4: f"{int(c)}.{h2}.{h3}",
          lambda c, h2, h3, h4: f"{int(c)}.{h2}.{h3}.{h4}"),
    "B": ("数字带章号",
          lambda c, h2, h3, h4: f"{c}.{h2}",
          lambda c, h2, h3, h4: f"{c}.{h2}.{h3}",
          lambda c, h2, h3, h4: f"{c}.{h2}.{h3}.{h4}"),
    "C": ("公文传统",
          lambda c, h2, h3, h4: f"{_cn_num(h2)}、",
          lambda c, h2, h3, h4: f"（{_cn_num(h3)}）",
          lambda c, h2, h3, h4: f"{h4}."),
    "D": ("公文简化",
          lambda c, h2, h3, h4: f"{_cn_num(h2)}、",
          lambda c, h2, h3, h4: f"{h3}.",
          lambda c, h2, h3, h4: f"（{h4}）"),
    "E": ("公文完整",
          lambda c, h2, h3, h4: f"{_cn_num(h2)}、",
          lambda c, h2, h3, h4: f"（{_cn_num(h3)}）",
          lambda c, h2, h3, h4: f"{h4}."),
    "F": ("纯数字嵌套",
          lambda c, h2, h3, h4: f"{h2}.",
          lambda c, h2, h3, h4: f"{h2}.{h3}",
          lambda c, h2, h3, h4: f"{h2}.{h3}.{h4}"),
    "G": ("数字+括号",
          lambda c, h2, h3, h4: f"{h2}.",
          lambda c, h2, h3, h4: f"（{h3}）",
          lambda c, h2, h3, h4: f"{'①②③④⑤⑥⑦⑧⑨⑩'[h4-1]}" if h4 <= 10 else f"({h4})"),
    "H": ("公文圆圈",
          lambda c, h2, h3, h4: f"{_cn_num(h2)}、",
          lambda c, h2, h3, h4: f"{h3}.",
          lambda c, h2, h3, h4: f"{'①②③④⑤⑥⑦⑧⑨⑩'[h4-1]}" if h4 <= 10 else f"({h4})"),
    "I": ("章节式",
          lambda c, h2, h3, h4: f"第{_cn_num(h2)}节",
          lambda c, h2, h3, h4: f"{_cn_num(h3)}、",
          lambda c, h2, h3, h4: f"（{_cn_num(h4)}）"),
    "J": ("罗马数字",
          lambda c, h2, h3, h4: f"{_ROMAN[h2-1]}." if h2 <= 12 else f"{h2}.",
          lambda c, h2, h3, h4: f"{_ROMAN[h2-1]}.{h3}" if h2 <= 12 else f"{h2}.{h3}",
          lambda c, h2, h3, h4: f"{_ROMAN[h2-1]}.{h3}.{h4}" if h2 <= 12 else f"{h2}.{h3}.{h4}"),
    "K": ("混合A",
          lambda c, h2, h3, h4: f"{_cn_num(h2)}、",
          lambda c, h2, h3, h4: f"（{_cn_num(h3)}）",
          lambda c, h2, h3, h4: f"{h4})"),
    "L": ("混合B",
          lambda c, h2, h3, h4: f"（{_cn_num(h2)}）",
          lambda c, h2, h3, h4: f"{h3}.",
          lambda c, h2, h3, h4: f"（{h4}）"),
}


# 编号风格按变体序号轮转(反串标):确定性派发,保证 N 个变体两两不同。
# 旧实现按 seed 随机挑,会出现两变体撞同一风格;且不分层级可读性。
# 全部 12 种均层级可区分(F/H 已修),排在前面的为更常见的工程文件风格。
_VARIANT_HEADING_STYLES = ["B", "C", "L", "D", "A", "I", "G", "K", "E", "F", "H", "J"]


def assign_heading_style(variant_index: int) -> str:
    """按变体序号(0,1,2,...)确定性分配编号风格,N 变体互不相同。"""
    if not _VARIANT_HEADING_STYLES:
        return "B"
    return _VARIANT_HEADING_STYLES[int(variant_index) % len(_VARIANT_HEADING_STYLES)]


def _normalize_heading_numbers(text: str, chapter_idx: int,
                               style_id: str = "B") -> str:
    """为 H2/H3/H4 自动补序号（支持 12 种编号风格）。

    LLM 输出的标题可能带序号（07.1 xxx）也可能不带（施工方案）。
    此函数先剥离已有序号，再用指定风格重新编号。

    style_id: A-L 对应 _HEADING_STYLES 中的编号风格。
    """
    if chapter_idx <= 0:
        return text
    ch_str = str(chapter_idx).zfill(2)

    style = _HEADING_STYLES.get(style_id, _HEADING_STYLES["B"])
    h2_fmt, h3_fmt, _ = style[1], style[2], style[3]

    # 检测标题行
    h2_pat = re.compile(r'^(##)\s+')
    h3_pat = re.compile(r'^(###)\s+')
    h4_pat = re.compile(r'^(####)\s+')
    # 已有序号模式（各种格式都匹配，以便剥离）
    any_num = re.compile(r'^(\d+\.?\d*\.?\d*\.?\d*|[一二三四五六七八九十]+[、．.]|（[一二三四五六七八九十\d]+）|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\.?\d*|第[一二三四五六七八九十\d]+节|[\d]+[）)]|①②③④⑤⑥⑦⑧⑨⑩)\s*')

    lines = text.split("\n")
    h2_counter = 0
    h3_counter = 0

    for i, line in enumerate(lines):
        # H3（要先于 H2 检测，因为 ### 也匹配 ##）
        m3 = h3_pat.match(line)
        if m3:
            h3_counter += 1
            title = line[m3.end():]
            # 剥离已有编号
            nm = any_num.match(title)
            if nm:
                title = title[nm.end():]
            prefix = h3_fmt(ch_str, h2_counter, h3_counter, 0)
            lines[i] = f"### {prefix} {title}"
            continue

        # H2
        m2 = h2_pat.match(line)
        if m2 and not h3_pat.match(line):
            h2_counter += 1
            h3_counter = 0
            title = line[m2.end():]
            # 剥离已有编号
            nm = any_num.match(title)
            if nm:
                title = title[nm.end():]
            prefix = h2_fmt(ch_str, h2_counter, 0, 0)
            lines[i] = f"## {prefix} {title}"
            continue

    return "\n".join(lines)


def _strip_bare_json(text: str, filename: str, data_dir: Path,
                     log: logging.Logger) -> str:
    """扫描并剥离非围栏包裹的裸 JSON 对象/数组。

    LLM 有时直接输出 JSON 而不用 ```json 包裹，这些必须剥离，
    否则原始 JSON 出现在最终文档中会导致废标。
    """
    # 匹配独立行上以 { 开头的多行块，到配对的 } 结束
    result = []
    lines = text.split("\n")
    i = 0
    bare_count = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # 检测独立行以 { 或 [ 开头（不在围栏代码块内）
        if stripped in ("{", "[") or (
                stripped.startswith("{") and stripped.endswith(",")
                and '"' in stripped):
            # 收集到配对的闭合括号
            open_char = "{" if "{" in stripped else "["
            close_char = "}" if open_char == "{" else "]"
            depth = 0
            block_lines = []
            j = i
            while j < len(lines):
                for ch in lines[j]:
                    if ch == open_char:
                        depth += 1
                    elif ch == close_char:
                        depth -= 1
                block_lines.append(lines[j])
                if depth <= 0:
                    break
                j += 1
            block_text = "\n".join(block_lines)
            # 尝试解析为 JSON
            parsed = _parse_json_safe(block_text)
            if parsed is not None and isinstance(parsed, (dict, list)):
                # 是有效 JSON，剥离并备份
                bare_count += 1
                data_dir.mkdir(parents=True, exist_ok=True)
                suffix = "" if bare_count == 1 else f"_{bare_count}"
                bak = data_dir / f"{Path(filename).stem}.bare{suffix}.json"
                bak.write_text(json.dumps(parsed, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                t = parsed.get("type", "") if isinstance(parsed, dict) else ""
                log.info(f"  [{filename}] 剥离裸 JSON (type={t})，{len(block_lines)} 行")
                i = j + 1
                continue
            # 不是有效 JSON，保留原文
        result.append(line)
        i += 1
    if bare_count:
        log.info(f"  [{filename}] 共剥离 {bare_count} 个裸 JSON 块")
    return "\n".join(result)


_PIPE_TABLE_LINE = re.compile(r"^\|.+\|[ \t]*$", re.MULTILINE)
_PIPE_SEP_LINE = re.compile(r"^\|[ \t:]*-{2,}[ \t:]*(\|[ \t:]*-{2,}[ \t:]*)*\|[ \t]*$", re.MULTILINE)

# ─── 伪横道表格检测与提取 ────────────────────────────────────────────

_GANTT_BLOCK_CHARS = set("█░▓▒")
_TIME_COL_RE = re.compile(r"第?\s*(\d+)\s*[-~至]\s*(\d+)\s*[天日]?")
_PIPE_START_RE = re.compile(r"^\|.+\|")


def _is_pipe_or_gantt_line(line: str) -> bool:
    """宽松判定：标准 pipe 表格行 OR 以 | 开头且含横道字符的溢出行。"""
    s = line.strip()
    if _PIPE_TABLE_LINE.match(s) or _PIPE_SEP_LINE.match(s):
        return True
    if s.startswith("|") and any(c in s for c in _GANTT_BLOCK_CHARS):
        return True
    return False


def _is_pseudo_gantt_table(table_lines: list[str]) -> bool:
    """判断一组 pipe table 行是否是伪横道图（含 █ 类字符模拟横道条）。"""
    bar_lines = 0
    for line in table_lines:
        if any(c in line for c in _GANTT_BLOCK_CHARS):
            bar_lines += 1
    return bar_lines >= 2


def _parse_pseudo_gantt(table_lines: list[str], log: logging.Logger) -> dict | None:
    """从伪横道 pipe table 提取 schedule JSON。

    表格格式: | 序号 | 工作内容 | 工期（天）| 第1-30天 | 第31-60天 | ... |
    返回与 render_schedule_png 兼容的 schedule dict，失败返回 None。
    """
    header_line = None
    sep_idx = -1
    data_lines = []

    for i, line in enumerate(table_lines):
        stripped = line.strip()
        if _PIPE_SEP_LINE.match(stripped):
            sep_idx = i
            if i > 0:
                header_line = table_lines[i - 1].strip()
            continue
        if sep_idx >= 0 and i > sep_idx and stripped.startswith("|"):
            data_lines.append(stripped)

    if not header_line or not data_lines:
        return None

    headers = [h.strip() for h in header_line.split("|")[1:-1]]
    if len(headers) < 3:
        return None

    # 识别时间段列: 找"第X-Y天"模式的列
    time_cols = []  # [(col_idx, day_start, day_end), ...]
    name_col = -1
    duration_col = -1

    for ci, h in enumerate(headers):
        h_clean = re.sub(r"\*+", "", h).strip()
        m = _TIME_COL_RE.search(h_clean)
        if m:
            time_cols.append((ci, int(m.group(1)), int(m.group(2))))
        elif name_col < 0 and any(k in h_clean for k in ("名称", "工程", "分项", "工序", "任务", "作业", "工作内容")):
            name_col = ci
        elif any(k in h_clean for k in ("工期", "天数", "历时")):
            duration_col = ci

    if name_col < 0:
        name_col = 1 if len(headers) > 1 else 0
    if duration_col < 0:
        for ci, h in enumerate(headers):
            h_clean = re.sub(r"\*+", "", h).strip()
            if "天" in h_clean and ci != name_col and (ci,) not in [(tc[0],) for tc in time_cols]:
                duration_col = ci
                break

    # 无时间段列且无工期列 → 无法解析
    if not time_cols and duration_col < 0:
        return None

    max_col_needed = max(tc[0] for tc in time_cols) if time_cols else max(name_col, duration_col)

    tasks = []
    for row_str in data_lines:
        last_pipe = row_str.rfind("|")
        if last_pipe > 0:
            trimmed = row_str[:last_pipe + 1]
        else:
            trimmed = row_str
        cells = [c.strip() for c in trimmed.split("|")[1:-1]]
        while len(cells) <= max_col_needed:
            cells.append("")
        if len(cells) <= max_col_needed:
            continue

        task_name = re.sub(r"\*+", "", cells[name_col]).strip() if name_col < len(cells) else ""
        # 去掉序号前缀如 "1. " "2. "
        task_name = re.sub(r"^\d+\.\s*", "", task_name).strip()
        if not task_name:
            continue

        dur_text = re.sub(r"\*+", "", cells[duration_col]).strip() if duration_col >= 0 and duration_col < len(cells) else ""
        dur_days = None
        dur_m = re.search(r"(\d+)", dur_text)
        if dur_m:
            dur_days = int(dur_m.group(1))

        if not any(c in row_str for c in _GANTT_BLOCK_CHARS):
            continue

        if time_cols:
            # 有多时间列：从 █ 所在列推断起止
            raw_cells = row_str.split("|")[1:]
            first_bar_day = None
            last_bar_day = None
            for col_idx, day_start, day_end in time_cols:
                cell_content = raw_cells[col_idx].strip() if col_idx < len(raw_cells) else ""
                if any(c in cell_content for c in _GANTT_BLOCK_CHARS):
                    if first_bar_day is None:
                        first_bar_day = day_start
                    last_bar_day = day_end
            if first_bar_day is None:
                first_bar_day = time_cols[0][1]
                last_bar_day = time_cols[-1][2]

            task = {"name": task_name, "start": first_bar_day, "end": last_bar_day}
            if dur_days:
                task["duration_days"] = dur_days
            tasks.append(task)
        else:
            # 无多时间列（单列或无列）：仅记录名称和工期，后续顺序排列
            if dur_days:
                tasks.append({"name": task_name, "duration_days": dur_days})

    if not tasks:
        return None

    if time_cols:
        total_end = max(tc[2] for tc in time_cols)
    else:
        # 顺序排列：前后任务有 20% 重叠（模拟搭接施工）
        cursor = 1
        for t in tasks:
            dur = t.get("duration_days", 30)
            t["start"] = cursor
            t["end"] = cursor + dur - 1
            cursor += max(1, int(dur * 0.8))
        total_end = max(t["end"] for t in tasks)

    log.info(f"  伪横道表格解析: {len(tasks)} 个任务, 总跨度 {total_end} 天")

    return {
        "type": "schedule",
        "tasks": tasks,
        "project": {"total_duration_days": total_end},
    }


def _extract_pseudo_gantt_tables(text: str, log: logging.Logger) -> list[tuple[int, int, str, dict]]:
    """扫描 markdown 文本，提取所有伪横道 pipe table。

    返回 [(start_offset, end_offset, title_text, schedule_data), ...]
    """
    lines = text.split("\n")
    results = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if not _is_pipe_or_gantt_line(line):
            i += 1
            continue

        # 收集连续的 table 行
        table_start = i
        # 向上找表格标题（紧邻的非空行）
        title = ""
        for back in range(table_start - 1, max(table_start - 3, -1), -1):
            candidate = lines[back].strip()
            if candidate and not _is_pipe_or_gantt_line(candidate):
                if "横道" in candidate or "进度" in candidate or "计划" in candidate:
                    title = re.sub(r"\*+", "", candidate).strip()
                break

        table_lines = []
        j = i
        while j < len(lines):
            s = lines[j].strip()
            if _is_pipe_or_gantt_line(s) or s == "":
                table_lines.append(lines[j])
                j += 1
                if s == "" and table_lines and any(_GANTT_BLOCK_CHARS & set(tl) for tl in table_lines):
                    break
            else:
                break

        # 去尾部空行
        while table_lines and not table_lines[-1].strip():
            table_lines.pop()

        if _is_pseudo_gantt_table(table_lines):
            data = _parse_pseudo_gantt(table_lines, log)
            if data:
                # 计算原文偏移
                start_off = sum(len(lines[k]) + 1 for k in range(table_start))
                end_off = sum(len(lines[k]) + 1 for k in range(j))
                if not title:
                    title = "施工进度横道图"
                results.append((start_off, end_off, title, data))
            i = j
        else:
            i += 1

    return results


def _replace_pseudo_gantt_tables(
    text: str,
    charts_dir: Path,
    data_dir: Path,
    stem: str,
    log: logging.Logger,
    chart_style: dict | None = None,
) -> str:
    """检测并替换 markdown 中的伪横道表格为甘特图 PNG。"""
    gantt_tables = _extract_pseudo_gantt_tables(text, log)
    if not gantt_tables:
        return text

    # 从后往前替换，避免偏移错乱
    for idx, (start, end, title, data) in enumerate(reversed(gantt_tables)):
        suffix = "" if len(gantt_tables) == 1 else f"_{idx}"
        backup = data_dir / f"{stem}.pseudo_gantt{suffix}.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        png_name = f"{stem}_pseudo_gantt{suffix}.png"
        png_path = charts_dir / png_name
        charts_dir.mkdir(parents=True, exist_ok=True)

        ok = render_schedule_png(data, png_path, title, log, chart_style)
        if ok:
            rel = Path("..") / charts_dir.name / png_name
            replacement = f"\n![{title}]({rel.as_posix()})\n"
            log.info(f"  [{stem}] 伪横道表格 → 甘特图 PNG: {png_name}")
        else:
            replacement = ""
            log.warning(f"  [{stem}] 伪横道表格渲染失败，已剥离")

        text = text[:start] + replacement + text[end:]

    return text


def _ensure_table_blank_lines(text: str) -> str:
    """确保 markdown pipe table 前后有空行，否则 pandoc 不识别。"""
    lines = text.split("\n")
    result = []
    in_table = False
    for i, line in enumerate(lines):
        is_table_line = bool(_PIPE_TABLE_LINE.match(line)) or bool(_PIPE_SEP_LINE.match(line))
        if is_table_line and not in_table:
            if result and result[-1].strip():
                result.append("")
            in_table = True
        elif not is_table_line and in_table:
            if result and result[-1].strip():
                result.append("")
            in_table = False
        result.append(line)
    return "\n".join(result)


def looks_like_ascii_art(content: str) -> bool:
    """判断无语言标签的 ``` 块是否是 ASCII 字符画。"""
    if not content.strip():
        return False
    chars = set(content)
    art_ratio = len(chars & _BOXART_CHARS) / max(len(chars), 1)
    return art_ratio > 0.02 or any(c in content for c in _BOXART_CHARS)


# ─── 单个 md 预处理 ───────────────────────────────────────────────────

_CHART_TITLES = {
    "schedule": "施工进度横道图",
    "org_chart": "项目管理机构框图",
    "hierarchy_flow": "管理体系图",
    "process_flow": "工艺流程图",
    "resource_curve": "资源配置图",
    "aoa_network": "双代号网络计划图",
    "site_layout": "施工总平面布置图",
}


# ─── 渲染器 Schema 单一真相源 ─────────────────────────────────────────
# 每种 chart type 的最小可用 JSON 形状 + 字段说明 + 示例。
# 用途:
#   1. bid_writer 注入 LLM prompt 让模型按 schema 写 ```json``` 块
#   2. docs/chart_schemas.md 文档自动生成
#   3. bid_charts_selftest.py 体检脚本拿 example 跑通
# 保持每个 example 都能直接被 _CHART_ADAPTERS + _CHART_RENDERERS 跑通(经过 adapter 兼容)。

_RENDERER_SCHEMAS: dict[str, dict] = {
    "schedule": {
        "title": "施工进度横道图(Gantt)",
        "use_when": "进度计划章节;附表四 进度网络图;任何含里程碑的工序时间表",
        "required": ["tasks"],
        "fields": {
            "tasks": "list of {name, start, end, phase}。start/end 接受 'YYYY-MM-DD' 或工期序号(int days from 0)。phase 用于横道颜色分组(可选)",
            "milestones": "list of {name, date}(可选,菱形标记)",
            "title": "图标题(可选,缺省走 _CHART_TITLES)",
        },
        "example": {
            "tasks": [
                {"name": "施工准备", "start": "2026-05-31", "end": "2026-06-09", "phase": "准备"},
                {"name": "基础工程", "start": "2026-06-10", "end": "2026-06-30", "phase": "基础"},
                {"name": "主体结构", "start": "2026-07-01", "end": "2026-07-31", "phase": "主体"},
            ],
            "milestones": [
                {"name": "M1 基础验收", "date": "2026-06-30"},
                {"name": "M2 主体封顶", "date": "2026-07-31"},
            ],
        },
    },
    "org_chart": {
        "title": "项目组织机构框图",
        "use_when": "项目组织管理机构章节;招标原文出现『框图』『组织机构』",
        "required": ["root"],
        "fields": {
            "root": "递归 {name|role, children:[]}。每个节点可有 children 数组",
            "title": "图标题(可选)",
        },
        "example": {
            "type": "org_chart",
            "root": {
                "name": "项目经理",
                "children": [
                    {"name": "技术负责人", "children": [{"name": "质检员"}, {"name": "试验员"}]},
                    {"name": "安全员", "children": [{"name": "文明施工员"}]},
                    {"name": "材料员"},
                    {"name": "资料员"},
                ],
            },
        },
    },
    "hierarchy_flow": {
        "title": "管理体系层级图",
        "use_when": "质量/安全/环保管理体系图;部门层级关系图",
        "required": ["layers"],
        "fields": {
            "layers": "list of {name(层名), nodes(成员名称数组)}。LLM 可写 list-of-list 由 adapter 自动转",
            "connections": "list of {from, to, label}。可写 list-of-list `[[a,b]]`",
            "title": "图标题(可选)",
        },
        "example": {
            "type": "hierarchy_flow",
            "layers": [
                {"name": "决策层", "nodes": ["项目经理"]},
                {"name": "管理层", "nodes": ["技术负责人", "安全负责人", "质量负责人"]},
                {"name": "执行层", "nodes": ["施工班组A", "施工班组B"]},
            ],
            "connections": [
                ["项目经理", "技术负责人"], ["项目经理", "安全负责人"], ["项目经理", "质量负责人"],
                ["技术负责人", "施工班组A"], ["技术负责人", "施工班组B"],
            ],
        },
    },
    "process_flow": {
        "title": "工艺流程图",
        "use_when": "施工工艺流程;质量验收流程;材料检测流程;任何带方向/判断的步骤链",
        "required": ["nodes", "edges"],
        "fields": {
            "nodes": "list of {id, label, shape}。shape 可选 box/diamond/oval。LLM 可只写 list-of-string",
            "edges": "list of {from, to, label}。可写 list-of-list `[[a,b],[b,c]]`",
            "title": "图标题(可选)",
        },
        "example": {
            "type": "process_flow",
            "nodes": [
                {"id": "s1", "label": "材料进场", "shape": "box"},
                {"id": "s2", "label": "现场抽检", "shape": "diamond"},
                {"id": "s3", "label": "合格入库", "shape": "box"},
                {"id": "s4", "label": "退场处理", "shape": "box"},
            ],
            "edges": [
                {"from": "s1", "to": "s2"},
                {"from": "s2", "to": "s3", "label": "合格"},
                {"from": "s2", "to": "s4", "label": "不合格"},
            ],
        },
    },
    "resource_curve": {
        "title": "资源配置曲线/柱状图",
        "use_when": "劳动力投入曲线;资金使用计划;材料进退场曲线",
        "required": ["periods", "series"],
        "fields": {
            "periods": "list of 时间标签(月/周/里程碑)",
            "series": "list of {name, values}。LLM 可写 dict {name:values} 由 adapter 自动转",
            "y_label": "纵轴单位(可选,如 '人' / '万元')",
            "title": "图标题(可选)",
        },
        "example": {
            "type": "resource_curve",
            "periods": ["6月", "7月", "8月", "9月"],
            "series": [
                {"name": "普工", "values": [5, 15, 20, 8]},
                {"name": "焊工", "values": [0, 10, 15, 5]},
            ],
            "y_label": "人",
        },
    },
    "aoa_network": {
        "title": "双代号网络计划图(AOA/CPM)",
        "use_when": "进度网络图(关键线路法);需要展示工序逻辑而非时间轴",
        "required": ["activities"],
        "fields": {
            "activities": "list of {from(int), to(int), name, duration}。from/to 是事件编号。LLM 写字符串 id 由 adapter 自动映射",
            "events": "list of int,可选,缺省由 activities 推",
            "title": "图标题(可选)",
        },
        "example": {
            "type": "aoa_network",
            "activities": [
                {"from": 1, "to": 2, "name": "施工准备", "duration": 9},
                {"from": 2, "to": 3, "name": "基础", "duration": 21},
                {"from": 3, "to": 4, "name": "主体", "duration": 31},
                {"from": 4, "to": 5, "name": "围护机电", "duration": 35},
                {"from": 5, "to": 6, "name": "竣工", "duration": 14},
            ],
        },
    },
    "site_layout": {
        "title": "施工总平面布置图",
        "use_when": "招标原文要求『施工总平面图』『总平面布置图』;附表五",
        "required": ["zones"],
        "fields": {
            "template": "rectangular_center | L_shape | U_shape,缺省 rectangular_center",
            "main_building": "主体建筑名称(显示在中央)",
            "zones": "dict {north,south,east,west: list of 设施名称}。LLM 写 list-of-{x,y,w,h,name} 由 adapter 按位置推方向",
            "cranes": "list of {position(northeast_of_building 等), label}",
            "title": "图标题(可选)",
        },
        "example": {
            "type": "site_layout",
            "template": "rectangular_center",
            "main_building": "498㎡ 仓储中心",
            "zones": {
                "north": ["材料堆场", "钢构件预拼场"],
                "south": ["办公生活区", "保安门卫"],
                "east": ["机械停放", "汽车吊就位点"],
                "west": ["临时变压器", "供水池"],
            },
            "cranes": [
                {"position": "northeast_of_building", "label": "汽车吊 25t"},
            ],
        },
    },
}


def get_chart_type_brief_list() -> list[tuple[str, str]]:
    """返回 [(type_name, 一行简要描述), ...] 用于 prompt Block 4 简要列表。

    bid_common.render_output_format_constraints_lines 调本函数避免硬编码。
    详细 schema 请用 get_schema_prompt_snippet(chart_type)。
    """
    out: list[tuple[str, str]] = []
    for t, sch in _RENDERER_SCHEMAS.items():
        title = sch.get("title", "")
        use_when = sch.get("use_when", "")
        first_use = use_when.split(";")[0].strip() if use_when else ""
        brief = f"{title}（适用: {first_use}）" if first_use else title
        out.append((t, brief))
    return out


def get_schema_prompt_snippet(chart_type: str) -> str:
    """返回该 type 的 prompt 片段,用于 bid_writer 注入到 LLM prompt。"""
    sch = _RENDERER_SCHEMAS.get(chart_type)
    if not sch:
        return ""
    fields_doc = "\n".join(f"  - {k}: {v}" for k, v in sch["fields"].items())
    example_json = json.dumps(sch["example"], ensure_ascii=False, indent=2)
    return (
        f"图表类型: {chart_type} ({sch['title']})\n"
        f"使用场景: {sch['use_when']}\n"
        f"必填字段: {', '.join(sch['required'])}\n"
        f"全字段说明:\n{fields_doc}\n"
        f"必须严格按以下 JSON 形状(可不带 type 字段,但建议带):\n"
        f"```json\n{example_json}\n```"
    )


def render_schemas_doc() -> str:
    """生成 docs/chart_schemas.md 内容。"""
    lines = ["# Chart Schemas — bid-gen 渲染器图表 JSON 形状",
             "",
             "本文档由 `bid_charts.py` 的 `_RENDERER_SCHEMAS` 自动生成。修改请改源,不要直接改本文件。",
             "",
             "全部 7 种渲染器都已配 adapter 兼容层,LLM 可宽松写,adapter 自动归一化。",
             "",
             "## 类型总览",
             "",
             "| 类型 | 图标题 | 主要使用场景 |",
             "|---|---|---|"]
    for t, sch in _RENDERER_SCHEMAS.items():
        lines.append(f"| `{t}` | {sch['title']} | {sch['use_when']} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    for t, sch in _RENDERER_SCHEMAS.items():
        lines.append(f"## `{t}` — {sch['title']}")
        lines.append("")
        lines.append(f"**使用场景:** {sch['use_when']}")
        lines.append("")
        lines.append(f"**必填字段:** {', '.join(sch['required'])}")
        lines.append("")
        lines.append("**全字段说明:**")
        lines.append("")
        for k, v in sch["fields"].items():
            lines.append(f"- `{k}`:{v}")
        lines.append("")
        lines.append("**最小可用 JSON:**")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(sch["example"], ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)

_CHART_RENDERERS = {
    "schedule": render_schedule_png,
    "org_chart": render_org_chart,
    "hierarchy_flow": render_hierarchy_flow,
    "process_flow": render_process_flow,
    "resource_curve": render_resource_curve,
    "aoa_network": render_aoa_network,
    "site_layout": render_site_layout,
}


_BULLET_RE = re.compile(r'^[ \t]*([*+\-])[ \t]+(\S.*)$')


def _normalize_md_lists(text: str) -> str:
    """规整无序列表,确保 pandoc 正确解析为列表 —— 否则 `**`/`*` 字面残留进 docx。

    下游模型两个常见毛病会让 pandoc 解析失败:
      1. bullet 带 4+ 空格缩进(`    *   **要点**`)→ 被当代码块,块内标记不解析;
      2. 列表紧贴上一段落、中间无空行 → pandoc 把 bullet 行并进段落,行首 `*` 变字面。
    本函数:所有 bullet 拉到行首并归一为 `- `;列表前缺空行时补一个空行;
    另剥离 `---`/`***`/`___` 整行分隔符(写手拿它当分节,pandoc 会渲染成横线)。
    (下游 assemble 仍会去掉项目符号,故拉平缩进不影响最终观感。)
    """
    out = []
    in_fence = False
    for line in text.split("\n"):
        s = line.lstrip()
        if s.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        # 整行分隔符(thematic break)→ 丢弃,避免 docx 里出现突兀横线
        if re.match(r'^[ \t]*([-*_])\1{2,}[ \t]*$', line):
            continue
        m = _BULLET_RE.match(line)
        if m:
            content = m.group(2)
            if not content.strip(" \t*　"):
                continue  # 空 bullet / 只剩残缺 ** 标记,整行丢弃
            prev = out[-1] if out else ""
            # 列表紧贴段落(上一行非空且不是 bullet)→ 补空行,否则 pandoc 并入段落
            if prev.strip() and not _BULLET_RE.match(prev):
                out.append("")
            out.append(f"- {content}")
        else:
            out.append(line)
    return "\n".join(out)


def preprocess_md(
    src_md: Path,
    tmp_dir: Path,
    charts_dir: Path,
    data_dir: Path,
    variant_id: str,
    log: logging.Logger,
    chapter_idx: int = 0,
    chart_style: dict | None = None,
    heading_style: str = "B",
) -> Path:
    """处理单个 md，返回临时 md 的 Path。

    原文件保持不变；所有剥离/替换发生在临时副本上。
    """
    text = src_md.read_text(encoding="utf-8")
    text = _normalize_md_lists(text)
    text = _prepend_chapter_number(text, chapter_idx)
    text = _normalize_heading_numbers(text, chapter_idx, heading_style)
    blocks = list(iter_fences(text))

    stem = src_md.stem
    type_counters: dict[str, int] = {}
    other_idx = 0
    mermaid_idx = 0

    for blk in reversed(blocks):
        replacement = None

        if blk.lang == "json":
            data = _parse_json_safe(blk.content)
            if data is None:
                log.warning(f"  [{src_md.name}] JSON 解析失败，已剥离")
                replacement = ""
                text = text[:blk.start] + ("\n" if blk.start > 0 and not text[:blk.start].endswith("\n") else "") + replacement + text[blk.end:]
                continue

            # LLM 偶尔把单 dict 包成 [{...}] 顶层 list,自动解包(否则 .get() 会崩溃整批)
            if isinstance(data, list):
                if len(data) == 1 and isinstance(data[0], dict):
                    log.info(f"  [{src_md.name}] JSON 顶层是 list,自动解包为 dict")
                    data = data[0]
                else:
                    log.warning(f"  [{src_md.name}] JSON 顶层是 list({len(data)} 项),无法渲染,已剥离")
                    replacement = ""
                    text = text[:blk.start] + ("\n" if blk.start > 0 and not text[:blk.start].endswith("\n") else "") + replacement + text[blk.end:]
                    continue

            ctype = classify_json(data)

            if ctype == "table":
                # 表格：转为 markdown 表格，不渲染成图片
                md_table = json_table_to_markdown(data)
                if md_table:
                    ci = type_counters.get("table", 0)
                    suffix = "" if ci == 0 else f"_{ci}"
                    backup = data_dir / f"{stem}.table{suffix}.json"
                    backup.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    type_counters["table"] = ci + 1
                    replacement = md_table
                    log.info(f"  [{src_md.name}] JSON 表格 → markdown 表格 ({data.get('title', '')})")
                else:
                    replacement = ""
                    log.warning(f"  [{src_md.name}] JSON 表格转换失败，已剥离")

            elif ctype != "other":
                # 图表：适配数据格式 → 渲染 PNG
                adapter = _CHART_ADAPTERS.get(ctype)
                render_data = adapter(data) if adapter else data
                _data_ok, _data_why = validate_chart_data(ctype, render_data)
                if not _data_ok:
                    log.warning(f"  [{src_md.name}] {ctype} 数据不足"
                                f"(渲染将为空图): {_data_why}")
                renderer = _CHART_RENDERERS.get(ctype)
                if renderer:
                    ci = type_counters.get(ctype, 0)
                    suffix = "" if ci == 0 else f"_{ci}"
                    backup = data_dir / f"{stem}.{ctype}{suffix}.json"
                    backup.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    png_name = f"{stem}_{ctype}{suffix}.png"
                    png_path = charts_dir / png_name
                    chart_title = data.get("title") or render_data.get("title") or \
                                  _CHART_TITLES.get(ctype, ctype)
                    ok = renderer(render_data, png_path, chart_title, log, chart_style)
                    if ok:
                        rel = Path("..") / charts_dir.name / png_name
                        replacement = f"\n![{chart_title}]({rel.as_posix()})\n"
                    else:
                        replacement = ""
                        log.warning(f"  [{src_md.name}] {ctype} 渲染失败，已剥离")
                    type_counters[ctype] = ci + 1
                else:
                    replacement = ""
                    log.warning(f"  [{src_md.name}] 类型 {ctype} 无渲染器，已剥离")

            else:
                # other：从输出中删除（防止原始 JSON 出现在文档中导致废标），备份供调试
                data_dir.mkdir(parents=True, exist_ok=True)
                backup = data_dir / f"{stem}.other{'' if other_idx == 0 else '_' + str(other_idx)}.json"
                backup.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                other_idx += 1
                replacement = ""
                log.info(f"  [{src_md.name}] 未识别 JSON (type={data.get('type', '')})，已剥离")

        elif blk.lang == "mermaid":
            backup = data_dir / f"{stem}.mermaid{'' if mermaid_idx == 0 else '_' + str(mermaid_idx)}.mmd"
            backup.write_text(blk.content, encoding="utf-8")
            mermaid_idx += 1
            log.info(f"  [{src_md.name}] 剥离 mermaid → {backup.name}")
            replacement = ""

        elif blk.lang == "" and looks_like_ascii_art(blk.content):
            log.info(f"  [{src_md.name}] 剥离疑似 ASCII 字符画块")
            replacement = ""

        if replacement is not None:
            text = text[:blk.start] + ("\n" if blk.start > 0 and not text[:blk.start].endswith("\n") else "") + replacement + text[blk.end:]

    text = re.sub(r"\n{3,}", "\n\n", text)

    # 剥离非围栏的裸 JSON 对象（LLM 有时不用 ```json 包裹就直接输出 JSON）
    text = _strip_bare_json(text, src_md.name, data_dir, log)

    # 伪横道表格（█ 字符模拟横道条的 pipe table）→ 甘特图 PNG
    text = _replace_pseudo_gantt_tables(text, charts_dir, data_dir, stem, log, chart_style)

    # 确保 markdown pipe table 前后有空行（pandoc 要求，否则当纯文本处理）
    text = _ensure_table_blank_lines(text)

    out = tmp_dir / src_md.name
    out.write_text(text, encoding="utf-8")
    return out


# ─── 批量预处理入口 ───────────────────────────────────────────────────

def preprocess_chapter_dir(
    chapter_dir: Path,
    md_files: list[Path],
    log: logging.Logger,
    variant_id: str = "",
    add_chapter_prefix: bool = True,
    chart_style: dict | None = None,
    heading_style: str = "",
) -> list[Path]:
    """对一个章节目录（output/ 或 output/VXX/）做批量预处理。

    返回临时 md 路径列表，供 pandoc 读取。原 md 文件不动。
    heading_style: A-L 编号风格，空字符串则自动基于 variant_id 随机分配。
    """
    tmp_dir = chapter_dir / ".assemble_tmp"
    charts_dir = chapter_dir / "charts"
    data_dir = chapter_dir / "charts_data"

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    charts_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 编号风格：指定则用指定值,否则按变体序号确定性派发(N 变体互不相同)
    if not heading_style:
        _vm = re.match(r"[Vv](\d+)", variant_id or "")
        heading_style = assign_heading_style((int(_vm.group(1)) - 1) if _vm else 0)
    style_name = _HEADING_STYLES.get(heading_style, _HEADING_STYLES["B"])[0]
    log.info(f"标题编号风格: {heading_style}({style_name})  variant={variant_id}")

    log.info(f"图表预处理: {chapter_dir.name}  ({len(md_files)} 个章节)")
    tmp_paths = []
    for idx, src in enumerate(md_files, start=1):
        if add_chapter_prefix:
            m = re.match(r"(\d+)[_-]", src.stem)
            chapter_idx = int(m.group(1)) if m else idx
        else:
            chapter_idx = 0
        tmp = preprocess_md(src, tmp_dir, charts_dir, data_dir, variant_id, log,
                            chapter_idx=chapter_idx, chart_style=chart_style,
                            heading_style=heading_style)
        tmp_paths.append(tmp)
    return tmp_paths


# ─── CLI ─────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="章节 md 预处理：剥离 JSON/mermaid、渲染进度甘特图")
    p.add_argument("chapter_dir", help="章节目录（output/ 或 output/VXX/）")
    p.add_argument("--variant", default="", help="variant ID，用于日志")
    args = p.parse_args()

    chapter_dir = Path(args.chapter_dir).expanduser().resolve()
    log = make_script_logger("bid_charts", chapter_dir / "charts.log")

    md_files = sorted(chapter_dir.glob("[0-9][0-9]_*.md"))
    if not md_files:
        log.error(f"{chapter_dir} 下未找到章节 md")
        return 2

    preprocess_chapter_dir(chapter_dir, md_files, log, args.variant)
    log.info("预处理完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())

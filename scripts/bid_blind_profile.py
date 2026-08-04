#!/usr/bin/env python3
"""bid_blind_profile.py — 地区专项技术暗标 profile（首个：重庆）。

职责：
  1. 检测/加载地区暗标 profile（configs/bid_blind_profiles.json）
  2. 把 profile 的招标硬性格式要求覆盖进 fmt（优先级最高，全部计入 specified）
  3. 提供组装后 DOCX/PDF/PNG 的专项合规检测函数（全部 degrade-safe：
     缺依赖或执行失败时 WARN 跳过、返回空表，绝不 fail）

激活条件（detect_blind_profile）：
  - 项目为暗标（bid_common.needs_blind_scan 为真）——非暗标项目一律 None；
  - 且 rules yaml 显式 `blind_profile: <name>`，或 profile.region_keywords
    命中 project_facts.json 的 location / 招标文件文本；
  - rules yaml 写 `blind_profile: none` 可显式关闭。

每个 check_* 返回 list[dict]（{rule, detail, location}），空 = 通过。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bid_common import canonical_rules_path, needs_blind_scan  # noqa: E402

# ─── 可选依赖（缺失时对应检测优雅跳过）────────────────────────────
try:
    from PIL import Image as _PILImage
except Exception:  # noqa: BLE001
    _PILImage = None

try:
    import fitz as _fitz  # PyMuPDF
except Exception:  # noqa: BLE001
    _fitz = None


def pymupdf_available() -> bool:
    return _fitz is not None


def pil_available() -> bool:
    return _PILImage is not None


# ─── 配置加载 ────────────────────────────────────────────────────

def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "configs" / "bid_blind_profiles.json"


def load_profiles() -> dict:
    """加载全部地区 profile；配置缺失/损坏时优雅返回 {}。"""
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    profiles = data.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def load_profile(name: str | None) -> dict | None:
    """按名加载单个 profile；name 为空/未知/配置缺失时返回 None。"""
    if not name:
        return None
    profile = load_profiles().get(name)
    return profile if isinstance(profile, dict) else None


# ─── profile 检测 ────────────────────────────────────────────────

_BLIND_PROFILE_KEY_RE = re.compile(r"^\s*blind_profile\s*[：:]\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", re.M)
_PROFILE_OFF_VALUES = {"none", "off", "false", "disable", "disabled"}


def _facts_location(extracted_dir: Path) -> str:
    """从 extracted/project_facts.json 读 location（建设地点）字段值。"""
    facts_path = Path(extracted_dir) / "project_facts.json"
    if not facts_path.exists():
        return ""
    try:
        payload = json.loads(facts_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else payload
    loc = facts.get("location") if isinstance(facts, dict) else None
    if isinstance(loc, dict):
        loc = loc.get("value")
    return str(loc or "")


def detect_blind_profile(extracted_dir) -> str | None:
    """判定应激活的地区暗标 profile 名；不激活返回 None。

    判定顺序：
      1. 非暗标项目（needs_blind_scan 为假）→ None
      2. rules 显式 `blind_profile: none/off` → None（显式关闭，最高优先）
      3. rules 显式 `blind_profile: <name>` 且配置中存在 → name
      4. region_keywords 命中 facts location 或招标文件文本 → 首个命中的 profile
    """
    extracted_dir = Path(extracted_dir)
    if not needs_blind_scan(extracted_dir):
        return None

    rules_path = canonical_rules_path(extracted_dir)
    rules_text = ""
    if rules_path.exists():
        try:
            rules_text = rules_path.read_text(encoding="utf-8")
        except OSError:
            rules_text = ""

    m = _BLIND_PROFILE_KEY_RE.search(rules_text)
    if m:
        declared = m.group(1).strip()
        if declared.lower() in _PROFILE_OFF_VALUES:
            return None
        if declared in load_profiles():
            return declared
        return None  # 显式指定了未知 profile：不猜测、不兜底到地区命中

    haystacks = [_facts_location(extracted_dir), rules_text]
    for name, profile in load_profiles().items():
        keywords = profile.get("region_keywords") or []
        if any(kw and any(kw in h for h in haystacks) for kw in keywords):
            return name
    return None


# ─── fmt 覆盖（招标硬性要求，优先级最高）──────────────────────────

def apply_profile_to_fmt(fmt: dict, specified: set, profile: dict, log: logging.Logger) -> None:
    """把 profile 的 fmt_overrides 写入 fmt 并全部标记 specified。

    - page_size 同步展开 page_width_cm / page_height_cm（走 bid_common.PAGE_SIZE_CM，
      不硬编码尺寸数字）；
    - headings_follow_body：H1-H3 字号/字体全部跟随正文、bold_h1=False（重庆：
      含所有级别标题全文四号仿宋不加粗）；
    - all_text_no_bold：打 fmt["_all_text_no_bold"] 标记，assemble 后处理据此
      连标题 run 也剥粗体。
    """
    from bid_common import PAGE_SIZE_CM

    overrides = profile.get("fmt_overrides") or {}
    for key, val in overrides.items():
        fmt[key] = val
        specified.add(key)
        if key == "page_size":
            dims = PAGE_SIZE_CM.get(str(val).upper())
            if dims:
                fmt["page_width_cm"], fmt["page_height_cm"] = dims
                specified.update(("page_width_cm", "page_height_cm"))
        log.info(f"  [暗标地区 profile] {key} = {val}（招标硬性，计入 specified）")

    if profile.get("headings_follow_body"):
        body_size = fmt["font_size_body"]
        body_font = fmt["font_name_cn"]
        for key in ("font_size_h1", "font_size_h2", "font_size_h3"):
            fmt[key] = body_size
            specified.add(key)
        fmt["font_name_h1"] = body_font
        specified.add("font_name_h1")
        fmt["bold_h1"] = False
        specified.add("bold_h1")
        log.info(
            f"  [暗标地区 profile] headings_follow_body：H1-H3 全部 {body_font} "
            f"{body_size}pt 不加粗（含标题全文无修饰）"
        )

    if profile.get("all_text_no_bold"):
        fmt["_all_text_no_bold"] = True
        log.info("  [暗标地区 profile] all_text_no_bold：后处理将剥除全部 run 粗体（含标题）")


# ─── DOCX 检测（zip 直读，不依赖 python-docx）────────────────────

def _docx_members(docx_path) -> dict[str, str]:
    """读 docx 内全部 xml 成员 → {name: text}；失败返回 {}。"""
    out: dict[str, str] = {}
    try:
        with zipfile.ZipFile(str(docx_path)) as z:
            for name in z.namelist():
                if name.endswith(".xml"):
                    out[name] = z.read(name).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return {}
    return out


_BOLD_OFF_VALUES = {"false", "0", "off", "none"}
_COLOR_OK_VALUES = {"auto", "000000"}


def _iter_paragraph_runs(document_xml: str):
    """逐段落产出 (段落文本前 30 字, run xml)。正则实现，容忍命名空间变体。"""
    for p in re.finditer(r"<w:p\b.*?</w:p>", document_xml, flags=re.DOTALL):
        pxml = p.group(0)
        ptext = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", pxml))[:30]
        for r in re.finditer(r"<w:r\b.*?</w:r>", pxml, flags=re.DOTALL):
            yield ptext, r.group(0)


def check_bold_runs(docx_path, log: logging.Logger | None = None) -> list[dict]:
    """全文无加粗检测：document.xml 中任何 run 带生效的 w:b / w:bCs 即违规。

    无白名单——重庆暗标"不得作加粗等任何修饰"含所有级别标题。
    样式级粗体不在本函数范围（组装侧由 fmt bold_h1=False 保证）。
    """
    findings: list[dict] = []
    xml = _docx_members(docx_path).get("word/document.xml", "")
    if not xml:
        if log:
            log.warning(f"  [bold 检测] 无法读取 document.xml：{docx_path}")
        return findings
    for ptext, rxml in _iter_paragraph_runs(xml):
        m = re.search(r"<w:(b|bCs)\b([^>]*)>", rxml)
        if not m:
            continue
        vm = re.search(r'w:val="([^"]+)"', m.group(2))
        if vm and vm.group(1).strip().lower() in _BOLD_OFF_VALUES:
            continue  # 显式关闭的粗体标记，不算违规
        findings.append({
            "rule": "all_text_no_bold",
            "detail": f"run 存在加粗修饰 <w:{m.group(1)}>（重庆暗标：全文含标题不得加粗）",
            "location": ptext or "(空段落)",
        })
    return findings


def check_color_runs(docx_path, log: logging.Logger | None = None) -> list[dict]:
    """文字彩色检测：document.xml 中 w:color w:val 不在 (auto, 000000) 即违规。"""
    findings: list[dict] = []
    xml = _docx_members(docx_path).get("word/document.xml", "")
    if not xml:
        if log:
            log.warning(f"  [彩色文字检测] 无法读取 document.xml：{docx_path}")
        return findings
    for ptext, rxml in _iter_paragraph_runs(xml):
        for cm in re.finditer(r'<w:color\s+w:val="([^"]+)"', rxml):
            val = cm.group(1).strip().lower()
            if val in _COLOR_OK_VALUES:
                continue
            findings.append({
                "rule": "no_color",
                "detail": f"run 文字颜色 w:val={cm.group(1)}（重庆暗标：文字不得使用彩色）",
                "location": ptext or "(空段落)",
            })
    return findings


def check_page_numbers(docx_path, log: logging.Logger | None = None) -> list[dict]:
    """页码检测：footer*.xml / header*.xml 中存在 PAGE 域（instrText / fldSimple）即违规。

    页眉中的页码同样违反"不得编制页码"，故 header 一并扫描。
    """
    findings: list[dict] = []
    members = _docx_members(docx_path)
    hf = {n: x for n, x in members.items()
          if re.match(r"word/(footer|header)\d*\.xml$", n)}
    if not hf:
        return findings
    for name, xml in sorted(hf.items()):
        for m in re.finditer(r"<w:instrText[^>]*>([^<]*)</w:instrText>", xml):
            if re.search(r"\bPAGE\b", m.group(1)):
                findings.append({
                    "rule": "no_page_numbers",
                    "detail": f"{name} 存在 PAGE 页码域（重庆暗标：不得编制页码）",
                    "location": name,
                })
                break
        else:
            for m in re.finditer(r'<w:fldSimple\b[^>]*w:instr="([^"]*)"', xml):
                if re.search(r"\bPAGE\b", m.group(1)):
                    findings.append({
                        "rule": "no_page_numbers",
                        "detail": f"{name} 存在 fldSimple PAGE 页码域（重庆暗标：不得编制页码）",
                        "location": name,
                    })
                    break
    return findings


# ─── 图片彩色检测（PIL）──────────────────────────────────────────

def _image_color_spread(img) -> int:
    """RGB 三通道最大像素级跨度（max(r,g,b)-min(r,g,b)）。纯灰度图 ≈ 0。

    缩样到 256px 内以控制耗时。
    """
    im = img.convert("RGB")
    im.thumbnail((256, 256))
    spread = 0
    for r, g, b in im.getdata():
        d = max(r, g, b) - min(r, g, b)
        if d > spread:
            spread = d
    return spread


def check_color_images(docx_or_png_dir, threshold: int | None = None,
                       log: logging.Logger | None = None) -> list[dict]:
    """嵌入图片/图表 PNG 彩色检测：任一像素通道跨度超阈值即彩色违规。

    入参可为组装后 docx（扫 word/media/*）或 PNG 目录（扫 *.png/*.jpg）。
    PIL 缺失时 WARN 跳过（返回空表）。
    """
    if _PILImage is None:
        if log:
            log.warning("  [彩色图片检测] PIL 不可用，跳过（不判违规）")
        return []
    path = Path(docx_or_png_dir)
    threshold = threshold if threshold is not None else 16
    findings: list[dict] = []

    targets: list[tuple[str, bytes]] = []
    if path.is_dir():
        for p in sorted(path.glob("*")):
            if p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                try:
                    targets.append((p.name, p.read_bytes()))
                except OSError:
                    continue
    elif path.is_file() and path.suffix.lower() == ".docx":
        try:
            with zipfile.ZipFile(str(path)) as z:
                for name in z.namelist():
                    if name.startswith("word/media/") and \
                            name.lower().endswith((".png", ".jpg", ".jpeg")):
                        targets.append((name, z.read(name)))
        except Exception as e:  # noqa: BLE001
            if log:
                log.warning(f"  [彩色图片检测] docx 读取失败，跳过：{e}")
            return []
    else:
        return []

    for name, blob in targets:
        try:
            img = _PILImage.open(BytesIO(blob))
            img.load()
            spread = _image_color_spread(img)
        except Exception as e:  # noqa: BLE001
            if log:
                log.warning(f"  [彩色图片检测] 图片解析失败 {name}：{e}（跳过该图）")
            continue
        if spread > threshold:
            findings.append({
                "rule": "no_color_images",
                "detail": f"图片 {name} 检出彩色（通道跨度 {spread} > 阈值 {threshold}；"
                          f"重庆暗标：图表不得使用彩色）",
                "location": name,
            })
    return findings


def grayscale_png_dir(png_dir, log: logging.Logger | None = None) -> int:
    """把目录下全部 PNG 就地转为灰度（convert("L")），返回处理张数。

    组装侧在图表嵌入前调用：比改渲染器主题简单且保证"图表不得使用彩色"。
    PIL 缺失或目录不存在时返回 0（不抛错）。
    """
    if _PILImage is None:
        if log:
            log.warning("  [图表灰度化] PIL 不可用，跳过")
        return 0
    png_dir = Path(png_dir)
    if not png_dir.is_dir():
        return 0
    count = 0
    for p in sorted(png_dir.glob("*.png")):
        try:
            img = _PILImage.open(p)
            img.load()
            img.convert("L").save(p, format="PNG", optimize=True)
            count += 1
        except Exception as e:  # noqa: BLE001
            if log:
                log.warning(f"  [图表灰度化] {p.name} 处理失败（跳过）：{e}")
    if count and log:
        log.info(f"  [图表灰度化] {count} 张 PNG 已转灰度（no_color 招标硬性要求）")
    return count


# ─── PDF 白页/倒页检测（PyMuPDF，可选）────────────────────────────

def check_blank_and_rotated_pages(pdf_path, log: logging.Logger | None = None) -> list[dict]:
    """白页/倒页检测：白页 = 页面文本与图像均空；倒页 = page.rotation ≠ 0。

    PyMuPDF 缺失 / PDF 不存在时 WARN 跳过（返回空表）。
    PDF 来源由调用方负责（可用现有 Word AppleScript 导出，失败则跳过）。
    """
    if _fitz is None:
        if log:
            log.warning("  [白页/倒页检测] PyMuPDF 不可用，跳过（不判违规）")
        return []
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        if log:
            log.warning(f"  [白页/倒页检测] PDF 不存在，跳过：{pdf_path}")
        return []
    findings: list[dict] = []
    try:
        doc = _fitz.open(str(pdf_path))
    except Exception as e:  # noqa: BLE001
        if log:
            log.warning(f"  [白页/倒页检测] PDF 打开失败，跳过：{e}")
        return []
    try:
        for i, page in enumerate(doc, start=1):
            if page.rotation != 0:
                findings.append({
                    "rule": "no_rotated_pages",
                    "detail": f"第 {i} 页旋转 {page.rotation}°（重庆暗标：不得出现倒页）",
                    "location": f"page {i}",
                })
            text = (page.get_text() or "").strip()
            has_image = bool(page.get_images(full=True))
            if not text and not has_image:
                findings.append({
                    "rule": "no_blank_pages",
                    "detail": f"第 {i} 页文本与图像均空（重庆暗标：不得出现白页）",
                    "location": f"page {i}",
                })
    finally:
        doc.close()
    return findings


# ─── 页数 advisory（永不 hard-fail）──────────────────────────────

def check_advisory_page_limit(page_count: int | None, profile: dict) -> list[dict]:
    """200 页参考上限：超限只产出 WARN 级条目，绝不影响退出码。

    重庆招标明确"不得将页数作为评审因素"（profile.pages_not_eval_factor）。
    """
    limit = int(profile.get("advisory_max_pages") or 0)
    if not limit or not page_count or page_count <= limit:
        return []
    return [{
        "rule": "advisory_max_pages",
        "severity": "warn",
        "detail": f"实际 {page_count} 页 > 参考上限 {limit} 页"
                  f"（招标明确页数不作为评审因素，仅提示，不判废）",
        "location": "全文",
    }]

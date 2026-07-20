import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bid_compliance import check_plan_alignment


def test_plan_alignment_ignores_leading_star_marker(tmp_path):
    md = tmp_path / "01_分项施工技术方案.md"
    md.write_text("# ★分项施工技术方案\n\n正文内容", encoding="utf-8")
    rows = [{
        "num": "01",
        "filename": md.name,
        "title": "分项施工技术方案",
        "score": 5,
        "target_chars": 0,
    }]

    result = check_plan_alignment(rows, [md], {md.name: 4}, logging.getLogger("test"))

    assert result[0]["title_match"] is True

"""工期合规门的绝对日期基准测试。"""

import logging
from datetime import date

from bid_compliance import check_schedule_duration


def test_schedule_date_before_earliest_start_is_violation(tmp_path):
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        """# 施工进度计划

```json
{
  "type": "schedule",
  "tasks": [
    {"name": "施工准备", "start": "2026-07-01", "end": "2026-07-03"}
  ]
}
```
""",
        encoding="utf-8",
    )
    result = check_schedule_duration(
        [md], tender_days=60, log=logging.getLogger("test"),
        earliest_start=date(2026, 7, 23),
    )
    assert result["violations"]
    assert result["violations"][0]["kind"] == "计划开工基准"

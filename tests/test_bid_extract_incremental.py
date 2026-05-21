import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bid_extract import _file_md5, _EXTRACT_CACHE_VERSION


def test_file_md5_stable(tmp_path):
    """同一文件多次取 md5 必须一致(增量判定的基础)。"""
    f = tmp_path / "a.txt"
    f.write_text("招标文件内容", encoding="utf-8")
    h1 = _file_md5(f)
    assert _file_md5(f) == h1


def test_file_md5_detects_change(tmp_path):
    """文件内容变化后 md5 必须变(否则增量会漏掉已改文件)。"""
    f = tmp_path / "a.txt"
    f.write_text("原始内容", encoding="utf-8")
    h1 = _file_md5(f)
    f.write_text("原始内容 + 修改", encoding="utf-8")
    assert _file_md5(f) != h1


def test_cache_version_is_int():
    """缓存版本号必须是整数,提取逻辑变更时 +1 使旧缓存失效。"""
    assert isinstance(_EXTRACT_CACHE_VERSION, int)

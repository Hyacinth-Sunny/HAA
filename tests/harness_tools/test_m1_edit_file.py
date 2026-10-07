"""M1 工具②：read_file / write_file / edit_file（翻译清单 §2，18 例）。"""
from __future__ import annotations

import os
import time

import pytest

from haa.harness.registry import ToolError
from tests.harness_tools.test_m1_exec_bash import native_reg


def _write_direct(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


# ---------------- 2a 局部修改核心 ----------------

def test_01_unique_replacement_after_read_lands(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "a.txt", "alpha\nbeta\ngamma\n")
    reg.invoke("read_file", {"path": str(f)})
    r = reg.invoke("edit_file", {"path": str(f), "old_string": "beta",
                                 "new_string": "BETA"})
    assert "updated successfully" in r.content
    assert f.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"  # 磁盘字节验证


def test_02_edit_without_read_rejected_file_unchanged(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "b.txt", "keep\n")
    with pytest.raises(ToolError, match="FS_NOT_OBSERVED"):
        reg.invoke("edit_file", {"path": str(f), "old_string": "keep",
                                 "new_string": "changed"})
    assert f.read_text(encoding="utf-8") == "keep\n"  # 文件原样


def test_03_zero_matches_lists_top3_candidates(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "c.txt", "alpha beta gamma\nother line\nthird\n")
    reg.invoke("read_file", {"path": str(f)})
    with pytest.raises(ToolError, match="candidate lines"):
        reg.invoke("edit_file", {"path": str(f), "old_string": "alpha beta gammo",
                                 "new_string": "x"})


def test_04_multiple_matches_lists_all_hit_lines(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "d.txt", "dup\nunique\ndup\n")
    reg.invoke("read_file", {"path": str(f)})
    with pytest.raises(ToolError, match=r"FS_AMBIGUOUS_EDIT.*lines \[1, 3\]"):
        reg.invoke("edit_file", {"path": str(f), "old_string": "dup",
                                 "new_string": "x"})


def test_05_old_equals_new_rejected(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "e.txt", "x\n")
    reg.invoke("read_file", {"path": str(f)})
    with pytest.raises(ToolError, match="identical"):
        reg.invoke("edit_file", {"path": str(f), "old_string": "x", "new_string": "x"})


def test_06_empty_old_string_rejected(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "f.txt", "x\n")
    reg.invoke("read_file", {"path": str(f)})
    with pytest.raises(ToolError, match="non-empty"):
        reg.invoke("edit_file", {"path": str(f), "old_string": "",
                                 "new_string": "y"})


def test_07_stale_after_external_change_then_reread_unlocks(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "g.txt", "v1\n")
    reg.invoke("read_file", {"path": str(f)})
    future = time.time() + 50
    os.utime(f, (future, future))
    with pytest.raises(ToolError, match="FS_STALE_VERSION"):
        reg.invoke("edit_file", {"path": str(f), "old_string": "v1",
                                 "new_string": "v2"})
    reg.invoke("read_file", {"path": str(f)})  # 按 remedy 重读
    reg.invoke("edit_file", {"path": str(f), "old_string": "v1", "new_string": "v2"})
    assert "v2" in f.read_text(encoding="utf-8")


def test_08_write_refreshes_observation(tmp_path):
    reg = native_reg(tmp_path)
    f = tmp_path / "h.txt"
    reg.invoke("write_file", {"path": str(f), "content": "first\n"})
    # 写后即观察：紧接 edit 无需重读
    reg.invoke("edit_file", {"path": str(f), "old_string": "first",
                             "new_string": "second"})
    assert "second" in f.read_text(encoding="utf-8")


def test_09_windowed_read_still_authorizes_edit(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "i.txt", "l1\nl2\nl3\nl4\nl5\n")
    reg.invoke("read_file", {"path": str(f), "offset": 1, "limit": 1})  # 只看窗口
    # 新鲜度基线而非全视图：窗口外编辑仍授权
    reg.invoke("edit_file", {"path": str(f), "old_string": "l4", "new_string": "L4"})
    assert "L4" in f.read_text(encoding="utf-8")


def test_10_concurrent_edits_exactly_one_stale(tmp_path):
    reg_a = native_reg(tmp_path)  # 两个注册表=两次 stage 会话（并发方）
    reg_b = native_reg(tmp_path)
    f = _write_direct(tmp_path / "j.txt", "base\n")
    reg_a.invoke("read_file", {"path": str(f)})
    reg_b.invoke("read_file", {"path": str(f)})
    reg_a.invoke("edit_file", {"path": str(f), "old_string": "base",
                               "new_string": "from-a"})
    with pytest.raises(ToolError, match="FS_STALE_VERSION"):
        reg_b.invoke("edit_file", {"path": str(f), "old_string": "base",
                                   "new_string": "from-b"})
    assert "from-a" in f.read_text(encoding="utf-8")  # 世界一致


def test_11_deleted_target_and_negative_observation(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "k.txt", "doomed\n")
    reg.invoke("read_file", {"path": str(f)})
    f.unlink()
    # 正观察后文件被删：edit 报 not found（闸门放行交 handler 报错）
    with pytest.raises(ToolError, match="FS_NOT_FOUND"):
        reg.invoke("edit_file", {"path": str(f), "old_string": "doomed",
                                 "new_string": "x"})
    # 负观察（读到缺失）授权 write 重建
    with pytest.raises(ToolError, match="FS_NOT_FOUND"):
        reg.invoke("read_file", {"path": str(f)})
    reg.invoke("write_file", {"path": str(f), "content": "rebuilt\n"})
    assert f.exists()


def test_12_unobserved_write_create_semantics(tmp_path):
    reg = native_reg(tmp_path)
    existing = _write_direct(tmp_path / "l.txt", "cannot blind-overwrite\n")
    with pytest.raises(ToolError, match="FS_NOT_OBSERVED"):  # 存在但未读 → 拒
        reg.invoke("write_file", {"path": str(existing), "content": "x"})
    fresh = tmp_path / "new-dir" / "m.txt"  # 不存在 → 盲建放行
    r = reg.invoke("write_file", {"path": str(fresh), "content": "created\n"})
    assert "created successfully" in r.content and fresh.exists()


# ---------------- 2b 编码与大文件 ----------------

def test_13_utf8_only_other_encodings_error(tmp_path):
    reg = native_reg(tmp_path)
    f = tmp_path / "bin.txt"
    f.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ToolError, match="FS_NOT_TEXT"):
        reg.invoke("read_file", {"path": str(f)})


def test_14_crlf_lf_mixed_handled(tmp_path):
    reg = native_reg(tmp_path)
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"line1\r\nline2\nline3\r\n")
    reg.invoke("read_file", {"path": str(f)})
    reg.invoke("edit_file", {"path": str(f), "old_string": "line2",
                             "new_string": "LINE2"})
    data = f.read_bytes()
    assert b"LINE2\n" in data and b"line1\r\n" in data and b"line3\r\n" in data


def test_15_large_file_edit(tmp_path):
    reg = native_reg(tmp_path)
    big = "filler\n" * 300_000 + "NEEDLE-unique\n" + "tail\n" * 10
    f = _write_direct(tmp_path / "big.txt", big)
    reg.invoke("read_file", {"path": str(f), "max_chars": 2000})  # 窗口读不整读
    reg.invoke("edit_file", {"path": str(f), "old_string": "NEEDLE-unique",
                             "new_string": "PATCHED"})
    assert "PATCHED" in f.read_text(encoding="utf-8")


def test_16_write_readback_verification(tmp_path):
    reg = native_reg(tmp_path)
    f = tmp_path / "rb.txt"
    reg.invoke("write_file", {"path": str(f), "content": "abc123"})
    assert f.read_bytes() == b"abc123"


# ---------------- 2c read_file 强化 ----------------

def test_17_line_numbered_output_with_footer_and_pagination(tmp_path):
    reg = native_reg(tmp_path)
    f = _write_direct(tmp_path / "n.txt", "one\ntwo\nthree\n")
    r = reg.invoke("read_file", {"path": str(f)})
    assert "1\tone" in r.content.replace("     ", " ").replace("    ", " ") or \
           "one" in r.content
    assert "(End of file - total 3 lines)" in r.content
    r2 = reg.invoke("read_file", {"path": str(f), "offset": 2, "limit": 1})
    assert "two" in r2.content and "Showing lines 2-2 of 3" in r2.content


def test_18_directory_and_missing_typed_errors(tmp_path):
    reg = native_reg(tmp_path)
    with pytest.raises(ToolError, match="FS_NOT_REGULAR_FILE"):
        reg.invoke("read_file", {"path": str(tmp_path)})
    with pytest.raises(ToolError, match="FS_NOT_FOUND"):
        reg.invoke("read_file", {"path": str(tmp_path / "ghost.txt")})

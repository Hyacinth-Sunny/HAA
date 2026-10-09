"""沙箱相对路径逃逸修复的回归测试（GPT-6.1-Sol /init 发现，P0 安全）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from haa.harness.registry import ToolError, ToolRegistry
from haa.harness.tools.native import apply_native_tools


def _reg(tmp_path):
    reg = ToolRegistry()
    apply_native_tools(reg, allowed_roots=(tmp_path,))
    return reg


class TestSandboxRelativePathEscape:
    """相对路径必须锚定沙箱根——不能读写沙箱外的文件。"""

    def test_relative_read_cannot_reach_outside(self, tmp_path):
        """模型用相对路径试图读沙箱外的文件 → 被沙箱锚定到沙箱内 → 不存在。"""
        reg = _reg(tmp_path)
        # 在 tmp_path 外放一个文件（模拟 data/smoke10_deepseek.env）
        secret = tmp_path.parent / "secret_file_for_escape_test.env"
        secret.write_text("FAKE_KEY=sk-should-never-be-readable")
        try:
            # 相对路径 "../secret_file_for_escape_test.env"
            # 锚定后 = tmp_path/../secret_file... = 沙箱外 → FS_NOT_FOUND
            with pytest.raises(ToolError, match="FS_NOT_FOUND|outside"):
                reg.invoke("read_file",
                           {"path": "../secret_file_for_escape_test.env"})
        finally:
            secret.unlink(missing_ok=True)

    def test_relative_write_lands_in_sandbox(self, tmp_path):
        """相对路径写入 → 落在沙箱内（不是进程 CWD）。"""
        reg = _reg(tmp_path)
        r = reg.invoke("write_file",
                       {"path": "test_relative_write.md", "content": "sandbox"})
        # 文件应该在 tmp_path 下（沙箱内），不在 CWD
        assert (tmp_path / "test_relative_write.md").exists()
        assert not Path("test_relative_write.md").exists()  # 不在 CWD

    def test_absolute_outside_still_rejected(self, tmp_path):
        """绝对路径指向沙箱外 → 检查层白名单拒绝（原有行为）。"""
        reg = _reg(tmp_path)
        with pytest.raises(ToolError):
            reg.invoke("read_file", {"path": "/etc/passwd"})

    def test_relative_inside_sandbox_works(self, tmp_path):
        """沙箱内相对路径正常工作。"""
        reg = _reg(tmp_path)
        (tmp_path / "inside.md").write_text("inside content")
        r = reg.invoke("read_file", {"path": "inside.md"})
        assert "inside content" in r.content

    def test_nested_relative_write(self, tmp_path):
        """嵌套子目录的相对路径也锚定到沙箱内。"""
        reg = _reg(tmp_path)
        reg.invoke("write_file",
                   {"path": "sub/dir/nested.md", "content": "nested"})
        assert (tmp_path / "sub" / "dir" / "nested.md").exists()

"""PDF 提取工具测试（mock pdftotext subprocess）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from haa.llm.pdf_utils import extract_pdf_text
from haa.llm.tools import ToolError


def _ok_proc(stdout="PDF text", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


def test_extract_pdf_text_layout(tmp_path):
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with patch("haa.llm.pdf_utils.subprocess.run", return_value=_ok_proc("Hello PDF world")):
        result = extract_pdf_text(str(pdf))
        assert "Hello PDF world" in result


def test_extract_pdf_not_found():
    with pytest.raises(ToolError):
        extract_pdf_text("/nonexistent/file.pdf")


def test_extract_pdf_truncate(tmp_path):
    pdf = tmp_path / "big.pdf"
    pdf.write_bytes(b"%PDF")
    long_text = "A" * 1000
    with patch("haa.llm.pdf_utils.subprocess.run", return_value=_ok_proc(long_text)):
        result = extract_pdf_text(str(pdf), max_chars=50)
        assert len(result) <= 80  # 50 + truncation marker
        assert "[... truncated ...]" in result


def test_extract_pdf_pdftotext_fail(tmp_path):
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"not pdf")
    with patch("haa.llm.pdf_utils.subprocess.run", return_value=_ok_proc("", returncode=1)):
        with pytest.raises(ToolError):
            extract_pdf_text(str(pdf))


def test_read_pdf_via_registry(tmp_path):
    """read_pdf handler via ToolRegistry（本地路径沙箱）。"""
    from haa.config import ToolsConfig
    from haa.llm.tools import ToolRegistry

    reg = ToolRegistry(ToolsConfig(), campaigns_dir=str(tmp_path), allowed_roots=[])
    # 写一个假 PDF
    pdf_dir = tmp_path / "camp1"
    pdf_dir.mkdir()
    (pdf_dir / "paper.pdf").write_bytes(b"%PDF-1.4")
    with patch("haa.llm.pdf_utils.subprocess.run", return_value=_ok_proc("Extracted text")):
        out = reg.execute("read_pdf", {"source": "paper.pdf"}, stage_name="SEEK", campaign_id="camp1")
        assert "Extracted text" in out

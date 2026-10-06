"""PDF 文本提取——使用 poppler-utils 的 pdftotext 命令行工具。

不依赖 Python PDF 库（pdfplumber/PyPDF2），直接调系统 pdftotext，
对学术论文排版处理最稳定。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from haa.llm.tools import ToolError


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n\n[... truncated ...]"


def extract_pdf_text(pdf_path: str, max_chars: int = 50000) -> str:
    """从本地 PDF 提取文本（先 -layout 保留排版，失败 fallback 普通模式）。"""
    p = Path(pdf_path)
    if not p.exists() or not p.is_file():
        raise ToolError(f"read_pdf: file not found: {pdf_path!r}")
    for mode_args in (["-layout"], []):
        try:
            proc = subprocess.run(
                ["pdftotext"] + mode_args + [str(p), "-"],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return _truncate(proc.stdout.strip(), max_chars)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    raise ToolError(f"read_pdf: pdftotext failed for {pdf_path!r}")


def extract_pdf_from_url(url: str, max_chars: int = 50000, timeout: int = 30) -> str:
    """下载 URL 的 PDF 并提取文本。"""
    import urllib.error
    import urllib.request

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HAA/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                tmp.write(resp.read())
            tmp_path = tmp.name
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"read_pdf: download failed: {exc}") from exc
    try:
        return extract_pdf_text(tmp_path, max_chars)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

"""LaTeX 编译器——迁移自 HM-Pro compiler.py（v0.9）。

编译序列：``pdflatex × 3 + bibtex``（条件性）：
::

    pdflatex -interaction=nonstopmode -halt-on-error main.tex
    bibtex main                          # 仅当 .aux 含 \\bibdata
    pdflatex -interaction=nonstopmode -halt-on-error main.tex
    pdflatex -interaction=nonstopmode -halt-on-error main.tex

三次 pdflatex 保证交叉引用解析。bibtex 仅在第一次 pdflatex 产出含
``\\bibdata`` 的 ``.aux`` 文件时运行（论文有参考文献才需要）。

**优雅降级**：pdflatex 未安装时（如本开发环境无 sudo），不报错——
生成 .tex 源文件 + ``COMPILE_REPORT.json`` 记录缺失，用户可手动编译。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("haa.p3.compiler")


@dataclass
class CompileResult:
    """LaTeX 编译结果。"""

    ok: bool
    pdf_path: Path | None = None
    pdf_bytes: int = 0
    commands: list[dict] = field(default_factory=list)
    log: str = ""
    errors: str = ""
    degraded: bool = False  # True = pdflatex 不可用，仅产出 .tex 源
    reason: str = ""


class LatexCompiler:
    """编译 ``paper_dir/main.tex`` → ``main.pdf``。

    Parameters
    ----------
    pdflatex_path:
        可选的 pdflatex 路径覆盖。默认用 ``shutil.which`` 探测。
    """

    FLAGS = ["-interaction=nonstopmode", "-halt-on-error"]

    def __init__(self, pdflatex_path: str | None = None) -> None:
        self.pdflatex_path = pdflatex_path

    def compile(self, paper_dir: str | Path) -> CompileResult:
        """编译 paper 目录下的 main.tex。

        优雅降级：pdflatex 缺失 → 返回 ``degraded=True``，不报错。
        """
        paper = Path(paper_dir)
        main = paper / "main.tex"
        if not main.is_file():
            return CompileResult(ok=False, reason="main.tex missing", errors=str(paper / "main.tex"))

        content = main.read_text(encoding="utf-8").strip()
        if not content:
            return CompileResult(ok=False, reason="main.tex empty")

        # --- Check pdflatex availability ---
        pdflatex = self.pdflatex_path or shutil.which("pdflatex")
        if pdflatex is None:
            logger.warning(
                "pdflatex not found — generating .tex source only (degraded mode). "
                "Install texlive to compile PDFs."
            )
            report = {
                "ok": False,
                "degraded": True,
                "reason": "pdflatex_not_installed",
                "message": "LaTeX source generated but not compiled. "
                           "Install texlive and run: pdflatex main.tex && bibtex main && pdflatex main.tex && pdflatex main.tex",
            }
            (paper / "COMPILE_REPORT.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return CompileResult(
                ok=False, degraded=True, reason="pdflatex_not_installed",
                errors="pdflatex not installed",
            )

        # --- Full compilation: pdflatex × 3 + bibtex ---
        commands: list[dict] = []
        try:
            commands.append(self._run(paper, [pdflatex, *self.FLAGS, "main.tex"]))

            # Conditional bibtex.
            aux = paper / "main.aux"
            if aux.is_file() and "\\bibdata" in aux.read_text(
                encoding="utf-8", errors="replace"
            ):
                bibtex = shutil.which("bibtex")
                if bibtex:
                    commands.append(self._run(paper, [bibtex, "main"]))

            commands.append(self._run(paper, [pdflatex, *self.FLAGS, "main.tex"]))
            commands.append(self._run(paper, [pdflatex, *self.FLAGS, "main.tex"]))

        except RuntimeError as exc:
            # Compilation failed — capture the error log.
            logger.error("LaTeX compilation failed: %s", str(exc)[:500])
            return CompileResult(
                ok=False, commands=commands, errors=str(exc)[-4000:],
                reason="compilation_error",
            )

        # --- Verify PDF ---
        pdf = paper / "main.pdf"
        if not pdf.is_file() or pdf.stat().st_size == 0:
            return CompileResult(
                ok=False, commands=commands, reason="empty_pdf",
                errors="LaTeX completed without a non-empty main.pdf",
            )

        report = {
            "ok": True,
            "commands": commands,
            "pdf_bytes": pdf.stat().st_size,
        }
        (paper / "COMPILE_REPORT.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        logger.info("LaTeX compiled: %s (%d bytes)", pdf, pdf.stat().st_size)
        return CompileResult(
            ok=True, pdf_path=pdf, pdf_bytes=pdf.stat().st_size, commands=commands,
        )

    @staticmethod
    def _run(cwd: Path, command: list[str]) -> dict:
        """Run one compilation command, raise RuntimeError on failure."""
        result = subprocess.run(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = result.stdout.decode("utf-8", "replace")
        if result.returncode != 0:
            raise RuntimeError(
                f"{' '.join(command)} failed (rc={result.returncode}):\n{output[-4000:]}"
            )
        return {"argv": command, "returncode": result.returncode}

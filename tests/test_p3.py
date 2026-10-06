"""Tests for P3: LaTeX paper production pipeline (v0.9).

Tests cover: LatexCompiler (degraded + mock compile), md_to_latex (LLM-driven
conversion + assembly), packager (.zip), and ProjectController.advance_to_p3.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from haa.config import default_config
from haa.models import Brief, Phase, Precursor, Project, ProjectStatus, Track
from haa.p3.latex_compiler import CompileResult, LatexCompiler
from haa.p3.md_to_latex import SECTION_ORDER, assemble_paper, convert_section, _extract_latex_block
from haa.p3.packager import package_deliverable
from haa.project_controller import ProjectController
from haa.state import StateStore


# --------------------------------------------------------------------------- #
#  Mocks
# --------------------------------------------------------------------------- #

class SectionAwareMockLLM:
    """Returns JSON for PAPER_INTEGRATE, LaTeX for MD2LATEX."""

    def __init__(self):
        self.calls: list[str] = []

    def call(self, messages, *, stage=None, **kw):
        self.calls.append(stage or "")
        content = messages[0]["content"]
        if stage == "P3_PAPER_INTEGRATE":
            # Return JSON with all 6 sections.
            return SimpleNamespace(content=json.dumps({
                "abstract": "We present a novel result.",
                "intro": "## Introduction\nThis is the intro.",
                "method": "## Method\nThe method is $f(x) = x^2$.",
                "eval": "## Experiments\nResults: accuracy 95%.",
                "related": "## Related Work\nPrior work exists.",
                "conclusion": "## Conclusion\nWe conclude.",
            }))
        if stage == "P3_MD2LATEX":
            # Return a simple LaTeX block.
            return SimpleNamespace(content="```latex\nThis is the converted section.\n```")
        return SimpleNamespace(content="")


# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_paper_dir(tmp_path):
    return tmp_path / "paper"


# --------------------------------------------------------------------------- #
#  LatexCompiler tests
# --------------------------------------------------------------------------- #

class TestLatexCompiler:

    def test_degraded_when_pdflatex_missing(self, tmp_paper_dir):
        """pdflatex not installed → degraded=True, .tex source preserved."""
        tmp_paper_dir.mkdir()
        (tmp_paper_dir / "main.tex").write_text("\\documentclass{article}\\begin{document}Hi\\end{document}")
        compiler = LatexCompiler()
        with patch("haa.p3.latex_compiler.shutil.which", return_value=None):
            result = compiler.compile(tmp_paper_dir)
        assert result.degraded
        assert not result.ok
        assert "pdflatex_not_installed" in result.reason
        # COMPILE_REPORT.json written.
        report = json.loads((tmp_paper_dir / "COMPILE_REPORT.json").read_text())
        assert report["degraded"]

    def test_missing_main_tex(self, tmp_paper_dir):
        """No main.tex → failure."""
        tmp_paper_dir.mkdir()
        compiler = LatexCompiler()
        result = compiler.compile(tmp_paper_dir)
        assert not result.ok
        assert "missing" in result.reason

    def test_empty_main_tex(self, tmp_paper_dir):
        """Empty main.tex → failure."""
        tmp_paper_dir.mkdir()
        (tmp_paper_dir / "main.tex").write_text("   \n  ")
        compiler = LatexCompiler()
        result = compiler.compile(tmp_paper_dir)
        assert not result.ok
        assert "empty" in result.reason

    def test_successful_compile_with_mock_pdflatex(self, tmp_paper_dir):
        """Mock pdflatex subprocess → 3 passes + PDF created."""
        tmp_paper_dir.mkdir()
        (tmp_paper_dir / "main.tex").write_text("\\documentclass{article}\\begin{document}Hi\\end{document}")

        def fake_run(cmd, **kw):
            # Simulate pdflatex/bibtex success + create main.pdf on last pass.
            if "pdflatex" in str(cmd):
                # Create the PDF on each pdflatex pass (idempotent).
                (tmp_paper_dir / "main.pdf").write_bytes(b"%PDF-1.4 fake")
                (tmp_paper_dir / "main.aux").write_text("\\relax")  # no \bibdata
            return subprocess_CompletedProcess(0)

        compiler = LatexCompiler(pdflatex_path="/fake/pdflatex")
        with patch("haa.p3.latex_compiler.subprocess.run", side_effect=fake_run), \
             patch("haa.p3.latex_compiler.shutil.which", return_value="/fake/pdflatex"):
            result = compiler.compile(tmp_paper_dir)
        assert result.ok
        assert result.pdf_path is not None
        assert result.pdf_bytes > 0
        assert len(result.commands) == 3  # 3 pdflatex passes (no bibtex — no \bibdata)

    def test_compile_with_bibtex(self, tmp_paper_dir):
        """main.aux with \\bibdata triggers bibtex."""
        tmp_paper_dir.mkdir()
        (tmp_paper_dir / "main.tex").write_text("\\documentclass{article}\\begin{document}Hi\\end{document}")

        call_count = [0]

        def fake_run(cmd, **kw):
            call_count[0] += 1
            if "pdflatex" in str(cmd):
                (tmp_paper_dir / "main.pdf").write_bytes(b"%PDF-1.4 fake")
                if call_count[0] == 1:
                    # First pass: write aux with \bibdata.
                    (tmp_paper_dir / "main.aux").write_text("\\bibdata{refs}")
                else:
                    (tmp_paper_dir / "main.aux").write_text("\\bibdata{refs}")
            return subprocess_CompletedProcess(0)

        compiler = LatexCompiler(pdflatex_path="/fake/pdflatex")
        with patch("haa.p3.latex_compiler.subprocess.run", side_effect=fake_run), \
             patch("haa.p3.latex_compiler.shutil.which", return_value="/fake/bibtex"):
            result = compiler.compile(tmp_paper_dir)
        assert result.ok
        assert len(result.commands) == 4  # pdflatex + bibtex + pdflatex × 2


class subprocess_CompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess."""
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = b"OK"


# --------------------------------------------------------------------------- #
#  md_to_latex tests
# --------------------------------------------------------------------------- #

class TestMdToLatex:

    def test_convert_section_extracts_latex_block(self):
        llm = SimpleNamespace(call=lambda messages, **kw: SimpleNamespace(
            content="Some text\n```latex\nThe result is $x^2$.\n```\nMore text"
        ))
        result = convert_section("method", "## Method\nDo the thing.", llm)
        assert "x^2" in result
        assert "```" not in result  # fence stripped

    def test_convert_section_no_block_returns_raw(self):
        llm = SimpleNamespace(call=lambda messages, **kw: SimpleNamespace(
            content="Just plain LaTeX $y = mx + b$."
        ))
        result = convert_section("intro", "text", llm)
        assert "y = mx + b" in result

    def test_assemble_paper_creates_modular_structure(self, tmp_path):
        sections = {
            "abstract": "Short abstract.",
            "intro": "Intro text.",
            "method": "Method text.",
            "eval": "Eval text.",
            "related": "Related text.",
            "conclusion": "Conclusion text.",
        }
        main_path = assemble_paper(tmp_path, sections, title="Test Paper")
        assert main_path.exists()
        assert (tmp_path / "sections").is_dir()
        # All section files created.
        for name in sections:
            assert (tmp_path / "sections" / f"{name}.tex").exists()
        # main.tex has \input for each section.
        main_content = main_path.read_text()
        for name in sections:
            assert f"sections/{name}" in main_content
        # Abstract wrapped in \begin{abstract}.
        abstract_content = (tmp_path / "sections" / "abstract.tex").read_text()
        assert "\\begin{abstract}" in abstract_content
        # Non-abstract sections have \section{}.
        intro_content = (tmp_path / "sections" / "intro.tex").read_text()
        assert "\\section{Introduction}" in intro_content
        # refs.bib created.
        assert (tmp_path / "refs.bib").exists()
        # math_commands.tex copied.
        assert (tmp_path / "math_commands.tex").exists()

    def test_assemble_paper_partial_sections(self, tmp_path):
        r"""Only some sections present → only those are \input-ed."""
        sections = {"abstract": "Abs.", "intro": "Intro."}
        assemble_paper(tmp_path, sections, title="Partial")
        main_content = (tmp_path / "main.tex").read_text()
        assert "sections/abstract" in main_content
        assert "sections/intro" in main_content
        assert "sections/method" not in main_content

    def test_extract_latex_block(self):
        assert _extract_latex_block("```latex\ncontent\n```") == "content"
        assert _extract_latex_block("```tex\nx\n```") == "x"
        assert _extract_latex_block("plain text") == "plain text"


# --------------------------------------------------------------------------- #
#  Packager tests
# --------------------------------------------------------------------------- #

class TestPackager:

    def test_package_contains_all_artifacts(self, tmp_path):
        paper = tmp_path / "paper"
        paper.mkdir()
        (paper / "main.tex").write_text("\\documentclass{article}")
        (paper / "main.pdf").write_bytes(b"%PDF-1.4")
        (paper / "sections").mkdir()
        (paper / "sections" / "intro.tex").write_text("intro")

        code = tmp_path / "code"
        code.mkdir()
        (code / "main.py").write_text("print('hello')")

        results = tmp_path / "results"
        results.mkdir()
        (results / "metrics.json").write_text('{"loss": 0.1}')

        out = tmp_path / "deliverable.zip"
        package_deliverable(paper, out, code_dir=code, results_dir=results, project_title="Test")

        assert out.exists()
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert "paper/main.tex" in names
            assert "paper/main.pdf" in names
            assert "paper/sections/intro.tex" in names
            assert "code/main.py" in names
            assert "results/metrics.json" in names
            assert "MANIFEST.md" in names

    def test_package_without_code_or_results(self, tmp_path):
        paper = tmp_path / "paper"
        paper.mkdir()
        (paper / "main.tex").write_text("content")
        out = tmp_path / "out.zip"
        package_deliverable(paper, out)
        assert out.exists()
        with zipfile.ZipFile(out) as zf:
            assert "paper/main.tex" in zf.namelist()
            assert "MANIFEST.md" in zf.namelist()

    def test_package_excludes_intermediates(self, tmp_path):
        """LaTeX intermediate files (.aux, .log, etc.) are excluded."""
        paper = tmp_path / "paper"
        paper.mkdir()
        (paper / "main.tex").write_text("content")
        (paper / "main.aux").write_text("aux")
        (paper / "main.log").write_text("log")
        out = tmp_path / "out.zip"
        package_deliverable(paper, out)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert "paper/main.tex" in names
            assert not any(n.endswith(".aux") for n in names)
            assert not any(n.endswith(".log") for n in names)


# --------------------------------------------------------------------------- #
#  ProjectController P3 orchestration
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "haa.db")
    yield s
    s.close()


@pytest.fixture
def cfg():
    return default_config()


def _setup_p3_project(store, cfg):
    """Create a project at EA phase, ready for P3."""
    from tests.test_project_controller import FakePipeline, _make_controller, MockLLM

    brief = Brief(title="Test Paper", problem_area="AI", track=Track.THEORY)
    pipe = FakePipeline(store, publish=True, scout_candidate_count=1)
    ctrl = _make_controller(store, cfg, pipeline=pipe)
    project = ctrl.create_project(brief)
    started = ctrl.start_project(project.id)
    precursor_campaign = started.precursors[0].campaign_id
    ctrl.select_precursor(project.id, precursor_campaign)

    # P2: fake success → EA phase.
    from tests.test_p2 import FakeCodingAgent, FakeTransport
    fake_code = store.db_path.parent / "fake_code"
    fake_code.mkdir(exist_ok=True)
    (fake_code / "main.py").write_text("print('ok')")
    coding_agent = FakeCodingAgent(code_dir=fake_code)
    transport = FakeTransport(store.db_path.parent / "exec", crash_first=0, bad_metrics_first=0)
    ctrl2 = ProjectController(
        cfg, store, budget=None, llm=MockLLM(),
        coding_agent_factory=lambda p, wd: coding_agent,
        p2_transport=transport,
    )
    ea_project = ctrl2.advance_to_p2(project.id)
    assert ea_project.phase == Phase.EA
    return ea_project


class TestProjectControllerP3:

    def test_advance_to_p3_success(self, store, cfg, tmp_path):
        """Full P3 flow: INTEGRATE → MD2LATEX → COMPILE → PACKAGE → DONE."""
        ea_project = _setup_p3_project(store, cfg)
        llm = SectionAwareMockLLM()
        ctrl = ProjectController(cfg, store, budget=None, llm=llm)
        result = ctrl.advance_to_p3(ea_project.id)

        assert result.phase == Phase.DONE
        assert result.p3_paper_dir
        assert result.p3_deliverable_path
        # Paper directory has main.tex + sections.
        paper_dir = Path(result.p3_paper_dir)
        assert (paper_dir / "main.tex").exists()
        assert (paper_dir / "sections").is_dir()
        # Deliverable zip exists.
        assert Path(result.p3_deliverable_path).exists()
        # LLM was called for PAPER_INTEGRATE + 6 MD2LATEX calls.
        assert "P3_PAPER_INTEGRATE" in llm.calls
        assert llm.calls.count("P3_MD2LATEX") == 6

    def test_advance_to_p3_rejects_wrong_phase(self, store, cfg):
        ctrl = ProjectController(cfg, store, budget=None, llm=SectionAwareMockLLM())
        brief = Brief(title="T", problem_area="AI", track=Track.THEORY)
        project = ctrl.create_project(brief)
        with pytest.raises(ValueError, match="EA"):
            ctrl.advance_to_p3(project.id)

    def test_complete_project_after_p3(self, store, cfg):
        """P3 → DONE → user can complete the project."""
        ea_project = _setup_p3_project(store, cfg)
        llm = SectionAwareMockLLM()
        ctrl = ProjectController(cfg, store, budget=None, llm=llm)
        done = ctrl.advance_to_p3(ea_project.id)
        assert done.phase == Phase.DONE
        completed = ctrl.complete_project(ea_project.id)
        assert completed.status == ProjectStatus.COMPLETED
        assert completed.is_terminal

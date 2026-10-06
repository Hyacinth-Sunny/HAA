r"""Markdown → LaTeX 转换 + 论文组装（v0.9）。

转换是 **LLM 驱动的**（非规则式），参考 HM-Pro RECIPE.md Stage 4b 的
per-section prompt 模式：每个章节独立转换，锁定 notation 宏，输出纯 LaTeX
body（无 preamble）。

组装遵循 HM-Pro 的模块化结构：``main.tex + \input{sections/*.tex}``。
HM-Pro 教训：第一篇论文是单文件 61KB main.tex，refinement 无法逐节操作——
必须模块化（HM-Pro ``_require_modular_paper()`` 强制检查）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from haa.prompts import render_prompt

logger = logging.getLogger("haa.p3.md2latex")

# 标准章节顺序（对应 HAA paper 的 6 个 section）。
# 渲染序（v1.0.5 论文前半部：新前体只产前四节；旧前体的尾三节仍转换）
SECTION_ORDER = ["abstract", "intro", "background", "method", "eval", "related", "conclusion"]

# 章节 LaTeX 环境映射（决定 \input 文件名 + section 标题）。
SECTION_TITLES = {
    "abstract": "Abstract",
    "intro": "Introduction",
    "background": "Background and Preliminaries",
    "method": "Method",
    "eval": "Experiments",
    "related": "Related Work",
    "conclusion": "Conclusion",
}


def convert_section(
    section_name: str,
    markdown: str,
    llm: Any,
    *,
    notation: dict[str, str] | None = None,
) -> str:
    """单章节 MD→LaTeX 转换（LLM 驱动）。

    返回 LaTeX body（无 preamble、无 \\documentclass），适合写入
    ``sections/<name>.tex`` 供 ``\\input{}`` 使用。
    """
    prompt = render_prompt(
        "p3_md_to_latex",
        section_name=section_name,
        section_title=SECTION_TITLES.get(section_name, section_name.title()),
        markdown=markdown,
        notation=notation or {},
    )
    resp = llm.call(
        messages=[{"role": "user", "content": prompt}],
        stage="P3_MD2LATEX",
    )
    return _extract_latex_block(resp.content)


def assemble_paper(
    paper_dir: str | Path,
    sections: dict[str, str],
    *,
    title: str = "Untitled",
    authors: str = "Anonymous",
    template: str = "plain",
) -> Path:
    """组装完整 LaTeX 论文目录。

    写入 ``paper_dir/main.tex`` + ``paper_dir/sections/*.tex`` +
    ``paper_dir/refs.bib``（空模板）。

    Returns main.tex 的路径。
    """
    paper_dir = Path(paper_dir)
    sections_dir = paper_dir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    # Write section files.
    written_sections: list[str] = []
    for name, latex_body in sections.items():
        if name == "abstract":
            # Abstract is special — \begin{abstract} … \end{abstract}.
            content = f"\\begin{{abstract}}\n{latex_body}\n\\end{{abstract}}\n"
        else:
            section_title = SECTION_TITLES.get(name, name.title())
            content = f"\\section{{{section_title}}}\n{latex_body}\n"
        (sections_dir / f"{name}.tex").write_text(content, encoding="utf-8")
        written_sections.append(name)

    # Write main.tex.
    main_content = _render_main_tex(title, authors, written_sections, template)
    main_path = paper_dir / "main.tex"
    main_path.write_text(main_content, encoding="utf-8")

    # Write empty refs.bib if not present.
    refs_path = paper_dir / "refs.bib"
    if not refs_path.exists():
        refs_path.write_text(
            "% Bibliography entries\n"
            "% Add BibTeX entries here, e.g.:\n"
            "% @inproceedings{key2024,\n"
            "%   title={...},\n"
            "%   author={...},\n"
            "%   booktitle={...},\n"
            "%   year={2024},\n"
            "% }\n",
            encoding="utf-8",
        )

    # Copy math_commands.tex if available.
    math_src = Path(__file__).parent / "assets" / "math_commands.tex"
    if math_src.exists():
        shutil_copy = __import__("shutil").copy2
        shutil_copy(str(math_src), str(paper_dir / "math_commands.tex"))

    logger.info(
        "paper assembled: %s (%d sections: %s)",
        main_path, len(written_sections), ", ".join(written_sections),
    )
    return main_path


def _render_main_tex(
    title: str, authors: str, sections: list[str], template: str
) -> str:
    """渲染 main.tex 内容。

    用 ``str.replace`` 而非 ``.format()``——LaTeX 花括号会与 format 冲突。
    """
    # Build \input lines for non-abstract sections.
    input_lines = []
    for name in sections:
        if name == "abstract":
            input_lines.append(r"\input{sections/abstract}")
        else:
            input_lines.append(rf"\input{{sections/{name}}}")
    inputs = "\n".join(input_lines)

    tpl = _ICLR_TEMPLATE if template == "iclr" else _PLAIN_TEMPLATE
    return (
        tpl
        .replace("<<<TITLE>>>", title)
        .replace("<<<AUTHORS>>>", authors)
        .replace("<<<INPUTS>>>", inputs)
    )


_PLAIN_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amssymb,amsthm,mathtools}
\usepackage{booktabs,tabularx}
\usepackage{enumitem}
\usepackage{algorithm}
\usepackage[noend]{algpseudocode}
\usepackage[hidelinks]{hyperref}
\usepackage{graphicx}
\input{math_commands}

\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\newtheorem{proposition}[theorem]{Proposition}
\newtheorem{corollary}[theorem]{Corollary}
\newtheorem{definition}[theorem]{Definition}
\newtheorem{assumption}[theorem]{Assumption}
\newtheorem{remark}[theorem]{Remark}

\title{<<<TITLE>>>}
\author{<<<AUTHORS>>>}
\date{}

\begin{document}
\maketitle

<<<INPUTS>>>

\bibliographystyle{plain}
\bibliography{refs}

\end{document}
"""


_ICLR_TEMPLATE = r"""\documentclass{article}
\usepackage{iclr2026_conference}
\input{math_commands}
\usepackage{amsthm}

\newtheorem{theorem}{Theorem}
\newtheorem{lemma}[theorem]{Lemma}
\newtheorem{proposition}[theorem]{Proposition}

\title{<<<TITLE>>>}
\author{<<<AUTHORS>>>}

\begin{document}
\maketitle

<<<INPUTS>>>

\bibliographystyle{iclr2026_conference}
\bibliography{refs}

\end{document}
"""


def _extract_latex_block(text: str) -> str:
    """从 LLM 输出中提取 ```latex ... ``` 代码块。

    如果没有代码块围栏，返回清理后的原文（去首尾空白）。
    """
    lines = text.strip().split("\n")
    in_block = False
    block_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```latex") or stripped.startswith("```tex"):
            in_block = True
            continue
        if stripped == "```" and in_block:
            in_block = False
            continue
        if in_block:
            block_lines.append(line)
    if block_lines:
        return "\n".join(block_lines).strip()
    # No fenced block — return cleaned text.
    return text.strip()

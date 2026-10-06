"""Prompt-template loader for the stages.

Each stage's instructions live as a Jinja2 Markdown file under ``prompts/``
(e.g. ``prompts/seek.md``, ``prompts/review/correctness.md``). A stage renders
the template with its working-memory objects (``brief``, ``candidate``,
``design``, ``review``, ``paper``, …) and feeds the result into the agent loop.

The templates use guards (``{% if brief %}``) rather than ``StrictUndefined``
because most context objects are legitimately ``None`` at some point in the
pipeline (e.g. ``candidate`` is None before SEEK selects the queue head).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template

# haa/prompts.py → parent is haa/ → parent again is the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = _PROJECT_ROOT / "prompts"

_env = Environment(
    loader=FileSystemLoader(str(PROMPTS_DIR)),
    autoescape=False,  # Markdown, not HTML.
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)
_cache: dict[str, Template] = {}


def _template(name: str) -> Template:
    if name not in _cache:
        _cache[name] = _env.get_template(f"{name}.md")
    return _cache[name]


def render_prompt(name: str, **vars: Any) -> str:
    """Render ``prompts/<name>.md`` with the given variables.

    ``name`` may include a subdir, e.g. ``"review/correctness"``. Templates are
    cached after first load.
    """
    return _template(name).render(**vars)


def prompt_names() -> list[str]:
    """All available template names (relative path, no ``.md``), sorted."""
    if not PROMPTS_DIR.exists():
        return []
    names = []
    for p in sorted(PROMPTS_DIR.rglob("*.md")):
        rel = p.relative_to(PROMPTS_DIR).as_posix()  # posix → "/" on all OSes
        names.append(rel[:-3])  # strip ".md"
    return names


def has_prompt(name: str) -> bool:
    """True if ``prompts/<name>.md`` exists."""
    return (PROMPTS_DIR / f"{name}.md").is_file()

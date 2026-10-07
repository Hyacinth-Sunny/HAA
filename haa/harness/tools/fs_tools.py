"""工具②：文件三件套原生强化（read_file / write_file / edit_file）。

大修第一章 §5.2 规格，行为规格来自 DSH 测试翻译清单 §2：
- read_file：行号输出＋页脚信封（`(End of file - total N lines)` /
  `Showing lines A-B of N`）；字节封顶标注；目录/缺失分类错误。
- edit_file：old_text 恰好一次命中；0 命中给最相近片段前 3 候选行号；
  ≥2 命中列全部行号；仅 UTF-8（其他编码明确报错）；写盘回读比对。
  先读后改与 mtime 校验由统一检查层写闸门强制（本层不重复实现，
  防线在 :class:`haa.harness.checklist.ReadBeforeWriteGate`）。
- write_file：未观察写=创建语义 / 已观察写=按版本替换（闸门语义），
  本层负责落盘与回读验证。
"""

from __future__ import annotations

import difflib
import logging
from pathlib import Path

from haa.harness.registry import ToolCallContext, ToolError, ToolResult

logger = logging.getLogger("haa.harness.tools.fs")

DEFAULT_READ_MAX_BYTES = 30_000
DEFAULT_READ_MAX_LINES = 2000


def _resolve(path: str) -> Path:
    return Path(path).expanduser()


def _read_text_strict(path: Path) -> str:
    """仅 UTF-8：其他编码明确报错（§5.2）。字节级读取——不做换行归一，
    CRLF/LF 混合文件原样保留（§2-14）。"""
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(
            f"FS_NOT_TEXT: {path.name} is not valid UTF-8 "
            f"({exc}; only UTF-8 files are supported)"
        ) from exc


# --------------------------------------------------------------------------- #
#  read_file
# --------------------------------------------------------------------------- #

def format_read_output(text: str, *, offset: int = 1, limit: int | None = None,
                       max_bytes: int = DEFAULT_READ_MAX_BYTES) -> tuple[str, dict]:
    """行号信封渲染（DSH formatReadOutput 的 HAA 翻译）。

    返回 (渲染文本, 窗口 meta)。空文件 → 仅页脚；分页 → Showing 行；
    字节封顶 → Output capped 提示与下一 offset 指引。
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]  # 尾换行不产生悬空空行
    total = len(lines)
    if total == 0:
        return "(empty file)", {"total_lines": 0, "offset": 1, "limit": 0}
    start = max(1, int(offset))
    end = min(total, start - 1 + (limit or total))
    window = lines[start - 1:end]
    body_lines = []
    used = 0
    capped = False
    shown = 0
    for i, ln in enumerate(window, start=start):
        numbered = f"{i:6d}\t{ln}"
        if used + len(numbered.encode()) > max_bytes:
            capped = True
            break
        body_lines.append(numbered)
        used += len(numbered.encode()) + 1
        shown += 1
    if capped:
        footer = (f"(Output capped. Showing lines {start}-{start + shown - 1}. "
                  f"Use offset={start + shown} to continue)")
    elif start > 1 or end < total:
        footer = f"(Showing lines {start}-{end} of {total})"
    else:
        footer = f"(End of file - total {total} lines)"
    body = "\n".join(body_lines)
    return (body + "\n" + footer) if body else footer, {
        "total_lines": total, "offset": start, "limit": shown or (end - start + 1),
    }


def make_read_file(services):

    def read_file(args: dict, ctx: ToolCallContext) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        if not raw:
            raise ToolError("read_file: 'path' must be non-empty")
        path = _resolve(raw)
        if not path.exists():
            raise ToolError(f"FS_NOT_FOUND: {raw} does not exist")
        if path.is_dir():
            raise ToolError(f"FS_NOT_REGULAR_FILE: {raw} is a directory")
        text = _read_text_strict(path)
        offset = args.get("offset", 1)
        offset = max(1, int(offset)) if isinstance(offset, (int, float)) else 1
        limit = args.get("limit")
        limit = max(1, int(limit)) if isinstance(limit, (int, float)) else None
        if limit is not None:
            limit = min(limit, DEFAULT_READ_MAX_LINES)
        max_bytes = DEFAULT_READ_MAX_BYTES
        if isinstance(args.get("max_chars"), (int, float)) and args["max_chars"] > 0:
            max_bytes = int(args["max_chars"])  # legacy 契约：max_chars 即输出帽
        body, meta = format_read_output(text, offset=offset, limit=limit,
                                         max_bytes=max_bytes)
        return ToolResult(content=body, meta=meta)

    return read_file


# --------------------------------------------------------------------------- #
#  write_file
# --------------------------------------------------------------------------- #

def make_write_file(services):

    def write_file(args: dict, ctx: ToolCallContext) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        content = args.get("content")
        if not raw:
            raise ToolError("write_file: 'path' must be non-empty")
        if not isinstance(content, str):
            raise ToolError("write_file: 'content' must be a string")
        path = _resolve(raw)
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        # 写盘回读比对（§5.2 一致性校验）
        if path.read_bytes() != content.encode("utf-8"):
            raise ToolError(f"FS_WRITE_VERIFY_FAILED: {raw} readback mismatch")
        action = "overwritten" if existed else "created"
        return ToolResult(content=f"{raw} {action} successfully ({len(content)} chars)",
                          meta={"created": not existed})

    return write_file


# --------------------------------------------------------------------------- #
#  edit_file
# --------------------------------------------------------------------------- #

def _closest_candidates(lines: list[str], old_text: str, k: int = 3) -> list[int]:
    """0 命中时给最相近片段的前 3 候选行号（difflib 相似度，§2-3）。"""
    target = old_text.strip().splitlines() or [old_text]
    probe = target[0][:120]
    scored = []
    for i, ln in enumerate(lines, start=1):
        ratio = difflib.SequenceMatcher(None, probe, ln.strip()[:120]).ratio()
        if ratio > 0.3:
            scored.append((ratio, i))
    scored.sort(reverse=True)
    return [i for _, i in scored[:k]]


def make_edit_file(services):

    def edit_file(args: dict, ctx: ToolCallContext) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        # legacy 契约用 old_string/new_string；DSH 风格 old_text 为别名
        old_text = args.get("old_string", args.get("old_text"))
        new_text = args.get("new_string", args.get("new_text"))
        if not raw:
            raise ToolError("edit_file: 'path' must be non-empty")
        if not isinstance(old_text, str) or not old_text:
            raise ToolError("edit_file: 'old_text' must be a non-empty string")
        if not isinstance(new_text, str):
            raise ToolError("edit_file: 'new_text' must be a string")
        if old_text == new_text:
            raise ToolError("edit_file: 'old_text' and 'new_text' are identical")
        path = _resolve(raw)
        if not path.exists():
            raise ToolError(f"FS_NOT_FOUND: {raw} does not exist")
        text = _read_text_strict(path)
        count = text.count(old_text)
        if count == 0:
            lines = text.split("\n")
            cands = _closest_candidates(lines, old_text)
            hint = (f"closest candidate lines: {cands}" if cands
                    else "no similar lines found — check the exact text (copy it "
                         "verbatim from a fresh read_file)")
            raise ToolError(
                f"FS_EDIT_NOT_FOUND: old_text not found in {raw} ({hint})")
        if count > 1 and not args.get("replace_all"):
            # 列出全部命中行号（§2-4：按换行起点二分定位）
            import bisect
            line_starts = [0]
            for i, ch in enumerate(text):
                if ch == "\n":
                    line_starts.append(i + 1)
            hit_lines: list[int] = []
            idx = text.find(old_text)
            while idx != -1 and len(hit_lines) < 50:
                hit_lines.append(bisect.bisect_right(line_starts, idx))
                idx = text.find(old_text, idx + 1)
            raise ToolError(
                f"FS_AMBIGUOUS_EDIT: old_text occurs {count} times in {raw} "
                f"(lines {hit_lines}) — include more surrounding context to make "
                f"it unique, or pass replace_all=true")
        if args.get("replace_all"):
            new = text.replace(old_text, new_text)
        else:
            new = text.replace(old_text, new_text, 1)
        path.write_bytes(new.encode("utf-8"))
        if path.read_bytes() != new.encode("utf-8"):
            raise ToolError(f"FS_WRITE_VERIFY_FAILED: {raw} readback mismatch")
        verb = "replaced all occurrences" if args.get("replace_all") else "updated"
        return ToolResult(content=f"{raw} {verb} successfully")

    return edit_file


FS_TOOL_RULES = (
    "File rules: read_file output carries line numbers — cite them when "
    "discussing code. edit_file requires old_text to match EXACTLY ONCE "
    "(copy verbatim from read_file, never from memory). If a file changed, "
    "re-read it before editing."
)

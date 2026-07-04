"""Filesystem tools over the confined per-thread workspace.

All paths are virtual (``/mnt/user-data/...``) or workspace-relative; the sandbox resolves and
confines them. Each tool takes a short ``description`` (a human-readable summary of the action,
used for logs/UI/audit) plus its operands.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from atom.tools.common import get_sandbox


@tool(parse_docstring=True)
def ls(runtime: ToolRuntime, description: str, path: str) -> str:
    """List the contents of a directory (up to two levels deep).

    Args:
        description: One-line summary of why you're listing this directory.
        path: Directory to list, e.g. /mnt/user-data/workspace.
    """
    return get_sandbox(runtime).list_dir(path)


@tool(parse_docstring=True)
def read_file(
    runtime: ToolRuntime,
    description: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a text file and return it with line numbers.

    Args:
        description: One-line summary of why you're reading this file.
        path: File to read.
        start_line: Optional 1-based inclusive first line.
        end_line: Optional 1-based inclusive last line.
    """
    text = get_sandbox(runtime).read_text(path)
    lines = text.splitlines()
    if not lines:
        return "(empty file)"
    start = max(1, start_line or 1)
    end = min(len(lines), end_line or len(lines))
    if start > end:
        return f"(no lines in range {start}-{end}; file has {len(lines)} lines)"
    width = len(str(end))
    body = "\n".join(f"{i:>{width}}\t{lines[i - 1]}" for i in range(start, end + 1))
    return body


@tool(parse_docstring=True)
def write_file(
    runtime: ToolRuntime, description: str, path: str, content: str, append: bool = False
) -> str:
    """Write text to a file, creating parent directories as needed.

    Args:
        description: One-line summary of what you're writing.
        path: Destination file. Put deliverables under /mnt/user-data/outputs.
        content: The text to write.
        append: If true, append to the end instead of overwriting.
    """
    get_sandbox(runtime).write_text(path, content, append=append)
    verb = "Appended to" if append else "Wrote"
    return f"{verb} {path} ({len(content)} chars)."


@tool(parse_docstring=True)
def edit_file(
    runtime: ToolRuntime,
    description: str,
    path: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
) -> str:
    """Replace an exact substring in a file (precise in-place edit).

    ``old_str`` must occur exactly once unless ``replace_all`` is true.

    Args:
        description: One-line summary of the edit.
        path: File to edit.
        old_str: Exact text to find (include enough context to be unique).
        new_str: Replacement text.
        replace_all: If true, replace every occurrence instead of requiring a unique match.
    """
    count = get_sandbox(runtime).edit_text(path, old_str, new_str, replace_all=replace_all)
    return f"Edited {path} ({count} replacement{'s' if count != 1 else ''})."


@tool(parse_docstring=True)
def glob(
    runtime: ToolRuntime,
    description: str,
    pattern: str,
    path: str,
    include_dirs: bool = False,
    max_results: int = 100,
) -> str:
    """Find files matching a glob pattern (e.g. ``**/*.py``), newest first.

    Args:
        description: One-line summary of what you're searching for.
        pattern: Glob pattern, relative to path (e.g. **/*.py).
        path: Root directory to search under.
        include_dirs: If true, also match directories.
        max_results: Maximum number of results to return.
    """
    hits = get_sandbox(runtime).glob(
        pattern, path, include_dirs=include_dirs, max_results=max_results
    )
    return "\n".join(hits) if hits else "(no matches)"


@tool(parse_docstring=True)
def grep(
    runtime: ToolRuntime,
    description: str,
    pattern: str,
    path: str,
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
) -> str:
    """Search file contents for a regex (or literal) pattern; returns ``file:line:text``.

    Args:
        description: One-line summary of what you're searching for.
        pattern: Regex to search for (or literal text if literal=true).
        path: Root directory to search under.
        glob: Optional glob to restrict which files are searched (e.g. *.py).
        literal: If true, treat pattern as a literal string, not a regex.
        case_sensitive: If true, match case-sensitively.
        max_results: Maximum number of matching lines to return.
    """
    hits = get_sandbox(runtime).grep(
        pattern,
        path,
        glob=glob,
        literal=literal,
        case_sensitive=case_sensitive,
        max_results=max_results,
    )
    return "\n".join(hits) if hits else "(no matches)"


FILESYSTEM_TOOLS = [ls, read_file, write_file, edit_file, glob, grep]

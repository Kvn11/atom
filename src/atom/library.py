"""The tool/skill LIBRARY subsystem (deviation #4).

Frequently-used tools/skills are bound/injected up front (see :mod:`atom.tools.registry` and the
system prompt); everything else lives on disk under ``$ATOM_HOME/tool_library`` and
``$ATOM_HOME/skill_library`` and is discovered via ``search_tools`` / ``search_skills`` (BM25),
then promoted into the live tool set / injected as context.

On-disk layout::

    tool_library/<name>/manifest.yaml     # name, description, keywords, tier, entrypoint
    tool_library/<name>/tool.py           # defines the @tool referenced by entrypoint (or use module:attr)
    skill_library/<name>/SKILL.md         # YAML front-matter (name/description/keywords/tier) + body
    skills/<name>/SKILL.md                # always-on skills (front-matter + body)
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import BaseTool

# ------------------------------------------------------------------ BM25 ranker

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


try:  # optional dependency; fall back to lexical overlap if missing
    from rank_bm25 import BM25Okapi
except Exception:  # noqa: BLE001
    BM25Okapi = None  # type: ignore[assignment]


class _Ranker:
    """Rank docs by BM25, gating on query-token overlap.

    The overlap gate matters on tiny corpora: BM25 (Okapi) IDF is negative for a term present in
    the only document, so ranking on score sign alone would drop every match. We keep any doc that
    shares a token with the query and order those by BM25 score.
    """

    def __init__(self, docs: list[str]):
        self._token_lists = [_tokenize(d) for d in docs]
        self._token_sets = [set(t) for t in self._token_lists]
        self._bm25 = BM25Okapi(self._token_lists) if (BM25Okapi and self._token_lists) else None

    def rank(self, query: str, k: int) -> list[int]:
        return [i for i, _ in self.rank_scored(query, k)]

    def rank_scored(self, query: str, k: int) -> list[tuple[int, float]]:
        """Return up to ``k`` ``(doc_index, normalized_score)`` for overlap-matched docs.

        Scores are min-max normalized across the matched set to ``[0, 1]`` (top match = 1.0), so a
        caller's ``min_score`` gate is meaningful and sidesteps BM25's negative-IDF on tiny corpora.
        """
        q = _tokenize(query)
        qs = set(q)
        if not qs or not self._token_lists:
            return []
        bm = list(self._bm25.get_scores(q)) if self._bm25 is not None else [0.0] * len(self._token_lists)
        scored = [
            (i, bm[i], len(qs & toks))
            for i, toks in enumerate(self._token_sets)
            if qs & toks
        ]
        if not scored:
            return []
        scored.sort(key=lambda t: (t[1], t[2]), reverse=True)
        vals = [s for _, s, _ in scored]
        lo, hi = min(vals), max(vals)
        span = hi - lo
        norm = lambda s: (s - lo) / span if span > 0 else 1.0  # noqa: E731
        return [(i, norm(s)) for i, s, _ in scored[:k]]


# ------------------------------------------------------------------ entries

@dataclass
class ToolEntry:
    name: str
    description: str
    keywords: list[str]
    tier: str  # "frequent" | "deferred"
    tool: BaseTool

    def doc(self) -> str:
        return f"{self.name} {self.description} {' '.join(self.keywords)}"


@dataclass
class SkillEntry:
    name: str
    description: str
    keywords: list[str]
    tier: str
    body: str

    def doc(self) -> str:
        return f"{self.name} {self.description} {' '.join(self.keywords)}"


# ------------------------------------------------------------------ loaders

def _load_tool_from_dir(dir_: Path, manifest: dict) -> BaseTool | None:
    entry = manifest.get("entrypoint")
    if not entry:
        return None
    if ":" in entry and not (dir_ / "tool.py").exists():
        mod_path, _, attr = entry.partition(":")
        obj = getattr(importlib.import_module(mod_path), attr)
    else:
        attr = entry.split(":")[-1]
        tool_py = dir_ / "tool.py"
        if not tool_py.exists():
            return None
        spec = importlib.util.spec_from_file_location(f"atom_libtool_{dir_.name}", tool_py)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        obj = getattr(module, attr)
    return obj if isinstance(obj, BaseTool) else None


def load_tool_entries(tool_library: Path) -> list[ToolEntry]:
    entries: list[ToolEntry] = []
    if not tool_library.is_dir():
        return entries
    for sub in sorted(p for p in tool_library.iterdir() if p.is_dir()):
        mf = sub / "manifest.yaml"
        if not mf.exists():
            continue
        try:
            manifest = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
            tool = _load_tool_from_dir(sub, manifest)
            if tool is None:
                continue
            entries.append(
                ToolEntry(
                    name=manifest.get("name", tool.name),
                    description=manifest.get("description", tool.description or ""),
                    keywords=list(manifest.get("keywords", [])),
                    tier=manifest.get("tier", "deferred"),
                    tool=tool,
                )
            )
        except Exception:  # noqa: BLE001 - a broken library entry must not break startup
            continue
    return entries


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_skill_md(text: str, fallback_name: str) -> SkillEntry:
    meta: dict[str, Any] = {}
    body = text
    m = _FRONTMATTER.match(text)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        body = m.group(2)
    return SkillEntry(
        name=meta.get("name", fallback_name),
        description=meta.get("description", ""),
        keywords=list(meta.get("keywords", [])),
        tier=meta.get("tier", "deferred"),
        body=body.strip(),
    )


def load_skill_entries(skill_dir: Path) -> list[SkillEntry]:
    entries: list[SkillEntry] = []
    if not skill_dir.is_dir():
        return entries
    for sub in sorted(p for p in skill_dir.iterdir() if p.is_dir()):
        md = sub / "SKILL.md"
        if not md.exists():
            continue
        try:
            entries.append(parse_skill_md(md.read_text(encoding="utf-8"), sub.name))
        except Exception:  # noqa: BLE001
            continue
    return entries


# ------------------------------------------------------------------ index

class LibraryIndex:
    def __init__(
        self,
        tools: list[ToolEntry],
        skills: list[SkillEntry],
        *,
        auto_promote_k: int = 3,
        min_score: float = 0.0,
    ):
        self.tools = tools
        self.skills = skills
        # Promotion tuning (wired from LibraryConfig by build_lead_agent).
        self.auto_promote_k = auto_promote_k
        self.min_score = min_score
        self._tool_ranker = _Ranker([t.doc() for t in tools])
        self._skill_ranker = _Ranker([s.doc() for s in skills])
        # Identifies the current deferred-tool catalog; promotions carry it so stale ones (from a
        # changed catalog, e.g. future MCP re-registration) can be invalidated.
        self.catalog_hash = hashlib.sha1(
            "|".join(sorted(self.deferred_tool_names())).encode("utf-8")
        ).hexdigest()[:12]

    def search_tools(self, query: str, k: int = 3, min_score: float = 0.0) -> list[ToolEntry]:
        return [self.tools[i] for i, sc in self._tool_ranker.rank_scored(query, k) if sc >= min_score]

    def search_skills(self, query: str, k: int = 3, min_score: float = 0.0) -> list[SkillEntry]:
        return [self.skills[i] for i, sc in self._skill_ranker.rank_scored(query, k) if sc >= min_score]

    def deferred_tools(self) -> list[BaseTool]:
        return [t.tool for t in self.tools if t.tier == "deferred"]

    def frequent_tools(self) -> list[BaseTool]:
        return [t.tool for t in self.tools if t.tier == "frequent"]

    def deferred_tool_names(self) -> set[str]:
        return {t.tool.name for t in self.tools if t.tier == "deferred"}

    def skill_by_name(self, name: str) -> SkillEntry | None:
        return next((s for s in self.skills if s.name == name), None)

    @property
    def has_tools(self) -> bool:
        return bool(self.tools)

    @property
    def has_skills(self) -> bool:
        return bool(self.skills)


def load_library(home: Path | str) -> LibraryIndex:
    home = Path(home)
    return LibraryIndex(
        load_tool_entries(home / "tool_library"),
        load_skill_entries(home / "skill_library"),
    )


def load_named_skills(home: Path | str, names: list[str]) -> list[SkillEntry]:
    """Load specific always-on skills by name (from ``skills/`` then ``skill_library/``)."""
    home = Path(home)
    out: list[SkillEntry] = []
    for name in names:
        for base in (home / "skills", home / "skill_library"):
            md = base / name / "SKILL.md"
            if md.exists():
                out.append(parse_skill_md(md.read_text(encoding="utf-8"), name))
                break
    return out


# ------------------------------------------------------------------ registry

_lock = threading.Lock()
_indexes: dict[str, LibraryIndex] = {}


def register_index(home: str, index: LibraryIndex) -> None:
    with _lock:
        _indexes[home] = index


def get_index(home: str | None) -> LibraryIndex | None:
    if not home:
        return None
    with _lock:
        return _indexes.get(home)

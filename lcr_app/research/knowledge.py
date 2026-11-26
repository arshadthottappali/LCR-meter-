"""Per-material knowledge cache, so the same work is never repeated.

    <data folder>/knowledge/<material key>/
        molecule.json            PubChem record
        papers.json              all papers found (merged, de-duplicated)
        extractions.json         values the AI extracted from each paper (per paper key)
        searches.json            queries already run (query -> date, source, n results)
        literature_summary.md    AI-written literature summary (+ literature_meta.json)
    <data folder>/knowledge/_crossref.json   DOI verification cache (shared)

The material key is the PubChem InChIKey when known (unique per structure,
so synonyms map to the same folder), otherwise a slug of the name.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .sources import Paper, merge_papers

STALE_DAYS = 180


def material_key(name: str, inchikey: str = "") -> str:
    if inchikey:
        return inchikey.upper()
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "unknown").lower()).strip("-")
    return f"name-{slug or 'unknown'}"


def norm_query(q: str) -> str:
    return " ".join(sorted(re.sub(r"[^a-z0-9 ]", " ", q.lower()).split()))


class KnowledgeBase:
    def __init__(self, root: str | Path, key: str) -> None:
        self.root = Path(root)
        self.dir = self.root / key
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key = key

    # ---------------------------------------------------------------- json io
    def _read(self, name: str, default):
        p = self.dir / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _write(self, name: str, data) -> None:
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / name)

    # --------------------------------------------------------------- molecule
    @property
    def molecule(self) -> dict:
        return self._read("molecule.json", {})

    def save_molecule(self, data: dict) -> None:
        self._write("molecule.json", {**data, "cached": datetime.now().isoformat(timespec="seconds")})

    # ----------------------------------------------------------------- papers
    def papers(self) -> dict[str, Paper]:
        return {k: Paper.from_dict(v) for k, v in self._read("papers.json", {}).items()}

    def add_papers(self, new: list[Paper]) -> int:
        cur = self.papers()
        before = len(cur)
        merged = merge_papers(list(cur.values()) + new)
        self._write("papers.json", {k: p.to_dict() for k, p in merged.items()})
        return len(merged) - before

    def update_paper(self, p: Paper) -> None:
        cur = self._read("papers.json", {})
        cur[p.key] = p.to_dict()
        self._write("papers.json", cur)

    # ---------------------------------------------------------------- searches
    def searched(self, query: str, source: str) -> bool:
        s = self._read("searches.json", {})
        e = s.get(f"{source}|{norm_query(query)}")
        if not e:
            return False
        return datetime.fromisoformat(e["date"]) > datetime.now() - timedelta(days=STALE_DAYS)

    def record_search(self, query: str, source: str, n: int) -> None:
        s = self._read("searches.json", {})
        s[f"{source}|{norm_query(query)}"] = {"query": query, "source": source, "n": n,
                                              "date": datetime.now().isoformat(timespec="seconds")}
        self._write("searches.json", s)

    def search_log(self) -> list[dict]:
        return list(self._read("searches.json", {}).values())

    # ------------------------------------------------------------ extractions
    def extractions(self) -> dict[str, dict]:
        return self._read("extractions.json", {})

    def save_extraction(self, paper_key: str, data: dict) -> None:
        e = self.extractions()
        e[paper_key] = {**data, "extracted": datetime.now().isoformat(timespec="seconds")}
        self._write("extractions.json", e)

    # ------------------------------------------------------ literature summary
    def literature_summary(self) -> tuple[str, dict]:
        p = self.dir / "literature_summary.md"
        if not p.exists():
            return "", {}
        return p.read_text(encoding="utf-8"), self._read("literature_meta.json", {})

    def save_literature_summary(self, text: str, meta: dict) -> None:
        (self.dir / "literature_summary.md").write_text(text, encoding="utf-8")
        self._write("literature_meta.json", {**meta, "date": datetime.now().isoformat(timespec="seconds")})

    def summary_is_fresh(self) -> bool:
        _, meta = self.literature_summary()
        if not meta.get("date"):
            return False
        return datetime.fromisoformat(meta["date"]) > datetime.now() - timedelta(days=STALE_DAYS)

    def clear_literature(self) -> None:
        for n in ("literature_summary.md", "literature_meta.json", "searches.json"):
            try:
                (self.dir / n).unlink()
            except OSError:
                pass


class CrossrefCache:
    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "_crossref.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def get(self, doi: str) -> dict | None:
        return self.data.get(doi)

    def put(self, doi: str, rec: dict) -> None:
        self.data[doi] = rec
        self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")

"""Free scholarly data sources (no AI, no paid keys).

* PubChem PUG-REST  – molecule identity and properties
* OpenAlex          – papers, abstracts (free; optional free API key / e-mail)
* Semantic Scholar  – papers, abstracts, TL;DR (free; optional free API key)
* Crossref          – DOI verification (every cited DOI is checked here)
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)
USER_AGENT = "LCRSweep-Research/1.1 (scientific literature lookup)"


class SourceError(RuntimeError):
    pass


def http_json(url: str, headers: dict | None = None, timeout: float = 30, retries: int = 3,
              data: bytes | None = None) -> dict:
    """GET (or POST when data is given) a JSON document with simple retry / back-off."""
    h = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=h, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise SourceError("not found (404)") from e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = float(e.headers.get("Retry-After", 0) or 0) or 2.0 * (attempt + 1) ** 2
                time.sleep(min(wait, 30))
                continue
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise SourceError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}: {body}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
    raise SourceError(f"Network error for {urllib.parse.urlsplit(url).netloc}: {last}")


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d)
    return d


# ------------------------------------------------------------------- PubChem
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUBCHEM_PROPS = ["MolecularFormula", "MolecularWeight", "IUPACName", "InChIKey", "XLogP", "TPSA",
                 "HBondDonorCount", "HBondAcceptorCount", "RotatableBondCount", "Complexity"]


def pubchem_lookup(name: str = "", cas: str = "", smiles: str = "") -> dict:
    """Return {cid, properties..., synonyms[:15], url} or raise SourceError if not found."""
    if smiles:
        ns, ident = "smiles", smiles
    elif cas:
        ns, ident = "name", cas
    elif name:
        ns, ident = "name", name
    else:
        raise SourceError("Give a name, CAS number or SMILES")
    q = urllib.parse.quote(ident, safe="")
    props = ",".join(PUBCHEM_PROPS + ["SMILES"])
    try:
        data = http_json(f"{PUBCHEM}/compound/{ns}/{q}/property/{props}/JSON")
    except SourceError as e:
        if "404" in str(e):
            raise SourceError(f"'{ident}' was not found in PubChem (polymers and mixtures often are not)") from e
        # older/newer API may not know 'SMILES' -> retry without it
        data = http_json(f"{PUBCHEM}/compound/{ns}/{q}/property/{','.join(PUBCHEM_PROPS)}/JSON")
    props_list = data.get("PropertyTable", {}).get("Properties", [])
    if not props_list:
        raise SourceError(f"'{ident}' was not found in PubChem")
    p = props_list[0]
    cid = p.get("CID")
    syn = []
    try:
        s = http_json(f"{PUBCHEM}/compound/cid/{cid}/synonyms/JSON")
        syn = s.get("InformationList", {}).get("Information", [{}])[0].get("Synonym", [])[:15]
    except SourceError:
        pass
    cas_found = next((x for x in syn if re.fullmatch(r"\d{2,7}-\d\d-\d", x)), "")
    return {"cid": cid, **{k: p.get(k) for k in PUBCHEM_PROPS + ["SMILES"] if k in p},
            "synonyms": syn, "cas": cas_found, "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"}


# ------------------------------------------------------------------ papers
@dataclass
class Paper:
    key: str  # normalised DOI if available, else source id
    title: str = ""
    doi: str = ""
    year: int | None = None
    authors: list[str] = field(default_factory=list)
    venue: str = ""
    abstract: str = ""
    tldr: str = ""
    cited_by: int = 0
    url: str = ""
    oa_url: str = ""
    sources: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    verified: bool = False  # DOI confirmed by Crossref

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Paper":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})

    def short_authors(self) -> str:
        if not self.authors:
            return "Unknown authors"
        if len(self.authors) > 3:
            return f"{self.authors[0]} et al."
        return ", ".join(self.authors)

    def citation(self) -> str:
        s = f"{self.short_authors()} ({self.year or 'n.d.'}). {self.title}."
        if self.venue:
            s += f" *{self.venue}*."
        if self.doi:
            s += f" https://doi.org/{self.doi}"
        return s


def _openalex_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def search_openalex(query: str, limit: int = 15, email: str = "", api_key: str = "") -> list[Paper]:
    params = {"search": query, "per-page": str(limit),
              "select": "id,doi,title,publication_year,authorships,primary_location,abstract_inverted_index,"
                        "cited_by_count,open_access"}
    if email:
        params["mailto"] = email
    if api_key:
        params["api_key"] = api_key
    data = http_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    out = []
    for w in data.get("results", []):
        doi = normalize_doi(w.get("doi"))
        loc = w.get("primary_location") or {}
        src = (loc.get("source") or {}).get("display_name") or ""
        out.append(Paper(
            key=doi or w.get("id", ""), title=(w.get("title") or "").strip(), doi=doi, year=w.get("publication_year"),
            authors=[(a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])][:8],
            venue=src, abstract=_openalex_abstract(w.get("abstract_inverted_index")),
            cited_by=w.get("cited_by_count") or 0, url=w.get("id", ""),
            oa_url=(w.get("open_access") or {}).get("oa_url") or "", sources=["OpenAlex"], queries=[query]))
    return out


_last_s2 = [0.0]


def search_semantic_scholar(query: str, limit: int = 15, api_key: str = "") -> list[Paper]:
    # unauthenticated users share a rate limit: keep ≥ 1.1 s between calls
    dt = time.time() - _last_s2[0]
    if dt < 1.1:
        time.sleep(1.1 - dt)
    _last_s2[0] = time.time()
    params = {"query": query, "limit": str(limit),
              "fields": "title,year,abstract,externalIds,authors,venue,citationCount,tldr,openAccessPdf,url"}
    headers = {"x-api-key": api_key} if api_key else {}
    data = http_json("https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params),
                     headers=headers, retries=4)
    out = []
    for p in data.get("data", []) or []:
        doi = normalize_doi((p.get("externalIds") or {}).get("DOI"))
        out.append(Paper(
            key=doi or f"s2:{p.get('paperId')}", title=(p.get("title") or "").strip(), doi=doi, year=p.get("year"),
            authors=[a.get("name", "") for a in (p.get("authors") or [])][:8], venue=p.get("venue") or "",
            abstract=p.get("abstract") or "", tldr=((p.get("tldr") or {}).get("text") or ""),
            cited_by=p.get("citationCount") or 0, url=p.get("url") or "",
            oa_url=((p.get("openAccessPdf") or {}).get("url") or ""), sources=["Semantic Scholar"], queries=[query]))
    return out


def crossref_verify(doi: str, expected_title: str = "") -> dict:
    """Check that a DOI exists (and matches the title). Returns metadata + 'verified' flag."""
    d = normalize_doi(doi)
    if not d:
        return {"verified": False, "reason": "no DOI"}
    try:
        data = http_json("https://api.crossref.org/works/" + urllib.parse.quote(d, safe="/"))
    except SourceError as e:
        return {"verified": False, "reason": str(e)}
    m = data.get("message", {})
    title = (m.get("title") or [""])[0]
    year = None
    for k in ("published-print", "published-online", "issued", "created"):
        parts = (m.get(k) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            year = parts[0][0]
            break
    authors = [f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in (m.get("author") or [])][:8]
    sim = difflib.SequenceMatcher(None, _norm_title(title), _norm_title(expected_title)).ratio() if expected_title else 1.0
    return {"verified": sim >= 0.6, "title": title, "year": year, "authors": authors,
            "venue": (m.get("container-title") or [""])[0], "title_similarity": round(sim, 3),
            "reason": "" if sim >= 0.6 else "DOI exists but its title does not match"}


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"<[^>]+>", "", (t or "").lower()))


def merge_papers(papers: list[Paper]) -> dict[str, Paper]:
    """De-duplicate by DOI; papers without DOI are matched by normalised title."""
    title_to_doi = {_norm_title(p.title)[:80]: p.doi for p in papers if p.doi and p.title}
    out: dict[str, Paper] = {}
    for p in papers:
        tkey = _norm_title(p.title)[:80]
        doi = p.doi or title_to_doi.get(tkey, "")
        k = doi or (f"t:{tkey}" if tkey else p.key)
        if k in out:
            q = out[k]
            if not q.doi and doi:
                q.doi = doi
            q.abstract = q.abstract if len(q.abstract) >= len(p.abstract) else p.abstract
            q.tldr = q.tldr or p.tldr
            q.oa_url = q.oa_url or p.oa_url
            q.cited_by = max(q.cited_by or 0, p.cited_by or 0)
            q.sources = sorted(set(q.sources + p.sources))
            q.queries = sorted(set(q.queries + p.queries))
            q.venue = q.venue or p.venue
            q.verified = q.verified or p.verified
        else:
            p.key = k
            p.doi = doi
            out[k] = p
    return out

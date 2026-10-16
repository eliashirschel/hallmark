#!/usr/bin/env python3
"""Re-check the entries ``relabel_ground_truth.py`` flipped HALLUCINATED -> VALID.

The 2026-05-30 relabel (commit 1743810) accepted a flip when the title matched,
the author list was "faithful", the venue was "not refuted" and the DOI
resolved. reference-audit's ``hallmark_label_errors.md`` reports three blind
spots in that gate, and this script re-tests every flip against tighter rules:

1. DOI: the cited DOI must belong to the cited paper, not merely resolve. Its
   own title (Crossref, or DataCite for ``10.48550/arXiv.*``) must match.
2. Authors: every cited person must be an author of the paper, compared person
   by person (surname plus first initial) against DBLP and OpenAlex, instead of
   a last-name share of at least 0.75. A list that drops authors without
   "and others" is reported as partial.
3. Venue: a cited conference or journal must appear on a non-arXiv record
   (DBLP, then OpenAlex). A paper whose only records are arXiv/CoRR cannot
   confirm it.

Name variants (Liam / William Fedus) are reported, not resolved: a flagged
author is a prompt to check by hand, not a verdict.

Usage:
    uv run python scripts/recheck_relabel_flips.py --splits dev_public,test_public
    # test_hidden needs data/v1.2/hidden/test_hidden.jsonl (gitignored); its
    # per-entry output goes to data/v1.2/hidden/ so it never reaches the public repo
    uv run python scripts/recheck_relabel_flips.py --splits test_hidden
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FLIPS = REPO / "results/reviewer_experiments/relabel_flips.json"
CACHE = REPO / "results/relabel_recheck/http_cache.json"
PUBLIC_OUT = REPO / "results/relabel_recheck"
HIDDEN_OUT = REPO / "data/v1.2/hidden"
SPLIT_FILES = {
    "dev_public": REPO / "data/v1.2/dev_public.jsonl",
    "test_public": REPO / "data/v1.2/test_public.jsonl",
    "test_hidden": REPO / "data/v1.2/hidden/test_hidden.jsonl",
}
TITLE_MATCH = 0.9

# Cited-venue keywords -> the DBLP / OpenAlex venue strings that confirm them.
VENUES: dict[str, tuple[str, ...]] = {
    "cvpr": ("cvpr", "computer vision and pattern recognition"),
    "iccv": ("iccv", "international conference on computer vision"),
    "eccv": ("eccv", "european conference on computer vision"),
    "neurips": ("neurips", "nips", "neural information processing systems"),
    "icml": ("icml", "international conference on machine learning"),
    "iclr": ("iclr", "international conference on learning representations"),
    "acl": ("acl", "association for computational linguistics"),
    "emnlp": ("emnlp", "empirical methods in natural language processing"),
    "naacl": ("naacl", "north american chapter"),
    "aaai": ("aaai",),
    "ijcai": ("ijcai",),
    "jmlr": ("jmlr", "j. mach. learn. res", "journal of machine learning research"),
    "tmlr": ("tmlr", "transactions on machine learning research"),
    "tpami": ("tpami", "pattern analysis and machine intelligence"),
    "nature": ("nature",),
    "science": ("science",),
}
VENUE_ALIASES = {"nips": "neurips", "neural information processing": "neurips"}
PREPRINT_VENUES = ("corr", "arxiv", "biorxiv", "medrxiv", "ssrn")

_cache: dict[str, object] = {}


def _get(url: str, *, accept: str = "application/json") -> object | None:
    if url in _cache:
        return _cache[url]
    req = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": "hallmark-relabel-recheck"}
    )
    data: object | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"lookup failed 3 times: {url}")
    _cache[url] = data
    time.sleep(0.3)
    return data


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.replace("\\", "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower().replace("{", "").replace("}", "")).strip()


def title_sim(a: str, b: str) -> float:
    return SequenceMatcher(None, " ".join(fold(a).split()), " ".join(fold(b).split())).ratio()


def split_authors(raw: str) -> tuple[list[str], bool]:
    names = [n.strip() for n in re.split(r"\s+and\s+", raw.replace("\n", " ")) if n.strip()]
    truncated = bool(names) and fold(names[-1]) in {"others", "et al"}
    return (names[:-1] if truncated else names), truncated


def person(name: str) -> tuple[str, str]:
    """(surname, first initial), from 'Last, First' or 'First Last'; DBLP's '0001' suffix dropped."""
    name = re.sub(r"\s+\d{4}$", "", name.strip())
    if "," in name:
        last, first = (p.strip() for p in name.split(",", 1))
    else:
        parts = name.split()
        last, first = (parts[-1], " ".join(parts[:-1])) if parts else ("", "")
    f = fold(first)
    return fold(last).replace(" ", ""), f[:1]


def same_person(cited: tuple[str, str], real: tuple[str, str]) -> bool:
    if not cited[0] or cited[0] != real[0]:
        return False
    return not cited[1] or not real[1] or cited[1] == real[1]


# --- sources -----------------------------------------------------------------


def dblp_records(title: str) -> list[dict]:
    words = [w for w in fold(title).split() if len(w) > 3][:4] or fold(title).split()[:2]
    text = " . ".join(f'?t ql:contains-word "{w}"' for w in words)
    query = (
        "PREFIX dblp: <https://dblp.org/rdf/schema#> "
        "PREFIX ql: <http://qlever.cs.uni-freiburg.de/builtin-functions/> "
        'SELECT ?p ?title ?venue ?doi (GROUP_CONCAT(?name; separator="|") AS ?authors) WHERE { '
        f"?p dblp:title ?title . ?t ql:contains-entity ?title . {text} . "
        "OPTIONAL { ?p dblp:publishedIn ?venue } OPTIONAL { ?p dblp:doi ?doi } "
        "?p dblp:authoredBy ?a . ?a dblp:primaryCreatorName ?name } "
        "GROUP BY ?p ?title ?venue ?doi LIMIT 40"
    )
    url = "https://sparql.dblp.org/sparql?" + urllib.parse.urlencode({"query": query})
    data = _get(url, accept="application/sparql-results+json")
    out = []
    for b in (data or {}).get("results", {}).get("bindings", []):  # type: ignore[union-attr]
        if title_sim(b["title"]["value"], title) < TITLE_MATCH:
            continue
        out.append(
            {
                "source": "dblp",
                "id": b["p"]["value"].rsplit("/rec/", 1)[-1],
                "venue": b.get("venue", {}).get("value", ""),
                "doi": b.get("doi", {}).get("value", "").removeprefix("https://doi.org/"),
                "authors": b["authors"]["value"].split("|"),
            }
        )
    return out


def openalex_records(title: str) -> list[dict]:
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(
        {"search": fold(title), "per-page": "5"}
    )
    data = _get(url)
    out = []
    for w in (data or {}).get("results", []):  # type: ignore[union-attr]
        if title_sim(w.get("title") or "", title) < TITLE_MATCH:
            continue
        venues = {
            (loc.get("source") or {}).get("display_name") or "" for loc in w.get("locations", [])
        }
        out.append(
            {
                "source": "openalex",
                "id": w["id"].rsplit("/", 1)[-1],
                "venues": sorted(v for v in venues if v),
                "doi": (w.get("doi") or "").removeprefix("https://doi.org/"),
                "authors": [a["author"]["display_name"] for a in w.get("authorships", [])],
            }
        )
    return out


def doi_owner_title(doi: str) -> tuple[str | None, str]:
    """Title registered for ``doi``, and where it came from. (None, ...) = unregistered."""
    q = urllib.parse.quote(doi)
    if doi.lower().startswith("10.48550/"):
        data = _get(f"https://api.datacite.org/dois/{q}")
        titles = ((data or {}).get("data") or {}).get("attributes", {}).get("titles", [])  # type: ignore[union-attr]
        return (titles[0]["title"] if titles else None), "datacite"
    data = _get(f"https://api.crossref.org/works/{q}")
    message = (data or {}).get("message") or {}  # type: ignore[union-attr]
    titles = message.get("title", [])
    if not titles:
        return None, "crossref"
    # Crossref splits some titles ('NeRF' + 'representing scenes as ...'); compare the whole.
    subtitle = message.get("subtitle", [])
    return (f"{titles[0]}: {subtitle[0]}" if subtitle else titles[0]), "crossref"


# --- checks ------------------------------------------------------------------


def check_doi(fields: dict) -> dict | None:
    doi = (fields.get("doi") or "").strip().removeprefix("https://doi.org/")
    if not doi:
        return None
    owner, via = doi_owner_title(doi)
    if owner is None:
        return {"status": "error", "detail": f"{doi} is not registered ({via})"}
    sim = title_sim(owner, fields.get("title", ""))
    if sim >= TITLE_MATCH:
        return {"status": "ok", "detail": f"{doi} is this paper ({via})"}
    return {"status": "error", "detail": f"{doi} belongs to '{owner}' ({via}, sim={sim:.2f})"}


def check_authors(fields: dict, records: list[dict]) -> dict | None:
    cited, truncated = split_authors(fields.get("author", ""))
    if not cited:
        return None
    lists = [[person(a) for a in r["authors"]] for r in records if r["authors"]]
    if not lists:
        return {"status": "unconfirmed", "detail": "no source returned an author list"}
    missing = [n for n in cited if not any(same_person(person(n), p) for lst in lists for p in lst)]
    seen = [person(n) for n in cited]
    dupes = sorted({cited[i] for i, p in enumerate(seen) if seen.count(p) > 1})
    real_n = max(len(lst) for lst in lists)
    if missing or dupes:
        detail = f"{len(missing)} of {len(cited)} cited not on the paper: {', '.join(missing)}"
        if dupes:
            detail += f"; listed twice: {', '.join(dupes)}"
        return {"status": "error", "detail": detail}
    if not truncated and len(cited) < real_n:
        return {
            "status": "partial",
            "detail": f"cites {len(cited)} of {real_n} authors without 'and others'",
        }
    return {"status": "ok", "detail": f"{len(cited)} cited, all on the paper ({real_n} total)"}


def cited_venue_key(fields: dict) -> tuple[str, str] | None:
    raw = fields.get("booktitle") or fields.get("journal") or ""
    if not raw:
        return None
    f = fold(raw)
    for alias, key in VENUE_ALIASES.items():
        if alias in f:
            return raw, key
    for key, needles in VENUES.items():
        if any(re.search(rf"\b{re.escape(n)}\b", f) for n in needles):
            return raw, key
    return raw, ""


def check_venue(fields: dict, records: list[dict]) -> dict | None:
    cited = cited_venue_key(fields)
    if cited is None:
        return None
    raw, key = cited
    if any(p in fold(raw) for p in PREPRINT_VENUES):
        return {"status": "ok", "detail": f"cites a preprint server ('{raw}')"}
    venues = sorted(
        {v for r in records for v in ([r["venue"]] if "venue" in r else r["venues"]) if v}
    )
    published = [v for v in venues if not any(p in fold(v) for p in PREPRINT_VENUES)]
    if not published:
        return {
            "status": "unconfirmed",
            "detail": f"cites '{raw}', but every record is a preprint ({', '.join(venues) or 'none'})",
        }
    if not key:
        return {"status": "manual", "detail": f"cites '{raw}'; records: {', '.join(published)}"}
    needles = VENUES[key]
    if any(re.search(rf"\b{re.escape(n)}\b", fold(v)) for v in published for n in needles):
        return {"status": "ok", "detail": f"'{raw}' confirmed by {', '.join(published)}"}
    return {"status": "error", "detail": f"cites '{raw}', published at {', '.join(published)}"}


def recheck(entry: dict) -> dict:
    fields = entry["fields"]
    title = fields.get("title", "")
    records = dblp_records(title) + openalex_records(title)
    checks = {
        "doi": check_doi(fields),
        "authors": check_authors(fields, records),
        "venue": check_venue(fields, records),
    }
    checks = {k: v for k, v in checks.items() if v is not None}
    statuses = {v["status"] for v in checks.values()}
    if "error" in statuses:
        verdict = "HALLUCINATED"
    elif statuses & {"partial", "unconfirmed", "manual"}:
        verdict = "CHECK_BY_HAND"
    else:
        verdict = "VALID"
    return {
        "bibtex_key": entry["bibtex_key"],
        "title": title,
        "current_label": entry["label"],
        "recheck_verdict": verdict,
        "checks": checks,
        "records": [f"{r['source']}:{r['id']}" for r in records],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--splits", default="dev_public,test_public")
    args = ap.parse_args()

    if CACHE.exists():
        _cache.update(json.loads(CACHE.read_text()))
    flips = json.loads(FLIPS.read_text())
    try:
        for split in args.splits.split(","):
            keys = set(flips[split])
            entries = [
                e
                for line in SPLIT_FILES[split].read_text().splitlines()
                if line.strip() and (e := json.loads(line))["bibtex_key"] in keys
            ]
            if len(entries) != len(keys):
                raise SystemExit(
                    f"[{split}] {len(keys) - len(entries)} flipped keys not in the split"
                )
            rows = [recheck(e) for e in entries]
            out_dir = HIDDEN_OUT if split == "test_hidden" else PUBLIC_OUT
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"recheck_flips_{split}.json"
            out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["recheck_verdict"]] = counts.get(r["recheck_verdict"], 0) + 1
            print(f"[{split}] {len(rows)} flips -> {counts}  ({out.relative_to(REPO)})")
    finally:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(_cache))


if __name__ == "__main__":
    main()

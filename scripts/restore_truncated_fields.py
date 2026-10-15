#!/usr/bin/env python3
"""Restore BibTeX fields that were cut off at a LaTeX brace.

When the entries were parsed from BibTeX, a field whose value held an accent
command or a protected word (``R{\\'e}, Christopher``, ``{AlphaFold}``) was cut at
that brace: ``R{\\'e`` and every author after it were lost. Thirteen entries are
affected (7 dev, 2 test, 4 hidden). Their raw BibTeX was never damaged:

* dev / test: ``raw_bibtex`` in ``data/v1.0/<split>.jsonl`` at commit d65e794
  (the v1.2 records carry ``raw_bibtex: null``);
* test_hidden: ``raw_bibtex`` in the record itself.

Each cut field is re-read from the raw BibTeX by brace counting, written in the
record's own author style, and written only if it passes a prefix check: every
name before the cut matches exactly, and the cut name is the start of the
restored one. Any failure stops the run before a file is written.

The restored text is what the generator wrote, so deliberate defects survive.
Two labels were derived from the cut text and are corrected (``OVERRIDES``):
ViViT (``swapped_authors`` came from the cut list; its real defect is the DOI)
and one test_hidden entry (a fully correct citation labelled from the old CrossRef/arXiv-DOI
bug; its override lives in the private hidden spec).

Usage:
    uv run python scripts/restore_truncated_fields.py --check   # report, write nothing
    uv run python scripts/restore_truncated_fields.py           # write the fixes
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW_COMMIT = "d65e794"
RELABELED_BY = "truncation-restore-2026-10-13"
SPLITS = {
    "dev_public": (REPO / "data/v1.2/dev_public.jsonl", REPO / "data/v1.2/dev_public_blind.jsonl"),
    "test_public": (
        REPO / "data/v1.2/test_public.jsonl",
        REPO / "data/v1.2/test_public_blind.jsonl",
    ),
    "test_hidden": (REPO / "data/v1.2/hidden/test_hidden.jsonl", None),
}
PUBLIC_LOG = REPO / "results/relabel_recheck/restore_log_public.json"
HIDDEN_LOG = REPO / "data/v1.2/hidden/restore_log_test_hidden.json"

ALL_TRUE = dict.fromkeys(
    ("doi_resolves", "title_exists", "authors_match", "venue_correct", "fields_complete"), True
)
OVERRIDES: dict[str, dict] = {
    "e65a9f529e01": {  # ViViT
        "label": "HALLUCINATED",
        "hallucination_type": "hybrid_fabrication",
        "difficulty_tier": 2,
        # EXPECTED_SUBTESTS[hybrid_fabrication]: title and authors are judged against
        # the paper the DOI resolves to, so both are False.
        "subtests": {
            **ALL_TRUE,
            "title_exists": False,
            "authors_match": False,
            "cross_db_agreement": False,
        },
        "explanation": (
            "Real paper (ViViT, ICCV 2021) with its correct authors, but the cited DOI "
            "10.1109/ICCV48922.2021.00678 belongs to 'Learning to Cut by Watching Movies'."
        ),
        "relabeled_from": "HALLUCINATED",
        "relabel_reason": (
            "Retyped swapped_authors -> hybrid_fabrication: swapped_authors came from comparing "
            "an author list cut off at 'Lu{\\v{c' with CrossRef; the full list is correct, "
            "and the DOI is another paper's (CrossRef)."
        ),
    },
}
# The test_hidden override would publish a hidden label, so it is read from
# the private spec (hallmark-hidden; copied to the gitignored data/v1.2/hidden/).
HIDDEN_SPEC = REPO / "data/v1.2/hidden/v1_2_4_fix_spec.json"
if HIDDEN_SPEC.exists():
    OVERRIDES.update(json.loads(HIDDEN_SPEC.read_text())["overrides"])


def broken(value: object) -> bool:
    return isinstance(value, str) and value.count("{") != value.count("}")


def raw_field(raw: str, name: str) -> str | None:
    m = re.search(rf"\b{name}\s*=\s*\{{", raw, re.I)
    if not m:
        return None
    i = j = m.end()
    depth = 1
    while depth and j < len(raw):
        depth += {"{": 1, "}": -1}.get(raw[j], 0)
        j += 1
    return raw[i : j - 1] if depth == 0 else None


def names(value: str) -> list[str]:
    return [n.strip() for n in re.split(r"\s+and\s+", value.strip()) if n.strip()]


def first_last(name: str) -> str:
    if "," not in name:
        return name
    last, first = (p.strip() for p in name.split(",", 1))
    return f"{first} {last}"


def restore_author(current: str, full: str) -> str:
    """Restored author field in the record's own style, or ValueError if the prefix check fails."""
    cur, raw_names = names(current), names(full)
    comma_style = any("," in n for n in cur[:-1])
    restored = raw_names if comma_style else [first_last(n) for n in raw_names]
    if len(restored) < len(cur):
        raise ValueError(f"restored list is shorter ({len(restored)} < {len(cur)})")
    for i, n in enumerate(cur[:-1]):
        if n != restored[i]:
            raise ValueError(f"name {i + 1} differs: {n!r} vs {restored[i]!r}")
    cut, idx = cur[-1], len(cur) - 1
    # The cut can also fall inside a given name with the surname kept after it
    # ('Fran{\c{c Beaufays' for 'Fran{\c{c}}oise Beaufays').
    cut_first, _, cut_last = cut.rpartition(" ")
    full_first, _, full_last = first_last(raw_names[idx]).rpartition(" ")
    inside_given = bool(cut_first) and cut_last == full_last and full_first.startswith(cut_first)
    if not (restored[idx].startswith(cut) or raw_names[idx].startswith(cut) or inside_given):
        raise ValueError(f"cut name {cut!r} is not the start of {restored[idx]!r}")
    return " and ".join(restored)


def raw_sources(split: str) -> dict[str, str]:
    if split == "test_hidden":
        return {}
    out = subprocess.run(
        ["git", "show", f"{RAW_COMMIT}:data/v1.0/{split}.jsonl"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    ).stdout
    return {
        r["bibtex_key"]: r["raw_bibtex"]
        for line in out.splitlines()
        if line.strip() and (r := json.loads(line)).get("raw_bibtex")
    }


def plan_split(split: str, records: list[dict]) -> list[dict]:
    old_raw = raw_sources(split)
    changes = []
    for r in records:
        bad = [k for k, v in r["fields"].items() if broken(v)]
        if not bad:
            continue
        raw = r.get("raw_bibtex") or old_raw.get(r["bibtex_key"])
        if not raw:
            raise SystemExit(f"[{split}] {r['bibtex_key']}: no raw BibTeX to restore from")
        fields = {}
        for k in bad:
            full = raw_field(raw, k)
            if full is None or broken(full):
                raise SystemExit(f"[{split}] {r['bibtex_key']}.{k}: raw field unreadable")
            if k == "author":
                try:
                    fields[k] = restore_author(r["fields"][k], full)
                except ValueError as e:
                    raise SystemExit(f"[{split}] {r['bibtex_key']}.author: {e}") from None
            elif full.startswith(r["fields"][k]):
                fields[k] = full
            else:
                raise SystemExit(f"[{split}] {r['bibtex_key']}.{k}: current value is not a prefix")
        changes.append(
            {
                "bibtex_key": r["bibtex_key"],
                "fields": {k: {"before": r["fields"][k], "after": v} for k, v in fields.items()},
                "record": {
                    k: {"before": r.get(k), "after": v}
                    for k, v in OVERRIDES.get(r["bibtex_key"], {}).items()
                },
            }
        )
    return changes


def apply(records: list[dict], changes: list[dict], *, blind: bool) -> None:
    by_key = {c["bibtex_key"]: c for c in changes}
    for r in records:
        c = by_key.get(r["bibtex_key"])
        if not c:
            continue
        for k, v in c["fields"].items():
            r["fields"][k] = v["after"]
        if not blind and c["record"]:
            for k, v in c["record"].items():
                r[k] = v["after"]
            r["relabeled_by"] = RELABELED_BY
            for stale in ("matched_source", "matched_id", "match_scores"):
                r.pop(stale, None)


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--check", action="store_true", help="report the planned changes, write nothing"
    )
    ap.add_argument("--splits", default=",".join(SPLITS))
    args = ap.parse_args()

    plans = {}
    for split in args.splits.split(","):
        labeled, _ = SPLITS[split]
        plans[split] = (read(labeled), plan_split(split, read(labeled)))
        print(f"[{split}] {len(plans[split][1])} entries pass the prefix check")
    if args.check:
        return

    public_log: dict[str, list[dict]] = {}
    for split, (records, changes) in plans.items():
        labeled, blind_path = SPLITS[split]
        apply(records, changes, blind=False)
        write(labeled, records)
        if blind_path is not None:
            blind = read(blind_path)
            apply(blind, changes, blind=True)
            write(blind_path, blind)
        if split == "test_hidden":
            HIDDEN_LOG.write_text(json.dumps(changes, indent=2, ensure_ascii=False) + "\n")
        else:
            public_log[split] = changes
    if public_log:
        PUBLIC_LOG.parent.mkdir(parents=True, exist_ok=True)
        PUBLIC_LOG.write_text(json.dumps(public_log, indent=2, ensure_ascii=False) + "\n")
    print("written")


if __name__ == "__main__":
    main()

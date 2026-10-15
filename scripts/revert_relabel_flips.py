#!/usr/bin/env python3
"""Revert the 2026-05-30 relabel flips that ``recheck_relabel_flips.py`` refuted.

``relabel_ground_truth.py`` (commit 1743810) flipped 29 entries HALLUCINATED ->
VALID. 18 of them cite metadata that does not belong to the paper; each was
checked against CrossRef, DataCite, DBLP, OpenAlex and OpenReview (see
``results/relabel_recheck/``). The gate that let them through had three gaps:

* it checked that a DOI resolves, not which paper it resolves to;
* it accepted an author list when 75% of the cited surnames were real;
* it took an arXiv-only record as "venue not refuted".

Each entry goes back to HALLUCINATED with the type its defect fits, not
necessarily its pre-relabel type: six of them were ``plausible_fabrication``,
which means the paper does not exist, and every one of these papers does. Sub-tests
follow ``EXPECTED_SUBTESTS`` for the new type. When an entry has a second defect
(a fabricated author list and a wrong venue), the type names the first and the
explanation names both.

Only labels change; the cited fields are untouched, so the blind files are too.

Usage:
    uv run python scripts/revert_relabel_flips.py --check   # report, write nothing
    uv run python scripts/revert_relabel_flips.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hallmark.dataset.schema import (  # noqa: E402
    EXPECTED_SUBTESTS,
    HALLUCINATION_TIER_MAP,
    HallucinationType,
)

RELABELED_BY = "relabel-recheck-2026-10-13"
SPLIT_FILES = {
    "dev_public": REPO / "data/v1.2/dev_public.jsonl",
    "test_public": REPO / "data/v1.2/test_public.jsonl",
    "test_hidden": REPO / "data/v1.2/hidden/test_hidden.jsonl",
}
PUBLIC_LOG = REPO / "results/relabel_recheck/revert_log_public.json"
HIDDEN_LOG = REPO / "data/v1.2/hidden/revert_log_test_hidden.json"

WRONG_DOI = "hybrid_fabrication"
AUTHORS = "swapped_authors"
VENUE = "wrong_venue"
PREPRINT = "preprint_as_published"

# split, key, new type, explanation. The test_hidden rows are not here: their keys
# and explanations would publish hidden labels, so they are read from the private
# spec (hallmark-hidden; copied to the gitignored data/v1.2/hidden/).
HIDDEN_SPEC = REPO / "data/v1.2/hidden/v1_2_4_fix_spec.json"
REVERTS: list[tuple[str, str, str, str]] = [
    (
        "dev_public",
        "d0f7f9c72c33",
        WRONG_DOI,
        "IBRNet (CVPR 2021); cited DOI 10.1109/CVPR46437.2021.00469 is 'Delving into Localization "
        "Errors for Monocular 3D Object Detection' (IBRNet is .00466).",
    ),
    (
        "dev_public",
        "f802800935ef",
        WRONG_DOI,
        "ImageBind (CVPR 2023); cited DOI 10.1109/CVPR52729.2023.02141 is 'Spatial-Frequency Mutual "
        "Learning for Face Super-Resolution' (ImageBind is .01457).",
    ),
    (
        "dev_public",
        "ff2931c3228f",
        WRONG_DOI,
        "MoCo v3 (ICCV 2021); cited DOI 10.1109/ICCV48922.2021.00952 is 'Do Image Classifiers "
        "Generalize Across Time?' (the paper is .00950).",
    ),
    (
        "dev_public",
        "ded9f5844e90",
        WRONG_DOI,
        "TensoRF (ECCV 2022); cited DOI 10.1007/978-3-031-19830-4_20 is 'ESS: Learning Event-Based "
        "Semantic Segmentation from Still Images' (TensoRF is 978-3-031-19824-3_20).",
    ),
    (
        "dev_public",
        "a24129d1c5e5",
        AUTHORS,
        "Flamingo (NeurIPS 2022); 4 of 19 cited authors (Koppula, Valko, Denoyer, Carreira) are not "
        "authors of the paper (DBLP conf/nips/AlayracDLMBHLMM22).",
    ),
    (
        "dev_public",
        "e9e08922a057",
        AUTHORS,
        "PaLM; at least 7 of 63 cited authors (e.g. Armand Joulin, Ed Chi) are not authors and Noam "
        "Shazeer is listed twice; also cited as ICML 2022, but PaLM is JMLR 2023 / arXiv 2022.",
    ),
    (
        "dev_public",
        "b939e55d7555",
        VENUE,
        "PaLM cited as ICML 2022; it was published in JMLR (2023) and on arXiv (2022), never at "
        "ICML (DBLP journals/jmlr/ChowdheryNDBMRBCSGSSTMRBTSPRDHPBAI23).",
    ),
    (
        "dev_public",
        "c65faf378a95",
        AUTHORS,
        "OPT; 3 of 10 named authors (Mona Dewan, Emily Dinan, Zhiguo Du) are not authors; also cited "
        "as ACL 2022, but OPT is an arXiv report only (DBLP journals/corr/abs-2205-01068).",
    ),
    (
        "dev_public",
        "db9d82ff3f94",
        PREPRINT,
        "Constitutional AI cited as ICLR 2023; it is an arXiv preprint only (DBLP "
        "journals/corr/abs-2212-08073; no conference record).",
    ),
    (
        "test_public",
        "eae03804fd48",
        WRONG_DOI,
        "Mip-NeRF (ICCV 2021); cited DOI 10.1109/ICCV48922.2021.00582 is 'Baking Neural Radiance "
        "Fields for Real-Time View Synthesis' (Mip-NeRF is .00580).",
    ),
    (
        "test_public",
        "e8e1379d3939",
        WRONG_DOI,
        "MAE (CVPR 2022); cited DOI 10.1109/CVPR52688.2022.01552 is 'Motion-from-Blur: 3D Shape and "
        "Motion Estimation of Motion-blurred Objects in Videos' (MAE is .01553).",
    ),
    (
        "test_public",
        "e7e5083fa94f",
        VENUE,
        "MiniGPT-4 cited as CVPR; it was published at ICLR 2024 (DBLP conf/iclr/Zhu0SLE24).",
    ),
    (
        "test_public",
        "d87ce678eb11",
        AUTHORS,
        "LLaMA 2; 15 of 70 cited names (e.g. 'Yuchen Zarov') are not on the paper; also cited as "
        "NeurIPS, but it is an arXiv report only (DBLP journals/corr/abs-2307-09288).",
    ),
    (
        "test_public",
        "c3e070166df2",
        PREPRINT,
        "LLaMA cited as ICLR 2023; it is an arXiv preprint only (DBLP journals/corr/abs-2302-13971).",
    ),
]
if HIDDEN_SPEC.exists():
    REVERTS += [tuple(row) for row in json.loads(HIDDEN_SPEC.read_text())["reverts"]]  # type: ignore[misc]

REASON = (
    "Reverted the systematic-relabel-2026-05-30 flip: re-checked against CrossRef/DataCite/DBLP/"
    "OpenAlex/OpenReview (scripts/recheck_relabel_flips.py); the citation's {defect}."
)
DEFECT = {
    WRONG_DOI: "DOI belongs to a different paper",
    AUTHORS: "author list names people who are not authors of the paper",
    VENUE: "venue is not where the paper was published",
    PREPRINT: "venue is claimed for a paper that exists only as a preprint",
}


def new_record(r: dict, htype: str, explanation: str) -> dict:
    t = HallucinationType(htype)
    subtests = dict(EXPECTED_SUBTESTS[t])
    subtests["doi_resolves"] = True  # all 18 cite a registered DOI (6 resolve to another paper)
    out = {k: v for k, v in r.items() if k not in ("matched_source", "matched_id", "match_scores")}
    out.update(
        label="HALLUCINATED",
        hallucination_type=htype,
        difficulty_tier=int(HALLUCINATION_TIER_MAP[t]),
        subtests=subtests,
        explanation=explanation,
        relabeled_from="VALID",
        relabel_reason=REASON.format(defect=DEFECT[htype]),
        relabeled_by=RELABELED_BY,
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--check", action="store_true", help="report the planned changes, write nothing"
    )
    args = ap.parse_args()

    logs: dict[str, list[dict]] = {}
    for split, path in SPLIT_FILES.items():
        todo = {k: (t, e) for s, k, t, e in REVERTS if s == split}
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        found = {r["bibtex_key"] for r in records} & todo.keys()
        if found != todo.keys():
            raise SystemExit(f"[{split}] keys not in the split: {sorted(todo.keys() - found)}")
        log = []
        for i, r in enumerate(records):
            if r["bibtex_key"] not in todo:
                continue
            if r["label"] != "VALID" or r.get("relabeled_by") != "systematic-relabel-2026-05-30":
                raise SystemExit(
                    f"[{split}] {r['bibtex_key']} is not a May-30 VALID flip; refusing"
                )
            records[i] = new_record(r, *todo[r["bibtex_key"]])
            log.append(
                {
                    "bibtex_key": r["bibtex_key"],
                    "before": {
                        k: r.get(k) for k in ("label", "hallucination_type", "relabel_reason")
                    },
                    "after": {
                        k: records[i][k] for k in ("label", "hallucination_type", "difficulty_tier")
                    },
                }
            )
        print(f"[{split}] {len(log)} entries VALID -> HALLUCINATED")
        logs[split] = log
        if not args.check:
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    if args.check:
        return
    HIDDEN_LOG.write_text(json.dumps(logs.pop("test_hidden"), indent=2, ensure_ascii=False) + "\n")
    PUBLIC_LOG.write_text(json.dumps(logs, indent=2, ensure_ascii=False) + "\n")
    print("written")


if __name__ == "__main__":
    main()

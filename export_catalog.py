"""Export a rich, filterable catalog of your library — the file to
upload to Claude when designing an outline or judging coverage.

Where a research pack answers "what do my papers say about X?", the
catalog answers "what does my library contain?": per paper, the entry
number, title, year, category, authors, the abstract (fetched by Stage
1), and — with --full — every figure and table caption from the index.
That's the material Claude reasons over when structuring a chapter and
deciding which papers anchor which section.

Usage:
    python export_catalog.py                          # brief: key info only
    python export_catalog.py --full                   # abstracts + captions
    python export_catalog.py --category solar-cell --since 2024 --full
    python export_catalog.py --years 2024             # exactly one year
    python export_catalog.py --years 2020,2023-2025   # any mix of years/ranges
    python export_catalog.py --category solar-cell,LED  # several categories
    python export_catalog.py --journal "nature energy,joule"  # journal name contains
    python export_catalog.py --entries 12,45,100-110  # specific Entry numbers
    python export_catalog.py --status downloaded      # only papers with PDFs

Filter to the topic of the chapter you're planning — a focused catalog
of a few hundred papers reads far better than everything at once.
"""
import argparse
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime

from doi_resolver import load_config, load_tracking, resolve_output_path
from build_index import DB_PATH, parse_number_spec

DOWNLOADED_STATUSES = ("downloaded", "downloaded_via_proxy")


def load_captions_by_doi():
    # Figure/table captions live in the search index, keyed by DOI —
    # join them back onto the catalog rows when available.
    if not os.path.exists(DB_PATH):
        return {}
    captions = {}
    db = sqlite3.connect(DB_PATH)
    try:
        rows = db.execute(
            "SELECT doi, text FROM chunks WHERE kind IN ('figure', 'table') ORDER BY doi, page"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        db.close()
    for doi, text in rows:
        captions.setdefault(doi, []).append(text)
    return captions


def main():
    parser = argparse.ArgumentParser(description="Export a library catalog for Claude.")
    parser.add_argument("--full", action="store_true", help="include abstracts and figure/table captions")
    parser.add_argument("--category", help="one or more categories, comma-separated (e.g. solar-cell,LED)")
    parser.add_argument("--journal", help="only papers whose journal name contains this text; "
                        "comma-separate alternatives (e.g. \"nature energy,joule\")")
    parser.add_argument("--since", type=int, help="only papers from this year onward")
    parser.add_argument("--until", type=int, help="only papers up to this year")
    parser.add_argument("--years", help="specific year(s): 2024, or 2020,2023-2025 (mix of years and ranges)")
    parser.add_argument("--entries", help="only these Entry numbers: 12,45,100-110")
    parser.add_argument("--status", help="only rows with this status (e.g. downloaded)")
    parser.add_argument("--out", help="output file or folder "
                        "(default: catalogs/catalog_<timestamp>.md; a folder keeps the default name inside it)")
    args = parser.parse_args()

    config = load_config()
    tracking = load_tracking(config["paths"]["tracking_csv"])
    if not tracking:
        sys.exit("No tracking sheet found — run the pipeline first.")

    rows = []
    for row in tracking.values():
        status = row.get("Status", "")
        if args.status:
            if status != args.status:
                continue
        elif status not in DOWNLOADED_STATUSES:
            continue  # by default, catalog only what's actually in the library
        if args.category:
            wanted = {c.strip().lower() for c in args.category.split(",") if c.strip()}
            if (row.get("Category", "") or "").lower() not in wanted:
                continue
        if args.journal:
            journal = (row.get("Journal", "") or "").lower()
            wanted_journals = [j.strip().lower() for j in args.journal.split(",") if j.strip()]
            if not any(j in journal for j in wanted_journals):
                continue
        year = row.get("Year", "")
        if args.since and (not year.isdigit() or int(year) < args.since):
            continue
        if args.until and (not year.isdigit() or int(year) > args.until):
            continue
        if args.years:
            if not year.isdigit() or int(year) not in parse_number_spec(args.years):
                continue
        if args.entries:
            entry = str(row.get("Entry", "") or "")
            if not entry.isdigit() or int(entry) not in parse_number_spec(args.entries):
                continue
        rows.append(row)

    if not rows:
        message = "No papers match those filters."
        if args.journal and not any((r.get("Journal") or "").strip() for r in tracking.values()):
            message += (
                "\nNote: the tracking sheet has no journal names yet — run "
                "python doi_resolver.py once to fetch them from CrossRef."
            )
        sys.exit(message)
    rows.sort(key=lambda r: int(r["Entry"]) if str(r.get("Entry", "")).isdigit() else 0)

    captions_by_doi = load_captions_by_doi() if args.full else {}

    categories = Counter((r.get("Category") or "?") for r in rows)
    years = Counter((r.get("Year") or "?") for r in rows)

    lines = [
        "# Library catalog",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M}. {len(rows)} paper(s) after filters.",
        "",
        "**By category:** " + ", ".join(f"{c}: {n}" for c, n in categories.most_common()),
        "",
        "**By year:** " + ", ".join(f"{y}: {n}" for y, n in sorted(years.items())),
        "",
        "Use this catalog to judge coverage and design outlines; cite papers "
        "by their [Entry N] labels. Full text passages come separately via "
        "research packs (export_for_claude.py).",
        "",
    ]

    for r in rows:
        header = f"## [Entry {r.get('Entry', '?')}] {r.get('Title', '').strip()}"
        meta_bits = [b for b in (r.get("Year", ""), r.get("Journal", ""), r.get("Category", ""), r.get("DOI", "")) if b]
        lines += [header, "", f"*{' | '.join(meta_bits)}*", ""]
        authors = (r.get("Authors") or "").strip()
        if authors:
            lines += [f"Authors: {authors}", ""]
        abstract = (r.get("Abstract") or "").strip()
        key_info = (r.get("Key_Info") or "").strip()
        if args.full and abstract and not abstract.startswith("(no abstract"):
            lines += [abstract, ""]
        elif key_info:
            lines += [key_info, ""]
        if args.full:
            for caption in captions_by_doi.get(r.get("DOI", ""), [])[:8]:
                lines += [f"- {caption[:250]}"]
            if captions_by_doi.get(r.get("DOI", "")):
                lines += [""]

    output = "\n".join(lines)
    out_path = resolve_output_path(args.out, "catalogs", f"catalog_{datetime.now():%Y%m%d_%H%M%S}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)

    size_kb = os.path.getsize(out_path) / 1024
    print(f"Catalog written to {out_path} ({size_kb:.0f} KB, {len(rows)} papers).")
    if size_kb > 1500:
        print(
            "That's a lot for one upload — consider narrowing with "
            "--category/--since, or dropping --full."
        )
    print("Upload it to Claude when you want an outline or a coverage assessment.")


if __name__ == "__main__":
    main()

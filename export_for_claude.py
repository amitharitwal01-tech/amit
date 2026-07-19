"""Bridge between your local library and Claude: build a "research
pack" — one self-contained file holding your article outline plus the
most relevant passages and figure captions retrieved from YOUR indexed
papers, with full citation metadata.

Upload the pack to Claude in chat and ask it to write the article. The
pack instructs the writer to use only the included passages and to cite
every claim as [Entry N, p.X], so the draft stays grounded in your
library even though the writing happens elsewhere.

Usage:
    python export_for_claude.py outline.txt
    python export_for_claude.py outline.txt --per-section 15

Outline format — plain text, exactly how you'd naturally write it:
    1. Introduction
    A paragraph describing what this section must cover...

    2. Process windows and mismatch taxonomy
    Another spec paragraph...
    Figure: something showing dose-response flatness

Lines starting with a number ("1.", "2)") or "#" begin a new section;
everything under a heading is that section's specification.
"""
import argparse
import os
import re
import shutil
import sys
from datetime import datetime

from doi_resolver import load_config, sanitize_filename
from ask_library import split_into_subquestions
import build_index

SECTION_HEADING = re.compile(r"^(?:\d+[.)]\s+|#+\s+)(.+)$")


def build_figure_image_map(db):
    # (doi, page) -> list of (caption, image_path) from the figures
    # table, so a retrieved figure caption can be mapped back to the
    # actual image file on disk for bundling and embedding.
    mapping = {}
    try:
        rows = db.execute("SELECT doi, page, caption, image_path FROM figures").fetchall()
    except Exception:
        return mapping
    for doi, page, caption, image_path in rows:
        mapping.setdefault((doi, page), []).append((caption or "", image_path))
    return mapping


def image_path_for_figure(figure_map, result):
    # Match a retrieved figure chunk to its image file: same doi+page,
    # and when several figures share a page, the one whose caption text
    # appears in the chunk.
    candidates = figure_map.get((result["doi"], result["page"]), [])
    if not candidates:
        return None
    chunk_text = result["text"].lower()
    for caption, path in candidates:
        snippet = (caption or "")[:40].lower().strip()
        if snippet and snippet in chunk_text and path and os.path.exists(path):
            return path
    for _, path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def parse_outline(text):
    sections = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        heading = SECTION_HEADING.match(stripped)
        if heading:
            current = {"heading": heading.group(1).strip(), "spec": []}
            sections.append(current)
        elif stripped:
            if current is None:
                current = {"heading": "Main", "spec": []}
                sections.append(current)
            current["spec"].append(stripped)
    for section in sections:
        section["spec"] = " ".join(section["spec"])
    return [s for s in sections if s["heading"] or s["spec"]]


def gather_for_section(db, embedder, section, per_section):
    queries = [section["heading"]]
    if section["spec"]:
        queries.append(section["spec"][:300])
        queries.extend(split_into_subquestions(section["spec"]))

    passages = []
    seen_texts = set()
    for query in queries:
        for result in build_index.retrieve(db, embedder, query, top_k=6):
            marker = result["text"][:120]
            if marker in seen_texts:
                continue
            seen_texts.add(marker)
            passages.append(result)
    passages.sort(key=lambda r: -r["score"])

    figures = []
    figure_query = section["spec"] or section["heading"]
    for result in build_index.retrieve(db, embedder, figure_query, top_k=3, kind="figure"):
        figures.append(result)

    return passages[:per_section], figures


def build_pack(outline_text, per_section, figures_dir):
    db = build_index.open_db()
    paper_count = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    if paper_count == 0:
        sys.exit("The library index is empty — run  python build_index.py  first.")
    embedder = build_index.get_embedder()
    figure_map = build_figure_image_map(db)

    sections = parse_outline(outline_text)
    if not sections:
        sys.exit("Couldn't find any sections in the outline file.")

    os.makedirs(figures_dir, exist_ok=True)
    copied_figures = 0

    lines = [
        "# Research pack",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} from a library of {paper_count} indexed paper(s).",
        "",
        "## Instructions for the writer",
        "",
        "Write the article defined by the outline below, section by section, "
        "using ONLY the source passages included in this pack as factual "
        "material. Every factual claim must cite its source as [Entry N, p.X], "
        "matching the passage labels. If the passages don't support something "
        "the outline asks for, say so explicitly rather than filling the gap "
        "from general knowledge. Scientific register; define terms; quantify "
        "where the sources quantify. Build the reference list from the "
        "References catalog at the end, citing only entries actually used.",
        "",
        "**Deliverable:** produce the article as a Word (.docx) file. Where a "
        "section lists a candidate figure with an `image file:` name, and the "
        "matching image was uploaded alongside this pack, embed that image at "
        "the appropriate point with a caption 'Figure N. ... (adapted from "
        "[Entry M]).' Reused published figures need the publisher's permission "
        "before actual submission — flag each one so the author can clear it.",
        "",
        "## Outline (verbatim)",
        "",
        "```",
        outline_text.strip(),
        "```",
        "",
    ]

    used_entries = {}
    for i, section in enumerate(sections, 1):
        print(f"[{i}/{len(sections)}] retrieving for {section['heading'][:60]!r} ...")
        passages, figures = gather_for_section(db, embedder, section, per_section)

        lines += [f"## Section {i}: {section['heading']}", ""]
        if section["spec"]:
            lines += [f"**Specification:** {section['spec']}", ""]
        if not passages:
            lines += ["_No relevant passages found in the library for this section._", ""]
        else:
            lines.append("### Source passages")
            lines.append("")
            for r in passages:
                used_entries[r["entry"]] = r
                lines.append(
                    f"**[Entry {r['entry']}, p.{r['page']}]** ({r['year']}, {r['category']}) {r['title']}"
                )
                lines.append(f"> {r['text']}")
                lines.append("")
        if figures:
            lines.append("### Candidate figures from the library")
            lines.append("")
            for r in figures:
                used_entries[r["entry"]] = r
                # Copy the actual image into the upload folder under a
                # name that ties it to its entry, so the writer can map
                # caption -> file -> placement when embedding it.
                src = image_path_for_figure(figure_map, r)
                image_note = ""
                if src:
                    ext = os.path.splitext(src)[1] or ".png"
                    fname = f"Entry{r['entry']}_p{r['page']}{ext}"
                    try:
                        shutil.copy2(src, os.path.join(figures_dir, fname))
                        copied_figures += 1
                        image_note = f"  —  image file: `{fname}`"
                    except Exception:
                        pass
                lines.append(f"- **[Entry {r['entry']}, p.{r['page']}]** {r['text']}{image_note}")
            lines += [
                "",
                f"_(Image files copied to the `{os.path.basename(figures_dir)}` "
                "folder — upload that whole folder together with this pack so "
                "the writer can embed the figures.)_",
                "",
            ]

    lines += ["## References catalog", ""]
    for entry in sorted(used_entries, key=lambda e: int(e) if str(e).isdigit() else 0):
        r = used_entries[entry]
        doi_row = build_index.open_db().execute(
            "SELECT doi FROM papers WHERE entry = ?", (str(entry),)
        ).fetchone()
        doi = doi_row[0] if doi_row else ""
        doi_part = f" https://doi.org/{doi}" if doi and doi.startswith("10.") else ""
        lines.append(f"- [Entry {entry}] {r['title']} ({r['year']}).{doi_part}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Build a research pack for Claude from your outline.")
    parser.add_argument("outline", help="path to your outline text file")
    parser.add_argument("--per-section", type=int, default=15,
                        help="max source passages per section (default 15)")
    args = parser.parse_args()

    if not os.path.exists(args.outline):
        sys.exit(f"Outline file not found: {args.outline}")
    with open(args.outline, "r", encoding="utf-8") as f:
        outline_text = f.read()

    load_config()  # fail early with a clear message if the config is broken

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"research_pack_{stamp}.md"
    figures_dir = f"research_pack_{stamp}_figures"
    pack = build_pack(outline_text, args.per_section, figures_dir)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(pack)
    size_kb = os.path.getsize(out_path) / 1024

    fig_count = len(os.listdir(figures_dir)) if os.path.isdir(figures_dir) else 0
    if fig_count == 0 and os.path.isdir(figures_dir):
        os.rmdir(figures_dir)

    print()
    print(f"Research pack written to {out_path} ({size_kb:.0f} KB).")
    if fig_count:
        print(f"Candidate figure images copied to {figures_dir}\\ ({fig_count} image(s)).")
        print("Upload BOTH the pack file and the figures folder to Claude, then")
        print("ask it to write the article as a .docx with the figures embedded.")
    else:
        print("Upload the pack to Claude and ask it to write the article as a .docx.")


if __name__ == "__main__":
    main()

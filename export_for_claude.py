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
    python export_for_claude.py outline.docx --journal "nature energy" --since 2023

The outline can be .txt, .md, or a Word .docx file. Library filters
(--journal, --years, --since/--until, --category, --entries) restrict
which papers the pack may draw passages from — see ask_library.py for
the same flags.

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
import html
import os
import re
import sys
from datetime import datetime

from doi_resolver import load_config, read_text_flexible, resolve_output_path
from ask_library import split_into_subquestions
import build_index

SECTION_HEADING = re.compile(r"^(?:\d+[.)]\s+|#+\s+)(.+)$")
HORIZONTAL_RULE = re.compile(r"^[-–—_=]{5,}$")


def read_outline(path):
    """The outline as plain text — whether it lives in a .txt/.md file
    (any common encoding) or a Word .docx, since outlines get drafted in
    Word as often as in Notepad."""
    if path.lower().endswith(".docx"):
        # Deliberately dependency-free, same as extract_cited_references:
        # a .docx is a zip and word/document.xml holds the text; paragraph
        # ends become newlines so the outline's line structure survives.
        import zipfile
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = xml.replace("</w:p>", "</w:p>\n")
        text = re.sub(r"<[^>]+>", "", xml)
        return html.unescape(text)
    return read_text_flexible(path)


def parse_outline(text):
    lines = text.splitlines()

    # Many outlines carry front-matter before the real sections: a title,
    # audience/length notes, and a "review philosophy" list of guiding
    # questions — often numbered, which the heading pattern would wrongly
    # read as sections. When the outline separates sections with a
    # horizontal rule (----), treat everything before the FIRST rule as
    # front-matter and skip it, so only the real sections remain.
    rule_positions = [i for i, ln in enumerate(lines) if HORIZONTAL_RULE.match(ln.strip())]
    if rule_positions:
        lines = lines[rule_positions[0] + 1:]

    sections = []
    current = None
    for line in lines:
        stripped = line.strip()
        if HORIZONTAL_RULE.match(stripped):
            continue  # separators are not content
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


def gather_for_section(db, embedder, section, per_section, filters=None):
    queries = [section["heading"]]
    if section["spec"]:
        queries.append(section["spec"][:300])
        queries.extend(split_into_subquestions(section["spec"]))

    passages = []
    seen_texts = set()
    for query in queries:
        for result in build_index.retrieve(db, embedder, query, top_k=6, filters=filters):
            marker = result["text"][:120]
            if marker in seen_texts:
                continue
            seen_texts.add(marker)
            passages.append(result)
    passages.sort(key=lambda r: -r["score"])

    figures = []
    figure_query = section["spec"] or section["heading"]
    for result in build_index.retrieve(db, embedder, figure_query, top_k=3, kind="figure", filters=filters):
        figures.append(result)

    return passages[:per_section], figures


def build_pack(outline_text, per_section, style_text="", filters=None):
    db = build_index.open_db()
    paper_count = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    if paper_count == 0:
        sys.exit("The library index is empty — run  python build_index.py  first.")
    if filters:
        filter_sql, filter_params = build_index.paper_filter_sql(filters)
        eligible = db.execute(
            f"SELECT COUNT(*) FROM papers p WHERE 1=1{filter_sql}", filter_params
        ).fetchone()[0]
        if eligible == 0:
            sys.exit(build_index.explain_no_matches(db, filters))
        print(f"Library filters: {build_index.describe_filters(filters)} — "
              f"{eligible} of {paper_count} indexed paper(s) eligible.")
    embedder = build_index.get_embedder()

    sections = parse_outline(outline_text)
    if not sections:
        sys.exit("Couldn't find any sections in the outline file.")

    filter_note = (
        f" Passages were restricted to papers matching: {build_index.describe_filters(filters)}."
        if filters else ""
    )
    lines = [
        "# Research pack",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} from a library of {paper_count} indexed paper(s)."
        + filter_note,
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
        "**Deliverable:** produce the article as a Word (.docx) file. For every "
        "figure the outline calls for, do NOT embed an image — instead insert a "
        "clearly bordered placeholder box at that point containing: the figure "
        "number and title from the outline, and for each panel a recommended "
        "source drawn from the 'Candidate figures' lists below, written as "
        "'Panel A: adapt from [Entry M], Fig. X (p.Y)'. This lets the author "
        "assemble the composite figure manually from the cited papers. Do the "
        "same for tables the outline calls for: a placeholder noting which "
        "entries supply the compared values. Reused published figures need the "
        "publisher's permission before submission — state that in each box.",
        "",
    ]

    if style_text.strip():
        lines += [
            "## Writing-style rules (follow these strictly)",
            "",
            "These override any generic-review habits. Where a rule conflicts "
            "with sounding conventional, obey the rule.",
            "",
            style_text.strip(),
            "",
        ]

    lines += [
        "## Outline (verbatim)",
        "",
        "```",
        outline_text.strip(),
        "```",
        "",
    ]

    used_entries = {}
    total = len(sections)
    for i, section in enumerate(sections, 1):
        percent = (i - 1) * 100 // total
        print(f"[{i}/{total}] {percent}% retrieving for {section['heading'][:60]!r} ...")
        passages, figures = gather_for_section(db, embedder, section, per_section, filters)

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
                meta = ", ".join(bit for bit in (r["year"], r.get("journal", ""), r["category"]) if bit)
                lines.append(
                    f"**[Entry {r['entry']}, p.{r['page']}]** ({meta}) {r['title']}"
                )
                lines.append(f"> {r['text']}")
                lines.append("")
        if figures:
            lines.append("### Candidate figures from the library (for placeholder-box recommendations)")
            lines.append("")
            for r in figures:
                used_entries[r["entry"]] = r
                lines.append(f"- **[Entry {r['entry']}, p.{r['page']}]** {r['text']}")
            lines.append("")

    lines += ["## References catalog", ""]
    for entry in sorted(used_entries, key=lambda e: int(e) if str(e).isdigit() else 0):
        r = used_entries[entry]
        doi_row = db.execute(
            "SELECT doi FROM papers WHERE entry = ?", (str(entry),)
        ).fetchone()
        doi = doi_row[0] if doi_row else ""
        doi_part = f" https://doi.org/{doi}" if doi and doi.startswith("10.") else ""
        lines.append(f"- [Entry {entry}] {r['title']} ({r['year']}).{doi_part}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Build a research pack for Claude from your outline.")
    parser.add_argument("outline", help="path to your outline file (.txt, .md, or .docx)")
    parser.add_argument("--per-section", type=int, default=15,
                        help="max source passages per section (default 15)")
    parser.add_argument("--style", default="style_rules.txt",
                        help="text file of writing-style rules to embed "
                             "(default: style_rules.txt if it exists)")
    parser.add_argument("--out", help="output file or folder (default: "
                        "research_packs/research_pack_<timestamp>.md; a folder keeps the default name inside it)")
    build_index.add_filter_args(parser)
    args = parser.parse_args()
    filters = build_index.filters_from_args(args)

    if not os.path.exists(args.outline):
        sys.exit(f"Outline file not found: {args.outline}")
    try:
        outline_text = read_outline(args.outline)
    except Exception as e:
        sys.exit(f"Could not read the outline file {args.outline}: {type(e).__name__}: {e}")

    style_text = ""
    if os.path.exists(args.style):
        style_text = read_text_flexible(args.style)
        print(f"Using writing-style rules from {args.style}.")
    elif args.style != "style_rules.txt":
        sys.exit(f"Style file not found: {args.style}")

    load_config()  # fail early with a clear message if the config is broken

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = resolve_output_path(args.out, "research_packs", f"research_pack_{stamp}.md")
    pack = build_pack(outline_text, args.per_section, style_text, filters)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(pack)
    size_kb = os.path.getsize(out_path) / 1024

    print()
    print(f"Research pack written to {out_path} ({size_kb:.0f} KB).")
    print("Upload the pack to Claude and ask it to write the article as a .docx")
    print("(figures come out as labeled placeholder boxes citing which entry/")
    print("figure to place — no image folder to upload).")


if __name__ == "__main__":
    main()

"""Phase 2 of the literature assistant: build a searchable index of
every downloaded paper.

Reads the tracking sheet, extracts the full text of each downloaded
PDF, detects scientific sections, excludes references and administrative tails,
splits the remaining text into paragraph-aware passages, converts each passage into an embedding
(a numerical "meaning fingerprint") with a free local model, and stores
everything in library_index/library.sqlite3 — linked back to the
tracking sheet's Entry/DOI/Title/Year/Category so answers can cite
properly.

Figures are indexed too: each figure image is saved, paired with its
caption (the paper's own description of it), and given a visual
fingerprint (CLIP embedding) — so figures can be found by describing
them in words, or by showing a similar image. Saved figure filenames
follow the same <Entry>_<Category>_<Year>_<Title> pattern as the PDFs
in downloads/, so a file in library_index/figures/ is traceable back to
its paper at a glance instead of by a cryptic DOI string.

Usage:
    python build_index.py                        # index new/changed papers
    python build_index.py --rebuild              # start the index over
    python build_index.py --search "..."         # best-matching text passages
    python build_index.py --find-figure "..."    # figures whose captions match
    python build_index.py --match-figure img.png # figures that LOOK like yours

Every search mode accepts library filters, combinable freely:
    --journal "nature energy,joule"   only papers from matching journals
    --years 2020,2023-2025            only those publication years
    --since 2023 / --until 2024       an open-ended year range
    --category solar-cell,LED         only those categories
    --entries 12,45,100-110           only those Entry numbers

Everything runs locally on CPU; nothing is uploaded anywhere. Search
modes return the papers' own verbatim text, so what they show is
exactly what's in your library.
"""
import argparse
import os
import re
import sqlite3
import sys

import numpy as np

from doi_resolver import build_pdf_filename, ensure_utf8_console, load_config, load_tracking

INDEX_DIR = "library_index"
DB_PATH = os.path.join(INDEX_DIR, "library.sqlite3")
FIGURES_DIR = os.path.join(INDEX_DIR, "figures")

# Small, fast on CPU, and strong for retrieval; downloaded once
# (~130 MB) on first run, then cached locally.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

# Visual fingerprints for figure images (also a one-time download).
IMAGE_EMBED_MODEL = "Qdrant/clip-ViT-B-32-vision"

CHUNK_TARGET_CHARS = 1400
CHUNK_MIN_CHARS = 250
PARAGRAPH_OVERLAP = 1

# Retrieval settings. Semantic similarity remains the main signal, while
# exact scientific terms and evidence-bearing sections receive a modest boost.
CANDIDATE_MULTIPLIER = 12
MAX_RESULTS_PER_PAPER = 2
NEIGHBOUR_CHUNKS = 1

SECTION_WEIGHTS = {
    "results_discussion": 1.12,
    "results": 1.12,
    "discussion": 1.08,
    "methods": 1.05,
    "experimental": 1.05,
    "table_caption": 1.06,
    "figure_caption": 1.04,
    "conclusion": 1.02,
    "abstract": 1.00,
    "introduction": 0.94,
    "main_body": 0.98,
    "unknown": 0.96,
}

# These sections are administrative or bibliographic rather than scientific
# evidence. Once references begin, the remaining PDF text is not indexed.
STOP_SECTION_NAMES = {
    "references", "bibliography", "acknowledgment", "acknowledgments",
    "acknowledgement", "acknowledgements", "author contributions",
    "author contribution", "conflict of interest", "conflicts of interest",
    "competing interests", "funding", "publisher's note", "publisher note",
}

# Embedded images smaller than this are logos, ORCID icons, etc.
MIN_FIGURE_PIXELS = 120

INDEXED_STATUSES = ("downloaded", "downloaded_via_proxy")


def open_db():
    os.makedirs(INDEX_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS papers(
            doi TEXT PRIMARY KEY, entry TEXT, title TEXT, year TEXT,
            category TEXT, pdf_path TEXT, mtime REAL, chunk_count INTEGER
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doi TEXT, page INTEGER, text TEXT, embedding BLOB,
            kind TEXT DEFAULT 'text'
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS chunks_doi ON chunks(doi)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS figures(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doi TEXT, page INTEGER, caption TEXT,
            image_path TEXT, clip BLOB
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS figures_doi ON figures(doi)")
    # Add scientific-context columns to older index files in place. A full
    # --rebuild is still required once so existing passages receive values.
    columns = [r[1] for r in db.execute("PRAGMA table_info(chunks)")]
    migrations = {
        "kind": "TEXT DEFAULT 'text'",
        "section": "TEXT DEFAULT 'unknown'",
        "chunk_index": "INTEGER",
        "page_end": "INTEGER",
    }
    for column, sql_type in migrations.items():
        if column not in columns:
            db.execute(f"ALTER TABLE chunks ADD COLUMN {column} {sql_type}")
    # Journal name per paper (filled from the tracking sheet; no rebuild
    # needed — every run re-syncs it from the sheet).
    paper_columns = [r[1] for r in db.execute("PRAGMA table_info(papers)")]
    if "journal" not in paper_columns:
        db.execute("ALTER TABLE papers ADD COLUMN journal TEXT DEFAULT ''")
    db.execute("CREATE INDEX IF NOT EXISTS chunks_section ON chunks(section)")
    db.execute("CREATE INDEX IF NOT EXISTS chunks_order ON chunks(doi, chunk_index)")
    db.commit()
    return db


def get_embedder():
    try:
        from fastembed import TextEmbedding
    except ImportError:
        sys.exit(
            "The 'fastembed' package is not installed.\n"
            "Run:  pip install fastembed\n"
            "then run this script again."
        )
    return TextEmbedding(model_name=EMBED_MODEL)


def get_image_embedder(required=False):
    # Loaded lazily and allowed to fail softly during indexing: figure
    # images and captions are still saved and searchable by text even
    # if the visual-fingerprint model can't load; only --match-figure
    # strictly needs it.
    try:
        from fastembed import ImageEmbedding
        return ImageEmbedding(model_name=IMAGE_EMBED_MODEL)
    except Exception as e:
        if required:
            sys.exit(
                f"Couldn't load the image-matching model ({type(e).__name__}: {e}).\n"
                "Check that 'fastembed' is installed and the model download completed."
            )
        print(f"  (figure visual matching disabled: {type(e).__name__}: {e})")
        return None


def clean_page_text(text):
    text = text.replace("\r", "")
    # Re-join words hyphenated across line breaks ("perov-\nskite").
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _page_text_in_reading_order(page):
    """Extract text blocks in approximate human reading order.

    Sorting blocks by vertical and then horizontal position is more reliable
    for journal PDFs than accepting an arbitrary object order. The original
    line breaks are retained because section and paragraph detection need them.
    """
    blocks = page.get_text("blocks")
    useful = []
    for block in blocks:
        x0, y0, x1, y1, block_text = block[:5]
        cleaned = clean_page_text(block_text)
        if cleaned:
            useful.append((round(y0 / 8) * 8, x0, cleaned))
    useful.sort(key=lambda item: (item[0], item[1]))
    return clean_page_text("\n\n".join(item[2] for item in useful))


def extract_pages(pdf_path):
    try:
        import fitz
    except ImportError:
        sys.exit(
            "The 'pymupdf' package is not installed.\n"
            "Run:  pip install pymupdf\n"
            "then run this script again."
        )
    pages = []
    with fitz.open(pdf_path) as doc:
        for number, page in enumerate(doc, 1):
            text = _page_text_in_reading_order(page)
            if text:
                pages.append((number, text))
    return pages


def _normalise_heading(text):
    value = re.sub(r"^\s*(?:\d+(?:\.\d+)*|[IVXLC]+)[.)]?\s*", "", text.strip(), flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" .:–—-").lower()
    aliases = {
        "summary": "abstract",
        "background": "introduction",
        "materials and methods": "methods",
        "materials & methods": "methods",
        "methodology": "methods",
        "experimental section": "experimental",
        "experiments": "experimental",
        "results and discussion": "results_discussion",
        "results & discussion": "results_discussion",
        "discussion and results": "results_discussion",
        "conclusions": "conclusion",
        "concluding remarks": "conclusion",
    }
    return aliases.get(value, value.replace(" ", "_"))


def _looks_like_heading(line):
    stripped = re.sub(r"\s+", " ", line.strip())
    if not stripped or len(stripped) > 90:
        return None
    normalised = _normalise_heading(stripped)
    recognised = {
        "abstract", "introduction", "methods", "experimental", "results",
        "discussion", "results_discussion", "conclusion", "references",
        "bibliography", "acknowledgment", "acknowledgments", "acknowledgement",
        "acknowledgements", "author_contributions", "author_contribution",
        "conflict_of_interest", "conflicts_of_interest", "competing_interests",
        "funding", "publisher's_note", "publisher_note",
    }
    if normalised in recognised:
        return normalised
    # Accept short numbered/title-case headings, but not ordinary sentences.
    numbered = bool(re.match(r"^\s*\d+(?:\.\d+)*[.)]?\s+[A-Za-z]", line))
    title_like = stripped.isupper() or (
        len(stripped.split()) <= 7
        and not stripped.endswith((".", ",", ";", "?", "!"))
        and sum(word[:1].isupper() for word in stripped.split()) >= max(1, len(stripped.split()) - 1)
    )
    if numbered or title_like:
        return normalised
    return None


def section_paragraphs(pages):
    """Return (section, page, paragraph) while excluding non-evidence tails."""
    current_section = "unknown"
    output = []
    stop = False
    for page_number, page_text in pages:
        if stop:
            break
        for raw in re.split(r"\n+", page_text):
            paragraph = raw.strip()
            if not paragraph:
                continue
            heading = _looks_like_heading(paragraph)
            if heading:
                heading_words = heading.replace("_", " ")
                if heading_words in STOP_SECTION_NAMES or heading in {
                    "references", "bibliography", "acknowledgment", "acknowledgments",
                    "acknowledgement", "acknowledgements", "author_contributions",
                    "author_contribution", "conflict_of_interest", "conflicts_of_interest",
                    "competing_interests", "funding", "publisher's_note", "publisher_note",
                }:
                    stop = True
                    break
                current_section = heading
                continue
            # Repeated headers, footers, page numbers and very short fragments
            # add retrieval noise but little scientific meaning.
            if re.fullmatch(r"(?:page\s*)?\d+(?:\s*of\s*\d+)?", paragraph, re.I):
                continue
            if len(paragraph) < 35 and not re.search(r"\d", paragraph):
                continue
            output.append((current_section, page_number, paragraph))
    return output


def chunk_scientific_text(pages):
    """Build paragraph-respecting chunks that never cross section boundaries."""
    paragraphs = section_paragraphs(pages)
    chunks = []
    section_buffer = []
    current_section = None

    def flush_section(items, section):
        if not items:
            return
        current = []
        current_length = 0
        for page_number, paragraph in items:
            added = len(paragraph) + (1 if current else 0)
            if current and current_length + added > CHUNK_TARGET_CHARS:
                text = "\n".join(p for _, p in current)
                if len(text) >= CHUNK_MIN_CHARS:
                    chunks.append((section or "unknown", current[0][0], current[-1][0], text))
                current = current[-PARAGRAPH_OVERLAP:] if PARAGRAPH_OVERLAP else []
                current_length = sum(len(p) + 1 for _, p in current)
            current.append((page_number, paragraph))
            current_length += added
        if current:
            text = "\n".join(p for _, p in current)
            if len(text) >= CHUNK_MIN_CHARS or not chunks:
                chunks.append((section or "unknown", current[0][0], current[-1][0], text))

    for section, page_number, paragraph in paragraphs:
        if current_section is None:
            current_section = section
        if section != current_section:
            flush_section(section_buffer, current_section)
            section_buffer = []
            current_section = section
        section_buffer.append((page_number, paragraph))
    flush_section(section_buffer, current_section)
    return chunks


FIGURE_CAPTION_START = re.compile(r"^(?:Fig(?:ure)?|Scheme)\.?\s*\d+", re.IGNORECASE)
TABLE_CAPTION_START = re.compile(r"^Table\.?\s*\d+", re.IGNORECASE)


def extract_captions(page_text, start_pattern):
    # Captions start a line with "Figure N" / "Table N" / "Scheme N"
    # and run until a blank line or the next caption. In-text mentions
    # ("as shown in Figure 2a...") sit mid-line, so anchoring to line
    # starts keeps them out.
    captions = []
    lines = page_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if start_pattern.match(line):
            caption = line
            j = i + 1
            while (
                j < len(lines)
                and lines[j].strip()
                and not start_pattern.match(lines[j].strip())
                and len(caption) < 600
            ):
                caption += " " + lines[j].strip()
                j += 1
            if len(caption) >= 25:  # "Figure 1" alone isn't a caption
                captions.append(caption[:600])
            i = j
        else:
            i += 1
    return captions


def extract_figure_captions(page_text):
    return extract_captions(page_text, FIGURE_CAPTION_START)


def extract_figures(pdf_path):
    # Returns (page_number, caption, image_bytes, extension) per figure.
    # Raster images embedded in the PDF are pulled out directly; when a
    # page clearly has figures (captions found) but no raster images —
    # vector-drawn plots are common — the whole page is rendered once so
    # visual matching still has something to look at.
    import fitz
    results = []
    with fitz.open(pdf_path) as doc:
        for number, page in enumerate(doc, 1):
            captions = extract_figure_captions(clean_page_text(page.get_text("text")))
            images = []
            seen = set()
            for info in page.get_images(full=True):
                xref = info[0]
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    img = doc.extract_image(xref)
                except Exception:
                    continue
                if img.get("width", 0) < MIN_FIGURE_PIXELS or img.get("height", 0) < MIN_FIGURE_PIXELS:
                    continue
                images.append((img["image"], img["ext"]))
            if not images and captions:
                images = [(page.get_pixmap(dpi=100).tobytes("png"), "png")]
            for idx, (data, ext) in enumerate(images):
                caption = captions[idx] if idx < len(captions) else ""
                results.append((number, caption, data, ext))
    return results


def embed_passages(embedder, texts):
    vectors = np.array(list(embedder.embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def index_figures(db, embedder, image_embedder, key, pdf_path, row):
    # Old figure files first, so a re-index doesn't leave orphans.
    for (old_path,) in db.execute("SELECT image_path FROM figures WHERE doi = ?", (key,)):
        try:
            os.remove(old_path)
        except OSError:
            pass
    db.execute("DELETE FROM figures WHERE doi = ?", (key,))

    figures = extract_figures(pdf_path)
    if not figures:
        return 0

    os.makedirs(FIGURES_DIR, exist_ok=True)
    # Same <Entry>_<Category>_<Year>_<Title> pattern as the PDF itself —
    # not a DOI/path-derived name — so a file in library_index/figures/
    # is traceable back to its paper at a glance, exactly like the PDFs
    # in downloads/ already are. Entry alone is unique per paper, so this
    # can't collide even when Category/Year/Title are blank.
    base = os.path.splitext(build_pdf_filename(
        row.get("Entry", ""), row.get("Category", ""), row.get("Year", ""),
        row.get("Title", ""), row.get("DOI", ""),
    ))[0]
    saved = []
    for n, (page_number, caption, data, ext) in enumerate(figures, 1):
        image_path = os.path.join(FIGURES_DIR, f"{base}_p{page_number}_{n}.{ext}")
        with open(image_path, "wb") as f:
            f.write(data)
        saved.append((page_number, caption, image_path))

    clip_vectors = [None] * len(saved)
    if image_embedder is not None:
        try:
            vectors = np.array(list(image_embedder.embed([p for _, _, p in saved])), dtype=np.float32)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            clip_vectors = list(vectors / norms)
        except Exception:
            pass

    db.executemany(
        "INSERT INTO figures(doi, page, caption, image_path, clip) VALUES (?, ?, ?, ?, ?)",
        [
            (key, page_number, caption, image_path,
             vector.tobytes() if vector is not None else None)
            for (page_number, caption, image_path), vector in zip(saved, clip_vectors)
        ],
    )

    # Captions go into the text index too (marked as figures), so a
    # normal search finds "the figure that shows X" alongside prose.
    captioned = [(page_number, f"[Figure, p.{page_number}] {caption}")
                 for page_number, caption, _ in saved if caption]
    if captioned:
        vectors = embed_passages(embedder, [text for _, text in captioned])
        db.executemany(
            "INSERT INTO chunks(doi, page, page_end, text, embedding, kind, section) VALUES (?, ?, ?, ?, ?, 'figure', 'figure_caption')",
            [
                (key, page_number, page_number, text, vector.tobytes())
                for (page_number, text), vector in zip(captioned, vectors)
            ],
        )
    return len(saved)


def index_paper(db, embedder, image_embedder, row):
    doi = row.get("DOI", "")
    pdf_path = row.get("PDF_Path", "")
    key = doi or pdf_path

    pages = extract_pages(pdf_path)
    if not pages:
        return 0, 0, "no extractable text (scanned images only?)"

    db.execute("DELETE FROM chunks WHERE doi = ?", (key,))

    scientific_chunks = chunk_scientific_text(pages)
    if not scientific_chunks:
        return 0, 0, "nothing substantial to index"

    vectors = embed_passages(embedder, [item[3] for item in scientific_chunks])
    db.executemany(
        """INSERT INTO chunks
           (doi, page, page_end, text, embedding, kind, section, chunk_index)
           VALUES (?, ?, ?, ?, ?, 'text', ?, ?)""",
        [
            (key, page_start, page_end, chunk, vector.tobytes(), section, chunk_index)
            for chunk_index, ((section, page_start, page_end, chunk), vector)
            in enumerate(zip(scientific_chunks, vectors), 1)
        ],
    )

    figure_count = index_figures(db, embedder, image_embedder, key, pdf_path, row)

    table_chunks = []
    for number, page_text in pages:
        for caption in extract_captions(page_text, TABLE_CAPTION_START):
            table_chunks.append((number, f"[Table, p.{number}] {caption}"))
    if table_chunks:
        vectors = embed_passages(embedder, [item[1] for item in table_chunks])
        db.executemany(
            """INSERT INTO chunks
               (doi, page, page_end, text, embedding, kind, section)
               VALUES (?, ?, ?, ?, ?, 'table', 'table_caption')""",
            [
                (key, number, number, caption, vector.tobytes())
                for (number, caption), vector in zip(table_chunks, vectors)
            ],
        )

    db.execute(
        """INSERT OR REPLACE INTO papers
           (doi, entry, title, year, journal, category, pdf_path, mtime, chunk_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            key, row.get("Entry", ""), row.get("Title", ""), row.get("Year", ""),
            row.get("Journal", ""), row.get("Category", ""), pdf_path,
            os.path.getmtime(pdf_path), len(scientific_chunks),
        ),
    )
    db.commit()
    return len(scientific_chunks), figure_count, ""


def papers_to_index(db, tracking, rebuild=False):
    known = {}
    if not rebuild:
        known = {
            doi: mtime
            for doi, mtime in db.execute("SELECT doi, mtime FROM papers")
        }
    todo = []
    for row in tracking.values():
        if row.get("Status") not in INDEXED_STATUSES:
            continue
        pdf_path = row.get("PDF_Path", "")
        if not pdf_path or not os.path.exists(pdf_path):
            continue
        key = row.get("DOI", "") or pdf_path
        # Re-index when the file changed (e.g. replaced by a re-download
        # or renamed by Stage 1) — same file, same index entry, no work.
        if key in known and abs(known[key] - os.path.getmtime(pdf_path)) < 1:
            continue
        todo.append(row)
    return todo


def sync_paper_metadata(db, tracking):
    """Refresh entry/title/year/journal/category on already-indexed
    papers from the tracking sheet — so metadata added or corrected
    later (e.g. journal names backfilled by Stage 1) reaches the index
    on the next run without re-extracting a single PDF."""
    updated = 0
    for row in tracking.values():
        if row.get("Status") not in INDEXED_STATUSES:
            continue
        key = row.get("DOI", "") or row.get("PDF_Path", "")
        if not key:
            continue
        cursor = db.execute(
            """UPDATE papers SET entry = ?, title = ?, year = ?, journal = ?, category = ?
               WHERE doi = ? AND (entry != ? OR title != ? OR year != ?
                                  OR COALESCE(journal, '') != ? OR category != ?)""",
            (
                row.get("Entry", ""), row.get("Title", ""), row.get("Year", ""),
                row.get("Journal", ""), row.get("Category", ""), key,
                row.get("Entry", ""), row.get("Title", ""), row.get("Year", ""),
                row.get("Journal", ""), row.get("Category", ""),
            ),
        )
        updated += cursor.rowcount
    if updated:
        db.commit()
        print(f"Refreshed metadata (entry/title/year/journal/category) on {updated} indexed paper(s).")


def run_indexing(rebuild=False):
    config = load_config()
    tracking = load_tracking(config["paths"]["tracking_csv"])
    if not tracking:
        sys.exit("No tracking sheet found — run the download pipeline first.")

    db = open_db()
    if rebuild:
        db.execute("DELETE FROM chunks")
        db.execute("DELETE FROM papers")
        db.commit()

    sync_paper_metadata(db, tracking)
    todo = papers_to_index(db, tracking, rebuild)
    already = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    if not todo:
        print(f"Index is up to date ({already} paper(s) indexed). Nothing to do.")
        return

    total = len(todo)
    print(f"{total} paper(s) to index ({already} already done).")
    print("Loading the embedding models (first run downloads them once — that can take a few minutes)...")
    embedder = get_embedder()
    image_embedder = get_image_embedder()
    print("Models loaded — indexing begins now.")

    total_chunks = 0
    total_figures = 0
    for i, row in enumerate(todo, 1):
        title = row.get("Title", "")[:70]
        # "[i/total]" at the start of the line doubles as the machine-
        # readable progress marker the app turns into its % bar.
        percent = (i - 1) * 100 // total
        print(f"[{i}/{total}] {percent}% {title!r}", end=" ... ", flush=True)
        try:
            count, figure_count, problem = index_paper(db, embedder, image_embedder, row)
        except Exception as e:
            print(f"failed ({type(e).__name__}: {e})")
            continue
        if problem:
            print(problem)
        else:
            total_chunks += count
            total_figures += figure_count
            print(f"{count} passages, {figure_count} figures")

    indexed = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    print()
    print(f"100% — done. {indexed} paper(s) in the index, {total_chunks} new passages and {total_figures} figures added.")
    print("Try:  python build_index.py --search \"your question\"")
    print("      python build_index.py --find-figure \"what the figure shows\"")
    print("      python build_index.py --match-figure path\\to\\your_image.png")


# ---------------------------------------------------------------------------
# Library filters — one shared implementation for every retrieval front end
# (the search modes here, ask_library.py, export_for_claude.py): narrow
# retrieval to papers by journal, year(s), category, or entry numbers.
# ---------------------------------------------------------------------------

def parse_number_spec(spec):
    # "2024" -> {2024};  "2020,2023-2025" -> {2020, 2023, 2024, 2025}.
    # Used for both year specs and Entry-number specs.
    numbers = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            numbers.update(range(int(start), int(end) + 1))
        else:
            numbers.add(int(part))
    return numbers


def add_filter_args(parser):
    group = parser.add_argument_group("library filters (all optional, combine freely)")
    group.add_argument("--journal", help="only papers whose journal name contains this text; "
                       "comma-separate alternatives (e.g. \"nature energy,joule\")")
    group.add_argument("--category", help="one or more categories, comma-separated (e.g. solar-cell,LED)")
    group.add_argument("--years", help="specific year(s): 2024, or 2020,2023-2025 (mix of years and ranges)")
    group.add_argument("--since", type=int, help="only papers from this year onward")
    group.add_argument("--until", type=int, help="only papers up to this year")
    group.add_argument("--entries", help="only these Entry numbers: 12,45,100-110")


def filters_from_args(args):
    filters = {}
    if getattr(args, "journal", None):
        filters["journals"] = [j.strip() for j in args.journal.split(",") if j.strip()]
    if getattr(args, "category", None):
        filters["categories"] = {c.strip().lower() for c in args.category.split(",") if c.strip()}
    if getattr(args, "years", None):
        filters["years"] = parse_number_spec(args.years)
    if getattr(args, "since", None):
        filters["since"] = args.since
    if getattr(args, "until", None):
        filters["until"] = args.until
    if getattr(args, "entries", None):
        filters["entries"] = parse_number_spec(args.entries)
    return filters or None


def _compact_ranges(numbers):
    parts = []
    start = prev = None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = n
    if start is not None:
        parts.append(f"{start}-{prev}" if prev > start else str(start))
    return ", ".join(parts)


def describe_filters(filters):
    if not filters:
        return ""
    bits = []
    if filters.get("journals"):
        bits.append("journal contains " + " or ".join(f"'{j}'" for j in filters["journals"]))
    if filters.get("categories"):
        bits.append("category " + "/".join(sorted(filters["categories"])))
    if filters.get("years"):
        bits.append("year(s) " + _compact_ranges(sorted(filters["years"])))
    if filters.get("since"):
        bits.append(f"from {filters['since']}")
    if filters.get("until"):
        bits.append(f"up to {filters['until']}")
    if filters.get("entries"):
        bits.append("entries " + _compact_ranges(sorted(filters["entries"])))
    return "; ".join(bits)


def paper_filter_sql(filters, alias="p"):
    """Translate a filters dict into an (SQL fragment, params) pair to
    append to a query that joins the papers table as `alias`. Year and
    entry comparisons CAST the stored text, so papers without a usable
    year are excluded by year filters — the same behavior
    export_catalog.py's filters have always had."""
    if not filters:
        return "", []
    clauses, params = [], []
    journals = filters.get("journals")
    if journals:
        clauses.append("(" + " OR ".join(
            f"LOWER(COALESCE({alias}.journal, '')) LIKE ?" for _ in journals) + ")")
        params.extend(f"%{j.lower()}%" for j in journals)
    categories = filters.get("categories")
    if categories:
        placeholders = ", ".join("?" for _ in categories)
        clauses.append(f"LOWER({alias}.category) IN ({placeholders})")
        params.extend(sorted(categories))
    years = filters.get("years")
    if years:
        placeholders = ", ".join("?" for _ in years)
        clauses.append(f"CAST({alias}.year AS INTEGER) IN ({placeholders})")
        params.extend(sorted(years))
    if filters.get("since"):
        clauses.append(f"CAST({alias}.year AS INTEGER) >= ?")
        params.append(filters["since"])
    if filters.get("until"):
        # BETWEEN 1 AND x, not just <= x: an unknown year CASTs to 0 and
        # must not slip through an upper-bound-only filter.
        clauses.append(f"CAST({alias}.year AS INTEGER) BETWEEN 1 AND ?")
        params.append(filters["until"])
    entries = filters.get("entries")
    if entries:
        placeholders = ", ".join("?" for _ in entries)
        clauses.append(f"CAST({alias}.entry AS INTEGER) IN ({placeholders})")
        params.extend(sorted(entries))
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def explain_no_matches(db, filters):
    """A helpful message for zero results under filters — including the
    one trap worth calling out: journal filtering before any journal
    names have been fetched into the library."""
    message = f"No indexed papers match the filters ({describe_filters(filters)})."
    if filters and filters.get("journals"):
        with_journal = db.execute(
            "SELECT COUNT(*) FROM papers WHERE COALESCE(journal, '') != ''"
        ).fetchone()[0]
        if with_journal == 0:
            message += (
                "\nNote: no paper in the index has a journal name yet. Run "
                "python doi_resolver.py once (it now fetches journal names), "
                "then python build_index.py to refresh the index metadata."
            )
    return message


def _query_terms(query):
    # Keep formulae, abbreviations and numbers that semantic search can blur.
    terms = re.findall(r"[A-Za-z][A-Za-z0-9+\-_.]{2,}|\d+(?:\.\d+)?%?", query.lower())
    stop = {"the", "and", "for", "with", "from", "that", "this", "what", "which", "was", "were", "are"}
    return [term for term in terms if term not in stop]


def _exact_term_score(query, text, title):
    terms = _query_terms(query)
    if not terms:
        return 0.0
    haystack = f"{title} {text}".lower()
    matched = sum(1 for term in terms if term in haystack)
    return matched / len(terms)


def _expand_with_neighbours(db, row):
    text, page, page_end, embedding, kind, section, chunk_index, entry, title, year, category, doi, journal = row
    if kind != "text" or chunk_index is None or NEIGHBOUR_CHUNKS <= 0:
        return text
    neighbours = db.execute(
        """SELECT chunk_index, text FROM chunks
           WHERE doi = ? AND kind = 'text' AND section = ?
             AND chunk_index BETWEEN ? AND ?
           ORDER BY chunk_index""",
        (doi, section, chunk_index - NEIGHBOUR_CHUNKS, chunk_index + NEIGHBOUR_CHUNKS),
    ).fetchall()
    if len(neighbours) <= 1:
        return text
    blocks = []
    for neighbour_index, neighbour_text in neighbours:
        marker = "MATCH" if neighbour_index == chunk_index else "CONTEXT"
        blocks.append(f"[{marker}] {neighbour_text}")
    return "\n\n".join(blocks)


def retrieve(db, embedder, query, top_k=5, kind=None, filters=None):
    """Section-aware hybrid retrieval, compatible with existing callers.

    Existing dictionary keys are preserved. Additional keys expose section,
    page range, chunk order, semantic score, lexical score and journal.
    `filters` (see paper_filter_sql) narrows retrieval to matching papers.
    """
    if kind == "figure":
        kind_clause = "c.kind = 'figure'"
    else:
        kind_clause = "c.kind IN ('text', 'figure', 'table')"
    filter_clause, filter_params = paper_filter_sql(filters)
    rows = db.execute(
        f"""SELECT c.text, c.page, COALESCE(c.page_end, c.page), c.embedding,
                   c.kind, COALESCE(c.section, 'unknown'), c.chunk_index,
                   p.entry, p.title, p.year, p.category, p.doi,
                   COALESCE(p.journal, '')
            FROM chunks c JOIN papers p ON p.doi = c.doi
            WHERE {kind_clause} AND c.embedding IS NOT NULL{filter_clause}""",
        filter_params,
    ).fetchall()
    if not rows:
        return []

    # With filters the paper pool can be tiny (a single journal, a
    # handful of entries) — the usual 2-per-paper diversity cap would
    # then starve top_k. Let the cap grow so top_k stays reachable.
    max_per_paper = MAX_RESULTS_PER_PAPER
    if filters:
        paper_pool = len({row[11] for row in rows})
        max_per_paper = max(MAX_RESULTS_PER_PAPER, -(-top_k // max(paper_pool, 1)))

    query_vector = np.array(list(embedder.query_embed(query)), dtype=np.float32)[0]
    norm = np.linalg.norm(query_vector)
    if norm:
        query_vector /= norm

    matrix = np.frombuffer(b"".join(row[3] for row in rows), dtype=np.float32).reshape(len(rows), -1)
    semantic_scores = matrix @ query_vector

    scored = []
    for index, row in enumerate(rows):
        semantic = float(semantic_scores[index])
        lexical = _exact_term_score(query, row[0], row[8])
        section_weight = SECTION_WEIGHTS.get(row[5], 0.98)
        # Semantic meaning dominates; exact scientific terms improve precision.
        combined = (0.82 * semantic + 0.18 * lexical) * section_weight
        scored.append((combined, semantic, lexical, index))
    scored.sort(reverse=True)

    candidate_limit = max(top_k * CANDIDATE_MULTIPLIER, top_k)
    selected = []
    per_paper = {}
    for combined, semantic, lexical, index in scored[:candidate_limit]:
        row = rows[index]
        doi = row[11]
        if per_paper.get(doi, 0) >= max_per_paper:
            continue
        per_paper[doi] = per_paper.get(doi, 0) + 1
        selected.append((combined, semantic, lexical, row))
        if len(selected) >= top_k:
            break

    results = []
    for combined, semantic, lexical, row in selected:
        text, page, page_end, _embedding, kind_value, section, chunk_index, entry, title, year, category, doi, journal = row
        results.append({
            "text": _expand_with_neighbours(db, row),
            "matched_text": text,
            "page": page,
            "page_end": page_end,
            "entry": entry,
            "title": title,
            "year": year,
            "journal": journal,
            "category": category,
            "doi": doi,
            "kind": kind_value,
            "section": section,
            "chunk_index": chunk_index,
            "score": combined,
            "semantic_score": semantic,
            "lexical_score": lexical,
        })
    return results


def run_search(query, top_k=5, kind=None, filters=None):
    db = open_db()
    embedder = get_embedder()
    results = retrieve(db, embedder, query, top_k, kind, filters)
    if not results:
        if filters:
            sys.exit(explain_no_matches(db, filters))
        sys.exit(
            "No matching index entries — run  python build_index.py  first."
            if kind is None else
            "No figure captions in the index yet — run  python build_index.py  first."
        )

    header = f"Top {len(results)} passages for: {query!r}"
    if filters:
        header += f"  [filters: {describe_filters(filters)}]"
    print(header)
    for rank, r in enumerate(results, 1):
        print()
        pages = f"p.{r['page']}" if r['page_end'] == r['page'] else f"pp.{r['page']}-{r['page_end']}"
        section = r.get("section", "unknown").replace("_", " ").title()
        meta = " | ".join(bit for bit in (r['year'], r.get('journal', ''), r['category'], section) if bit)
        print(f"--- {rank}. [Entry {r['entry']} | {meta}] {r['title'][:80]}  ({pages}, score {r['score']:.2f})")
        print(r["text"])


def run_match_figure(image_path, top_k=5, filters=None):
    # "Here is a figure — what in my library looks like this?" The
    # given image and every stored figure share the same visual-
    # fingerprint space, so nearest neighbours are visually similar
    # figures; each brings its caption and source paper along.
    if not os.path.exists(image_path):
        sys.exit(f"Image not found: {image_path}")
    db = open_db()
    filter_clause, filter_params = paper_filter_sql(filters)
    rows = db.execute(
        f"""SELECT f.caption, f.page, f.image_path, f.clip, p.entry, p.title, p.year
            FROM figures f JOIN papers p ON p.doi = f.doi
            WHERE f.clip IS NOT NULL{filter_clause}""",
        filter_params,
    ).fetchall()
    if not rows:
        if filters:
            sys.exit(explain_no_matches(db, filters))
        sys.exit(
            "No figures with visual fingerprints in the index yet — run "
            "python build_index.py (with the image model able to load) first."
        )

    embedder = get_image_embedder(required=True)
    query_vector = np.array(list(embedder.embed([image_path])), dtype=np.float32)[0]
    norm = np.linalg.norm(query_vector)
    if norm:
        query_vector /= norm

    matrix = np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    scores = matrix @ query_vector
    best = np.argsort(scores)[::-1][:top_k]

    print(f"Figures most similar to {image_path}:")
    for rank, idx in enumerate(best, 1):
        caption, page, stored_path, _, entry, title, year = rows[idx]
        print()
        print(f"--- {rank}. [Entry {entry} | {year}] {title[:80]}  (p.{page}, similarity {scores[idx]:.2f})")
        print(f"    image: {stored_path}")
        print(f"    caption: {caption or '(no caption found for this figure)'}")


def main():
    # --search/--find-figure/--match-figure never call load_config() (they
    # don't need the pipeline config at all), so they'd otherwise miss the
    # UTF-8 console fix load_config() normally provides — and PDF text
    # routinely contains ligature characters like "ﬁ" that crash a plain
    # print() on Windows without it.
    ensure_utf8_console()
    parser = argparse.ArgumentParser(description="Build/search the local paper index.")
    parser.add_argument("--rebuild", action="store_true", help="re-index everything with scientific section labels")
    # nargs="+" lets the question be typed with or without quotes —
    # every word after the flag belongs to the query.
    parser.add_argument("--search", metavar="QUERY", nargs="+", help="show the best-matching passages for a question")
    parser.add_argument("--find-figure", metavar="QUERY", nargs="+", help="find figures by describing what they show")
    parser.add_argument("--match-figure", metavar="IMAGE", help="find stored figures visually similar to an image file")
    parser.add_argument("--top", type=int, default=5, help="how many results the search modes show")
    add_filter_args(parser)
    args = parser.parse_args()
    filters = filters_from_args(args)

    if args.search:
        run_search(" ".join(args.search), args.top, filters=filters)
    elif args.find_figure:
        run_search(" ".join(args.find_figure), args.top, kind="figure", filters=filters)
    elif args.match_figure:
        run_match_figure(args.match_figure, args.top, filters=filters)
    else:
        if filters:
            parser.error("--journal/--category/--years/--since/--until/--entries only "
                         "apply to the search modes (--search, --find-figure, --match-figure)")
        run_indexing(args.rebuild)


if __name__ == "__main__":
    main()

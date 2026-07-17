"""Phase 2 of the literature assistant: build a searchable index of
every downloaded paper.

Reads the tracking sheet, extracts the full text of each downloaded
PDF, splits it into passages, converts each passage into an embedding
(a numerical "meaning fingerprint") with a free local model, and stores
everything in library_index/library.sqlite3 — linked back to the
tracking sheet's Entry/DOI/Title/Year/Category so answers can cite
properly.

Figures are indexed too: each figure image is saved, paired with its
caption (the paper's own description of it), and given a visual
fingerprint (CLIP embedding) — so figures can be found by describing
them in words, or by showing a similar image.

Usage:
    python build_index.py                        # index new/changed papers
    python build_index.py --rebuild              # start the index over
    python build_index.py --search "..."         # best-matching text passages
    python build_index.py --find-figure "..."    # figures whose captions match
    python build_index.py --match-figure img.png # figures that LOOK like yours

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

from doi_resolver import load_config, load_tracking, sanitize_filename

INDEX_DIR = "library_index"
DB_PATH = os.path.join(INDEX_DIR, "library.sqlite3")
FIGURES_DIR = os.path.join(INDEX_DIR, "figures")

# Small, fast on CPU, and strong for retrieval; downloaded once
# (~130 MB) on first run, then cached locally.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

# Visual fingerprints for figure images (also a one-time download).
IMAGE_EMBED_MODEL = "Qdrant/clip-ViT-B-32-vision"

CHUNK_TARGET_CHARS = 1200
CHUNK_OVERLAP_CHARS = 200

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
    # Older index files predate the 'kind' column — add it in place so
    # nobody has to rebuild just because the schema grew.
    columns = [r[1] for r in db.execute("PRAGMA table_info(chunks)")]
    if "kind" not in columns:
        db.execute("ALTER TABLE chunks ADD COLUMN kind TEXT DEFAULT 'text'")
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


def extract_pages(pdf_path):
    # PyMuPDF reads scientific-journal layouts (two columns, figures)
    # far more faithfully than lighter libraries.
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
            text = clean_page_text(page.get_text("text"))
            if text:
                pages.append((number, text))
    return pages


def chunk_page_text(text):
    # Passages of ~CHUNK_TARGET_CHARS, split at paragraph boundaries,
    # with a small tail of the previous chunk carried over so a
    # sentence cut by the split is still findable in one piece.
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 1 > CHUNK_TARGET_CHARS:
            chunks.append(current)
            tail = current[-CHUNK_OVERLAP_CHARS:]
            # Start the carried-over tail at a word boundary.
            current = tail.split(" ", 1)[-1] if " " in tail else tail
        current = f"{current} {para}".strip() if current else para
    # Keep a trailing fragment only if it's substantial (or the page
    # produced nothing else) — lone caption scraps aren't worth a row.
    if current and (len(current) >= 200 or not chunks):
        chunks.append(current)
    return chunks


FIGURE_CAPTION_START = re.compile(r"^(?:Fig(?:ure)?|Scheme)\.?\s*\d+", re.IGNORECASE)


def extract_figure_captions(page_text):
    # Captions start a line with "Figure N" / "Fig. N" / "Scheme N" and
    # run until a blank line or the next caption. In-text mentions
    # ("as shown in Figure 2a...") sit mid-line, so anchoring to line
    # starts keeps them out.
    captions = []
    lines = page_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if FIGURE_CAPTION_START.match(line):
            caption = line
            j = i + 1
            while (
                j < len(lines)
                and lines[j].strip()
                and not FIGURE_CAPTION_START.match(lines[j].strip())
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


def index_figures(db, embedder, image_embedder, key, pdf_path):
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
    base = sanitize_filename(key.replace("/", "_"))
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
            "INSERT INTO chunks(doi, page, text, embedding, kind) VALUES (?, ?, ?, ?, 'figure')",
            [
                (key, page_number, text, vector.tobytes())
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

    page_chunks = []
    for number, text in pages:
        for chunk in chunk_page_text(text):
            page_chunks.append((number, chunk))
    if not page_chunks:
        return 0, 0, "nothing substantial to index"

    vectors = embed_passages(embedder, [c for _, c in page_chunks])
    db.executemany(
        "INSERT INTO chunks(doi, page, text, embedding) VALUES (?, ?, ?, ?)",
        [
            (key, number, chunk, vector.tobytes())
            for (number, chunk), vector in zip(page_chunks, vectors)
        ],
    )

    figure_count = index_figures(db, embedder, image_embedder, key, pdf_path)

    db.execute(
        """INSERT OR REPLACE INTO papers
           (doi, entry, title, year, category, pdf_path, mtime, chunk_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            key, row.get("Entry", ""), row.get("Title", ""), row.get("Year", ""),
            row.get("Category", ""), pdf_path, os.path.getmtime(pdf_path), len(page_chunks),
        ),
    )
    db.commit()
    return len(page_chunks), figure_count, ""


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

    todo = papers_to_index(db, tracking, rebuild)
    already = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    if not todo:
        print(f"Index is up to date ({already} paper(s) indexed). Nothing to do.")
        return

    print(f"{len(todo)} paper(s) to index ({already} already done).")
    print("Loading the embedding models (first run downloads them once)...")
    embedder = get_embedder()
    image_embedder = get_image_embedder()

    total_chunks = 0
    total_figures = 0
    for i, row in enumerate(todo, 1):
        title = row.get("Title", "")[:70]
        print(f"[{i}/{len(todo)}] {title!r}", end=" ... ", flush=True)
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
    print(f"Done. {indexed} paper(s) in the index, {total_chunks} new passages and {total_figures} figures added.")
    print("Try:  python build_index.py --search \"your question\"")
    print("      python build_index.py --find-figure \"what the figure shows\"")
    print("      python build_index.py --match-figure path\\to\\your_image.png")


def retrieve(db, embedder, query, top_k=5, kind=None):
    # The reusable heart of every search mode (and of ask_library.py):
    # returns the top passages as dicts instead of printing them.
    kind_filter = "WHERE c.kind = 'figure'" if kind == "figure" else ""
    rows = db.execute(
        f"""SELECT c.text, c.page, c.embedding, p.entry, p.title, p.year, p.category, p.doi
            FROM chunks c JOIN papers p ON p.doi = c.doi {kind_filter}"""
    ).fetchall()
    if not rows:
        return []

    query_vector = np.array(list(embedder.query_embed(query)), dtype=np.float32)[0]
    norm = np.linalg.norm(query_vector)
    if norm:
        query_vector /= norm

    matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    scores = matrix @ query_vector
    best = np.argsort(scores)[::-1][:top_k]

    return [
        {
            "text": rows[i][0], "page": rows[i][1], "entry": rows[i][3],
            "title": rows[i][4], "year": rows[i][5], "category": rows[i][6],
            "doi": rows[i][7], "score": float(scores[i]),
        }
        for i in best
    ]


def run_search(query, top_k=5, kind=None):
    db = open_db()
    embedder = get_embedder()
    results = retrieve(db, embedder, query, top_k, kind)
    if not results:
        sys.exit(
            "No matching index entries — run  python build_index.py  first."
            if kind is None else
            "No figure captions in the index yet — run  python build_index.py  first."
        )

    print(f"Top {len(results)} passages for: {query!r}")
    for rank, r in enumerate(results, 1):
        print()
        print(f"--- {rank}. [Entry {r['entry']} | {r['year']} | {r['category']}] {r['title'][:80]}  (p.{r['page']}, score {r['score']:.2f})")
        print(r["text"])


def run_match_figure(image_path, top_k=5):
    # "Here is a figure — what in my library looks like this?" The
    # given image and every stored figure share the same visual-
    # fingerprint space, so nearest neighbours are visually similar
    # figures; each brings its caption and source paper along.
    if not os.path.exists(image_path):
        sys.exit(f"Image not found: {image_path}")
    db = open_db()
    rows = db.execute(
        """SELECT f.caption, f.page, f.image_path, f.clip, p.entry, p.title, p.year
           FROM figures f JOIN papers p ON p.doi = f.doi
           WHERE f.clip IS NOT NULL"""
    ).fetchall()
    if not rows:
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
    parser = argparse.ArgumentParser(description="Build/search the local paper index.")
    parser.add_argument("--rebuild", action="store_true", help="re-index everything from scratch")
    # nargs="+" lets the question be typed with or without quotes —
    # every word after the flag belongs to the query.
    parser.add_argument("--search", metavar="QUERY", nargs="+", help="show the best-matching passages for a question")
    parser.add_argument("--find-figure", metavar="QUERY", nargs="+", help="find figures by describing what they show")
    parser.add_argument("--match-figure", metavar="IMAGE", help="find stored figures visually similar to an image file")
    parser.add_argument("--top", type=int, default=5, help="how many results the search modes show")
    args = parser.parse_args()

    if args.search:
        run_search(" ".join(args.search), args.top)
    elif args.find_figure:
        run_search(" ".join(args.find_figure), args.top, kind="figure")
    elif args.match_figure:
        run_match_figure(args.match_figure, args.top)
    else:
        run_indexing(args.rebuild)


if __name__ == "__main__":
    main()

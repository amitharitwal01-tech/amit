"""Phase 2 of the literature assistant: build a searchable index of
every downloaded paper.

Reads the tracking sheet, extracts the full text of each downloaded
PDF, splits it into passages, converts each passage into an embedding
(a numerical "meaning fingerprint") with a free local model, and stores
everything in library_index/library.sqlite3 — linked back to the
tracking sheet's Entry/DOI/Title/Year/Category so answers can cite
properly.

Usage:
    python build_index.py                 # index new/changed papers
    python build_index.py --rebuild       # start the index over
    python build_index.py --search "..."  # test: show best-matching passages

Everything runs locally on CPU; nothing is uploaded anywhere. The
--search mode returns the papers' own verbatim text, so what it shows
is exactly what's in your library.
"""
import argparse
import os
import re
import sqlite3
import sys

import numpy as np

from doi_resolver import load_config, load_tracking

INDEX_DIR = "library_index"
DB_PATH = os.path.join(INDEX_DIR, "library.sqlite3")

# Small, fast on CPU, and strong for retrieval; downloaded once
# (~130 MB) on first run, then cached locally.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

CHUNK_TARGET_CHARS = 1200
CHUNK_OVERLAP_CHARS = 200

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
            doi TEXT, page INTEGER, text TEXT, embedding BLOB
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS chunks_doi ON chunks(doi)")
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


def embed_passages(embedder, texts):
    vectors = np.array(list(embedder.embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def index_paper(db, embedder, row):
    doi = row.get("DOI", "")
    pdf_path = row.get("PDF_Path", "")
    key = doi or pdf_path

    pages = extract_pages(pdf_path)
    if not pages:
        return 0, "no extractable text (scanned images only?)"

    db.execute("DELETE FROM chunks WHERE doi = ?", (key,))

    page_chunks = []
    for number, text in pages:
        for chunk in chunk_page_text(text):
            page_chunks.append((number, chunk))
    if not page_chunks:
        return 0, "nothing substantial to index"

    vectors = embed_passages(embedder, [c for _, c in page_chunks])
    db.executemany(
        "INSERT INTO chunks(doi, page, text, embedding) VALUES (?, ?, ?, ?)",
        [
            (key, number, chunk, vector.tobytes())
            for (number, chunk), vector in zip(page_chunks, vectors)
        ],
    )
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
    return len(page_chunks), ""


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
    print("Loading the embedding model (first run downloads it once)...")
    embedder = get_embedder()

    total_chunks = 0
    for i, row in enumerate(todo, 1):
        title = row.get("Title", "")[:70]
        print(f"[{i}/{len(todo)}] {title!r}", end=" ... ", flush=True)
        try:
            count, problem = index_paper(db, embedder, row)
        except Exception as e:
            print(f"failed ({type(e).__name__}: {e})")
            continue
        if problem:
            print(problem)
        else:
            total_chunks += count
            print(f"{count} passages")

    indexed = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    print()
    print(f"Done. {indexed} paper(s) in the index, {total_chunks} new passages added.")
    print(f"Try it:  python build_index.py --search \"your question here\"")


def run_search(query, top_k=5):
    db = open_db()
    rows = db.execute(
        """SELECT c.text, c.page, c.embedding, p.entry, p.title, p.year, p.category
           FROM chunks c JOIN papers p ON p.doi = c.doi"""
    ).fetchall()
    if not rows:
        sys.exit("The index is empty — run  python build_index.py  first.")

    embedder = get_embedder()
    query_vector = np.array(list(embedder.query_embed(query)), dtype=np.float32)[0]
    norm = np.linalg.norm(query_vector)
    if norm:
        query_vector /= norm

    matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    scores = matrix @ query_vector
    best = np.argsort(scores)[::-1][:top_k]

    print(f"Top {len(best)} passages for: {query!r}")
    for rank, idx in enumerate(best, 1):
        text, page, _, entry, title, year, category = rows[idx]
        print()
        print(f"--- {rank}. [Entry {entry} | {year} | {category}] {title[:80]}  (p.{page}, score {scores[idx]:.2f})")
        print(text)


def main():
    parser = argparse.ArgumentParser(description="Build/search the local paper index.")
    parser.add_argument("--rebuild", action="store_true", help="re-index everything from scratch")
    parser.add_argument("--search", metavar="QUERY", help="show the best-matching passages for a question")
    parser.add_argument("--top", type=int, default=5, help="how many passages --search shows")
    args = parser.parse_args()

    if args.search:
        run_search(args.search, args.top)
    else:
        run_indexing(args.rebuild)


if __name__ == "__main__":
    main()

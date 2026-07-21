#!/usr/bin/env python3
"""Ask questions of the locally indexed paper library.

This is the review-oriented, multi-query retrieval front end. It searches the
SQLite passage index built by build_index.py (never the tracking sheet's
key-points column), fuses many complementary searches, and reports honestly on
how much of the library it actually looked at.

A citation only proves a claim is grounded in a retrieved passage. It does not
prove the retrieval was comprehensive. This script is therefore careful to keep
four very different situations apart, and to say which one it is in:

  * not retrieved            - in the index, but no query surfaced it;
  * not sufficiently         - retrieved, but the passages do not settle it;
    supported
  * not indexed              - the PDF was downloaded but never made it into
                               the index (extraction or indexing failure);
  * genuinely absent         - not present anywhere in the complete library.

Key features
------------
* focused, review, and exhaustive search modes
* scientific query decomposition and terminology expansion, with the
  systematic review dimensions *guaranteed* a slot (never truncated away)
* the whole embedding matrix is loaded once and reused across every query
* reciprocal-rank fusion across queries, keyed on stable chunk identity
* paper-aware diversification and passage de-duplication
* a retrieval-coverage report, CSV audit, and JSON search manifest that expose
  the four states above, including how much of the library went unprobed
* question-pack export, verbatim mode, Ollama, Gemini, and llama-cpp support

Examples
--------
python ask_library.py --mode focused "What does CsBr do at the interface?"
python ask_library.py --mode review --pack "What has been done in evaporated PSCs?"
python ask_library.py --mode exhaustive --no-ai "GIWAXS in perovskite films"
python ask_library.py --mode review --queries-file queries.txt --pack "question"
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import requests

import build_index
from doi_resolver import load_config, load_tracking, resolve_output_path


ANSWERS_DIR = "answers"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_GGUF_REPO = "bartowski/Qwen2.5-7B-Instruct-GGUF"
DEFAULT_GGUF_FILE = "*Q4_K_M.gguf"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_KEY_PATH = "gemini_api_key.txt"

# Hybrid-score weights. These MUST mirror build_index.retrieve() so that the
# cached in-process search below ranks passages identically to
# "python build_index.py --search". Semantic meaning dominates; exact
# scientific terms sharpen precision.
SEMANTIC_WEIGHT = 0.82
LEXICAL_WEIGHT = 0.18

# How much retrieved text to hand a *local* answering model in one prompt.
# Small local models (llama-cpp defaults to 8k-16k tokens, Ollama likewise)
# overflow long ago before review/exhaustive evidence sets fit. The full set
# is always preserved in the CSV audit, JSON manifest, and --pack export; only
# the text sent to the model is trimmed, and the trim is reported.
MODEL_EVIDENCE_CHAR_BUDGET = 24000

MODE_SETTINGS = {
    "focused": {
        "top_per_query": 12,
        "final_passages": 12,
        "max_passages_per_paper": 2,
        "min_papers": 4,
        "max_queries": 6,
    },
    "review": {
        "top_per_query": 50,
        "final_passages": 36,
        "max_passages_per_paper": 2,
        "min_papers": 15,
        # Large enough that the 11 systematic dimensions + negative-evidence
        # probe + terminology expansions all fit alongside a few decomposition
        # sub-questions. Extra queries are cheap now that the matrix is cached.
        "max_queries": 22,
    },
    "exhaustive": {
        "top_per_query": 150,
        "final_passages": 100,
        "max_passages_per_paper": 3,
        "min_papers": 30,
        "max_queries": 32,
    },
}

DECOMPOSE_SYSTEM = (
    "Break the research question into independent literature-search dimensions. "
    "Include methods, materials, performance, mechanisms, stability, scale-up, "
    "limitations, contradictory evidence, and research gaps when relevant. "
    "Output only standalone search questions, one per line, with no numbering."
)

ANSWER_SYSTEM = (
    "You are a scientific literature assistant. Answer strictly and only from "
    "the numbered source passages provided. Never use outside knowledge. Write "
    "precise scientific prose. Every factual claim must carry a citation such as "
    "[Entry 3, p.5], copied from the supporting passage label. Distinguish an "
    "author-reported limitation from an evidence-derived gap. Do not infer that a "
    "topic is absent from the complete library merely because it was not retrieved. "
    "If evidence is insufficient, state: 'The retrieved evidence does not provide "
    "enough information to answer this part. This does not establish that the "
    "information is absent from the complete indexed library.' Note conflicting "
    "evidence explicitly."
)

TERM_GROUPS = {
    "evaporation": [
        "thermal evaporation", "co-evaporation", "vacuum deposition",
        "vapour deposition", "vapor deposition", "co-sublimation",
        "physical vapor deposition", "physical vapour deposition",
        "hybrid vapor deposition", "hybrid vapour deposition",
        "sequential evaporation", "vapor conversion", "vapour conversion",
    ],
    "giwaxs": [
        "GIWAXS", "GIWAX", "GIXRD", "GIXD",
        "grazing-incidence wide-angle X-ray scattering",
        "grazing incidence X-ray diffraction", "2D scattering pattern",
        "azimuthal distribution", "crystallographic texture",
        "preferred orientation", "face-on", "edge-on", "qxy", "qz",
    ],
    "stability": [
        "operational stability", "maximum power point", "MPPT",
        "light soaking", "thermal ageing", "thermal aging", "lifetime",
        "durability", "T80", "degradation",
    ],
    "scale": [
        "large area", "module", "scale-up", "scalable", "uniformity",
        "reproducibility", "yield", "throughput", "manufacturing",
    ],
    "interface": [
        "interface", "buried interface", "interfacial", "passivation",
        "buffer layer", "energy alignment", "nonradiative recombination",
        "defect density", "ion accumulation",
    ],
}

REVIEW_DIMENSIONS = [
    "materials compositions and bandgaps",
    "deposition fabrication processing methods",
    "device architecture transport layers and interfaces",
    "photovoltaic performance efficiency voltage current fill factor",
    "film crystallization morphology defects and orientation",
    "mechanism and structure property relationships",
    "operational stability MPPT thermal light and environmental ageing",
    "large-area devices modules scale-up reproducibility yield and throughput",
    "applications tandems semitransparent flexible and indoor devices",
    "limitations challenges failures trade-offs and contradictory findings",
    "explicitly reported future work unresolved questions and research gaps",
]

NEGATIVE_EVIDENCE_TERMS = (
    "limitation challenge however failed lower efficiency poor stability "
    "degradation nonradiative recombination hindered transport unreacted "
    "precursor nonuniform irreproducible scale-up material loss trade-off"
)


def configure_utf8_output() -> None:
    """Prevent Windows CP1252 failures on scientific Unicode text."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


configure_utf8_output()


# --------------------------------------------------------------------------- #
# Answering back ends (optional; retrieval works without any of them)          #
# --------------------------------------------------------------------------- #
def read_gemini_key(cfg: Dict[str, Any]) -> str:
    key = str(cfg.get("gemini_api_key") or "").strip()
    if key:
        return key
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        return Path(GEMINI_KEY_PATH).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class GeminiLLM:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.name = f"Google Gemini API ({model})"

    def chat(self, system: str, user: str, timeout: int = 300) -> str:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
        }
        # Key passed as a query parameter rather than baked into the URL string.
        response = requests.post(
            url, params={"key": self.api_key}, json=payload, timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(part.get("text", "") for part in parts).strip()


class OllamaLLM:
    def __init__(self, url: str, model: str) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.name = f"Ollama ({model})"

    def chat(self, system: str, user: str, timeout: int = 300) -> str:
        response = requests.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "options": {"temperature": 0.1},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()["message"]["content"].strip()


class LlamaCppLLM:
    def __init__(self, repo: str, filename: str) -> None:
        from llama_cpp import Llama

        print("Loading local llama-cpp model...")
        self.llm = Llama.from_pretrained(
            repo_id=repo, filename=filename, n_ctx=16384, verbose=False
        )
        self.name = f"llama-cpp ({repo.split('/')[-1]})"

    def chat(self, system: str, user: str, timeout: int = 300) -> str:
        del timeout
        result = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=4096,
        )
        return result["choices"][0]["message"]["content"].strip()


def make_llm(config: Dict[str, Any], disabled: bool = False):
    if disabled:
        return None
    cfg = config.get("assistant", {}) or {}

    gemini_key = read_gemini_key(cfg)
    if gemini_key:
        return GeminiLLM(gemini_key, cfg.get("gemini_model", DEFAULT_GEMINI_MODEL))

    ollama_url = cfg.get("ollama_url", DEFAULT_OLLAMA_URL)
    ollama_model = cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL)
    try:
        response = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=2)
        if response.ok:
            return OllamaLLM(ollama_url, ollama_model)
    except requests.RequestException:
        pass

    try:
        import llama_cpp  # noqa: F401
        return LlamaCppLLM(
            cfg.get("gguf_repo", DEFAULT_GGUF_REPO),
            cfg.get("gguf_file", DEFAULT_GGUF_FILE),
        )
    except (ImportError, RuntimeError, OSError, ValueError) as exc:
        print(f"No answering AI available; using retrieval-only mode ({exc}).")
        return None


# --------------------------------------------------------------------------- #
# Query planning                                                               #
# --------------------------------------------------------------------------- #
def clean_query(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" \t\n-;:,. ")


def split_into_subquestions(question: str) -> List[str]:
    """Rule-based decomposition (also imported by export_for_claude.py).

    Long scientific paragraphs pack several topics into few sentences; split on
    sentence ends, semicolons and dash asides, keeping substantial fragments.
    """
    sentences = re.split(r"(?<=[.!?])\s+", question.strip())
    parts: List[str] = []
    for sentence in sentences:
        if len(sentence) > 220:
            parts.extend(re.split(r";\s*|\s+[—–]\s+", sentence))
        else:
            parts.append(sentence)
    cleaned = [clean_query(part)[:400] for part in parts if len(clean_query(part)) >= 20]
    return cleaned[:10] or [clean_query(question)[:400]]


def question_needs_decomposition(question: str) -> bool:
    stripped = question.strip()
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", stripped) if s]
    broad_markers = re.search(
        r"\b(review|what has been done|state of the art|research gap|landscape|"
        r"systematic|comprehensive|all studies|literature)\b",
        stripped,
        re.I,
    )
    return bool(
        len(stripped) > 180
        or stripped.count("?") > 1
        or len(sentences) > 2
        or broad_markers
    )


def llm_subquestions(question: str, llm, limit: int) -> List[str]:
    if llm is None:
        return []
    try:
        output = llm.chat(DECOMPOSE_SYSTEM, question, timeout=300)
        candidates = []
        for line in output.splitlines():
            line = re.sub(r"^\s*[-*•\d.)]+\s*", "", line)
            line = clean_query(line)
            if len(line) >= 15:
                candidates.append(line)
        return candidates[:limit]
    except Exception as exc:
        print(f"AI query planning failed; using rule-based planning ({exc}).")
        return []


def detected_expansions(question: str) -> List[str]:
    lower = question.lower()
    expansions: List[str] = []
    for group, terms in TERM_GROUPS.items():
        if group in lower or any(term.lower() in lower for term in terms):
            expansions.append(" ".join(terms))
    return expansions


def build_search_queries(
    question: str,
    mode: str,
    llm=None,
    manual_queries: Optional[Sequence[str]] = None,
) -> List[str]:
    """Preserve the original query and add independent retrieval probes.

    The systematic probes (the original question, any manual queries, the
    review dimensions, the negative-evidence sweep and terminology expansions)
    are *guaranteed*: they are what makes a search reproducible and broad, so
    they are never dropped to make room. Only the optional decomposition
    sub-questions compete for the leftover budget. This fixes the earlier
    behaviour where a productive LLM decomposition could crowd out every
    systematic dimension and silently narrow the search.
    """
    max_queries = MODE_SETTINGS[mode]["max_queries"]
    original = clean_query(question)
    subject = original[:240]

    guaranteed: List[str] = [original]
    if manual_queries:
        guaranteed.extend(clean_query(q) for q in manual_queries if clean_query(q))
    if mode in {"review", "exhaustive"}:
        guaranteed.extend(f"{subject} {dimension}" for dimension in REVIEW_DIMENSIONS)
        guaranteed.append(f"{subject} {NEGATIVE_EVIDENCE_TERMS}")
    guaranteed.extend(detected_expansions(question))

    optional: List[str] = []
    if question_needs_decomposition(question):
        optional.extend(llm_subquestions(question, llm, max_queries))
        optional.extend(split_into_subquestions(question))

    unique: List[str] = []
    seen: set = set()

    def add(candidate: str) -> None:
        candidate = clean_query(candidate)
        key = candidate.casefold()
        if candidate and key not in seen:
            seen.add(key)
            unique.append(candidate)

    for query in guaranteed:
        add(query)
    # Never trim a guaranteed probe; the budget only limits optional ones.
    budget = max(max_queries, len(unique))
    for query in optional:
        if len(unique) >= budget:
            break
        add(query)
    return unique


# --------------------------------------------------------------------------- #
# Cached, single-load retrieval                                                #
# --------------------------------------------------------------------------- #
def result_key(result: Dict[str, Any]) -> Tuple:
    """Stable identity for a retrieved passage.

    Prefers the chunk's database id (unique by construction). Falls back to
    full metadata + full matched text for any result that lacks one, so that
    table and figure caption chunks - whose chunk_index is NULL - can no longer
    collide on a shared 160-character prefix.
    """
    chunk_id = result.get("chunk_id")
    if chunk_id is not None:
        return ("id", int(chunk_id))
    return (
        "meta",
        str(result.get("doi", "")),
        str(result.get("kind", "text")),
        int(result.get("chunk_index") or -1),
        int(result.get("page") or -1),
        str(result.get("matched_text", result.get("text", ""))),
    )


def normalised_tokens(text: str) -> set:
    return set(re.findall(r"[A-Za-z0-9][A-Za-z0-9+_.-]{2,}", text.lower()))


def token_jaccard(a: str, b: str) -> float:
    left, right = normalised_tokens(a), normalised_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class LibraryIndex:
    """The passage index, read once and queried many times.

    build_index.retrieve() re-reads every embedding from SQLite and rebuilds
    the score matrix on *each* call. Review and exhaustive modes fire dozens of
    queries, so that repeated full scan dominated run time and made the tool
    scale badly with the library. Here the embedding matrix and passage
    metadata are loaded a single time; each query is then one matrix-vector
    product against the cached embeddings, scored with the identical hybrid
    formula as build_index.retrieve() so ranking stays consistent with
    "python build_index.py --search".
    """

    # Column order returned by the load query (embedding kept separately).
    _COLS = (
        "chunk_id", "text", "page", "page_end", "kind", "section",
        "chunk_index", "entry", "title", "year", "category", "doi",
    )

    def __init__(self, db) -> None:
        self.db = db
        rows = db.execute(
            """SELECT c.id, c.text, c.page, COALESCE(c.page_end, c.page),
                      c.kind, COALESCE(c.section, 'unknown'), c.chunk_index,
                      p.entry, p.title, p.year, p.category, p.doi, c.embedding
               FROM chunks c JOIN papers p ON p.doi = c.doi
               WHERE c.embedding IS NOT NULL
                 AND c.kind IN ('text', 'figure', 'table')"""
        ).fetchall()
        self.rows = [row[:12] for row in rows]
        if rows:
            self.matrix = np.frombuffer(
                b"".join(row[12] for row in rows), dtype=np.float32
            ).reshape(len(rows), -1)
        else:
            self.matrix = np.zeros((0, 1), dtype=np.float32)
        self.indexed_dois = {row[11] for row in self.rows}

    def __len__(self) -> int:
        return len(self.rows)

    def _as_result(self, meta: Sequence[Any], combined: float,
                   semantic: float, lexical: float) -> Dict[str, Any]:
        # Rebuild the 12-tuple that build_index._expand_with_neighbours expects
        # (text, page, page_end, embedding, kind, section, chunk_index, entry,
        #  title, year, category, doi); the embedding slot is read but unused.
        expand_row = (
            meta[1], meta[2], meta[3], None, meta[4], meta[5],
            meta[6], meta[7], meta[8], meta[9], meta[10], meta[11],
        )
        return {
            "chunk_id": meta[0],
            "text": build_index._expand_with_neighbours(self.db, expand_row),
            "matched_text": meta[1],
            "page": meta[2],
            "page_end": meta[3],
            "entry": meta[7],
            "title": meta[8],
            "year": meta[9],
            "category": meta[10],
            "doi": meta[11],
            "kind": meta[4],
            "section": meta[5],
            "chunk_index": meta[6],
            "score": combined,
            "semantic_score": semantic,
            "lexical_score": lexical,
        }

    def search(self, query: str, query_vector: np.ndarray, top_k: int) -> List[Dict[str, Any]]:
        if not self.rows:
            return []
        semantic_scores = self.matrix @ query_vector
        query_terms = build_index._query_terms(query)

        scored: List[Tuple[float, float, float, int]] = []
        for index, meta in enumerate(self.rows):
            semantic = float(semantic_scores[index])
            # Reproduce build_index._exact_term_score with the terms hoisted out
            # of the row loop (identical result, far less work per query).
            if query_terms:
                haystack = f"{meta[8]} {meta[1]}".lower()
                lexical = sum(1 for term in query_terms if term in haystack) / len(query_terms)
            else:
                lexical = 0.0
            weight = build_index.SECTION_WEIGHTS.get(meta[5], 0.98)
            combined = (SEMANTIC_WEIGHT * semantic + LEXICAL_WEIGHT * lexical) * weight
            scored.append((combined, semantic, lexical, index))
        scored.sort(reverse=True)

        candidate_limit = max(top_k * build_index.CANDIDATE_MULTIPLIER, top_k)
        per_paper: Dict[str, int] = defaultdict(int)
        results: List[Dict[str, Any]] = []
        for combined, semantic, lexical, index in scored[:candidate_limit]:
            meta = self.rows[index]
            doi = meta[11]
            if per_paper[doi] >= build_index.MAX_RESULTS_PER_PAPER:
                continue
            per_paper[doi] += 1
            results.append(self._as_result(meta, combined, semantic, lexical))
            if len(results) >= top_k:
                break
        return results


def embed_queries(embedder, queries: Sequence[str]) -> np.ndarray:
    """Embed every query in one batched call, L2-normalised like the passages."""
    vectors = np.array(list(embedder.query_embed(list(queries))), dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def retrieve_multi_query(
    index: LibraryIndex,
    embedder,
    queries: Sequence[str],
    top_per_query: int,
    final_passages: int,
    max_per_paper: int,
    min_papers: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, set]]:
    """Fuse query rankings, rank papers, diversify sections, and de-duplicate.

    Returns the selected passages, the ranked per-paper candidate table, and a
    map of query -> set of candidate DOIs (so coverage can show which search
    dimensions returned nothing at all).
    """
    query_vectors = embed_queries(embedder, queries)
    fused: Dict[Any, Dict[str, Any]] = {}
    per_query_candidates: Dict[str, set] = {}
    rrf_k = 60.0

    for query, query_vector in zip(queries, query_vectors):
        results = index.search(query, query_vector, top_per_query)
        per_query_candidates[query] = {str(r.get("doi", "")) for r in results}
        for rank, result in enumerate(results, 1):
            key = result_key(result)
            item = fused.setdefault(
                key,
                {
                    "result": result,
                    "fusion_score": 0.0,
                    "best_score": float("-inf"),
                    "best_semantic": float("-inf"),
                    "best_lexical": float("-inf"),
                    "matched_queries": [],
                },
            )
            item["fusion_score"] += 1.0 / (rrf_k + rank)
            item["best_score"] = max(item["best_score"], float(result.get("score", 0.0)))
            item["best_semantic"] = max(item["best_semantic"], float(result.get("semantic_score", 0.0)))
            item["best_lexical"] = max(item["best_lexical"], float(result.get("lexical_score", 0.0)))
            item["matched_queries"].append(query)

    candidates: List[Dict[str, Any]] = []
    for item in fused.values():
        result = dict(item["result"])
        result["fusion_score"] = item["fusion_score"]
        result["best_score"] = item["best_score"]
        # Report the strongest scores seen across all queries, not the first.
        result["semantic_score"] = item["best_semantic"]
        result["lexical_score"] = item["best_lexical"]
        result["matched_queries"] = list(dict.fromkeys(item["matched_queries"]))
        result["query_count"] = len(result["matched_queries"])
        result["fused_sort_score"] = (
            item["fusion_score"]
            + 0.02 * item["best_score"]
            + 0.002 * min(result["query_count"], 5)
        )
        candidates.append(result)

    candidates.sort(key=lambda r: r["fused_sort_score"], reverse=True)

    by_paper: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for result in candidates:
        by_paper[str(result.get("doi", ""))].append(result)

    paper_rows: List[Dict[str, Any]] = []
    for doi, passages in by_paper.items():
        scores = sorted((p["fused_sort_score"] for p in passages), reverse=True)
        paper_score = scores[0]
        if len(scores) > 1:
            paper_score += 0.35 * scores[1]
        if len(scores) > 2:
            paper_score += 0.15 * scores[2]
        best = passages[0]
        paper_rows.append(
            {
                "doi": doi,
                "entry": best.get("entry", ""),
                "title": best.get("title", ""),
                "year": best.get("year", ""),
                "category": best.get("category", ""),
                "paper_score": paper_score,
                "candidate_passages": len(passages),
                "matched_queries": sorted(
                    {q for passage in passages for q in passage["matched_queries"]}
                ),
                "passages": passages,
            }
        )
    paper_rows.sort(key=lambda p: p["paper_score"], reverse=True)

    # --- Selection: diversify across papers and sections, O(n) throughout. ---
    selected: List[Dict[str, Any]] = []
    selected_ids: set = set()
    per_paper_count: Dict[str, int] = defaultdict(int)
    sections_by_paper: Dict[str, set] = defaultdict(set)
    texts_by_paper: Dict[str, List[str]] = defaultdict(list)

    def take(passage: Dict[str, Any]) -> None:
        doi = str(passage.get("doi", ""))
        selected.append(passage)
        selected_ids.add(result_key(passage))
        per_paper_count[doi] += 1
        sections_by_paper[doi].add(passage.get("section", "unknown"))
        texts_by_paper[doi].append(str(passage.get("matched_text", passage.get("text", ""))))

    target_papers = min(min_papers, len(paper_rows))

    # First pass: one best passage from each of the top-ranked papers.
    for paper in paper_rows:
        if len(selected) >= final_passages or len(per_paper_count) >= target_papers:
            break
        take(paper["passages"][0])

    # Second pass: complementary passages, preferring new sections and low overlap.
    for paper in paper_rows:
        if len(selected) >= final_passages:
            break
        doi = paper["doi"]
        for passage in paper["passages"]:
            if len(selected) >= final_passages:
                break
            if per_paper_count[doi] >= max_per_paper:
                break
            if result_key(passage) in selected_ids:
                continue
            text = str(passage.get("matched_text", passage.get("text", "")))
            if any(token_jaccard(text, old) >= 0.80 for old in texts_by_paper[doi]):
                continue
            section = passage.get("section", "unknown")
            if section in sections_by_paper[doi] and len(paper["passages"]) > 1:
                continue
            take(passage)

    # Final fill if strict section diversity left unused capacity.
    if len(selected) < final_passages:
        for passage in candidates:
            if len(selected) >= final_passages:
                break
            doi = str(passage.get("doi", ""))
            if per_paper_count[doi] >= max_per_paper or result_key(passage) in selected_ids:
                continue
            take(passage)

    selected.sort(key=lambda r: r["fused_sort_score"], reverse=True)
    return selected, paper_rows, per_query_candidates


# --------------------------------------------------------------------------- #
# Formatting                                                                   #
# --------------------------------------------------------------------------- #
def page_label(result: Dict[str, Any]) -> str:
    start = result.get("page", "?")
    end = result.get("page_end", start)
    return f"p.{start}" if end == start else f"pp.{start}-{end}"


def citation_label(result: Dict[str, Any]) -> str:
    return f"[Entry {result.get('entry', '?')}, p.{result.get('page', '?')}]"


def format_passages(results: Sequence[Dict[str, Any]]) -> str:
    blocks = []
    for index, result in enumerate(results, 1):
        blocks.append(
            f"Source {index}: {citation_label(result)} "
            f"({result.get('year', '')}, {result.get('category', '')}, "
            f"{result.get('section', 'unknown')}) "
            f"{str(result.get('title', ''))[:120]}\n"
            f"{result.get('text', '')}"
        )
    return "\n\n".join(blocks)


def verbatim_answer(results: Sequence[Dict[str, Any]]) -> str:
    blocks = []
    for result in results:
        blocks.append(
            f"**{citation_label(result)}** ({result.get('year', '')}) "
            f"{result.get('title', '')}  \n"
            f"Section: {str(result.get('section', 'unknown')).replace('_', ' ')}; "
            f"retrieved by {result.get('query_count', 1)} query or queries.\n\n"
            f"> {str(result.get('text', '')).replace(chr(10), chr(10) + '> ')}"
        )
    return "\n\n".join(blocks)


def cap_evidence_for_model(
    selected: Sequence[Dict[str, Any]], char_budget: int
) -> List[Dict[str, Any]]:
    """Top-ranked passages that fit a local model's context, always >= 1."""
    kept: List[Dict[str, Any]] = []
    used = 0
    for result in selected:
        block = len(str(result.get("text", ""))) + 200  # per-block framing
        if kept and used + block > char_budget:
            break
        kept.append(result)
        used += block
    return kept


def flag_uncited_sentences(body: str) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s+", body)
    exceptions = (
        "retrieved evidence does not provide",
        "does not establish that the information is absent",
    )
    return [
        sentence
        for sentence in sentences
        if len(sentence) > 80
        and not re.search(r"\[Entry\s+\d+", sentence)
        and not any(term in sentence.lower() for term in exceptions)
    ]


def cited_entries(body: str) -> List[int]:
    return sorted({int(value) for value in re.findall(r"\[Entry\s+(\d+)", body)})


def build_references(db, entries: Iterable[int]) -> List[str]:
    references = []
    for entry in entries:
        row = db.execute(
            "SELECT entry, title, year, doi FROM papers WHERE entry = ?",
            (str(entry),),
        ).fetchone()
        if not row:
            continue
        entry_no, title, year, doi = row
        doi_part = f" https://doi.org/{doi}" if doi and str(doi).startswith("10.") else ""
        references.append(f"[Entry {entry_no}] {title} ({year}).{doi_part}")
    return references


# --------------------------------------------------------------------------- #
# Coverage, health, and traceability                                           #
# --------------------------------------------------------------------------- #
def index_statistics(db) -> Dict[str, int]:
    papers = db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    chunks = db.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0]
    text_chunks = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE kind = 'text' AND embedding IS NOT NULL"
    ).fetchone()[0]
    figure_chunks = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE kind = 'figure' AND embedding IS NOT NULL"
    ).fetchone()[0]
    return {
        "indexed_papers": papers,
        "indexed_passages": chunks,
        "text_passages": text_chunks,
        "figure_captions": figure_chunks,
    }


def library_health(db, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compare what was downloaded with what actually made it into the index.

    A downloaded PDF that never reached the index is an extraction/indexing
    failure (or has simply not been indexed yet) - it is emphatically not
    evidence that its content is absent from the library. Surfacing this keeps
    'not indexed' from being mistaken for 'genuinely absent'. Degrades quietly
    when the tracking sheet is unavailable.
    """
    info: Dict[str, Any] = {
        "downloaded_papers": None,
        "unindexed_papers": None,
        "unindexed_examples": [],
    }
    try:
        config = config or load_config()
        tracking = load_tracking(config["paths"]["tracking_csv"])
    except Exception:
        return info
    if not tracking:
        return info

    indexed_keys = {row[0] for row in db.execute("SELECT doi FROM papers")}
    downloaded = 0
    unindexed_rows = []
    for row in tracking.values():
        if row.get("Status") not in build_index.INDEXED_STATUSES:
            continue
        pdf_path = row.get("PDF_Path", "")
        if not pdf_path or not os.path.exists(pdf_path):
            continue
        downloaded += 1
        key = row.get("DOI", "") or pdf_path
        if key not in indexed_keys:
            unindexed_rows.append(row)

    info["downloaded_papers"] = downloaded
    info["unindexed_papers"] = len(unindexed_rows)
    info["unindexed_examples"] = [
        f"Entry {r.get('Entry', '?')}: {str(r.get('Title', ''))[:70]}"
        for r in unindexed_rows[:10]
    ]
    return info


def coverage_summary(
    stats: Dict[str, int],
    health: Dict[str, Any],
    queries: Sequence[str],
    selected: Sequence[Dict[str, Any]],
    papers: Sequence[Dict[str, Any]],
    per_query_candidates: Dict[str, set],
    requested_min_papers: int,
) -> Dict[str, Any]:
    represented = len({str(r.get("doi", "")) for r in selected})
    candidate_papers = len(papers)
    indexed_papers = stats["indexed_papers"]

    query_selected_counts = {
        query: len(
            {
                str(result.get("doi", ""))
                for result in selected
                if query in result.get("matched_queries", [])
            }
        )
        for query in queries
    }
    query_candidate_counts = {
        query: len(per_query_candidates.get(query, set())) for query in queries
    }
    empty_dimensions = [query for query in queries if query_candidate_counts.get(query, 0) == 0]
    covered_dimensions = sum(1 for count in query_selected_counts.values() if count > 0)
    unprobed = max(indexed_papers - candidate_papers, 0)

    coverage = {
        **stats,
        "queries_run": len(queries),
        "candidate_papers": candidate_papers,
        "represented_papers": represented,
        "selected_passages": len(selected),
        "library_unprobed_papers": unprobed,
        "library_probed_fraction": round(candidate_papers / indexed_papers, 4) if indexed_papers else 0.0,
        "downloaded_papers": health.get("downloaded_papers"),
        "unindexed_papers": health.get("unindexed_papers"),
        "unindexed_examples": health.get("unindexed_examples", []),
        "dimensions_with_selected_evidence": covered_dimensions,
        "empty_search_dimensions": empty_dimensions,
        "requested_minimum_papers": requested_min_papers,
        "minimum_paper_target_met": represented >= min(requested_min_papers, candidate_papers),
        "query_selected_paper_counts": query_selected_counts,
        "query_candidate_paper_counts": query_candidate_counts,
        "coverage_warning": (
            "This is estimated retrieval coverage within the indexed library, not proof "
            "that every relevant paper was found."
        ),
    }
    return coverage


def coverage_markdown(coverage: Dict[str, Any]) -> str:
    target = "yes" if coverage["minimum_paper_target_met"] else "no"
    unprobed = coverage.get("library_unprobed_papers", 0)
    probed_pct = coverage.get("library_probed_fraction", 0.0) * 100
    downloaded = coverage.get("downloaded_papers")
    unindexed = coverage.get("unindexed_papers")

    lines = [
        "## Retrieval and coverage status",
        "",
        f"- Indexed papers (searchable): {coverage['indexed_papers']}",
        f"- Indexed passages: {coverage['indexed_passages']} "
        f"({coverage.get('text_passages', 0)} text, {coverage.get('figure_captions', 0)} figure captions)",
        f"- Search queries run: {coverage['queries_run']}",
        f"- Candidate papers retrieved by at least one query: {coverage['candidate_papers']} "
        f"({probed_pct:.0f}% of the indexed library)",
        f"- Indexed papers no query surfaced ('not retrieved'): {unprobed}",
        f"- Distinct papers represented in final evidence: {coverage['represented_papers']}",
        f"- Selected passages: {coverage['selected_passages']}",
        f"- Query dimensions with selected evidence: "
        f"{coverage['dimensions_with_selected_evidence']} of {coverage['queries_run']}",
        f"- Minimum paper target met: {target} "
        f"(target {coverage['requested_minimum_papers']})",
    ]

    if downloaded is not None:
        lines.append(
            f"- Downloaded papers vs. indexed: {downloaded} downloaded, "
            f"{coverage['indexed_papers']} indexed"
        )
    if unindexed:
        lines.append(
            f"- Downloaded but NOT indexed ('not indexed' - extraction/indexing "
            f"failure or not yet indexed): {unindexed}"
        )
        for example in coverage.get("unindexed_examples", [])[:5]:
            lines.append(f"    - {example}")
    if coverage.get("empty_search_dimensions"):
        empties = coverage["empty_search_dimensions"]
        lines.append(
            f"- Search dimensions that returned nothing ({len(empties)}); a "
            "terminology gap or a genuine absence, not yet distinguished:"
        )
        for query in empties[:8]:
            lines.append(f"    - {query[:110]}")

    lines += [
        "",
        "### How to read this",
        "",
        "- **Not retrieved:** in the index but unsurfaced. If many papers went "
        "unprobed, or dimensions came back empty, treat gaps as retrieval limits, "
        "not absence - rerun with `--mode review`/`exhaustive`, add `--queries-file` "
        "terms, or widen terminology.",
        "- **Not sufficiently supported:** retrieved, but the passages do not "
        "settle the question. The answer says so explicitly.",
        "- **Not indexed:** downloaded but missing from the index (see above). Fix "
        "by re-running `build_index.py`; never read as absence.",
        "- **Genuinely absent:** only defensible after broad retrieval probed most "
        "of the library and still found nothing - and even then, only for the "
        "indexed library.",
        "",
        f"- Important qualification: {coverage['coverage_warning']}",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Output artefacts                                                             #
# --------------------------------------------------------------------------- #
def save_audit_csv(path: Path, papers: Sequence[Dict[str, Any]], selected) -> None:
    selected_keys = {result_key(r) for r in selected}
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "rank", "entry", "doi", "title", "year", "category",
                "paper_score", "candidate_passages", "matched_query_count",
                "best_section", "best_page", "best_semantic_score",
                "best_lexical_score", "included_in_final_evidence",
            ],
        )
        writer.writeheader()
        for rank, paper in enumerate(papers, 1):
            best = paper["passages"][0]
            writer.writerow(
                {
                    "rank": rank,
                    "entry": paper["entry"],
                    "doi": paper["doi"],
                    "title": paper["title"],
                    "year": paper["year"],
                    "category": paper["category"],
                    "paper_score": f"{paper['paper_score']:.8f}",
                    "candidate_passages": paper["candidate_passages"],
                    "matched_query_count": len(paper["matched_queries"]),
                    "best_section": best.get("section", "unknown"),
                    "best_page": page_label(best),
                    "best_semantic_score": f"{float(best.get('semantic_score', 0)):.6f}",
                    "best_lexical_score": f"{float(best.get('lexical_score', 0)):.6f}",
                    "included_in_final_evidence": any(
                        result_key(p) in selected_keys for p in paper["passages"]
                    ),
                }
            )


def save_manifest(
    path: Path,
    question: str,
    mode: str,
    queries: Sequence[str],
    settings: Dict[str, int],
    coverage: Dict[str, Any],
    selected: Sequence[Dict[str, Any]],
) -> None:
    payload = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "question": question,
        "mode": mode,
        "embedding_model": getattr(build_index, "EMBED_MODEL", "unknown"),
        "settings": settings,
        "queries": list(queries),
        "coverage": coverage,
        "selected_evidence": [
            {
                "chunk_id": r.get("chunk_id"),
                "entry": r.get("entry"),
                "doi": r.get("doi"),
                "title": r.get("title"),
                "page": r.get("page"),
                "page_end": r.get("page_end"),
                "section": r.get("section"),
                "kind": r.get("kind"),
                "matched_queries": r.get("matched_queries", []),
                "fusion_score": r.get("fusion_score"),
                "semantic_score": r.get("semantic_score"),
                "lexical_score": r.get("lexical_score"),
            }
            for r in selected
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_manual_queries(path: Optional[str]) -> List[str]:
    if not path:
        return []
    return [
        clean_query(line)
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
        if clean_query(line) and not line.lstrip().startswith("#")
    ]


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def retrieve_evidence(
    question: str,
    mode: str,
    index: LibraryIndex,
    embedder,
    llm,
    manual_queries: Sequence[str],
    overrides: Dict[str, Optional[int]],
    config: Optional[Dict[str, Any]] = None,
):
    settings = dict(MODE_SETTINGS[mode])
    for key, value in overrides.items():
        if value is not None:
            settings[key] = value

    queries = build_search_queries(question, mode, llm, manual_queries)
    print(f"Running {len(queries)} search query or queries in {mode!r} mode...")
    selected, papers, per_query_candidates = retrieve_multi_query(
        index,
        embedder,
        queries,
        settings["top_per_query"],
        settings["final_passages"],
        settings["max_passages_per_paper"],
        settings["min_papers"],
    )
    stats = index_statistics(index.db)
    health = library_health(index.db, config)
    coverage = coverage_summary(
        stats, health, queries, selected, papers, per_query_candidates, settings["min_papers"]
    )
    return selected, papers, queries, settings, coverage


def export_question_pack(
    question: str,
    selected: Sequence[Dict[str, Any]],
    queries: Sequence[str],
    coverage: Dict[str, Any],
    db,
    output_path: Path,
) -> None:
    entries = sorted(
        {int(r["entry"]) for r in selected if str(r.get("entry", "")).isdigit()}
    )
    references = build_references(db, entries)
    content = (
        "## Question pack\n\n"
        "### Instructions for the writer\n\n"
        f"{ANSWER_SYSTEM}\n\n"
        f"**Question:** {question}\n\n"
        "### Search queries used\n\n"
        + "\n".join(f"- {query}" for query in queries)
        + "\n\n"
        + coverage_markdown(coverage)
        + "\n## Retrieved evidence\n\n"
        + format_passages(selected)
        + "\n\n## References catalog\n\n"
        + "\n".join(f"- {reference}" for reference in references)
        + "\n"
    )
    output_path.write_text(content, encoding="utf-8")


def answer_with_llm(question: str, selected, coverage, llm) -> str:
    kept = cap_evidence_for_model(selected, MODEL_EVIDENCE_CHAR_BUDGET)
    trimmed_note = ""
    if len(kept) < len(selected):
        print(
            f"Note: sending {len(kept)}/{len(selected)} passages to the model "
            "(context budget); full evidence is in the CSV, JSON, and --pack export."
        )
        trimmed_note = (
            f"\n\nContext note: {len(kept)} of {len(selected)} retrieved passages are "
            "shown below because of the model's context limit. Absence of a passage "
            "here is a context-window limit, not evidence of absence."
        )
    evidence = format_passages(kept)
    prompt = (
        f"Question: {question}\n\n"
        f"Retrieval status:\n{coverage_markdown(coverage)}{trimmed_note}\n\n"
        f"Numbered source passages:\n\n{evidence}\n\n"
        "Write one integrated answer. Separate established work, limitations, and "
        "research gaps. Do not claim field-wide completeness from retrieval silence."
    )
    return llm.chat(ANSWER_SYSTEM, prompt, timeout=600)


def read_question(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8-sig").strip()
    return " ".join(args.question).strip()


def companion_path(output_path: Path, suffix: str) -> Path:
    return output_path.with_name(output_path.stem + suffix)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ask the indexed paper library using paper-aware multi-query retrieval."
    )
    parser.add_argument("question", nargs="*", help="question text; quote it or use --file")
    parser.add_argument("--file", help="read the question from a UTF-8 text file")
    parser.add_argument(
        "--mode",
        choices=sorted(MODE_SETTINGS),
        default="focused",
        help="focused fact retrieval, broad review, or exhaustive evidence mapping",
    )
    parser.add_argument("--queries-file", help="optional file with one search query per line")
    parser.add_argument("--top", type=int, help="override final number of passages")
    parser.add_argument("--top-per-query", type=int, help="override candidates per query")
    parser.add_argument("--min-papers", type=int, help="target minimum distinct papers")
    parser.add_argument(
        "--max-passages-per-paper", type=int, help="maximum final passages from one paper"
    )
    parser.add_argument("--no-ai", action="store_true", help="retrieval-only verbatim output")
    parser.add_argument(
        "--pack",
        action="store_true",
        help="export the question, search details, and evidence instead of answering locally",
    )
    parser.add_argument(
        "--out",
        help="output Markdown file or folder; companion CSV and JSON files are saved beside it",
    )
    args = parser.parse_args()

    question = read_question(args)
    if not question:
        parser.error("Provide a question or use --file.")

    config = load_config()
    # Pack mode needs no answering model, but review planning may use one.
    llm = make_llm(config, disabled=args.no_ai)
    db = build_index.open_db()

    index = LibraryIndex(db)
    if len(index) == 0:
        sys.exit(
            "The searchable index is empty (no embedded passages). Run:\n"
            "    python build_index.py\n"
            "This is an indexing state, not evidence that any topic is absent."
        )

    embedder = build_index.get_embedder()
    manual_queries = load_manual_queries(args.queries_file)

    overrides = {
        "final_passages": args.top,
        "top_per_query": args.top_per_query,
        "min_papers": args.min_papers,
        "max_passages_per_paper": args.max_passages_per_paper,
    }
    selected, papers, queries, settings, coverage = retrieve_evidence(
        question, args.mode, index, embedder, llm, manual_queries, overrides, config
    )
    if not selected:
        sys.exit(
            "No passages were retrieved from the current index. This may indicate an "
            "indexing, extraction, terminology, or retrieval problem; it does not prove "
            "that the topic is absent from the PDFs. Try --mode review/exhaustive or a "
            "--queries-file with alternative terminology."
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_name = f"question_pack_{stamp}.md" if args.pack else f"answer_{stamp}.md"
    output_path = Path(resolve_output_path(args.out, ANSWERS_DIR, default_name))
    audit_path = companion_path(output_path, "_retrieval_audit.csv")
    manifest_path = companion_path(output_path, "_search_manifest.json")

    save_audit_csv(audit_path, papers, selected)
    save_manifest(manifest_path, question, args.mode, queries, settings, coverage, selected)

    if args.pack:
        export_question_pack(question, selected, queries, coverage, db, output_path)
    else:
        if args.no_ai or llm is None:
            body = verbatim_answer(selected)
        else:
            print(f"Writing grounded answer via {llm.name}...")
            body = answer_with_llm(question, selected, coverage, llm)

        references = build_references(db, cited_entries(body))
        uncited = flag_uncited_sentences(body)
        warning = ""
        if uncited:
            warning = (
                "\n\n## Citation check\n\n"
                "The following long sentences have no Entry citation and should be verified:\n\n"
                + "\n".join(f"- {sentence}" for sentence in uncited)
            )
        document = (
            f"# Library answer\n\n**Question:** {question}\n\n"
            f"{body}\n\n{coverage_markdown(coverage)}"
            f"\n## References\n\n"
            + ("\n".join(f"- {ref}" for ref in references) or "No cited entries detected.")
            + warning
            + "\n"
        )
        output_path.write_text(document, encoding="utf-8")

    print()
    print(f"Saved main output: {output_path}")
    print(f"Saved retrieval audit: {audit_path}")
    print(f"Saved search manifest: {manifest_path}")
    print(
        f"Evidence contains {len(selected)} passages from "
        f"{coverage['represented_papers']} distinct papers; "
        f"{coverage['candidate_papers']} candidate papers were identified "
        f"({coverage['library_unprobed_papers']} indexed papers went unprobed)."
    )
    if coverage.get("unindexed_papers"):
        print(
            f"Heads-up: {coverage['unindexed_papers']} downloaded paper(s) are NOT in "
            "the index (extraction/indexing gap) - rerun build_index.py to include them."
        )
    print("Reminder: estimated retrieval coverage is not proof of complete literature recall.")


if __name__ == "__main__":
    main()

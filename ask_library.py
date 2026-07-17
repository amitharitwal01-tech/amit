"""Phase 3 of the literature assistant: ask your library questions —
including long, paragraph-sized ones.

The question is automatically broken into its distinct sub-questions,
each sub-question is answered from retrieved passages of YOUR indexed
papers only, every claim carries a citation like [Entry 3, p.5], and
the parts are assembled into one answer saved as a Markdown file in
answers/.

The answering AI is found automatically, in this order:
- Ollama, if it's running (free local AI, https://ollama.com).
- llama-cpp-python, if installed — a pip-only local AI for machines
  where installing programs isn't allowed:
      pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
  First use downloads the model once (~4.7 GB), then it runs offline.
- Neither: retrieval-only — the same sub-questions, each answered with
  the most relevant verbatim passages. Nothing is ever invented in
  this mode by construction.

With either AI, written answers are grounded in and cited to the
retrieved passages, and sentences produced without a citation are
flagged for you to verify rather than silently trusted.

Usage:
    python ask_library.py "your question or whole paragraph here"
    python ask_library.py --file question.txt
    python ask_library.py --no-ai "..."   # force verbatim-passages mode
"""
import argparse
import os
import re
import sys
from datetime import datetime

import requests

from doi_resolver import load_config
import build_index

ANSWERS_DIR = "answers"

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"

DECOMPOSE_SYSTEM = (
    "You break complex research queries into their distinct sub-questions. "
    "Output only the sub-questions, one per line, each phrased as a standalone "
    "question that could be searched independently. No numbering, no commentary."
)

ANSWER_SYSTEM = (
    "You are a scientific literature assistant. Answer strictly and only from "
    "the numbered source passages provided — never from outside knowledge. "
    "Write precise scientific prose, as a scientist would: define terms, state "
    "mechanisms, quantify where the sources quantify. Every factual claim must "
    "carry a citation like [Entry 3, p.5], copied from the label of the passage "
    "that supports it. If the passages do not contain enough information to "
    "answer, reply exactly: The library does not cover this."
)


DEFAULT_GGUF_REPO = "Qwen/Qwen2.5-7B-Instruct-GGUF"
DEFAULT_GGUF_FILE = "*q4_k_m.gguf"


class OllamaLLM:
    def __init__(self, url, model):
        self.url = url
        self.model = model
        self.name = f"{model} via Ollama"

    def chat(self, system, prompt, timeout=900):
        resp = requests.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                # Low temperature: this is grounded synthesis, not
                # creative writing — we want the passages' content,
                # tightly phrased.
                "options": {"temperature": 0.2},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()


class LlamaCppLLM:
    # Same local-AI idea as Ollama, but delivered as a plain pip
    # package — for machines where installing programs isn't allowed.
    # The model file comes from HuggingFace through the same download
    # mechanism the embedding models already use, and is cached locally.
    def __init__(self, repo, filename):
        from llama_cpp import Llama
        print(
            "Loading the local AI model (first ever use downloads it once, "
            "~4.7 GB — after that it starts from disk)..."
        )
        self.llm = Llama.from_pretrained(
            repo_id=repo, filename=filename,
            n_ctx=8192, verbose=False,
        )
        self.name = f"{repo.split('/')[-1]} via llama-cpp-python"

    def chat(self, system, prompt, timeout=None):
        out = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1200,
        )
        return out["choices"][0]["message"]["content"].strip()


def make_llm(config):
    # Returns the first working AI backend, or None (verbatim mode).
    cfg = config.get("assistant", {}) or {}
    url = cfg.get("ollama_url", DEFAULT_OLLAMA_URL)
    model = cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL)
    try:
        if requests.get(f"{url}/api/tags", timeout=3).ok:
            return OllamaLLM(url, model)
    except requests.RequestException:
        pass

    try:
        import llama_cpp  # noqa: F401 — presence check before the heavy load
    except ImportError:
        return None
    try:
        return LlamaCppLLM(
            cfg.get("gguf_repo", DEFAULT_GGUF_REPO),
            cfg.get("gguf_file", DEFAULT_GGUF_FILE),
        )
    except Exception as e:
        print(f"(couldn't load the llama-cpp model: {type(e).__name__}: {e})")
        return None


def split_into_subquestions(question):
    # No-AI decomposition: long scientific paragraphs pack several
    # topics into few sentences, separated by sentence ends, semicolons
    # and em-dash asides — split there, keep substantial fragments.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", question.strip())
    parts = []
    for sentence in sentences:
        if len(sentence) > 220:
            parts.extend(re.split(r";\s*|\s+—\s+", sentence))
        else:
            parts.append(sentence)
    cleaned = []
    for part in parts:
        part = part.strip(" \t\n-—;:,.")
        if len(part) >= 40:
            cleaned.append(part[:300])
    return cleaned[:8] or [question.strip()[:300]]


def decompose_question(question, llm):
    if llm:
        try:
            output = llm.chat(DECOMPOSE_SYSTEM, question, timeout=300)
            subs = [
                line.strip(" \t-•*0123456789.").strip()
                for line in output.splitlines()
            ]
            subs = [s for s in subs if len(s) >= 15]
            if 1 <= len(subs) <= 12:
                return subs
        except Exception as e:
            print(f"(sub-question detection via local AI failed, using rule-based split: {e})")
    return split_into_subquestions(question)


def format_passages(results):
    blocks = []
    for r in results:
        label = f"[Entry {r['entry']}, p.{r['page']}]"
        blocks.append(f"{label} ({r['year']}, {r['category']}) {r['title'][:90]}\n{r['text']}")
    return "\n\n".join(blocks)


def verbatim_answer(results):
    lines = []
    for r in results:
        lines.append(
            f"**[Entry {r['entry']}, p.{r['page']}]** ({r['year']}) {r['title'][:90]}:\n"
            f"> {r['text']}"
        )
    return "\n\n".join(lines)


def flag_uncited_sentences(body):
    # The grounding contract says every factual sentence cites a
    # passage. Sentences that don't are where hallucination could hide —
    # surface them instead of trusting silently.
    sentences = re.split(r"(?<=[.!?])\s+", body)
    return [
        s for s in sentences
        if len(s) > 80
        and not re.search(r"\[Entry \d+", s)
        and "library does not cover" not in s.lower()
    ]


def cited_entries(body):
    return sorted({int(m) for m in re.findall(r"\[Entry (\d+)", body)})


def build_references(db, entries):
    refs = []
    for entry in entries:
        row = db.execute(
            "SELECT entry, title, year, doi FROM papers WHERE entry = ?", (str(entry),)
        ).fetchone()
        if row:
            entry_no, title, year, doi = row
            doi_part = f" https://doi.org/{doi}" if doi and doi.startswith("10.") else ""
            refs.append(f"[Entry {entry_no}] {title} ({year}).{doi_part}")
    return refs


def main():
    parser = argparse.ArgumentParser(description="Ask your paper library a question (or a whole paragraph).")
    parser.add_argument("question", nargs="*", help="the question; quote it, or use --file")
    parser.add_argument("--file", help="read the question from a text file")
    parser.add_argument("--top", type=int, default=6, help="passages retrieved per sub-question")
    parser.add_argument("--no-ai", action="store_true", help="skip the local AI; verbatim passages only")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            question = f.read().strip()
    else:
        question = " ".join(args.question).strip()
    if not question:
        parser.error("give a question, either on the command line or with --file")

    config = load_config()
    db = build_index.open_db()
    if db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0:
        sys.exit("The library index is empty — run  python build_index.py  first.")
    embedder = build_index.get_embedder()

    llm = None
    if not args.no_ai:
        llm = make_llm(config)
        if llm:
            print(f"Local AI: {llm.name}.")
        else:
            print(
                "No local AI found — answers will be verbatim passages only.\n"
                "For written answers, either run Ollama (https://ollama.com), or —\n"
                "if you can't install programs — install the pip-only backend:\n"
                "  pip install llama-cpp-python --extra-index-url "
                "https://abetlen.github.io/llama-cpp-python/whl/cpu"
            )

    subs = decompose_question(question, llm)
    print(f"\nDetected {len(subs)} sub-question(s):")
    for i, sub in enumerate(subs, 1):
        print(f"  {i}. {sub[:100]}{'...' if len(sub) > 100 else ''}")

    sections = []
    used_entries = set()
    for i, sub in enumerate(subs, 1):
        print(f"\n[{i}/{len(subs)}] retrieving + answering ...", flush=True)
        results = build_index.retrieve(db, embedder, sub, top_k=args.top)
        if not results:
            sections.append((sub, "_No relevant passages found in the library._", []))
            continue

        if llm:
            prompt = f"Sub-question: {sub}\n\nSource passages:\n\n{format_passages(results)}"
            try:
                body = llm.chat(ANSWER_SYSTEM, prompt)
            except Exception as e:
                print(f"  local AI failed on this part ({e}) — falling back to verbatim passages")
                body = verbatim_answer(results)
            uncited = flag_uncited_sentences(body)
            if uncited:
                body += (
                    "\n\n> ⚠ Sentences without citations (verify against the passages "
                    "before trusting):\n"
                    + "\n".join(f"> - {s}" for s in uncited)
                )
            used_entries.update(cited_entries(body))
        else:
            body = verbatim_answer(results)
            used_entries.update(int(r["entry"]) for r in results if str(r["entry"]).isdigit())

        sections.append((sub, body, results))

    lines = ["# Answer from your library", "", f"**Question:** {question}", ""]
    if not llm:
        lines += [
            "> ⚠ **No local AI was active for this run.** What follows are "
            "verbatim excerpts from your papers (retrieval-only mode), NOT "
            "composed answers. For written, structured answers, get Ollama "
            "or llama-cpp-python working and run the question again.",
            "",
        ]
    for i, (sub, body, _) in enumerate(sections, 1):
        lines += [f"## {i}. {sub}", "", body, ""]
    refs = build_references(db, sorted(used_entries))
    if refs:
        lines += ["## References (from your library)", ""] + [f"- {r}" for r in refs]

    output = "\n".join(lines)
    os.makedirs(ANSWERS_DIR, exist_ok=True)
    out_path = os.path.join(ANSWERS_DIR, f"answer_{datetime.now():%Y%m%d_%H%M%S}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)

    print("\n" + "=" * 70)
    print(output)
    print("=" * 70)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()

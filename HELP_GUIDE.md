# Help Guide — Paper Pipeline & Manuscript Engine

This walks through everything the pipeline can do, from your very first
run to finishing a manuscript, in order. Every step shows **two ways**
to do it: through the Manuscript Engine app, and as a plain command you
can type in a terminal. If the app ever won't start on your machine,
every single feature is still reachable from the terminal — nothing is
GUI-only.

This same file lives at `HELP_GUIDE.md` in your pipeline folder, and is
also viewable inside the app itself (sidebar → **Help**).

---

## 0. One-time setup

**Recommended: a dedicated environment for this folder.** Anaconda's
`base` environment often conflicts with PySide6's Qt files (DLL errors,
very slow installs) — a plain virtual environment avoids that entirely.
From inside your pipeline folder:

```
conda deactivate
python -m venv pipeline_env
pipeline_env\Scripts\activate
pip install -r paper_pipeline_requirements.txt PySide6 ruamel.yaml
python -m playwright install chromium
```

**If your terminal prompt starts with `(base)`** (Anaconda's default
environment auto-activating), run `conda deactivate` first, as above —
otherwise you'll end up with a prompt like `(pipeline_env) (base) ...`
where *both* are active, Anaconda's own Qt/DLL files stay on `PATH`
alongside the venv's, and you'll hit the exact same DLL error the venv
was supposed to fix. One-time permanent fix so new terminals never
auto-activate `base`: `conda config --set auto_activate_base false`.

(On macOS/Linux, activate with `source pipeline_env/bin/activate`
instead.) `paper_pipeline_requirements.txt` covers the download/index/AI
scripts (`requests`, `pandas`, `openpyxl`, `PyMuPDF`, `pypdf`,
`fastembed`, `playwright`, ...); `PySide6`/`ruamel.yaml` are only needed
for the desktop app.

Once this venv exists, you never need to type `activate` again to run
the app day to day — see **one-click launch** just below.

### One-click launch (Windows)

Double-click **`Launch_Manuscript_Engine.bat`** in your pipeline folder
— it opens the app directly, no terminal, no typing. For an actual
desktop icon: right-click that file → **Send to → Desktop (create
shortcut)**, then (optional) right-click the new shortcut → Properties
→ Change Icon, to give it its own look.

If double-clicking seems to do nothing, run
**`Launch_Manuscript_Engine_Debug.bat`** instead — it's identical but
keeps a console window open so you can see exactly what went wrong.

Both `.bat` files assume the venv is named `pipeline_env` and sits in
the same folder as `manuscript_engine.py`, exactly as set up above —
they run that venv's own Python directly, which uses its packages
without needing `activate` at all.

**Files you fill in once by hand** (all local-only, never uploaded or
committed — see `.gitignore`):

| File | What goes in it |
|---|---|
| `paper_pipeline_config.yaml` | your Unpaywall e-mail, folder paths, proxy hostname, etc. |
| `login_credentials.txt` | your university login, as `ID: ...` / `PW: ...` |
| `gemini_api_key.txt` | optional — a free Gemini API key, only if you want local AI answers |

**In the app:** open **Settings**. The "Pipeline settings" and
"Advanced" tabs edit `paper_pipeline_config.yaml` (your comments in the
file are preserved). "Login & API keys" edits the two credential files.
"Writing style rules" edits `style_rules.txt`.

**From a terminal:** just open these files in any text editor
(Notepad, VS Code, etc.) and edit them directly — they're plain text.

---

## 1. Get papers into your library

### 1a. Prepare your paper list

Put one or more `.xlsx` files with your papers (Title/DOI/Authors
columns) into the `publication_data/` folder (or whatever
`paths.excel_input_dir` is set to in the config). Every file in that
folder gets read and combined automatically — no need to merge them
yourself.

### 1b. Stage 1 — resolve DOIs and download open-access PDFs

Fast, no login needed. Reads your Excel files, fills in metadata
(year, authors, abstract, category), and downloads whatever's freely
available.

- **In the app:** sidebar → **Get Papers** → "Stage 1" card → **Run**.
- **From a terminal:**
  ```
  python doi_resolver.py
  ```

### 1c. Stage 2 — download the rest through your university proxy

Opens a real browser window. Signs in automatically using
`login_credentials.txt` when it can; if it ever can't, it prints a
message asking you to finish signing in by hand in that window.

- **In the app:** "Stage 2" card → **Run**. If it needs you to sign in
  by hand, a **"I've handled it — Continue"** button appears once
  you're signed in — click it.
- **From a terminal:**
  ```
  python proxy_download.py
  ```
  If it asks you to sign in, do so in the browser window it opened,
  then come back to the terminal and press Enter.

Papers that hit a bot-check or verification wall are skipped
automatically (never pause the batch) and logged to
`human_check_needed.xlsx` for you to look at afterward.

### 1d. Or run both stages in one command

- **In the app:** "Run both" card → **Run both**.
- **From a terminal:**
  ```
  python download_papers.py
  ```

### 1e. Import PDFs you already downloaded some other way

Copies them into `downloads/`, renames them to match the pipeline's
naming, and adds them to the tracking sheet — safe to re-run over a
folder you keep adding to (already-imported papers are skipped).

- **In the app:** "Import PDFs you already have" card → pick the
  folder → **Import from this folder**.
- **From a terminal:**
  ```
  python import_existing_pdfs.py
  ```
  (it will ask which folder to import from), or non-interactively:
  ```
  python import_existing_pdfs.py --folder "C:\path\to\pdfs" --yes
  ```

---

## 2. Build the search index

Do this after every download batch (Stage 1/2 or import). Extracts
text, figures, and table captions from every PDF so they become
searchable.

- **In the app:** **Get Papers** → "Build / update the search index"
  card → **Run**. Check "Full rebuild" only if you want to re-index
  everything from scratch (normally not needed).
- **From a terminal:**
  ```
  python build_index.py
  ```
  Full rebuild:
  ```
  python build_index.py --rebuild
  ```

---

## 3. Search & ask your library

### 3a. Instant lookup (no AI, always fast)

- **In the app:** **Search & Ask** → "Quick lookup" card:
  - **Search** — text search across every paper.
  - **Find figure** — describe a figure in words.
  - **Match image** — give it an image file, finds visually similar figures.
- **From a terminal:**
  ```
  python build_index.py --search moisture stability of tin perovskites
  python build_index.py --find-figure cross-section of device stack
  python build_index.py --match-figure my_image.png
  ```
  Add `--top 10` to any of these for more results (default 5).

### 3b. Ask a real question

`--pack` is the fast, recommended path on any machine: it doesn't run
local AI at all — it retrieves the evidence and writes a small file you
upload to Claude, which writes the actual answer with citations.

- **In the app:** **Search & Ask** → "Ask your library" card — type
  your question (a single question or a whole paragraph), leave
  "--pack" checked, optionally set "Save to", click **Ask**.
- **From a terminal:**
  ```
  python ask_library.py "what mechanisms explain Sn(II) oxidation in tin perovskites?" --pack
  ```
  To choose where it saves:
  ```
  python ask_library.py "your question" --pack --out my_folder/my_question.md
  ```
  Uncheck "--pack" (or drop `--pack` on the command line) to try
  answering locally instead, using Gemini/Ollama/llama-cpp if
  configured, or verbatim passages with `--no-ai`.

---

## 4. Plan and draft a manuscript

### 4a. Export a catalog of your library

A filterable overview (title/year/category/abstract per paper, plus
figure/table captions with `--full`) — upload this to Claude when
you're designing an outline or checking topic coverage.

- **In the app:** **Export & Draft** → "Export a library catalog" card
  — set Category/Years/Full as needed → **Export catalog**.
- **From a terminal:**
  ```
  python export_catalog.py
  python export_catalog.py --full --category solar-cell --since 2023
  python export_catalog.py --years 2020,2023-2025
  ```
  Choose where it saves: `--out my_folder/` or `--out my_catalog.md`.

### 4b. Write your outline

A plain `.txt` file, sections separated by a line of dashes (`----`).
See any of your existing `*_Outline*.txt` files as an example.

### 4c. Build a research pack for a section (or the whole outline)

Retrieves the evidence passages for every section, embeds your
`style_rules.txt`, and produces one file to upload to Claude for
drafting.

- **In the app:** **Export & Draft** → "Build a research pack for an
  outline" card — pick your outline file → **Build research pack**.
- **From a terminal:**
  ```
  python export_for_claude.py my_outline.txt
  python export_for_claude.py my_outline.txt --per-section 25 --style style_rules.txt
  ```
  Choose where it saves: `--out my_folder/` or `--out pack_section5.md`.

### 4d. Draft with Claude

Upload the research pack (and, if useful, the catalog) to Claude in
chat, and ask it to write the section(s) as a `.docx` — citing sources
as `[Entry N, p.X]`. Iterate section by section.

---

## 5. Finalize a finished manuscript

Once a draft is truly final, pull out exactly what it cites.

- **In the app:** **Finalize Manuscript** — pick your `.docx`,
  optionally set an output folder, then run each of the three tools.
- **From a terminal:**
  ```
  python extract_cited_references.py my_manuscript.docx
  python extract_cited_figures.py my_manuscript.docx
  python export_citation_library.py my_manuscript.docx
  ```
  All three default to `finalized/<manuscript name>/...` so different
  manuscripts never mix; `--out` overrides that if you want.

**What each produces:**
- `extract_cited_references.py` — every cited `[Entry N]` PDF, copied
  into one folder, plus `reference_manifest.csv`.
- `extract_cited_figures.py` — the source image/page for every cited
  figure/table box, organized as `Figure1/`, `Table1/`, ... plus
  `figure_table_manifest.csv`.
- `export_citation_library.py` — a `.ris` file to import into EndNote
  or Zotero (File → Import), and an `_insertion_checklist.csv` listing
  citations in the order they first appear, so you can work through the
  document top to bottom replacing `[Entry N]` with a real citation
  from the plugin.

---

## 6. Keep the workspace tidy

Older runs may have left generated files (`catalog_*.md`,
`research_pack_*.md`, answer files) sitting in the workspace root
instead of their organized folders. This sweeps them in — nothing is
ever deleted.

- **In the app:** **Home** → "Tidy the workspace" card → **Tidy now**.
- **From a terminal:**
  ```
  python tidy_workspace.py
  ```

See `PAPER_PIPELINE_README.md` for the full breakdown of which files
in your folder are tools, your data, disposable outputs, or safe-to-
delete caches.

---

## 7. Troubleshooting

**The app won't launch at all.** Everything above works from a plain
terminal — use the "From a terminal" command for whatever you were
trying to do, and come back to the app later.

**`ImportError: DLL load failed while importing QtCore`** (Windows,
common with Anaconda) — a DLL conflict between Anaconda's own files and
PySide6's, not a code bug. Two ways this shows up:

- Anaconda's `base` environment is the *only* one active (no venv yet):
  fix is the dedicated `pipeline_env` venv from section 0 above. If
  you'd rather fix `base` directly instead: `conda install -c
  conda-forge --override-channels pyside6` (slow — "Solving
  environment" over a large `base` env can take many minutes — and can
  hit `InvalidArchiveError`, fixed by `conda clean --packages
  --tarballs -y` then retrying).
- **You already have `pipeline_env` and still see this error, with a
  prompt like `(pipeline_env) (base) ...`** — `base` is still active
  *underneath* the venv, so its DLLs are still on `PATH` and conflict
  with the venv's own PySide6. Run `conda deactivate` (this only drops
  `base`; the venv stays active) and try again. Make it permanent with
  `conda config --set auto_activate_base false` so new terminals never
  auto-activate `base` in the first place. The `.bat` launchers do this
  automatically for you.

**A script says a module isn't installed.** Re-run:
```
pip install -r paper_pipeline_requirements.txt
```

**A script can't find its config / relative paths look wrong.** Every
script assumes you're running it *from inside the pipeline folder*
(the one with `paper_pipeline_config.yaml` in it). In the app, this is
handled for you (Settings → Scripts folder); from a terminal, `cd` into
that folder first.

**Where did my output file go?** By default, into the organized folder
named in the "Files" table of `PAPER_PIPELINE_README.md`
(`catalogs/`, `research_packs/`, `answers/`, `finalized/<name>/`). Add
`--out <folder or filename>` to any command to choose exactly where.

---

## 8. Full command reference (copy-paste cheat sheet)

```
# Setup
pip install -r paper_pipeline_requirements.txt
python -m playwright install chromium

# Get papers
python doi_resolver.py
python proxy_download.py
python download_papers.py
python import_existing_pdfs.py --folder "C:\path\to\pdfs" --yes

# Index
python build_index.py
python build_index.py --rebuild

# Search & ask
python build_index.py --search your search terms
python build_index.py --find-figure a description of the figure
python build_index.py --match-figure my_image.png
python ask_library.py "your question" --pack

# Plan & draft
python export_catalog.py --full --category solar-cell --since 2023
python export_for_claude.py my_outline.txt --per-section 20

# Finalize
python extract_cited_references.py my_manuscript.docx
python extract_cited_figures.py my_manuscript.docx
python export_citation_library.py my_manuscript.docx

# Tidy up
python tidy_workspace.py
```

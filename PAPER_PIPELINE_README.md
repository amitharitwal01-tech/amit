# Paper Download Pipeline

A two-stage script pipeline that takes a spreadsheet of papers (title,
authors, and optionally DOI) and downloads as many of them as possible:

- **Stage 1 (`doi_resolver.py`)** — resolves missing DOIs via Crossref,
  normalizes them (strips `https://doi.org/`-style prefixes), resolves
  each DOI's actual publisher hostname (e.g. a specific Wiley journal
  imprint's subdomain) via the public `doi.org` redirect, then checks
  [Unpaywall](https://unpaywall.org/) for a legal, free copy and
  downloads whatever it finds.
- **Stage 2 (`proxy_download.py`)** — for whatever's left, builds the
  institutional-proxy PDF URL directly from the publisher URL Stage 1
  resolved (deterministic — no clicking through pages to find a download
  button), logs in through your university's proxy/SSO with your own
  credentials, and downloads the paper. If nothing serves a raw PDF, it
  prints whatever article page it landed on directly to PDF (Chromium's
  native print-to-PDF, same as Ctrl+P → Save as PDF) rather than hunting
  for a download control — publisher UIs change constantly and are the
  least reliable thing to depend on.

Every paper's status is tracked in `download_tracking.csv`, which both
stages read and update, so you can stop and re-run either stage at any
time without redoing completed work.

## Setup

See `PAPER_PIPELINE_README.md`'s companion setup walkthrough for the full
click-by-click Windows instructions. Short version:

```
pip install -r paper_pipeline_requirements.txt
playwright install chromium
```

Then edit `paper_pipeline_config.yaml`:

- `unpaywall_email` — a real email address (required by Unpaywall's API).
- `paths.excel_input` — your Excel filename.
- `excel.*_column` — the column names in your sheet (title is required;
  DOI and authors are optional but improve resolution accuracy).
- `proxy.hostname_mangling_suffix` — if your institution's proxy rewrites
  hostnames (dots become dashes, then this suffix is appended — e.g.
  `pubs.acs.org` becomes `pubs-acs-org.bib-proxy.uhasselt.be`) rather than
  using a URL prefix, set it here. This is Stage 2's primary access route
  when set; it's built directly from each paper's DOI and Stage 1's
  resolved publisher hostname, no page-scraping involved.
- `libkey.library_id` — your institution's LibKey library ID, if it uses
  LibKey/LibKey Nomad for full-text links (look for `libkey.io/libraries/<id>/...`
  the next time you click a LibKey button, or find it at
  https://libkey.io/choose-library). Only used when
  `proxy.hostname_mangling_suffix` is blank.
- `login.*` — your institution's proxy login URL and, if the defaults
  don't work, the CSS selectors for the username/password/submit fields
  on that login page. Leave `login.proxy_login_url` blank to log in by
  hand in the browser window Stage 2 opens.
- `proxy.url_prefix` — only used as a fallback when
  `proxy.hostname_mangling_suffix` and `libkey.library_id` are both
  blank; needed only if your institution uses an EZproxy-style URL
  prefix rather than SSO/Shibboleth.

## Running

```
python doi_resolver.py
python proxy_download.py
```

Run them in that order. `proxy_download.py` only processes rows Stage 1
marked `needs_proxy`, and will ask for your login ID and password
(password entry is hidden — that's normal).

## What's in your folder — and what you can safely delete

Everything in the workspace falls into one of four groups.

**1. The tools (keep — this is the pipeline itself)**

| File | Purpose |
|---|---|
| `doi_resolver.py` | Stage 1 — resolve DOIs, fetch metadata, download open-access PDFs |
| `proxy_download.py` | Stage 2 — download the rest through the university proxy |
| `download_papers.py` | Runs Stage 1 then Stage 2 in one go |
| `import_existing_pdfs.py` | Bring PDFs you downloaded elsewhere into the library |
| `import_from_laptop.py` | Scan folders (e.g. OneDrive) for research-paper PDFs and import them, matching Supporting Information files to their main article — terminal only |
| `build_index.py` | Build/search the text + figure index of every PDF |
| `ask_library.py` | Ask the library a question (`--pack` for a Claude upload file) |
| `export_catalog.py` | Filterable library overview for outline planning |
| `export_for_claude.py` | Research pack: per-section evidence for drafting |
| `extract_cited_references.py` | After finalizing: copy every cited PDF out |
| `extract_cited_figures.py` | After finalizing: pull cited figure/table sources |
| `export_citation_library.py` | After finalizing: .ris export for EndNote/Zotero |
| `manuscript_engine.py` | The desktop app that drives all of the above |
| `Launch_Manuscript_Engine.bat` | Windows one-click launcher (no terminal, no typing) — see `HELP_GUIDE.md` §0 |
| `Launch_Manuscript_Engine_Debug.bat` | Same, but keeps the console open — use if the normal launcher seems to do nothing |

**2. Your data and settings (keep — irreplaceable or hand-written)**

| File | Purpose |
|---|---|
| `paper_pipeline_config.yaml` | All settings for every script |
| `paper_pipeline_requirements.txt` | Python package dependencies |
| `style_rules.txt` | Your writing-style rules, embedded into research packs |
| `HELP_GUIDE.md` | Full step-by-step walkthrough (app steps + equivalent terminal commands) — also viewable in the app's Help page |
| `login_credentials.txt`, `gemini_api_key.txt` | Local-only secrets (never share/commit) |
| `publication_data/` (or your input .xlsx files) | The paper lists you feed Stage 1 |
| `download_tracking.csv` / `.xlsx` | The master record of every paper |
| `human_check_needed.xlsx` | Papers that hit a verification wall — revisit by hand |
| `downloads/` | Your PDF library |
| `library_index/` | The search index (rebuildable, but slow — keep it) |
| Outline files (`*_Outline*.txt`, `PSC_review`, etc.) | Your manuscript outlines |

**3. Generated outputs (disposable — recreate any of them with one command)**

New runs now write these into tidy folders instead of the workspace root:

| Folder | What lands there | Made by |
|---|---|---|
| `catalogs/` | `catalog_<timestamp>.md` | `export_catalog.py` |
| `research_packs/` | `research_pack_<timestamp>.md` | `export_for_claude.py` |
| `answers/` | question packs and answers | `ask_library.py` |
| `finalized/<manuscript name>/` | cited PDFs, figures/tables, .ris library | the three finalize tools |

Every one of these also takes `--out` (folder and/or filename of your
choice), and the desktop app has a "Save to" field in each section —
leave it blank for the defaults above. Old copies you've already
uploaded or used can be deleted freely. `python tidy_workspace.py`
moves any strays from the workspace root into these folders.

**4. Caches and leftovers (safe to delete whenever)**

| Item | What it is |
|---|---|
| `__pycache__/`, `debug/` | Python/debug caches — recreated automatically |
| `browser_profile/` | Stage 2's saved login session — deleting just means signing in again |
| `pipeline_gui.py` | The old trial GUI, replaced by `manuscript_engine.py` |

## Tracking CSV columns

`Title, Authors, DOI, Status, PDF_Path, Source_URL, Publisher_URL, Notes, Last_Updated`

`Publisher_URL` is the exact article URL (e.g.
`https://advanced.onlinelibrary.wiley.com/doi/10.1002/adfm.75159`) that
`https://doi.org/<doi>` redirected to — resolved by Stage 1 with a plain
HTTP request (no login involved; only the *content* behind that URL is
gated, not the redirect itself). Stage 2 uses its domain to build the
proxy PDF URL directly. **If you're upgrading from an older run, re-run
`python doi_resolver.py` once** so this column gets populated for
existing rows — Stage 2 falls back to a rougher DOI-prefix guess when
it's blank.

Status values: `downloaded`, `downloaded_via_proxy`, `needs_proxy`,
`manual_check_needed`, `no_doi_found`.

If a paper ends up as `manual_check_needed`, the `Notes` column names the
exact URL the script tried and why it gave up — open that URL yourself
once logged in to see what's different about that publisher's page.

## A note on legitimate use

This pipeline only fetches papers you already have a legal right to read:
open-access copies via Unpaywall, or subscription content via your own
university's paid access and your own login. It doesn't bypass paywalls,
share credentials, or download anything in bulk beyond what you'd
otherwise click through manually. Respect your institution's and each
publisher's terms of use, and don't redistribute downloaded PDFs.

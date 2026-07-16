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
  institutional-proxy PDF URL directly from the publisher hostname Stage 1
  resolved (deterministic — no clicking through pages to find a download
  button), tries LibKey as a fallback, logs in through your university's
  proxy/SSO with your own credentials, and downloads the paper.

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
  https://libkey.io/choose-library). Used as a fallback if the proxy URL
  above doesn't pan out for a given paper.
- `login.*` — your institution's proxy login URL and, if the defaults
  don't work, the CSS selectors for the username/password/submit fields
  on that login page. Leave `login.proxy_login_url` blank to log in by
  hand in the browser window Stage 2 opens (this is the recommended
  setting if you're using LibKey — you'll land on a real article/login
  page rather than a blank tab).
- `proxy.url_prefix` — only used as a fallback when `libkey.library_id`
  is blank or a paper has no DOI; needed only if your institution uses
  an EZproxy-style URL prefix rather than SSO/Shibboleth.

## Running

```
python doi_resolver.py
python proxy_download.py
```

Run them in that order. `proxy_download.py` only processes rows Stage 1
marked `needs_proxy`, and will ask for your login ID and password
(password entry is hidden — that's normal).

## Files

| File | Purpose |
|---|---|
| `doi_resolver.py` | Stage 1 script |
| `proxy_download.py` | Stage 2 script |
| `paper_pipeline_config.yaml` | All settings for both stages |
| `paper_pipeline_requirements.txt` | Python package dependencies |
| `download_tracking.csv` | Generated — running status of every paper |
| `downloads/` | Generated — where PDFs land |

## Tracking CSV columns

`Title, Authors, DOI, Status, PDF_Path, Source_URL, Publisher_Host, Notes, Last_Updated`

`Publisher_Host` is the actual domain (e.g. `advanced.onlinelibrary.wiley.com`)
that `https://doi.org/<doi>` redirected to — resolved by Stage 1, and what
Stage 2 uses to build the proxy PDF URL directly. **If you're upgrading
from an older run, re-run `python doi_resolver.py` once** so this column
gets populated for existing rows — Stage 2 falls back to a rougher
DOI-prefix guess when it's blank.

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

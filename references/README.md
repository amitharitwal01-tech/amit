# Reference reports — drop the six exemplars here

These are the benchmark reports whose craft the tool's own reports are meant to
match. They are the source material for `report_rules.txt` (the report-design
standard), the same way a set of writing guides was the source material for
`style_rules.txt`.

## Why they live in the repo rather than being fetched

The session that writes the rules cannot reach any of these six hosts —
`slidemodel.com`, `web-assets.bcg.com`, `globalrenewablesalliance.org`, and
`ert.eu` are all refused by the network egress policy. Committing the files is
also the *better* option: rules about colour coding, exhibit layout, grid, and
typography can only be derived from the rendered pages. A text scrape would
throw away most of what these documents are being studied for.

## What to put here

Download each source and commit it under the filename below. Keep the
numbering — it is the order the sources are studied in, and the rules cite
sources by these names.

| # | Filename | Source |
|---|---|---|
| 1 | `01_slidemodel_consulting-report-guide.pdf` | https://slidemodel.com/consulting-report-how-to-write-and-present-one/ |
| 2 | `02_bcg_refining-oversight-ai-driven-world.pdf` | https://web-assets.bcg.com/99/93/6ed97aee4c94989306b346f6a005/refining-oversight-for-a-volatile-ai-driven-world.pdf |
| 3 | `03_bcg_europe-deep-tech-opportunity.pdf` | https://web-assets.bcg.com/47/1a/b995bbe3487299578ea65ae6254b/unlocking-europes-8-trillion-deep-tech-opportunity.pdf |
| 4 | `04_gra_financing-3x-renewables-2030.pdf` | https://globalrenewablesalliance.org/wp-content/uploads/2025/11/GRA_Financing-3xRenewables-by-2030.pdf |
| 5 | `05_ert_strengthening-europes-energy-infrastructure.pdf` | https://ert.eu/wp-content/uploads/2024/04/ERT-Strengthening-Europes-energy-infrastructure_March-2024.pdf |
| 6 | `06_bcg_2020-annual-sustainability-report.pdf` | https://web-assets.bcg.com/40/84/80b567044409b74c32806275a3c1/bcg-2020-annual-sustainability-report-apr-2021-r2.pdf |

Number 1 is a web article, not a PDF. Open it in a browser and use
Print → Save as PDF, so the figures and callouts survive.

Adding more exemplars later is welcome — continue the numbering and add a row.
A report you personally admire is worth more than a famous one you do not.

## What gets extracted from them

Each document is read page by page for:

- **Architecture** — section order, what the opening pages do, where the
  argument's conclusion sits relative to its evidence, how long each part runs.
- **Exhibits** — which chart form carries which kind of claim, how exhibits are
  numbered, titled, captioned, sourced, and footnoted; the ratio of exhibits to
  pages; what is never drawn as a chart.
- **Tables** — when a table beats a chart, column ordering, how values are
  aligned and rounded, how a table is made readable at a glance.
- **Colour and type** — the working palette and what each colour *means*, how
  emphasis is signalled, the heading hierarchy, the page grid, use of white
  space, callouts, pull quotes, and sidebars.
- **Language** — how headlines and exhibit titles are written (assertions
  versus labels), summary style, and how numbers are framed in prose.

Nothing is committed to the standard from memory or from generic advice about
consulting decks: every rule in `report_rules.txt` is traceable to something
observed in one of these files, and cites the source number and page.

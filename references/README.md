# Reference reports — the exemplars behind report_rules.txt

These are the benchmark reports whose craft the tool's own reports are meant to
match. They are the source material for `report_rules.txt` (the report-design
standard), the same way a set of writing guides was the source material for
`style_rules.txt`.

Every rule in sections 2-8 of `report_rules.txt` cites the document number and
page it was observed on, e.g. `[03 p11]`. Nothing in that file comes from
memory or from generic advice about consulting decks — if a rule is not
traceable to a page here, it does not belong there.

## The set

| # | File | Document | Pages | Shape |
|---|---|---|---|---|
| 01 | `01_bcg_sustainability-imperative-emerging-markets.pdf` | BCG, *The Sustainability Imperative in Emerging Markets*, Mar 2023 | 16 | Portrait report — prose-led, exhibits as designed colour blocks |
| 02 | `02_bcg_guide-to-cost-and-growth.pdf` | BCG, *Guide to Cost and Growth* (Executive Perspectives), Jan 2025 | 24 | Landscape deck — one message per page, action titles |
| 03 | `03_bcg-baywa_agri-pv-regenerative-agriculture.pdf` | BCG + BayWa r.e., *How Agri-PV can Boost the Transition to Regenerative Agriculture in Europe*, Nov 2024 | 28 | Portrait report — numbered exhibits, schematics, numeric appendix |
| 04 | `04_bcg_investor-perspectives-q1-q2-2025.pdf` | BCG, *Investor Perspectives Series Q1 & Q2 2025* | 30 | Landscape survey deck — maximum data density |

Between them these cover both shapes a report can take (portrait prose report,
landscape deck) and the full exhibit range: data charts, schematics,
taxonomies, comparison matrices, trajectory charts with uncertainty bands, and
audit-grade numeric tables.

## Why they live in the repo

Rules about colour coding, exhibit layout, grid, and typography can only be
derived from the rendered pages — a text scrape throws away most of what these
documents are being studied for. Keeping the files here means every citation in
`report_rules.txt` stays checkable, and the standard can be extended or
challenged later against the same evidence.

The session that wrote the rules also could not reach any of these publishers:
`web-assets.bcg.com` and the other source hosts are refused by the network
egress policy, so fetching them at read time is not an option.

## Adding more

Continue the numbering and add a row. A report you personally admire is worth
more than a famous one you do not — the point is to widen the range of forms
the rules are drawn from, especially:

- a scientific review or journal article, to ground the rules in the register
  the tool actually writes in;
- anything using an exhibit form not already in the set.

After adding one, re-read it page by page and fold what it shows into
`report_rules.txt` with citations. Rules already there should be revised only
against evidence, not preference.

## What was extracted

Each document was read for architecture (section order, what the opening pages
do, where conclusions sit relative to evidence), exhibits (which form carries
which claim, numbering, titling, sourcing, footnoting, density), tables (when a
table beats a chart, grouping, units, derivations), colour and type (what each
colour means, emphasis, hierarchy, grid), and language (how headlines and
exhibit titles are written, how numbers are framed).

The strongest single finding, and the biggest gap in the tool's current output,
is section 7 of the standard: the habits that make a report credible — marking
which evidence base each page rests on, giving the instrument and not only the
result, stating n including exceptions, showing every number against a
baseline, ranges instead of false precision, assumptions where they bite, and
marking the edge of scope inside the exhibit itself.

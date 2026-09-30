# Portfolio open roles

What Miton's portfolio is hiring, refreshed every Monday, at
`/admin/portfolio`. It replaces the hand-built claude.ai artifact "Miton
Portfolio Hiring", which was accurate but cost an afternoon a week and left no
history behind.

It lives in Miton Talent and not in Alister, by the rule in
[miton-layer-architecture.md](miton-layer-architecture.md): **which companies
are "the portfolio" is Miton's own list**, and so is the decision to watch it.
Alister holds facts about the whole market; this is Miton looking at 44 named
companies. Nothing here reads from or writes to Alister.

## What a reader gets

* **Open roles now**, with function, country, city and a link to the posting.
* **Who is hiring**, a tile per company with the technical share in the bar.
* **Changes this week**: what opened, what closed, who started hiring, who went
  quiet.
* **Read before you trust a zero**: which pages were actually read this run,
  which refused us, which were read by the model rather than an API.

## How a run works

`scripts/scrape_portfolio.py` (cron, Mondays 06:10) or the "Run the scrape now"
button on the page.

1. **Read** each company's careers page. An ATS with a public board is read
   through its API (Recruitee, Ashby, SmartRecruiters, Personio, Recruitis,
   StartupJobs, Greenhouse, Lever, Workable). Anything else is fetched as HTML
   and read by Claude, which is told to list only what is open and to treat an
   open-application form as not a role. If an ATS board 404s, the page itself is
   read instead and the run says so.
2. **Collapse** copies of one requisition. Two rows are one role when the title
   normalises to the same fingerprint *and* they share a posting URL: a board
   that repeats one requisition per city repeats its URL, while two shops hiring
   the same job have two postings. Every city is kept in the role's locations.
   Cross-language copies of one requisition (a German and a Czech version of the
   same job) only merge when a company's `config.aliases` says they are the same.
3. **Classify**: function bucket and country from keyword rules; the model is
   asked, in one batched call, only about titles the rules cannot place. Every
   role records which of the two decided it (`fn_source`), so a wrong bucket is
   findable. Technical = Engineering & AI, Data & Analytics, Product & Design.
4. **Diff**: a role seen again keeps its row and its `first_seen_at`; a role
   that is gone gets `closed_at`. That is the whole "changes this week" panel,
   with no snapshot table.

### The rule that matters

**A page that could not be read closes nothing.** A 403, a timeout, a parse
break or a missing model key leaves last week's roles standing, the company is
marked unread on the page, and its number is labelled as last known. A failed
fetch must never be able to look like a company that stopped hiring, because
that is a conclusion someone would act on.

## Tables

| Table | One row is |
|---|---|
| `portfolio_companies` | a company in the portfolio: careers URL, adapter, category, the standing caveat |
| `portfolio_runs` | one weekly run and its totals |
| `portfolio_company_runs` | what one run saw at one company: status, adapter, HTTP status, count, error |
| `portfolio_roles` | one requisition, with `first_seen` / `last_seen` / `closed` |

## Adding or fixing a company

The list is seeded from `backend/talent/portfolio/seed_companies.json` and
upserted on every deploy, so an edit there plus a release is enough:

```bash
# after editing the JSON
backend/.venv/bin/python scripts/scrape_portfolio.py --seed
```

Per-company knobs live in `config`:

| Key | Effect |
|---|---|
| `board` | the ATS board name when the URL does not carry it (`{"board": "rohlik"}`) |
| `aliases` | merge two fingerprints into one requisition (cross-language copies) |
| `fallback_html` | `false` turns off "if the ATS board fails, read the page" |
| `skip` | do not scrape this company at all |

## Working on it

```bash
cd backend && .venv/bin/python -m pytest -q tests/test_portfolio.py
# read every page, write nothing
scripts/scrape_portfolio.py --dry-run --only rohlik-group,knihobot
# one company, for real
scripts/scrape_portfolio.py --only aim --actor marketa
# no model calls at all (ATS boards only)
scripts/scrape_portfolio.py --no-llm
```

`PORTFOLIO_MODEL` overrides the model used for reading pages and classifying
titles; `PORTFOLIO_CLASSIFY_LLM=0` turns the classifier's model call off and
leaves unknown titles in "Other".

## Known limits

* Roles that never reach a careers page are invisible here. A LinkedIn-only
  posting, a Discord hire or a founder's DM will not appear.
* Eight portfolio companies were merged into the gastro holding Piano Group in
  early 2026 and still show separately, because miton.cz lists them
  separately; they carry `group_name = "Piano Group"`.
* A company's own board can be narrower than the ATS behind it. Rohlik's Ashby
  board carries every warehouse posting, so its count is higher than the
  hand-collapsed 57 the artifact reported in September 2026. The number here is
  what the board publishes, per posting.
* The model reads a page as text. It cannot see a board that renders only in a
  browser; such a page is reported as blocked rather than as zero.

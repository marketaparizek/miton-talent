"""Portfolio open roles: the weekly scrape of every portfolio company's careers page.

Moved in from the claude.ai artifact "Miton Portfolio Hiring" that was built by
hand every week. The pieces:

  collapse.py  normalise a title into a requisition fingerprint, so one job
               posted in four cities is one role and not four
  classify.py  title/team/location -> function bucket, technical flag, country
  adapters.py  read a careers page: one adapter per ATS, plus an HTML fallback
  extract.py   the HTML fallback's reader (Claude turns a careers page into rows)
  scrape.py    one run: every company, upsert what is open, close what is gone
  store.py     the reads the page and the JSON need, including the weekly diff
  seed.py      the portfolio itself and the 14 Sep 2026 baseline from the artifact
  view.py      /admin/portfolio (the dashboard) and /admin/portfolio.json
"""

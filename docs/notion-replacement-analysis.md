# Leaving Notion: the recruiting data and where it goes

Status: analysis, 2026-09-29. Nothing implemented yet.

Miton is cancelling Notion. Notion is not only the store for talent chat
submissions. It is the whole recruiting back office: the applicant inbox, the
candidate database, per-search tables shared with founders, and the outreach
log. Four Claude skills and one chat backend write into it.

This document inventories what lives there, what each piece is for, and
proposes one target system instead of five separate replacements. The
recommendation is: **Alister becomes the recruiting back office.** One
database, one admin, one API that the chat backend and the skills talk to.
Google Sheets stays as a one-way mirror for people without an Alister login.

The earlier version of this document covered only the talent chat. That
analysis is folded into section 4.1.

---

## 1. Inventory: what is in Notion today

Everything sits under the page "HR v Mitonu". Counts are from the live
workspace on 2026-09-29.

| Notion object | What it is | Rows | Written by | Read by |
|---|---|---|---|---|
| **Hledáme chytré lidi** (database) | Applicant inbox. Every inbound application since Dec 2023: Typeform, StartupJobs, now talent chat. Status workflow, AI summary, scoring fields. | 1 477 | talent chat `/submit`, formerly Typeform and Bardeen automation | `candidate-triage` skill, Markéta by hand |
| **Full databáze kandidátů** (database) | The long-term candidate pool. Everyone Miton has sourced, interviewed or placed since Jan 2023, across roles and portfolio companies. Has `Alister profil` URL, `Shared with founder`, `Stage`, `Interviewed with`, `Sourced for`. | 1 609 | `notion-search-table` skill (mirror of every shortlist), Markéta by hand | Markéta, Míša, "HR Dashboardy" |
| **Sdílení searchů** (page tree) | One small database per search, grouped by month, 2023 to 2026. Roughly 80 of them. Each has the same shape: Name, LinkedIn/CV, current company and position, Sourced for, Status, Interviewed with, comment columns per founder ("Matův koment", "Michal"). This is what founders open. | ~80 databases, 5 to 30 rows each | `notion-search-table` skill | founders, portfolio hiring managers, Markéta |
| **Outreach table** (database) | Log of first-contact messages: channel, mode (pitch / referral ask), sent date, follow-up due, outreach status. Started July 2026. | 38 (22 sent, 16 draft) | `candidate-outreach` skill, Markéta by hand | Markéta ("To follow up" view) |
| **HR Dashboardy** | Placements and "Evidence propojení" (intro tracking), rolled up from the tables above. | small | Markéta by hand | Miton partners |
| Role views under "Miton databáze kandidátů" | ~30 linked views of the Full database filtered by Position (CMO, CTO, Backend developer, …). | views, no data | | Markéta |

Talent chat rows in the inbox: 29 since July 2026 (20 unprocessed, 9
rejected). Everything else in the inbox is older Typeform and StartupJobs
history, mostly rejected.

Not in scope, but also dies with Notion and needs a plain export: the
"Talent leady" newsletter archive, the HR content database, ESOP and "HR
funkce" pages, Alister meeting notes and roadmap pages.

### 1.1 What depends on it (code and skills)

| Consumer | Where | Notion touchpoint |
|---|---|---|
| Talent chat backend | `~/miton-talent-chat/backend/app.py`, ~300 lines | creates inbox rows, PATCHes scoring, `/diag` probe |
| `candidate-triage` skill | claude.ai skill | reads inbox rows with Status = Unprocessed, writes Status + Gmail drafts |
| `notion-search-table` skill | claude.ai skill | creates a new search database under "Sdílení searchů", mirrors the same people into the Full database |
| `candidate-outreach` skill | claude.ai skill (also `~/Downloads/candidate-outreach/`) | writes Outreach table rows, reads company context; follow-up tracking "lives with Markéta (Notion)" |
| Alister "Draft outreach" widget button | `github-sourcing` branch `feature/widget-outreach` | deliberately does **not** write to Notion (already decided) |
| Founder Momentum List board | claude.ai artifact | already moved off Notion on 2026-09-17 |

The decision on 17 September ("Notion se ukončuje, Outreach tabulku
nezapojovat") already set the direction. This document finishes it.

---

## 2. What the data actually is, once you strip the Notion shapes

Four Notion databases, but only two kinds of thing:

1. **A person Miton is in contact with about a role.** Applicant, sourced
   candidate, referral. Has contact details, a LinkedIn, notes, and a
   history of stages. This is one entity whether they came in through the
   chat or through an Alister search. Today it is split across the inbox and
   the Full database, and duplicated into every per-search table.
2. **A search: a role at a company, with a list of people and what happened
   to each.** Today one Notion database per search. Founders comment in it.
   Placements and dashboards roll up from it.

Outreach is an event on the person (message sent, replied). Triage is a
stage change on the person. Scoring is an attribute of the person.

Alister already has half of this: `person_entities` (the sourced people),
`shortlists` + `shortlist_items` (a search with people, rating liked /
disliked / maybe, notes), `users`, admin auth, Resend e-mail, PDF and HTML
shortlist export through the MCP. What it lacks: people who are **not** in
Alister (applicants, non-tech roles, referrals), a stage pipeline, founder
access without a full Alister login, and the outreach log.

The watchlist migration in September solved the same "person not in
Alister" problem by keying on the LinkedIn handle with an optional
`person_entity_id`. The same pattern works here.

---

## 3. Options for the whole back office

### A. Google Sheets for everything

One spreadsheet per concern (inbox, candidates, outreach), one tab per
search. The chat appends rows via a service account. Skills write via the
Sheets API. Founders get a link to their tab.

Works for the small tables. Breaks on the two things that matter: the
candidate pool (1 600 rows with multi-selects, stages, relations to
searches, notes, a link to Alister) and the per-search tables with founder
comments. A sheet has no keys, no history, no validation, and personal data
of 1 600 people one "anyone with link" click away. The Alister admin would
then be a viewer of someone else's Google API. Rejected as the system of
record. Kept as the mirror.

### B. Alister as the back office, Sheets as the mirror (recommended)

Two new tables in the Alister Postgres, a new admin tab, one ingest endpoint
for the chat, and the skills switched from Notion MCP to Alister MCP tools.
Founders see searches through a shareable Alister page, or through the
Sheet mirror if they refuse to log in.

Everything the Notion set-up did is expressible in Alister with things
Alister already has. Details in section 4.

### C. Buy an ATS (Recruitee, Teamtailor, Ashby, …)

Solves the pipeline and founder-sharing problems out of the box, and GDPR
retention is a checkbox. But it does not know Alister's 120K profiles, the
scoring, the market data, or the MCP workflow, so every Alister search
would need a second manual copy into the ATS. Miton is a VC recruiting for
portfolio companies, not an employer running one funnel. The ATS would be
a fourth tool, and the reason Notion was tolerable was that it was one
tool. Not recommended now. Worth revisiting only if the recruiting team
grows beyond Markéta and Míša.

### D. Move nothing, export to files, use Alister shortlists as they are

Cheapest. Loses the applicant inbox (the chat would only e-mail), loses
founder comments, loses the candidate history. Not acceptable while the
chat is live on miton.cz.

---

## 4. Target design (option B)

Two tables carry everything. The names are placeholders.

### 4.1 `candidates`: every person Miton is in contact with

Replaces the inbox **and** the Full database. One row per person, keyed on
the LinkedIn handle when known, otherwise on e-mail.

| Group | Columns |
|---|---|
| identity | `id`, `full_name`, `email`, `linkedin_url`, `linkedin_identifier` (unique, nullable), `person_entity_id` (FK, nullable, ON DELETE SET NULL, re-resolved like the watchlist) |
| origin | `source` (talent_chat / startupjobs / typeform / sourcing / referral / alister_search), `first_seen_at`, `submission_uid` (uuid from the chat, idempotency) |
| what they told us | `area[]`, `level[]`, `work_mode[]`, `search_status[]` (the existing `ALLOWED_*` lists), `note`, `cv_filename`, `lang` |
| what we know | `current_company`, `current_position`, `summary` (AI), `transcript` (jsonb), `notes` (recruiter), `hiring_review` |
| consent | `consent`, `consent_text`, `consent_at` |
| evaluation | `score`, `company_tier`, `education_tier`, `fit_areas[]`, `recommendation`, `reasoning`, `scored_at` |
| pipeline | `stage` (Applied / Sourced / Contacted / Up for a call / Interviewing / Offer / Hired / Rejected / Rejected after interview / Not interested, verbatim from the Full database), `owner` (Miton / Miton C), `assigned_to` (FK users), `interviewed_with[]` |
| housekeeping | `created_at`, `updated_at`, `deleted_at` |

The inbox Status (Unprocessed / Contacted / Placed in database / Rejected)
collapses into `stage`: Unprocessed → Applied, Contacted → Contacted,
Placed in database → Sourced, Rejected → Rejected.

Optional `candidate_events` table (stage changes, outreach sent, reply,
intro booked, with who and when). This replaces the Outreach table and
gives the "To follow up" view for free: events of type `outreach_sent`
older than 4 days with no later `reply` event. Recommended in phase 1
because outreach is the newest workflow and the one most likely to grow.

### 4.2 `searches` + `search_candidates`: a role, and who was considered

Replaces the ~80 per-search Notion databases.

`searches`: `id`, `company` (FK companies, nullable), `company_name`,
`role`, `opened_at`, `closed_at`, `status`, `shortlist_id` (FK shortlists,
nullable, when it came out of an Alister search), `share_token`, `notes`.

`search_candidates`: `search_id`, `candidate_id`, `position`, `founder_rating`
(liked / disliked / maybe, same vocabulary as shortlist items),
`founder_comment`, `miton_comment`, `outcome` (Sourced / Contacted /
Interviewing / Hired / Hired with Miton lead / Rejected / Newsletter
potential), `added_at`.

Placements dashboard = `search_candidates WHERE outcome LIKE 'Hired%'`
grouped by month. "Počet searchů/měsíc" = `searches` grouped by
`opened_at`. Both become admin cards, no hand-maintained tables.

### 4.3 How each workflow runs afterwards

**Talent chat.** `/submit` POSTs one JSON body to
`POST /inbound/candidates` on the Alister API with `X-Inbound-Token`. Upsert
on `submission_uid`. Scoring PATCHes `/inbound/candidates/{uid}/score`. The
e-mail and the local JSONL backup stay. About 200 lines of Notion code in
`app.py` go away; the `ALLOWED_*` lists stay as the shared vocabulary.

**Triage.** The skill calls Alister MCP tools instead of Notion:
`list_candidates(stage="Applied")`, `set_candidate_stage`, and keeps writing
Gmail drafts. Same twice-a-week rhythm, same output. The admin tab shows the
same queue for doing it by hand.

**Search tables for founders.** The skill calls `create_search` with the
shortlist id or a manual list, which creates the search, adds the
candidates (creating `candidates` rows for people not yet there), and
returns a share link. Founders open `alisterai.com/s/<token>`: a read-mostly
page with the candidate cards, thumbs up / down, and a comment box. No login,
token in the URL, revocable per search. This is the "Palec nahoru nebo dolů,
poznámky, možnost kandidáta přidat, odebrat" from the 30 July meeting notes,
which was already on the Alister roadmap.

**Outreach.** The skill writes one `candidate_events` row of type
`outreach_drafted` per draft, and the admin (or the skill, when Markéta says
"sent") flips it to `outreach_sent`. The Alister "Draft outreach" widget
button can write the same event, so both paths converge. Follow-up queue is
a filter in the admin tab.

**Dashboards.** Two cards on the admin tab: placements per month, searches
per month. The "Evidence propojení" intro log becomes events of type `intro`.

### 4.4 Admin surface

One new admin tab, "Recruiting", with three sub-views (pill tabs, per the
Dune admin brief):

- **Inbox**: candidates with stage Applied, newest first. Detail drawer:
  summary, note, contact, transcript, evaluation, stage select, notes,
  delete. Bulk "reject with template" later.
- **Candidates**: the full pool, filters on stage, area, position, company,
  owner, free text. Link to the Alister profile when `person_entity_id` is
  set.
- **Searches**: list with company, role, opened, count, hired. Detail: the
  candidate list with founder ratings and comments, "copy share link",
  "add from shortlist", "export CSV".

Built from the same constants and classes as `watchlist.tsx`, no raster.
Gated by `get_admin_user_required`. Founders never see the admin; they see
`/s/<token>`.

### 4.5 Google Sheet mirror

One spreadsheet, shared to the miton.cz domain, three tabs: Candidates,
Searches, Outreach. Alister rewrites each tab from the database nightly and
after any admin write (or just nightly, if that proves enough). Flat columns,
no transcripts, a link back to the admin record. One-way. Edits in the sheet
are overwritten. If nobody opens it after a month, delete it and keep the
CSV export.

Implementation: a service account with the Sheets API, key in the Alister
`.env`, `gspread` or plain REST. Half a day.

### 4.6 Ingest and MCP surface

Alister API:

- `POST /inbound/candidates`, `PATCH /inbound/candidates/{uid}/score`:
  shared token, rate limited, used only by the chat.
- `/admin/recruiting/...`: candidates, searches, events, CSV export, admin
  session required.
- `GET /s/{token}`, `POST /s/{token}/rating`, `POST /s/{token}/comment`:
  founder page, token only.

Alister MCP (the server Claude already talks to): `list_candidates`,
`get_candidate`, `set_candidate_stage`, `add_candidate_event`,
`create_search`, `add_to_search`. These are what the three skills call
instead of the Notion MCP. The skills' prose barely changes; only the tool
names and the table shapes do.

---

## 5. Migration: what to export and how it maps

Export **everything** first, whatever happens next. Notion's own workspace
export (Markdown + CSV, with files) plus an API dump to JSONL for the four
databases, both kept in `~/miton-notion-export/` and in Google Drive. The
API dump matters because the CSV export loses page bodies (transcripts,
evaluations) and relation ids.

| Notion | Target | Notes |
|---|---|---|
| Hledáme chytré lidi (1 477) | `candidates` with `source` per the Source column (empty → typeform for rows before 2026, startupjobs where Inzerát says so) | transcripts from page bodies for the 29 chat rows; CV files from the `CV` property only for non-rejected rows |
| Full databáze kandidátů (1 609) | `candidates` (merge with the above on LinkedIn handle, then e-mail, then exact name + company) | expect a few hundred overlaps; keep both `Poznámky` and `Notes`; `Alister profil` URL → `person_entity_id` |
| ~80 search databases | `searches` (one per database, company and role parsed from the title, month from the parent page) + `search_candidates` | founder comment columns → `founder_comment`; per-search Status → `outcome` |
| Outreach table (38) | `candidate_events` type outreach_drafted / outreach_sent | match to `candidates` on LinkedIn or e-mail |
| HR Dashboardy | derived, nothing to import | verify the placement count matches the old dashboard after import |

The merge step is the only part that needs Markéta's eyes: a CSV of
"probable duplicates" to confirm before the import commits.

---

## 6. Cut-over plan

1. **Now**: full Notion export (workspace export + API dump). Nothing else
   depends on this being perfect, so do it first and repeat it the day
   before cancellation.
2. Alister `dev`: migration, models, queries, API, MCP tools. Import the
   export into `alister_dev`. Review the duplicate CSV.
3. Admin tab on dev. Founder share page on dev.
4. Talent chat on Railway: dual delivery, Notion and Alister, for a week.
5. Switch the three skills to the Alister MCP tools. Run one triage and one
   search table end to end on dev.
6. Promote to prod. Re-import from a fresh export. Point the chat at prod.
   Drop Notion delivery from the chat.
7. Sheet mirror. Nightly job.
8. Cancel Notion.

Rough effort in Claude Code sessions: schema + API + MCP tools one day,
admin tab one day, founder share page half a day, import scripts and the
duplicate review half a day, chat switch two hours, skills update two hours,
Sheet mirror half a day. Roughly four working days spread over two or three
weeks because of the dual-run and the review steps.

---

## 7. Decisions needed before building

1. **One candidate pool or two.** The design merges the applicant inbox and
   the Full database into one `candidates` table with a `source` column.
   The alternative is two tables, which keeps the Notion split and doubles
   the admin work. Recommendation: one.
2. **Founder access.** Token link per search (recommended, no accounts to
   manage) versus Alister user accounts for founders (more control, more
   support). The Sheet mirror is the fallback either way.
3. **CV files.** Keep them only in the recruiter e-mail (as today), or
   store them in Alister so the admin can open them. Recommendation: e-mail
   only in phase 1, plus `cv_filename`. Revisit after a month of use.
4. **Retention.** A number of months after last stage change, written into
   the still-pending privacy policy, enforced by a nightly delete. 1 257
   rejected applicants from 2023 to 2026 should not be imported at all
   unless there is a reason to keep them.
5. **Who else edits.** If Míša or founders need to edit candidate notes,
   they need Alister accounts (role `user` is enough with a small
   permission tweak). If only Markéta edits, nothing changes.
6. **Old search tables.** Import all ~80 (history and dashboards stay
   exact) or only 2025 to 2026 (less merge noise). Recommendation: all,
   since the import is scripted and the dashboards want the history.

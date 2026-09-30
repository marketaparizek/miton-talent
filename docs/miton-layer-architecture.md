# The Miton layer: what belongs to Alister, what belongs to Miton

Status: architecture proposal, 2026-09-29. Companion to
`notion-replacement-analysis.md`, which inventories the Notion data. This
note answers a different question: **where should Miton-specific things
live so that Alister stays one understandable product.**

## 1. The problem

Alister's database has 72 tables in five clean bands (source, identity,
canonical person, derived, reference). That is complex but legible: every
table answers "what do we know about the Czech tech market". The band that
is starting to blur is APPLICATION. It holds product tables (users,
shortlists, activity) and, since September, `watchlist_people` and
`watchlist_events`, which carry columns like `miton_relation` and
`segment`. Those are not facts about the market. They are Miton's
relationships.

The Notion replacement would add candidates, searches, founder comments
and outreach to the same band. Then "portfolio hiring" and the founder
momentum list would follow. Within a year the application band would be
half Alister product, half Miton back office, with no line between them.
That is the mess to avoid.

## 2. The rule that draws the line

**Alister holds facts about the market. Anyone could be a customer for
them. Miton holds Miton's relationships and decisions.**

| Question | Answer | Lives in |
|---|---|---|
| Who is this person, where did they work, what did they build | fact about the market | Alister |
| Is this company hiring, what roles, since when | fact about the market | Alister |
| A person's LinkedIn changed this week | fact about the market | Alister (the detector) |
| Which 300 people Miton watches and why | Miton's relationship | Miton |
| Who applied to Miton through the chat, what stage they are in | Miton's relationship | Miton |
| Which candidates were shown to which founder, and what the founder said | Miton's decision | Miton |
| Who Markéta messaged and when to follow up | Miton's decision | Miton |
| Which companies are "the portfolio" | Miton's list | Miton |
| Open roles across the portfolio this week | Alister facts filtered by Miton's list | Miton view over Alister data |

Applied to what exists today: the watchlist **mechanism** (scrape a list of
LinkedIn handles weekly, diff, emit events) is Alister. The **list** and
the human columns (`miton_relation`, `segment`, `manual_*`, the recorded
outcome) are Miton. The job postings **collector** is Alister. The
**portfolio** filter and the Monday Slack digest are Miton.

## 3. Three ways to separate, and which one

### 3.1 A namespace inside the Alister database

A Postgres schema `miton.*` in the same database, own migrations folder,
own Python package `src/miton/`, own API prefix `/miton/`, own admin
section. Cheapest. It gives a visible boundary in the schema diagram and
in the code tree, and nothing else changes: one deploy, one auth, one
admin shell.

The weakness: the boundary is a convention. Nothing stops a foreign key
from `miton.candidates` to `person_entities`, and once one exists the
layer cannot be lifted out without a migration. It also keeps Miton's
recruiting data inside the product database, which matters the day Alister
has a second paying customer with their own admin users.

### 3.2 A separate service with its own database (recommended)

"Miton Talent": its own repository, its own FastAPI backend, its own
Postgres database, its own MCP server, its own admin. It talks to Alister
only through Alister's public API and MCP, the same way an external
customer would. Alister does not know Miton Talent exists.

The talent chat backend is already this service in embryo: a standalone
FastAPI app on Railway, Miton's own, with Resend and an Anthropic key.
It grows into the Miton layer instead of a new repo appearing.

The cost is a second admin, a second login, a second deploy. The gain is
that Alister stays a product and Miton Talent stays a back office, each
with one job. Miton Talent can be ugly and pragmatic; Alister cannot.

### 3.3 Full microservices

Separate services per concern (inbox, searches, watchlist, outreach).
Overkill for one recruiter and one colleague. The two-box design already
gives the separation that matters.

## 4. The target picture

```
                 miton.cz career page          Claude (skills, MCP)
                          |                            |
                          v                            v
   +------------------------------------------------------------------+
   |  MITON TALENT   (Miton's back office, private)                   |
   |  repo: miton-talent (grown from miton-talent-chat)               |
   |  Railway or Hetzner, own Postgres "miton_talent"                 |
   |                                                                  |
   |  chat backend      candidates + events     searches + founders   |
   |  (today's app.py)  (inbox, pool, outreach) (share pages /s/tok)  |
   |                                                                  |
   |  watch list        portfolio               dashboards            |
   |  (who + why)       (company list, digest)  (placements, intros)  |
   |                                                                  |
   |  admin (Google login, miton.cz only)   MCP server for skills     |
   |  Google Sheet mirror (nightly, one way)                          |
   +------------------------------------------------------------------+
                          |  read only, via API / MCP, by API key
                          v
   +------------------------------------------------------------------+
   |  ALISTER   (product: market facts, sourcing, market intelligence)|
   |  repo: github-sourcing, Hetzner, Postgres alister_prod           |
   |                                                                  |
   |  people, companies, repos, jobs, scores, identity, shortlists    |
   |  change detection for any list of handles (generic)              |
   |  job postings for any company (generic)                          |
   +------------------------------------------------------------------+
```

Everything Miton Talent needs from Alister is a read:

| Miton Talent asks | Alister endpoint (existing or small addition) |
|---|---|
| "who is linkedin.com/in/x" | profile lookup by LinkedIn URL (exists in MCP `view_profiles` / search) |
| "job postings for these 40 companies" | `job_postings` filtered by company ids (small addition to the API) |
| "what changed for these 300 handles this week" | the watchlist detector, exposed as a generic "monitor these handles" endpoint |
| "candidate card for a founder page" | `view_profiles` detailed |

Alister never writes to Miton Talent and never reads from it.

## 5. What moves where

| Thing | Today | Target | When |
|---|---|---|---|
| Talent chat submissions | Notion | Miton Talent `candidates` | phase 1 |
| Applicant inbox, candidate pool, triage | Notion | Miton Talent `candidates` + `candidate_events` | phase 1 |
| Search tables for founders | Notion (80 databases) | Miton Talent `searches` + share pages | phase 1 |
| Outreach log | Notion | Miton Talent `candidate_events` | phase 1 |
| Watch list: who and why, outcomes | Alister `watchlist_people` (dev branch) | Miton Talent; Alister keeps only the generic detector | phase 2 |
| Portfolio hiring digest | claude.ai artifact, by hand every week | **done**: Miton Talent scrapes the careers pages itself every Monday (`docs/portfolio-open-roles.md`); it needs nothing from Alister, because the portfolio list is Miton's own | phase 2 |
| Founder momentum list | claude.ai artifact | Miton Talent, optional | later |
| HR dashboards | Notion | Miton Talent cards | phase 2 |
| Alister admin "Watchlist" tab | Alister | removed from Alister once phase 2 lands | phase 2 |

Phase 1 is exactly the Notion replacement, built in the right box. Phase 2
is the clean-up that gets the Miton columns out of Alister.

## 6. Where to run it

Decision (2026-09-29): **Hetzner, next to Alister.** The separation that
matters is in code and data, not in the machine. What keeps the boundary on a
shared box:

- its own database `miton_talent` on the shared Postgres instance, never a
  schema inside `alister_prod`;
- its own folder `/root/live/miton-talent`, systemd unit, port (8200) and `.env`;
- its own nginx vhost and domain, `talent.miton.cz` (a Miton domain, not an
  Alister one, because it is Miton's back office);
- its own daily backup with 30 days of retention (Alister backs up only
  `alister_prod`, biweekly).

Everything is in `deploy/`. Railway was the alternative (managed Postgres,
deploy on push, about 10 to 15 USD a month) and was rejected to keep one
place, one way of operating, zero extra cost. The known cost of the choice: one
server is a single point of failure for Alister and the chat on miton.cz
alike, and the "prod is read-only for agents" rule must cover this folder too.

## 7. The admin question, honestly

A second admin is the real price of option 3.2. Three ways to pay it, from
cheapest:

1. **Google Sheets as the admin** for phase 1. The service ingests and
   syncs; humans work in the sheet. Founders get a tab. Zero UI code.
   Limits are in `notion-replacement-analysis.md` section 3, option A.
2. **A minimal server-rendered admin** in the FastAPI app (Jinja templates,
   a few pages: inbox, candidate, search, share page). No React, no build
   step, Google login restricted to miton.cz. Two or three days.
3. **A Next.js admin reusing Alister's Dune tokens** as a separate app.
   Looks like Alister, is not Alister. Four or five days.

Start with 1, add 2 when the sheet gets in the way. 3 only if the back
office becomes something founders use daily.

**Login, decided 2026-09-30:** no Google login and no second login. Alister
is the only place anyone signs in; it carries three account tiers (`admin`,
`miton`, `user`) and hands `admin`/`miton` accounts on an `@miton.cz`
address into Miton Talent with a one-minute signed token
(`TALENT_HANDOFF_SECRET`, shared by the two services). Miton Talent keeps
its own session cookie and re-checks the address on every request. This
keeps the rule in section 4: the only thing the two services share is one
secret in `.env`. Google sign-in, if it ever comes, goes into Alister and
the handoff stays as it is. Details: `backend/talent/auth.py`.

## 8. Decisions

1. Confirm the rule in section 2. Every future table gets sorted by it.
2. Confirm option 3.2 (separate service) over 3.1 (namespace).
3. Railway or Hetzner.
4. Phase 1 admin: Sheets, minimal server-rendered, or Next.js.
5. Whether the watchlist move (phase 2) happens before or after the
   watchlist's first weekly runs on Alister prove the detector works.

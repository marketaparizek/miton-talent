---
name: candidate-triage
description: Triage new inbound candidates from the Miton Talent applicant inbox (the Talent chat on miton.cz, read through the Alister Market connector) and prepare Gmail drafts, either a rejection or an invitation to an intro call. Use this skill whenever Markéta says she wants to go through new candidates, process the inbox, answer applicants, clear the inbox, run triage, write rejections, or invite someone to a call. Trigger it even when she just says "projdi nové kandidáty", "kdo přišel", "odpovězme lidem" or similar, without naming the skill. Runs roughly twice a week.
---

# Candidate triage

Turn new inbound candidates into ready Gmail drafts. Two outcomes only: rejection, or invitation to an intro call. Never send anything. Markéta reviews every draft in Gmail and sends it herself.

## Data

The applicants live in Miton Talent. Read and update them with three tools of the **Alister Market** connector. Notion ("Hledáme chytré lidi") is no longer the inbox. Do not read it or write to it.

- `applicant_inbox()` lists everyone still waiting for an answer, newest first. For each person you get the `uid`, name, e-mail, language (`cs` or `en`), source, when they applied and how many days ago, the date a rejection may go out, the recommendation and score, area, level and a short summary.
- `applicant_detail(uid)` gives one person in full: the chat transcript, the full summary, their own message, the reasoning behind the recommendation and the history. Use it only when the inbox row is not enough to write the one-line reason in the report.
- `applicant_update(uid, stage, reply)` records what was sent. It is the only way anything changes. It is recorded under Markéta's name.

The tool results end with a short reminder of these rules. If a tool answers that Miton Talent is not answering, stop and tell Markéta. Do not guess the inbox from anywhere else.

## Workflow

1. Call `applicant_inbox()`. Everyone it lists is waiting for an answer. Everyone else has already been handled. If it says it shows only part of the list, call it again with a higher `limit`.
2. For each person, read the recommendation, name, e-mail, language, summary, area and level from the inbox row.
3. Apply the decision rules below.
4. Pick the language.
5. Create a Gmail draft using the matching template. One draft per candidate.
6. Post a report in chat, one line per candidate: name, decision, one short reason. Nothing longer.
7. Wait. After Markéta confirms she has sent them, record each one:
   - invitation: `applicant_update(uid, stage="contacted", reply="invitation")`
   - rejection: `applicant_update(uid, stage="rejected", reply="rejection")`

   Never change anything before she confirms. An unsent draft stays in the inbox. If she sent only some of them, record only those.

Never call the Gmail send tool. Drafts only.

## Decision rules

The decision is already in the inbox. Read the recommendation and follow it. Do not re-derive it.

- Call → invitation
- Potential fit → invitation
- Poslat founderovi → invitation. The call comes first, the founder handoff is a separate step outside this skill.
- Template reply → rejection
- Low fit → rejection
- K rozhodnutí → no draft. List in the report and ask.
- no recommendation → no draft. List in the report and ask. This means the automatic evaluation did not run for that person.

No e-mail means no draft, whatever the recommendation. Those candidates cannot be contacted, so list them and leave them in the inbox.

## Two-day delay on rejections

Never draft a rejection for someone who applied less than two days ago. A same-day no looks like nobody read it.

Each inbox row says when a rejection may go out ("rejection allowed from"). That date is 48 hours after the application. Before it, the candidate stays in the inbox and goes into a "TOO SOON" block in the report with that date. The next run picks them up.

Invitations have no delay. Draft those as soon as they come up.

## Language

Use the language from the inbox row: `cs` → Czech, `en` → English. When it is missing, match the candidate: Czech if they wrote in Czech or their profile is Czech, English otherwise. Never translate the templates. Use the version below as written.

Czech drafts need the vocative. Eva → Evo, Jakub → Jakube, Petr → Petře, Tomáš → Tomáši, Jana → Jano, Ondřej → Ondřeji, Lucie → Lucie, Marek → Marku. If a name is unusual and the vocative is uncertain, fall back to "Hezký den," with no name rather than getting it wrong.

## Templates

Use these verbatim. Change the name only. Do not add lines, do not modernise the wording. All four templates share the same subject line by language, "Moc díky za zájem o Miton!" in Czech and "Thank you for your interest in Miton" in English. Do not invent a different subject for the invitation.

### Rejection, Czech

Subject: Moc díky za zájem o Miton!

```
Hezký den {vocative},

moc děkujeme za zájem o možnosti v Mitonu a děkujeme za shrnutí informací o Vašich dosavadních zkušenostech.

Bohužel v současné době nemáme otevřenou roli, která by odpovídala Vašemu zaměření a zkušenostem. Tato situace se však může v budoucnu změnit a budeme moc rádi pokud nám dovolíte si ponechat Vaše údaje a spojit se s Vámi pokud by se u nás objevila role, která by Vám ladila. A prosím, opravdu to neberte jako frázi aneb nové role se u nás či v rámci našeho portfolia skutečně objevují velmi často.

Ještě jednou moc díky za zájem o Miton a doufáme, že se nám v budoucnu podaří najít společnou cestu.

S pozdravem
M.
```

### Rejection, English

Subject: Thank you for your interest in Miton

```
Hi {first name},

Thank you so much for reaching out and for taking the time to walk me through your experience.

I went through our portfolio to see if there was a good fit right now, but at the moment we do not have a role that matches your focus. That can change pretty fast though, so I would love to keep your details and get back to you when something relevant comes up. Only if you are happy with that, of course.

Thanks again for thinking of us. I hope our paths cross somewhere down the line.

Best,
M.
```

### Invitation, Czech

Subject: Moc díky za zájem o Miton!

```
Hezký den {vocative},

moc díky za Váš zájem o možnosti v Mitonu. Budeme moc rádi pokud se dohodneme na společném intro callu skrze google meet, kde bychom mohli probrat situace na obou stranách.

Tady naleznete moje calendly https://calendly.com/marketa-parizek/30min a dejte vědět zda si vyberete termín, který by Vám ladil. Pokud by Vám žádný termín nevyhovoval, tak dejte vědět a zkusím se přizpůsobit.

Přeji hezký den a těším se na seznámení.
M.
```

### Invitation, English

Subject: Thank you for your interest in Miton

```
Hi {first name},

Thank you very much for your interest in opportunities at Miton. We would be very happy to set up a joint intro call via Google Meet to discuss the situation on both sides.

You can find my Calendly link here: https://calendly.com/marketa-parizek/30min. Please let me know if you choose a time that works for you. If none of the available slots fit your schedule, just let me know and I will try to adjust.

Wishing you a lovely day,
Markéta from Miton
```

The apology for a late reply is not in the template. Add the sentence "and my sincere apologies for getting back to you with such a delay" (or the Czech equivalent) only when the candidate has been waiting more than three weeks, that is, the inbox row says 21 or more days ago.

## Report format

```
12 new candidates. 3 calls, 9 rejections. All drafts in Gmail.

CALL
- Andrej Novák, Call, backend, ex Productboard
- ...

REJECT
- Eva Dvořáková, Low fit, agency marketing, no portfolio fit
- ...

ASKING
- Pavel Šimek, K rozhodnutí, GTM but very junior, draft not created
- Jan Bez Mailu, Call, no e-mail, draft not created

TOO SOON
- Jana Krátká, applied 18 July, rejection from 20 July
```

No commentary after the list. Never show the `uid` in the report. It is only for `applicant_update`.

## After Markéta confirms

Record each sent reply with `applicant_update` as in step 7, then reply with one line: how many were recorded, and anyone whose update failed with the tool's message. Someone already moved by a colleague comes back as "already in stage …". Mention it, do not retry.

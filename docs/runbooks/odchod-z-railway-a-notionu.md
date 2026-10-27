# Odchod z Railway a Notionu

Talent chat na miton.cz přejde z Railway na Hetzner (talent.alisterai.com). Uchazeči přestanou chodit do Notionu „Hledáme chytré lidi“ a budou v databázi Miton Talentu. Triáž poběží přes záložku Applicants v Alister hubu a přes skill candidate-triage.

Postup má sedm fází. Fáze 1 až 4 se dají udělat kdykoli a návštěvníci miton.cz si ničeho nevšimnou. Ostré je až přepnutí iframu (fáze 5). Pak běží týden souběhu, během kterého se dá kdykoli vrátit zpátky.

Server Hetzner je `root@46.225.26.134`. Příkazy s `ssh -t` pouštěj ze svého Macu.

---

## Fáze 0. Co mít v ruce

- Právo pushovat do `marketaparizek/miton-talent` a do repa Alisteru.
- SSH na Hetzner.
- `NOTION_TOKEN` integrace Miton webu. Je v Railway pod Variables, případně v `~/.config/miton-talent/notion_token` na Macu.
- Kontakt na Petra Križka (VIRTII), který spravuje miton.cz.

---

## Fáze 1. Talent na Hetzner

Na Hetzneru zatím běží Talent bez auditních oprav a bez API pro inbox. Nová verze nenastartuje bez `SESSION_SECRET`, proto se proměnné nastavují **před** nasazením.

**1.1 Sloučit větev do main a pushnout (Mac)**

```bash
cd ~/miton-talent && git checkout main && git merge --ff-only feature/applicant-inbox && git push origin main
```

**1.2 Zkontrolovat pracovní složku na serveru**

Na server se v minulosti kopírovaly soubory ručně. Nejdřív se podívej, co tam je:

```bash
ssh root@46.225.26.134 'cd /root/live/miton-talent && git status --short'
```

Pokud vypíše změněné soubory a jsou to jen ty ručně nakopírované (`scripts/import_notion_export.py`, `backend/talent/store.py`), zahoď je, jinak `git pull` neprojde:

```bash
ssh root@46.225.26.134 'cd /root/live/miton-talent && git checkout -- .'
```

**1.3 Nastavit proměnné v `.env`**

Každý příkaz se zeptá na hodnotu, nic se nezobrazí a služba se restartuje. Zatím poběží stará verze, které nové proměnné nevadí.

`SESSION_SECRET` je nový klíč pro přihlášení do back office. Musí být jiný než `TALENT_HANDOFF_SECRET`. Vygeneruj ho na Macu a vlož:

```bash
openssl rand -hex 32
```

```bash
ssh -t root@46.225.26.134 'bash /root/live/miton-talent/deploy/set_secret.sh SESSION_SECRET'
```

`FRAME_ANCESTORS` určuje, kdo smí chat vložit do iframu. Na dobu přepínání jsou v seznamu i Petrovy testovací weby. Vlož tuhle hodnotu (bez mezer):

`https://www.miton.cz,https://miton.cz,https://preview.miton.cz,https://miton.wx.virtii.com,https://alisterai.com`

```bash
ssh -t root@46.225.26.134 'bash /root/live/miton-talent/deploy/set_secret.sh FRAME_ANCESTORS'
```

Zapnout automatické hodnocení uchazečů (vlož `1`):

```bash
ssh -t root@46.225.26.134 'bash /root/live/miton-talent/deploy/set_secret.sh SCORING_ENABLED'
```

Zapnout zápis do Notionu na dobu souběhu (vlož token z Railway):

```bash
ssh -t root@46.225.26.134 'bash /root/live/miton-talent/deploy/set_secret.sh NOTION_TOKEN'
```

**1.4 Nasadit**

Skript nejdřív zazálohuje databázi, pak stáhne main, nainstaluje závislosti a restartuje službu. Při startu proběhne migrace `e4f5a6b7c8d9` (přihlašovací session, limity chatu, zámky). Na konci ověří zdraví a to, že `/diag` je zamčený.

```bash
ssh root@46.225.26.134 'cd /root/live/miton-talent && bash deploy/deploy.sh'
```

Má skončit řádky `<- healthy` a `/diag is behind the sign-in ... <- ok`.

**1.5 Ověřit**

```bash
curl -s https://talent.alisterai.com/health
```

```bash
curl -sI https://talent.alisterai.com/ | grep -i content-security
```

Druhý příkaz musí vypsat `frame-ancestors` s miton.cz.

Pak otevři https://talent.alisterai.com/, projdi krátký chat s vymyšlenými údaji a e-mailem `marketa.parizek+test@miton.cz` a odešli ho. Má přijít e-mail s přihláškou. Testovací řádek nemaž hned, poslouží ve fázi 2.

**Když to nenastartuje:** `ssh root@46.225.26.134 'journalctl -u miton-talent -n 50 --no-pager'`. Nejčastější příčina je chybějící nebo krátký `SESSION_SECRET` (aspoň 32 znaků) nebo stejný jako `TALENT_HANDOFF_SECRET`.

---

## Fáze 2. Alister dev

**2.1 Pushnout větev a sloučit do dev (Mac)**

```bash
cd ~/gs-wt-applicants && git push origin feature/applicant-inbox
```

```bash
cd ~/github-sourcing && git checkout dev && git pull && git merge feature/applicant-inbox && git push origin dev
```

**2.2 Nasadit na dev**

`sync-dev.sh` restartuje web a API, ale ne MCP server Alister Market. Ten se restartuje zvlášť:

```bash
ssh root@46.225.26.134 'cd /root/live/github-sourcing-dev && bash scripts/sync-dev.sh && bash start_dev_analyst_mcp.sh'
```

**2.3 Ověřit**

Otevři https://dev.alisterai.com/miton?tab=applicants. Má tam být záložka Applicants a v ní testovací přihláška z fáze 1.5 a uchazeči naimportovaní z Notionu, kteří čekají. Rozklikni testovací řádek a dej „Invitation sent“. Řádek zmizí a v detailu (Contacted) bude historie s tvým e-mailem.

Testovací řádek pak smaž úplně. Nejjednodušší je podle e-mailu, proto v testu používej adresu, kterou žádný skutečný uchazeč nemá, třeba `marketa.parizek+test@miton.cz`. Bez `--yes` příkaz jen spočítá, co by smazal:

```bash
ssh root@46.225.26.134 'cd /root/live/miton-talent/backend && set -a && . ../.env && set +a && .venv/bin/python -m talent.admin_cli erase --email marketa.parizek+test@miton.cz'
```

Když počet sedí, pusť totéž s `--yes` na konci.

---

## Fáze 3. Alister prod

Konektor Alister Market v Claude vede nejspíš na prod (`alisterai.com/analyst-mcp`). Skill proto potřebuje nové tooly na produ. U konektoru to ověříš v nastavení Claude, URL je vidět v detailu konektoru.

Pozor, `promote-to-prod.sh` přenese na prod **všechno**, co je na dev. Na dev teď čeká víc věcí (authz opravy z auditu, New builders v3, Traffic). Vyber jednu cestu:

- **A. Pustit celý dev.** Pokud je dev odsouhlasený, `bash scripts/promote-to-prod.sh` na serveru ve složce dev, jak jsi zvyklá.
- **B. Jen inbox.** Na main přenést tři commity inboxu (`b58706b`, `dfd79d9`, `99c4839`) přes `git cherry-pick`, pushnout main a na produ `git pull` + restart. Stejně jako u Market trends 26.10. Ráda to připravím jako samostatnou větev.

Po nasazení restartuj prod MCP Alister Market. Ani promote-to-prod.sh ho nerestartuje:

```bash
ssh root@46.225.26.134 'systemctl restart alister-prod-analyst-mcp'
```

Pak v Claude u konektoru Alister Market klikni na ⋮ a **Refresh tools list**. Znovu se připojovat nemusíš. Ostatní uživatelé konektoru tooly neuvidí, jsou jen pro účty Miton.

**Ověřit:** v novém chatu v Claude napiš „zavolej applicant_inbox“. Má vrátit seznam čekajících lidí.

---

## Fáze 4. Nový skill (až v den přepnutí)

Nový skill čte jen Talent. Dokud chat běží přes Railway, noví uchazeči jsou jen v Notionu, proto ho vyměň až v den přepnutí iframu.

1. V Claude otevři nastavení skillů organizace.
2. Starý candidate-triage nahraď souborem `docs/skills/candidate-triage/SKILL.md` z repa Talentu.
3. Vyzkoušej „projdi nové kandidáty“. Musí vzniknout koncepty v Gmailu a report v chatu. Notion se nesmí objevit.

---

## Fáze 5. Přepnutí iframu (Petr, VIRTII)

Mění se jen doména v `src`, parametry zůstávají (`?lang=en` u anglické stránky, případně `?resize=1`).

Návrh e-mailu Petrovi:

> Ahoj Petře, přesouváme náborový chat z Railway na náš server. Prosím na stránkách /kariera/ a /en/career/ změň v iframu adresu z `https://miton-talent-chat-production.up.railway.app` na `https://talent.alisterai.com`. Parametry za otazníkem nech, jak jsou. Nejdřív to prosím zkus na preview.miton.cz a dej vědět, ať to otestuju, pak na ostrý web. Díky! Markéta

**Ověřit na preview i na ostrém webu:**

- Chat se načte (prázdný rámeček znamená, že doména chybí ve `FRAME_ANCESTORS`).
- Anglická stránka mluví anglicky.
- Testovací přihláška dorazí e-mailem a objeví se v Applicants. Pak ji smaž jako v kroku 2.3.

Den přepnutí je **den D**.

---

## Fáze 6. Týden souběhu (D až D+7)

**Jak to běží:**

- Nový chat zapisuje do Talentu **a zároveň** do Notionu (díky `NOTION_TOKEN` z kroku 1.3). Notion je jen pojistka.
- Triáž dělej **jen** přes nový skill nebo záložku Applicants. Stavy v Notionu neměň, jinak se rozejdou dvě pravdy.
- Railway nech běžet. Kdo měl stránku otevřenou nebo v cache, může ještě chvíli psát do starého chatu. Ten zapisuje jen do Notionu.

**Doimport uchazečů z Railway.** První import z 30.9. nezná lidi, kteří přišli přes Railway potom. Import je opakovatelný: řádek, který už v Talentu je, pozná podle Notion id nebo e-mailu a nezdvojí ho. Stav, který jsi už změnila v Talentu, nepřepíše. Odmítnuté přeskočí. Pusť ho v den D+1 a znovu v D+7:

```bash
ssh root@46.225.26.134 'cd /root/live/miton-talent && set -a && . ./.env && set +a && backend/.venv/bin/python scripts/export_notion.py --out /root/miton-notion-export-2 --skip-searches --force'
```

```bash
ssh root@46.225.26.134 'cd /root/live/miton-talent && set -a && . ./.env && set +a && backend/.venv/bin/python scripts/import_notion_export.py --export /root/miton-notion-export-2 --skip-rejected --dry-run'
```

Když čísla v dry-run sedí (přibude zhruba tolik lidí, kolik jich přišlo přes Railway od 30.9.), pusť totéž bez `--dry-run`.

**Návrat zpátky**, kdyby se něco pokazilo: Petr vrátí v iframu adresu Railway a vrátíš starý skill. Railway i Notion celou dobu běží, takže se nic neztratí. Lidi, kteří mezitím přišli přes Hetzner, jsou v Notionu taky.

---

## Fáze 7. Úklid (D+7)

Až týden proběhne bez problémů:

1. **Vypnout zápis do Notionu:**

   ```bash
   ssh root@46.225.26.134 "cd /root/live/miton-talent && sed -i '/^NOTION_TOKEN=/d' .env && systemctl restart miton-talent"
   ```

2. **Zúžit `FRAME_ANCESTORS`** na `https://www.miton.cz,https://miton.cz,https://alisterai.com` (stejně jako v kroku 1.3).
3. **Railway:** v dashboardu smazat službu a zrušit plán.
4. **Notion:** v Settings → Connections odpojit integraci Miton webu a smazat `~/.config/miton-talent/notion_token` na Macu.
5. **Starý skill** z Claude odstranit, pokud tam ještě je.
6. **Kód:** odstranit z `backend/app.py` zápis do Notionu (`_write_notion` a spol.) a `/diag` kontrolu Notionu. To je práce pro mě, samostatný commit.
7. Volitelně **orotovat `ANTHROPIC_API_KEY`**. Při prvním vkládání se 30.9. zobrazil v terminálu.

---

## Rollback nasazení Talentu (fáze 1)

`deploy.sh` udělá zálohu databáze, než cokoli změní (`/root/db_backups/miton_talent`). Kdyby nová verze dělala potíže a iframe ještě nebyl přepnutý, stačí se vrátit na předchozí commit main (`5c213ca`) a vrátit migraci:

```bash
ssh root@46.225.26.134 'cd /root/live/miton-talent && set -a && . ./.env && set +a && cd backend && .venv/bin/alembic downgrade d3e4f5a6b7c8 && cd .. && git checkout 5c213ca && systemctl restart miton-talent'
```

Pak opravit, vrátit `git checkout main` a znovu `deploy.sh`.

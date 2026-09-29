#!/usr/bin/env python3
"""Export Miton's recruiting data out of Notion before the workspace is cancelled.

Writes one JSONL file per database plus page bodies, into an output folder:

  out/
    hledame_chytre_lidi.jsonl       applicant inbox, 1 row per page (properties flattened)
    full_databaze_kandidatu.jsonl   the long-term candidate pool
    outreach_table.jsonl            outreach log
    searches/<db-id>.jsonl          one file per per-search database under "Sdílení searchů"
    searches/index.jsonl            db id, title, parent (month/year) page titles
    bodies/<page-id>.json           page body blocks for inbox rows (transcripts, evaluations)
    manifest.json                   counts + timestamps, so a re-run can be compared

Run it with the same integration token the chat backend uses (Railway variable
NOTION_TOKEN; the integration must be connected to the "HR v Mitonu" tree):

  NOTION_TOKEN=ntn_... python scripts/export_notion.py --out ~/miton-notion-export

Re-runnable. Never modifies Notion. Rate limited to ~3 requests/second.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"

DATABASES = {
    "hledame_chytre_lidi": "1ba6065a-c67d-4ba6-97f5-1a6c9662d137",
    "full_databaze_kandidatu": "4e95d606-42b0-45ac-8033-8f2029a3fb32",
    "outreach_table": "2c77361a-c607-4740-b3f2-2554e91b140e",
}
SEARCHES_ROOT_PAGE = "3b876b6f-63eb-41a5-abe0-f6fc7d648222"   # "Sdílení searchů"
BODIES_FOR = ["hledame_chytre_lidi"]                          # inbox rows carry transcripts


class Notion:
    def __init__(self, token: str):
        self.h = httpx.Client(
            base_url=API, timeout=30,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": VERSION,
                     "Content-Type": "application/json"},
        )
        self._last = 0.0

    def _throttle(self):
        wait = 0.34 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _call(self, method: str, path: str, **kw):
        for attempt in range(6):
            self._throttle()
            r = self.h.request(method, path, **kw)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "2")))
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401:
                sys.exit("Notion says 401 Unauthorized: NOTION_TOKEN is not a valid integration secret. "
                         "Copy the Internal Integration Secret (starts with ntn_) from notion.so/profile/integrations.")
            if r.status_code in (403, 404):
                sys.exit(f"Notion says {r.status_code} for {path}: the integration is not connected to this page. "
                         "Open 'HR v Mitonu' in Notion -> ... -> Connections -> add the integration, then re-run.")
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Notion API kept failing: {method} {path}")

    def database(self, db_id: str) -> dict:
        return self._call("GET", f"/databases/{db_id}")

    def query_all(self, db_id: str):
        cursor = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self._call("POST", f"/databases/{db_id}/query", json=body)
            for page in data.get("results", []):
                yield page
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    def children_all(self, block_id: str):
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._call("GET", f"/blocks/{block_id}/children", params=params)
            for b in data.get("results", []):
                yield b
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")


def rich_text(rt) -> str:
    return "".join(x.get("plain_text", "") for x in (rt or []))


def flatten(page: dict) -> dict:
    """Turn a Notion page's properties into plain values. Keeps the raw properties
    too, so nothing is lost if a type is handled crudely here."""
    out = {
        "id": page["id"], "url": page.get("url"),
        "created_time": page.get("created_time"), "last_edited_time": page.get("last_edited_time"),
        "archived": page.get("archived", False),
    }
    for name, prop in (page.get("properties") or {}).items():
        t = prop.get("type")
        v = prop.get(t)
        if t in ("title", "rich_text"):
            out[name] = rich_text(v)
        elif t in ("select", "status"):
            out[name] = (v or {}).get("name")
        elif t == "multi_select":
            out[name] = [o.get("name") for o in (v or [])]
        elif t == "date":
            out[name] = (v or {}).get("start")
        elif t == "files":
            out[name] = [{"name": f.get("name"),
                          "url": (f.get("file") or f.get("external") or {}).get("url")} for f in (v or [])]
        elif t == "relation":
            out[name] = [r.get("id") for r in (v or [])]
        elif t == "formula":
            fv = v or {}
            out[name] = fv.get(fv.get("type"))
        elif t in ("people", "created_by", "last_edited_by"):
            out[name] = [p.get("name") or p.get("id") for p in (v if isinstance(v, list) else [v] if v else [])]
        else:  # number, checkbox, url, email, phone_number, created_time, ...
            out[name] = v
    out["_properties"] = page.get("properties")
    return out


def block_text(b: dict) -> dict:
    t = b.get("type")
    body = b.get(t) or {}
    return {"id": b["id"], "type": t, "text": rich_text(body.get("rich_text")),
            "has_children": b.get("has_children", False)}


def _already_exported(out: Path, key: str) -> int | None:
    """Row count of a previous complete export of this database, else None."""
    done = out / f"{key}.done"
    if done.exists():
        try:
            return int(done.read_text().strip())
        except ValueError:
            return None
    return None


def export_database(n: Notion, key: str, db_id: str, out: Path, with_bodies: bool, force: bool = False) -> int:
    prev = _already_exported(out, key)
    if prev is not None and not force:
        print(f"  {key}: already exported ({prev} rows), skipping", file=sys.stderr)
        return prev
    meta = n.database(db_id)
    (out / f"{key}.schema.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    count = 0
    with (out / f"{key}.jsonl").open("w", encoding="utf-8") as fh:
        for page in n.query_all(db_id):
            row = flatten(page)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
            if with_bodies:
                blocks = [block_text(b) for b in n.children_all(page["id"])]
                (out / "bodies" / f"{page['id']}.json").write_text(
                    json.dumps(blocks, ensure_ascii=False))
            if count % 100 == 0:
                print(f"  {key}: {count} rows", file=sys.stderr)
    (out / f"{key}.done").write_text(str(count))
    return count


def walk_child_databases(n: Notion, page_id: str, path: list[str]):
    """Yield (db_id, title, path) for every child_database anywhere under a page,
    descending through toggles, columns and child pages."""
    for b in n.children_all(page_id):
        t = b.get("type")
        if t == "child_database":
            yield b["id"], (b.get("child_database") or {}).get("title", ""), path
        elif t == "child_page":
            title = (b.get("child_page") or {}).get("title", "")
            yield from walk_child_databases(n, b["id"], path + [title])
        elif b.get("has_children"):
            label = rich_text((b.get(t) or {}).get("rich_text")) if t in ("heading_1", "heading_2", "heading_3", "toggle") else None
            yield from walk_child_databases(n, b["id"], path + ([label] if label else []))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="notion-export")
    ap.add_argument("--skip-searches", action="store_true")
    ap.add_argument("--skip-bodies", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-export databases that already have a .done marker")
    ap.add_argument("--probe", help="diagnose one database/block id: print raw API answers and exit")
    args = ap.parse_args()

    token = os.environ.get("NOTION_TOKEN", "").strip()
    token_file = Path.home() / ".config" / "miton-talent" / "notion_token"
    if (not token or token.startswith("ntn_VLOZ")) and token_file.exists():
        token = token_file.read_text().strip()
        print(f"používám uložený token z {token_file} (smaž ho po exportu)", file=sys.stderr)
    if not token or token.startswith("ntn_VLOZ"):
        import getpass
        print("Vlož Internal Integration Secret z notion.so/profile/integrations (začíná ntn_).", file=sys.stderr)
        print("Při vkládání se nic nezobrazuje, to je v pořádku. Pak stiskni Enter.", file=sys.stderr)
        token = getpass.getpass("NOTION_TOKEN: ")
    # Take just the token out of whatever was pasted (extra text, spaces, newlines are fine).
    import re
    # Notion internal secrets are "ntn_" + 46 alphanumerics. Take the FIRST such
    # run so a token pasted twice in a row still yields one clean token.
    found = re.findall(r"ntn_[A-Za-z0-9]{46}", token or "") or \
        re.findall(r"secret_[A-Za-z0-9]{43}", token or "") or \
        re.findall(r"(?:ntn|secret)_[A-Za-z0-9]{20,}", token or "")
    if not found:
        print("V zadaném textu není token integrace (má začínat ntn_). Zkus to znovu.", file=sys.stderr)
        return 2
    token = found[0]
    print(f"token přijat ({token[:6]}…{token[-4:]})", file=sys.stderr)
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token)
        os.chmod(token_file, 0o600)
    except OSError:
        pass
    out = Path(args.out).expanduser()
    (out / "bodies").mkdir(parents=True, exist_ok=True)
    (out / "searches").mkdir(parents=True, exist_ok=True)
    n = Notion(token)
    if args.probe:
        pid = args.probe
        for method, path, body in (("GET", f"/databases/{pid}", None),
                                   ("POST", f"/databases/{pid}/query", {"page_size": 1}),
                                   ("GET", f"/blocks/{pid}", None),
                                   ("GET", f"/pages/{pid}", None)):
            n._throttle()
            r = n.h.request(method, path, json=body) if body else n.h.request(method, path)
            txt = r.text
            try:
                j = r.json()
                keep = {k: j.get(k) for k in ("object", "type", "title", "code", "message", "is_inline", "parent", "data_sources") if k in j}
                if j.get("type") and j.get(j["type"]) is not None and isinstance(j.get(j["type"]), dict):
                    keep[j["type"]] = {k: v for k, v in j[j["type"]].items() if k in ("title", "database_id", "data_source_id")}
                txt = json.dumps(keep, ensure_ascii=False)
            except ValueError:
                pass
            print(f"{method} {path} -> {r.status_code}: {txt[:600]}", file=sys.stderr)
        return 0
    manifest = {"exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "databases": {}, "searches": {}}

    for key, db_id in DATABASES.items():
        print(f"exporting {key} ...", file=sys.stderr)
        manifest["databases"][key] = export_database(
            n, key, db_id, out, with_bodies=(key in BODIES_FOR and not args.skip_bodies), force=args.force)

    if not args.skip_searches:
        print("walking Sdílení searchů ...", file=sys.stderr)
        with (out / "searches" / "index.jsonl").open("w", encoding="utf-8") as idx:
            for db_id, title, path in walk_child_databases(n, SEARCHES_ROOT_PAGE, []):
                try:
                    cnt = export_database(n, f"searches/{db_id}", db_id, out, with_bodies=False, force=args.force)
                    rec = {"id": db_id, "title": title, "path": path, "rows": cnt}
                except httpx.HTTPStatusError as e:
                    # 400 "no data sources accessible" = a LINKED view of another database
                    # (in this workspace: filtered views of "Full databáze kandidátů").
                    # Record it so the import can rebuild the search from the pool rows.
                    print(f"  linked/skip {db_id} ({title}): {e.response.status_code}", file=sys.stderr)
                    rec = {"id": db_id, "title": title, "path": path, "rows": 0, "linked": True,
                           "error": e.response.status_code}
                idx.write(json.dumps(rec, ensure_ascii=False) + "\n")
                manifest["searches"][db_id] = rec
                print(f"  {'/'.join(path)} :: {title or db_id}: {rec['rows']}", file=sys.stderr)

    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in manifest.items() if k != "searches"}, ensure_ascii=False), file=sys.stderr)
    print(f"searches: {len(manifest['searches'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

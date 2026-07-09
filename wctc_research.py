#!/usr/bin/env python3
"""
World Cup Trading Championships — Deep Research runner.

Reads your winners CSV, deduplicates people, and runs one Perplexity
`sonar-deep-research` query per person (their full championship record from
your own table is injected into the prompt). Results are stored in SQLite so
the run is fully resumable: crash, rate limit, or Ctrl+C at person #120 and
the next run picks up at #121 — you never pay twice for the same person.

Commands:
    python wctc_research.py init data.csv     # load CSV -> SQLite (safe to re-run)
    python wctc_research.py status            # progress + spend estimate
    python wctc_research.py run               # research all pending people
    python wctc_research.py run --limit 3     # research only N people (test first!)
    python wctc_research.py retry-errors      # reset failed rows to pending
    python wctc_research.py export            # one big markdown doc + CSV of results

Setup:
    pip install requests
    export PPLX_API_KEY="pplx-..."            # your Perplexity API key

IMPORTANT — set a hard spending limit in the Perplexity API dashboard before
running the full batch. Deep research queries commonly cost ~$0.40–$1.50+ EACH;
254 people can plausibly total $150–$400+.
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "wctc.db"
ENV_PATH = Path(__file__).parent / ".env"
API_URL = "https://api.perplexity.ai/chat/completions"
MODEL = "sonar-deep-research"          # Perplexity's most capable research model
REASONING_EFFORT = "high"              # maximum depth ("low"/"medium"/"high")
MAX_RETRIES = 3                        # per-person retries on transient errors
TIMEOUT_SECONDS = 60 * 30              # deep research can take many minutes

PROMPT_TEMPLATE = """You are producing a research report about {name}, \
a competitor in the World Cup Trading Championships (WCTC / Robbins World Cup).

Their verified competition record (from the official leaderboards) is:
{record}

Produce the report in EXACTLY four sections, each starting with the exact marker \
line shown below (these markers are parsed by software — reproduce them verbatim, \
each on its own line):

<<<BIOGRAPHY>>>
A CONCISE biography: strictly 1–2 short paragraphs maximum. Who they are, country, \
trading style/instruments, and the essence of their championship record. No lists, \
no links here. Brevity is mandatory for this section only.

<<<STUDIES>>>
What they studied to become this good. Structure: FIRST a short explanation (1-2 \
paragraphs) of their education, mentors, methodologies and trading approach. THEN a \
LONG bullet list — as many entries as you can verify — of the concrete resources \
behind that: every book, paper, course, mentor's work, methodology guide or \
influence. EVERY bullet must be a markdown hyperlink to the resource, formatted \
like: - [Resource title](https://url) — one-line note on why it matters. Do not \
list bare URLs; embed every link inside the resource name.

<<<TEACHES>>>
What they teach, sell, or share with others. Structure: FIRST a short explanation \
(1-2 paragraphs) of how this person shares knowledge. THEN a LONG bullet list — as \
exhaustive as possible — of every course, authored book, YouTube channel, \
newsletter, signal service, website, podcast appearance, interview, blog, or \
mentorship program they offer. EVERY bullet must be a markdown hyperlink: \
- [Resource title](https://url) — one-line note (include price when findable).

<<<FREE_RESOURCES>>>
Free alternatives to learn the same topics. Structure: FIRST a short explanation \
(1-2 paragraphs) of what skills/topics the free roadmap covers. THEN a LONG bullet \
list — as many high-quality entries as possible — of FREE resources: free courses, \
YouTube channels, public-domain books, university OCW, broker education portals, \
open-source tools. EVERY bullet must be a markdown hyperlink: \
- [Resource title](https://url) — one-line note on what it teaches.

Rules:
- Only include real, working links from your research; never invent URLs. Every \
factual claim should be backed by the linked sources themselves.
- The bullet lists are the heart of this report: make them as long as the evidence \
allows. More verified resources is always better.
- If little or nothing is publicly known about this person, say so explicitly in \
the affected sections rather than inventing anything, and still complete \
<<<FREE_RESOURCES>>> based on the discipline they competed in (futures, forex, etc.).
- The biography must stay short; the other three sections should be extensive.
"""

SECTION_KEYS = {          # marker -> db column
    "BIOGRAPHY": "bio_md",
    "STUDIES": "studies_md",
    "TEACHES": "teaches_md",
    "FREE_RESOURCES": "free_md",
}


# ---------------------------------------------------------------------------- env

def load_env():
    """Load KEY=VALUE lines from a `.env` file sitting next to this script into
    the environment, without needing the python-dotenv package. Real environment
    variables already set in the shell take precedence and are not overwritten."""
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")   # tolerate quotes
        if key and key not in os.environ:
            os.environ[key] = value


# ----------------------------------------------------------------------------- db

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            record TEXT NOT NULL,           -- their rows from the CSV, formatted
            status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
            report_md TEXT,                 -- full raw model output (backup)
            bio_md TEXT,                    -- concise biography (1-2 paragraphs)
            studies_md TEXT,                -- what they studied (extensive markdown)
            teaches_md TEXT,                -- what they teach/sell/share (extensive)
            free_md TEXT,                   -- free alternative resources (extensive)
            model TEXT,
            cost_estimate REAL,             -- rough $ if usage info is returned
            error TEXT,
            request_id TEXT,                -- async job id (crash recovery)
            updated_at TEXT
        )""")
    # migrate older databases: add newer columns, drop the removed citations column
    existing = {r["name"] for r in con.execute("PRAGMA table_info(people)")}
    for col in ("bio_md", "studies_md", "teaches_md", "free_md", "request_id"):
        if col not in existing:
            con.execute(f"ALTER TABLE people ADD COLUMN {col} TEXT")
    if "citations" in existing:
        try:
            con.execute("ALTER TABLE people DROP COLUMN citations")
        except sqlite3.OperationalError:
            pass    # very old SQLite can't drop columns; harmless to leave it
    con.commit()
    return con


def normalize_name(raw: str) -> str:
    """Light normalization so 'Darren O’Niel' and 'Darren O´Neill'-style
    apostrophe variants don't create duplicate research jobs."""
    s = unicodedata.normalize("NFKC", raw.strip())
    s = s.replace("’", "'").replace("´", "'").replace("`", "'")
    s = re.sub(r"\s+", " ", s)
    return s


# --------------------------------------------------------------------------- init

def cmd_init(csv_path: str):
    con = get_db()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    people = {}
    for r in rows:
        name = normalize_name(r["Name"])
        line = f"- {r['Year']}: {r['Event']} — {r['Place']} place, {r['Percentage']}% net return"
        people.setdefault(name, [])
        if line not in people[name]:          # CSV has some duplicated rows
            people[name].append(line)
    added = 0
    for name, lines in people.items():
        record = "\n".join(sorted(lines, reverse=True))
        cur = con.execute(
            "INSERT OR IGNORE INTO people (name, record) VALUES (?, ?)", (name, record))
        added += cur.rowcount
        # keep record fresh for already-known, still-pending people
        con.execute(
            "UPDATE people SET record=? WHERE name=? AND status='pending'", (record, name))
    con.commit()
    total = con.execute("SELECT COUNT(*) c FROM people").fetchone()["c"]
    print(f"Loaded {len(rows)} rows -> {total} unique people ({added} new).")


# ---------------------------------------------------------------------------- api
# Deep research jobs run for many minutes; a plain (synchronous) request often
# dies at Cloudflare with a 502 while the job KEEPS RUNNING and BILLING server-
# side. Perplexity's async API fixes this: submit the job (instant), get a
# request_id, then poll until it's finished. The request_id is stored in the
# database immediately, so even a crash or lost connection can't orphan a paid job.

ASYNC_URL = "https://api.perplexity.ai/async/chat/completions"
POLL_EVERY_SECONDS = 60            # how often to check a running job
MAX_WAIT_MINUTES = 90              # give up polling a single job after this


def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def submit_job(prompt: str, api_key: str) -> str:
    """Submit an async deep research job; returns the request_id immediately."""
    payload = {"model": MODEL,
               "reasoning_effort": REASONING_EFFORT,
               "messages": [{"role": "user", "content": prompt}]}
    body = {"request": payload}
    resp = requests.post(ASYNC_URL, headers=_headers(api_key), json=body, timeout=120)
    if resp.status_code == 400:
        # some API versions accept the payload unwrapped; try that before failing
        resp = requests.post(ASYNC_URL, headers=_headers(api_key), json=payload,
                             timeout=120)
    resp.raise_for_status()
    data = resp.json()
    request_id = data.get("id") or data.get("request_id")
    if not request_id:
        raise KeyError(f"no request id in async response: {str(data)[:200]}")
    return request_id


def poll_job(request_id: str, api_key: str) -> dict | None:
    """Check an async job once. Returns the completed chat response dict,
    None if still running, or raises on FAILED."""
    resp = requests.get(f"{ASYNC_URL}/{request_id}", headers=_headers(api_key),
                        timeout=120)
    resp.raise_for_status()
    data = resp.json()
    status = (data.get("status") or "").upper()
    if status in ("CREATED", "IN_PROGRESS", "PROCESSING", "PENDING", "QUEUED"):
        return None
    if status in ("COMPLETED", "COMPLETE", "SUCCESS", "DONE"):
        return data.get("response") or data     # completion may be nested or flat
    raise RuntimeError(f"async job {status}: {str(data.get('error_message') or data)[:300]}")


def wait_for_job(request_id: str, api_key: str) -> dict:
    """Poll until the job finishes, printing a heartbeat so the wait is visible.
    Transient poll errors are tolerated — the job keeps running server-side
    regardless of our connection."""
    start = time.time()
    deadline = start + MAX_WAIT_MINUTES * 60
    while time.time() < deadline:
        try:
            result = poll_job(request_id, api_key)
            if result is not None:
                return result
            elapsed = (time.time() - start) / 60
            print(f"    ... researching ({elapsed:.0f} min elapsed, typical "
                  f"jobs take 5-30 min)", flush=True)
        except requests.RequestException:
            print("    ... network blip while checking, job unaffected", flush=True)
        time.sleep(POLL_EVERY_SECONDS)
    raise TimeoutError(f"job {request_id} still running after {MAX_WAIT_MINUTES} min "
                       "(it is NOT lost — rerun `run` later to collect it)")


def extract(data: dict):
    text = data["choices"][0]["message"]["content"]
    # strip <think> blocks if present
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    usage = data.get("usage", {}) or {}
    # rough cost estimate from published sonar-deep-research rates
    cost = (usage.get("prompt_tokens", 0) * 2 +
            usage.get("completion_tokens", 0) * 8 +
            usage.get("citation_tokens", 0) * 2 +
            usage.get("reasoning_tokens", 0) * 3) / 1_000_000 \
        + usage.get("num_search_queries", 0) * 5 / 1000
    return text, (cost or None)


def parse_sections(text: str) -> dict:
    """Split the model output on <<<MARKER>>> lines into the four db columns.
    Tolerates extra whitespace or markdown around the markers. If a marker is
    missing, that column is left None (the full raw text is always kept in
    report_md as a backup, so nothing is ever lost)."""
    sections = {v: None for v in SECTION_KEYS.values()}
    pattern = r"<{2,3}\s*(" + "|".join(SECTION_KEYS) + r")\s*>{2,3}"
    parts = re.split(pattern, text)
    # parts = [preamble, MARKER, content, MARKER, content, ...]
    for i in range(1, len(parts) - 1, 2):
        key = SECTION_KEYS.get(parts[i].strip())
        if key:
            content = parts[i + 1].strip()
            # drop a leading markdown heading the model may add under the marker
            content = re.sub(r"^#+\s*[^\n]*\n+", "", content, count=1) \
                if re.match(r"^#+\s", content) else content
            sections[key] = content or None
    return sections


# ---------------------------------------------------------------------------- run

def cmd_run(limit: int | None):
    load_env()
    api_key = os.environ.get("PPLX_API_KEY")
    if not api_key:
        sys.exit("No API key found. Create a `.env` file next to this script "
                 "containing:\n    PPLX_API_KEY=pplx-your-key-here\n"
                 "(or set it in your shell:  export PPLX_API_KEY='pplx-...')")
    con = get_db()
    # 'submitted' rows are already-paid jobs running on Perplexity's servers —
    # ALWAYS collect those first (never resubmit = never double-bill).
    todo = con.execute("SELECT * FROM people WHERE status IN ('submitted','pending') "
                       "ORDER BY CASE status WHEN 'submitted' THEN 0 ELSE 1 END, id"
                       ).fetchall()
    if limit:
        todo = todo[:limit]
    if not todo:
        print("Nothing pending. Run `status` or `export`.")
        return
    print(f"Processing {len(todo)} people with {MODEL} via the ASYNC API "
          f"(reasoning_effort={REASONING_EFFORT}).\n"
          f"Each job takes ~5-30 min. Ctrl+C anytime — submitted jobs keep "
          f"running server-side and are collected on the next `run`.\n")
    for i, p in enumerate(todo, 1):
        t0 = time.time()
        try:
            if p["status"] == "submitted" and p["request_id"]:
                print(f"[{i}/{len(todo)}] {p['name']} — resuming job "
                      f"{p['request_id'][:12]}... (already paid, just collecting)",
                      flush=True)
                request_id = p["request_id"]
            else:
                print(f"[{i}/{len(todo)}] {p['name']} — submitting ...", flush=True)
                prompt = PROMPT_TEMPLATE.format(name=p["name"], record=p["record"])
                request_id = _with_retries(lambda: submit_job(prompt, api_key))
                # persist the job id IMMEDIATELY: from here on this job can
                # always be recovered, even if the script dies right now
                con.execute("UPDATE people SET status='submitted', request_id=?, "
                            "updated_at=datetime('now') WHERE id=?",
                            (request_id, p["id"]))
                con.commit()
            data = wait_for_job(request_id, api_key)
            text, cost = extract(data)
            secs = parse_sections(text)
            con.execute(
                """UPDATE people SET status='done', report_md=?, bio_md=?,
                   studies_md=?, teaches_md=?, free_md=?,
                   model=?, cost_estimate=?, error=NULL,
                   updated_at=datetime('now') WHERE id=?""",
                (text, secs["bio_md"], secs["studies_md"], secs["teaches_md"],
                 secs["free_md"], MODEL, cost, p["id"]))
            con.commit()
            mins = (time.time() - t0) / 60
            cost_s = f"~${cost:.2f}" if cost else "n/a"
            parsed = sum(1 for v in secs.values() if v)
            n_links = len(re.findall(r"\[[^\]]+\]\(https?://", text))
            print(f"    done in {mins:.1f} min, {n_links} linked resources, "
                  f"{parsed}/4 sections parsed, est. cost {cost_s}")
        except TimeoutError as e:
            # job still running; leave status='submitted' so it's collected later
            print(f"    still running: {e}")
        except requests.HTTPError as e:
            _fail(con, p["id"], f"HTTP {e.response.status_code}: "
                                f"{e.response.text[:300]}")
        except (requests.RequestException, RuntimeError, KeyError) as e:
            _fail(con, p["id"], str(e)[:300])
    cmd_status()


def _with_retries(fn):
    """Run fn with retries on transient HTTP errors (submission is cheap and
    idempotent-safe to retry because billing only starts once a job runs)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except requests.HTTPError as e:
            code = e.response.status_code
            if code in (429, 500, 502, 503, 504, 520, 521, 522, 524) \
                    and attempt < MAX_RETRIES:
                wait = 90 * attempt
                print(f"    server error {code} on submit, retrying in {wait}s ...")
                time.sleep(wait)
                continue
            raise
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                print(f"    transient error ({e}), retrying ...")
                time.sleep(15 * attempt)
                continue
            raise


def _fail(con, pid, msg):
    print(f"    FAILED: {msg}")
    con.execute("UPDATE people SET status='error', error=?, "
                "updated_at=datetime('now') WHERE id=?", (msg, pid))
    con.commit()


def cmd_retry_errors():
    con = get_db()
    n = con.execute("UPDATE people SET status='pending', error=NULL "
                    "WHERE status='error'").rowcount
    con.commit()
    print(f"Reset {n} errored people to pending.")


# ------------------------------------------------------------------------- status

def cmd_status():
    con = get_db()
    rows = con.execute(
        "SELECT status, COUNT(*) c, COALESCE(SUM(cost_estimate),0) s "
        "FROM people GROUP BY status").fetchall()
    total_cost = 0.0
    print("\nStatus:")
    for r in rows:
        print(f"  {r['status']:8} {r['c']:4}")
        total_cost += r["s"]
    if total_cost:
        print(f"  estimated spend so far: ~${total_cost:.2f}")


# ------------------------------------------------------------------------- export

def cmd_export():
    con = get_db()
    done = con.execute(
        "SELECT * FROM people WHERE status='done' ORDER BY name").fetchall()
    if not done:
        print("No finished reports yet.")
        return
    out_md = Path(__file__).parent / "WCTC_Research_Report.md"
    out_csv = Path(__file__).parent / "wctc_results.csv"
    titles = [("bio_md", "Biography"),
              ("studies_md", "What they studied"),
              ("teaches_md", "Courses & resources they offer"),
              ("free_md", "Free alternatives to learn this")]
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("# World Cup Trading Championships — Winner Deep Research\n\n")
        f.write(f"_{len(done)} people researched with {MODEL}._\n\n---\n\n")
        for p in done:
            f.write(f"# {p['name']}\n\n")
            if all(p[c] is None for c, _ in titles):
                # marker parsing failed for this person: fall back to raw output
                f.write((p["report_md"] or "").strip() + "\n\n")
            else:
                for col, title in titles:
                    f.write(f"## {title}\n\n{(p[col] or '_Not found._').strip()}\n\n")
            f.write("\n---\n\n")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "status", "biography", "studies",
                    "teaches_resources", "free_resources", "cost_estimate"])
        for p in con.execute("SELECT * FROM people ORDER BY name"):
            w.writerow([p["name"], p["status"], p["bio_md"] or "",
                        p["studies_md"] or "", p["teaches_md"] or "",
                        p["free_md"] or "", p["cost_estimate"] or ""])
    print(f"Wrote {out_md.name} ({len(done)} reports) and {out_csv.name}.")


# --------------------------------------------------------------------------- main

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init");   p_init.add_argument("csv")
    p_run = sub.add_parser("run");     p_run.add_argument("--limit", type=int)
    sub.add_parser("status")
    sub.add_parser("retry-errors")
    sub.add_parser("export")
    a = ap.parse_args()
    if a.cmd == "init":
        cmd_init(a.csv)
    elif a.cmd == "run":
        cmd_run(a.limit)
    elif a.cmd == "status":
        cmd_status()
    elif a.cmd == "retry-errors":
        cmd_retry_errors()
    elif a.cmd == "export":
        cmd_export()
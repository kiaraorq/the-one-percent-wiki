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

Return your report as a single JSON object matching the response schema, with \
these six fields. Extract the information and format it as specified in the \
schema — return ONLY the JSON object, no prose before or after it.

"biography":
A CONCISE biography: strictly 1–2 short paragraphs maximum. Who they are, country, \
trading style/instruments, and the essence of their championship record. No lists, \
no links here. Brevity is mandatory for this field only.

"country":
A single country name (e.g. "United States", "Italy", "Spain"). If genuinely \
unverifiable, write exactly: Unknown

"tags":
An array of 4-10 lowercase hyphenated tags for filtering. Choose from this \
vocabulary where applicable — futures, forex, stocks, options, day-trading, \
swing-trading, systematic, algorithmic, discretionary, technical-analysis, \
fundamental, author, educator, fund-manager, multiple-wins — and add any other \
short, hyphenated tags that genuinely describe this person. Only include tags \
supported by your research.

"studies":
The education that made them this good — the goal of this section is that the \
reader can GET THE SAME EDUCATION this trader had. Structure: FIRST a short \
explanation (1-2 paragraphs). THEN — MANDATORY — end the section with a long \
bullet list, MINIMUM 10 entries, more is better, every bullet a markdown \
hyperlink: - [Resource title](https://url) — one-line note on why it matters.
Every link must point to the LEARNING MATERIAL ITSELF: the books they read, the \
institutions or programs they attended, their mentors' published works, papers, \
methodologies, indicator documentation. If person-specific sources run out before \
10 entries, CONTINUE with the canonical learning resources for their trading \
discipline, introduced with "Canonical resources for this discipline:". An \
incomplete list is NOT acceptable; a clearly-labeled discipline-level list is.

"teaches":
Everything this trader has made available for the public to learn from, free or \
paid. Structure: FIRST a short explanation (1-2 paragraphs). THEN — MANDATORY — a \
bullet list, MINIMUM 8 entries, more is better, every bullet a markdown hyperlink \
with a one-line note (include prices when findable). Valid entries: their courses, \
authored books, YouTube channels, newsletters, signal services, educational \
websites, webinars, and interviews or podcast episodes WHERE THEY EXPLAIN THEIR \
METHODS (label those as interviews). If verified offerings run out before 8, \
CONTINUE with educational offerings from other VERIFIED champions of the same \
discipline, introduced with "From other champions of this discipline:". Never \
attribute a course to this person unless verified — but never leave the list short.

"free_resources":
Free ways to learn the same skills this trader studied or teaches. Structure: \
FIRST a short explanation (1-2 paragraphs) of what skills the free roadmap \
covers. THEN — MANDATORY — a long bullet list, MINIMUM 15 entries, more is \
better: free courses, YouTube channels, public-domain books, university OCW, \
broker/exchange education portals, open-source tools. Every bullet a markdown \
hyperlink with a one-line note on what it teaches.

LINK QUALITY RULES — these override everything else:
- Every single link in every list must be something the reader can STUDY FROM: a \
course, book, video channel, paper, documentation, or structured lesson.
- BANNED as list entries (they teach nothing): the World Cup Trading \
Championships website and any of its pages (home, standings, leaderboards, \
historical results, rules); broker/contest promotion pages; social media \
promotional posts (Instagram, Facebook, X); news articles that merely report \
results; generic profile or biography pages. The reader already knows this \
person is a champion — do not link to proof of it.
- No bare URLs, and no citation footnote markers like [1] or [12] inside the \
lists — each bullet stands alone as [Title](url) — note.

Rules:
- Only include real, working links found in your research; NEVER invent URLs. If \
you cannot verify a link, leave it out and find another real one instead.
- The resource lists at the end of each section are the ENTIRE PURPOSE of this \
report. A section without its list is a failed section. Meet every minimum; \
exceed it whenever the evidence allows.
- The fallback tiers exist so honesty and completeness can coexist: never \
fabricate person-specific claims, and never deliver a short list.
- The biography must stay short; the other three sections should be extensive.
"""

SECTION_KEYS = {          # marker -> db column
    "BIOGRAPHY": "bio_md",
    "COUNTRY": "country",
    "TAGS": "tags",
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
            full_response TEXT,             -- complete raw API response (always saved)
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
    for col in ("bio_md", "studies_md", "teaches_md", "free_md", "request_id",
                "country", "tags", "full_response"):
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


# tags derivable for free from the Event column of the CSV
EVENT_TAG_RULES = [
    ("forex", "forex"),
    ("futures", "futures"),
    ("day trading", "day-trading"),
    ("stock", "stocks"),
    ("option", "options"),
    ("global cup", "global-cup"),
    ("quarterly", "quarterly"),
]


def tags_from_events(events) -> set:
    tags = set()
    for ev in events:
        low = ev.lower()
        for needle, tag in EVENT_TAG_RULES:
            if needle in low:
                tags.add(tag)
    return tags


def merge_tags(existing: str | None, new: set) -> str:
    cur = {t.strip().lower() for t in (existing or "").split(",") if t.strip()}
    return ",".join(sorted(cur | {t.lower() for t in new}))


# --------------------------------------------------------------------------- init

def cmd_init(csv_path: str):
    con = get_db()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    people = {}
    events = {}
    for r in rows:
        name = normalize_name(r["Name"])
        line = f"- {r['Year']}: {r['Event']} — {r['Place']} place, {r['Percentage']}% net return"
        people.setdefault(name, [])
        events.setdefault(name, set()).add(r["Event"])
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
        # event-derived tags are free: merge them for EVERYONE (never removes
        # tags, so research-derived tags on finished people are preserved)
        row = con.execute("SELECT tags FROM people WHERE name=?", (name,)).fetchone()
        con.execute("UPDATE people SET tags=? WHERE name=?",
                    (merge_tags(row["tags"], tags_from_events(events[name])), name))
    con.commit()
    total = con.execute("SELECT COUNT(*) c FROM people").fetchone()["c"]
    print(f"Loaded {len(rows)} rows -> {total} unique people ({added} new). "
          f"Event-derived tags applied to everyone.")


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


RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "trader_research_report",
        "schema": {
            "type": "object",
            "properties": {
                "biography":     {"type": "string",
                                  "description": "Concise 1-2 paragraph biography, markdown, no links"},
                "country":       {"type": "string",
                                  "description": "Single country name, or 'Unknown'"},
                "tags":          {"type": "array", "items": {"type": "string"},
                                  "description": "4-10 lowercase hyphenated filtering tags"},
                "studies":       {"type": "string",
                                  "description": "Markdown: short explanation then long bullet list of hyperlinked learning resources"},
                "teaches":       {"type": "string",
                                  "description": "Markdown: short explanation then long bullet list of everything this person offers, hyperlinked"},
                "free_resources": {"type": "string",
                                  "description": "Markdown: short explanation then long bullet list of free hyperlinked resources"},
            },
            "required": ["biography", "country", "tags",
                         "studies", "teaches", "free_resources"],
        },
    },
}


def submit_job(prompt: str, api_key: str) -> str:
    """Submit an async deep research job; returns the request_id immediately."""
    payload = {"model": MODEL,
               "reasoning_effort": REASONING_EFFORT,
               "response_format": RESPONSE_SCHEMA,
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


def parse_json_response(text: str) -> dict | None:
    """Parse a structured-output JSON response into the section columns.
    Tolerates <think> blocks (already stripped), markdown code fences, and
    stray prose around the object. Returns None if no valid JSON is found."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)           # code fences
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    clean = lambda v: re.sub(r"\[\d{1,3}\](?!\()", "", v).strip() or None \
        if isinstance(v, str) else None
    tags = obj.get("tags")
    if isinstance(tags, list):
        tags = ",".join(str(t) for t in tags if str(t).strip())
    elif not isinstance(tags, str):
        tags = None
    sections = {
        "bio_md": clean(obj.get("biography")),
        "country": clean(obj.get("country")),
        "tags": tags,
        "studies_md": clean(obj.get("studies")),
        "teaches_md": clean(obj.get("teaches")),
        "free_md": clean(obj.get("free_resources")),
    }
    big = [sections["studies_md"], sections["teaches_md"], sections["free_md"]]
    sections["_parse_ok"] = any(big)
    # same sanity guards as the marker parser
    c = sections.get("country")
    if c and (len(c) > 60 or "\n" in c.strip()):
        sections["country"] = None
    t2 = sections.get("tags")
    if t2 and (len(t2) > 200 or t2.count(",") > 20):
        sections["tags"] = None
    return sections


def parse_response(text: str) -> dict:
    """Preferred entry point: try structured JSON first (the format the API
    now enforces), fall back to legacy <<<MARKER>>> parsing for old responses."""
    parsed = parse_json_response(text)
    if parsed and parsed["_parse_ok"]:
        return parsed
    return parse_sections(text)


def parse_sections(text: str) -> dict:
    """Split the model output on <<<MARKER>>> lines into the db columns.

    Returns a dict of the section columns PLUS a special "_parse_ok" flag.
    If the delimiter markers are missing or too few were found, _parse_ok is
    False and the metadata fields (country/tags) are left empty rather than
    being filled with misassigned report text — so a malformed response can
    never dump the whole report into the tags column again."""
    sections = {v: None for v in SECTION_KEYS.values()}
    pattern = r"<{2,3}\s*(" + "|".join(SECTION_KEYS) + r")\s*>{2,3}"
    parts = re.split(pattern, text)
    markers_found = (len(parts) - 1) // 2
    for i in range(1, len(parts) - 1, 2):
        key = SECTION_KEYS.get(parts[i].strip())
        if key:
            content = parts[i + 1].strip()
            content = re.sub(r"^#+\s*[^\n]*\n+", "", content, count=1) \
                if re.match(r"^#+\s", content) else content
            content = re.sub(r"\[\d{1,3}\](?!\()", "", content)
            sections[key] = content.strip() or None

    # a valid report has all 6 markers; require at least the 3 big sections
    big = [sections["studies_md"], sections["teaches_md"], sections["free_md"]]
    sections["_parse_ok"] = markers_found >= 3 and any(big)

    # sanity guards: country is one short line, tags is one short line. If the
    # model dumped prose into them (the old corruption), discard rather than store.
    c = sections.get("country")
    if c and (len(c) > 60 or "\n" in c.strip()):
        sections["country"] = None
    t = sections.get("tags")
    if t and (len(t) > 200 or t.count(",") > 20):
        sections["tags"] = None
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
            # FIRST THING after the response arrives: save the complete raw API
            # response to full_response, before any parsing that could fail.
            # This row now permanently holds exactly what sonar returned.
            con.execute("UPDATE people SET full_response=?, "
                        "updated_at=datetime('now') WHERE id=?",
                        (json.dumps(data, ensure_ascii=False), p["id"]))
            con.commit()
            text, cost = extract(data)
            secs = parse_response(text)
            if not secs["_parse_ok"]:
                # The model didn't emit usable section markers. The raw response
                # is already stored in full_response above; keep the cleaned text
                # in report_md too, and mark the row 'error' so it shows up in
                # retry-errors and is NEVER treated as a good report.
                con.execute("""UPDATE people SET status='error', report_md=?,
                               error='unparseable: model omitted section markers',
                               cost_estimate=?, updated_at=datetime('now')
                               WHERE id=?""", (text, cost, p["id"]))
                con.commit()
                print(f"    PARSE FAILED — model omitted markers; raw saved to "
                      f"full_response, marked error (retry with `retry-errors`). "
                      f"est. cost ~${cost:.2f}" if cost else
                      "    PARSE FAILED — raw saved to full_response, marked error")
                continue
            # clean the metadata fields: country is one line; model tags are
            # normalized and MERGED with the event-derived tags from init
            country = (secs.get("country") or "").strip().splitlines()
            country = country[0].strip(" .") if country else None
            model_tags = {t.strip().lower().replace(" ", "-")
                          for t in (secs.get("tags") or "").replace("\n", ",").split(",")
                          if t.strip() and len(t.strip()) <= 40}
            tags = merge_tags(p["tags"], model_tags)
            con.execute(
                """UPDATE people SET status='done', report_md=?,
                   bio_md=?, country=?, tags=?,
                   studies_md=?, teaches_md=?, free_md=?,
                   model=?, cost_estimate=?, error=NULL,
                   updated_at=datetime('now') WHERE id=?""",
                (text, secs["bio_md"], country, tags,
                 secs["studies_md"], secs["teaches_md"],
                 secs["free_md"], MODEL, cost, p["id"]))
            con.commit()
            mins = (time.time() - t0) / 60
            cost_s = f"~${cost:.2f}" if cost else "n/a"
            counts = section_link_counts(secs)
            thin_flag = any(n < MIN_LINKS[c] for c, n in counts.items())
            print(f"    done in {mins:.1f} min — links: "
                  f"studies {counts['studies_md']}, teaches {counts['teaches_md']}, "
                  f"free {counts['free_md']} — est. cost {cost_s}"
                  + ("  << THIN: below minimums, see `thin` command" if thin_flag else ""))
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


def cmd_requeue(names: list[str]):
    """Reset specific people to pending so the next `run` re-researches them
    (their old report is discarded). Use after updating the CSV with new wins
    for someone already researched. Matches names case-insensitively."""
    con = get_db()
    total = 0
    for raw in names:
        name = normalize_name(raw)
        n = con.execute(
            """UPDATE people SET status='pending', report_md=NULL, bio_md=NULL,
               studies_md=NULL, teaches_md=NULL, free_md=NULL, request_id=NULL,
               error=NULL, updated_at=datetime('now')
               WHERE lower(name)=lower(?)""", (name,)).rowcount
        if n:
            print(f"re-queued: {name}")
            total += n
        else:
            print(f"not found: {name}")
    con.commit()
    if total:
        print(f"\n{total} re-queued. Now run `init your.csv` to refresh their win "
              "records, then `run`. Note: re-researching costs credits again.")


# ------------------------------------------------------------------------- status

MIN_LINKS = {"studies_md": 10, "teaches_md": 8, "free_md": 15}

# link targets that teach nothing — flagged by the `thin` audit
JUNK_LINK_PATTERNS = re.compile(
    r"https?://[^)\s]*("
    r"worldcupchampionships\.com"
    r"|instagram\.com|facebook\.com|twitter\.com|x\.com/(?!.*status.*video)"
    r"|tiktok\.com"
    r")", re.IGNORECASE)


def section_link_counts(row) -> dict:
    return {col: len(re.findall(r"\[[^\]]+\]\(https?://", row[col] or ""))
            for col in MIN_LINKS}


def junk_link_count(row) -> int:
    text = " ".join((row[c] or "") for c in MIN_LINKS)
    return len(JUNK_LINK_PATTERNS.findall(text))


def cmd_thin(limit: int | None = None):
    """List finished people whose resource lists are below the required
    minimums, worst first, and print a ready-to-copy requeue command.
    With --limit N, show only the N worst so they can be fixed in batches."""
    con = get_db()
    thin = []
    for p in con.execute("SELECT * FROM people WHERE status='done' ORDER BY name"):
        counts = section_link_counts(p)
        junk = junk_link_count(p)
        deficit = sum(max(0, MIN_LINKS[c] - n) for c, n in counts.items()) + junk
        if deficit > 0:
            thin.append((deficit, p["name"], counts, junk))
    if not thin:
        print("All finished reports meet the resource-list minimums "
              f"(studies>={MIN_LINKS['studies_md']}, teaches>={MIN_LINKS['teaches_md']}, "
              f"free>={MIN_LINKS['free_md']}) and contain no junk links.")
        return
    thin.sort(key=lambda t: (-t[0], t[1]))       # biggest deficit first
    total = len(thin)
    if limit:
        thin = thin[:limit]
    label = f"showing worst {len(thin)} of {total}" if limit and total > len(thin) \
        else f"{total} report(s)"
    print(f"{label} needing attention (studies/teaches/free links, junk links, score):\n")
    for deficit, name, c, junk in thin:
        junk_s = f"  junk:{junk}" if junk else ""
        print(f"  {name:35} {c['studies_md']:>3} / {c['teaches_md']:>3} / "
              f"{c['free_md']:>3}{junk_s}   (-{deficit})")
    names = " ".join(f'"{n}"' for _, n, _, _ in thin)
    print(f"\nTo re-research {'these ' + str(len(thin)) if limit else 'them'} "
          f"(costs credits):\n"
          f"  python wctc_research.py requeue {names}\n"
          f"  python wctc_research.py run")


def cmd_repair():
    """Re-parse the stored raw report_md for every finished person using the
    current parser, and fix rows corrupted by the old parser (e.g. report text
    dumped into the tags column). Costs nothing — no API calls. Rows whose raw
    report has no usable markers are moved to 'error' so `retry-errors` can
    re-research them."""
    con = get_db()
    rows = con.execute("SELECT * FROM people WHERE status='done'").fetchall()
    fixed = broken = 0
    for p in rows:
        raw = p["report_md"] or p["full_response"]
        # detect corruption: giant tags field, or missing big sections
        corrupt = (p["tags"] and (len(p["tags"]) > 200 or p["tags"].count(",") > 20)) \
            or not (p["studies_md"] or p["teaches_md"] or p["free_md"])
        if not corrupt:
            continue
        if not raw:
            con.execute("UPDATE people SET status='error', "
                        "error='corrupt row, no raw report to reparse' WHERE id=?",
                        (p["id"],))
            broken += 1
            continue
        secs = parse_response(raw)
        if not secs["_parse_ok"]:
            con.execute("UPDATE people SET status='error', tags=?, "
                        "error='unparseable on reparse: model omitted markers' "
                        "WHERE id=?", (p["tags"] if p["tags"] and len(p["tags"]) < 200 else None, p["id"]))
            broken += 1
            continue
        country = (secs.get("country") or "").strip().splitlines()
        country = country[0].strip(" .") if country else None
        model_tags = {t.strip().lower().replace(" ", "-")
                      for t in (secs.get("tags") or "").replace("\n", ",").split(",")
                      if t.strip() and len(t.strip()) <= 40}
        # re-merge with clean event tags derived from the record
        ev_tags = tags_from_events(
            {ln.split("—")[0].split(":",1)[1].strip()
             for ln in (p["record"] or "").splitlines() if ":" in ln and "—" in ln})
        tags = merge_tags(",".join(sorted(ev_tags | model_tags)), set())
        con.execute("""UPDATE people SET bio_md=?, country=?, tags=?, studies_md=?,
                       teaches_md=?, free_md=?,
                       full_response=COALESCE(full_response, report_md)
                       WHERE id=?""",
                    (secs["bio_md"], country, tags, secs["studies_md"],
                     secs["teaches_md"], secs["free_md"], p["id"]))
        fixed += 1
    con.commit()
    print(f"Repair complete: {fixed} row(s) re-parsed and fixed, "
          f"{broken} moved to 'error' for re-research (run `retry-errors` then `run`).")
    if fixed or broken:
        print("Then run `export-json` to refresh the site.")


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
        w.writerow(["name", "status", "country", "tags", "biography", "studies",
                    "teaches_resources", "free_resources", "cost_estimate"])
        for p in con.execute("SELECT * FROM people ORDER BY name"):
            w.writerow([p["name"], p["status"], p["country"] or "",
                        p["tags"] or "", p["bio_md"] or "",
                        p["studies_md"] or "", p["teaches_md"] or "",
                        p["free_md"] or "", p["cost_estimate"] or ""])
    print(f"Wrote {out_md.name} ({len(done)} reports) and {out_csv.name}.")


def cmd_export_json():
    """Export the database to wctc_data.json (pure JSON) and wctc_data.js
    (same data wrapped for the dashboard, so it works when opened as a local
    file without any web server)."""
    con = get_db()
    # backfill: rows saved before full_response was always stored copy it
    # from report_md so exports always carry the raw response
    con.execute("UPDATE people SET full_response = report_md "
                "WHERE full_response IS NULL AND report_md IS NOT NULL")
    con.commit()
    people = []
    win_re = re.compile(r"^- (?P<year>[\d\-]+): (?P<event>.+) — (?P<place>\S+) "
                        r"place, (?P<pct>[\d.,]+)% net return$")
    for p in con.execute("SELECT * FROM people ORDER BY name"):
        wins = []
        for line in (p["record"] or "").splitlines():
            m = win_re.match(line.strip())
            if m:
                wins.append({"year": m["year"], "event": m["event"],
                             "place": m["place"],
                             "pct": float(m["pct"].replace(",", ""))})
        people.append({
            "name": p["name"], "status": p["status"], "wins": wins,
            "country": p["country"],
            "tags": [t for t in (p["tags"] or "").split(",") if t],
            "bio": p["bio_md"], "studies": p["studies_md"],
            "teaches": p["teaches_md"], "free": p["free_md"],
            "report": p["report_md"],   # full raw model response, always exported
            "cost": p["cost_estimate"], "updated_at": p["updated_at"],
        })
    data = {"generated_at": time.strftime("%Y-%m-%d %H:%M"),
            "model": MODEL, "people": people}
    base = Path(__file__).parent
    (base / "wctc_data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (base / "wctc_data.js").write_text(
        "window.WCTC_DATA = " + json.dumps(data, ensure_ascii=False) + ";",
        encoding="utf-8")
    n_done = sum(1 for x in people if x["status"] == "done")
    print(f"Wrote wctc_data.json and wctc_data.js "
          f"({len(people)} people, {n_done} researched). "
          f"Open index.html to view The One Percent.")


# --------------------------------------------------------------------------- main

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init");   p_init.add_argument("csv")
    p_run = sub.add_parser("run");     p_run.add_argument("--limit", type=int)
    sub.add_parser("status")
    sub.add_parser("retry-errors")
    sub.add_parser("repair")
    p_thin = sub.add_parser("thin")
    p_thin.add_argument("--limit", type=int,
                        help="show only the N worst reports")
    sub.add_parser("export")
    sub.add_parser("export-json")
    p_rq = sub.add_parser("requeue")
    p_rq.add_argument("names", nargs="+",
                      help='person names to re-research, e.g. requeue "Bret Miller"')
    a = ap.parse_args()
    if a.cmd == "init":
        cmd_init(a.csv)
    elif a.cmd == "run":
        cmd_run(a.limit)
    elif a.cmd == "status":
        cmd_status()
    elif a.cmd == "retry-errors":
        cmd_retry_errors()
    elif a.cmd == "repair":
        cmd_repair()
    elif a.cmd == "thin":
        cmd_thin(a.limit)
    elif a.cmd == "requeue":
        cmd_requeue(a.names)
    elif a.cmd == "export":
        cmd_export()
    elif a.cmd == "export-json":
        cmd_export_json()
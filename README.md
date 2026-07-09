# WCTC Deep Research App

Researches every unique person in your World Cup Trading Championships table using Perplexity's most capable research model (`sonar-deep-research` via its async API, reasoning effort high). Each person gets four markdown fields stored in a local SQLite database (`wctc.db`):

- **biography** — concise, 1–2 paragraphs
- **studies** — short explanation, then a long bullet list of hyperlinked resources
- **teaches_resources** — short explanation, then every course/book/channel they offer, hyperlinked
- **free_resources** — short explanation, then a long hyperlinked list of free alternatives

---

## 1. One-time setup

```bash
pip install requests
```

Rename `.env.example` to `.env` and put your real key inside:

```
PPLX_API_KEY=pplx-your-key-here
```

Get the key at https://www.perplexity.ai/account/api (add a payment method and buy API credits there — note API credits are separate from Pro/Max subscription credits). **Set a hard spending limit in the API billing dashboard before big runs.** Deep research commonly costs ~$0.40–$1.50+ per person; 254 people can total $150–$400+.

## 2. Load your CSV

Put your CSV in the same folder as the script (any filename works), then:

```bash
python wctc_research.py init your_file.csv
```

The CSV must have the columns: `Year, Event, Place, Name, Percentage`. The script deduplicates rows into unique people and stores each person's full win record. `init` is **always safe to re-run** — it never deletes anything and never duplicates people.

## 3. Test with one person first

```bash
python wctc_research.py run --limit 1
```

You'll see it submit the job, then print a heartbeat every minute while researching (jobs take ~5–30 min). When it finishes, run `export` and check you like the format before spending on all 254. Ctrl+C is always safe: submitted jobs keep running on Perplexity's servers and are collected on the next `run` without paying again.

## 4. Full run

```bash
python wctc_research.py run
```

Leave it going — it's a multi-day background job. Useful commands (safe to use from a second terminal while it runs):

```bash
python wctc_research.py status        # done/pending/error counts + spend estimate
python wctc_research.py export        # write the report .md and results .csv so far
python wctc_research.py retry-errors  # re-queue everyone whose job failed
```

## 5. Read the results

`export` produces:
- **WCTC_Research_Report.md** — all reports in one document (open in VS Code preview, Obsidian, Typora...)
- **wctc_results.csv** — one row per person, one column per section (opens in Excel/Google Sheets)

To browse the raw database, install DB Browser for SQLite (https://sqlitebrowser.org) and open `wctc.db`.

## 6. The dashboard

A visual way to browse everything, no server needed:

```bash
python wctc_research.py export-json
```

This writes `wctc_data.js` (and `wctc_data.json`). Keep `dashboard.html` in the same folder and double-click it — it opens in your browser. You get a searchable leaderboard of all traders sorted by best return, filters for researched/not-yet, and a full report view per person: win record, biography, and the three resource sections with clickable links.

Whenever new research finishes, run `export-json` again and refresh the page. (Internet is needed the first time the page loads, to fetch fonts and the React library.)

---

## Updating your CSV later

**New people added to the CSV?** Just re-run:

```bash
python wctc_research.py init updated_file.csv
python wctc_research.py run
```

Only the new names get researched; finished people are skipped (no re-spend).

**An already-researched person got NEW wins?** By default their old report is kept untouched (the safe choice — no surprise charges). To regenerate their report with the updated record, use `requeue`:

```bash
python wctc_research.py requeue "Bret Miller" "David Trullas Vila"
python wctc_research.py init updated_file.csv
python wctc_research.py run
```

What `requeue` does, step by step:
1. You pass it one or more names, each wrapped in quotes (quotes matter because names contain spaces — without them the shell would treat `Bret` and `Miller` as two different people). Matching is case-insensitive: `"bret miller"` works.
2. It marks those people as `pending` again and discards their old report.
3. `init` then refreshes their win record from your updated CSV.
4. `run` re-researches only them. **This costs credits again** — that's why it never happens automatically.

---

## Command reference

| Command | What it does |
|---|---|
| `init <file.csv>` | Load/refresh the CSV into the database. Safe to re-run anytime. |
| `run` | Research everyone pending (async submit + poll, resumable). |
| `run --limit N` | Research only the next N people (use `--limit 1` to test). |
| `status` | Progress counts and estimated spend. |
| `export` | Write `WCTC_Research_Report.md` + `wctc_results.csv` from finished people. |
| `export-json` | Write `wctc_data.js`/`.json` for the dashboard (`dashboard.html`). |
| `retry-errors` | Re-queue all failed people. |
| `requeue "Name" ...` | Re-queue specific people by name to re-research them (costs credits). |

## Cost tuning

- `REASONING_EFFORT` in the script: `"high"` = maximum depth. `"medium"` roughly halves cost per query.
- Your hard spending cap in the Perplexity dashboard is the ultimate safety net: if hit, jobs pause, nothing is lost, and the script resumes once you add credits.
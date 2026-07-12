# WCTC Deep Research App

Researches every unique person in your World Cup Trading Championships table using Perplexity's most capable research model (`sonar-deep-research` via its async API, reasoning effort high). Each person gets these fields stored in a local SQLite database (`wctc.db`):

- **biography** — concise, 1–2 paragraphs
- **country** — found during research ("Unknown" if unverifiable)
- **tags** — comma-separated filtering tags from two sources: discipline tags derived automatically from your CSV's Event column (free, applied to everyone at `init`), merged with style tags found during research (systematic, discretionary, author, educator, multiple-wins...)
- **studies** — short explanation, then a long bullet list of hyperlinked resources
- **teaches_resources** — short explanation, then every course/book/channel they offer, hyperlinked
- **free_resources** — short explanation, then a long hyperlinked list of free alternatives

Country and tags appear in the CSV export, the JSON export, and the dashboard — where tags become clickable filter chips and search also matches countries and tags. People researched before this feature exists will have event-derived tags but no country/style tags until re-researched (`requeue`).

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

## 6. The website — "The One Percent"

A multi-page site to browse everything visually, no server needed:

```bash
python wctc_research.py export-json
```

This writes `wctc_data.js` (and `wctc_data.json`). Keep these site files in the same folder and double-click **index.html**:

- **index.html** — home: headline stats and the all-time podium
- **podiums.html** — top-3 podium graphics filtered by market category (futures / forex / stocks / options) and by year
- **insights.html** — which trading strategies champions actually use, most common tags, and countries (built from completed research)
- **explorer.html** — the full table: filter by year, trading style, place, and country, search names/bios/tags, and click any row to read the complete research report
- **styles.css / app-common.js** — shared styling and helpers (keep them next to the pages)

Whenever new research finishes, run `export-json` again and refresh. (Internet is needed on first load for fonts and icons.)

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
| `repair` | Re-parse stored reports to fix rows corrupted by an old parser (e.g. report text dumped into tags). Free — no API calls. Unrecoverable rows are moved to `error` for re-research. |
| `thin` | List finished reports whose resource lists are below the minimums, worst first, with a ready-to-copy requeue command. |
| `thin --limit N` | Same, but only the N worst reports — fix thin reports in affordable batches. |
| `requeue "Name" ...` | Re-queue specific people by name to re-research them (costs credits). |

## Cost tuning

- `REASONING_EFFORT` in the script: `"high"` = maximum depth. `"medium"` roughly halves cost per query.
- Your hard spending cap in the Perplexity dashboard is the ultimate safety net: if hit, jobs pause, nothing is lost, and the script resumes once you add credits.

## Quality control: the resource lists

Every section must end with a hyperlinked resource list meeting these minimums: **studies ≥ 10 links, teaches ≥ 8, free resources ≥ 15** (configurable via `MIN_LINKS` at the top of the script). Each section's links serve a distinct purpose: **studies** links point to the actual learning material the trader learned from (books, programs, mentors' works — so you can get the same education); **teaches** links point to everything the trader made available to the public, free or paid; **free resources** links point to free ways to learn the same skills. For obscure traders the model fills lists with clearly-labeled discipline-level resources instead of leaving them short — always real links, never invented ones.

Links that teach nothing are banned from lists: the championship's own website, standings/leaderboard pages, social-media promos, and news that merely reports results. The `thin` audit also detects these junk links in finished reports (shown as `junk:N`).

While a batch runs, each finished report prints its per-section link counts, flagged with `<< THIN` if any section is below minimum. To audit everything already researched:

```bash
python wctc_research.py thin             # every report needing attention, worst first
python wctc_research.py thin --limit 10  # only the 10 worst
```

Reports are sorted by score (missing links + junk links, shown as `(-N)` per row), so the worst offenders come first. Each run prints the exact `requeue` command for the reports shown — with `--limit`, that command covers only those N, letting you fix the worst in small, affordable batches (re-research one batch, run `thin` again, repeat). Re-researching costs credits, so it never happens automatically — you decide.
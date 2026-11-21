# Job Monitor Agent

An automated job search agent for SWE roles, powered by GitHub Actions and Claude (Anthropic).

## What it does

**Daily digest (12pm CST)** — Checks all tracked company career pages and job boards, scores each
role against a candidate profile using Claude, then emails a ranked digest sorted by fit score.

**Hourly alerts** — Silently checks all sources every hour and sends an alert only when page
content changes (indicating new postings). AI scoring is off by default for hourly runs; pass
`--use-ai` to enable it.

Page-hash state is persisted across runs via Actions cache, so change detection actually tracks
diffs over time rather than starting fresh each run.

---

## AI fit scoring

Each role found is scored 1–100 by Claude Haiku using the candidate's resume. Each job is scored
once; the score is saved in state and reused by later runs. Job descriptions are stripped of
EEO/legal and benefits boilerplate before scoring, and the shared instructions + resume are sent as a
cached prompt prefix. Jobs in the daily digest are grouped into three tiers:

- **Qualifying** — matched by role-classification regex
- **Likely Qualifies** — Claude inferred the candidate likely meets requirements
- **Review** — no strong signal either way

Within each tier, jobs are sorted: fit score (high → low) → flagged companies → recency.

The daily digest opens with a **Top 3 Picks** banner from a single Claude Sonnet call over the
best-scored jobs. Token usage per run is printed in the log and kept in `ai_usage_log` in the state file.

---

## Tracked companies

27 companies across tech, finance, defense, and industrial sectors. See
`job_monitor_agent/job_monitor.py` for the full list.

## Job boards

12 searches across LinkedIn, Indeed, Dice, Wellfound, and BuiltIn targeting
entry-level / new-grad SWE roles posted within the last 24 hours.

---

## Configuration

Credentials are stored as encrypted GitHub Actions secrets:

| Secret | Purpose |
|---|---|
| `NOTIFY_EMAIL` | Recipient address for all alerts |
| `SMTP_USER` | Gmail address for outbound email |
| `SMTP_PASS` | Gmail app password |
| `ANTHROPIC_API_KEY` | Claude API key for fit scoring |
| `RESUME_TEXT` | Plain-text resume for scoring context |
| `CANDIDATE_PROFILE` | One-line profile shown in the email footer |

---

## Auto-fix

If a monitor run fails, `auto_fix.yml` asks Claude for a minimal set of search/replace edits to
`job_monitor.py`, applies and validates them, then opens a pull request on an `auto-fix/run-N` branch. A second job runs
[Claude Code Action](https://github.com/anthropics/claude-code-action) to review the PR and post
a verdict as comments, using the workflow's built-in `GITHUB_TOKEN` (no GitHub App needed). Nothing is merged automatically. Only one auto-fix PR is open at a time.
An email is sent whether or not a fix was produced.

One-time setup: enable **Settings → Actions → General → Allow GitHub Actions to create and
approve pull requests**.

---

## Running it

### Command-line flags

| Flag | What it does | AI scoring |
|---|---|---|
| `--scheduled` | Daily digest: every job not yet sent in a digest, ranked, with Top Picks | Always on |
| `--hourly` | Change detection: emails only if new qualifying/review jobs were found | Off unless `--use-ai` |
| `--use-ai` | Turns on Claude fit scoring for hourly or on-demand runs (Haiku) | — |
| *(no flag)* | On-demand run: checks everything and always sends an email | Off unless `--use-ai` |

```bash
cd job_monitor_agent
./job_monitor_run.sh --scheduled          # daily digest
./job_monitor_run.sh --hourly             # hourly check, no AI
./job_monitor_run.sh --hourly --use-ai    # hourly check with fit scores
./job_monitor_run.sh --use-ai             # on-demand run with fit scores
```

`job_monitor_run.sh` sets the credentials and picks the Python interpreter (see
[Local Mac runs](#local-mac-runs-launchd)). Hourly AI scoring is skipped during quiet hours
(12am–7am CST), and a job scored by an hourly run is not scored again by the digest.

### GitHub Actions

**Manual run:** **Actions → Job Monitor → Run workflow**, pick `scheduled` (daily digest with AI
scoring) or `hourly`. For `hourly`, tick **use_ai** to add `--use-ai`.

**AI on every scheduled hourly run:** **Settings → Secrets and variables → Actions → Variables
→ New repository variable**, name `HOURLY_USE_AI`, value `true`. Delete it (or set `false`) to
turn it off. This is a plain variable, not a secret, because it holds no private data.

## Local Mac runs (launchd)

Copy `job_monitor_agent/job_monitor_run.sh.template` to `job_monitor_agent/job_monitor_run.sh`,
fill in your credentials, and load the plists with `launchctl load`.

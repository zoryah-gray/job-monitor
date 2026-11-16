# Job Monitor

Automated job search monitor for new SWE roles. Runs entirely on GitHub Actions — no local machine or app required.

## What it does

**Daily digest (12pm CST)** — Checks all tracked company career pages and job boards, then emails a full status report regardless of whether anything changed.

**Hourly alerts** — Silently checks all sources every hour and sends an email only when page content changes (indicating new postings). Quiet hours apply: no emails sent between 12am–7am CST.

Both jobs persist page-hash state across runs via Actions cache, so the hourly change detection actually tracks diffs over time rather than starting fresh each run.

---

## Tracked Companies (22)

| Company | Role Focus |
|---|---|
| ⚡ **Accenture** | AI Native Software Engineer (priority watch) |
| Adobe | Software Engineer |
| Amazon | Software Engineer — Full Time |
| Bank of America | Software Engineer |
| Bechtel | Software Engineer |
| Brunswick | Software Engineer |
| Capital One | Software Engineer |
| Caterpillar (CAT) | Software Engineer |
| EcoLab | Software Engineer |
| Epic Systems | All open roles |
| GE Healthcare | Software Engineer |
| General Dynamics | Software Engineer |
| Honeywell | Software Engineer |
| IBM | Software Engineer |
| Intel | Software Engineer |
| Apple | All US roles |
| Medtronic | Software Engineer |
| Microsoft | Software Engineer — Students & Graduates |
| P&G | Software Engineer |
| Polaris | Information Technology roles |
| United Airlines | Software Engineer |
| UPS | Software Engineer |

---

## Job Boards (12 searches)

| Board | Search |
|---|---|
| LinkedIn | New Grad SWE — last 24h |
| LinkedIn | Entry Level SWE — last 24h |
| LinkedIn | Early Career SWE — last 24h |
| LinkedIn | Associate SWE / 0–2yr — last 24h |
| LinkedIn Posts | #newgrad #softwareengineer hiring |
| LinkedIn Posts | #earlycareerswe hiring |
| Indeed | Entry Level SWE — last 24h |
| Indeed | New Grad SWE — last 24h |
| Indeed | 0–2 Years Experience SWE — last 24h |
| Dice | SWE Full Time — last 24h |
| Wellfound / AngelList | Startup SWE roles |
| BuiltIn | Entry Level SWE |

---

## Configuration

Credentials are stored as encrypted Actions secrets (never in code):

| Secret | Purpose |
|---|---|
| `NOTIFY_EMAIL` | Recipient address for all alerts |
| `SENDGRID_API_KEY` | SendGrid API key for outbound email |

SendGrid trial expires **January 15, 2027**. The script will warn 30 days out. To switch to Gmail after expiry, add `SMTP_USER` and `SMTP_PASS` secrets and remove `SENDGRID_API_KEY`.

---

## Manual trigger

Go to **Actions → Job Monitor → Run workflow** and pick `scheduled` (sends full digest) or `hourly` (sends only if changes detected).

## Candidate profile

Full stack SWE · Python, JavaScript/TypeScript, React, AWS, PostgreSQL · open to relocation · available immediately

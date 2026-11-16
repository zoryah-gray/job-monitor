#!/usr/bin/env python3
"""
Job Monitor v2 — Playwright-based, reads actual JD content, classifies by experience.

Classifies each job by what the description SAYS, not the search filter label:
  ✅ qualify     — JD or title explicitly says entry level / new grad / 0-2 yr
  🔍 review      — no clear experience signal (manual check needed)
  ✗  disqualify  — JD or title clearly requires 3+ yr or is senior/principal/lead/director

Run:
  python3 job_monitor.py              # on-demand (always sends email)
  python3 job_monitor.py --scheduled  # 12pm CST digest label
  python3 job_monitor.py --hourly     # sends only if new qualify/review jobs found

Quiet hours: no emails 12am–7am CST regardless of mode.
"""

import asyncio
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

try:
    from playwright.async_api import async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# ── Constants ───────────────────────────────────────────────────────────────────
CST             = timezone(timedelta(hours=-6))
NOTIFY_EMAIL    = os.environ.get("NOTIFY_EMAIL", "")
STATE_FILE      = Path(__file__).parent / "job_monitor_state.json"
MAX_JOBS_DAILY  = 25   # max new JD pages to deep-crawl per source, full run
MAX_JOBS_HOURLY = 10   # max new JD pages to deep-crawl per source, hourly run
MAX_STATE_URLS  = 2000 # cap on seen_job_urls entries in state
SENDGRID_EXPIRY = datetime(2027, 1, 15, tzinfo=CST)

# ── Company career sites ─────────────────────────────────────────────────────────
COMPANIES = [
    {"name": "Accenture",
     "url":  "https://www.accenture.com/us-en/careers/jobsearch?jk=software+engineer+AI+native&cl=us",
     "note": "⚡ AI Native SWE role expected ~Nov 15-16 with Feb start — check manually"},
    {"name": "Adobe",
     "url":  "https://careers.adobe.com/us/en/search-results?keywords=software%20engineer&country=United%20States"},
    {"name": "Amazon",
     "url":  "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=United+States&job_type=Full-Time"},
    {"name": "Apple",
     "url":  "https://jobs.apple.com/en-us/search?location=united-states-USA"},
    {"name": "Bank of America",
     "url":  "https://careers.bankofamerica.com/en-us/search-jobs?keywords=software+engineer&country=United%20States"},
    {"name": "Bechtel",
     "url":  "https://www.bechtel.com/careers/jobs/?q=software+engineer"},
    {"name": "Brunswick",
     "url":  "https://careers.brunswick.com/us/en/search-results?keywords=software+engineer"},
    {"name": "Capital One",
     "url":  "https://www.capitalonecareers.com/search-jobs?keywords=software+engineer&country=United%20States"},
    {"name": "Caterpillar (CAT)",
     "url":  "https://careers.caterpillar.com/en/jobs/?keyword=software+engineer&country=United%20States"},
    {"name": "EcoLab",
     "url":  "https://jobs.ecolab.com/global/en/search-results?keywords=software%20engineer"},
    {"name": "Epic Systems",
     "url":  "https://careers.epic.com/Jobs"},
    {"name": "GE Healthcare",
     "url":  "https://careers.gehealthcare.com/global/en/search-results?keywords=software+engineer"},
    {"name": "General Dynamics",
     "url":  "https://www.gd.com/careers?keyword=software+engineer"},
    {"name": "Honeywell",
     "url":  "https://careers.honeywell.com/us/en/search-results?keywords=software+engineer"},
    {"name": "IBM",
     "url":  "https://www.ibm.com/careers/search?field_keyword_08[0]=Software%20Engineer&field_keyword_18[0]=United%20States"},
    {"name": "Intel",
     "url":  "https://jobs.intel.com/en/search-jobs/software%20engineer/United%20States/599/1/2/6252001/39/-98/0/50"},
    {"name": "Medtronic",
     "url":  "https://jobs.medtronic.com/jobs/search?keywords=software+engineer&country=United+States"},
    {"name": "Microsoft",
     "url":  "https://careers.microsoft.com/us/en/search-results?keywords=software+engineer&exp=Students+and+graduates"},
    {"name": "P&G",
     "url":  "https://www.pgcareers.com/global/en/search-results?keywords=software+engineer&country=United%20States"},
    {"name": "Polaris",
     "url":  "https://www.polaris.com/en-us/careers/job-categories/information-technology/"},
    {"name": "United Airlines",
     "url":  "https://careers.united.com/us/en/search-results?keywords=software+engineer"},
    {"name": "UPS",
     "url":  "https://www.jobs-ups.com/search-jobs?keywords=software+engineer"},
]

# ── Job boards ───────────────────────────────────────────────────────────────────
JOB_BOARDS = [
    {"name": "LinkedIn — New Grad SWE (last 24h)",
     "url":  "https://www.linkedin.com/jobs/search/?keywords=software+engineer+new+grad&location=United+States&f_TPR=r86400&f_JT=F",
     "group": "linkedin"},
    {"name": "LinkedIn — Entry Level SWE (last 24h)",
     "url":  "https://www.linkedin.com/jobs/search/?keywords=software+engineer+entry+level&location=United+States&f_TPR=r86400&f_JT=F&f_E=1%2C2",
     "group": "linkedin"},
    {"name": "LinkedIn — Early Career SWE (last 24h)",
     "url":  "https://www.linkedin.com/jobs/search/?keywords=software+engineer+early+career&location=United+States&f_TPR=r86400&f_JT=F",
     "group": "linkedin"},
    {"name": "LinkedIn — Associate SWE / 0-2yr (last 24h)",
     "url":  "https://www.linkedin.com/jobs/search/?keywords=associate+software+engineer&location=United+States&f_TPR=r86400&f_JT=F&f_E=1%2C2",
     "group": "linkedin"},
    {"name": "LinkedIn Posts — #newgrad #softwareengineer",
     "url":  "https://www.linkedin.com/search/results/content/?keywords=%23newgrad%20%23softwareengineer%20hiring&datePosted=%22past-24h%22&origin=FACETED_SEARCH",
     "group": "linkedin_posts",
     "note": "LinkedIn feed posts — may require sign-in"},
    {"name": "LinkedIn Posts — #earlycareerswe hiring",
     "url":  "https://www.linkedin.com/search/results/content/?keywords=%23earlycareerswe%20hiring&datePosted=%22past-24h%22&origin=FACETED_SEARCH",
     "group": "linkedin_posts",
     "note": "LinkedIn feed posts — may require sign-in"},
    {"name": "Indeed — Entry Level SWE (last 24h)",
     "url":  "https://www.indeed.com/jobs?q=software+engineer+entry+level&l=United+States&jt=fulltime&fromage=1",
     "group": "indeed"},
    {"name": "Indeed — New Grad SWE (last 24h)",
     "url":  "https://www.indeed.com/jobs?q=%22new+grad%22+%22software+engineer%22&l=United+States&jt=fulltime&fromage=1",
     "group": "indeed"},
    {"name": "Indeed — 0-2 Years Experience SWE (last 24h)",
     "url":  "https://www.indeed.com/jobs?q=software+engineer+%220-2+years%22&l=United+States&jt=fulltime&fromage=1",
     "group": "indeed"},
    {"name": "Dice — SWE Full Time (last 24h)",
     "url":  "https://www.dice.com/jobs?q=software+engineer&location=United+States&filters.postedDate=ONE&filters.employmentType=FULLTIME",
     "group": "dice"},
    {"name": "Wellfound / AngelList — Startup SWE",
     "url":  "https://wellfound.com/jobs?role=software-engineer&location=United+States",
     "group": "startups"},
    {"name": "BuiltIn — Entry Level SWE",
     "url":  "https://builtin.com/jobs/dev-engineering?search=software+engineer&level=Entry+Level",
     "group": "startups"},
]

# ── Classification ───────────────────────────────────────────────────────────────
# Disqualify: JD clearly requires 3+ years, or is a senior/staff/principal/lead/director role.
DISQUALIFY_RE = re.compile(
    r"""(?:
        \b(?:requires?|minimum|at\s+least|must\s+have)\s+[3-9]\d*\+?\s*years? |
        \b[3-9]\d*\+?\s*(?:years?|yr)\s+(?:of\s+)?(?:professional\s+|relevant\s+|industry\s+)?experience |
        \bexperience[:\s]+[3-9]\d*\+?\s*(?:years?|yr) |
        \b[3-9]\+\s*(?:years?|yr)\b |
        \b1[0-9]\+?\s*years? |
        \bsenior\s+(?:software\s+)?(?:engineer|developer)\b |
        \bsr\.?\s+(?:software\s+)?(?:engineer|developer)\b |
        \bstaff\s+(?:software\s+)?(?:engineer|developer)\b |
        \bprincipal\s+(?:software\s+)?(?:engineer|developer)\b |
        \blead\s+(?:software\s+)?(?:engineer|developer)\b |
        \b(?:engineering|software|technical)\s+manager\b |
        \bdirector\s+of\s+(?:engineering|software|technology)\b |
        \bvice\s+president\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Qualify: JD explicitly says new grad / entry level / 0-2 years.
QUALIFY_RE = re.compile(
    r"""(?:
        \bnew\s*grad(?:uate)?\b |
        \bentry[\s\-]level\b |
        \bearly[\s\-]career\b |
        \bjunior\s+(?:software\s+)?(?:engineer|developer)\b |
        \brecent\s*grad(?:uate)?\b |
        \bclass\s+of\s+20(?:2[3-9]|[3-9]\d)\b |
        \b0[\s\-–]2\s+years?\b |
        \b0[\s\-–]1\s+years?\b |
        \bone\s+to\s+two\s+years?\b |
        \bzero\s+to\s+(?:one|two)\s+years?\b |
        \b(?:no|without)\s+(?:prior\s+)?(?:professional\s+)?experience\s+(?:required|necessary|needed)\b |
        \bassociate\s+software\s+engineer\b |
        \bsoftware\s+engineer\s+(?:i|1)\b |
        \bgraduate\s+(?:software\s+)?engineer\b |
        \b(?:college|university)\s+(?:new\s+)?grad(?:uate)?\b |
        \bswe\s+new\s*grad\b |
        \bfresh\s*grad(?:uate)?\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

TITLE_DISQUALIFY = frozenset([
    "senior", " sr ", "sr.", "staff engineer", "principal", "lead engineer",
    "engineering manager", "director of", "vice president", "vp ", "head of engineering",
])
TITLE_QUALIFY = frozenset([
    "new grad", "entry level", "early career", "junior", "associate software",
    "graduate engineer", "software engineer i", "swe i", "swe new grad",
])


def classify_jd(title: str, description: str) -> tuple[str, str]:
    """
    Returns (verdict, reason) where verdict is 'qualify', 'disqualify', or 'review'.
    JD description signals take priority over title signals.
    """
    text = (description or "")

    m = DISQUALIFY_RE.search(text)
    if m:
        return "disqualify", f"JD: '{m.group(0).strip()[:70]}'"

    m = QUALIFY_RE.search(text)
    if m:
        return "qualify", f"JD: '{m.group(0).strip()[:70]}'"

    t = (title or "").lower()
    for w in TITLE_DISQUALIFY:
        if w in t:
            return "disqualify", f"Title: '{w}'"
    for w in TITLE_QUALIFY:
        if w in t:
            return "qualify", f"Title: '{w}'"

    return "review", "No experience signal found — manual check recommended"


# ── State management ─────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        # Migrate v1 key
        if "content_hashes" in data and "page_hashes" not in data:
            data["page_hashes"] = data.pop("content_hashes")
        data.setdefault("page_hashes", {})
        data.setdefault("seen_job_urls", {})
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"page_hashes": {}, "seen_job_urls": {}}


def save_state(state: dict):
    seen = state.get("seen_job_urls", {})
    if len(seen) > MAX_STATE_URLS:
        keep = sorted(seen.items(), key=lambda x: x[1].get("first_seen", ""), reverse=True)
        state["seen_job_urls"] = dict(keep[:MAX_STATE_URLS])
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Browser ──────────────────────────────────────────────────────────────────────
class Browser:
    """Single Playwright browser instance for the entire run."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._ctx = None

    async def __aenter__(self):
        if HAS_PLAYWRIGHT:
            self._pw = await async_playwright().__aenter__()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                ],
            )
            self._ctx = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
        return self

    async def __aexit__(self, *args):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.__aexit__(*args)

    async def fetch(self, url: str, timeout_ms: int = 20_000) -> tuple[str | None, str | None]:
        """Returns (html, error_or_None)."""
        if not HAS_PLAYWRIGHT or self._ctx is None:
            return _fetch_urllib(url)
        page = None
        try:
            page = await self._ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Let JS-rendered content settle
            await page.wait_for_timeout(1800)
            html = await page.content()
            return html, None
        except PlaywrightTimeout:
            return None, f"Timeout after {timeout_ms // 1000}s"
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:80]}"
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass


def _fetch_urllib(url: str) -> tuple[str | None, str | None]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        ctx = ssl.create_default_context()
        req = Request(url, headers=headers)
        with urlopen(req, timeout=15, context=ctx) as resp:
            raw = resp.read()
            return raw.decode("utf-8", errors="replace"), None
    except HTTPError as e:
        return None, f"HTTP {e.code}"
    except URLError as e:
        return None, f"URLError: {str(e.reason)[:60]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"


# ── HTML parsing ─────────────────────────────────────────────────────────────────
def _page_hash(html: str) -> str:
    return hashlib.md5(html[:8192].encode(errors="replace")).hexdigest()


def _text_from_html(html: str) -> str:
    """Extract readable text from a job description page."""
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        # Try to find the job description container specifically
        for attr, pattern in [
            ("class", re.compile(r"(?i)job[-_]?desc|job[-_]?details?|posting[-_]?body|job[-_]?content|job[-_]?summary")),
            ("id",    re.compile(r"(?i)job[-_]?desc|job[-_]?details?|description|posting[-_]?body|job[-_]?content")),
        ]:
            el = soup.find(attrs={attr: pattern})
            if el:
                text = el.get_text(separator=" ", strip=True)
                if len(text) > 200:
                    return text
        return soup.get_text(separator=" ", strip=True)
    # Fallback: strip HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _title_from_html(html: str) -> str:
    """Extract job title from a JD page."""
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            t = soup.title.string.strip()
            # Remove " — Company Name" or " | Company" suffixes
            return re.split(r"\s*[-|–·]\s*", t)[0].strip()
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        return ""
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        t = m.group(1).strip()
        return re.split(r"\s*[-|–·]\s*", t)[0].strip()
    return ""


# ── Job link extraction ──────────────────────────────────────────────────────────
# Third-party ATS domains that company sites commonly redirect to
ATS_DOMAINS = frozenset([
    "greenhouse.io", "lever.co", "workday.com", "myworkdayjobs.com",
    "icims.com", "taleo.net", "successfactors.com", "smartrecruiters.com",
    "jobvite.com", "brassring.com", "kenexa.com", "ultipro.com",
    "linkedin.com", "indeed.com", "dice.com", "wellfound.com", "builtin.com",
])


def _is_job_link(url: str, source_url: str) -> bool:
    """Heuristic: does this URL look like a single job posting (not a search results page)?"""
    try:
        u = urlparse(url)
        base = urlparse(source_url)
    except Exception:
        return False

    # Same domain or known ATS domain
    if u.netloc and u.netloc != base.netloc:
        if not any(ats in u.netloc for ats in ATS_DOMAINS):
            return False

    path  = u.path.lower()
    query = u.query.lower()

    # Must have an ID-like component in path or query
    has_numeric_id = bool(re.search(r"/\d{4,}", path))
    has_query_id   = bool(re.search(
        r"[?&](jk|job_id|id|req|jobid|position_id|job_number|openingid|req_id)=[a-z0-9_-]{4,}",
        "?" + query, re.IGNORECASE
    ))
    has_long_slug  = bool(re.search(r"/[a-z][a-z0-9-]{20,}/?$", path))  # Greenhouse/Lever style

    if not (has_numeric_id or has_query_id or has_long_slug):
        return False

    # Must have job-related path segment
    job_keywords = ["job", "career", "position", "opening", "requisition", "apply", "role", "posting", "work"]
    if not any(kw in path for kw in job_keywords):
        # Allow if strong numeric ID (/view/1234567)
        if not re.search(r"/(?:view|detail|show)/\d{4,}", path):
            return False

    # Exclude search / listing / category pages
    if re.search(r"[?&](q|keywords?|search|query)=", "?" + query):
        return False
    if re.search(r"(?:/search|/results|/list(?:ing)?|/category|/categories|/jobs/?$)", path):
        return False

    return True


def _extract_job_links(html: str, source_url: str) -> list[str]:
    """Extract individual job listing URLs from a search results page."""
    links: set[str] = set()

    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("//"):
                href = "https:" + href
            elif not href.startswith("http"):
                href = urljoin(source_url, href)
            # Normalize: strip trailing fragment
            href = href.split("#")[0]
            if _is_job_link(href, source_url):
                links.add(href)
    else:
        for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
            href = m.group(1)
            if href.startswith("//"):
                href = "https:" + href
            elif not href.startswith("http"):
                href = urljoin(source_url, href)
            href = href.split("#")[0]
            if _is_job_link(href, source_url):
                links.add(href)

    return list(links)[:150]  # cap raw extraction


# ── Crawl a single source ────────────────────────────────────────────────────────
async def crawl_source(browser: Browser, source: dict, state: dict, max_jobs: int) -> dict:
    name     = source["name"]
    url      = source["url"]
    is_board = source.get("_is_board", False)

    result = {
        "name":                  name,
        "url":                   url,
        "note":                  source.get("note", ""),
        "accessible":            False,
        "error":                 None,
        "page_changed":          False,
        "is_board":              is_board,
        "jobs_qualify":          [],
        "jobs_review":           [],
        "jobs_disqualify_count": 0,
        "new_links_found":       0,
    }

    # 1. Fetch the search / listing page
    html, err = await browser.fetch(url)
    if html is None:
        result["error"] = err
        print(f"⚠️  {err}")
        return result

    result["accessible"] = True

    # 2. Check whether page content changed since last run
    h     = _page_hash(html)
    old_h = state["page_hashes"].get(name)
    state["page_hashes"][name] = h

    if old_h is None:
        # First run — establish baseline, no comparison yet
        print("⬦ (baseline)")
        return result

    if old_h == h:
        print("= (no change)")
        return result

    result["page_changed"] = True

    # 3. Extract individual job links
    job_links = _extract_job_links(html, url)
    seen      = state.setdefault("seen_job_urls", {})
    new_links = [lnk for lnk in job_links if lnk not in seen]
    result["new_links_found"] = len(new_links)

    if not new_links:
        print(f"🆕 (page changed, {len(job_links)} links — all already seen)")
        return result

    print(f"🆕 {len(new_links)} new links → ", end="", flush=True)

    # 4. Deep-crawl each new JD (up to max_jobs)
    qual = rev = disq = 0
    for jd_url in new_links[:max_jobs]:
        jd_html, jd_err = await browser.fetch(jd_url, timeout_ms=15_000)
        now = datetime.now(tz=CST).isoformat()

        if jd_html is None:
            seen[jd_url] = {
                "title":      "",
                "source":     name,
                "first_seen": now,
                "verdict":    "review",
                "reason":     f"JD inaccessible: {jd_err}",
            }
            result["jobs_review"].append({
                "title":  f"[Job link from {name}]",
                "url":    jd_url,
                "reason": f"Could not fetch: {jd_err}",
            })
            rev += 1
        else:
            title   = _title_from_html(jd_html)
            jd_text = _text_from_html(jd_html)
            verdict, reason = classify_jd(title, jd_text)

            seen[jd_url] = {
                "title":      title,
                "source":     name,
                "first_seen": now,
                "verdict":    verdict,
                "reason":     reason,
            }
            if verdict == "qualify":
                result["jobs_qualify"].append({"title": title, "url": jd_url, "reason": reason})
                qual += 1
            elif verdict == "review":
                result["jobs_review"].append({"title": title, "url": jd_url, "reason": reason})
                rev += 1
            else:
                result["jobs_disqualify_count"] += 1
                disq += 1

        await asyncio.sleep(0.5)

    state["seen_job_urls"] = seen
    print(f"✅{qual} 🔍{rev} ✗{disq}")
    return result


# ── Email ────────────────────────────────────────────────────────────────────────
def _sendgrid_banner() -> tuple[str, str]:
    days = (SENDGRID_EXPIRY - datetime.now(tz=CST)).days
    if days > 30:
        return "", ""
    if days <= 0:
        h = ('<div style="background:#fef2f2;border:1px solid #dc2626;border-radius:6px;'
             'padding:12px 16px;margin-bottom:14px;font-size:13px;">'
             '<strong>🚨 SendGrid trial EXPIRED</strong> — switch to Gmail: '
             'add <code>SMTP_USER</code> + <code>SMTP_PASS</code> as Actions secrets '
             'and remove <code>SENDGRID_API_KEY</code>.</div>')
        t = "🚨 SENDGRID TRIAL EXPIRED — add SMTP_USER + SMTP_PASS secrets."
    else:
        icon = "🔴" if days <= 7 else "🟡"
        h = (f'<div style="background:#fff7ed;border:1px solid #f97316;border-radius:6px;'
             f'padding:12px 16px;margin-bottom:14px;font-size:13px;">'
             f'{icon} <strong>SendGrid trial expires in {days} day{"s" if days != 1 else ""}</strong>'
             f' (Jan 15 2027). Add <code>SMTP_USER</code> + <code>SMTP_PASS</code> as Actions secrets '
             f'and remove <code>SENDGRID_API_KEY</code> before then.</div>')
        t = f"{icon} SENDGRID TRIAL EXPIRES IN {days} DAYS (Jan 15 2027)."
    return h, t


def _job_rows_html(jobs: list[dict], link_color: str) -> str:
    if not jobs:
        return '<p style="color:#888;font-size:13px;margin:0 0 12px 0;">None found this run.</p>'
    rows = ""
    for j in jobs:
        title  = (j.get("title") or "Untitled").replace("<", "&lt;").replace(">", "&gt;")
        reason = (j.get("reason") or "").replace("<", "&lt;")[:100]
        source = (j.get("source") or "").replace("<", "&lt;")
        url    = j.get("url", "#")
        rows += (
            f'<tr><td style="padding:8px 10px;border-bottom:1px solid #eee;">'
            f'<a href="{url}" style="color:{link_color};font-weight:600;">{title}</a>'
            f'<br><small style="color:#888;">{source} — {reason}</small>'
            f'</td></tr>\n'
        )
    return f'<table style="width:100%;border-collapse:collapse;font-size:13px;">{rows}</table>'


def build_html(source_results, all_qualify, all_review, total_disq,
               run_time, is_scheduled, changed_sources):
    label = "12pm CST Digest" if is_scheduled else (
        f"🆕 {len(all_qualify)} Qualifying Job{'s' if len(all_qualify) != 1 else ''}"
        if all_qualify else "Job Alert"
    )
    accessible = sum(1 for r in source_results if r["accessible"])
    blocked    = len(source_results) - accessible
    sg_html, _ = _sendgrid_banner()

    changed_note = ""
    if changed_sources:
        names = ", ".join(changed_sources[:8]) + ("…" if len(changed_sources) > 8 else "")
        changed_note = (
            f'<div style="background:#fef9c3;border:1px solid #d97706;border-radius:6px;'
            f'padding:10px 14px;margin-bottom:14px;font-size:13px;">'
            f'<strong>🆕 Sources with page changes:</strong> {names}</div>'
        )

    blocked_rows = "".join(
        f'<tr>'
        f'<td style="padding:4px 8px;font-size:12px;">{r["name"]}</td>'
        f'<td style="padding:4px 8px;font-size:12px;color:#888;">{r.get("error", "blocked")}</td>'
        f'<td style="padding:4px 8px;font-size:12px;">'
        f'<a href="{r["url"]}" style="color:#0055cc;">Visit →</a></td></tr>\n'
        for r in source_results if not r["accessible"]
    )
    blocked_table = (
        f'<details style="margin-top:14px;font-size:13px;">'
        f'<summary style="cursor:pointer;color:#666;">'
        f'{blocked} source{"s" if blocked != 1 else ""} blocked/inaccessible (expand)</summary>'
        f'<table style="width:100%;border-collapse:collapse;margin-top:8px;">{blocked_rows}</table>'
        f'</details>'
    ) if blocked_rows else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Monitor — {label}</title></head>
<body style="font-family:Arial,Helvetica,sans-serif;max-width:860px;margin:0 auto;padding:20px;color:#222;">

<div style="background:#0f172a;color:#f8fafc;padding:18px 22px;border-radius:8px;margin-bottom:18px;">
  <h1 style="margin:0;font-size:20px;letter-spacing:0.3px;">🔍 Job Monitor — {label}</h1>
  <p style="margin:6px 0 0;opacity:0.75;font-size:13px;">
    Run: {run_time} &nbsp;|&nbsp; {accessible}/{len(source_results)} sources accessible ·
    ✅ {len(all_qualify)} qualifying · 🔍 {len(all_review)} review · ✗ {total_disq} disqualified
  </p>
</div>

{sg_html}

<div style="background:#fefce8;border:1px solid #f59e0b;border-radius:6px;padding:12px 16px;margin-bottom:18px;font-size:13px;">
  <strong>⚡ Priority watch — Accenture AI Native Software Engineer:</strong>
  Role expected to open ~Nov 15–16 with February start.
  <a href="https://www.accenture.com/us-en/careers/jobsearch?jk=AI+native+software+engineer&cl=us" style="color:#0055cc;">
    Search Accenture →
  </a>
</div>

{changed_note}

<h2 style="font-size:16px;border-bottom:2px solid #166534;padding-bottom:6px;color:#166534;margin-top:0;">
  ✅ Qualifying Jobs ({len(all_qualify)})
</h2>
<p style="font-size:12px;color:#666;margin:0 0 10px;">
  JD or title explicitly says entry level / new grad / 0–2 yr experience
</p>
{_job_rows_html(all_qualify, "#166534")}

<h2 style="font-size:16px;border-bottom:2px solid #92400e;padding-bottom:6px;color:#92400e;margin-top:28px;">
  🔍 Needs Review ({len(all_review)})
</h2>
<p style="font-size:12px;color:#666;margin:0 0 10px;">
  No clear experience signal found — check the JD manually
</p>
{_job_rows_html(all_review, "#92400e")}

<div style="margin-top:12px;padding:8px 12px;background:#f1f5f9;border-radius:4px;font-size:12px;color:#666;">
  ✗ {total_disq} job{"s" if total_disq != 1 else ""} disqualified
  (title or JD says senior / 3+ years) — not shown above.
</div>

{blocked_table}

<div style="margin-top:24px;padding:12px 16px;background:#f0fdf4;border:1px solid #86efac;border-radius:6px;font-size:12px;color:#166534;">
  <strong>Profile:</strong> Full stack SWE · 
  Python, JavaScript/TypeScript, React, AWS, PostgreSQL · open to relocation · available immediately
</div>

<p style="font-size:11px;color:#999;margin-top:18px;">
  Sources marked blocked are <em>not</em> closed — click links to visit directly.
  Digest runs daily at 12pm CST · Hourly change detection.
</p>
</body></html>"""


def build_text(source_results, all_qualify, all_review, total_disq,
               run_time, is_scheduled, changed_sources):
    _, sg_text = _sendgrid_banner()
    label = "12pm CST Digest" if is_scheduled else (
        f"{len(all_qualify)} QUALIFYING JOBS FOUND" if all_qualify else "JOB ALERT"
    )
    lines = [
        f"JOB MONITOR — {label}",
        f"Run: {run_time}",
        "=" * 60,
        "",
        "⚡ PRIORITY: Accenture AI Native SWE",
        "   https://www.accenture.com/us-en/careers/jobsearch?jk=AI+native+software+engineer&cl=us",
        "",
    ]
    if sg_text:
        lines += [sg_text, ""]
    if changed_sources:
        lines += [f"🆕 PAGE CHANGES: {', '.join(changed_sources)}", ""]

    lines += ["✅ QUALIFYING JOBS", "-" * 40]
    if all_qualify:
        for j in all_qualify:
            lines += [
                f"  {j.get('title', 'Untitled')}",
                f"  Source: {j.get('source', '')} — {j.get('reason', '')}",
                f"  {j['url']}",
                "",
            ]
    else:
        lines += ["  None found this run.", ""]

    lines += ["🔍 NEEDS REVIEW", "-" * 40]
    if all_review:
        for j in all_review:
            lines += [
                f"  {j.get('title', 'Untitled')}",
                f"  Source: {j.get('source', '')} — {j.get('reason', '')}",
                f"  {j['url']}",
                "",
            ]
    else:
        lines += ["  None found this run.", ""]

    lines += [
        f"✗ {total_disq} disqualified (senior / 3+ years) — not shown.",
        "",
        "Blocked sources are NOT closed — visit links manually.",
    ]
    return "\n".join(lines)


# ── Send email ───────────────────────────────────────────────────────────────────
def _sendgrid(subject, html, text, to):
    key = os.environ.get("SENDGRID_API_KEY", "")
    if not key:
        return False, "SENDGRID_API_KEY not set"
    payload = json.dumps({
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": "jobmonitor@jobnotify.dev", "name": "Job Monitor"},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text},
            {"type": "text/html",  "value": html},
        ],
    }).encode()
    req = Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urlopen(req, timeout=30, context=ctx) as r:
            return True, f"SendGrid OK {r.status}"
    except HTTPError as e:
        return False, f"SendGrid HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return False, f"SendGrid: {e}"


def _smtp(subject, html, text, to):
    user = os.environ.get("SMTP_USER", "")
    pw   = os.environ.get("SMTP_PASS", "")
    if not user or not pw:
        return False, "SMTP_USER / SMTP_PASS not set"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = user
    msg["To"]      = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.sendmail(user, to, msg.as_string())
        return True, "SMTP OK"
    except Exception as e:
        return False, f"SMTP: {e}"


def send_email(subject, html, text, to):
    if os.environ.get("SENDGRID_API_KEY"):
        ok, msg = _sendgrid(subject, html, text, to)
        if ok:
            return True, msg
        print(f"  SendGrid failed ({msg}) — trying SMTP")
    return _smtp(subject, html, text, to)


# ── Entry point ──────────────────────────────────────────────────────────────────
async def run_async(is_scheduled: bool, is_hourly: bool):
    if not NOTIFY_EMAIL:
        print("ERROR: NOTIFY_EMAIL environment variable is not set.")
        sys.exit(1)

    now_cst  = datetime.now(tz=CST)
    run_time = now_cst.strftime("%Y-%m-%d %H:%M CST")
    mode     = "scheduled" if is_scheduled else ("hourly" if is_hourly else "on-demand")
    max_jobs = MAX_JOBS_HOURLY if is_hourly else MAX_JOBS_DAILY

    print(f"[{run_time}] Job Monitor v2 | mode={mode} | playwright={HAS_PLAYWRIGHT} | bs4={HAS_BS4}")

    quiet = 0 <= now_cst.hour < 7
    if quiet:
        print("  Quiet hours (12am–7am CST) — will check but not send email.")

    state = load_state()

    all_sources = (
        [{**c, "_is_board": False} for c in COMPANIES] +
        [{**b, "_is_board": True}  for b in JOB_BOARDS]
    )

    source_results = []
    async with Browser() as browser:
        for src in all_sources:
            print(f"  [{src['name']}] ", end="", flush=True)
            r = await crawl_source(browser, src, state, max_jobs)
            source_results.append(r)
            await asyncio.sleep(0.3)

    # Aggregate across all sources
    all_qualify  = []
    all_review   = []
    total_disq   = 0
    changed_srcs = [r["name"] for r in source_results if r.get("page_changed")]
    accessible   = sum(1 for r in source_results if r["accessible"])

    for r in source_results:
        for j in r["jobs_qualify"]:
            all_qualify.append({**j, "source": r["name"], "source_url": r["url"]})
        for j in r["jobs_review"]:
            all_review.append({**j, "source": r["name"], "source_url": r["url"]})
        total_disq += r.get("jobs_disqualify_count", 0)

    print(
        f"\n  Results: ✅ {len(all_qualify)} qualifying | "
        f"🔍 {len(all_review)} review | ✗ {total_disq} disqualified | "
        f"{accessible}/{len(source_results)} sources accessible"
    )
    print(f"  Sources with page changes: {len(changed_srcs)}")

    # Decide whether to send
    should_send = False
    skip_reason = None
    if quiet:
        skip_reason = f"quiet hours ({now_cst.strftime('%H:%M')} CST)"
    elif is_hourly:
        if all_qualify or all_review:
            should_send = True
        else:
            skip_reason = "hourly run — no new qualifying/review jobs found"
    else:
        should_send = True  # scheduled digest or on-demand always sends

    if skip_reason:
        print(f"  ⏸  Email skipped: {skip_reason}")
    elif should_send:
        subj_tag = "12pm Digest" if is_scheduled else (
            f"{len(all_qualify)} qualifying job{'s' if len(all_qualify) != 1 else ''}"
            if all_qualify else "job alert"
        )
        subject = f"[Job Monitor] {subj_tag} — {run_time}"
        html = build_html(source_results, all_qualify, all_review, total_disq,
                          run_time, is_scheduled, changed_srcs)
        text = build_text(source_results, all_qualify, all_review, total_disq,
                          run_time, is_scheduled, changed_srcs)
        ok, msg = send_email(subject, html, text, NOTIFY_EMAIL)
        print(f"  {'✅' if ok else '❌'} Email ({subj_tag}): {msg}")
        if not ok:
            print("\n--- PLAIN TEXT REPORT (email failed) ---")
            print(text[:3000])
        state["last_email_status"] = "ok" if ok else "failed"
        state["last_email_time"]   = run_time

    state["last_run"]             = run_time
    state["last_changed_sources"] = changed_srcs
    save_state(state)
    print("Done.")


def run(is_scheduled=False, is_hourly=False):
    asyncio.run(run_async(is_scheduled, is_hourly))


if __name__ == "__main__":
    is_scheduled = "--scheduled" in sys.argv
    is_hourly    = "--hourly"    in sys.argv
    run(is_scheduled=is_scheduled, is_hourly=is_hourly)

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

# ── Priority / flagged companies ─────────────────────────────────────────────────
# Jobs from these companies are sorted to the top of email results.
# Add any company name exactly as it appears in COMPANIES to prioritise it.
FLAGGED_COMPANIES = frozenset([
    "Accenture",
    # "Microsoft",
    # "Google",
])

# ── Industry map ─────────────────────────────────────────────────────────────────
# (industry label, is_non_tech) — used for the digest insights panel.
COMPANY_INDUSTRIES: dict[str, tuple[str, bool]] = {
    "Accenture":                    ("Consulting / Tech Services",        False),
    "Adobe":                        ("Creative Software / Tech",          False),
    "Amazon":                       ("E-commerce / Cloud",                False),
    "Amazon Web Services (AWS)":    ("Cloud / Infrastructure",            False),
    "American Express (AMEX)":      ("Finance / Payments",                True),
    "Apple":                        ("Consumer Electronics / Tech",        False),
    "Bank of America":              ("Finance / Banking",                  True),
    "Bechtel":                      ("Engineering / Construction",         True),
    "Brunswick":                    ("Marine / Recreation / Mfg",          True),
    "Capital One":                  ("Finance / Banking / Fintech",        True),
    "Caterpillar (CAT)":            ("Heavy Equipment / Manufacturing",    True),
    "Coca-Cola":                    ("Food & Beverage / Consumer Goods",   True),
    "Deloitte":                     ("Consulting / Professional Services", True),
    "EcoLab":                       ("Industrial / Water Treatment",       True),
    "Epic Systems":                 ("Healthcare IT",                      True),
    "GE Healthcare":                ("Healthcare / Medical Devices",       True),
    "General Dynamics":             ("Defense / Aerospace",                True),
    "Honeywell":                    ("Aerospace / Industrial Automation",  True),
    "IBM":                          ("Tech / Consulting",                  False),
    "Intel":                        ("Semiconductors / Hardware",          False),
    "Medtronic":                    ("Medical Devices / Healthcare",       True),
    "Microsoft":                    ("Tech / Cloud / AI",                  False),
    "P&G":                          ("Consumer Goods / FMCG",              True),
    "Polaris":                      ("Powersports / Recreation / Mfg",    True),
    "United Airlines":              ("Aviation / Transportation",           True),
    "UPS":                          ("Logistics / Supply Chain",            True),
    "Zoom":                         ("Tech / Communications",               False),
}

# Non-tech industry blurbs shown in the 12pm digest insights panel.
NON_TECH_INDUSTRY_NOTES: list[tuple[str, str, list[str]]] = [
    (
        "Finance & Banking",
        "Huge tech investment in digital banking, fraud detection, and payment systems. "
        "Competitive comp and strong job stability — often overlooked by new grads.",
        ["Bank of America", "Capital One", "American Express (AMEX)"],
    ),
    (
        "Healthcare & Medical Devices",
        "Mission-driven work building diagnostic tools, patient records, and surgical robotics. "
        "Epic Systems hires heavily for new grads with no prior experience required.",
        ["Medtronic", "GE Healthcare", "Epic Systems"],
    ),
    (
        "Defense & Aerospace",
        "Embedded systems, navigation software, and AI/ML for defense. "
        "US citizenship often satisfies security clearance pre-screening.",
        ["General Dynamics", "Honeywell"],
    ),
    (
        "Consumer Goods & Manufacturing",
        "Digital transformation is driving SWE demand outside Silicon Valley — "
        "usually lower competition than Big Tech with strong benefits.",
        ["P&G", "Caterpillar (CAT)", "Coca-Cola", "Brunswick", "Polaris"],
    ),
    (
        "Logistics & Transportation",
        "Real-time tracking, route optimization, and digital ops at massive scale. "
        "Engineering orgs often overlooked by new grads — worth checking.",
        ["UPS", "United Airlines"],
    ),
    (
        "Consulting / Professional Services",
        "Firms like Accenture and Deloitte deploy SWEs across industries. "
        "Great for breadth of experience and exposure early-career.",
        ["Accenture", "Deloitte"],
    ),
    (
        "Engineering Services & Industrial",
        "IoT, process automation, water-treatment systems, and large-scale infrastructure. "
        "Growing demand for SWEs who can bridge hardware and software.",
        ["Bechtel", "EcoLab"],
    ),
]

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
     "url":  "https://jobs.apple.com/en-us/search?location=united-states-USA",
     "timeout_ms": 35000},
    {"name": "Bank of America",
     "url":  "https://careers.bankofamerica.com/en-us/search-jobs?keywords=software+engineer&country=United%20States"},
    {"name": "Bechtel",
     "url":  "https://www.bechtel.com/careers/jobs/?q=software+engineer"},
    {"name": "Brunswick",
     "url":  "https://careers.brunswick.com/us/en/search-results?keywords=software+engineer",
     "timeout_ms": 45000},
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
     "url":  "https://careers.honeywell.com/en/sites/Honeywell/jobs?keyword=software+engineer&country=united+states+of+america"},
    {"name": "IBM",
     "url":  "https://www.ibm.com/careers/search?field_keyword_08[0]=Software%20Engineer&field_keyword_18[0]=United%20States"},
    {"name": "Intel",
     "url":  "https://jobs.intel.com/en/search-jobs/software%20engineer/United%20States/599/1/2/6252001/39/-98/0/50"},
    {"name": "Medtronic",
     "url":  "https://medtronic.wd1.myworkdayjobs.com/en-US/MedtronicCareers?q=software+engineer"},
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
    {"name": "Amazon Web Services (AWS)",
     "url":  "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=United+States&business_category[]=amazon-web-services&job_type=Full-Time"},
    {"name": "American Express (AMEX)",
     "url":  "https://aexp.eightfold.ai/careers?query=software+engineer&location=United+States"},
    {"name": "Coca-Cola",
     "url":  "https://careers.coca-colacompany.com/en/jobs?search=software+engineer&location=United+States"},
    {"name": "Deloitte",
     "url":  "https://apply.deloitte.com/en_US/careers/SearchJobs/software%20engineer?listFilterMode=1&jobRecordsPerPage=10&sort=relevancy"},
    {"name": "Zoom",
     "url":  "https://www.careers.zoom.us/job-results"},
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
            self._pw = await async_playwright().start()
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
            await self._pw.stop()

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
    fetch_timeout = source.get("timeout_ms", 20_000)
    html, err = await browser.fetch(url, timeout_ms=fetch_timeout)
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
                "time":   now,
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
                result["jobs_qualify"].append({"title": title, "url": jd_url, "reason": reason, "time": now})
                qual += 1
            elif verdict == "review":
                result["jobs_review"].append({"title": title, "url": jd_url, "reason": reason, "time": now})
                rev += 1
            else:
                result["jobs_disqualify_count"] += 1
                disq += 1

        await asyncio.sleep(0.5)

    state["seen_job_urls"] = seen
    print(f"✅{qual} 🔍{rev} ✗{disq}")
    return result


# ── Email helpers ────────────────────────────────────────────────────────────────

def _fmt_time(iso_str: str) -> str:
    """Format an ISO timestamp as 12-hour time, e.g. '2:28 PM'."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%-I:%M %p")
    except Exception:
        return ""


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
    """
    Renders each job in the format:
      [Company] Job Title — Time
      ——————————————————————————
      reason / notes
    """
    if not jobs:
        return '<p style="color:#888;font-size:13px;margin:4px 0 16px 0;font-style:italic;">None found this run.</p>'

    parts = []
    for j in jobs:
        title   = (j.get("title") or "Untitled").replace("<", "&lt;").replace(">", "&gt;")
        reason  = (j.get("reason") or "").replace("<", "&lt;")[:120]
        source  = (j.get("source") or "").replace("<", "&lt;")
        url     = j.get("url", "#")
        t       = _fmt_time(j.get("time", ""))
        time_str = f' - [{t}]' if t else ""
        flagged = source in FLAGGED_COMPANIES

        badge = (
            '<span style="background:#fef9c3;border:1px solid #d97706;color:#92400e;'
            'font-size:10px;font-weight:700;padding:1px 5px;border-radius:3px;'
            'margin-right:5px;vertical-align:middle;">★ PRIORITY</span>'
            if flagged else ""
        )

        parts.append(
            f'<div style="margin-bottom:14px;">'
            f'<div style="font-size:13px;margin-bottom:3px;">'
            f'{badge}'
            f'<span style="color:#555;font-weight:600;">[{source}]</span> '
            f'<a href="{url}" style="color:{link_color};font-weight:700;text-decoration:none;">{title}</a>'
            f'<span style="color:#999;font-size:12px;">{time_str}</span>'
            f'</div>'
            f'<div style="border-top:1px solid #e5e7eb;margin:4px 0 5px 0;"></div>'
            f'<div style="color:#666;font-size:12px;">{reason}</div>'
            f'</div>'
        )
    return "\n".join(parts)


def _accenture_alert_html(all_qualify: list[dict], all_review: list[dict]) -> str:
    """
    Returns the Accenture AI Native SWE alert HTML only if:
    - A job from Accenture was found this run, OR
    - A job with AI/AI-native/February-start signals was found anywhere.
    Returns empty string otherwise.
    """
    ai_re = re.compile(
        r'\b(ai[\s\-]?native|artificial\s+intelligence|machine\s+learning|'
        r'february\s+start|feb(?:ruary)?\s+20\d\d|starts?\s+(?:in\s+)?february)\b',
        re.IGNORECASE,
    )
    for job in all_qualify + all_review:
        src   = job.get("source", "")
        title = job.get("title", "")
        reason = job.get("reason", "")
        if src == "Accenture" or ai_re.search(title) or ai_re.search(reason):
            link = job.get("url", "https://www.accenture.com/us-en/careers/jobsearch?jk=AI+native+software+engineer&cl=us")
            match_note = (
                f'<a href="{link}" style="color:#0055cc;font-weight:700;">'
                f'{"[Accenture] " + (job.get("title") or "AI Native Software Engineer") if src == "Accenture" else "Similar role found"}'
                f'</a>'
            )
            return (
                '<div style="background:#fefce8;border:2px solid #f59e0b;border-radius:7px;'
                'padding:12px 16px;margin-bottom:18px;font-size:13px;">'
                '<strong>⚡ Accenture AI Native SWE — FOUND:</strong> '
                f'{match_note}'
                '</div>'
            )
    return ""  # not found — no banner


def _insights_html(source_results: list[dict], all_qualify: list[dict], all_review: list[dict]) -> str:
    """
    Builds the insights panel for the 12pm digest:
    - Keyword/skill trends noticed across JDs
    - Most active industries this run
    - Non-tech industries hiring SWEs
    """
    all_jobs = all_qualify + all_review

    # ── Industry activity from this run ─────────────────────────────────────────
    industry_hits: dict[str, int] = {}
    for job in all_jobs:
        src = job.get("source", "")
        entry = COMPANY_INDUSTRIES.get(src)
        if entry:
            ind, _ = entry
            industry_hits[ind] = industry_hits.get(ind, 0) + 1

    top_industries = sorted(industry_hits.items(), key=lambda x: -x[1])[:5]
    top_ind_html = ""
    if top_industries:
        items = " &nbsp;·&nbsp; ".join(
            f'<strong>{ind}</strong> ({n} job{"s" if n > 1 else ""})'
            for ind, n in top_industries
        )
        top_ind_html = (
            f'<p style="font-size:13px;color:#374151;margin:0 0 16px 0;">'
            f'<strong>Most active industries this run:</strong> {items}</p>'
        )

    # ── Tech keyword trends across JD titles ───────────────────────────────────
    TECH_STACK_KEYWORDS = [
        "Python", "Java", "JavaScript", "TypeScript", "React", "Node",
        "AWS", "Azure", "GCP", "SQL", "Go", "Rust", "C++", "C#", "Kotlin",
        "Docker", "Kubernetes", "ML", "AI", "Machine Learning", "Full Stack",
        "Backend", "Frontend", "Mobile", "iOS", "Android", "Data",
        "Cloud", "DevOps", "Embedded", "Systems",
    ]
    keyword_counts: dict[str, int] = {}
    for job in all_jobs:
        title = job.get("title", "")
        reason = job.get("reason", "")
        combined = f"{title} {reason}".lower()
        for kw in TECH_STACK_KEYWORDS:
            if kw.lower() in combined:
                keyword_counts[kw] = keyword_counts.get(kw, 0) + 1

    top_keywords = sorted(keyword_counts.items(), key=lambda x: -x[1])[:8]

    # ── Qualifying signal breakdown ────────────────────────────────────────────
    qual_signals: dict[str, int] = {}
    for job in all_qualify:
        reason = job.get("reason", "")
        # Grab the signal keyword
        for sig in ["new grad", "entry level", "early career", "junior", "associate", "0-2", "0–2", "no experience"]:
            if sig in reason.lower():
                qual_signals[sig] = qual_signals.get(sig, 0) + 1
                break

    # ── Build JD observations section ─────────────────────────────────────────
    jd_observations = ""
    if all_jobs:
        # Tech stack chips
        stack_chips = ""
        if top_keywords:
            chips = "".join(
                f'<span style="display:inline-block;background:#e0f2fe;color:#0369a1;'
                f'border-radius:12px;padding:2px 10px;font-size:11px;font-weight:600;'
                f'margin:2px 3px 2px 0;">{kw} ({n})</span>'
                for kw, n in top_keywords
            )
            stack_chips = (
                f'<div style="margin-bottom:14px;">'
                f'<div style="font-size:12px;font-weight:700;color:#374151;margin-bottom:6px;">'
                f'Most-requested skills across job titles this run:</div>'
                f'<div>{chips}</div>'
                f'</div>'
            )

        # Qualifying signals
        signal_text = ""
        if qual_signals:
            sig_items = ", ".join(f'"{k}" ({v}×)' for k, v in sorted(qual_signals.items(), key=lambda x: -x[1]))
            signal_text = (
                f'<p style="font-size:12px;color:#374151;margin:0 0 12px 0;">'
                f'<strong>Qualifying signals found:</strong> {sig_items}</p>'
            )

        total_seen = len(all_jobs)
        qualify_pct = round(len(all_qualify) / total_seen * 100) if total_seen else 0
        jd_observations = (
            f'<div style="margin-bottom:20px;">'
            f'<p style="font-size:12px;color:#374151;margin:0 0 10px 0;">'
            f'Across <strong>{total_seen}</strong> new job{"s" if total_seen != 1 else ""} crawled this run, '
            f'<strong>{len(all_qualify)}</strong> ({qualify_pct}%) matched entry-level signals and '
            f'<strong>{len(all_review)}</strong> need a manual check.</p>'
            f'{signal_text}'
            f'{stack_chips}'
            f'</div>'
        )
    else:
        jd_observations = (
            f'<p style="font-size:12px;color:#9ca3af;font-style:italic;margin:0 0 16px 0;">'
            f'No new JDs crawled this run — no page changes detected.</p>'
        )

    # ── Non-tech sectors panel ─────────────────────────────────────────────────
    sector_rows = ""
    for industry, note, companies in NON_TECH_INDUSTRY_NOTES:
        co_list = ", ".join(companies)
        active  = industry_hits.get(industry, 0)
        active_badge = (
            f' <span style="background:#dcfce7;color:#166534;border-radius:3px;'
            f'font-size:11px;padding:1px 5px;font-weight:700;">↑ {active} new</span>'
            if active else ""
        )
        sector_rows += (
            f'<div style="margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #e5e7eb;">'
            f'<div style="font-size:13px;font-weight:700;color:#1e3a5f;margin-bottom:3px;">'
            f'{industry}{active_badge}</div>'
            f'<div style="font-size:12px;color:#374151;margin-bottom:3px;">{note}</div>'
            f'<div style="font-size:11px;color:#9ca3af;">Companies in watchlist: {co_list}</div>'
            f'</div>'
        )

    return (
        f'<div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px;'
        f'padding:18px 20px;margin-top:28px;">'
        f'<h2 style="font-size:15px;color:#1e3a5f;margin:0 0 4px 0;'
        f'border-bottom:2px solid #cbd5e1;padding-bottom:8px;">'
        f'📊 What the Monitor Noticed Today</h2>'
        f'<p style="font-size:11px;color:#9ca3af;margin:8px 0 16px 0;">'
        f'Patterns and signals extracted across all job descriptions crawled this run.</p>'
        f'{top_ind_html}'
        f'{jd_observations}'
        f'<h3 style="font-size:13px;color:#374151;margin:16px 0 10px 0;'
        f'border-top:1px solid #e5e7eb;padding-top:14px;">'
        f'🌐 Non-Tech Industries That Hire SWEs</h3>'
        f'<p style="font-size:12px;color:#6b7280;margin:0 0 14px 0;">'
        f'Not all tech jobs live at tech companies. These industries actively hire SWEs '
        f'— and usually with less competition than Big Tech.</p>'
        f'{sector_rows}'
        f'</div>'
    )


def build_html(source_results, all_qualify, all_review, total_disq,
               run_time, is_scheduled, is_hourly, changed_sources):
    accessible = sum(1 for r in source_results if r["accessible"])
    blocked    = len(source_results) - accessible
    sg_html, _ = _sendgrid_banner()
    accenture_html = _accenture_alert_html(all_qualify, all_review)

    # ── Header label ────────────────────────────────────────────────────────────
    if is_hourly:
        header_label    = "⏰ Hourly Check"
        header_subtitle = f"Scanning all {len(source_results)} sources for new listings since last run"
        header_bg       = "#1e3a5f"
    elif is_scheduled:
        header_label    = "📬 Daily Job Search Digest"
        header_subtitle = "Your curated new-grad SWE opportunities — updated daily at 12pm CST"
        header_bg       = "#0f172a"
    else:
        header_label    = "🔍 On-Demand Run"
        header_subtitle = f"Manual check across all {len(source_results)} sources"
        header_bg       = "#0f172a"

    nq  = len(all_qualify)
    nr  = len(all_review)
    summary_line = (
        f'{accessible}/{len(source_results)} sources accessible &nbsp;·&nbsp; '
        f'✅ {nq} qualifying &nbsp;·&nbsp; 🔍 {nr} review &nbsp;·&nbsp; ✗ {total_disq} disqualified'
    )

    # ── Blocked sources (collapsed) ──────────────────────────────────────────────
    blocked_rows = "".join(
        f'<tr>'
        f'<td style="padding:4px 8px;font-size:12px;">{r["name"]}</td>'
        f'<td style="padding:4px 8px;font-size:12px;color:#888;">{r.get("error","blocked")}</td>'
        f'<td style="padding:4px 8px;font-size:12px;">'
        f'<a href="{r["url"]}" style="color:#0055cc;">Visit →</a></td></tr>\n'
        for r in source_results if not r["accessible"]
    )
    blocked_section = (
        f'<details style="margin-top:10px;font-size:12px;">'
        f'<summary style="cursor:pointer;color:#9ca3af;">'
        f'{blocked} source{"s" if blocked != 1 else ""} blocked / inaccessible — click to expand</summary>'
        f'<table style="width:100%;border-collapse:collapse;margin-top:8px;">{blocked_rows}</table>'
        f'</details>'
    ) if blocked_rows else ""

    # ── Job sections ─────────────────────────────────────────────────────────────
    qualify_section = (
        f'<h2 style="font-size:15px;border-bottom:2px solid #166534;padding-bottom:6px;'
        f'color:#166534;margin-top:24px;">✅ Qualifying Jobs ({nq})</h2>'
        f'<p style="font-size:11px;color:#9ca3af;margin:0 0 10px 0;">'
        f'JD or title explicitly says entry level / new grad / 0–2 yr experience</p>'
        f'{_job_rows_html(all_qualify, "#166534")}'
    )
    review_section = (
        f'<h2 style="font-size:15px;border-bottom:2px solid #92400e;padding-bottom:6px;'
        f'color:#92400e;margin-top:24px;">🔍 Needs Review ({nr})</h2>'
        f'<p style="font-size:11px;color:#9ca3af;margin:0 0 10px 0;">'
        f'No clear experience signal — check the JD manually</p>'
        f'{_job_rows_html(all_review, "#92400e")}'
    )
    disq_note = (
        f'<div style="margin-top:10px;padding:7px 12px;background:#f1f5f9;'
        f'border-radius:4px;font-size:12px;color:#9ca3af;">'
        f'✗ {total_disq} job{"s" if total_disq != 1 else ""} disqualified '
        f'(title or JD says senior / 3+ years) — not shown above.</div>'
    )

    # ── Profile chip ─────────────────────────────────────────────────────────────
    profile_chip = (
        '<div style="margin-top:20px;padding:10px 14px;background:#f0fdf4;'
        'border:1px solid #86efac;border-radius:6px;font-size:11px;color:#166534;">'
        '<strong>Profile:</strong> Full stack SWE · '
        'Python, JS/TS, React, AWS, PostgreSQL · open to relocation · available immediately'
        '</div>'
    )

    # ── Digest-only extras ───────────────────────────────────────────────────────
    insights_section = (
        _insights_html(source_results, all_qualify, all_review)
        if is_scheduled else ""
    )

    footer = (
        '<p style="font-size:11px;color:#9ca3af;margin-top:20px;text-align:center;">'
        'Blocked sources are <em>not</em> closed — visit links directly. &nbsp;|&nbsp; '
        'Digest: daily at 12pm CST &nbsp;|&nbsp; Hourly change detection'
        '</p>'
    )

    # ── Newsletter intro (digest only) ───────────────────────────────────────────
    newsletter_intro = ""
    if is_scheduled:
        nq_phrase  = f"<strong>{nq} qualifying role{'s' if nq != 1 else ''}</strong>" if nq else "no qualifying roles"
        nr_phrase  = f"<strong>{nr} to review</strong>" if nr else "nothing flagged for review"
        newsletter_intro = (
            f'<div style="border-left:4px solid #0f172a;padding:12px 16px;'
            f'background:#f9fafb;margin-bottom:20px;border-radius:0 6px 6px 0;">'
            f'<p style="margin:0;font-size:14px;color:#374151;line-height:1.6;">'
            f"Today's scan turned up {nq_phrase} and {nr_phrase} across "
            f'<strong>{accessible}</strong> accessible sources. '
            f'Flagged companies appear first in each section below.</p>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Monitor — {header_label}</title></head>
<body style="font-family:Arial,Helvetica,sans-serif;max-width:860px;margin:0 auto;padding:20px;color:#222;">

<div style="background:{header_bg};color:#f8fafc;padding:18px 22px;border-radius:8px;margin-bottom:18px;">
  <h1 style="margin:0;font-size:20px;letter-spacing:0.3px;">{header_label}</h1>
  <p style="margin:5px 0 0;opacity:0.85;font-size:13px;">{header_subtitle}</p>
  <p style="margin:6px 0 0;opacity:0.65;font-size:12px;">{run_time} &nbsp;·&nbsp; {summary_line}</p>
</div>

{sg_html}
{newsletter_intro}
{accenture_html}
{qualify_section}
{review_section}
{disq_note}
{blocked_section}
{profile_chip}
{insights_section}
{footer}
</body></html>"""


def build_text(source_results, all_qualify, all_review, total_disq,
               run_time, is_scheduled, is_hourly, changed_sources):
    _, sg_text = _sendgrid_banner()
    accessible = sum(1 for r in source_results if r["accessible"])

    if is_hourly:
        label    = "⏰ HOURLY CHECK"
        sublabel = f"Scanning all {len(source_results)} sources for new listings since last run"
    elif is_scheduled:
        label    = "📬 DAILY JOB SEARCH DIGEST"
        sublabel = "Your curated new-grad SWE opportunities — 12pm CST"
    else:
        label    = "JOB MONITOR — ON-DEMAND"
        sublabel = ""

    lines = [
        label,
    ]
    if sublabel:
        lines.append(sublabel)
    lines += [
        f"Run: {run_time}",
        f"{accessible}/{len(source_results)} sources accessible · "
        f"✅ {len(all_qualify)} qualifying · 🔍 {len(all_review)} review · ✗ {total_disq} disqualified",
        "=" * 60,
        "",
    ]

    if sg_text:
        lines += [sg_text, ""]

    # Accenture alert only if found
    ai_re = re.compile(
        r'\b(ai[\s\-]?native|artificial\s+intelligence|machine\s+learning|'
        r'february\s+start|feb(?:ruary)?\s+20\d\d|starts?\s+(?:in\s+)?february)\b',
        re.IGNORECASE,
    )
    for job in all_qualify + all_review:
        src = job.get("source", "")
        if src == "Accenture" or ai_re.search(job.get("title", "")) or ai_re.search(job.get("reason", "")):
            lines += [
                "⚡ ACCENTURE AI NATIVE SWE FOUND:",
                f"   {job.get('title', 'Role found')} — {job.get('url', '')}",
                "",
            ]
            break

    lines += ["✅ QUALIFYING JOBS", "-" * 40]
    if all_qualify:
        for j in all_qualify:
            t = _fmt_time(j.get("time", ""))
            time_str = f" - [{t}]" if t else ""
            priority = "★ PRIORITY  " if j.get("source", "") in FLAGGED_COMPANIES else ""
            lines += [
                f"  {priority}[{j.get('source', '')}] {j.get('title', 'Untitled')}{time_str}",
                f"  ——",
                f"  {j.get('reason', '')}",
                f"  {j.get('url', '')}",
                "",
            ]
    else:
        lines += ["  None found this run.", ""]

    lines += ["🔍 NEEDS REVIEW", "-" * 40]
    if all_review:
        for j in all_review:
            t = _fmt_time(j.get("time", ""))
            time_str = f" - [{t}]" if t else ""
            priority = "★ PRIORITY  " if j.get("source", "") in FLAGGED_COMPANIES else ""
            lines += [
                f"  {priority}[{j.get('source', '')}] {j.get('title', 'Untitled')}{time_str}",
                f"  ——",
                f"  {j.get('reason', '')}",
                f"  {j.get('url', '')}",
                "",
            ]
    else:
        lines += ["  None found this run.", ""]

    lines += [
        f"✗ {total_disq} disqualified (senior / 3+ years) — not shown.",
        "",
        "Blocked sources are NOT closed — visit links directly.",
    ]

    if is_scheduled:
        # Keyword trends
        TECH_STACK_KEYWORDS = [
            "Python", "Java", "JavaScript", "TypeScript", "React", "Node",
            "AWS", "Azure", "GCP", "SQL", "Go", "Rust", "C++", "C#",
            "Docker", "Kubernetes", "ML", "AI", "Machine Learning",
            "Full Stack", "Backend", "Frontend", "Mobile", "Data", "Cloud",
        ]
        keyword_counts: dict[str, int] = {}
        for j in all_qualify + all_review:
            combined = f"{j.get('title','')} {j.get('reason','')}".lower()
            for kw in TECH_STACK_KEYWORDS:
                if kw.lower() in combined:
                    keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
        top_kw = sorted(keyword_counts.items(), key=lambda x: -x[1])[:6]

        lines += [
            "",
            "=" * 60,
            "📊 WHAT THE MONITOR NOTICED TODAY",
            "-" * 40,
        ]
        if top_kw:
            kw_str = "  " + "  ·  ".join(f"{kw} ({n})" for kw, n in top_kw)
            lines += ["Most-requested skills across JD titles:", kw_str, ""]

        lines += [
            "🌐 NON-TECH INDUSTRIES HIRING SWEs",
            "-" * 40,
            "Not all tech jobs live at tech companies.",
            "",
        ]
        for industry, note, companies in NON_TECH_INDUSTRY_NOTES:
            lines += [
                f"• {industry} ({', '.join(companies)})",
                f"  {note}",
                "",
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
    run_time = now_cst.strftime("%B %-d, %Y %-I:%M %p CST")
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

    # Flagged companies always appear first in results
    def _flagged_first(job: dict) -> int:
        return 0 if job.get("source", "") in FLAGGED_COMPANIES else 1
    all_qualify.sort(key=_flagged_first)
    all_review.sort(key=_flagged_first)

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
                          run_time, is_scheduled, is_hourly, changed_srcs)
        text = build_text(source_results, all_qualify, all_review, total_disq,
                          run_time, is_scheduled, is_hourly, changed_srcs)
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

#!/usr/bin/env python3
"""
Daily QA Job Scraper
Searches 6 job sources for remote QA / QA Automation roles and emails a
formatted digest to the configured recipient.

Sources
-------
  1. Indeed + ZipRecruiter   – via python-jobspy (LinkedIn/Glassdoor blocked)
  2. We Work Remotely        – dedicated QA RSS feed (most reliable free source)
  3. Remotive API            – free JSON API, good remote-job coverage
  4. RemoteOK API            – free JSON API with tag-based search
  5. Greenhouse API          – public ATS boards for 30+ top startups
  6. Lever API               – public ATS boards for 20+ top startups
  7. Jobright (jobright.ai)  – HTML scrape of server-rendered Next.js __NEXT_DATA__

Environment variables (set as GitHub Actions secrets):
  SENDER_EMAIL      – Gmail address to send from
  SENDER_PASSWORD   – Gmail App Password (16-char, NOT your account password)

Optional:
  SMTP_HOST              – defaults to smtp.gmail.com
  SMTP_PORT              – defaults to 587
  EXCHANGE_RATE_API_KEY  – exchangerate-api.com key for live USD→NGN rate
"""

import os
import re
import html
import json
import logging
import smtplib
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from difflib import SequenceMatcher
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import Optional

import requests
import pandas as pd
from jobspy import scrape_jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Recipients ───────────────────────────────────────────────────────────────
RECIPIENT_EMAIL = "shola.mich.7438@gmail.com"
SENDER_EMAIL    = os.environ["SENDER_EMAIL"]
SENDER_PASSWORD = os.environ["SENDER_PASSWORD"]
SMTP_HOST       = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "587"))

# ── Date window ──────────────────────────────────────────────────────────────
# Accept jobs posted within the last 72 h (3 days).
# Job boards often index postings 24-48 h after they go live; a strict 24 h
# window silently drops many fresh roles.
MAX_AGE_HOURS = 72

# ── Salary gate ──────────────────────────────────────────────────────────────
MIN_NGN_MONTHLY   = 2_000_000          # ₦2M/month minimum
FALLBACK_USD_RATE = 1_580              # fallback if live rate fetch fails

# ── Timezone acceptance ───────────────────────────────────────────────────────
# WAT = UTC+1; accept UTC−1 → UTC+3 (±2 h)
ACCEPTED_TZ_TOKENS = {
    "CVT", "EGT", "AZOT",                       # UTC-1
    "GMT", "UTC", "WET", "UT",                   # UTC 0
    "WAT", "CET", "MET", "WEST",                 # UTC+1
    "CAT", "CEST", "WAST", "EET", "SAST",        # UTC+2
    "EAT", "MSK", "AST", "EEST",                 # UTC+3
    "EUROPE", "UK", "EMEA", "AFRICA",
    "NIGERIA", "GHANA", "KENYA", "SOUTH AFRICA",
    "GERMANY", "FRANCE", "NETHERLANDS", "POLAND",
    "SPAIN", "PORTUGAL", "ISRAEL", "TURKEY", "ROMANIA", "UKRAINE",
    "WORLDWIDE", "GLOBAL", "ANYWHERE",
}

# ── JobSpy search terms ───────────────────────────────────────────────────────
# Used only for Indeed + ZipRecruiter (LinkedIn/Glassdoor actively block scraping)
JOBSPY_QUERIES = [
    "QA Automation Engineer",
    "SDET remote",
    "Test Automation Engineer remote",
    "Quality Assurance Engineer remote",
    "Performance Test Engineer remote",
    "Security Test Engineer remote",
]

# ── Greenhouse / Lever company slugs ─────────────────────────────────────────
# These companies use public ATS boards — no auth required
GREENHOUSE_SLUGS = [
    "stripe", "notion", "linear", "vercel", "figma", "retool",
    "datadog", "snyk", "postman", "browserstack", "lambdatest",
    "hashicorp", "confluent", "airbyte", "dbtlabs", "mixpanel",
    "amplitude", "grafana", "pagerduty", "incident-io",
    "atlassian", "gitlab", "circleci", "sonarqube",
    "newrelic", "dynatrace", "saucelabs",
]

LEVER_SLUGS = [
    "github", "figma", "notion", "linear", "vercel",
    "segment", "heap", "percy", "chromatic", "testim",
    "mabl", "rainforest-qa", "checkly",
]

# ── Resume skill keywords ─────────────────────────────────────────────────────
SKILL_KEYWORDS = [
    "Selenium", "Playwright", "Cypress", "Appium", "WebdriverIO",
    "TestNG", "JUnit", "pytest", "NUnit", "xUnit",
    "Robot Framework", "Cucumber", "SpecFlow", "Gherkin",
    "Python", "Java", "JavaScript", "TypeScript", "C#", "Go", "Kotlin",
    "Jenkins", "GitHub Actions", "GitLab CI", "CircleCI", "Travis CI",
    "Docker", "Kubernetes", "Terraform", "Ansible",
    "JMeter", "k6", "Gatling", "Locust", "LoadRunner",
    "OWASP", "Burp Suite", "ZAP", "Nessus", "Snyk", "Penetration Testing",
    "SAST", "DAST", "Vulnerability",
    "REST", "GraphQL", "gRPC", "Postman", "RestAssured", "Karate",
    "SQL", "PostgreSQL", "MySQL", "MongoDB",
    "Grafana", "Prometheus", "Datadog", "Splunk", "ELK",
    "BDD", "TDD", "Agile", "Scrum", "Shift-left",
    "Contract Testing", "Pact", "A/B Testing",
    "AWS", "GCP", "Azure", "Lambda", "S3",
    "iOS", "Android", "XCUITest", "Espresso",
]

PRIORITY_COMPANIES = {
    "stripe", "notion", "linear", "vercel", "figma", "retool",
    "datadog", "hashicorp", "confluent", "postman", "dbt labs",
    "airbyte", "segment", "mixpanel", "amplitude", "heap",
    "browserstack", "lambdatest", "sauce labs", "testim",
    "mabl", "rainforest qa", "percy", "chromatic", "checkly",
    "github", "gitlab", "atlassian", "jetbrains", "circleci",
    "sonarqube", "snyk", "aqua security", "checkov",
    "grafana labs", "prometheus", "new relic", "dynatrace",
    "pagerduty", "incident.io", "rootly",
}


# ─────────────────────────────────────────────────────────────────────────────
# Exchange-rate helper
# ─────────────────────────────────────────────────────────────────────────────

def get_usd_to_ngn() -> float:
    api_key = os.getenv("EXCHANGE_RATE_API_KEY")
    if api_key:
        try:
            resp = requests.get(
                f"https://v6.exchangerate-api.com/v6/{api_key}/latest/USD",
                timeout=10,
            )
            resp.raise_for_status()
            rate = float(resp.json()["conversion_rates"]["NGN"])
            log.info("Live USD→NGN rate: %.2f", rate)
            return rate
        except Exception as exc:
            log.warning("Rate fetch failed (%s); using fallback %.0f", exc, FALLBACK_USD_RATE)
    return float(FALLBACK_USD_RATE)


# ─────────────────────────────────────────────────────────────────────────────
# Scrapers
# ─────────────────────────────────────────────────────────────────────────────

def _make_job(
    title: str,
    company: str,
    location: str,
    description: str,
    url: str,
    date_posted: str,
    salary_text: str = "",
    salary_min: float = 0.0,
    salary_max: float = 0.0,
    salary_interval: str = "yearly",
    source: str = "",
) -> dict:
    return {
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip() or "Remote",
        "description": description,
        "job_url": url,
        "date_posted": date_posted,
        "salary_text": salary_text,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_interval": salary_interval,
        "source": source,
    }


# ── 1. Indeed + ZipRecruiter via JobSpy ──────────────────────────────────────

def scrape_jobspy(query: str) -> list[dict]:
    """Indeed + ZipRecruiter only — LinkedIn and Glassdoor block scrapers."""
    try:
        df = scrape_jobs(
            site_name=["indeed", "zip_recruiter"],
            search_term=query,
            location="Remote",
            results_wanted=25,
            hours_old=MAX_AGE_HOURS,
            country_indeed="USA",
        )
        if df is None or df.empty:
            return []
        jobs = []
        for row in df.to_dict("records"):
            s_min = float(row.get("min_amount") or 0)
            s_max = float(row.get("max_amount") or 0)
            interval = str(row.get("interval") or "yearly").lower()
            s_text = ""
            if s_min or s_max:
                label = {"hourly": "/hr", "monthly": "/mo", "yearly": "/yr"}.get(interval, f"/{interval}")
                s_text = f"${s_min:,.0f}–${s_max:,.0f}{label}" if s_max else f"${s_min:,.0f}+{label}"
            # Convert pandas date to ISO string
            raw_date = row.get("date_posted")
            if hasattr(raw_date, "isoformat"):
                date_str = raw_date.isoformat()
            else:
                date_str = str(raw_date or "")
            jobs.append(_make_job(
                title=str(row.get("title") or ""),
                company=str(row.get("company") or ""),
                location=str(row.get("location") or "Remote"),
                description=str(row.get("description") or ""),
                url=str(row.get("job_url") or ""),
                date_posted=date_str,
                salary_text=s_text,
                salary_min=s_min,
                salary_max=s_max,
                salary_interval=interval,
                source=str(row.get("site") or "JobSpy"),
            ))
        log.info("JobSpy '%s': %d results", query, len(jobs))
        return jobs
    except Exception as exc:
        log.warning("JobSpy '%s' error: %s", query, exc)
        return []


# ── 2. We Work Remotely RSS ──────────────────────────────────────────────────

_WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-testing-qa-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-software-dev-jobs.rss",
]

def fetch_weworkremotely() -> list[dict]:
    jobs: list[dict] = []
    for feed_url in _WWR_FEEDS:
        try:
            resp = requests.get(
                feed_url,
                headers={"User-Agent": "Mozilla/5.0 (QA Job Digest Bot)"},
                timeout=15,
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                # WWR format: "Company: Title  [Region]"
                company, _, rest = title.partition(": ")
                job_title = re.sub(r"\s*\[.*?\]", "", rest).strip() or title
                pub_date = item.findtext("pubDate") or ""
                try:
                    dt = parsedate_to_datetime(pub_date).isoformat()
                except Exception:
                    dt = pub_date
                region = item.findtext("region") or "Remote"
                desc = item.findtext(f"content:encoded", namespaces=ns) or item.findtext("description") or ""
                url = item.findtext("link") or item.findtext("guid") or ""
                jobs.append(_make_job(
                    title=job_title,
                    company=company,
                    location=region,
                    description=desc,
                    url=url,
                    date_posted=dt,
                    source="WeWorkRemotely",
                ))
        except Exception as exc:
            log.warning("WWR feed %s error: %s", feed_url, exc)
    log.info("We Work Remotely: %d raw results", len(jobs))
    return jobs


# ── 3. Remotive API ───────────────────────────────────────────────────────────

def fetch_remotive() -> list[dict]:
    jobs: list[dict] = []
    for cat in ["qa", "testing", "devops"]:
        try:
            resp = requests.get(
                "https://remotive.com/api/remote-jobs",
                params={"category": cat, "limit": 50},
                timeout=15,
            )
            resp.raise_for_status()
            for j in resp.json().get("jobs", []):
                jobs.append(_make_job(
                    title=j.get("title", ""),
                    company=j.get("company_name", ""),
                    location=j.get("candidate_required_location", "Remote"),
                    description=j.get("description", ""),
                    url=j.get("url", ""),
                    date_posted=j.get("publication_date", ""),
                    salary_text=j.get("salary", ""),
                    source="Remotive",
                ))
        except Exception as exc:
            log.warning("Remotive '%s' error: %s", cat, exc)
    log.info("Remotive: %d raw results", len(jobs))
    return jobs


# ── 4. RemoteOK API ───────────────────────────────────────────────────────────

def fetch_remoteok() -> list[dict]:
    jobs: list[dict] = []
    for tag in ["qa", "testing", "selenium", "playwright", "automation"]:
        try:
            resp = requests.get(
                f"https://remoteok.com/api?tag={tag}",
                headers={"User-Agent": "Mozilla/5.0 (QA Job Digest Bot)"},
                timeout=15,
            )
            resp.raise_for_status()
            for j in resp.json():
                if not isinstance(j, dict) or "position" not in j:
                    continue
                # epoch field is a Unix timestamp integer
                epoch = j.get("epoch")
                if epoch:
                    dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
                else:
                    dt = j.get("date", "")
                jobs.append(_make_job(
                    title=j.get("position", ""),
                    company=j.get("company", ""),
                    location=j.get("location", "Remote"),
                    description=j.get("description", ""),
                    url=j.get("url", ""),
                    date_posted=dt,
                    source="RemoteOK",
                ))
        except Exception as exc:
            log.warning("RemoteOK '%s' error: %s", tag, exc)
    log.info("RemoteOK: %d raw results", len(jobs))
    return jobs


# ── 5. Greenhouse public ATS API ──────────────────────────────────────────────

def fetch_greenhouse() -> list[dict]:
    jobs: list[dict] = []
    for slug in GREENHOUSE_SLUGS:
        try:
            resp = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                params={"content": "true"},
                timeout=10,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            company_name = slug.replace("-", " ").title()
            for j in resp.json().get("jobs", []):
                location = ""
                for loc in j.get("offices", []) or j.get("location", {}).values():
                    if isinstance(loc, dict):
                        location = loc.get("name", "")
                    else:
                        location = str(loc)
                    break
                if not location:
                    location = j.get("location", {}).get("name", "Remote")
                jobs.append(_make_job(
                    title=j.get("title", ""),
                    company=company_name,
                    location=location or "Remote",
                    description=j.get("content", ""),
                    url=j.get("absolute_url", ""),
                    date_posted=j.get("updated_at", ""),
                    source="Greenhouse",
                ))
        except Exception as exc:
            log.debug("Greenhouse '%s' error: %s", slug, exc)
    log.info("Greenhouse: %d raw results", len(jobs))
    return jobs


# ── 6. Lever public ATS API ───────────────────────────────────────────────────

def fetch_lever() -> list[dict]:
    jobs: list[dict] = []
    for slug in LEVER_SLUGS:
        try:
            resp = requests.get(
                f"https://api.lever.co/v0/postings/{slug}",
                params={"mode": "json"},
                timeout=10,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            company_name = slug.replace("-", " ").title()
            for j in resp.json():
                location = j.get("categories", {}).get("location", "Remote")
                # Lever uses createdAt as millisecond epoch
                created_ms = j.get("createdAt")
                if created_ms:
                    dt = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc).isoformat()
                else:
                    dt = ""
                jobs.append(_make_job(
                    title=j.get("text", ""),
                    company=company_name,
                    location=location,
                    description=j.get("descriptionPlain", "") or j.get("description", ""),
                    url=j.get("hostedUrl", ""),
                    date_posted=dt,
                    source="Lever",
                ))
        except Exception as exc:
            log.debug("Lever '%s' error: %s", slug, exc)
    log.info("Lever: %d raw results", len(jobs))
    return jobs


# ── 7. Jobright (jobright.ai) — server-rendered __NEXT_DATA__ JSON ────────────
#
# Jobright has no public API. The /remote-jobs page is a Next.js app that
# server-renders the first 30 search results into an inline <script id="__NEXT_DATA__">
# JSON blob, which we parse. This is fragile: a frontend refactor could change
# the key path or move the data to client-side hydration. If results suddenly
# drop to 0 from this source, inspect the page HTML and update the JSON path.
#
# robots.txt explicitly allows /jobs/* and /remote-jobs/*, so this is permitted.

_JOBRIGHT_TITLES = ",".join([
    "QA Engineer", "QA Automation Engineer", "SDET",
    "Test Automation Engineer", "Quality Assurance Engineer",
    "Software Test Engineer", "Performance Test Engineer",
    "Security Test Engineer", "Test Engineer", "Automation Engineer",
])

_JOBRIGHT_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_jobright() -> list[dict]:
    """Parse Jobright's server-rendered job results from __NEXT_DATA__."""
    jobs: list[dict] = []
    try:
        resp = requests.get(
            "https://jobright.ai/remote-jobs",
            params={"jobTitle": _JOBRIGHT_TITLES},
            headers={"User-Agent": _JOBRIGHT_BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=20,
        )
        resp.raise_for_status()
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            resp.text, re.DOTALL,
        )
        if not m:
            log.warning("Jobright: __NEXT_DATA__ block not found — page structure may have changed")
            return jobs
        data = json.loads(m.group(1))
        items = data.get("props", {}).get("pageProps", {}).get("defaultData", []) or []
        for item in items:
            jr = item.get("jobResult") or {}
            cr = item.get("companyResult") or {}
            if not jr.get("jobTitle"):
                continue
            # publishTime arrives as "YYYY-MM-DD HH:MM:SS" in UTC
            raw_dt = (jr.get("publishTime") or "").strip()
            date_iso = ""
            if raw_dt:
                try:
                    dt = datetime.strptime(raw_dt, "%Y-%m-%d %H:%M:%S")
                    date_iso = dt.replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    date_iso = raw_dt
            location = jr.get("jobLocation") or ""
            is_remote_flag = str(jr.get("isRemote", "")).lower() in ("true", "1")
            if is_remote_flag:
                location = f"Remote — {location}" if location else "Remote"
            jobs.append(_make_job(
                title=jr.get("jobTitle", ""),
                company=cr.get("companyName") or "Unknown",
                location=location,
                description=jr.get("jobSummary", "") or "",
                url=jr.get("applyLink") or jr.get("url") or "",
                date_posted=date_iso,
                source="Jobright",
            ))
    except Exception as exc:
        log.warning("Jobright fetch error: %s", exc)
    log.info("Jobright: %d raw results", len(jobs))
    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# Filters
# ─────────────────────────────────────────────────────────────────────────────

_QA_TITLE_PATTERN = re.compile(
    r"\b("
    r"qa engineer|qa automation|qa lead|qa manager|qa analyst|qa tester|qa specialist|"
    r"quality assurance|quality engineer|quality analyst|quality lead|quality manager|"
    r"test engineer|test automation|test lead|test manager|test analyst|test architect|"
    r"automation engineer|automation tester|automation qa|"
    r"sdet|software development engineer in test|"
    r"performance test|security test|load test|"
    r"e2e engineer|end.to.end|"
    r"software tester|manual tester|"
    r"mobile qa analyst|senior software quality assurance analyst|software test engineer|"
    r"senior quality engineer|engineer, qa automation|senior qa engineer|"
    r"product tester|senior qa mobile|qa mobile|mobile qa"
    r")\b"
    r"|(?<!\w)qa(?!\w)",          # standalone "QA" not part of another word
    re.IGNORECASE,
)

_TITLE_BLOCKLIST = re.compile(
    r"\b("
    r"sales|erp|backend developer|frontend developer|full.?stack developer|"
    r"customer support|call cent(re|er)|field engineer|instrumentation|"
    r"internal audit|accountant|financial|marketing|data engineer|"
    r"devops engineer|site reliability engineer|sre|"
    r"product manager|project manager|scrum master|business analyst|"
    r"software engineer(?! in test)|staff engineer(?! in test)"
    r")\b",
    re.IGNORECASE,
)


# Canonical QA titles for fuzzy-match fallback. Mirrors _QA_TITLE_PATTERN
# alternations but as plain strings so SequenceMatcher can compare against them.
_CANONICAL_QA_TITLES = [
    "qa engineer", "qa automation engineer", "qa lead", "qa manager",
    "qa analyst", "qa tester", "qa specialist",
    "qa mobile", "mobile qa", "mobile qa analyst", "senior qa mobile",
    "senior qa engineer", "engineer qa automation",
    "quality assurance engineer", "quality assurance analyst",
    "senior software quality assurance analyst",
    "quality engineer", "quality analyst", "quality lead", "quality manager",
    "senior quality engineer",
    "test engineer", "test automation engineer", "test lead", "test manager",
    "test analyst", "test architect", "software test engineer",
    "automation engineer", "automation tester",
    "sdet", "software development engineer in test",
    "performance test engineer", "security test engineer", "load test engineer",
    "software tester", "manual tester", "product tester",
]

FUZZY_TITLE_THRESHOLD = 0.80


def _normalize_title(s: str) -> str:
    """Lowercase + collapse punctuation to spaces for fair fuzzy comparison."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def _best_qa_fuzzy_score(title: str) -> tuple[float, str]:
    """Highest similarity ratio between a normalized title and any canonical QA title."""
    norm = _normalize_title(title)
    if not norm:
        return 0.0, ""
    best_score = 0.0
    best_match = ""
    for canonical in _CANONICAL_QA_TITLES:
        # Exact substring → 1.0 (handles "Senior X", "X II", "X (Remote)" etc.)
        if canonical in norm:
            return 1.0, canonical
        score = SequenceMatcher(None, norm, canonical).ratio()
        if score > best_score:
            best_score = score
            best_match = canonical
    return best_score, best_match


def is_qa_relevant(job: dict) -> bool:
    title = job.get("title", "")
    if _TITLE_BLOCKLIST.search(title):
        return False
    if _QA_TITLE_PATTERN.search(title):
        return True
    # Fuzzy fallback: catch titles the strict regex misses (abbreviations,
    # unusual punctuation, slight word variations).
    score, canonical = _best_qa_fuzzy_score(title)
    if score >= FUZZY_TITLE_THRESHOLD:
        log.info("Fuzzy match: %r ≈ %r (%.2f)", title, canonical, score)
        return True
    return False


def parse_posted_dt(job: dict) -> Optional[datetime]:
    raw = job.get("date_posted")
    if raw is None:
        return None
    if hasattr(raw, "tzinfo"):          # datetime / pandas Timestamp
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)
    if hasattr(raw, "year") and not hasattr(raw, "hour"):   # date object
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
    s = str(raw).strip()
    if not s or s.lower() in ("none", "nan", "nat", ""):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def is_posted_recently(job: dict) -> bool:
    """Accept jobs posted within MAX_AGE_HOURS; include those with no date."""
    dt = parse_posted_dt(job)
    if dt is None:
        return True   # unknown date — include, flag in email as "date unknown"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    return dt >= cutoff


def is_remote(job: dict) -> bool:
    location = job.get("location", "").lower()
    return "remote" in location or location.strip() in ("", "worldwide", "global", "anywhere")


def is_timezone_ok(job: dict) -> bool:
    text = (job.get("description", "") + " " + job.get("location", "")).upper()
    tz_mentions = re.findall(r"\bUTC[+-]\d+\b|\b[A-Z]{2,4}T\b|UTC[+-]?\d*", text)
    if not tz_mentions:
        return True
    for tok in tz_mentions:
        m = re.match(r"UTC([+-])(\d+)", tok)
        if m:
            sign = 1 if m.group(1) == "+" else -1
            offset = sign * int(m.group(2))
            if -1 <= offset <= 3:
                return True
    for token in ACCEPTED_TZ_TOKENS:
        if token in text:
            return True
    return False


def parse_salary_usd_annual(job: dict) -> Optional[float]:
    if job.get("salary_min") or job.get("salary_max"):
        value = job["salary_max"] or job["salary_min"]
        interval = job.get("salary_interval", "yearly")
        if interval == "hourly":
            return value * 2080
        if interval == "monthly":
            return value * 12
        return value
    text = job.get("salary_text", "") + " " + job.get("description", "")
    amounts = re.findall(r"\$\s?([\d,]+)(?:k)?", text, re.IGNORECASE)
    if not amounts:
        return None
    nums = []
    for a in amounts:
        n = float(a.replace(",", ""))
        ctx = text[text.find(a): text.find(a) + 10].lower()
        if "k" in ctx:
            n *= 1000
        nums.append(n)
    annual = max(nums)
    if annual < 20_000:
        annual *= 12
    return annual


def salary_ok(job: dict, usd_rate: float) -> bool:
    annual = parse_salary_usd_annual(job)
    if annual is None:
        return True
    min_annual_usd = (MIN_NGN_MONTHLY * 12) / usd_rate
    return annual >= min_annual_usd


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────────────────────────────────────

def deduplicate(jobs: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for job in jobs:
        key = (job["title"].lower().strip(), job["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# Keyword extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_keywords(description: str) -> list[str]:
    desc_upper = description.upper()
    return [kw for kw in SKILL_KEYWORDS if kw.upper() in desc_upper]


# ─────────────────────────────────────────────────────────────────────────────
# HTML email builder
# ─────────────────────────────────────────────────────────────────────────────

_CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;background:#f4f6f9;margin:0;padding:20px}
.wrapper{max-width:800px;margin:0 auto;background:#fff;border-radius:10px;
         overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.1)}
.header{background:linear-gradient(135deg,#1a73e8,#0d47a1);color:#fff;padding:28px 32px}
.header h1{margin:0;font-size:22px;letter-spacing:.4px}
.header p{margin:6px 0 0;opacity:.85;font-size:13px}
.body{padding:24px 32px}
.summary{background:#e8f0fe;border-left:4px solid #1a73e8;padding:12px 16px;
         border-radius:0 6px 6px 0;margin-bottom:24px;font-size:13px;color:#333}
.source-bar{margin-bottom:20px;font-size:12px;color:#555}
.source-bar span{display:inline-block;background:#f1f3f4;border-radius:4px;
                 padding:2px 8px;margin:2px 4px 2px 0}
.job-card{border:1px solid #e0e0e0;border-radius:8px;margin-bottom:20px;overflow:hidden}
.job-header{background:#fafafa;padding:14px 18px;border-bottom:1px solid #e0e0e0}
.job-title{font-size:16px;font-weight:600;color:#1a73e8;margin:0}
.job-meta{font-size:12px;color:#666;margin:4px 0 0}
.job-body{padding:14px 18px}
.badge{display:inline-block;padding:3px 9px;border-radius:12px;font-size:11px;
       font-weight:600;margin-right:6px;margin-bottom:6px}
.badge-blue{background:#e8f0fe;color:#1a73e8}
.badge-green{background:#e6f4ea;color:#1e8e3e}
.badge-orange{background:#fef3e2;color:#e37400}
.badge-red{background:#fce8e6;color:#c5221f}
.badge-purple{background:#f3e8fd;color:#7b1fa2}
.badge-grey{background:#f1f3f4;color:#555}
.kw-section{margin-top:12px}
.kw-section h4{font-size:12px;color:#555;margin:0 0 6px;text-transform:uppercase;letter-spacing:.5px}
.apply-btn{display:inline-block;margin-top:12px;padding:9px 20px;background:#1a73e8;
           color:#fff!important;text-decoration:none;border-radius:5px;font-size:13px;
           font-weight:600}
.footer{background:#f4f6f9;padding:16px 32px;font-size:11px;color:#999;text-align:center}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Output sanitization — every field below originates from external job boards
# (Indeed posters, Lever boards, etc.) and must be treated as untrusted.
# ─────────────────────────────────────────────────────────────────────────────

_MAX_FIELD_LEN = 500   # cap any single field to avoid runaway HTML payloads


def _safe_text(value, max_len: int = _MAX_FIELD_LEN) -> str:
    """HTML-escape a value, coerce to string, and truncate to max_len chars."""
    if value is None:
        return ""
    s = str(value)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return html.escape(s, quote=True)


def _safe_url(value) -> str:
    """Return value only if it is an http(s) URL; otherwise '#'."""
    if not value:
        return "#"
    try:
        parsed = urlparse(str(value))
    except ValueError:
        return "#"
    if parsed.scheme not in ("http", "https"):
        return "#"
    if not parsed.netloc:
        return "#"
    return html.escape(str(value), quote=True)


def _valid_job(job: dict) -> bool:
    """Reject jobs that are missing required string fields or malformed."""
    title = job.get("title")
    company = job.get("company")
    if not isinstance(title, str) or not title.strip():
        return False
    if not isinstance(company, str) or not company.strip():
        return False
    return True


def _salary_str(job: dict, usd_rate: float) -> str:
    annual = parse_salary_usd_annual(job)
    if annual is None:
        return "Not disclosed"
    monthly_ngn = (annual / 12) * usd_rate
    if job.get("salary_text"):
        return f"₦{monthly_ngn:,.0f}/mo  ({_safe_text(job['salary_text'], 100)})"
    return f"₦{monthly_ngn:,.0f}/mo  (≈${annual/1000:.0f}k/yr)"


def _type_badges(job: dict) -> str:
    t = (job["title"] + " " + job["description"]).lower()
    out = []
    if any(k in t for k in ["selenium","playwright","cypress","appium","webdriver"]):
        out.append('<span class="badge badge-blue">UI Automation</span>')
    if any(k in t for k in ["jmeter","k6","gatling","locust","performance","load test"]):
        out.append('<span class="badge badge-orange">Performance</span>')
    if any(k in t for k in ["security","owasp","burp","pentest","sast","dast"]):
        out.append('<span class="badge badge-red">Security</span>')
    if any(k in t for k in ["api","rest","graphql","postman","restassured"]):
        out.append('<span class="badge badge-green">API Testing</span>')
    if any(k in t for k in ["mobile","ios","android","xcuitest","espresso"]):
        out.append('<span class="badge badge-purple">Mobile</span>')
    return "".join(out) or '<span class="badge badge-blue">QA</span>'


def _card(job: dict, usd_rate: float, idx: int) -> str:
    # SKILL_KEYWORDS is a hardcoded allowlist, so kw_html is safe by construction.
    kws = extract_keywords(job.get("description", ""))
    kw_html = " ".join(f'<span class="badge badge-blue">{k}</span>' for k in kws[:20])

    company_raw = (job.get("company") or "Unknown").strip()
    is_p = company_raw.lower() in PRIORITY_COMPANIES
    company_safe = _safe_text(company_raw, 200)
    company_label = f"⭐ {company_safe}" if is_p else company_safe

    dt = parse_posted_dt(job)
    date_label = dt.strftime("%Y-%m-%d") if dt else "date unknown"

    title_safe    = _safe_text(job.get("title", ""), 250)
    location_safe = _safe_text(job.get("location", "Remote"), 150)
    source_safe   = _safe_text(job.get("source", ""), 50)
    url_safe      = _safe_url(job.get("job_url"))

    return f"""
<div class="job-card">
  <div class="job-header">
    <p class="job-title">{idx}. {title_safe}</p>
    <p class="job-meta">
      🏢 {company_label} &nbsp;|&nbsp; 📍 {location_safe}
      &nbsp;|&nbsp; 📅 {date_label} &nbsp;|&nbsp; 🔗 {source_safe}
    </p>
  </div>
  <div class="job-body">
    <div>{_type_badges(job)}</div>
    <p style="margin:10px 0 4px;font-size:13px">
      <strong>💰 Salary:</strong> {_salary_str(job, usd_rate)}
    </p>
    <div class="kw-section">
      <h4>Resume keywords from this posting</h4>
      {kw_html or '<span style="color:#999;font-size:12px">No matched keywords</span>'}
    </div>
    <a class="apply-btn" href="{url_safe}" target="_blank" rel="noopener noreferrer">Apply Now →</a>
  </div>
</div>"""


def build_email_html(jobs: list[dict], usd_rate: float, today: str, stats: dict) -> str:
    count = len(jobs)
    cards = "".join(_card(j, usd_rate, i + 1) for i, j in enumerate(jobs))
    source_chips = "".join(
        f'<span>{src}: {n}</span>'
        for src, n in sorted(stats.items())
    )
    no_results = '<p style="color:#999;text-align:center;padding:40px 0">No new QA jobs matched your criteria in the last 72 h. Check back tomorrow!</p>'
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>🔍 Daily QA Jobs Digest — {today}</h1>
    <p>Remote QA / Automation roles within WAT ±2 h &nbsp;|&nbsp; salary ≥ ₦2M/mo &nbsp;|&nbsp; posted ≤ 72 h ago</p>
  </div>
  <div class="body">
    <div class="summary">
      Found <strong>{count} qualified job(s)</strong>.
      Exchange rate: <strong>1 USD = ₦{usd_rate:,.0f}</strong>.
    </div>
    <div class="source-bar">Sources checked today: {source_chips}</div>
    {cards if cards else no_results}
  </div>
  <div class="footer">
    Indeed · ZipRecruiter · We Work Remotely · Remotive · RemoteOK · Greenhouse · Lever · Jobright<br>
    Disable the GitHub Actions workflow in your repo to stop receiving these emails.
  </div>
</div>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Email dispatch
# ─────────────────────────────────────────────────────────────────────────────

def send_email(html_body: str, subject: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
    log.info("Email sent → %s", RECIPIENT_EMAIL)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    today = date.today().isoformat()
    log.info("=== QA Job Scraper – %s ===", today)

    usd_rate = get_usd_to_ngn()

    # ── 1. Collect raw jobs from all sources ──────────────────────────────────
    raw: list[dict] = []

    for query in JOBSPY_QUERIES:
        raw.extend(scrape_jobspy(query))

    raw.extend(fetch_weworkremotely())
    raw.extend(fetch_remotive())
    raw.extend(fetch_remoteok())
    raw.extend(fetch_greenhouse())
    raw.extend(fetch_lever())
    raw.extend(fetch_jobright())

    log.info("Total raw records before filtering: %d", len(raw))

    # ── 2. Filter with per-stage counts for debugging ─────────────────────────
    after_shape     = [j for j in raw             if _valid_job(j)]
    after_qa        = [j for j in after_shape     if is_qa_relevant(j)]
    after_recency   = [j for j in after_qa        if is_posted_recently(j)]
    after_remote    = [j for j in after_recency   if is_remote(j)]
    after_tz        = [j for j in after_remote    if is_timezone_ok(j)]
    after_salary    = [j for j in after_tz        if salary_ok(j, usd_rate)]

    log.info(
        "Filter funnel: raw=%d → shape=%d → qa_title=%d → recency=%d → remote=%d → tz=%d → salary=%d",
        len(raw), len(after_shape), len(after_qa), len(after_recency),
        len(after_remote), len(after_tz), len(after_salary),
    )

    qualified = after_salary

    # ── 3. Deduplicate & sort (priority companies first, then newest) ─────────
    qualified = deduplicate(qualified)
    qualified.sort(
        key=lambda j: (
            0 if j["company"].lower().strip() in PRIORITY_COMPANIES else 1,
            -(parse_posted_dt(j) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        ),
    )
    qualified = qualified[:50]

    log.info("Final digest size: %d jobs", len(qualified))

    # ── 4. Source breakdown for email header ──────────────────────────────────
    source_stats: dict[str, int] = {}
    for j in qualified:
        src = j.get("source", "Other")
        source_stats[src] = source_stats.get(src, 0) + 1

    # ── 5. Build & send email ─────────────────────────────────────────────────
    html = build_email_html(qualified, usd_rate, today, source_stats)
    subject = f"[QA Jobs] {len(qualified)} remote role(s) – {today}"
    send_email(html, subject)
    log.info("Done.")


if __name__ == "__main__":
    main()

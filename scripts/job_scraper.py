#!/usr/bin/env python3
"""
Daily QA Job Scraper
Searches multiple job boards for remote QA / QA Automation roles and
emails a formatted digest to the configured recipient.

Environment variables required (set as GitHub Actions secrets):
  SENDER_EMAIL      – Gmail address used to send the digest
  SENDER_PASSWORD   – Gmail App Password (not your account password)

Optional:
  SMTP_HOST         – defaults to smtp.gmail.com
  SMTP_PORT         – defaults to 587
  EXCHANGE_RATE_API_KEY – exchangerate-api.com key for live USD→NGN rate
"""

import os
import re
import json
import logging
import smtplib
import textwrap
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

# ── Salary gate ──────────────────────────────────────────────────────────────
# Minimum 2,000,000 NGN/month  (~$1,290 at 1 USD = 1550 NGN)
MIN_NGN_MONTHLY   = 2_000_000
FALLBACK_USD_RATE = 1_550          # used when live rate fetch fails
MIN_USD_MONTHLY   = MIN_NGN_MONTHLY / FALLBACK_USD_RATE   # ≈ $1,290
MIN_USD_ANNUAL    = MIN_USD_MONTHLY * 12                  # ≈ $15,484

# ── Timezone acceptance ───────────────────────────────────────────────────────
# WAT = UTC+1; accept roles in UTC-1 → UTC+3 (±2 h from WAT)
ACCEPTED_TZ_TOKENS = {
    # UTC-1
    "CVT", "EGT", "AZOT",
    # UTC 0
    "GMT", "UTC", "WET", "UT",
    # UTC+1  (WAT)
    "WAT", "CET", "MET", "WEST",
    # UTC+2
    "CAT", "CEST", "WAST", "EET", "SAST",
    # UTC+3
    "EAT", "MSK", "AST", "EEST",
    # Region keywords that imply an accepted timezone
    "EUROPE", "UK", "EMEA", "AFRICA", "NIGERIA", "GHANA", "KENYA",
    "SOUTH AFRICA", "GERMANY", "FRANCE", "NETHERLANDS", "POLAND",
    "SPAIN", "PORTUGAL", "ISRAEL", "TURKEY", "ROMANIA", "UKRAINE",
    "WORLDWIDE", "GLOBAL", "ANYWHERE",
}

# ── Search configuration ─────────────────────────────────────────────────────
SEARCH_QUERIES = [
    "QA Engineer",
    "QA Automation Engineer",
    "SDET",
    "Test Automation Engineer",
    "Quality Assurance Engineer",
    "Performance Test Engineer",
    "Security Test Engineer",
    "Software Test Engineer",
]

# Keywords we look for in descriptions to surface as resume hints
SKILL_KEYWORDS = [
    # Automation frameworks
    "Selenium", "Playwright", "Cypress", "Appium", "WebdriverIO",
    "TestNG", "JUnit", "pytest", "NUnit", "xUnit",
    "Robot Framework", "Cucumber", "SpecFlow", "Gherkin",
    # Languages
    "Python", "Java", "JavaScript", "TypeScript", "C#", "Go", "Kotlin",
    # CI/CD & DevOps
    "Jenkins", "GitHub Actions", "GitLab CI", "CircleCI", "Travis CI",
    "Docker", "Kubernetes", "Terraform", "Ansible",
    # Performance
    "JMeter", "k6", "Gatling", "Locust", "LoadRunner",
    # Security
    "OWASP", "Burp Suite", "ZAP", "Nessus", "Snyk", "Penetration Testing",
    "SAST", "DAST", "Vulnerability",
    # API & Services
    "REST", "GraphQL", "gRPC", "Postman", "RestAssured", "Karate",
    # Databases
    "SQL", "PostgreSQL", "MySQL", "MongoDB",
    # Monitoring & Observability
    "Grafana", "Prometheus", "Datadog", "Splunk", "ELK",
    # Methodology
    "BDD", "TDD", "Agile", "Scrum", "Shift-left",
    "Contract Testing", "Pact", "A/B Testing",
    # Cloud
    "AWS", "GCP", "Azure", "Lambda", "S3",
    # Mobile
    "iOS", "Android", "XCUITest", "Espresso",
]

# Verified / well-known startup companies to prioritise
PRIORITY_COMPANIES = {
    "stripe", "notion", "linear", "vercel", "figma", "retool",
    "datadog", "hashicorp", "confluent", "postman", "dbt labs",
    "airbyte", "segment", "mixpanel", "amplitude", "heap",
    "browserstack", "lambdatest", "sauce labs", "testim",
    "mabl", "rainforest qa", "percy", "chromatic",
    "github", "gitlab", "atlassian", "jetbrains", "circleci",
    "sonarqube", "snyk", "aqua security", "checkov",
    "grafana labs", "prometheus", "new relic", "dynatrace",
    "pagerduty", "incident.io", "rootly",
}


# ─────────────────────────────────────────────────────────────────────────────
# Exchange-rate helper
# ─────────────────────────────────────────────────────────────────────────────

def get_usd_to_ngn() -> float:
    """Fetch live USD→NGN rate; fall back to constant if API is unavailable."""
    api_key = os.getenv("EXCHANGE_RATE_API_KEY")
    if api_key:
        try:
            url = f"https://v6.exchangerate-api.com/v6/{api_key}/latest/USD"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            rate = resp.json()["conversion_rates"]["NGN"]
            log.info("Live USD→NGN rate: %.2f", rate)
            return float(rate)
        except Exception as exc:
            log.warning("Rate fetch failed (%s); using fallback %d", exc, FALLBACK_USD_RATE)
    return float(FALLBACK_USD_RATE)


# ─────────────────────────────────────────────────────────────────────────────
# Job-board scrapers
# ─────────────────────────────────────────────────────────────────────────────

def scrape_jobspy(query: str) -> list[dict]:
    """Scrape LinkedIn, Indeed, Glassdoor, and ZipRecruiter via JobSpy."""
    try:
        df = scrape_jobs(
            site_name=["linkedin", "indeed", "glassdoor", "zip_recruiter"],
            search_term=query,
            location="Remote",
            results_wanted=20,
            hours_old=25,           # only jobs posted in the last 25 h
            country_indeed="USA",
        )
        jobs = df.to_dict("records") if not df.empty else []
        log.info("JobSpy '%s': %d results", query, len(jobs))
        return jobs
    except Exception as exc:
        log.warning("JobSpy error for '%s': %s", query, exc)
        return []


def fetch_remotive() -> list[dict]:
    """Fetch QA jobs from the Remotive public API."""
    categories = ["qa", "testing", "devops"]
    jobs: list[dict] = []
    for cat in categories:
        try:
            resp = requests.get(
                "https://remotive.com/api/remote-jobs",
                params={"category": cat, "limit": 50},
                timeout=15,
            )
            resp.raise_for_status()
            for j in resp.json().get("jobs", []):
                jobs.append({
                    "title": j.get("title", ""),
                    "company": j.get("company_name", ""),
                    "location": j.get("candidate_required_location", "Remote"),
                    "description": j.get("description", ""),
                    "job_url": j.get("url", ""),
                    "date_posted": j.get("publication_date", ""),
                    "salary_text": j.get("salary", ""),
                    "source": "Remotive",
                    "company_logo": j.get("company_logo", ""),
                })
        except Exception as exc:
            log.warning("Remotive '%s' error: %s", cat, exc)
    log.info("Remotive: %d raw results", len(jobs))
    return jobs


def fetch_remoteok() -> list[dict]:
    """Fetch QA/test jobs from the RemoteOK public API."""
    tags = ["qa", "testing", "selenium", "playwright", "automation"]
    jobs: list[dict] = []
    for tag in tags:
        try:
            resp = requests.get(
                f"https://remoteok.com/api?tag={tag}",
                headers={"User-Agent": "Mozilla/5.0 (QA Job Scraper)"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            for j in data:
                if not isinstance(j, dict) or "position" not in j:
                    continue
                jobs.append({
                    "title": j.get("position", ""),
                    "company": j.get("company", ""),
                    "location": j.get("location", "Remote"),
                    "description": j.get("description", ""),
                    "job_url": j.get("url", ""),
                    "date_posted": j.get("date", ""),
                    "salary_text": "",
                    "source": "RemoteOK",
                    "company_logo": j.get("company_logo", ""),
                })
        except Exception as exc:
            log.warning("RemoteOK '%s' error: %s", tag, exc)
    log.info("RemoteOK: %d raw results", len(jobs))
    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation helpers
# ─────────────────────────────────────────────────────────────────────────────

def normalise_jobspy_row(row: dict) -> dict:
    """Convert a JobSpy DataFrame row into our canonical dict."""
    salary_min = row.get("min_amount") or 0
    salary_max = row.get("max_amount") or 0
    salary_interval = str(row.get("interval") or "yearly").lower()
    salary_text = ""
    if salary_min or salary_max:
        interval_label = {"hourly": "/hr", "monthly": "/mo", "yearly": "/yr"}.get(
            salary_interval, f"/{salary_interval}"
        )
        salary_text = (
            f"${salary_min:,.0f}–${salary_max:,.0f}{interval_label}"
            if salary_max
            else f"${salary_min:,.0f}+{interval_label}"
        )
    return {
        "title": str(row.get("title") or ""),
        "company": str(row.get("company") or ""),
        "location": str(row.get("location") or "Remote"),
        "description": str(row.get("description") or ""),
        "job_url": str(row.get("job_url") or ""),
        "date_posted": str(row.get("date_posted") or ""),
        "salary_text": salary_text,
        "salary_min": float(salary_min),
        "salary_max": float(salary_max),
        "salary_interval": salary_interval,
        "source": str(row.get("site") or "JobSpy"),
        "company_logo": "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Filters
# ─────────────────────────────────────────────────────────────────────────────

# Match ONLY against the job title — descriptions are far too noisy and let
# unrelated roles (Sales, Backend, Support, etc.) slip through.
_QA_TITLE_PATTERN = re.compile(
    r"\b("
    r"qa engineer|qa automation|qa lead|qa manager|qa analyst|qa tester|qa specialist|"
    r"quality assurance|quality engineer|quality analyst|quality lead|quality manager|"
    r"test engineer|test automation|test lead|test manager|test analyst|test architect|"
    r"automation engineer|automation tester|automation qa|"
    r"sdet|software development engineer in test|"
    r"performance test|security test|load test|"
    r"e2e engineer|end.to.end|"
    r"software tester|manual tester|qa$"
    r")\b",
    re.IGNORECASE,
)

# Explicit blocklist — titles that contain QA-adjacent words but are not QA roles
_TITLE_BLOCKLIST = re.compile(
    r"\b("
    r"sales|erp|backend developer|frontend developer|full.?stack|"
    r"customer support|call cent(re|er)|field engineer|instrumentation|"
    r"audit|accountant|financial|marketing|data engineer|devops engineer|"
    r"product manager|project manager|scrum master|business analyst"
    r")\b",
    re.IGNORECASE,
)


def is_qa_relevant(job: dict) -> bool:
    title = job.get("title", "")
    if _TITLE_BLOCKLIST.search(title):
        return False
    return bool(_QA_TITLE_PATTERN.search(title))


def parse_posted_dt(job: dict) -> Optional[datetime]:
    """Parse date_posted into an aware UTC datetime, or return None."""
    raw = job.get("date_posted")
    if raw is None:
        return None
    # pandas Timestamp / datetime.datetime
    if hasattr(raw, "tzinfo"):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)
    # datetime.date (no time component)
    if hasattr(raw, "year") and not hasattr(raw, "hour"):
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
    s = str(raw).strip()
    if not s or s.lower() in ("none", "nan", "nat", ""):
        return None
    # ISO datetime: "2026-05-08T10:30:00" or "...Z"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # Date-only string: "2026-05-08"
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        return d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def is_posted_within_24h(job: dict) -> bool:
    """Accept only jobs posted within the last 25 hours (buffer for clock skew)."""
    dt = parse_posted_dt(job)
    if dt is None:
        log.debug("Skipping '%s' — no parseable date", job.get("title", "?"))
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=25)
    return dt >= cutoff


def is_remote(job: dict) -> bool:
    location = job.get("location", "").lower()
    return "remote" in location or location.strip() == ""


def is_timezone_ok(job: dict) -> bool:
    """Return True if the job mentions an acceptable timezone or no timezone at all."""
    text = (job.get("description", "") + " " + job.get("location", "")).upper()

    # If no timezone restriction is mentioned, assume it's open → accept
    tz_mentions = re.findall(r"\bUTC[+-]\d+\b|\b[A-Z]{2,4}T\b|UTC[+-]?\d*", text)
    if not tz_mentions:
        return True

    # Check for explicit UTC offsets in ±2 h of WAT (UTC+1 = +1)
    for tok in tz_mentions:
        match = re.match(r"UTC([+-])(\d+)", tok)
        if match:
            sign = 1 if match.group(1) == "+" else -1
            offset = sign * int(match.group(2))
            if -1 <= offset <= 3:   # WAT±2
                return True

    # Check for known tz abbreviations / region keywords
    for token in ACCEPTED_TZ_TOKENS:
        if token in text:
            return True

    return False


def parse_salary_usd_annual(job: dict) -> Optional[float]:
    """
    Return the best annual USD salary estimate we can derive.
    Returns None if the posting provides no salary data.
    """
    # JobSpy structured salary
    if job.get("salary_min") or job.get("salary_max"):
        value = job["salary_max"] or job["salary_min"]
        interval = job.get("salary_interval", "yearly")
        if interval == "hourly":
            return value * 2080
        if interval == "monthly":
            return value * 12
        return value   # yearly

    # Text-based fallback (Remotive / RemoteOK)
    text = job.get("salary_text", "") + " " + job.get("description", "")
    amounts = re.findall(r"\$\s?([\d,]+)(?:k)?", text, re.IGNORECASE)
    if not amounts:
        return None

    nums = [float(a.replace(",", "")) * (1000 if "k" in text[text.find(a):text.find(a)+10].lower() else 1)
            for a in amounts]
    annual_guess = max(nums)
    # If value looks like a monthly rate (< 20k), annualise it
    if annual_guess < 20_000:
        annual_guess *= 12
    return annual_guess


def salary_ok(job: dict, usd_rate: float) -> bool:
    """Return True if salary is above threshold OR undisclosed (we include those)."""
    annual = parse_salary_usd_annual(job)
    if annual is None:
        return True   # include jobs that don't disclose salary
    min_annual_usd = (MIN_NGN_MONTHLY * 12) / usd_rate
    return annual >= min_annual_usd


# ─────────────────────────────────────────────────────────────────────────────
# Keyword extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_keywords(description: str) -> list[str]:
    """Return matched skill keywords found in the job description."""
    found = []
    desc_upper = description.upper()
    for kw in SKILL_KEYWORDS:
        if kw.upper() in desc_upper:
            found.append(kw)
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────────────────────────────────────

def deduplicate(jobs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for job in jobs:
        key = (job["title"].lower().strip(), job["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


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
.job-card{border:1px solid #e0e0e0;border-radius:8px;margin-bottom:20px;overflow:hidden}
.job-header{background:#fafafa;padding:14px 18px;display:flex;
            align-items:center;gap:14px;border-bottom:1px solid #e0e0e0}
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
.kw-section{margin-top:12px}
.kw-section h4{font-size:12px;color:#555;margin:0 0 6px;text-transform:uppercase;letter-spacing:.5px}
.apply-btn{display:inline-block;margin-top:12px;padding:9px 20px;background:#1a73e8;
           color:#fff!important;text-decoration:none;border-radius:5px;font-size:13px;
           font-weight:600}
.footer{background:#f4f6f9;padding:16px 32px;font-size:11px;color:#999;text-align:center}
"""

def salary_to_ngn(job: dict, usd_rate: float) -> str:
    annual = parse_salary_usd_annual(job)
    if annual is None:
        return "Not disclosed"
    monthly_ngn = (annual / 12) * usd_rate
    if job.get("salary_text"):
        return f"₦{monthly_ngn:,.0f}/mo  ({job['salary_text']})"
    return f"₦{monthly_ngn:,.0f}/mo  (${annual/1000:.0f}k/yr)"


def build_badge(text: str, colour: str) -> str:
    return f'<span class="badge badge-{colour}">{text}</span>'


def job_type_badges(job: dict) -> str:
    title_desc = (job["title"] + " " + job["description"]).lower()
    badges = []
    if any(k in title_desc for k in ["selenium","playwright","cypress","appium","webdriver"]):
        badges.append(build_badge("UI Automation", "blue"))
    if any(k in title_desc for k in ["jmeter","k6","gatling","locust","performance","load test"]):
        badges.append(build_badge("Performance", "orange"))
    if any(k in title_desc for k in ["security","owasp","burp","pentest","sast","dast"]):
        badges.append(build_badge("Security", "red"))
    if any(k in title_desc for k in ["api","rest","graphql","postman","restassured"]):
        badges.append(build_badge("API Testing", "green"))
    if any(k in title_desc for k in ["mobile","ios","android","appium","xcuitest","espresso"]):
        badges.append(build_badge("Mobile", "purple"))
    return "".join(badges) or build_badge("QA", "blue")


def job_to_html_card(job: dict, usd_rate: float, index: int) -> str:
    keywords = extract_keywords(job["description"])
    kw_html = " ".join(
        f'<span class="badge badge-blue">{kw}</span>' for kw in keywords[:20]
    )
    salary_str = salary_to_ngn(job, usd_rate)
    company = job["company"] or "Unknown"
    location = job["location"] or "Remote"
    source = job.get("source", "")
    date_str = str(job.get("date_posted", ""))[:10]
    url = job.get("job_url", "#")
    is_priority = company.lower().strip() in PRIORITY_COMPANIES
    company_label = f"⭐ {company}" if is_priority else company

    return f"""
<div class="job-card">
  <div class="job-header">
    <div>
      <p class="job-title">{index}. {job['title']}</p>
      <p class="job-meta">
        🏢 {company_label} &nbsp;|&nbsp; 📍 {location}
        &nbsp;|&nbsp; 📅 {date_str} &nbsp;|&nbsp; 🔗 {source}
      </p>
    </div>
  </div>
  <div class="job-body">
    <div>{job_type_badges(job)}</div>
    <p style="margin:10px 0 4px;font-size:13px">
      <strong>💰 Salary:</strong> {salary_str}
    </p>
    <div class="kw-section">
      <h4>Resume keywords from this posting</h4>
      {kw_html if kw_html else '<span style="color:#999;font-size:12px">No matched keywords</span>'}
    </div>
    <a class="apply-btn" href="{url}" target="_blank">Apply Now →</a>
  </div>
</div>
"""


def build_email_html(jobs: list[dict], usd_rate: float, today: str) -> str:
    count = len(jobs)
    cards = "".join(job_to_html_card(j, usd_rate, i + 1) for i, j in enumerate(jobs))
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>{_CSS}</style></head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>🔍 Daily QA Jobs Digest — {today}</h1>
    <p>Remote QA / Automation roles within WAT ±2 h | salary ≥ ₦2M/mo equivalent</p>
  </div>
  <div class="body">
    <div class="summary">
      Found <strong>{count} qualified job(s)</strong> posted in the last 24 hours.
      Exchange rate used: <strong>1 USD = ₦{usd_rate:,.0f}</strong>.
    </div>
    {cards if cards else '<p style="color:#999;text-align:center;padding:40px 0">No new jobs matched your criteria today. Check back tomorrow!</p>'}
  </div>
  <div class="footer">
    Scraped from LinkedIn · Indeed · Glassdoor · ZipRecruiter · Remotive · RemoteOK<br>
    Unsubscribe by disabling the GitHub Actions workflow in your repository.
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

    # ── 1. Collect raw jobs ───────────────────────────────────────────────────
    raw: list[dict] = []

    # JobSpy (LinkedIn / Indeed / Glassdoor / ZipRecruiter)
    for query in SEARCH_QUERIES:
        for row in scrape_jobspy(query):
            raw.append(normalise_jobspy_row(row))

    # Free REST APIs
    raw.extend(fetch_remotive())
    raw.extend(fetch_remoteok())

    log.info("Total raw records: %d", len(raw))

    # ── 2. Filter ─────────────────────────────────────────────────────────────
    qualified = []
    for job in raw:
        if not is_qa_relevant(job):        # title must match QA role pattern
            continue
        if not is_posted_within_24h(job):  # hard 25 h recency gate
            continue
        if not is_remote(job):
            continue
        if not is_timezone_ok(job):
            continue
        if not salary_ok(job, usd_rate):
            continue
        qualified.append(job)

    log.info("After filtering: %d jobs", len(qualified))

    # ── 3. Deduplicate & sort (priority companies first) ──────────────────────
    qualified = deduplicate(qualified)
    qualified.sort(
        key=lambda j: (
            0 if j["company"].lower().strip() in PRIORITY_COMPANIES else 1,
            j.get("date_posted", "") or "",
        ),
        reverse=False,
    )
    qualified = qualified[:50]   # cap at 50 per digest

    log.info("Final digest size: %d jobs", len(qualified))

    # ── 4. Build & send email ─────────────────────────────────────────────────
    html = build_email_html(qualified, usd_rate, today)
    subject = f"[QA Jobs] {len(qualified)} remote QA role(s) – {today}"
    send_email(html, subject)

    log.info("Done.")


if __name__ == "__main__":
    main()

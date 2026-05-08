# QA Job Daily Scraper

Runs every weekday at **10:00 WAT (09:00 UTC)** via GitHub Actions.  
Searches six job boards for remote QA / QA Automation roles, filters them,
and emails a formatted digest to **shola.mich.7438@gmail.com**.

---

## What it does

| Step | Detail |
|------|--------|
| **Sources** | LinkedIn · Indeed · Glassdoor · ZipRecruiter (via JobSpy) + Remotive API + RemoteOK API |
| **Roles** | QA Engineer, QA Automation Engineer, SDET, Test Automation Engineer, Performance Test Engineer, Security Test Engineer |
| **Timezone filter** | Roles in UTC−1 → UTC+3 (WAT ± 2 h); worldwide/global roles always included |
| **Salary gate** | ≥ ₦2,000,000/month equivalent (≈ $1,290/mo at current rate); undisclosed salaries are **included** |
| **Company signals** | Priority companies (Stripe, Notion, Datadog, BrowserStack, etc.) sorted to top |
| **Resume keywords** | Each job card shows matched skills from the job description |
| **Deduplication** | Cross-source duplicates are removed |

---

## One-time setup

### 1 — Fork / push this repo to GitHub

```bash
git remote add origin https://github.com/<YOUR_USERNAME>/QA-job-website-daily-scraping.git
git push -u origin main
```

### 2 — Enable GitHub Actions

Go to **Actions** tab → click **"I understand my workflows, go ahead and enable them"** if prompted.

### 3 — Add repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Value |
|-------------|-------|
| `SENDER_EMAIL` | The Gmail address you want to send from (e.g. `yourname@gmail.com`) |
| `SENDER_PASSWORD` | A **Gmail App Password** — NOT your account password (see below) |
| `EXCHANGE_RATE_API_KEY` | *(optional)* Free key from [exchangerate-api.com](https://www.exchangerate-api.com/) for live USD→NGN rates |

#### How to create a Gmail App Password

1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Select app **Mail** → device **Other** → name it `QA Job Scraper`
3. Copy the 16-character password — paste it as `SENDER_PASSWORD`

> If you don't see App Passwords, enable **2-Step Verification** first.

### 4 — Test immediately

Go to **Actions → Daily QA Job Digest → Run workflow** to trigger a manual run
without waiting until tomorrow.

---

## Project structure

```
.
├── .github/
│   └── workflows/
│       └── daily_job_scraper.yml   ← GitHub Actions schedule
└── scripts/
    ├── job_scraper.py              ← main scraper + mailer
    └── requirements.txt            ← Python dependencies
```

---

## Customisation

| What to change | Where |
|----------------|-------|
| Add / remove job titles | `SEARCH_QUERIES` list in `job_scraper.py` |
| Adjust timezone window | `ACCEPTED_TZ_TOKENS` set & the offset check in `is_timezone_ok()` |
| Change minimum salary | `MIN_NGN_MONTHLY` constant |
| Add priority companies | `PRIORITY_COMPANIES` set |
| Add skill keywords | `SKILL_KEYWORDS` list |
| Run on weekends too | Change `1-5` → `*` in the cron expression |

---

## Email sample

Each job card in the digest shows:

- Job title, company (⭐ for priority companies), location, source, date posted
- Type badges: UI Automation · Performance · Security · API Testing · Mobile
- Salary estimate in ₦/month with original currency
- Resume keyword chips matched from the job description
- Direct **Apply Now →** link

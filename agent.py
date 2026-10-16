"""
AI Job Agent - finds Azure Data Engineer jobs, scores them with AI, and sends them
to Gmail and WhatsApp. Runs free on GitHub Actions.

Sources
  - Adzuna API           (optional; now asks for a card)
  - Jooble API           (free key by email)  every run
  - JSearch via RapidAPI (free key)           once a day  -> LinkedIn, Indeed, Glassdoor,
                                                               Naukri & company sites via Google for Jobs
  - Your Gmail inbox     (job-alert emails)   every run   -> LinkedIn, Naukri, Indeed, foundit,
                                                               Instahyre, Glassdoor alert emails
AI
  - Groq API (free key, no card) scores every job 0-100 against your profile,
    reads experience and salary from the description. Without a key, a rule-based
    score is used instead.
Notify
  - Gmail (HTML digest, sent from and to your own Gmail with an App Password)
  - WhatsApp via CallMeBot (top matches)      - Telegram (optional backup)
All keys come from environment variables (GitHub Actions secrets).
"""
import email, hashlib, html, imaplib, json, os, re, smtplib, sys, time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

# ======================= YOUR SEARCH SETTINGS ===========================
# Every source searches each of these terms (full job text is searched by the site itself).
SEARCH_TERMS = ["PySpark", "Databricks", "Azure Data Factory", "ADF", "ADLS", "Synapse", "Python"]
QUERIES = SEARCH_TERMS
COUNTRY = "in"                      # India

# A job is sent if its title or description contains ANY ONE of these keywords.
# Nothing else is checked (no experience, salary, title or AI-score filter).
KEYWORDS = ["pyspark", "databricks", "adf", "azure data factory", "adls", "python", "synapse"]

MAX_WHATSAPP = 8                    # only used if WhatsApp/Telegram secrets are added
EXP_MIN, EXP_MAX, MIN_SALARY_LPA = 5, 10, 18   # shown in the email text only, not used as filters

PROFILE = """Azure Data Engineer, 8 years IT experience (4+ years data engineering).
Skills: Azure Data Factory (ADF), ADLS Gen2, Azure Synapse Analytics, Azure Databricks, PySpark,
Python, SQL, T-SQL, SQL Server, Delta Lake, Medallion architecture, incremental loads, data migration,
Azure DevOps CI/CD, Key Vault, Unity Catalog, Power BI.
Wants: Data Engineer / Senior Data Engineer / Azure Data Engineer roles in India, 5-10 years, 18 LPA+."""
# The AI score is only used to SORT jobs (best first). It never removes a job.
LINKEDIN_PAGES = 20                  # 25 jobs per page, posted in the last 24 hours
NAUKRI_PAGES = 20                    # 20 jobs per page, posted in the last day
JSEARCH_TERMS = 4                   # JSearch free quota is small: only the first N search terms, morning run only
JOOBLE_PAGES = 3                    # pages of Jooble results per search (about 20 jobs per page)
ALERT_DAYS = 3                      # how many days back to read job-alert emails
KEEP_DAYS = 30
# ========================================================================

ROOT = os.path.dirname(os.path.abspath(__file__))
JOBS_FILE = os.path.join(ROOT, "docs", "jobs.json")
IST = timezone(timedelta(hours=5, minutes=30))
env = lambda k, d="": (os.environ.get(k) or d).strip()
UA = {"User-Agent": "Mozilla/5.0 (job-alert-bot)"}


# ----------------------------------------------------------------- helpers
def clean(t):
    return re.sub(r"\s+", " ", html.unescape(t or "")).strip()


def job_id(title, company, url=""):
    """Same job = same posting id in its link (or same link). Different postings are never merged by title."""
    u = (url or "").split("?")[0].rstrip("/")
    m = re.search(r"(?:jobs/view/|jk=|jobid=|/j/|-)(\d{6,})$", u) or re.search(r"(?:jobs/view/|jk=|jobid=)(\d{6,})", url or "")
    key = (urlsplit(u).netloc + ":" + m.group(1)) if m else (u or re.sub(r"[^a-z0-9]", "", f"{title}{company}".lower()))
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def parse_experience(text):
    """Return (min, max) years if the text states it, else (None, None)."""
    t = (text or "").lower().replace("–", "-").replace("—", "-")
    m = re.search(r"(\d{1,2})\s*(?:\+)?\s*(?:-|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b <= 40:
            return a, b
    m = re.search(r"(?:minimum|min\.?|at least|over)?\s*(\d{1,2})\s*\+\s*(?:years?|yrs?)", t) or \
        re.search(r"(\d{1,2})\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience|exp)", t)
    if m:
        a = int(m.group(1))
        if a <= 40:
            return a, None
    return None, None


def parse_salary_lpa(text):
    """Find '18-25 LPA', '20 LPA', 'Rs 18,00,000 - 25,00,000', '₹20L' style salaries. Returns (min, max) LPA."""
    t = (text or "").lower().replace(",", "")
    m = re.search(r"(\d{1,2}(?:\.\d)?)\s*(?:-|to)\s*(\d{1,3}(?:\.\d)?)\s*(?:lpa|lakhs?|l\b|lacs?)", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d{1,3}(?:\.\d)?)\s*(?:lpa|lakhs? per annum|lacs? per annum)", t)
    if m:
        return float(m.group(1)), None
    m = re.search(r"(?:₹|rs\.?|inr)\s*(\d{6,8})\s*(?:-|to)\s*(?:₹|rs\.?|inr)?\s*(\d{6,8})", t)
    if m:
        return int(m.group(1)) / 1e5, int(m.group(2)) / 1e5
    return None, None


KW_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b", re.I)


def rule_filter(j):
    """Only rule: the job's title or details contain at least one keyword (whole word, any case).
    No role, title, experience, salary or score check."""
    if j.get("search_kw"):          # the job site itself matched your keyword in the full job text
        return True
    return bool(KW_RE.search(f"{j.get('title', '')} {j.get('desc', '')}"))


def enrich(j):
    text = f"{j.get('exp_text', '')} {j['title']} {j.get('desc', '')} {j.get('salary_text', '')}"
    if j.get("exp_min") is None:
        j["exp_min"], j["exp_max"] = parse_experience(text)
    if j.get("sal_min") is None:
        j["sal_min"], j["sal_max"] = parse_salary_lpa(text)
    return j


# ----------------------------------------------------------------- sources
def fetch_adzuna(query):
    app_id, app_key = env("ADZUNA_APP_ID"), env("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    out = []
    r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}/search/1", timeout=30, params={
        "app_id": app_id, "app_key": app_key, "what": query, "results_per_page": 50,
        "max_days_old": 3, "sort_by": "date", "content-type": "application/json"})
    r.raise_for_status()
    for x in r.json().get("results", []):
        smin, smax = x.get("salary_min"), x.get("salary_max")
        estimated = str(x.get("salary_is_predicted", "0")) == "1"
        out.append({
            "title": clean(x.get("title")), "company": (x.get("company") or {}).get("display_name", ""),
            "location": (x.get("location") or {}).get("display_name", ""), "url": x.get("redirect_url", ""),
            "source": "Adzuna", "posted": x.get("created", ""), "desc": clean(x.get("description")),
            # Adzuna's predicted salaries are guesses, so only trust real ones
            "sal_min": round(smin / 1e5, 1) if smin and not estimated else None,
            "sal_max": round(smax / 1e5, 1) if smax and not estimated else None,
        })
    return out


def fetch_jooble(query):
    """Jooble: free key by email (jooble.org/api/about), no card. Aggregates Indian job boards."""
    key = env("JOOBLE_API_KEY")
    if not key:
        return []
    raw = []
    for page in range(1, JOOBLE_PAGES + 1):
        r = requests.post(f"https://jooble.org/api/{key}", timeout=30,
                          json={"keywords": query, "location": "India", "page": str(page)})
        r.raise_for_status()
        jobs = r.json().get("jobs", []) or []
        raw += jobs
        if len(jobs) < 10:
            break
    out = []
    for x in raw:
        out.append({
            "title": clean(BeautifulSoup(x.get("title", ""), "html.parser").get_text()),
            "company": clean(x.get("company")), "location": clean(x.get("location")) or "India",
            "url": x.get("link", ""), "source": f"Jooble ({clean(x.get('source')) or 'web'})",
            "posted": x.get("updated", ""),
            "desc": clean(BeautifulSoup(x.get("snippet", ""), "html.parser").get_text()),
            "salary_text": clean(x.get("salary")), "search_kw": query,
        })
    return out


JSEARCH_HOST = os.environ.get("JSEARCH_HOST") or "jsearch.p.rapidapi.com"
# JSearch renamed its endpoint in newer API versions; try the known names until one exists.
JSEARCH_PATHS = [p for p in [os.environ.get("JSEARCH_PATH")] if p] + ["/job-search", "/search-v2", "/jobs/search", "/search"]
_jsearch_path = None


def fetch_jsearch(query):
    global _jsearch_path
    key = env("RAPIDAPI_KEY")
    if not key:
        return []
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": JSEARCH_HOST}
    params = {"query": f"{query} in India", "page": 1, "num_pages": 1, "country": "in", "date_posted": "3days"}
    for path in ([_jsearch_path] if _jsearch_path else JSEARCH_PATHS):
        r = requests.get(f"https://{JSEARCH_HOST}{path}", timeout=40, headers=headers, params=params)
        if r.status_code == 404 and not _jsearch_path:
            print(f"  JSearch path {path} not found, trying next")
            time.sleep(1.5)
            continue
        r.raise_for_status()
        if not _jsearch_path:
            print(f"  JSearch working path: {path}")
        _jsearch_path = path
        break
    else:
        raise RuntimeError("no JSearch search path worked; send Claude the Code snippet from RapidAPI")
    out = []
    for x in r.json().get("data", []) or []:
        months = ((x.get("job_required_experience") or {}).get("required_experience_in_months"))
        smin, smax, period = x.get("job_min_salary"), x.get("job_max_salary"), (x.get("job_salary_period") or "").upper()
        mult = {"YEAR": 1, "MONTH": 12}.get(period)
        out.append({
            "title": clean(x.get("job_title")), "company": clean(x.get("employer_name")),
            "location": ", ".join(p for p in [x.get("job_city"), x.get("job_state")] if p) or "India",
            "url": x.get("job_apply_link") or x.get("job_google_link") or "",
            "source": f"JSearch ({x.get('job_publisher') or 'web'})", "posted": x.get("job_posted_at_datetime_utc", ""),
            "desc": clean(x.get("job_description"))[:4000],
            "exp_min": round(months / 12) if months else None, "exp_max": None,
            "sal_min": round(smin * mult / 1e5, 1) if (smin and mult) else None,
            "sal_max": round(smax * mult / 1e5, 1) if (smax and mult) else None,
        })
    return out


BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/129.0 Safari/537.36", "Accept-Language": "en-IN,en;q=0.9"}


def _pause(a=3, b=6):
    import random
    time.sleep(random.uniform(a, b))


def fetch_linkedin(term):
    """LinkedIn public (no login) job search, India, posted in the last 24 hours."""
    out = []
    for start in range(0, LINKEDIN_PAGES * 25, 25):
        r = requests.get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search", timeout=30,
                         headers=BROWSER, params={"keywords": term, "location": "India", "f_TPR": "r86400",
                                                  "start": start})
        if r.status_code == 429:
            print(f"  LinkedIn is rate-limiting (429) after {len(out)} jobs for '{term}'")
            break
        r.raise_for_status()
        cards = BeautifulSoup(r.text, "html.parser").select("li")
        got = 0
        for c in cards:
            t = c.select_one("h3.base-search-card__title")
            link = c.select_one("a.base-card__full-link") or c.select_one("a[href*='/jobs/view/']")
            if not t or not link:
                continue
            comp = c.select_one("h4.base-search-card__subtitle")
            loc = c.select_one("span.job-search-card__location")
            when = c.select_one("time")
            out.append({"title": clean(t.get_text()), "company": clean(comp.get_text()) if comp else "",
                        "location": clean(loc.get_text()) if loc else "India",
                        "url": link["href"].split("?")[0], "source": "LinkedIn",
                        "posted": when.get("datetime", "") if when else "", "desc": "", "search_kw": term})
            got += 1
        if got < 10:
            break
        _pause()
    return out


def fetch_naukri(term):
    """Naukri search (the same JSON their website uses), posted in the last day."""
    slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
    headers = dict(BROWSER, **{"appid": "109", "systemid": "Naukri", "clientid": "d3skt0p",
                               "Accept": "application/json", "Referer": f"https://www.naukri.com/{slug}-jobs"})
    out = []
    for page in range(1, NAUKRI_PAGES + 1):
        r = requests.get("https://www.naukri.com/jobapi/v3/search", timeout=30, headers=headers, params={
            "noOfResults": 20, "urlType": "search_by_keyword", "searchType": "adv", "keyword": term,
            "pageNo": page, "k": term, "seoKey": f"{slug}-jobs", "src": "jobsearchDesk", "latLong": "", "jobAge": 1})
        r.raise_for_status()
        jobs = r.json().get("jobDetails") or []
        for x in jobs:
            ph = {p.get("type"): p.get("label", "") for p in x.get("placeholders", []) or []}
            url = x.get("jdURL") or ""
            out.append({"title": clean(x.get("title")), "company": clean(x.get("companyName")),
                        "location": clean(ph.get("location")), "source": "Naukri",
                        "url": url if url.startswith("http") else f"https://www.naukri.com{url}",
                        "posted": x.get("footerPlaceholderLabel", ""),
                        "desc": clean(BeautifulSoup(x.get("jobDescription", "") or "", "html.parser").get_text())
                                + " " + clean(x.get("tagsAndSkills")),
                        "salary_text": ph.get("salary", ""), "exp_text": ph.get("experience", ""),
                        "search_kw": term})
        if len(jobs) < 20:
            break
        _pause(2, 4)
    return out


def _walk_jobs(obj, found):
    """Find job-like records inside a page's embedded JSON."""
    if isinstance(obj, dict):
        title = obj.get("title") or obj.get("designation") or obj.get("jobTitle")
        jid = obj.get("id") or obj.get("jobId")
        cd = obj.get("companyData") if isinstance(obj.get("companyData"), dict) else {}
        comp = obj.get("companyName") or cd.get("companyName")
        if isinstance(title, str) and jid and (comp or obj.get("jobDetailUrl") or obj.get("min") is not None):
            found.append(obj)
        for v in obj.values():
            _walk_jobs(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_jobs(v, found)


def fetch_hirist(term):
    """Hirist public search page (tech jobs, India)."""
    slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
    r = requests.get(f"https://www.hirist.tech/search/{slug}-jobs", timeout=30, headers=BROWSER,
                     params={"minexp": 3, "maxexp": 15})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    # 1) embedded JSON (Next.js style pages)
    for sc in soup.find_all("script"):
        txt = sc.string or ""
        if sc.get("id") == "__NEXT_DATA__" or (txt.strip().startswith("{") and '"title"' in txt):
            try:
                recs = []
                _walk_jobs(json.loads(txt), recs)
            except Exception:
                continue
            for x in recs:
                url = x.get("jobDetailUrl") or f"https://www.hirist.tech/j/{x.get('id')}"
                if url in seen:
                    continue
                seen.add(url)
                cd = x.get("companyData") if isinstance(x.get("companyData"), dict) else {}
                out.append({"title": clean(x.get("title") or x.get("designation")),
                            "company": clean(x.get("companyName") or cd.get("companyName")),
                            "location": clean(", ".join(l.get("name", "") for l in x.get("locations", []) if isinstance(l, dict))),
                            "url": url, "source": "Hirist", "posted": "", "desc": clean(str(x.get("tags", ""))),
                            "search_kw": term})
    # 2) plain links like /j/company-role-123456
    for a in soup.find_all("a", href=True):
        if re.search(r"/j/[\w-]+-\d{4,}", a["href"]):
            url = a["href"] if a["href"].startswith("http") else "https://www.hirist.tech" + a["href"]
            if url in seen:
                continue
            seen.add(url)
            text = clean(a.get_text(" ")) or re.sub(r"-\d+$", "", url.rsplit("/", 1)[-1]).replace("-", " ").title()
            out.append({"title": text[:120], "company": "", "location": "", "url": url, "source": "Hirist",
                        "posted": "", "desc": "", "search_kw": term})
    return out


ALERT_SENDERS = {
    "linkedin.com": "LinkedIn", "naukri.com": "Naukri", "naukrigulf": None, "hirist.tech": "Hirist",
    "hirist.com": "Hirist", "instahyre.com": "Instahyre", "foundit.in": "foundit", "indeed.com": "Indeed",
    "glassdoor.com": "Glassdoor", "glassdoor.co.in": "Glassdoor", "cutshort.io": "Cutshort",
    "iimjobs.com": "iimjobs", "wellfound.com": "Wellfound", "shine.com": "Shine",
}
JOB_WORDS = re.compile(r"engineer|developer|architect|lead|consultant|analyst|specialist|manager|scientist|"
                       r"data|etl|databricks|pyspark|azure|spark|big ?data|sde", re.I)
SKIP_TEXT = {"view job", "apply now", "apply", "view details", "see job", "easy apply", "view all jobs", "see all jobs",
             "view similar jobs", "unsubscribe", "manage alerts", "job alert", "search jobs", "view more jobs",
             "see more jobs", "edit alert", "help", "privacy policy", "terms", "settings"}


def _strip_tracking(url):
    p = urlsplit(url)
    if "linkedin.com" in p.netloc:
        m = re.search(r"/jobs/view/(\d+)", p.path)
        if m:
            return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return url


def _alert_jobs(body, name, date):
    """Pull job title links out of a job-alert email. Works with tracked/redirect links too."""
    soup = BeautifulSoup(body, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href, text = a["href"], clean(a.get_text(" "))
        if not href.startswith("http") or not (6 <= len(text) <= 120) or text.lower() in SKIP_TEXT:
            continue
        if not (JOB_WORDS.search(text) or KW_RE.search(text)) or re.search(r"unsubscribe|privacy|settings|help", href, re.I):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        # company / location / skills usually follow the title link in the email
        nxt = [clean(t) for t in a.find_all_next(string=True, limit=14)]
        nxt = [t for t in nxt if t and t != text]
        after = " ".join(nxt)[:250]
        company = clean(re.split(r"[·|•]", nxt[0])[0])[:60] if nxt else ""
        out.append({"title": text, "company": company, "location": "", "url": _strip_tracking(href),
                    "source": f"{name} alert email", "posted": date, "desc": after, "from_email": True})
    return out


def fetch_gmail_alerts():
    """Read job-alert emails from your Gmail (read-only, All Mail, last ALERT_DAYS days)."""
    user, pwd = env("GMAIL_USER"), env("GMAIL_APP_PASSWORD").replace(" ", "")
    if not (user and pwd):
        return []
    out, per_source = [], {}
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pwd)
    typ, _ = M.select('"[Gmail]/All Mail"', readonly=True)   # catches alerts in Promotions/Updates/labels too
    if typ != "OK":
        M.select("INBOX", readonly=True)
    since = (datetime.now() - timedelta(days=ALERT_DAYS)).strftime("%d-%b-%Y")
    for domain, name in ALERT_SENDERS.items():
        if not name:
            continue
        typ, data = M.search(None, f'(SINCE {since} FROM "{domain}")')
        nums = data[0].split() if data and data[0] else []
        for num in nums[-40:]:
            typ, msg_data = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                    break
            if body:
                got = _alert_jobs(body, name, msg.get("Date", ""))
                per_source[name] = per_source.get(name, 0) + len(got)
                out += got
        if nums:
            print(f"  {name}: {len(nums)} alert emails, {per_source.get(name, 0)} jobs")
    M.logout()
    return out


# ----------------------------------------------------------------- AI scoring
def ai_score(jobs):
    """Score jobs with Groq (free). Falls back to a keyword score if no key or on error."""
    key = env("GROQ_API_KEY")
    models = groq_models(key) if key else []
    for i in range(0, len(jobs), 8):
        batch = jobs[i:i + 8]
        done = False
        if key:
            items = [{"id": k, "title": j["title"], "company": j["company"], "location": j["location"],
                      "text": (j.get("desc") or "")[:1500],
                      "note": "title only (from a job-alert email); judge on title" if j.get("from_email") else ""} for k, j in enumerate(batch)]
            prompt = (f"Candidate profile:\n{PROFILE}\n\nRate how well each job fits this candidate (0-100). "
                      "Penalise jobs that need skills the candidate lacks as core (e.g. AWS/GCP-only, Java/Scala-only, "
                      "pure BI/analyst, SAP), or seniority far outside 5-10 years. Extract experience and salary only if stated "
                      "(salary in INR lakhs per annum). Reply with JSON only: "
                      '{"jobs":[{"id":0,"score":85,"reason":"<=15 words","exp_min":5,"exp_max":9,"sal_min_lpa":null,"sal_max_lpa":null}]}'
                      f"\n\nJobs:\n{json.dumps(items, ensure_ascii=False)}")
            for model in models:
                try:
                    r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=60,
                                      headers={"Authorization": f"Bearer {key}"},
                                      json={"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                                            "messages": [{"role": "user", "content": prompt}]})
                    if r.status_code == 429:
                        time.sleep(20)
                        continue
                    if r.status_code >= 400:
                        print(f"AI scoring with {model} failed: HTTP {r.status_code} {r.text[:200]}")
                        continue
                    res = json.loads(r.json()["choices"][0]["message"]["content"]).get("jobs", [])
                    for x in res:
                        j = batch[int(x["id"])]
                        j["score"] = int(x.get("score", 0))
                        j["reason"] = x.get("reason", "")
                        for a, b in [("exp_min", "exp_min"), ("exp_max", "exp_max"), ("sal_min", "sal_min_lpa"), ("sal_max", "sal_max_lpa")]:
                            if j.get(a) is None and isinstance(x.get(b), (int, float)):
                                j[a] = x[b]
                    done = True
                    break
                except Exception as e:
                    print(f"AI scoring with {model} failed: {e}")
            time.sleep(4)          # stay under Groq's free tokens-per-minute limit
        if not done:
            for j in batch:
                j.setdefault("score", keyword_score(j))
                j.setdefault("reason", "keyword match")
    return jobs


def groq_models(key):
    """Ask Groq which chat models exist right now (model names change over time) and pick good ones."""
    wanted = [env("GROQ_MODEL")] if env("GROQ_MODEL") else []
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", timeout=30, headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", []) if m.get("active", True)]
    except Exception as e:
        print(f"Could not list Groq models: {e}")
        return wanted or ["llama-3.3-70b-versatile"]
    skip = ("whisper", "tts", "guard", "embed", "vision", "audio", "playai", "compound", "orpheus")
    chat = [i for i in ids if not any(w in i.lower() for w in skip)]
    prefer = ["gpt-oss-120b", "llama-4", "llama-3.3-70b", "qwen", "gpt-oss-20b", "llama"]
    ranked = sorted(chat, key=lambda i: next((n for n, p in enumerate(prefer) if p in i.lower()), 99))
    picked = (wanted + ranked)[:2]
    print(f"Groq models available: {len(chat)}; using {picked}")
    return picked


def keyword_score(j):
    text = f"{j['title']} {j.get('desc', '')}".lower()
    weights = {"azure": 15, "data factory": 12, "adf": 12, "databricks": 15, "pyspark": 15, "synapse": 10,
               "adls": 8, "python": 6, "delta": 5, "sql": 4}
    s = 30 + sum(w for k, w in weights.items() if k in text)
    if "senior" in j["title"].lower() or "lead" in j["title"].lower():
        s += 5
    if j.get("from_email"):   # alerts you subscribed to: only the title is known, so trust the title match
        s += 25
    return min(s, 95)


# ----------------------------------------------------------------- notify
def fmt_exp(j):
    lo, hi = j.get("exp_min"), j.get("exp_max")
    return f"{lo}-{hi} yrs" if lo is not None and hi is not None else (f"{lo}+ yrs" if lo is not None else "exp n/a")


def fmt_sal(j):
    lo, hi = j.get("sal_min"), j.get("sal_max")
    if lo and hi:
        return f"₹{lo:g}-{hi:g} LPA"
    if lo or hi:
        return f"₹{(lo or hi):g} LPA"
    return "salary not disclosed"


def send_email(jobs):
    user, pwd, to = env("GMAIL_USER"), env("GMAIL_APP_PASSWORD").replace(" ", ""), env("EMAIL_TO") or env("GMAIL_USER")
    if not (user and pwd) or not jobs:
        return
    rows = []
    for j in jobs:
        badge = "#1a7f37" if j["score"] >= 80 else "#9a6700" if j["score"] >= 70 else "#57606a"
        rows.append(f"""
<tr><td style="padding:12px 14px;border-bottom:1px solid #e5e7eb">
  <div style="font-size:15px;font-weight:600"><a href="{html.escape(j['url'])}" style="color:#1d4ed8;text-decoration:none">{html.escape(j['title'])}</a></div>
  <div style="color:#374151;font-size:13px;margin-top:2px">{html.escape(j['company'] or '-')} &middot; {html.escape(j['location'] or 'India')}</div>
  <div style="color:#6b7280;font-size:12px;margin-top:4px">{fmt_exp(j)} &middot; {fmt_sal(j)} &middot; {html.escape(j['source'])}</div>
  <div style="color:#4b5563;font-size:12px;margin-top:4px;font-style:italic">{html.escape(j.get('reason', ''))}</div>
</td><td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;text-align:right;vertical-align:top">
  <span style="background:{badge};color:#fff;border-radius:12px;padding:3px 9px;font-size:12px;font-weight:600">{j['score']}</span>
</td></tr>""")
    dash = env("DASHBOARD_URL")
    body = f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;margin:auto">
<h2 style="color:#2E3A4F;margin-bottom:4px">{len(jobs)} new Azure Data Engineer jobs</h2>
<div style="color:#6b7280;font-size:13px;margin-bottom:12px">Jobs mentioning any of: {", ".join(KEYWORDS)}. Sorted by AI match score.</div>
<table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px">{''.join(rows)}</table>
{f'<p style="font-size:13px"><a href="{dash}">Open your job dashboard</a></p>' if dash else ''}
<p style="color:#9ca3af;font-size:11px">Sent by your AI Job Agent on GitHub Actions.</p></div>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Job Agent] {len(jobs)} new jobs - top: {jobs[0]['title']} @ {jobs[0]['company'] or 'n/a'}"
    msg["From"], msg["To"] = f"AI Job Agent <{user}>", to
    msg.attach(MIMEText("\n\n".join(f"{j['title']} | {j['company']} | {fmt_exp(j)} | {fmt_sal(j)}\n{j['url']}" for j in jobs), "plain"))
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print(f"Email sent to {to}")


def send_whatsapp(text):
    phone, key = env("CALLMEBOT_PHONE"), env("CALLMEBOT_APIKEY")
    if not (phone and key):
        return
    try:
        r = requests.get(f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={quote(text)}&apikey={key}", timeout=40)
        print("WhatsApp:", r.status_code)
    except Exception as e:
        print("WhatsApp failed:", e)


def send_telegram(text):
    tok, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=30,
                          data={"chat_id": chat, "text": text, "disable_web_page_preview": True})
        except Exception as e:
            print("Telegram failed:", e)


def notify_chat(jobs):
    if not jobs:
        return
    top = jobs[:MAX_WHATSAPP]
    lines = [f"*{j['score']}* | {j['title']}\n{j['company'] or '-'} | {fmt_exp(j)} | {fmt_sal(j)}\n{j['url']}" for j in top]
    head = f"🔔 {len(jobs)} new Azure DE jobs (top {len(top)}):\n\n"
    tail = f"\n\nFull list in your Gmail." + (f" Dashboard: {env('DASHBOARD_URL')}" if env("DASHBOARD_URL") else "")
    # CallMeBot works best with short messages, so send in chunks of 4 jobs
    for i in range(0, len(lines), 4):
        msg = (head if i == 0 else "") + "\n\n".join(lines[i:i + 4]) + (tail if i + 4 >= len(lines) else "")
        send_whatsapp(msg)
        send_telegram(msg)
        time.sleep(3)


# ----------------------------------------------------------------- main
def main():
    use_jsearch = "--with-jsearch" in sys.argv
    dry = "--dry-run" in sys.argv
    try:
        store = json.load(open(JOBS_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        store = {"updated": "", "jobs": []}
    seen = {j["id"] for j in store["jobs"]} | set(store.get("seen_ids", []))

    found, blocked = [], set()
    print("---- Source check ----")
    for name, keys in [("Adzuna", ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"]), ("Jooble", ["JOOBLE_API_KEY"]),
                       ("JSearch", ["RAPIDAPI_KEY"]), ("Gmail", ["GMAIL_USER", "GMAIL_APP_PASSWORD"]),
                       ("Groq AI", ["GROQ_API_KEY"])]:
        missing = [k for k in keys if not env(k)]
        print(f"{name:8}: {'MISSING secret ' + ', '.join(missing) if missing else 'key found'}")
    if not use_jsearch:
        print("JSearch : skipped on this run (it runs on the morning run and on manual runs)")
    print("----------------------")
    sources = [fetch_linkedin, fetch_naukri, fetch_hirist, fetch_jooble, fetch_adzuna]
    per_source = {}
    for n, q in enumerate(SEARCH_TERMS):
        run = list(sources) + ([fetch_jsearch] if use_jsearch and n < JSEARCH_TERMS and fetch_jsearch not in blocked else [])
        for fetch in run:
            if fetch in blocked:
                continue
            name = fetch.__name__.replace("fetch_", "")
            if fetch is fetch_jsearch:
                time.sleep(2)
            try:
                got = fetch(q)
                print(f"{name:8} '{q}': {len(got)} jobs")
                per_source[name] = per_source.get(name, 0) + len(got)
                found += got
            except requests.HTTPError as e:
                code = e.response.status_code
                print(f"{name:8} '{q}' FAILED: HTTP {code} {e.response.text[:150]}")
                if code in (401, 403, 406, 999):   # blocked / not allowed: skip this source for the rest of the run
                    print(f"  -> {name} is blocking or rejecting requests; skipping it for this run")
                    blocked.add(fetch)
            except Exception as e:
                print(f"{name:8} '{q}' FAILED: {e}")
                if "no JSearch search path" in str(e):
                    blocked.add(fetch)
    print("Jobs per source:", ", ".join(f"{k} {v}" for k, v in per_source.items()) or "none")
    try:
        got = fetch_gmail_alerts()
        print(f"gmail alert emails: {len(got)} jobs")
        found += got
    except Exception as e:
        print("Gmail alert reading FAILED:", e)
        if "Application-specific password" in str(e) or "Invalid credentials" in str(e):
            print("  -> GMAIL_APP_PASSWORD must be a 16-letter App Password from myaccount.google.com/apppasswords,"
                  " not your normal Gmail password")
    print(f"Fetched {len(found)} jobs")

    candidates, ids = [], set()
    dup, old, nokw = 0, 0, 0
    for j in found:
        if not j.get("title") or not j.get("url"):
            continue
        j["id"] = job_id(j["title"], j["company"], j["url"])
        if j["id"] in ids:
            dup += 1
            continue
        ids.add(j["id"])
        if j["id"] in seen:
            old += 1
            continue
        if rule_filter(enrich(j)):
            candidates.append(j)
        else:
            nokw += 1
    print(f"Breakdown: {len(found)} fetched -> {dup} duplicates across searches, {old} already sent earlier, "
          f"{nokw} without any keyword -> {len(candidates)} new matches")
    print(f"New after filters: {len(candidates)}")

    scored = ai_score(candidates)          # score is used for sorting only
    scored.sort(key=lambda j: j["score"], reverse=True)
    print(f"Matches (any keyword): {len(scored)}")

    now = datetime.now(IST).isoformat(timespec="minutes")
    for j in scored:
        j.pop("desc", None)
        j["found"] = now
    if dry:
        for j in scored:
            print(f"{j['score']:>3} | {j['title']} | {j['company']} | {fmt_exp(j)} | {fmt_sal(j)}")
        return
    cutoff = (datetime.now(IST) - timedelta(days=KEEP_DAYS)).isoformat()
    store["jobs"] = [j for j in scored + store["jobs"] if j.get("found", "") >= cutoff]
    # remember rejected ids too, so the same job is not re-scored every run (bounded list)
    store["seen_ids"] = (list(ids) + store.get("seen_ids", []))[:5000]
    store["updated"] = now
    os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
    json.dump(store, open(JOBS_FILE, "w"), indent=1, ensure_ascii=False)

    send_email(scored)
    notify_chat(scored)


if __name__ == "__main__":
    main()

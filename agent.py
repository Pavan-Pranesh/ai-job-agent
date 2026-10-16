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
QUERIES = ["Azure Data Engineer", "PySpark Data Engineer", "Databricks Data Engineer",
           "ADF Data Engineer", "Python Data Engineer Azure"]
COUNTRY = "in"                      # India

# A job is sent if its title or description contains ANY ONE of these keywords.
# Nothing else is checked (no experience, salary, title or AI-score filter).
KEYWORDS = ["pyspark", "databricks", "adf", "azure data factory", "adls", "python", "synapse" , "azure data factory" , "fabric" , "azure sql","data lake","delta" ]

MAX_WHATSAPP = 8                    # only used if WhatsApp/Telegram secrets are added
EXP_MIN, EXP_MAX, MIN_SALARY_LPA = 5, 10, 18   # shown in the email text only, not used as filters

PROFILE = """Azure Data Engineer, 8 years IT experience (4+ years data engineering).
Skills: Azure Data Factory (ADF), ADLS Gen2, Azure Synapse Analytics, Azure Databricks, PySpark,
Python, SQL, T-SQL, SQL Server, Delta Lake, Medallion architecture, incremental loads, data migration,
Azure DevOps CI/CD, Key Vault, Unity Catalog, Power BI.
Wants: Data Engineer / Senior Data Engineer / Azure Data Engineer roles in India, 5-10 years, 18 LPA+."""
# The AI score is only used to SORT jobs (best first). It never removes a job.
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
    m = re.search(r"(?:jobs/view/|jk=|jobid=|-)(\d{6,})", url or "")
    key = m.group(1) if m else re.sub(r"[^a-z0-9]", "", f"{title}{company}".lower())
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
    """Only rule: the job mentions at least one keyword (whole word, any case)."""
    return bool(KW_RE.search(f"{j.get('title', '')} {j.get('desc', '')}"))


def enrich(j):
    text = f"{j['title']} {j.get('desc', '')} {j.get('salary_text', '')}"
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
    r = requests.post(f"https://jooble.org/api/{key}", timeout=30,
                      json={"keywords": query, "location": "India", "page": "1"})
    r.raise_for_status()
    out = []
    for x in r.json().get("jobs", []) or []:
        out.append({
            "title": clean(BeautifulSoup(x.get("title", ""), "html.parser").get_text()),
            "company": clean(x.get("company")), "location": clean(x.get("location")) or "India",
            "url": x.get("link", ""), "source": f"Jooble ({clean(x.get('source')) or 'web'})",
            "posted": x.get("updated", ""),
            "desc": clean(BeautifulSoup(x.get("snippet", ""), "html.parser").get_text()),
            "salary_text": clean(x.get("salary")),
        })
    return out


def fetch_jsearch(query):
    key = env("RAPIDAPI_KEY")
    if not key:
        return []
    r = requests.get("https://jsearch.p.rapidapi.com/search", timeout=40,
                     headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
                     params={"query": f"{query} in India", "page": 1, "num_pages": 1,
                             "country": "in", "date_posted": "3days"})
    r.raise_for_status()
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


ALERT_SENDERS = {
    "linkedin.com": "LinkedIn", "naukri.com": "Naukri", "indeed.com": "Indeed", "foundit.in": "foundit",
    "instahyre.com": "Instahyre", "glassdoor.com": "Glassdoor", "hirist.tech": "Hirist", "cutshort.io": "Cutshort",
}
JOB_LINK = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/|naukri\.com/(?:job-listings|.*jid=)|indeed\.com/(?:rc/clk|viewjob|pagead)|"
                      r"foundit\.in/job/|instahyre\.com/job|glassdoor\.co\.in/job-listing|glassdoor\.com/job-listing|"
                      r"hirist\.tech/j/|cutshort\.io/job/", re.I)


def _strip_tracking(url):
    p = urlsplit(url)
    if "linkedin.com" in p.netloc:
        m = re.search(r"/jobs/view/(\d+)", p.path)
        if m:
            return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return urlunsplit((p.scheme, p.netloc, p.path, p.query if "indeed" in p.netloc or "jid=" in p.query else "", ""))


def fetch_gmail_alerts():
    """Read job-alert emails (last 2 days) from your Gmail, read-only, and pull out job links."""
    user, pwd = env("GMAIL_USER"), env("GMAIL_APP_PASSWORD").replace(" ", "")
    if not (user and pwd):
        return []
    out = []
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pwd)
    M.select("INBOX", readonly=True)
    since = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
    for domain, name in ALERT_SENDERS.items():
        typ, data = M.search(None, f'(SINCE {since} FROM "{domain}")')
        for num in (data[0].split() if data and data[0] else [])[-25:]:
            typ, msg_data = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                    break
            if not body:
                continue
            soup = BeautifulSoup(body, "html.parser")
            seen = set()
            for a in soup.find_all("a", href=True):
                href, text = a["href"], clean(a.get_text(" "))
                if not JOB_LINK.search(href) or len(text) < 6 or len(text) > 120 or \
                        text.lower() in {"view job", "apply now", "apply", "view details", "see job", "easy apply", "view all jobs"}:
                    continue
                url = _strip_tracking(href)
                if url in seen:
                    continue
                seen.add(url)
                # the company/location usually sit in the text right after the title link
                after = clean(" ".join(s for s in a.find_all_next(string=True, limit=8)))[len(text):len(text) + 160]
                out.append({"title": text, "company": "", "location": "", "url": url,
                            "source": f"{name} alert email", "posted": msg.get("Date", ""),
                            "desc": after, "from_email": True})
    M.logout()
    return out


# ----------------------------------------------------------------- AI scoring
def ai_score(jobs):
    """Score jobs with Groq (free). Falls back to a keyword score if no key or on error."""
    key = env("GROQ_API_KEY")
    models = [env("GROQ_MODEL", "llama-3.3-70b-versatile"), "llama-3.1-8b-instant"]
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
                    r.raise_for_status()
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
            time.sleep(2)
        if not done:
            for j in batch:
                j.setdefault("score", keyword_score(j))
                j.setdefault("reason", "keyword match")
    return jobs


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

    found = []
    for q in QUERIES:
        for fetch in [fetch_adzuna, fetch_jooble] + ([fetch_jsearch] if use_jsearch else []):
            try:
                found += fetch(q)
            except Exception as e:
                print(f"{fetch.__name__}('{q}') failed: {e}")
    try:
        found += fetch_gmail_alerts()
    except Exception as e:
        print("Gmail alert reading failed:", e)
    print(f"Fetched {len(found)} jobs")

    candidates, ids = [], set()
    for j in found:
        if not j.get("title") or not j.get("url"):
            continue
        j["id"] = job_id(j["title"], j["company"], j["url"])
        if j["id"] in seen or j["id"] in ids:
            continue
        ids.add(j["id"])
        if rule_filter(enrich(j)):
            candidates.append(j)
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

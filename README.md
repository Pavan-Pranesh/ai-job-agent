# AI Job Agent (free)

Finds **Azure Data Engineer** jobs (ADF, ADLS Gen2, Synapse, Databricks, PySpark, Python),
filters for **5–10 years** and **18 LPA+**, scores each job with AI against your profile,
and sends the matches to **Gmail** and **WhatsApp** four times a day. It runs on
GitHub Actions, so your laptop and phone don't need to be on.

```
Adzuna API ──┐
JSearch API ─┼─► filters (role, 5-10 yrs, 18 LPA+) ─► AI score (Groq) ─► Gmail digest
Gmail alerts ┘     (LinkedIn / Naukri / Indeed /         0-100 fit        WhatsApp top 8
                     foundit / Instahyre emails)                          Dashboard
```

Runs at about **8:15 AM, 12:15 PM, 4:15 PM and 8:15 PM IST**. You can also run it any time
from the Actions tab.

---

## Setup (about 25 minutes, one time)

### 1. Gmail App Password (sends the email and reads your job-alert emails)
1. Turn on **2-Step Verification**: myaccount.google.com → Security.
2. Open **myaccount.google.com/apppasswords**, create one named `job-agent`.
3. Copy the 16-character password.

### 2. WhatsApp key (CallMeBot, free)
1. Save **+34 621 062 163** in your phone contacts as "CallMeBot".
   (Check callmebot.com/blog/free-api-whatsapp-messages in case the number changed.)
2. From your WhatsApp, send it: `I allow callmebot to send me messages`
3. It replies with your **apikey**.

### 3. AI key (Groq, free, no credit card)
1. Sign up at **console.groq.com** → API Keys → Create key.
2. Copy the key (starts with `gsk_`).

### 4. Job source keys
- **Adzuna** (every run): developer.adzuna.com → sign up → create app → copy **App ID** and **App Key**.
- **JSearch** (once a day; covers LinkedIn, Indeed, Glassdoor, Naukri & company sites via Google for Jobs):
  rapidapi.com → search "JSearch" → subscribe to the free **Basic** plan → copy **X-RapidAPI-Key**.

### 5. Turn on job alerts (so the agent can read them from Gmail)
- **LinkedIn**: search "Azure Data Engineer", set filters (Experience: Mid-Senior, Location: India / Bengaluru),
  turn on **Set alert** → email, daily.
- **Naukri**: search "Azure Data Engineer", Experience 5–10, Salary 18+ Lakhs → **Create job alert**.
- Optional: the same on Indeed, foundit, Instahyre.

### 6. Put the code on GitHub
1. Create a **public** repo named `ai-job-agent` (public = free unlimited Actions minutes and free Pages).
2. Upload everything in this folder, keeping the `.github/workflows` and `docs` folders.

### 7. Add the secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the 16-character App Password from step 1 |
| `EMAIL_TO` | where to send the digest (your Gmail; comma-separate for more) |
| `CALLMEBOT_PHONE` | your WhatsApp number with country code, no `+` (e.g. `91XXXXXXXXXX`) |
| `CALLMEBOT_APIKEY` | from step 2 |
| `GROQ_API_KEY` | from step 3 |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | from step 4 |
| `RAPIDAPI_KEY` | from step 4 |

Secrets stay hidden even in a public repo. Never put them in the code.

### 8. Dashboard (optional)
Settings → **Pages** → Deploy from a branch → `main` / `/docs` → Save.
Your dashboard: `https://<your-username>.github.io/ai-job-agent/`.
Add it under **Secrets and variables → Actions → Variables** as `DASHBOARD_URL` so emails and
WhatsApp messages link to it.

### 9. Test
**Actions** tab → **AI Job Agent** → **Run workflow**. In 2–3 minutes you should get an email
and a WhatsApp message (if there are new matches). The run log shows how many jobs were
fetched, filtered and matched.

---

## Changing what it looks for
Everything is at the top of `agent.py`:

| Setting | What it does |
|---|---|
| `QUERIES` | search phrases sent to Adzuna and JSearch |
| `EXP_MIN`, `EXP_MAX` | experience band (5–10) |
| `MIN_SALARY_LPA` | minimum salary (18) |
| `KEEP_UNKNOWN_SALARY` | keep jobs that don't show salary (most Indian posts don't) |
| `MIN_AI_SCORE` | drop jobs the AI scores below this (60) |
| `MAX_WHATSAPP` | how many top jobs go to WhatsApp per run (8) |
| `PROFILE` | the profile the AI compares jobs against; update it when your resume changes |

## How the filters work
- A job is dropped only when it **states** experience or salary outside your range.
  "Salary not disclosed" jobs are kept and labelled, because most Indian companies hide salary.
  Set `KEEP_UNKNOWN_SALARY = False` if you only want jobs that show 18 LPA+.
- Jobs seen before are never sent twice.
- The AI penalises AWS-only, GCP-only, Java/Scala-only, BI-analyst and SAP roles.

## Free-tier limits
- **JSearch** free plan is small (about 100–200 calls a month), so it runs once a day:
  3 queries × 30 days ≈ 90 calls.
- **Adzuna**: about 360 calls a month here, well inside the free tier.
- **Groq**: free daily request limits are far above what this uses.
- **CallMeBot** is a free hobby service for messaging yourself. It can be slow or down
  sometimes, and email is the reliable channel. For a backup, create a Telegram bot with
  @BotFather and add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets.
- GitHub pauses scheduled workflows after **60 days with no repo activity**. The agent commits
  `docs/jobs.json` on runs with new jobs, which normally keeps it active.

## What it can't do
- **No direct LinkedIn or Naukri scraping.** Both forbid it and block it, and it can get your account
  flagged. The agent reads the alert emails they send you instead, which is allowed.
- It finds and ranks jobs; **it doesn't apply for you.**

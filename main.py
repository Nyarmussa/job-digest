#!/usr/bin/env python3
"""
Job-match engine for Ryan Assum. Two modes:

  python main.py email      -> weekly best-of, emailed via Gmail (Mondays)
  python main.py dashboard  -> daily list, written to docs/ for GitHub Pages

Both modes: pull from JSearch and/or Adzuna, keep full-time salaried roles only,
filter by salary/recency/remote, then score with Claude. If a FEEDBACK_URL is set,
past thumbs up/down are loaded and fed to Claude so rankings learn your taste.

Secrets (GitHub Actions):
  Required (both modes): ANTHROPIC_API_KEY  + at least one job source:
      RAPIDAPI_KEY                     (JSearch, recommended)
      ADZUNA_APP_ID + ADZUNA_APP_KEY   (free fallback)
  email mode also needs: GMAIL_ADDRESS, GMAIL_APP_PASSWORD
  Recommended: FEEDBACK_URL            (Apps Script web-app URL; enables learning + buttons)
  Optional: CLAUDE_MODEL, SALARY_MIN (110000), MAX_DAYS_OLD (14), MAX_TO_SCORE (25)
"""

import os
import sys
import json
import html
import time
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import anthropic

# ---------------------------------------------------------------------------
# Profile & rules
# ---------------------------------------------------------------------------
TARGET_TITLES = [
    "senior data analyst",
    "senior business analyst",
    "business intelligence analyst",
]

PROFILE = """Ryan Assum, based in Bozeman MT, works fully remote. ~11 years finance + analytics.
Current: Business Analyst III, Parks & Resorts at Delaware North (remote). BS Finance.
Core stack: Databricks (SQL - CTEs, window functions; Python/PySpark), Tableau (Desktop,
Server/Online, Tableau Server Client), Alteryx, KNIME, Power BI, Looker, NetSuite, Excel/VBA.
Strengths: gold-standard data models, high-adoption dashboards, executive revenue/plan/labor
reporting for President & COO, reporting automation, early AI work (Databricks Genie semantic
layer + agents).
HONEST GAPS (do NOT count as strengths; downweight roles that hard-require them): Snowflake,
BigQuery, dbt, Airflow, and heavy A/B testing / experimentation / statistics. Databricks is his
only cloud warehouse."""

SALARY_MIN = int(os.environ.get("SALARY_MIN", "110000"))
MAX_DAYS_OLD = int(os.environ.get("MAX_DAYS_OLD", "14"))
MAX_TO_SCORE = int(os.environ.get("MAX_TO_SCORE", "25"))
MIN_SCORE = 3.5
EMAIL_MAX = 7        # weekly best-of
DASH_MAX = 20        # daily dashboard (all clearing the bar, capped)
MIN_EMPLOYEES = 200
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
FEEDBACK_URL = os.environ.get("FEEDBACK_URL", "").strip()

ADZUNA_COUNTRY = "us"
ADZUNA_PAGES = 2
JSEARCH_PAGES = 1
REMOTE_HINTS = ("remote", "work from home", "wfh", "anywhere", "distributed")
MONTANA_HINTS = ("montana", ", mt", " mt ", "bozeman", "missoula", "billings", "helena")
SALARIED_TEMP_BLOCK = ("CONTRACTOR", "CONTRACT", "PART_TIME", "PARTTIME",
                       "INTERN", "TEMPORARY", "TEMP")


def log(msg):
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Feedback (preference learning)
# ---------------------------------------------------------------------------
def load_feedback():
    """GET the Apps Script endpoint -> list of {vote,title,company,url}."""
    if not FEEDBACK_URL:
        return {"liked": [], "disliked": []}
    try:
        r = requests.get(FEEDBACK_URL, timeout=20)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        log(f"Feedback load failed (continuing without it): {e}")
        return {"liked": [], "disliked": []}
    liked, disliked = [], []
    for row in rows:
        vote = str(row.get("vote", "")).lower()
        label = f"{row.get('title','')} @ {row.get('company','')}".strip(" @")
        if not label:
            continue
        if vote in ("up", "like", "yes", "1", "👍"):
            liked.append(label)
        elif vote in ("down", "dislike", "no", "0", "👎"):
            disliked.append(label)
    # keep it small + recent
    fb = {"liked": liked[-30:], "disliked": disliked[-30:]}
    log(f"Loaded feedback: {len(fb['liked'])} liked, {len(fb['disliked'])} disliked")
    return fb


def feedback_context(fb):
    if not fb["liked"] and not fb["disliked"]:
        return ""
    parts = ["\nThe candidate has given feedback on past postings. Use it to adjust scores:"]
    if fb["liked"]:
        parts.append("LIKED (score similar roles higher): " + "; ".join(fb["liked"]))
    if fb["disliked"]:
        parts.append("DISLIKED (score similar roles lower): " + "; ".join(fb["disliked"]))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def _annualize(amount, period):
    if not amount:
        return None
    factor = {"HOUR": 2080, "DAY": 260, "WEEK": 52, "MONTH": 12, "YEAR": 1}.get(
        (period or "").upper(), 1)
    try:
        return int(float(amount) * factor)
    except Exception:
        return None


def _jsearch_request(url, headers, params, provider, title):
    """One GET with retry on 5xx/429. Returns parsed jobs list, or None on failure."""
    for attempt in (1, 2, 3):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 200:
                payload = r.json().get("data", [])
                # search-v2 returns {"jobs": [...], "cursor": ...}; older shape is a bare list
                return payload.get("jobs", []) if isinstance(payload, dict) else payload
            body = (r.text or "").strip().replace("\n", " ")[:300]
            log(f"JSearch ({provider}) '{title}' HTTP {r.status_code}: {body}")
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(3 * attempt)
                continue
            return None
        except Exception as e:
            log(f"JSearch ({provider}) fetch error for '{title}': {e}")
            return None
    return None


def _normalize_jsearch(j):
    loc_bits = [j.get("job_city"), j.get("job_state"), j.get("job_country")]
    remote = bool(j.get("job_is_remote")) or (j.get("work_arrangement") == "remote")
    loc = ", ".join([b for b in loc_bits if b]) or ("Remote" if remote else "")
    return {
        "id": f"js:{j.get('job_id')}",
        "title": j.get("job_title", ""),
        "company": {"display_name": j.get("employer_name") or "Unknown"},
        "location": {"display_name": loc},
        "salary_min": _annualize(j.get("job_min_salary"), j.get("job_salary_period")),
        "salary_max": _annualize(j.get("job_max_salary"), j.get("job_salary_period")),
        "created": j.get("job_posted_at_datetime_utc", ""),
        "redirect_url": j.get("job_apply_link", ""),
        "description": j.get("job_description", "") or "",
        "job_is_remote": remote,
        "employment_type": (j.get("job_employment_type") or "").upper(),
        "pay_period": (j.get("job_salary_period") or "").upper(),
        "source": "JSearch",
    }


def _jsearch_via_openwebninja(key):
    url = "https://api.openwebninja.com/jsearch/search-v2"
    headers = {"x-api-key": key}
    out = []
    for title in TARGET_TITLES:
        params = {"query": title, "country": "us", "date_posted": "month",
                  "employment_types": "FULLTIME", "work_from_home": "true",
                  "num_pages": str(JSEARCH_PAGES)}
        jobs = _jsearch_request(url, headers, params, "OpenWebNinja", title)
        if jobs is None:
            continue
        out += [_normalize_jsearch(j) for j in jobs]
        log(f"JSearch (OpenWebNinja) '{title}': {len(jobs)} results")
    return out


def _jsearch_via_rapidapi(key):
    # Same OpenWeb Ninja engine behind RapidAPI now uses /search-v2 (the old /search 404s).
    url = "https://jsearch.p.rapidapi.com/search-v2"
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    out = []
    for title in TARGET_TITLES:
        params = {"query": title, "country": "us", "date_posted": "month",
                  "employment_types": "FULLTIME", "work_from_home": "true",
                  "num_pages": str(JSEARCH_PAGES)}
        jobs = _jsearch_request(url, headers, params, "RapidAPI", title)
        if jobs is None:
            continue
        out += [_normalize_jsearch(j) for j in jobs]
        log(f"JSearch (RapidAPI) '{title}': {len(jobs)} results")
    return out


def fetch_jsearch():
    """Try OpenWeb Ninja first; if it returns nothing (e.g. a 503 outage) and a
    RapidAPI key is set, automatically fail over to RapidAPI JSearch."""
    own = os.environ.get("OPENWEBNINJA_API_KEY", "").strip()
    rapid = os.environ.get("RAPIDAPI_KEY", "").strip()
    results = []
    if own:
        results = _jsearch_via_openwebninja(own)
    if not results and rapid:
        if own:
            log("JSearch: OpenWeb Ninja returned nothing — failing over to RapidAPI.")
        results = _jsearch_via_rapidapi(rapid)
    return results


def fetch_adzuna(app_id, app_key):
    seen = {}
    # Adzuna has no "remote" flag, so ask for it explicitly: one pass requiring the
    # word "remote" alongside the title, one pass scoped to Montana. This surfaces roles
    # that will actually survive the remote/MT filter instead of onsite ones we'd discard.
    passes = [{"label": "remote", "what": "remote"},
              {"label": "MT", "where": "Montana"}]
    for title in TARGET_TITLES:
        for p in passes:
            for page in range(1, ADZUNA_PAGES + 1):
                url = f"https://api.adzuna.com/v1/api/jobs/{ADZUNA_COUNTRY}/search/{page}"
                params = {
                    "app_id": app_id, "app_key": app_key, "what_phrase": title,
                    "results_per_page": 50, "max_days_old": MAX_DAYS_OLD,
                    "salary_min": SALARY_MIN, "full_time": 1, "permanent": 1,
                    "content-type": "application/json",
                }
                if "what" in p:
                    params["what"] = p["what"]
                if "where" in p:
                    params["where"] = p["where"]
                try:
                    r = requests.get(url, params=params, timeout=30)
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    log(f"Adzuna fetch failed for '{title}' [{p['label']}] p{page}: {e}")
                    continue
                for job in data.get("results", []):
                    jid = f"az:{job.get('id')}"
                    if jid not in seen:
                        job["id"] = jid
                        job["source"] = "Adzuna"
                        job["job_is_remote"] = None
                        job["employment_type"] = ("FULLTIME" if job.get("contract_time") == "full_time"
                                                  else (job.get("contract_time") or "").upper())
                        job["pay_period"] = "YEAR"
                        seen[jid] = job
                log(f"Adzuna '{title}' [{p['label']}] p{page}: {len(data.get('results', []))} results")
    return list(seen.values())


def merge_dedupe(*lists):
    merged, seen = [], set()
    for lst in lists:
        for job in lst:
            key = (job.get("title", "").strip().lower(),
                   (job.get("company") or {}).get("display_name", "").strip().lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append(job)
    log(f"Merged to {len(merged)} unique postings across sources")
    return merged


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------
def _age_days(created):
    if not created:
        return None
    try:
        d = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - d).days
    except Exception:
        return None


def looks_remote_or_mt(job):
    if job.get("job_is_remote") is True:
        return True
    blob = " ".join([
        job.get("title", ""), job.get("description", ""),
        (job.get("location") or {}).get("display_name", ""),
    ]).lower()
    return any(h in blob for h in REMOTE_HINTS) or any(h in blob for h in MONTANA_HINTS)


def is_salaried_fulltime(job):
    if job.get("pay_period") == "HOUR":
        return False
    et = job.get("employment_type") or ""
    if any(b in et for b in SALARIED_TEMP_BLOCK):
        return False
    return True


def prefilter(jobs):
    kept = []
    for job in jobs:
        age = _age_days(job.get("created"))
        if age is not None and age > MAX_DAYS_OLD:
            continue
        if not is_salaried_fulltime(job):
            continue
        smin = job.get("salary_min")
        if ((smin is None) or (smin >= SALARY_MIN)) and looks_remote_or_mt(job):
            kept.append(job)
    log(f"Pre-filter kept {len(kept)} of {len(jobs)}")
    kept.sort(key=lambda j: j.get("created", ""), reverse=True)
    return kept[:MAX_TO_SCORE]


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------
def score_with_claude(jobs, api_key, fb, cap):
    if not jobs:
        return []
    client = anthropic.Anthropic(api_key=api_key)
    compact = [{
        "i": i, "title": j.get("title", ""),
        "company": (j.get("company") or {}).get("display_name", "Unknown"),
        "location": (j.get("location") or {}).get("display_name", ""),
        "salary_min": j.get("salary_min"), "salary_max": j.get("salary_max"),
        "created": j.get("created", ""), "description": (j.get("description") or "")[:1200],
    } for i, j in enumerate(jobs)]

    prompt = f"""You are screening job postings for a candidate. Candidate profile:

{PROFILE}
{feedback_context(fb)}

Scoring rules:
- Base salary floor is ${SALARY_MIN:,}. If salary is null, don't penalize but set salary_disclosed=false.
- Prefer companies with >= {MIN_EMPLOYEES} employees (NOT startups). Estimate size; if unsure, lower confidence.
- Reward remote-friendly, recent company growth, and companies that embrace AI.
- Score fit 1.0-5.0 mainly on match to the candidate's REAL experience (Databricks/Tableau/SQL/
  Python/exec reporting/finance-adjacent). Downweight roles that hard-require Snowflake, BigQuery,
  dbt, Airflow, or heavy experimentation/statistics.
- If feedback is provided above, weight toward LIKED patterns and away from DISLIKED ones.

Postings as JSON:
{json.dumps(compact)}

Return ONLY a JSON array (no prose). One object per posting with keys:
  "i", "score" (1.0-5.0), "size_estimate", "likely_startup" (bool), "ai_posture",
  "growth_signal", "why" (one sentence), "caveat" (short or ""), "salary_disclosed" (bool)
"""
    msg = client.messages.create(model=CLAUDE_MODEL, max_tokens=4000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        scores = json.loads(text)
    except Exception as e:
        log(f"Could not parse Claude JSON: {e}\nRaw: {text[:400]}")
        return []

    results = []
    for s in scores:
        idx = s.get("i")
        if idx is None or idx < 0 or idx >= len(jobs):
            continue
        job = jobs[idx]
        if s.get("likely_startup") and "likely <200" in str(s.get("size_estimate", "")).lower():
            continue
        if float(s.get("score", 0)) < MIN_SCORE:
            continue
        results.append({
            "title": job.get("title", ""),
            "company": (job.get("company") or {}).get("display_name", "Unknown"),
            "location": (job.get("location") or {}).get("display_name", ""),
            "salary_min": job.get("salary_min"), "salary_max": job.get("salary_max"),
            "created": (job.get("created") or "")[:10], "url": job.get("redirect_url", ""),
            "source": job.get("source", ""), "score": round(float(s.get("score", 0)), 1),
            "size_estimate": s.get("size_estimate", "unknown"), "ai_posture": s.get("ai_posture", ""),
            "growth_signal": s.get("growth_signal", ""), "why": s.get("why", ""),
            "caveat": s.get("caveat", ""), "salary_disclosed": s.get("salary_disclosed", True),
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:cap]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def money(v):
    return f"${int(v):,}" if isinstance(v, (int, float)) and v else None


def salary_text(r):
    lo, hi = money(r["salary_min"]), money(r["salary_max"])
    s = f"{lo}–{hi}" if lo and hi else (f"{lo}+" if lo else None)
    return s if (r.get("salary_disclosed", True) and s) else "salary not disclosed"


SCREEN_NOTE = (f"Full-time salaried only (no hourly/contract/temp) • salary ≥ ${SALARY_MIN:,} "
               f"(or undisclosed, flagged) • ~{MIN_EMPLOYEES}+ employees (estimate) • remote or "
               f"Montana • posted within {MAX_DAYS_OLD} days • fit ≥ {MIN_SCORE}/5.0. "
               "Size / AI / growth are best-effort estimates — verify before applying.")


# ---------- email ----------
def build_email_html(results):
    today = dt.date.today().strftime("%B %d, %Y")
    if not results:
        return (f"<h2>Weekly job matches — {today}</h2><p>No postings cleared the bar this week. "
                "That's normal some weeks — nothing worth your time beats filler.</p>")
    rows = [f"<h2>Weekly job matches — {today}</h2>",
            f"<p><b>{len(results)} role(s)</b> cleared the bar this week, best first.</p>"]
    for r in results:
        caveat = (f'<div style="color:#b45309;font-size:13px;margin-top:4px">⚠ '
                  f'{html.escape(r["caveat"])}</div>') if r["caveat"] else ""
        src = f' &nbsp;•&nbsp; via {html.escape(r["source"])}' if r.get("source") else ""
        rows.append(f"""
        <div style="border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;margin:12px 0">
          <div style="font-size:16px;font-weight:700;color:#1f3864">
            {html.escape(r['title'])} — {html.escape(r['company'])}
            <span style="float:right;color:#059669">{r['score']}/5.0</span></div>
          <div style="color:#555;font-size:13px;margin:4px 0">{html.escape(r['location'])}
            &nbsp;•&nbsp; {html.escape(salary_text(r))} &nbsp;•&nbsp; posted {html.escape(r['created'])}{src}</div>
          <div style="font-size:14px;margin-top:6px">{html.escape(r['why'])}</div>
          <div style="color:#666;font-size:12px;margin-top:4px">Size: {html.escape(str(r['size_estimate']))}
            &nbsp;|&nbsp; AI: {html.escape(str(r['ai_posture']))} &nbsp;|&nbsp;
            Growth: {html.escape(str(r['growth_signal']))}</div>{caveat}
          <div style="margin-top:8px"><a href="{html.escape(r['url'])}"
            style="color:#1f3864;font-weight:600">View posting →</a></div></div>""")
    rows.append(f'<p style="color:#777;font-size:12px;margin-top:18px">Screened on: {SCREEN_NOTE}</p>')
    return "\n".join(rows)


def send_email(html_body, n):
    addr = os.environ["GMAIL_ADDRESS"]
    pw = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Weekly job matches — {dt.date.today():%b %d} ({n} role{'s' if n != 1 else ''})"
    msg["From"] = addr
    msg["To"] = addr
    msg.attach(MIMEText("HTML email — open in an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(addr, pw)
        server.sendmail(addr, [addr], msg.as_string())
    log(f"Email sent to {addr}")


# ---------- dashboard ----------
def build_dashboard(results):
    os.makedirs("docs", exist_ok=True)
    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open("docs/jobs.json", "w") as f:
        json.dump({"updated": updated, "jobs": results}, f, indent=2)

    data_js = json.dumps(results)
    fb_url_js = json.dumps(FEEDBACK_URL)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ryan's Daily Job Matches</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:820px;margin:0 auto;
       padding:24px 16px;color:#111;background:#fafafa}}
  h1{{color:#1f3864;margin-bottom:2px}} .sub{{color:#666;font-size:13px;margin-bottom:18px}}
  .card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 18px;margin:14px 0;
        box-shadow:0 1px 2px rgba(0,0,0,.04)}}
  .title{{font-size:17px;font-weight:700;color:#1f3864}} .score{{float:right;color:#059669}}
  .meta{{color:#555;font-size:13px;margin:5px 0}} .why{{font-size:14px;margin-top:6px}}
  .sig{{color:#666;font-size:12px;margin-top:5px}} .caveat{{color:#b45309;font-size:13px;margin-top:5px}}
  a.apply{{color:#1f3864;font-weight:600;text-decoration:none}}
  .vote{{margin-top:10px}} .vote button{{font-size:18px;border:1px solid #ddd;background:#fff;border-radius:8px;
        padding:4px 12px;margin-right:8px;cursor:pointer}} .voted{{opacity:.5}}
  .foot{{color:#888;font-size:12px;margin-top:22px;border-top:1px solid #eee;padding-top:12px}}
</style></head><body>
<h1>Daily job matches</h1>
<div class="sub">Updated {updated} • best first • 👍/👎 teaches the ranking what you like</div>
<div id="list"></div>
<div class="foot">Screened on: {html.escape(SCREEN_NOTE)}</div>
<script>
const JOBS = {data_js};
const FEEDBACK_URL = {fb_url_js};
const voted = JSON.parse(localStorage.getItem('voted')||'{{}}');
function money(v){{return (typeof v==='number'&&v)?'$'+v.toLocaleString():null;}}
function salary(r){{let lo=money(r.salary_min),hi=money(r.salary_max);
  let s=lo&&hi?lo+'–'+hi:(lo?lo+'+':null); return (r.salary_disclosed!==false&&s)?s:'salary not disclosed';}}
function vote(id,r,v){{
  voted[id]=v; localStorage.setItem('voted',JSON.stringify(voted));
  render();
  if(FEEDBACK_URL){{ try{{ fetch(FEEDBACK_URL,{{method:'POST',mode:'no-cors',
    headers:{{'Content-Type':'text/plain;charset=utf-8'}},
    body:JSON.stringify({{vote:v,title:r.title,company:r.company,url:r.url}})}}); }}catch(e){{}} }}
}}
function esc(s){{return (s||'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
function render(){{
  if(!JOBS.length){{document.getElementById('list').innerHTML=
    '<div class="card">No roles cleared the bar today. Check back tomorrow.</div>';return;}}
  document.getElementById('list').innerHTML = JOBS.map((r,i)=>{{
    const id=(r.url||r.title+ '|' +r.company);
    const v=voted[id];
    const caveat=r.caveat?'<div class="caveat">⚠ '+esc(r.caveat)+'</div>':'';
    const src=r.source?' • via '+esc(r.source):'';
    return `<div class="card">
      <div class="title">${{esc(r.title)}} — ${{esc(r.company)}}<span class="score">${{r.score}}/5.0</span></div>
      <div class="meta">${{esc(r.location)}} • ${{esc(salary(r))}} • posted ${{esc(r.created)}}${{src}}</div>
      <div class="why">${{esc(r.why)}}</div>
      <div class="sig">Size: ${{esc(String(r.size_estimate))}} | AI: ${{esc(String(r.ai_posture))}} | Growth: ${{esc(String(r.growth_signal))}}</div>
      ${{caveat}}
      <div class="vote">
        <button class="${{v==='up'?'voted':''}}" onclick='vote(${{JSON.stringify(id)}}, ${{JSON.stringify(r)}}, "up")'>👍</button>
        <button class="${{v==='down'?'voted':''}}" onclick='vote(${{JSON.stringify(id)}}, ${{JSON.stringify(r)}}, "down")'>👎</button>
        <a class="apply" href="${{esc(r.url)}}" target="_blank" rel="noopener">View posting →</a>
      </div></div>`;
  }}).join('');
}}
render();
</script></body></html>"""
    with open("docs/index.html", "w") as f:
        f.write(page)
    log(f"Dashboard written to docs/ ({len(results)} roles)")


# ---------------------------------------------------------------------------
def gather_and_score(cap):
    have_js = bool(os.environ.get("OPENWEBNINJA_API_KEY", "").strip()
                   or os.environ.get("RAPIDAPI_KEY", "").strip())
    have_az = bool(os.environ.get("ADZUNA_APP_ID", "").strip()
                   and os.environ.get("ADZUNA_APP_KEY", "").strip())
    log(f"Sources configured: JSearch={'yes' if have_js else 'no'}, "
        f"Adzuna={'yes' if have_az else 'no'}")
    if not (have_js or have_az):
        log("ERROR: no job source. Set OPENWEBNINJA_API_KEY (or RAPIDAPI_KEY) and/or ADZUNA_APP_ID + ADZUNA_APP_KEY.")
        sys.exit(1)
    js = fetch_jsearch() if have_js else []
    az = fetch_adzuna(os.environ["ADZUNA_APP_ID"], os.environ["ADZUNA_APP_KEY"]) if have_az else []
    jobs = merge_dedupe(js, az)
    candidates = prefilter(jobs)
    fb = load_feedback()
    return score_with_claude(candidates, os.environ["ANTHROPIC_API_KEY"], fb, cap)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "email"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("ERROR: missing ANTHROPIC_API_KEY")
        sys.exit(1)

    if mode == "dashboard":
        results = gather_and_score(DASH_MAX)
        build_dashboard(results)
    elif mode == "email":
        for n in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
            if not os.environ.get(n):
                log(f"ERROR: missing {n} for email mode")
                sys.exit(1)
        results = gather_and_score(EMAIL_MAX)
        send_email(build_email_html(results), len(results))
    else:
        log(f"Unknown mode '{mode}'. Use 'email' or 'dashboard'.")
        sys.exit(1)
    log("Done.")


if __name__ == "__main__":
    main()

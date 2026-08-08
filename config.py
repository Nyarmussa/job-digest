"""
User-tunable settings for the job-match engine.

Edit this file to change what the digest searches for and how it filters —
you shouldn't need to touch main.py for day-to-day tweaks. A few values can also
be overridden with GitHub Actions secrets (noted below) without editing anything.
"""

import os

# --- What to search for ------------------------------------------------------
# Broad titles cast a wider net; the salary floor and Claude's scoring do the
# real filtering. Add/remove titles here. Each title costs JSEARCH_PAGES credits
# per run, so watch your monthly API quota (free tier = 200/month).
TARGET_TITLES = [
    "data analyst",
    "business analyst",
    "business intelligence analyst",
]

# The candidate profile Claude scores every posting against. Keep the "honest
# gaps" section — it stops the model from over-crediting skills you don't have.
PROFILE = """Ryan Assum, based in Bozeman MT, works fully remote. ~11 years finance + analytics.
Current: Business Analyst III, Parks & Resorts at Delaware North (remote). BS Finance.
Core stack: Databricks (SQL - CTEs, window functions; Python/PySpark), Tableau (Desktop,
Server/Online, Tableau Server Client), Alteryx, KNIME, Power BI, Looker, NetSuite, Excel/VBA.
Strengths: gold-standard data models, high-adoption dashboards, executive revenue/plan/labor
reporting for President & COO, reporting automation, early AI work (Databricks Genie semantic
layer + agents).
HONEST GAPS (do NOT count as strengths): Snowflake,
BigQuery, dbt, Airflow, and heavy A/B testing / experimentation / statistics. Databricks is his
only cloud warehouse."""

# --- Filters (SALARY_MIN / MAX_DAYS_OLD can be overridden via secrets) --------
SALARY_MIN = int(os.environ.get("SALARY_MIN", "110000"))   # base salary floor (USD)
MAX_DAYS_OLD = int(os.environ.get("MAX_DAYS_OLD", "21"))   # only postings this fresh
MIN_EMPLOYEES = 150                                         # "no startups" threshold (est.)
MIN_SCORE = 3                                             # only surface roles >= this fit score (1-5)

# --- Volume / cost knobs -----------------------------------------------------
JSEARCH_PAGES = 10        # results per title = pages x 10 (each page = 1 API credit)
MAX_TO_SCORE = int(os.environ.get("MAX_TO_SCORE", "40"))   # max candidates sent to Claude per run
EMAIL_MAX = 7            # roles in the weekly best-of email
DASH_MAX = 40            # roles shown on the dashboard
ADZUNA_PAGES = 2         # Adzuna pages per title per pass
ADZUNA_COUNTRY = "us"

# --- Model -------------------------------------------------------------------
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# --- Matching hints (used to detect remote / Montana / non-salaried) ---------
REMOTE_HINTS = ("remote", "work from home", "wfh", "anywhere", "distributed")
MONTANA_HINTS = ("montana", ", mt", " mt ", "bozeman", "missoula", "billings", "helena")
SALARIED_TEMP_BLOCK = ("CONTRACTOR", "CONTRACT", "PART_TIME", "PARTTIME",
                       "INTERN", "TEMPORARY", "TEMP")

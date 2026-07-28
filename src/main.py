from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from jobspy import scrape_jobs


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "preferences.toml"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "jobs.csv"
EXPERIMENTAL_SITES = ["google"]
SUPPORTED_SITES = ["indeed", "linkedin", *EXPERIMENTAL_SITES]
TRACKING_COLUMNS = [
    "job_title",
    "company",
    "location",
    "salary",
    "job_type",
    "job_link",
    "application_status",
    "resume_status",
]
DEFAULT_CONFIG: dict[str, Any] = {
    "profile": {
        "field": "",
        "student_level": "",
        "location": "",
        "master_resume_path": "resumes/source/Master_Resume.md",
        "current_resume_path": "",
    },
    "resume_generation": {
        "template_path": "templates/resume.tex",
        "output_dir": "resumes/generated",
        "job_details_path": "data/job_details.json",
        "model": "gpt-4.1-mini",
    },
    "resume": {
        "header": {
            "name": "Candidate Name",
            "location": "",
            "email": "",
            "github": "",
            "linkedin": "",
            "portfolio": "",
        },
        "education": [],
        "skills": {},
        "latex": {
            "font_name": "Calibri",
            "font_path": "",
            "upright_font": "Calibri.ttf",
            "bold_font": "Calibrib.ttf",
            "italic_font": "Calibrii.ttf",
            "bold_italic_font": "Calibriz.ttf",
        },
    },
    "search": {
        "sites": ["indeed", "linkedin"],
        "search_terms": ["target role"],
        "job_types": ["fulltime"],
        "hours_old": 24,
        "results_wanted": 25,
        "remote": True,
        "include_google": False,
        "top_n_jobs": 15,
    },
    "fit": {
        "minimum_score": 30,
        "target_title_keywords": [],
        "preferred_keywords": [],
        "excluded_keywords": [],
        "seniority_keywords": [],
        "student_friendly_keywords": [],
    },
}

TEMPLATE_PREAMBLE = r"""
\documentclass[letterpaper,11pt]{article}

\usepackage{fontspec}
\setmainfont[
  Path = {__FONT_PATH__},
  UprightFont = __UPRIGHT_FONT__,
  BoldFont = __BOLD_FONT__,
  ItalicFont = __ITALIC_FONT__,
  BoldItalicFont = __BOLD_ITALIC_FONT__
]{__FONT_NAME__}
\usepackage{latexsym}
\usepackage[empty]{fullpage}
\usepackage{titlesec}
\usepackage{marvosym}
\usepackage[usenames,dvipsnames]{color}
\usepackage{verbatim}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{fancyhdr}
\usepackage[english]{babel}
\usepackage{tabularx}

\pagestyle{fancy}
\fancyhf{}
\fancyfoot{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}

\addtolength{\oddsidemargin}{-0.5in}
\addtolength{\evensidemargin}{-0.5in}
\addtolength{\textwidth}{1in}
\addtolength{\topmargin}{-.5in}
\addtolength{\textheight}{1.0in}

\urlstyle{same}

\raggedbottom
\raggedright
\setlength{\tabcolsep}{0in}

\titleformat{\section}{
  \vspace{-4pt}\scshape\raggedright\large
}{}{0em}{}[\color{black}\titlerule \vspace{-5pt}]

\newcommand{\resumeItem}[1]{
  \item\small{
    {#1 \vspace{-2pt}}
  }
}

\newcommand{\resumeSubheading}[4]{
  \vspace{-2pt}\item
    \begin{tabular*}{0.97\textwidth}[t]{l@{\extracolsep{\fill}}r}
      \textbf{#1} & #2 \\
      \textit{\small#3} & \textit{\small #4} \\
    \end{tabular*}\vspace{-7pt}
}

\newcommand{\resumeProjectHeading}[2]{
    \item
    \begin{tabular*}{0.97\textwidth}{l@{\extracolsep{\fill}}r}
      \small#1 & #2 \\
    \end{tabular*}\vspace{-7pt}
}

\newcommand{\resumeSubItem}[1]{\resumeItem{#1}\vspace{-4pt}}

\renewcommand\labelitemii{$\vcenter{\hbox{\tiny$\bullet$}}$}

\newcommand{\resumeSubHeadingListStart}{\begin{itemize}[leftmargin=0.15in, label={}]}
\newcommand{\resumeSubHeadingListEnd}{\end{itemize}}
\newcommand{\resumeItemListStart}{\begin{itemize}}
\newcommand{\resumeItemListEnd}{\end{itemize}\vspace{-5pt}}
""".strip("\n")

_LATEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_LATEX_ESCAPE_RE = re.compile("|".join(re.escape(k) for k in _LATEX_SPECIAL))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull, score, and track job listings for JobPilot."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Preferences TOML path. Defaults to {DEFAULT_CONFIG_PATH}.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"CSV path to update. Defaults to {DEFAULT_OUTPUT_PATH}.",
    )
    parser.add_argument("--location", help="Override the configured search location.")
    parser.add_argument(
        "--site",
        action="append",
        dest="sites",
        choices=SUPPORTED_SITES,
        help="Job site to query. Repeat for multiple sites.",
    )
    parser.add_argument(
        "--include-google",
        action="store_true",
        help="Also query Google Jobs. Experimental: current parsing may return zero results.",
    )
    parser.add_argument(
        "--search-term",
        action="append",
        dest="search_terms",
        help="Search term to query. Repeat for multiple terms.",
    )
    parser.add_argument(
        "--job-type",
        action="append",
        dest="job_types",
        help="JobSpy job type. Repeat for multiple types.",
    )
    parser.add_argument(
        "--results-wanted",
        type=int,
        help="Number of results to request per search term, job type, and site.",
    )
    parser.add_argument(
        "--hours-old",
        type=int,
        help="Only request jobs posted in the last N hours.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned searches without calling job boards.",
    )
    parser.add_argument(
        "--compact-existing",
        action="store_true",
        help="Rewrite the existing CSV into the compact tracker format without pulling jobs.",
    )
    parser.add_argument(
        "--check-resumes",
        action="store_true",
        help="Verify configured resume files and report basic text extraction readiness.",
    )
    parser.add_argument(
        "--draft-next-resume",
        action="store_true",
        help="Draft a tailored LaTeX resume for the next found job with no draft.",
    )
    parser.add_argument(
        "--check-next-job-context",
        action="store_true",
        help="Report whether the next resume candidate has cached job-description context.",
    )
    return parser.parse_args()


def load_preferences(path: Path) -> dict[str, Any]:
    preferences = deep_merge(DEFAULT_CONFIG, {})
    if path.exists():
        with path.open("rb") as config_file:
            preferences = deep_merge(preferences, tomllib.load(config_file))
    return preferences


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = {
        key: deep_merge(value, {}) if isinstance(value, dict) else value
        for key, value in base.items()
    }
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_search_settings(
    args: argparse.Namespace, preferences: dict[str, Any]
) -> dict[str, Any]:
    search = preferences["search"]
    profile = preferences["profile"]
    sites = list(args.sites or search["sites"])
    include_google = args.include_google or search.get("include_google", False)
    if include_google and "google" not in sites:
        sites.append("google")
    sites = [site for site in sites if site in SUPPORTED_SITES]

    return {
        "sites": sites,
        "search_terms": args.search_terms or search["search_terms"],
        "job_types": args.job_types or search["job_types"],
        "location": args.location or profile["location"],
        "results_wanted": args.results_wanted or search["results_wanted"],
        "hours_old": args.hours_old or search["hours_old"],
        "remote": bool(search.get("remote", True)),
        "top_n_jobs": int(search.get("top_n_jobs", 15)),
    }


# Query each configured source/term/type combination and collect raw JobSpy rows.
def pull_jobs(
    settings: dict[str, Any], dry_run: bool
) -> tuple[pd.DataFrame, dict[str, int]]:
    frames: list[pd.DataFrame] = []
    source_counts = {site: 0 for site in settings["sites"]}

    for site in settings["sites"]:
        for search_term in settings["search_terms"]:
            for job_type in settings["job_types"]:
                print(
                    "Searching "
                    f"site={site!r}, term={search_term!r}, type={job_type!r}, "
                    f"location={settings['location']!r}, remote={settings['remote']}"
                )
                if dry_run:
                    continue

                try:
                    jobs = scrape_jobs(
                        **build_scraper_kwargs(settings, site, search_term, job_type)
                    )
                except Exception as exc:
                    print(f"Search failed for site={site!r}: {exc}")
                    continue

                source_counts[site] += len(jobs)
                if not jobs.empty:
                    jobs["search_term"] = search_term
                    jobs["requested_job_type"] = job_type
                    frames.append(jobs)

    if not frames:
        return pd.DataFrame(), source_counts

    frames = [frame.dropna(axis=1, how="all") for frame in frames if not frame.empty]
    return pd.concat(frames, ignore_index=True), source_counts


def build_scraper_kwargs(
    settings: dict[str, Any], site: str, search_term: str, job_type: str
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "site_name": [site],
        "search_term": search_term,
        "location": settings["location"],
        "is_remote": settings["remote"],
        "job_type": job_type,
        "results_wanted": settings["results_wanted"],
        "hours_old": settings["hours_old"],
        "country_indeed": "USA",
        "description_format": "markdown",
    }
    if site != "google":
        kwargs["google_search_term"] = (
            f"{search_term} remote jobs near {settings['location']} "
            f"posted in the last {settings['hours_old']} hours"
        )
    return kwargs


# Convert raw scraper rows into the compact tracker shape used by jobs.csv.
def normalize_jobs(
    raw_jobs: pd.DataFrame, preferences: dict[str, Any], settings: dict[str, Any]
) -> pd.DataFrame:
    if raw_jobs.empty:
        return empty_jobs_frame()

    raw_jobs = filter_jobs_with_descriptions(raw_jobs)
    raw_jobs = filter_recent_jobs(raw_jobs, settings["hours_old"])
    if raw_jobs.empty:
        return empty_jobs_frame()

    normalized = pd.DataFrame()
    normalized["job_title"] = get_series(raw_jobs, "title")
    normalized["company"] = get_series(raw_jobs, "company")
    normalized["location"] = get_series(raw_jobs, "location")
    normalized["salary"] = raw_jobs.apply(format_salary, axis=1)
    normalized["job_type"] = get_series(raw_jobs, "job_type").where(
        get_series(raw_jobs, "job_type").astype(str) != "",
        get_series(raw_jobs, "requested_job_type"),
    )
    normalized["job_link"] = get_series(raw_jobs, "job_url")

    scores = raw_jobs.apply(lambda row: score_job(row, preferences, settings), axis=1)
    normalized["fit_score"] = [score["score"] for score in scores]
    normalized["application_status"] = [
        "skipped" if score["skip_reason"] else "found" for score in scores
    ]
    normalized["resume_status"] = "not_started"
    ranked = filter_actionable_jobs(dedupe_jobs(normalized))
    ranked = ranked.sort_values("fit_score", ascending=False).head(settings["top_n_jobs"])
    return order_columns(ranked)


def filter_jobs_with_descriptions(raw_jobs: pd.DataFrame) -> pd.DataFrame:
    if raw_jobs.empty or "description" not in raw_jobs.columns:
        return pd.DataFrame()
    descriptions = raw_jobs["description"].fillna("").astype(str).str.strip()
    return raw_jobs[descriptions != ""].reset_index(drop=True)


def filter_recent_jobs(raw_jobs: pd.DataFrame, hours_old: int) -> pd.DataFrame:
    if raw_jobs.empty or "date_posted" not in raw_jobs.columns:
        return pd.DataFrame()

    posted_dates = pd.to_datetime(raw_jobs["date_posted"], errors="coerce").dt.date
    cutoff_date = (datetime.now() - timedelta(hours=hours_old)).date()
    recent = posted_dates.notna() & (posted_dates >= cutoff_date)
    return raw_jobs[recent].reset_index(drop=True)


# Store full descriptions and fit metadata only for selected tracker jobs.
def update_job_details_cache(
    raw_jobs: pd.DataFrame,
    selected_jobs: pd.DataFrame,
    preferences: dict[str, Any],
    settings: dict[str, Any],
) -> None:
    if raw_jobs.empty or selected_jobs.empty:
        return

    cache_path = resolve_project_path(preferences["resume_generation"]["job_details_path"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    selected_links = set(selected_jobs["job_link"].astype(str))
    details = read_job_details_cache(preferences)

    cacheable_jobs = filter_recent_jobs(
        filter_jobs_with_descriptions(raw_jobs), settings["hours_old"]
    )
    for _, row in cacheable_jobs.iterrows():
        job_link = str(row.get("job_url", "") or "")
        if not job_link or job_link not in selected_links:
            continue

        score = score_job(row, preferences, settings)
        details[job_link] = {
            "job_title": clean_text(row.get("title", "")),
            "company": clean_text(row.get("company", "")),
            "location": clean_text(row.get("location", "")),
            "salary": format_salary(row),
            "job_type": clean_text(row.get("job_type", "") or row.get("requested_job_type", "")),
            "source": clean_text(row.get("site", "")),
            "date_posted": clean_text(row.get("date_posted", "")),
            "description": clean_text(row.get("description", "")),
            "company_industry": clean_text(row.get("company_industry", "")),
            "company_description": clean_text(row.get("company_description", "")),
            "skills": clean_text(row.get("skills", "")),
            "fit_score": score["score"],
            "fit_reason": score["reason"],
        }

    cache_path.write_text(json.dumps(details, indent=2, sort_keys=True), encoding="utf-8")


def read_job_details_cache(preferences: dict[str, Any]) -> dict[str, Any]:
    cache_path = resolve_project_path(preferences["resume_generation"]["job_details_path"])
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def clean_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def format_salary(row: pd.Series) -> str:
    min_amount = first_available_value(row, ["min_amount", "raw_min_amount"])
    max_amount = first_available_value(row, ["max_amount", "raw_max_amount"])
    interval = first_available_value(row, ["interval", "raw_interval"])
    currency = first_available_value(row, ["currency", "raw_currency"])

    if pd.isna(min_amount) and pd.isna(max_amount):
        return ""

    currency_prefix = "$" if str(currency).upper() == "USD" else f"{currency} "
    amounts = []
    for amount in [min_amount, max_amount]:
        if pd.notna(amount) and str(amount) != "":
            amounts.append(f"{currency_prefix}{float(amount):,.0f}")

    if not amounts:
        return ""

    salary = " - ".join(amounts)
    if interval and pd.notna(interval):
        salary = f"{salary} {interval}"
    return salary


def first_available_value(row: pd.Series, columns: list[str]) -> Any:
    for column in columns:
        value = row.get(column, "")
        if pd.notna(value) and str(value) != "":
            return value
    return ""


# Score a job against configured title, keyword, flexibility, and seniority signals.
def score_job(
    row: pd.Series, preferences: dict[str, Any], settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    fit_config = preferences["fit"]
    target_title_keywords = fit_config["target_title_keywords"]
    preferred_keywords = fit_config["preferred_keywords"]
    seniority_keywords = fit_config.get("seniority_keywords", fit_config["excluded_keywords"])
    student_friendly_keywords = fit_config.get("student_friendly_keywords", [])
    minimum_score = int(fit_config["minimum_score"])
    title = str(row.get("title", "")).lower()
    job_type = str(row.get("job_type", "")).lower()
    requested_job_type = str(row.get("requested_job_type", "")).lower()
    location = str(row.get("location", "")).lower()
    description = str(row.get("description", "")).lower()
    haystack = " ".join(
        str(row.get(column, ""))
        for column in ["title", "company", "location", "job_type", "description"]
    ).lower()

    title_matches = [keyword for keyword in target_title_keywords if keyword_matches(keyword, title)]
    preferred_matches = [
        keyword for keyword in preferred_keywords if keyword_matches(keyword, haystack)
    ]
    student_matches = [
        keyword for keyword in student_friendly_keywords if keyword_matches(keyword, haystack)
    ]
    seniority_matches = [
        keyword for keyword in seniority_keywords if keyword_matches(keyword, title)
    ]
    required_years = extract_required_years(haystack)
    keyword_density = sum(count_keyword(keyword, description) for keyword in preferred_keywords)
    remote_match = any(keyword in haystack for keyword in ["remote", "work from home", "wfh"])
    location_match = settings is not None and location_matches_target(
        location, settings["location"]
    )
    actual_flexible_match = bool(student_matches) or any(
        keyword in job_type for keyword in ["intern", "parttime", "part time", "part-time"]
    )
    requested_flexible_match = any(
        keyword in requested_job_type for keyword in ["intern", "parttime", "part time", "part-time"]
    )

    score = 0
    score += min(40, len(title_matches) * 20)
    score += min(25, len(preferred_matches) * 4 + keyword_density)
    score += min(20, len(student_matches) * 7)
    if actual_flexible_match:
        score += 12
    elif requested_flexible_match:
        score += 3
    if remote_match:
        score += 8
    if location_match:
        score += 8
    score -= len(seniority_matches) * 35
    if required_years >= 3:
        score -= min(45, required_years * 8)
    score = max(0, min(100, score))

    skip_reasons = []
    if not clean_text(row.get("description", "")):
        skip_reasons.append("empty description")
    if not title_matches:
        skip_reasons.append("title does not match target role")
    if not actual_flexible_match:
        skip_reasons.append("not student/internship/part-time friendly")
    if seniority_matches:
        skip_reasons.append(f"seniority keywords: {', '.join(seniority_matches[:4])}")
    if required_years >= 3:
        skip_reasons.append(f"requires {required_years}+ years")
    if score < minimum_score:
        skip_reasons.append(f"fit score below {minimum_score}")

    reason_parts = []
    if title_matches:
        reason_parts.append(f"title: {', '.join(title_matches[:4])}")
    if preferred_matches:
        reason_parts.append(f"keywords: {', '.join(preferred_matches[:6])}")
    if student_matches:
        reason_parts.append(f"student-friendly: {', '.join(student_matches[:4])}")
    if remote_match:
        reason_parts.append("remote")
    if location_match:
        reason_parts.append("location")

    return {
        "score": max(score, 0),
        "reason": "; ".join(reason_parts) if reason_parts else "no preferred keywords matched",
        "skip_reason": "; ".join(skip_reasons),
    }


def extract_required_years(text: str) -> int:
    matches = re.findall(r"(\d+)\s*\+?\s*(?:years|yrs)", text)
    return max([int(match) for match in matches], default=0)


def keyword_matches(keyword: str, text: str) -> bool:
    keyword = keyword.lower()
    if re.search(r"\W", keyword):
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def count_keyword(keyword: str, text: str) -> int:
    keyword = keyword.lower()
    if re.search(r"\W", keyword):
        return text.count(keyword)
    return len(re.findall(rf"\b{re.escape(keyword)}\b", text))


def location_matches_target(location: str, target_location: str) -> bool:
    target = target_location.lower()
    target_tokens = [token.strip() for token in re.split(r"[,/]", target) if token.strip()]
    location_aliases = {
        "dc": ["washington", "dc", "district of columbia"],
        "washington dc": ["washington", "dc", "district of columbia"],
        "washington, dc": ["washington", "dc", "district of columbia"],
        "new york": ["new york", "ny", "nyc"],
        "new york, ny": ["new york", "ny", "nyc"],
        "nyc": ["new york", "ny", "nyc"],
        "san francisco": ["san francisco", "sf", "bay area"],
        "san francisco, ca": ["san francisco", "sf", "bay area"],
        "sf": ["san francisco", "sf", "bay area"],
        "united states": ["united states", "usa", "us"],
        "usa": ["united states", "usa", "us"],
    }
    aliases = set(target_tokens)
    aliases.update(location_aliases.get(target, []))
    aliases.update(["remote", "united states"])
    return any(alias and alias in location for alias in aliases)


def update_jobs_csv(
    output_path: Path, new_jobs: pd.DataFrame, preferences: dict[str, Any]
) -> tuple[int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_jobs = read_existing_jobs(output_path, preferences)
    before_count = len(existing_jobs)

    combined = pd.concat([existing_jobs, new_jobs], ignore_index=True, sort=False)
    combined = filter_actionable_jobs(order_columns(dedupe_jobs(combined)))
    combined.to_csv(
        output_path,
        quoting=csv.QUOTE_NONNUMERIC,
        escapechar="\\",
        index=False,
    )
    return len(combined) - before_count, len(combined)


def read_existing_jobs(output_path: Path, preferences: dict[str, Any]) -> pd.DataFrame:
    if not output_path.exists():
        return empty_jobs_frame()

    existing = pd.read_csv(output_path)
    if set(TRACKING_COLUMNS).issubset(existing.columns):
        return filter_actionable_jobs(order_columns(existing))

    return normalize_legacy_jobs(existing, preferences)


def normalize_legacy_jobs(existing: pd.DataFrame, preferences: dict[str, Any]) -> pd.DataFrame:
    if existing.empty:
        return empty_jobs_frame()

    normalized = pd.DataFrame()
    normalized["job_title"] = first_available_series(existing, ["job_title", "title"])
    normalized["company"] = get_series(existing, "company")
    normalized["location"] = get_series(existing, "location")
    normalized["salary"] = first_available_series(existing, ["salary"])
    if normalized["salary"].astype(str).eq("").all():
        normalized["salary"] = existing.apply(format_salary, axis=1)
    normalized["job_type"] = first_available_series(
        existing, ["job_type", "requested_job_type"]
    )
    normalized["job_link"] = first_available_series(existing, ["job_link", "job_url"])
    normalized["application_status"] = first_available_series(
        existing, ["application_status"]
    )
    if normalized["application_status"].astype(str).eq("").all():
        scores = existing.apply(lambda row: score_job(row, preferences), axis=1)
        normalized["application_status"] = [
            "skipped" if score["skip_reason"] else "found" for score in scores
        ]
    normalized["resume_status"] = first_available_series(existing, ["resume_status"])
    normalized["resume_status"] = normalized["resume_status"].where(
        normalized["resume_status"].astype(str) != "", "not_started"
    )
    return filter_actionable_jobs(order_columns(dedupe_jobs(normalized)))


def dedupe_jobs(jobs: pd.DataFrame) -> pd.DataFrame:
    if jobs.empty:
        return jobs

    deduped = jobs.copy()
    if "job_link" in deduped.columns:
        populated = deduped["job_link"].notna() & (deduped["job_link"].astype(str) != "")
        with_key = deduped[populated].drop_duplicates(subset=["job_link"], keep="first")
        without_key = deduped[~populated]
        deduped = pd.concat([with_key, without_key], ignore_index=True, sort=False)
    return deduped.reset_index(drop=True)


def get_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column in frame.columns:
        return frame[column].fillna("")
    return pd.Series([""] * len(frame), index=frame.index)


def first_available_series(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    for column in columns:
        if column in frame.columns:
            return frame[column].fillna("")
    return pd.Series([""] * len(frame), index=frame.index)


def empty_jobs_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=TRACKING_COLUMNS)


def order_columns(jobs: pd.DataFrame) -> pd.DataFrame:
    for column in TRACKING_COLUMNS:
        if column not in jobs.columns:
            jobs[column] = ""
    return jobs[TRACKING_COLUMNS]


def filter_actionable_jobs(jobs: pd.DataFrame) -> pd.DataFrame:
    if jobs.empty or "application_status" not in jobs.columns:
        return jobs
    return jobs[jobs["application_status"].astype(str) != "skipped"].reset_index(
        drop=True
    )


def print_source_summary(source_counts: dict[str, int]) -> None:
    if not source_counts:
        return
    print("Source results:")
    for source, count in source_counts.items():
        print(f"- {source}: {count}")


def print_quality_summary(
    raw_jobs: pd.DataFrame, selected_jobs: pd.DataFrame, settings: dict[str, Any]
) -> None:
    if raw_jobs.empty:
        print("No jobs found.")
        return

    with_descriptions = len(filter_jobs_with_descriptions(raw_jobs))
    recent_with_descriptions = len(
        filter_recent_jobs(filter_jobs_with_descriptions(raw_jobs), settings["hours_old"])
    )
    print(f"Raw jobs: {len(raw_jobs)}")
    print(f"Jobs with descriptions: {with_descriptions}")
    print(f"Jobs with descriptions posted within {settings['hours_old']} hours: {recent_with_descriptions}")
    print(f"Selected jobs saved: {len(selected_jobs)}")


def check_resumes(preferences: dict[str, Any]) -> None:
    profile = preferences["profile"]
    resume_paths = {
        "master_resume_path": profile.get("master_resume_path", ""),
        "current_resume_path": profile.get("current_resume_path", ""),
    }

    for label, configured_path in resume_paths.items():
        path = resolve_project_path(configured_path)
        if not configured_path:
            print(f"{label}: not configured")
            continue
        if not path.exists():
            print(f"{label}: missing at {path}")
            continue

        if path.suffix.lower() in [".md", ".txt"]:
            text = path.read_text(encoding="utf-8")
            print(f"{label}: readable text file at {path} ({len(text)} characters)")
        elif path.suffix.lower() == ".pdf":
            pdf_status = extract_pdf_status(path)
            print(f"{label}: {pdf_status}")
        else:
            print(f"{label}: exists at {path}, unsupported file type {path.suffix!r}")


def resolve_project_path(configured_path: str) -> Path:
    path = Path(configured_path)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def extract_pdf_status(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return (
            f"found PDF at {path}, but pypdf is not installed. "
            "Run `venv/bin/pip install -r requirements.txt` before PDF ingestion."
        )

    reader = PdfReader(path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return f"readable PDF at {path} ({len(reader.pages)} pages, {len(text)} characters)"


# Build the LLM instruction prompt while keeping role focus and skill categories configurable.
def build_resume_content_prompt(
    job: pd.Series,
    job_details: dict[str, Any],
    master_resume: str,
    reference_text: str,
    preferences: dict[str, Any],
) -> str:
    role_focus = build_role_focus(preferences)
    skill_categories = list(preferences.get("resume", {}).get("skills", {}).keys())
    job_context = "\n".join(
        [
            f"Job title: {job['job_title']}",
            f"Company: {job['company']}",
            f"Location: {job['location']}",
            f"Job type: {job['job_type']}",
            f"Salary: {job['salary']}",
            f"Job link: {job['job_link']}",
            f"Source: {job_details.get('source', '')}",
            f"Date posted: {job_details.get('date_posted', '')}",
            f"Company industry: {job_details.get('company_industry', '')}",
            f"Skills: {job_details.get('skills', '')}",
            "",
            "Job description:",
            job_details.get("description", ""),
            "",
            "Company description:",
            job_details.get("company_description", ""),
        ]
    )

    return f"""
You are a skilled recruiter for {role_focus}. You are experienced in vetting
resumes and also helping design them. Your task is to analyse the job
description and the master resume to create a targeted resume. Your ONLY output
is JSON. A separate program will render your JSON into the final one-page resume,
so your job is entirely about CONTENT DECISIONS: what to include, what to cut,
and how to word it.

=====================================================================
PHASE 1 -- ANALYZE (internal, do not include in output)
=====================================================================
1. Extract the 8-12 most important requirements, responsibilities, and
   keywords from the job description (tools, methods, domains, soft
   skills, seniority signals).
2. Score every experience entry and every project in the MASTER RESUME
   against those requirements.
3. Decide what to include using the SELECTION RULES below.

=====================================================================
SELECTION RULES (hard constraints)
=====================================================================
- Experience: include at most 3 roles, whichever are most relevant to
  this job. Fewer than 3 is fine if fewer are relevant. Drop roles with
  no technical/analytical relevance (e.g., campus ambassador, TA admin
  duties) unless the job specifically values that kind of experience
  (teaching, mentorship, community-facing work).
- Bullets per role: 3, chosen or synthesized from the master resume
  to best match Phase 1 keywords.
- Projects: always exactly 3, ranked by relevance to the job. Prefer
  relevance over "impressiveness" -- a smaller project that matches the
  JD's tools/domain beats a flashier one that doesn't.
- Bullets per project: exactly 2. Master resume project entries are
  prose paragraphs -- compress each into exactly 2 resume-style bullets.
  Preserve every quantified result relevant to this job (accuracy, AUC,
  F1, RMSE, sample sizes, percentages) exactly as stated. Do not
  recompute, round further, or average any numbers.
- Total combined experience + project bullets should not exceed ~14-15.
  If selections run long, cut the least relevant bullet first, then the
  least relevant project or role -- never shorten by vague compression.
- skills_additions: for each category, you may add a skill ONLY if it
  is evidenced elsewhere in the master resume's experience or project
  text AND relevant to this job. Never add anything untraceable to the
  master resume. If nothing qualifies for a category, return an empty
  list for it. Do not attempt to reorder, rename, or remove anything --
  you are only ever appending. Use these configured skill categories:
  {", ".join(skill_categories) if skill_categories else "none"}.

=====================================================================
REWRITING RULES
=====================================================================
- Every bullet follows this shape: [strong past-tense action verb] +
  [what was built or done] + [method/tool] + [quantified outcome or
  business impact]. Match the density and tone of the REFERENCE RESUME
  TEXT below.
- No two bullets across the entire output may start with the same
  verb. Never use weak openers: "Responsible for," "Worked on,"
  "Helped," "Assisted with."
- You may rephrase and reorder emphasis within a true bullet to
  foreground the part most relevant to this job, but never change what
  was actually done, invent a metric, or attribute a result that isn't
  in the master resume.
- Where the JD uses specific terminology for something genuinely done
  (e.g., JD says "predictive modeling," master resume says "XGBoost
  classifier"), fold in the JD's terminology alongside the specific
  method, not instead of it.

=====================================================================
FACTUAL GUARDRAILS (absolute)
=====================================================================
- The MASTER RESUME is the sole source of truth. Never invent
  employers, dates, metrics, tools, or results, and never add a skill
  that cannot be traced to the master resume text.

=====================================================================
OUTPUT FORMAT
=====================================================================
Return ONLY a JSON object matching this exact schema -- no markdown
fences, no commentary, no Phase 1 analysis, nothing before or after it:

{build_resume_content_schema(skill_categories)}

=====================================================================
INPUTS
=====================================================================
Target job:
{job_context}

MASTER RESUME:
{master_resume}

REFERENCE RESUME TEXT (style/density model):
{reference_text}
""".strip()


def build_role_focus(preferences: dict[str, Any]) -> str:
    profile = preferences.get("profile", {})
    fit = preferences.get("fit", {})
    field = clean_text(profile.get("field", ""))
    title_keywords = fit.get("target_title_keywords", [])
    if field and title_keywords:
        examples = ", ".join(title_keywords[:4])
        return f"{field} roles matching configured target titles such as {examples}"
    if field:
        return f"{field} roles"
    if title_keywords:
        examples = ", ".join(title_keywords[:4])
        return f"roles matching configured target titles such as {examples}"
    return "the configured target roles"


def build_resume_content_schema(skill_categories: list[str]) -> str:
    skill_lines = [
        f'    "{category}": ["string", ...]' for category in skill_categories
    ]
    skills_schema = "{\n" + ",\n".join(skill_lines) + "\n  }" if skill_lines else "{}"
    return f"""
{{
  "experience": [
    {{
      "title": "string",
      "company": "string",
      "location": "string",
      "dates": "string, e.g. 'August 2024 -- May 2025'",
      "bullets": ["string", "string"]   // 2-3 bullets
    }}
    // at most 3 entries total, ranked by relevance to the job
  ],
  "projects": [
    {{
      "name": "string",
      "tools": "string, e.g. 'Python, XGBoost, Lasso, Random Forest'",
      "bullets": ["string", "string"]   // exactly 2 bullets
    }}
    // exactly 3 entries, ranked by relevance to the job
  ],
  "skills_additions": {skills_schema}
}}
""".strip()


def escape_latex(text: str) -> str:
    return _LATEX_ESCAPE_RE.sub(lambda m: _LATEX_SPECIAL[m.group()], text)


def render_resume(content: dict[str, Any], preferences: dict[str, Any]) -> str:
    resume = preferences.get("resume", {})
    body = "\n\n\n".join(
        [
            render_header(resume.get("header", {})),
            render_education(resume.get("education", [])),
            render_experience(content["experience"]),
            render_projects(content["projects"]),
            render_skills(
                content.get("skills_additions", {}),
                resume.get("skills", {}),
            ),
        ]
    )
    preamble = build_latex_preamble(resume.get("latex", {}))
    return f"{preamble}\n\n\\begin{{document}}\n\n{body}\n\n\\end{{document}}\n"


def build_latex_preamble(latex_config: dict[str, Any]) -> str:
    font_path = str(latex_config.get("font_path", ""))
    if font_path and not font_path.endswith("/"):
        font_path = f"{font_path}/"
    font_name = str(latex_config.get("font_name", "Latin Modern Roman"))
    replacements = {
        "__FONT_PATH__": font_path,
        "__UPRIGHT_FONT__": str(latex_config.get("upright_font", "")),
        "__BOLD_FONT__": str(latex_config.get("bold_font", "")),
        "__ITALIC_FONT__": str(latex_config.get("italic_font", "")),
        "__BOLD_ITALIC_FONT__": str(latex_config.get("bold_italic_font", "")),
        "__FONT_NAME__": font_name,
    }
    preamble = TEMPLATE_PREAMBLE
    if not font_path and not any(
        replacements[key]
        for key in [
            "__UPRIGHT_FONT__",
            "__BOLD_FONT__",
            "__ITALIC_FONT__",
            "__BOLD_ITALIC_FONT__",
        ]
    ):
        preamble = preamble.replace(
            "\\setmainfont[\n"
            "  Path = {__FONT_PATH__},\n"
            "  UprightFont = __UPRIGHT_FONT__,\n"
            "  BoldFont = __BOLD_FONT__,\n"
            "  ItalicFont = __ITALIC_FONT__,\n"
            "  BoldItalicFont = __BOLD_ITALIC_FONT__\n"
            "]{__FONT_NAME__}",
            "\\setmainfont{__FONT_NAME__}",
        )
    for placeholder, value in replacements.items():
        preamble = preamble.replace(placeholder, value)
    return preamble


def render_header(header: dict[str, Any]) -> str:
    contact_parts = []
    email = clean_text(header.get("email", ""))
    if email:
        contact_parts.append(f"\\href{{mailto:{email}}}{{\\underline{{Email}}}}")
    for label, key in [
        ("LinkedIn", "linkedin"),
        ("GitHub", "github"),
        ("Portfolio", "portfolio"),
    ]:
        url = clean_text(header.get(key, ""))
        if url:
            contact_parts.append(f"\\href{{{url}}}{{\\underline{{{label}}}}}")
    location = clean_text(header.get("location", ""))
    if location:
        contact_parts.append(escape_latex(location))

    return (
        "\\begin{center}\n"
        "    \\textbf{\\Huge "
        f"{escape_latex(clean_text(header.get('name', '')))}}} \\\\ \\vspace{{1pt}}\n"
        f"    \\small {' $|$ '.join(contact_parts)}\n"
        "\\end{center}"
    )


def render_education(education: list[dict[str, Any]]) -> str:
    lines = ["\\section{Education}", "  \\resumeSubHeadingListStart"]
    for entry in education:
        institution = escape_latex(clean_text(entry.get("institution", "")))
        location = escape_latex(clean_text(entry.get("location", "")))
        degree = escape_latex(clean_text(entry.get("degree", "")))
        dates = escape_latex(clean_text(entry.get("dates", "")))
        lines.append(
            f"    \\resumeSubheading\n"
            f"      {{{institution}}}{{{location}}}\n"
            f"      {{{degree}}}{{{dates}}}"
        )
    lines.append("  \\resumeSubHeadingListEnd")
    return "\n".join(lines)


def render_experience(experience: list[dict[str, Any]]) -> str:
    lines = ["\\section{Experience}", "  \\resumeSubHeadingListStart"]
    for role in experience[:3]:
        title = escape_latex(role["title"])
        company = escape_latex(role["company"])
        location = escape_latex(role["location"])
        dates = escape_latex(role["dates"])
        lines.append(
            f"    \\resumeSubheading\n"
            f"      {{{title}}}{{{dates}}}\n"
            f"      {{{company}}}{{{location}}}"
        )
        lines.append("      \\resumeItemListStart")
        for bullet in role["bullets"][:3]:
            lines.append(f"        \\resumeItem{{{escape_latex(bullet)}}}")
        lines.append("      \\resumeItemListEnd")
    lines.append("  \\resumeSubHeadingListEnd")
    return "\n".join(lines)


def render_projects(projects: list[dict[str, Any]]) -> str:
    lines = ["\\section{Projects}", "    \\resumeSubHeadingListStart"]
    for project in projects[:3]:
        name = escape_latex(project["name"])
        tools = escape_latex(project["tools"])
        heading = f"\\textbf{{{name}}} $|$ \\emph{{{tools}}}"
        lines.append(f"      \\resumeProjectHeading\n          {{{heading}}}{{}}")
        lines.append("          \\resumeItemListStart")
        for bullet in project["bullets"][:2]:
            lines.append(f"            \\resumeItem{{{escape_latex(bullet)}}}")
        lines.append("          \\resumeItemListEnd")
    lines.append("    \\resumeSubHeadingListEnd")
    return "\n".join(lines)


def render_skills(
    skills_additions: dict[str, list[str]], base_skills: dict[str, list[str]]
) -> str:
    lines = ["\\section{Skills}", " \\begin{itemize}[leftmargin=0.15in, label={}]"]
    lines.append("    \\small{\\item{")
    rows = []
    for category, base_list in base_skills.items():
        additions = skills_additions.get(category.replace("\\&", "&"), []) or []
        additions = skills_additions.get(category, []) or additions
        combined = list(base_list) + [
            item for item in additions if item not in base_list
        ]
        combined_escaped = [escape_latex(skill) for skill in combined]
        rows.append(
            f"     \\textbf{{{escape_latex(category)}}}{{: {', '.join(combined_escaped)}}}"
        )
    lines.append(" \\\\\n".join(rows))
    lines.append("    }}")
    lines.append(" \\end{itemize}")
    return "\n".join(lines)


def draft_next_resume(output_path: Path, preferences: dict[str, Any]) -> None:
    jobs = read_existing_jobs(output_path, preferences)
    candidate_index = next_resume_candidate_index(jobs)
    if candidate_index is None:
        print("No found jobs with resume_status=not_started.")
        return

    job = jobs.loc[candidate_index]
    resume_content = generate_tailored_resume_content(job, preferences)
    draft_tex = render_resume(resume_content, preferences)
    output_dir = resolve_project_path(preferences["resume_generation"]["output_dir"])

    slug = slugify(f"{job['company']} {job['job_title']}")
    resume_dir = output_dir / slug
    resume_dir.mkdir(parents=True, exist_ok=True)
    json_path = resume_dir / f"{slug}.json"
    tex_path = resume_dir / f"{slug}.tex"
    json_path.write_text(
        json.dumps(resume_content, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tex_path.write_text(draft_tex, encoding="utf-8")
    pdf_path = compile_latex(tex_path, resume_dir)

    jobs.loc[candidate_index, "application_status"] = "ready_to_apply"
    jobs.loc[candidate_index, "resume_status"] = "drafted"
    jobs.to_csv(output_path, quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False)

    print(f"Drafted resume for {job['job_title']} at {job['company']}.")
    print(f"Content JSON: {json_path}")
    print(f"LaTeX: {tex_path}")
    if pdf_path:
        print(f"PDF: {pdf_path}")
    else:
        print("PDF compile did not complete; LaTeX draft was saved.")


def check_next_job_context(output_path: Path, preferences: dict[str, Any]) -> None:
    jobs = read_existing_jobs(output_path, preferences)
    candidate_index = next_resume_candidate_index(jobs)
    if candidate_index is None:
        print("No found jobs with resume_status=not_started.")
        return

    job = jobs.loc[candidate_index]
    details = read_job_details_cache(preferences).get(str(job["job_link"]), {})
    description = details.get("description", "")
    print(f"Next job: {job['job_title']} at {job['company']}")
    print(f"Job link: {job['job_link']}")
    print(f"Cached details: {'yes' if details else 'no'}")
    print(f"Description characters: {len(description)}")


def next_resume_candidate_index(jobs: pd.DataFrame) -> int | None:
    if jobs.empty:
        return None
    candidates = jobs[
        (jobs["application_status"].astype(str) == "found")
        & (jobs["resume_status"].astype(str).isin(["", "not_started", "nan"]))
    ]
    if candidates.empty:
        return None
    return int(candidates.index[0])


def generate_tailored_resume(job: pd.Series, preferences: dict[str, Any]) -> str:
    return render_resume(generate_tailored_resume_content(job, preferences), preferences)


def generate_tailored_resume_content(
    job: pd.Series, preferences: dict[str, Any]
) -> dict[str, Any]:
    load_dotenv_if_available()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set. Add it to .env or your shell.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI SDK is not installed. Run `venv/bin/pip install -r requirements.txt`."
        ) from exc

    master_resume = read_text_file(preferences["profile"]["master_resume_path"])
    reference_text = read_resume_reference_text(
        preferences["profile"].get("current_resume_path", "")
    )
    model = preferences["resume_generation"]["model"]

    job_details = read_job_details_cache(preferences).get(str(job["job_link"]), {})
    prompt = build_resume_content_prompt(
        job,
        job_details,
        master_resume,
        reference_text,
        preferences,
    )
    client = OpenAI()
    response = client.responses.create(
        model=model,
        input=prompt,
        temperature=0.2,
    )
    return parse_resume_content_json(extract_response_text(response))


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env")


def read_text_file(configured_path: str) -> str:
    return resolve_project_path(configured_path).read_text(encoding="utf-8")


def read_resume_reference_text(configured_path: str) -> str:
    if not configured_path:
        return ""
    path = resolve_project_path(configured_path)
    if path.suffix.lower() in [".md", ".txt"]:
        return path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".pdf":
        return ""

    from pypdf import PdfReader

    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", "")
    if output_text:
        return output_text.strip()
    return str(response).strip()


def parse_resume_content_json(output_text: str) -> dict[str, Any]:
    cleaned = strip_json_fence(output_text)
    try:
        content = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Resume model response was not valid JSON.") from exc

    validate_resume_content(content)
    return content


def strip_json_fence(output_text: str) -> str:
    cleaned = output_text.strip()
    if not cleaned.startswith("```"):
        return cleaned

    lines = cleaned.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def validate_resume_content(content: Any) -> None:
    if not isinstance(content, dict):
        raise RuntimeError("Resume content JSON must be an object.")

    required_keys = ["experience", "projects", "skills_additions"]
    missing = [key for key in required_keys if key not in content]
    if missing:
        raise RuntimeError(f"Resume content JSON is missing: {', '.join(missing)}.")

    if not isinstance(content["experience"], list):
        raise RuntimeError("Resume content JSON field 'experience' must be a list.")
    if not isinstance(content["projects"], list):
        raise RuntimeError("Resume content JSON field 'projects' must be a list.")
    if not isinstance(content["skills_additions"], dict):
        raise RuntimeError(
            "Resume content JSON field 'skills_additions' must be an object."
        )

    for role in content["experience"]:
        validate_resume_entry(
            role,
            ["title", "company", "location", "dates", "bullets"],
        )
    for project in content["projects"]:
        validate_resume_entry(project, ["name", "tools", "bullets"])


def validate_resume_entry(entry: Any, required_keys: list[str]) -> None:
    if not isinstance(entry, dict):
        raise RuntimeError("Resume content entries must be objects.")

    missing = [key for key in required_keys if key not in entry]
    if missing:
        raise RuntimeError(f"Resume content entry is missing: {', '.join(missing)}.")
    if not isinstance(entry["bullets"], list):
        raise RuntimeError("Resume content entry field 'bullets' must be a list.")


# Compile the generated TeX in a temporary build folder and keep only the PDF.
def compile_latex(tex_path: Path, output_dir: Path) -> Path | None:
    build_dir = output_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                "xelatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-output-directory={build_dir}",
                str(tex_path),
            ],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    compiled_pdf_path = build_dir / f"{tex_path.stem}.pdf"
    pdf_path = output_dir / f"{tex_path.stem}.pdf"
    if result.returncode == 0 and compiled_pdf_path.exists():
        compiled_pdf_path.replace(pdf_path)
        clean_latex_build_dir(build_dir)
        return pdf_path
    clean_latex_build_dir(build_dir)
    return None


def clean_latex_build_dir(build_dir: Path) -> None:
    for path in build_dir.iterdir():
        if path.is_file():
            path.unlink()
    try:
        build_dir.rmdir()
    except OSError:
        pass


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "resume-draft"


def main() -> None:
    args = parse_args()
    preferences = load_preferences(args.config)

    if args.draft_next_resume:
        draft_next_resume(args.output, preferences)
        return

    if args.check_next_job_context:
        check_next_job_context(args.output, preferences)
        return

    if args.check_resumes:
        check_resumes(preferences)
        return

    if args.compact_existing:
        existing_jobs = read_existing_jobs(args.output, preferences)
        existing_jobs.to_csv(
            args.output,
            quoting=csv.QUOTE_NONNUMERIC,
            escapechar="\\",
            index=False,
        )
        print(f"Compacted {len(existing_jobs)} jobs in {args.output}.")
        return

    settings = resolve_search_settings(args, preferences)
    raw_jobs, source_counts = pull_jobs(settings, args.dry_run)

    print_source_summary(source_counts)
    if args.dry_run:
        print("Dry run complete. No jobs were pulled or saved.")
        return

    normalized_jobs = normalize_jobs(raw_jobs, preferences, settings)
    update_job_details_cache(raw_jobs, normalized_jobs, preferences, settings)
    print_quality_summary(raw_jobs, normalized_jobs, settings)
    added_count, total_count = update_jobs_csv(args.output, normalized_jobs, preferences)
    print(f"Added {added_count} new jobs to {args.output}.")
    print(f"Tracking {total_count} total jobs.")


if __name__ == "__main__":
    main()

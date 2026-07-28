# JobPilot

JobPilot is a local command-line assistant for job search organization. It can pull job listings, score them against configurable role-fit rules, track selected jobs in a CSV, cache job descriptions locally, and draft tailored LaTeX resumes from a master resume using the OpenAI API.

It does not submit applications automatically. You stay in control of every application.

## Features

- Pull listings with `python-jobspy` from configured job sources.
- Filter for role title, preferred keywords, freshness, location, remote fit, seniority, and experience requirements.
- Track jobs in `data/jobs.csv`.
- Cache full job descriptions in an ignored local file for resume drafting.
- Generate tailored resume content with JSON validation.
- Render a one-page LaTeX resume and optionally compile it with XeLaTeX.

## Project Structure

- `src/main.py`: CLI entry point and application logic.
- `config/preferences.toml`: public-safe example preferences.
- `data/jobs.csv`: empty starter tracker.
- `data/job_details.json`: ignored local cache created after job pulls.
- `resumes/source/Master_Resume.example.md`: example master resume format.
- `resumes/generated/`: ignored generated resume outputs.
- `templates/resume.tex`: reference LaTeX resume template.

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

For resume drafting, create a local `.env` file:

```bash
cp .env.example .env
```

Then set `OPENAI_API_KEY` in `.env`.

Resume PDF generation also requires XeLaTeX. On macOS, install MacTeX or BasicTeX. On Linux, install a TeX Live package that includes `xelatex`.

## Configure

Edit `config/preferences.toml` before using the tool:

- Replace placeholder contact details in `[resume.header]`.
- Set your preferred field, location, search terms, job types, and fit keywords.
- Replace `resumes/source/Master_Resume.example.md` with your own private master resume path.
- Optionally set `current_resume_path` to a PDF or text resume used as a style reference.

Private resume files under `resumes/source/` are ignored by git by default, except the checked-in example.

## Usage

Preview planned searches without calling job boards:

```bash
python src/main.py --dry-run
```

Pull jobs and update the tracker:

```bash
python src/main.py
```

Run a targeted search:

```bash
python src/main.py --search-term "data analyst intern" --site indeed --results-wanted 10
```

Check configured resume inputs:

```bash
python src/main.py --check-resumes
```

Check whether the next job has cached description context:

```bash
python src/main.py --check-next-job-context
```

Draft the next resume for the first job marked `application_status=found` and `resume_status=not_started`:

```bash
python src/main.py --draft-next-resume
```

Generated resume artifacts are written to:

```text
resumes/generated/<company-title-slug>/
```

## Tracker Format

`data/jobs.csv` stores:

- `job_title`
- `company`
- `location`
- `salary`
- `job_type`
- `job_link`
- `application_status`
- `resume_status`

Common statuses:

- `application_status`: `found`, `ready_to_apply`, `applied`, `rejected`, `interview`
- `resume_status`: `not_started`, `drafted`, `reviewed`

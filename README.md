# Bajrang Computer Institute of Technology (BCIT)

Flask website with Cloudflare D1 for structured data and Cloudflare R2 for files.

## Features

- Public institute website, courses, admission and enquiry
- Student and admin portals
- Attendance, fees, payments and receipts
- Exams, assignments and study materials
- AI Assistant

## Run locally

1. Create a virtual environment.
2. Install dependencies: `python -m pip install -r requirements.txt`
3. Put your existing private settings in `.env` (do not commit or share it).
4. Run: `python app.py`

## Render deployment

Use this Start Command for the free-trial configuration: `gunicorn -c gunicorn.conf.py app:app`

Keep all existing Render environment variables unchanged. Do not upload `.env` to GitHub.

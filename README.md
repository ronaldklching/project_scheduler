# Installation Coordination System

A Flask app for coordinating installation projects, schedules, site requests, project media, and incident logs.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="replace-with-a-long-random-value"
python3 app.py
```

The app runs on `http://127.0.0.1:8083` by default.

## Runtime Data

The SQLite database, uploaded files, and generated backups are intentionally ignored by Git:

- `installation_coordination.db`
- `static/uploads/`
- `backups/`

Back up those files separately before deploying or moving the application.

## Default Accounts

On first startup the app seeds demo accounts in `app.py`. Change or remove those seeded credentials before using the app with real project data.

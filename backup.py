import os
import shutil
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "installation_coordination.db"
UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
BACKUP_DIR = BASE_DIR / "backups"
RETENTION_DAYS = 30


def today_backup_prefix():
    return date.today().isoformat()


def backup_exists_for_today():
    prefix = today_backup_prefix()
    return (BACKUP_DIR / f"{prefix}_database.db").exists() and (BACKUP_DIR / f"{prefix}_uploads.zip").exists()


def backup_database(destination):
    if not DATABASE.exists():
        return False

    source = sqlite3.connect(DATABASE)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return True


def backup_uploads(destination):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        if not UPLOAD_FOLDER.exists():
            return
        for path in UPLOAD_FOLDER.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(BASE_DIR))


def cleanup_old_backups(retention_days=RETENTION_DAYS):
    if not BACKUP_DIR.exists():
        return

    cutoff = datetime.now().timestamp() - (retention_days * 24 * 60 * 60)
    for path in BACKUP_DIR.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()


def run_backup(force=False):
    BACKUP_DIR.mkdir(exist_ok=True)

    if not force and backup_exists_for_today():
        return {
            "created": False,
            "message": "Today's backup already exists.",
        }

    prefix = today_backup_prefix()
    database_backup = BACKUP_DIR / f"{prefix}_database.db"
    uploads_backup = BACKUP_DIR / f"{prefix}_uploads.zip"
    temp_database_backup = database_backup.with_suffix(".db.tmp")
    temp_uploads_backup = uploads_backup.with_suffix(".zip.tmp")

    if temp_database_backup.exists():
        temp_database_backup.unlink()
    if temp_uploads_backup.exists():
        temp_uploads_backup.unlink()

    database_created = backup_database(temp_database_backup)
    backup_uploads(temp_uploads_backup)

    if database_created:
        os.replace(temp_database_backup, database_backup)
    elif temp_database_backup.exists():
        temp_database_backup.unlink()

    os.replace(temp_uploads_backup, uploads_backup)
    cleanup_old_backups()

    return {
        "created": True,
        "database": str(database_backup) if database_created else None,
        "uploads": str(uploads_backup),
    }


if __name__ == "__main__":
    result = run_backup(force=True)
    print(result["message"] if "message" in result else "Backup created.")

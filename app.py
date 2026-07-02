import os
import secrets
import sqlite3
from datetime import date
from functools import wraps

from backup import backup_exists_for_today, run_backup
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "installation_coordination.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-for-production")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

PROJECT_STATUSES = ["Draft", "Pending Confirmation", "Confirmed", "In Progress", "Completed"]
SCHEDULE_STATUSES = ["Draft", "Pending", "Confirmed"]
FOLLOW_UP_STATUSES = ["Pending", "In Progress", "Complete"]
USER_ROLES = ["admin", "pm", "installer", "client"]
MEDIA_CATEGORIES = {
    "inspection": "Inspection",
    "installation_complete": "Installation Complete",
}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic"}
VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "m4v"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
LAST_BACKUP_CHECK_DATE = None


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    migrate_users_for_client_role(db)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'pm', 'installer', 'client')),
            client_name TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_code TEXT NOT NULL UNIQUE,
            client_name TEXT NOT NULL,
            site_name TEXT NOT NULL,
            digital_g_contact TEXT,
            pm_name TEXT NOT NULL,
            scope_of_work TEXT,
            todays_scope TEXT,
            delivery_date TEXT,
            site_inspection_date TEXT,
            installation_dates TEXT,
            share_code TEXT,
            schedule_status TEXT NOT NULL DEFAULT 'Draft',
            status TEXT NOT NULL DEFAULT 'Draft',
            next_action TEXT,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS incident_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            incident_date TEXT NOT NULL,
            issue TEXT NOT NULL,
            root_cause TEXT,
            action_taken TEXT,
            responsible_person TEXT,
            follow_up_status TEXT NOT NULL DEFAULT 'Pending',
            attachment_original_filename TEXT,
            attachment_filename TEXT,
            attachment_media_type TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS project_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            media_type TEXT NOT NULL,
            uploaded_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS schedule_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            requested_dates TEXT NOT NULL,
            notes TEXT,
            requested_by TEXT,
            status TEXT NOT NULL DEFAULT 'Pending PM Review',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            reviewed_by TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS schedule_change_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            old_installation_dates TEXT,
            new_installation_dates TEXT,
            old_schedule_status TEXT,
            new_schedule_status TEXT,
            reason TEXT NOT NULL,
            changed_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        """
    )

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    ensure_project_share_codes(db)
    ensure_incident_attachment_columns(db)
    db.execute("UPDATE incident_logs SET follow_up_status = 'Pending' WHERE follow_up_status = 'Open'")
    db.execute("UPDATE incident_logs SET follow_up_status = 'Complete' WHERE follow_up_status = 'Closed'")

    users = [
        ("admin", "admin123", "admin", None, 1),
        ("pm", "pm123", "pm", None, 1),
        ("installer", "installer123", "installer", None, 1),
        ("gammon", "gammon123", "client", "Gammon", 1),
    ]
    for username, password, role, client_name, active in users:
        db.execute(
            """
            INSERT OR IGNORE INTO users (username, password_hash, role, client_name, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, generate_password_hash(password), role, client_name, active),
        )
    db.commit()


def generate_share_code():
    while True:
        code = secrets.token_urlsafe(8)
        exists = get_db().execute("SELECT 1 FROM projects WHERE share_code = ?", (code,)).fetchone()
        if exists is None:
            return code


def migrate_users_for_client_role(db):
    table = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if table is None:
        return

    columns = db.execute("PRAGMA table_info(users)").fetchall()
    column_names = {column["name"] for column in columns}
    create_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()["sql"]

    needs_rebuild = "client" not in create_sql or "client_name" not in column_names or "active" not in column_names
    if not needs_rebuild:
        return

    db.executescript(
        """
        CREATE TABLE users_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'pm', 'installer', 'client')),
            client_name TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    if "client_name" in column_names and "active" in column_names:
        db.execute(
            """
            INSERT INTO users_new (id, username, password_hash, role, client_name, active)
            SELECT id, username, password_hash, role, client_name, active FROM users
            """
        )
    elif "client_name" in column_names:
        db.execute(
            """
            INSERT INTO users_new (id, username, password_hash, role, client_name, active)
            SELECT id, username, password_hash, role, client_name, 1 FROM users
            """
        )
    else:
        db.execute(
            """
            INSERT INTO users_new (id, username, password_hash, role, client_name, active)
            SELECT id, username, password_hash, role, NULL, 1 FROM users
            """
        )
    db.executescript(
        """
        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;
        """
    )


def ensure_project_share_codes(db):
    columns = db.execute("PRAGMA table_info(projects)").fetchall()
    column_names = {column["name"] for column in columns}
    if "share_code" not in column_names:
        db.execute("ALTER TABLE projects ADD COLUMN share_code TEXT")

    projects_without_codes = db.execute(
        "SELECT id FROM projects WHERE share_code IS NULL OR share_code = ''"
    ).fetchall()
    for project in projects_without_codes:
        db.execute(
            "UPDATE projects SET share_code = ? WHERE id = ?",
            (secrets.token_urlsafe(8), project["id"]),
        )


def ensure_incident_attachment_columns(db):
    columns = db.execute("PRAGMA table_info(incident_logs)").fetchall()
    column_names = {column["name"] for column in columns}
    for column_name in (
        "attachment_original_filename",
        "attachment_filename",
        "attachment_media_type",
    ):
        if column_name not in column_names:
            db.execute(f"ALTER TABLE incident_logs ADD COLUMN {column_name} TEXT")


@app.before_request
def load_logged_in_user():
    maybe_run_daily_backup()

    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def maybe_run_daily_backup():
    global LAST_BACKUP_CHECK_DATE

    if request.endpoint == "static":
        return

    today = date.today().isoformat()
    if LAST_BACKUP_CHECK_DATE == today:
        return

    LAST_BACKUP_CHECK_DATE = today
    if backup_exists_for_today():
        return

    try:
        run_backup()
    except Exception as error:
        app.logger.error("Daily backup failed: %s", error)


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def pm_or_admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        if g.user["role"] not in ("admin", "pm"):
            abort(403)
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        if g.user["role"] != "admin":
            abort(403)
        return view(**kwargs)

    return wrapped_view


def get_project(project_id):
    project = get_db().execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        abort(404)
    return project


def client_can_access_project(user, project):
    if user["role"] != "client":
        return True
    return bool(user["client_name"]) and project["client_name"].casefold() == user["client_name"].casefold()


def get_accessible_project(project_id):
    project = get_project(project_id)
    if not client_can_access_project(g.user, project):
        abort(403)
    return project


def get_project_by_share_code(share_code):
    project = get_db().execute(
        "SELECT * FROM projects WHERE share_code = ?",
        (share_code,),
    ).fetchone()
    if project is None:
        abort(404)
    return project


def get_incident(project_id, incident_id):
    incident = get_db().execute(
        "SELECT * FROM incident_logs WHERE id = ? AND project_id = ?",
        (incident_id, project_id),
    ).fetchone()
    if incident is None:
        abort(404)
    return incident


def get_user(user_id):
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    return user


def installer_can_view_dates(project):
    return project["schedule_status"] == "Confirmed"


def split_installation_dates(value):
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def collect_installation_dates():
    dates = [date.strip() for date in request.form.getlist("installation_dates") if date.strip()]
    return "\n".join(dates)


def collect_requested_dates():
    dates = [date.strip() for date in request.form.getlist("requested_dates") if date.strip()]
    return "\n".join(dates)


def get_file_extension(filename):
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


def allowed_upload(filename):
    return get_file_extension(filename) in ALLOWED_EXTENSIONS


def media_type_for(filename):
    extension = get_file_extension(filename)
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return "file"


def save_upload(file_storage, folder_name):
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_upload(file_storage.filename):
        raise ValueError("Upload must be an image or short video file.")

    original_filename = secure_filename(file_storage.filename)
    extension = get_file_extension(original_filename)
    stored_filename = f"{secrets.token_hex(12)}.{extension}"
    relative_folder = os.path.join("uploads", folder_name)
    absolute_folder = os.path.join(app.static_folder, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    file_storage.save(os.path.join(absolute_folder, stored_filename))

    return {
        "original_filename": original_filename,
        "stored_filename": f"{relative_folder}/{stored_filename}",
        "media_type": media_type_for(original_filename),
    }


def collect_project_form():
    schedule_status = request.form.get("schedule_status", "Draft")
    status = request.form.get("status", "Draft")
    if schedule_status not in SCHEDULE_STATUSES:
        schedule_status = "Draft"
    if status not in PROJECT_STATUSES:
        status = "Draft"

    return {
        "project_code": request.form.get("project_code", "").strip(),
        "client_name": request.form.get("client_name", "").strip(),
        "site_name": request.form.get("site_name", "").strip(),
        "digital_g_contact": request.form.get("digital_g_contact", "").strip(),
        "pm_name": request.form.get("pm_name", "").strip(),
        "scope_of_work": request.form.get("scope_of_work", "").strip(),
        "todays_scope": request.form.get("todays_scope", "").strip(),
        "delivery_date": request.form.get("delivery_date", "").strip(),
        "site_inspection_date": request.form.get("site_inspection_date", "").strip(),
        "installation_dates": collect_installation_dates(),
        "schedule_status": schedule_status,
        "status": status,
        "next_action": request.form.get("next_action", "").strip(),
        "notes": request.form.get("notes", "").strip(),
    }


def schedule_changed(project, data):
    old_dates = project["installation_dates"] or ""
    new_dates = data["installation_dates"] or ""
    old_status = project["schedule_status"] or ""
    new_status = data["schedule_status"] or ""
    return old_dates != new_dates or old_status != new_status


def add_schedule_change_log(project_id, old_dates, new_dates, old_status, new_status, reason, changed_by):
    get_db().execute(
        """
        INSERT INTO schedule_change_logs (
            project_id, old_installation_dates, new_installation_dates,
            old_schedule_status, new_schedule_status, reason, changed_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, old_dates or "", new_dates or "", old_status or "", new_status or "", reason, changed_by),
    )


def validate_project(data):
    errors = []
    for field, label in (
        ("project_code", "Project code"),
        ("client_name", "Client name"),
        ("site_name", "Site name"),
        ("pm_name", "PM name"),
    ):
        if not data[field]:
            errors.append(f"{label} is required.")
    return errors


@app.route("/")
def index():
    if g.user is None:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if user and user["active"] and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            flash("Logged in successfully.", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid username, password, or inactive account.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("login"))


@app.route("/change-password", methods=("GET", "POST"))
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not check_password_hash(g.user["password_hash"], current_password):
            flash("Current password is incorrect.", "danger")
        elif len(new_password) < 6:
            flash("New password must be at least 6 characters.", "danger")
        elif new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
        else:
            get_db().execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), g.user["id"]),
            )
            get_db().commit()
            flash("Password changed.", "success")
            return redirect(url_for("dashboard"))

    return render_template("change_password.html")


@app.route("/users")
@admin_required
def users_index():
    users = get_db().execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("users.html", users=users)


@app.route("/users/new", methods=("GET", "POST"))
@admin_required
def create_user():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        client_name = request.form.get("client_name", "").strip()
        active = 1 if request.form.get("active") == "on" else 0

        errors = []
        if not username:
            errors.append("Username is required.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if role not in USER_ROLES:
            errors.append("Choose a valid role.")
        if role == "client" and not client_name:
            errors.append("Client name is required for client users.")

        if errors:
            for error in errors:
                flash(error, "danger")
        else:
            try:
                get_db().execute(
                    """
                    INSERT INTO users (username, password_hash, role, client_name, active)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        generate_password_hash(password),
                        role,
                        client_name if role == "client" else None,
                        active,
                    ),
                )
                get_db().commit()
                flash("User created.", "success")
                return redirect(url_for("users_index"))
            except sqlite3.IntegrityError:
                flash("Username already exists.", "danger")

    return render_template("user_form.html", user=None, roles=USER_ROLES)


@app.route("/users/<int:user_id>/edit", methods=("GET", "POST"))
@admin_required
def edit_user(user_id):
    user = get_user(user_id)
    if request.method == "POST":
        role = request.form.get("role", "")
        client_name = request.form.get("client_name", "").strip()
        active = 1 if request.form.get("active") == "on" else 0

        errors = []
        if role not in USER_ROLES:
            errors.append("Choose a valid role.")
        if role == "client" and not client_name:
            errors.append("Client name is required for client users.")
        if user["id"] == g.user["id"] and not active:
            errors.append("You cannot disable your own account.")

        if errors:
            for error in errors:
                flash(error, "danger")
        else:
            get_db().execute(
                """
                UPDATE users
                SET role = ?, client_name = ?, active = ?
                WHERE id = ?
                """,
                (role, client_name if role == "client" else None, active, user_id),
            )
            get_db().commit()
            flash("User updated.", "success")
            return redirect(url_for("users_index"))

    return render_template("user_form.html", user=user, roles=USER_ROLES)


@app.route("/users/<int:user_id>/reset-password", methods=("GET", "POST"))
@admin_required
def reset_user_password(user_id):
    user = get_user(user_id)
    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if len(new_password) < 6:
            flash("Password must be at least 6 characters.", "danger")
        elif new_password != confirm_password:
            flash("Password and confirmation do not match.", "danger")
        else:
            get_db().execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), user_id),
            )
            get_db().commit()
            flash(f"Password reset for {user['username']}.", "success")
            return redirect(url_for("users_index"))

    return render_template("reset_password.html", user=user)


@app.route("/dashboard")
@login_required
def dashboard():
    if g.user["role"] == "client":
        projects = get_db().execute(
            """
            SELECT * FROM projects
            WHERE lower(client_name) = lower(?)
            ORDER BY updated_at DESC, id DESC
            """,
            (g.user["client_name"] or "",),
        ).fetchall()
    else:
        projects = get_db().execute("SELECT * FROM projects ORDER BY updated_at DESC, id DESC").fetchall()
    return render_template("dashboard.html", projects=projects)


@app.route("/projects/new", methods=("GET", "POST"))
@pm_or_admin_required
def create_project():
    if request.method == "POST":
        data = collect_project_form()
        errors = validate_project(data)
        if errors:
            for error in errors:
                flash(error, "danger")
        else:
            try:
                cursor = get_db().execute(
                    """
                    INSERT INTO projects (
                        project_code, client_name, site_name, digital_g_contact, pm_name,
                        scope_of_work, todays_scope, delivery_date, site_inspection_date,
                        installation_dates, share_code, schedule_status, status, next_action, notes
                    )
                    VALUES (
                        :project_code, :client_name, :site_name, :digital_g_contact, :pm_name,
                        :scope_of_work, :todays_scope, :delivery_date, :site_inspection_date,
                        :installation_dates, :share_code, :schedule_status, :status, :next_action, :notes
                    )
                    """,
                    {**data, "share_code": generate_share_code()},
                )
                get_db().commit()
                flash("Project created.", "success")
                return redirect(url_for("project_detail", project_id=cursor.lastrowid))
            except sqlite3.IntegrityError:
                flash("Project code must be unique.", "danger")

    return render_template(
        "project_form.html",
        project=None,
        installation_date_values=[""],
        project_statuses=PROJECT_STATUSES,
        schedule_statuses=SCHEDULE_STATUSES,
    )


@app.route("/projects/<int:project_id>")
@login_required
def project_detail(project_id):
    project = get_accessible_project(project_id)
    incidents = get_db().execute(
        "SELECT * FROM incident_logs WHERE project_id = ? ORDER BY incident_date DESC, id DESC",
        (project_id,),
    ).fetchall()
    project_media = get_db().execute(
        "SELECT * FROM project_media WHERE project_id = ? ORDER BY created_at DESC, id DESC",
        (project_id,),
    ).fetchall()
    schedule_requests = get_db().execute(
        "SELECT * FROM schedule_requests WHERE project_id = ? ORDER BY created_at DESC, id DESC",
        (project_id,),
    ).fetchall()
    schedule_change_logs = get_db().execute(
        "SELECT * FROM schedule_change_logs WHERE project_id = ? ORDER BY created_at DESC, id DESC",
        (project_id,),
    ).fetchall()
    media_by_category = {category: [] for category in MEDIA_CATEGORIES}
    for item in project_media:
        media_by_category.setdefault(item["category"], []).append(item)

    return render_template(
        "project_detail.html",
        project=project,
        incidents=incidents,
        schedule_requests=schedule_requests,
        schedule_change_logs=schedule_change_logs,
        media_by_category=media_by_category,
        media_categories=MEDIA_CATEGORIES,
        installation_dates=split_installation_dates(project["installation_dates"]),
        can_view_dates=(g.user["role"] != "installer" or installer_can_view_dates(project)),
        share_url=url_for("shared_job_task", share_code=project["share_code"], _external=True),
        site_share_url=url_for("site_team_task", share_code=project["share_code"], _external=True),
    )


@app.route("/projects/<int:project_id>/edit", methods=("GET", "POST"))
@pm_or_admin_required
def edit_project(project_id):
    project = get_project(project_id)
    if request.method == "POST":
        data = collect_project_form()
        data["id"] = project_id
        errors = validate_project(data)
        schedule_change_reason = request.form.get("schedule_change_reason", "").strip()
        has_schedule_change = schedule_changed(project, data)
        if has_schedule_change and not schedule_change_reason:
            errors.append("Schedule change reason is required when installation dates or schedule status change.")
        if errors:
            for error in errors:
                flash(error, "danger")
        else:
            try:
                get_db().execute(
                    """
                    UPDATE projects
                    SET project_code = :project_code,
                        client_name = :client_name,
                        site_name = :site_name,
                        digital_g_contact = :digital_g_contact,
                        pm_name = :pm_name,
                        scope_of_work = :scope_of_work,
                        todays_scope = :todays_scope,
                        delivery_date = :delivery_date,
                        site_inspection_date = :site_inspection_date,
                        installation_dates = :installation_dates,
                        schedule_status = :schedule_status,
                        status = :status,
                        next_action = :next_action,
                        notes = :notes,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                    """,
                    data,
                )
                if has_schedule_change:
                    add_schedule_change_log(
                        project_id,
                        project["installation_dates"],
                        data["installation_dates"],
                        project["schedule_status"],
                        data["schedule_status"],
                        schedule_change_reason,
                        g.user["username"],
                    )
                get_db().commit()
                flash("Project updated.", "success")
                return redirect(url_for("project_detail", project_id=project_id))
            except sqlite3.IntegrityError:
                flash("Project code must be unique.", "danger")

    return render_template(
        "project_form.html",
        project=project,
        installation_date_values=split_installation_dates(project["installation_dates"]) or [""],
        project_statuses=PROJECT_STATUSES,
        schedule_statuses=SCHEDULE_STATUSES,
    )


@app.route("/projects/<int:project_id>/brief")
@login_required
def brief(project_id):
    project = get_accessible_project(project_id)
    can_include_dates = project["schedule_status"] == "Confirmed"
    dates = split_installation_dates(project["installation_dates"])
    if can_include_dates:
        english_dates = ", ".join(dates) if dates else "Confirmed, dates not entered."
        chinese_dates = ", ".join(dates) if dates else "已確認，但尚未輸入日期。"
    else:
        english_dates = "Installation schedule to be confirmed by PM. Do not promise any date on-site."
        chinese_dates = "安裝時間表尚待 PM 確認。現場不可承諾任何日期。"

    message_lines = [
        "Pre-work Brief",
        f"Project: {project['project_code']}",
        f"Site: {project['site_name']}",
        "Today's Scope:",
        project["todays_scope"] or "To be advised by PM.",
        "Installation Dates:",
        english_dates,
        "Important Reminder:",
        "No schedule commitment should be made on-site. Any schedule questions must be referred to PM.",
        f"PM Contact: {project['pm_name']}",
        "",
        "開工前簡報",
        f"項目: {project['project_code']}",
        f"地點: {project['site_name']}",
        "今日工作範圍:",
        project["todays_scope"] or "由 PM 通知。",
        "安裝日期:",
        chinese_dates,
        "重要提醒:",
        "現場不可直接承諾任何安裝日期。如客戶或現場查詢時間表，請一律轉交 PM 確認。",
        f"PM 聯絡人: {project['pm_name']}",
    ]
    return render_template(
        "brief.html",
        project=project,
        message="\n".join(message_lines),
        installation_dates=split_installation_dates(project["installation_dates"]),
        can_include_dates=can_include_dates,
    )


@app.route("/job/<share_code>")
def shared_job_task(share_code):
    project = get_project_by_share_code(share_code)
    can_view_dates = project["schedule_status"] == "Confirmed"
    incidents = get_db().execute(
        "SELECT * FROM incident_logs WHERE project_id = ? ORDER BY incident_date DESC, id DESC",
        (project["id"],),
    ).fetchall()
    return render_template(
        "shared_job.html",
        project=project,
        incidents=incidents,
        installation_dates=split_installation_dates(project["installation_dates"]),
        can_view_dates=can_view_dates,
    )


@app.route("/site/<share_code>")
def site_team_task(share_code):
    project = get_project_by_share_code(share_code)
    can_view_dates = project["schedule_status"] == "Confirmed"
    schedule_requests = get_db().execute(
        "SELECT * FROM schedule_requests WHERE project_id = ? ORDER BY created_at DESC, id DESC",
        (project["id"],),
    ).fetchall()
    return render_template(
        "site_team.html",
        project=project,
        schedule_requests=schedule_requests,
        installation_dates=split_installation_dates(project["installation_dates"]),
        can_view_dates=can_view_dates,
    )


@app.route("/site/<share_code>/schedule-request", methods=("POST",))
def site_team_schedule_request(share_code):
    project = get_project_by_share_code(share_code)
    requested_dates = collect_requested_dates()
    notes = request.form.get("request_notes", "").strip()
    requested_by = request.form.get("requested_by", "").strip() or "Site team"

    if not requested_dates:
        flash("Choose at least one requested installation date.", "danger")
        return redirect(url_for("site_team_task", share_code=share_code))

    get_db().execute(
        """
        INSERT INTO schedule_requests (project_id, requested_dates, notes, requested_by)
        VALUES (?, ?, ?, ?)
        """,
        (project["id"], requested_dates, notes, requested_by),
    )
    get_db().commit()
    flash("Installation date request sent to PM for review.", "success")
    return redirect(url_for("site_team_task", share_code=share_code))


@app.route("/projects/<int:project_id>/media", methods=("POST",))
@login_required
def upload_project_media(project_id):
    if g.user["role"] == "client":
        abort(403)
    get_project(project_id)
    category = request.form.get("category", "")
    if category not in MEDIA_CATEGORIES:
        flash("Choose a valid upload category.", "danger")
        return redirect(url_for("project_detail", project_id=project_id))

    try:
        upload = save_upload(request.files.get("media_file"), f"projects/{project_id}")
    except ValueError as error:
        flash(str(error), "danger")
        return redirect(url_for("project_detail", project_id=project_id))

    if upload is None:
        flash("Choose a photo or short video to upload.", "danger")
        return redirect(url_for("project_detail", project_id=project_id))

    get_db().execute(
        """
        INSERT INTO project_media (
            project_id, category, original_filename, stored_filename, media_type, uploaded_by
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            category,
            upload["original_filename"],
            upload["stored_filename"],
            upload["media_type"],
            g.user["username"],
        ),
    )
    get_db().commit()
    flash(f"{MEDIA_CATEGORIES[category]} media uploaded.", "success")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/schedule-request", methods=("POST",))
@login_required
def request_schedule_change(project_id):
    project = get_accessible_project(project_id)
    if g.user["role"] != "client":
        abort(403)

    requested_dates = collect_requested_dates()
    notes = request.form.get("request_notes", "").strip()
    if not requested_dates:
        flash("Choose at least one requested installation date.", "danger")
        return redirect(url_for("project_detail", project_id=project_id))

    get_db().execute(
        """
        INSERT INTO schedule_requests (project_id, requested_dates, notes, requested_by)
        VALUES (?, ?, ?, ?)
        """,
        (project["id"], requested_dates, notes, g.user["username"]),
    )
    get_db().commit()
    flash("Installation date request sent to PM for review.", "success")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/schedule-request/<int:request_id>/<action>", methods=("POST",))
@pm_or_admin_required
def review_schedule_request(project_id, request_id, action):
    project = get_project(project_id)
    schedule_request = get_db().execute(
        "SELECT * FROM schedule_requests WHERE id = ? AND project_id = ?",
        (request_id, project_id),
    ).fetchone()
    if schedule_request is None:
        abort(404)

    if action == "confirm":
        get_db().execute(
            """
            UPDATE projects
            SET installation_dates = ?,
                schedule_status = 'Confirmed',
                status = 'Confirmed',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (schedule_request["requested_dates"], project["id"]),
        )
        add_schedule_change_log(
            project["id"],
            project["installation_dates"],
            schedule_request["requested_dates"],
            project["schedule_status"],
            "Confirmed",
            f"Confirmed requested dates from {schedule_request['requested_by'] or 'client/site team'}."
            + (f" Notes: {schedule_request['notes']}" if schedule_request["notes"] else ""),
            g.user["username"],
        )
        new_status = "Confirmed by PM"
        flash("Client requested dates confirmed as official installation dates.", "success")
    elif action == "reject":
        new_status = "Rejected"
        flash("Client schedule request rejected.", "info")
    else:
        abort(404)

    get_db().execute(
        """
        UPDATE schedule_requests
        SET status = ?, reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ?
        WHERE id = ? AND project_id = ?
        """,
        (new_status, g.user["username"], request_id, project_id),
    )
    get_db().commit()
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/schedule-request/<int:request_id>/delete", methods=("POST",))
@admin_required
def delete_schedule_request(project_id, request_id):
    get_project(project_id)
    result = get_db().execute(
        "DELETE FROM schedule_requests WHERE id = ? AND project_id = ?",
        (request_id, project_id),
    )
    get_db().commit()
    if result.rowcount:
        flash("Schedule request deleted.", "success")
    else:
        flash("Schedule request was not found.", "danger")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/schedule-change-log/<int:log_id>/delete", methods=("POST",))
@admin_required
def delete_schedule_change_log(project_id, log_id):
    get_project(project_id)
    result = get_db().execute(
        "DELETE FROM schedule_change_logs WHERE id = ? AND project_id = ?",
        (log_id, project_id),
    )
    get_db().commit()
    if result.rowcount:
        flash("Schedule change log deleted.", "success")
    else:
        flash("Schedule change log was not found.", "danger")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/incidents/new", methods=("GET", "POST"))
@pm_or_admin_required
def create_incident(project_id):
    project = get_project(project_id)
    if request.method == "POST":
        follow_up_status = request.form.get("follow_up_status", "Pending")
        if follow_up_status not in FOLLOW_UP_STATUSES:
            follow_up_status = "Pending"

        data = {
            "project_id": project_id,
            "incident_date": request.form.get("incident_date", "").strip(),
            "issue": request.form.get("issue", "").strip(),
            "root_cause": request.form.get("root_cause", "").strip(),
            "action_taken": request.form.get("action_taken", "").strip(),
            "responsible_person": request.form.get("responsible_person", "").strip(),
            "follow_up_status": follow_up_status,
        }

        if not data["incident_date"] or not data["issue"]:
            flash("Date and issue are required.", "danger")
        else:
            try:
                upload = save_upload(request.files.get("attachment"), f"incidents/{project_id}")
            except ValueError as error:
                flash(str(error), "danger")
                return render_template(
                    "incident_form.html",
                    project=project,
                    incident=None,
                    follow_up_statuses=FOLLOW_UP_STATUSES,
                )

            get_db().execute(
                """
                INSERT INTO incident_logs (
                    project_id, incident_date, issue, root_cause, action_taken,
                    responsible_person, follow_up_status, attachment_original_filename,
                    attachment_filename, attachment_media_type
                )
                VALUES (
                    :project_id, :incident_date, :issue, :root_cause, :action_taken,
                    :responsible_person, :follow_up_status, :attachment_original_filename,
                    :attachment_filename, :attachment_media_type
                )
                """,
                {
                    **data,
                    "attachment_original_filename": upload["original_filename"] if upload else None,
                    "attachment_filename": upload["stored_filename"] if upload else None,
                    "attachment_media_type": upload["media_type"] if upload else None,
                },
            )
            get_db().commit()
            flash("Incident added.", "success")
            return redirect(url_for("project_detail", project_id=project_id))

    return render_template(
        "incident_form.html",
        project=project,
        incident=None,
        follow_up_statuses=FOLLOW_UP_STATUSES,
    )


@app.route("/projects/<int:project_id>/incidents/<int:incident_id>/edit", methods=("GET", "POST"))
@pm_or_admin_required
def edit_incident(project_id, incident_id):
    project = get_project(project_id)
    incident = get_incident(project_id, incident_id)

    if request.method == "POST":
        follow_up_status = request.form.get("follow_up_status", "Pending")
        if follow_up_status not in FOLLOW_UP_STATUSES:
            follow_up_status = "Pending"

        data = {
            "id": incident_id,
            "project_id": project_id,
            "incident_date": request.form.get("incident_date", "").strip(),
            "issue": request.form.get("issue", "").strip(),
            "root_cause": request.form.get("root_cause", "").strip(),
            "action_taken": request.form.get("action_taken", "").strip(),
            "responsible_person": request.form.get("responsible_person", "").strip(),
            "follow_up_status": follow_up_status,
        }

        if not data["incident_date"] or not data["issue"]:
            flash("Date and issue are required.", "danger")
        else:
            update_data = data
            try:
                upload = save_upload(request.files.get("attachment"), f"incidents/{project_id}")
            except ValueError as error:
                flash(str(error), "danger")
                return render_template(
                    "incident_form.html",
                    project=project,
                    incident=incident,
                    follow_up_statuses=FOLLOW_UP_STATUSES,
                )

            attachment_sql = ""
            if upload:
                update_data = {
                    **data,
                    "attachment_original_filename": upload["original_filename"],
                    "attachment_filename": upload["stored_filename"],
                    "attachment_media_type": upload["media_type"],
                }
                attachment_sql = """,
                    attachment_original_filename = :attachment_original_filename,
                    attachment_filename = :attachment_filename,
                    attachment_media_type = :attachment_media_type"""

            get_db().execute(
                f"""
                UPDATE incident_logs
                SET incident_date = :incident_date,
                    issue = :issue,
                    root_cause = :root_cause,
                    action_taken = :action_taken,
                    responsible_person = :responsible_person,
                    follow_up_status = :follow_up_status
                    {attachment_sql}
                WHERE id = :id AND project_id = :project_id
                """,
                update_data,
            )
            get_db().commit()
            flash("Incident updated.", "success")
            return redirect(url_for("project_detail", project_id=project_id))

    return render_template(
        "incident_form.html",
        project=project,
        incident=incident,
        follow_up_statuses=FOLLOW_UP_STATUSES,
    )


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", message="You do not have permission to do that."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", message="That record was not found."), 404


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True, port=8083)  # or any port you prefer

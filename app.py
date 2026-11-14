
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from functools import wraps
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv, dotenv_values
from flask import (
    Flask, jsonify, request, send_file, send_from_directory,
    redirect, session, render_template
)
from PIL import Image, ImageOps
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas
from werkzeug.utils import secure_filename

from cloudflare_storage import (
    D1Connection,
    D1Error,
    storage_upload,
    storage_bytes,
    storage_delete,
    log_admin_activity,
)
import qrcode


# ---------------------------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_FILE, override=True)

DEFAULT_ADMIN_PASSWORD = ""

PUBLIC_DIR = BASE_DIR / "public"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"

if os.environ.get("VERCEL"):
    DATA_DIR = Path("/tmp/bcit-data")
    UPLOAD_DIR = Path("/tmp/bcit-uploads")

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(
    __name__,
    static_folder=str(PUBLIC_DIR),
    static_url_path=""
)

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 + 100_000
app.config["JSON_AS_ASCII"] = False

_secret_key_file = DATA_DIR / ".flask_secret_key"

if os.environ.get("SECRET_KEY"):
    _secret_key = os.environ["SECRET_KEY"]
else:
    if not _secret_key_file.exists():
        _secret_key_file.write_text(
            secrets.token_urlsafe(48),
            encoding="utf-8"
        )
        try:
            os.chmod(_secret_key_file, 0o600)
        except OSError:
            pass

    _secret_key = _secret_key_file.read_text(
        encoding="utf-8"
    ).strip()

app.secret_key = _secret_key


@app.before_request
def _bcit_mark_request_start():
    request.environ["bcit_request_started_at"] = time.perf_counter()


@app.after_request
def _bcit_response_headers(response):
    # Public assets may be cached; private API/student/payment responses are
    # deliberately not cached here.
    if request.method == "GET" and request.endpoint == "static" and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=3600, stale-while-revalidate=86400"

    started = request.environ.get("bcit_request_started_at")
    if started is not None:
        elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
        response.headers["X-BCIT-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
    return response
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("COOKIE_SECURE", "0").strip() == "1"
)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------

COURSES = [
    {
        "name": "ADCA",
        "fee": 8000,
        "duration": "12 Months",
        "topics": [
            "Computer Fundamentals and Operating Systems",
            "MS Word, Excel and PowerPoint",
            "Advanced Excel and Database Management",
            "Programming Fundamentals, HTML and CSS",
            "Accounting Software and Practical Projects",
        ],
    },
    {
        "name": "DCA",
        "fee": 4000,
        "duration": "6 Months",
        "topics": [
            "Computer Fundamentals and Windows",
            "MS Word, Excel and PowerPoint",
            "Internet, Email and Networking Basics",
            "Database Fundamentals",
            "Typing Practice and Practical Projects",
        ],
    },
    {
        "name": "DFA",
        "fee": 6000,
        "duration": "6 Months",
        "topics": [
            "Financial Accounting Fundamentals",
            "Journal, Ledger and Trial Balance",
            "Profit and Loss Account and Balance Sheet",
            "Tally, GST and Inventory Management",
            "Payroll and Excel Accounting Reports",
        ],
    },
    {
        "name": "PGDCA",
        "fee": 15000,
        "duration": "2 Years",
        "topics": [
            "Programming Concepts and Problem Solving",
            "C/C++ or Python Fundamentals",
            "Database Management and SQL",
            "Data Structures and Software Engineering",
            "Networking, Web Development and Project Work",
        ],
    },
    {
        "name": "DTP",
        "fee": 4000,
        "duration": "3 Months",
        "topics": [
            "Desktop Publishing Fundamentals",
            "Hindi and English Typing",
            "Page Layout and Document Formatting",
            "CorelDRAW and Photoshop Basics",
            "Poster, Banner, Brochure and Print Design",
        ],
    },
    {
        "name": "BCC",
        "fee": 2000,
        "duration": "2 Months",
        "topics": [
            "Computer Basics, Starting and Shutting Down",
            "Keyboard and Mouse Operations",
            "Notepad, WordPad and File Management",
            "Internet Search and YouTube Basics",
            "Basic Typing and File Saving",
        ],
    },
    {
        "name": "CCA",
        "fee": 8000,
        "duration": "6 Months",
        "topics": [
            "Input/Output Devices, CPU and Memory",
            "Windows OS, Files and Folders",
            "Paint, Notepad and File Management",
            "MS Word Letters and Basic Excel Tables",
            "Google Search and Email",
        ],
    },
    {
        "name": "CIC",
        "fee": 8000,
        "duration": "6 Months",
        "topics": [
            "History and Generations of Computers",
            "Information Technology Basics",
            "Windows and File Management",
            "MS Word and PowerPoint",
            "Internet Browsing and Social Media Safety",
        ],
    },
    {
        "name": "CIT",
        "fee": 6000,
        "duration": "6 Months",
        "topics": [
            "Information Technology Fundamentals",
            "Windows, Linux and DOS Basics",
            "Advanced MS Word, Excel and PowerPoint",
            "Web Browsing and Cyber Security",
            "Database Basics: MS Access or SQL",
        ],
    },
    {
        "name": "COA",
        "fee": 6000,
        "duration": "6 Months",
        "topics": [
            "Office Automation Concepts",
            "Word: Mail Merge, Letters and Official Notices",
            "Excel: Salary Sheets and Formulas",
            "PowerPoint Office Presentations",
            "Printer, Scanner and PDF Operations",
        ],
    },
    {
        "name": "CDOE",
        "fee": 3000,
        "duration": "3 Months",
        "topics": [
            "Hindi Typing: Kruti Dev and Mangal",
            "English Typing Speed and Accuracy",
            "Excel Data Entry and Record Management",
            "Sorting, Filtering and Data Validation",
            "Keyboard Shortcuts and Google Sheets",
        ],
    },
    {
        "name": "CCC",
        "fee": 12000,
        "duration": "2 Months",
        "topics": [
            "Computer and GUI-Based Operating Systems",
            "Word Processing, Spreadsheets and Presentations",
            "Internet, WWW, Email and Social Networking",
            "Digital Payments, UPI and E-Governance",
            "Cyber Security and Future Skills",
        ],
    },
]

COURSE_NAMES = {course["name"] for course in COURSES}


# ---------------------------------------------------------------------------
# Helpers and database
# ---------------------------------------------------------------------------

def get_upi_id():
    """Read institute UPI ID from environment or .env."""
    upi_id = (
        os.environ.get("UPI_ID") or ""
    ).strip().strip("\"' ")

    if not upi_id and ENV_FILE.exists():
        upi_id = str(
            dotenv_values(ENV_FILE).get("UPI_ID") or ""
        ).strip().strip("\"' ")

    return upi_id


def get_db():
    """Return the Cloudflare D1 database adapter."""
    return D1Connection()


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS enquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL,
                course TEXT,
                message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS admissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                father_name TEXT NOT NULL,
                mother_name TEXT NOT NULL,
                dob TEXT NOT NULL,
                address TEXT NOT NULL,
                mobile TEXT NOT NULL,
                email TEXT NOT NULL,
                course TEXT NOT NULL,
                photo TEXT,
                aadhaar TEXT,
                admission_date TEXT,
                gender TEXT,
                qualification TEXT,
                admission_fee INTEGER NOT NULL DEFAULT 500,
                payment_reference TEXT,
                payment_datetime TEXT,
                payment_status TEXT NOT NULL DEFAULT 'pending',
                status_token TEXT UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                receipt_token TEXT UNIQUE
            );

            CREATE TABLE IF NOT EXISTS file_metadata (
                object_key TEXT PRIMARY KEY,
                original_filename TEXT,
                content_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                category TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS admin_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_admin_activity_created
                ON admin_activity(created_at);
            """
        )

        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(admissions)"
            ).fetchall()
        }

        migrations = {
            "receipt_token": "TEXT",
            "aadhaar": "TEXT",
            "admission_date": "TEXT",
            "gender": "TEXT",
            "qualification": "TEXT",
            "admission_fee": "INTEGER NOT NULL DEFAULT 500",
            "payment_reference": "TEXT",
            "payment_datetime": "TEXT",
            "payment_status": "TEXT NOT NULL DEFAULT 'pending'",
            "status_token": "TEXT",
        }

        for column, definition in migrations.items():
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE admissions ADD COLUMN "
                    f"{column} {definition}"
                )

        # Profile lookup is a frequent public operation. This expression index
        # matches the exact mobile + DOB + lower(email) lookup used by the API.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admissions_profile_lookup "
            "ON admissions(mobile, dob, lower(email), id DESC)"
        )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_admissions_receipt_token "
            "ON admissions(receipt_token)"
        )


def clean(value, limit=500):
    return str(value or "").strip()[:limit]


def valid_phone(value):
    return bool(
        re.fullmatch(r"[0-9+\-\s()]{8,18}", value or "")
    )


def valid_email(value):
    return bool(
        re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value or "")
    )


def admin_required(fn):
    """Protect admin APIs with the dashboard login session."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = (
            os.environ.get("ADMIN_PASSWORD")
            or DEFAULT_ADMIN_PASSWORD
        ).strip()

        if not expected:
            return jsonify(
                error=(
                    "Admin password is not configured. "
                    "Set a strong ADMIN_PASSWORD in the private .env file."
                )
            ), 503

        if session.get("admin_authenticated") is not True:
            return jsonify(error="Admin login required."), 401

        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Home and public APIs
# ---------------------------------------------------------------------------

@app.get("/")
def home():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/api/health")
def health():
    try:
        with get_db() as conn:
            conn.execute("SELECT 1 AS connected").fetchone()

        return jsonify(
            ok=True,
            service="BCIT Flask API",
            database="D1 connected"
        )
    except Exception:
        app.logger.exception("D1 health check failed")
        return jsonify(
            ok=False,
            service="BCIT Flask API",
            database="unavailable"
        ), 503


@app.get("/api/courses")
def courses():
    return jsonify(COURSES)


@app.post("/api/enquiries")
def create_enquiry():
    data = request.get_json(silent=True) or {}

    name = clean(data.get("name"), 100)
    phone = clean(data.get("phone"), 30)
    email = clean(data.get("email"), 150)
    course = clean(data.get("course"), 30)
    message = clean(data.get("message"), 2000)

    if not name or not valid_phone(phone) or not valid_email(email):
        return jsonify(
            error=(
                "A valid name, phone number, and email address "
                "are required."
            )
        ), 400

    if course and course not in COURSE_NAMES:
        return jsonify(error="Please select a valid course."), 400

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO enquiries
            (name, phone, email, course, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, phone, email, course, message),
        )
        new_id = cur.lastrowid

    return jsonify(
        message="Your enquiry has been submitted successfully.",
        id=new_id,
    ), 201


# ---------------------------------------------------------------------------
# UPI QR
# ---------------------------------------------------------------------------

@app.get("/api/payment/qr")
def payment_qr():
    upi_id = get_upi_id()

    if (
        not upi_id
        or "your-upi-id" in upi_id.lower()
        or "example" in upi_id.lower()
    ):
        return jsonify(
            error=(
                "Institute UPI_ID is not configured. "
                "Add the real UPI ID to .env."
            )
        ), 503

    params = urllib.parse.urlencode({
        "pa": upi_id,
        "pn": "Bajrang Computer Institute of Technology",
        "am": "500.00",
        "cu": "INR",
        "tn": "BCIT Admission Fee",
    })

    uri = "upi://pay?" + params

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(uri)
    qr.make(fit=True)

    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    return send_file(buffer, mimetype="image/png", max_age=0)


# ---------------------------------------------------------------------------
# Admission form
# ---------------------------------------------------------------------------

@app.post("/api/admissions")
def create_admission():
    data = request.form

    student = clean(data.get("studentName"), 100)
    father = clean(data.get("fatherName"), 100)
    mother = clean(data.get("motherName"), 100)
    dob = clean(data.get("dob"), 20)
    admission_date = clean(data.get("admissionDate"), 20)
    aadhaar = re.sub(r"\s+", "", clean(data.get("aadhaar"), 12))
    gender = clean(data.get("gender"), 20)
    qualification = clean(data.get("qualification"), 30)
    address = clean(data.get("address"), 1000)
    mobile = clean(data.get("mobile"), 30)
    email = clean(data.get("email"), 150)
    course = clean(data.get("course"), 30)
    payment_reference = clean(data.get("paymentReference"), 100)
    payment_datetime = clean(data.get("paymentDateTime"), 40)
    photo = request.files.get("photo")

    valid_genders = {"None", "Male", "Female", "Transgender"}
    valid_qualifications = {
        "8th", "9th", "10th", "12th",
        "Under Graduate", "Post Graduate",
    }

    required = [
        student, father, mother, dob, admission_date,
        gender, qualification, address, payment_reference,
        payment_datetime, valid_phone(mobile), valid_email(email),
        course in COURSE_NAMES,
    ]

    if not all(required):
        return jsonify(
            error="Please complete all required fields with valid information."
        ), 400

    if gender not in valid_genders:
        return jsonify(error="Please select a valid gender."), 400

    if qualification not in valid_qualifications:
        return jsonify(error="Please select a valid qualification."), 400

    if aadhaar and not re.fullmatch(r"[0-9]{12}", aadhaar):
        return jsonify(
            error="Aadhaar must contain exactly 12 digits, or leave it blank."
        ), 400

    try:
        datetime.fromisoformat(payment_datetime)

        if date.fromisoformat(dob) > date.today():
            return jsonify(
                error="Date of birth cannot be in the future."
            ), 400

        if date.fromisoformat(admission_date) > date.today():
            return jsonify(
                error="Date of admission cannot be in the future."
            ), 400

    except ValueError:
        return jsonify(
            error=(
                "Please enter valid date of birth, admission date, "
                "and payment date/time."
            )
        ), 400

    if not photo or not photo.filename:
        return jsonify(
            error="Please upload a passport-size student photo."
        ), 400

    ext = Path(secure_filename(photo.filename)).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS or photo.mimetype not in ALLOWED_MIMES:
        return jsonify(
            error="The photo must be in JPG, PNG, or WEBP format."
        ), 400

    if not get_upi_id():
        return jsonify(
            error="Institute UPI_ID is not configured on the server."
        ), 503

    photo_name = "admissions/" + secrets.token_hex(16) + ext
    status_token = secrets.token_urlsafe(32)
    receipt_token = secrets.token_urlsafe(32)

    storage_upload(photo, photo_name, category="admission-photo")

    try:
        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO admissions (
                    student_name, father_name, mother_name, dob,
                    address, mobile, email, course, photo, aadhaar,
                    admission_date, gender, qualification, admission_fee,
                    payment_reference, payment_datetime, payment_status,
                    status_token, receipt_token
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 500,
                    ?, ?, 'pending', ?, ?
                )
                """,
                (
                    student, father, mother, dob, address, mobile,
                    email, course, photo_name, aadhaar or None,
                    admission_date, gender, qualification,
                    payment_reference, payment_datetime,
                    status_token, receipt_token,
                ),
            )

            new_id = cur.lastrowid

            course_fee = {
                item["name"]: item["fee"] for item in COURSES
            }.get(course, 0)

            conn.execute(
                """
                UPDATE admissions
                SET total_course_fee=?, student_active=1, student_deleted=0
                WHERE id=?
                """,
                (course_fee, new_id),
            )

            conn.execute(
                """
                INSERT OR IGNORE INTO portal_students
                (admission_id, student_code, active, deleted, total_fee)
                VALUES (?, ?, 1, 0, ?)
                """,
                (new_id, str(new_id), course_fee),
            )

            conn.execute(
                """
                INSERT OR IGNORE INTO fee_payments
                (admission_id, amount, mode, status, reference,
                 payment_date, payment_datetime, receipt_token)
                VALUES (?, 500, 'upi', 'pending', ?, ?, ?, ?)
                """,
                (
                    new_id, payment_reference, admission_date,
                    payment_datetime, secrets.token_urlsafe(32),
                ),
            )

    except Exception:
        try:
            storage_delete(photo_name)
        except Exception:
            app.logger.exception(
                "Could not clean up failed admission photo upload"
            )
        raise

    return jsonify(
        message=(
            "Admission application received. "
            "Payment is NOT verified."
        ),
        id=new_id,
        payment_status="pending",
        status_token=status_token,
        receipt_url=f"/api/receipt/{receipt_token}.pdf",
    ), 201


# ---------------------------------------------------------------------------
# Admission payment status and verification
# ---------------------------------------------------------------------------

@app.get("/api/admissions/payment-status/<status_token>")
def admission_payment_status(status_token):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, payment_status, receipt_token
            FROM admissions WHERE status_token = ?
            """,
            (status_token,),
        ).fetchone()

    if not row:
        return jsonify(error="Application status link not found."), 404

    if not row["receipt_token"]:
        return jsonify(
            payment_status=row["payment_status"],
            message="Receipt is not available.",
        ), 200

    paid = row["payment_status"] == "paid"

    return jsonify(
        payment_status="paid" if paid else "pending",
        message=(
            "Payment verified successfully."
            if paid else
            "Application receipt is ready, but payment has NOT been verified."
        ),
        receipt_url=f"/api/receipt/{row['receipt_token']}.pdf",
    ), 200


@app.post("/api/admin/admissions/<int:admission_id>/verify-payment")
@admin_required
def verify_admission_payment(admission_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM admissions WHERE id = ?",
            (admission_id,),
        ).fetchone()

        if not row:
            return jsonify(error="Admission application not found."), 404

        conn.execute(
            """
            UPDATE admissions
            SET payment_status = 'paid'
            WHERE id = ?
            """,
            (admission_id,),
        )

        row = conn.execute(
            "SELECT receipt_token FROM admissions WHERE id = ?",
            (admission_id,),
        ).fetchone()

        token = row["receipt_token"]

        if not token:
            token = secrets.token_urlsafe(32)
            conn.execute(
                "UPDATE admissions SET receipt_token = ? WHERE id = ?",
                (token, admission_id),
            )

    return jsonify(
        message="Payment marked as verified.",
        id=admission_id,
        receipt_url=f"/api/receipt/{token}.pdf",
    ), 200


@app.post("/api/admin/admissions/<int:admission_id>/payment-status")
@admin_required
def update_admission_payment_status(admission_id):
    """Allow authenticated admins to set payment to pending or verified."""
    data = request.get_json(silent=True) or {}
    requested_status = str(data.get("status", "")).strip().lower()

    if requested_status not in {"pending", "verified"}:
        return jsonify(
            error="Status must be 'pending' or 'verified'."
        ), 400

    stored_status = (
        "paid" if requested_status == "verified" else "pending"
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, receipt_token FROM admissions WHERE id = ?",
            (admission_id,),
        ).fetchone()

        if not row:
            return jsonify(error="Admission application not found."), 404

        token = row["receipt_token"]

        if stored_status == "paid" and not token:
            token = secrets.token_urlsafe(32)
            conn.execute(
                """
                UPDATE admissions
                SET payment_status = ?, receipt_token = ?
                WHERE id = ?
                """,
                (stored_status, token, admission_id),
            )
        else:
            conn.execute(
                "UPDATE admissions SET payment_status = ? WHERE id = ?",
                (stored_status, admission_id),
            )

    return jsonify(
        message="Payment status updated.",
        id=admission_id,
        payment_status=stored_status,
        status="verified" if stored_status == "paid" else "pending",
        receipt_url=(
            f"/api/receipt/{token}.pdf"
            if stored_status == "paid" and token else None
        ),
    ), 200


# ---------------------------------------------------------------------------
# Student profile
# ---------------------------------------------------------------------------

@app.post("/api/student/profile")
def student_profile():
    data = request.get_json(silent=True) or {}

    mobile = clean(data.get("mobile"), 30)
    dob = clean(data.get("dob"), 20)
    email = clean(data.get("email"), 150).lower()

    if not mobile or not dob or not email:
        return jsonify(
            error="Enter mobile number, date of birth, and email address."
        ), 400

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, student_name, course, admission_date, created_at,
                   admission_fee, payment_reference, payment_datetime,
                   payment_status, receipt_token
            FROM admissions
            WHERE mobile = ? AND dob = ? AND lower(email) = ?
            ORDER BY id DESC
            """,
            (mobile, dob, email),
        ).fetchall()

        results = []

        for row in rows:
            token = row["receipt_token"]

            if not token:
                token = secrets.token_urlsafe(32)
                conn.execute(
                    "UPDATE admissions SET receipt_token = ? WHERE id = ?",
                    (token, row["id"]),
                )

            results.append({
                "id": row["id"],
                "student_name": row["student_name"],
                "course": row["course"],
                "admission_date": row["admission_date"],
                "created_at": row["created_at"],
                "fee": row["admission_fee"],
                "payment_reference": row["payment_reference"],
                "payment_datetime": row["payment_datetime"],
                "payment_status": row["payment_status"],
                "receipt_url": f"/api/receipt/{token}.pdf",
            })

    if not results:
        return jsonify(
            error=(
                "No admission records matched. Check your mobile, "
                "date of birth, and email."
            )
        ), 404

    return jsonify(
        student_name=results[0]["student_name"],
        admissions=results,
    ), 200


# ---------------------------------------------------------------------------
# Professional one-page A4 admission receipt
# ---------------------------------------------------------------------------

@app.get("/api/receipt/<token>.pdf")
def download_receipt(token):
    """Professional single-page A4 admission receipt."""

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM admissions WHERE receipt_token = ?",
            (token,)
        ).fetchone()

    if not row:
        return jsonify(error="Receipt not found."), 404

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    left = 35
    right = width - 35
    page_width = right - left

    navy = (0.07, 0.15, 0.29)
    red = (0.85, 0.05, 0.07)
    orange = (0.95, 0.39, 0.05)
    black = (0.12, 0.13, 0.15)
    grey = (0.35, 0.39, 0.45)
    light_grey = (0.84, 0.87, 0.91)
    pale_blue = (0.94, 0.96, 0.98)
    white = (1, 1, 1)
    green = (0.05, 0.46, 0.23)

    is_paid = (
        str(row["payment_status"] or "").strip().lower() == "paid"
    )

    def safe_value(value):
        if value is None or not str(value).strip():
            return "Not provided"
        return " ".join(str(value).replace("\r", " ").split())

    def env_value(name):
        value = os.environ.get(name, "").strip().strip("\"'")

        if not value and ENV_FILE.exists():
            value = str(
                dotenv_values(ENV_FILE).get(name) or ""
            ).strip().strip("\"'")

        return value or "Not configured"

    def text(x, y, value, size=8, bold=False, color=black):
        pdf.setFillColorRGB(*color)
        pdf.setFont(
            "Helvetica-Bold" if bold else "Helvetica",
            size
        )
        pdf.drawString(x, y, str(value))

    pdf.setStrokeColorRGB(*light_grey)
    pdf.setLineWidth(0.8)
    pdf.roundRect(
        22, 20, width - 44, height - 40,
        8, fill=0, stroke=1
    )

    # Circular BCIT logo
    logo_x = left + 28
    logo_y = height - 70

    pdf.setFillColorRGB(*navy)
    pdf.circle(logo_x, logo_y, 27, fill=1, stroke=0)

    pdf.setStrokeColorRGB(*white)
    pdf.setLineWidth(1)
    pdf.circle(logo_x, logo_y, 22.5, fill=0, stroke=1)

    pdf.setFillColorRGB(*white)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawCentredString(logo_x, logo_y + 3, "BCIT")

    pdf.setFillColorRGB(*red)
    pdf.setFont("Helvetica-Bold", 4.7)
    pdf.drawCentredString(
        logo_x, logo_y - 7, "COMPUTER INSTITUTE"
    )

    # Institute heading
    title_x = left + 66

    pdf.setFillColorRGB(*navy)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(
        title_x,
        height - 48,
        "BAJRANG COMPUTER INSTITUTE OF TECHNOLOGY"
    )

    pdf.setFillColorRGB(*grey)
    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.drawString(
        title_x,
        height - 65,
        "ADMISSION ACKNOWLEDGEMENT RECEIPT"
    )

    pdf.setFont("Helvetica", 8)
    pdf.drawString(
        title_x,
        height - 81,
        "Registration: Emax/en/92566"
    )

    pdf.setStrokeColorRGB(*orange)
    pdf.setLineWidth(2)
    pdf.line(left, height - 105, right, height - 105)

    # Receipt title and application ID
    title_y = height - 128

    pdf.setFillColorRGB(*navy)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(left + 2, title_y, "ADMISSION RECEIPT")

    pdf.setStrokeColorRGB(*navy)
    pdf.setLineWidth(0.9)
    pdf.roundRect(
        right - 143, title_y - 5, 143, 23,
        4, fill=0, stroke=1
    )

    pdf.setFillColorRGB(*navy)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawCentredString(
        right - 71.5,
        title_y + 3,
        f"APPLICATION ID: {row['id']}"
    )

    # Student photograph
    photo_width = 64
    photo_height = 77
    photo_x = right - photo_width
    photo_y = height - 224

    photo_name = row["photo"] or ""

    if photo_name:
        try:
            photo_data = storage_bytes(photo_name)

            with Image.open(BytesIO(photo_data)) as im:
                im = ImageOps.fit(
                    im.convert("RGB"),
                    (320, 385),
                    method=Image.Resampling.LANCZOS
                )

                photo_buffer = BytesIO()
                im.save(photo_buffer, format="JPEG", quality=90)
                photo_buffer.seek(0)

                pdf.setStrokeColorRGB(*orange)
                pdf.setLineWidth(1)
                pdf.roundRect(
                    photo_x - 2, photo_y - 2,
                    photo_width + 4, photo_height + 4,
                    3, fill=0, stroke=1
                )

                pdf.drawImage(
                    ImageReader(photo_buffer),
                    photo_x, photo_y,
                    width=photo_width,
                    height=photo_height,
                    preserveAspectRatio=True,
                    mask="auto"
                )

        except Exception:
            app.logger.warning(
                "Could not render student photo on receipt.",
                exc_info=True
            )

    # Receipt information
    fee = row["admission_fee"]
    if fee is None:
        fee = 500

    aadhaar = str(row["aadhaar"] or "").strip()
    masked_aadhaar = (
        "XXXX-XXXX-" + aadhaar[-4:]
        if aadhaar else "Not provided"
    )

    fields = [
        ("Submission Date", row["created_at"]),
        ("Student Name", row["student_name"]),
        ("Father's Name", row["father_name"]),
        ("Mother's Name", row["mother_name"]),
        ("Date of Birth", row["dob"]),
        ("Date of Admission", row["admission_date"]),
        ("Gender", row["gender"]),
        ("Qualification", row["qualification"]),
        ("Aadhaar Number", masked_aadhaar),
        ("Course Applied For", row["course"]),
        ("Admission Fee", f"INR {fee}"),
        ("Mobile Number", row["mobile"]),
        ("Email Address", row["email"]),
        ("Address", row["address"]),
        ("Institute Account Holder", env_value("BANK_ACCOUNT_HOLDER")),
        ("Institute Bank Name", env_value("BANK_NAME")),
        ("Institute Account Number", env_value("BANK_ACCOUNT_NUMBER")),
        ("Institute IFSC", env_value("BANK_IFSC")),
        ("Institute UPI ID", get_upi_id() or "Not configured"),
        ("UTR / Payment Reference", row["payment_reference"]),
        ("Payment Date & Time", row["payment_datetime"]),
        (
            "Payment Status",
            "VERIFIED / PAID" if is_paid
            else "PENDING - NOT VERIFIED"
        ),
    ]

    # Two-column layout
    section_top = height - 244
    content_x = left + 10
    content_right = right - 10
    label_width = 155
    column_gap = 12
    value_x = content_x + label_width + column_gap
    value_width = content_right - value_x

    pdf.setFillColorRGB(*pale_blue)
    pdf.roundRect(
        left + 2, section_top - 17,
        page_width - 4, 20,
        4, fill=1, stroke=0
    )

    text(
        left + 10,
        section_top - 11,
        "APPLICATION & PAYMENT DETAILS",
        9,
        True,
        navy
    )

    y = section_top - 29
    row_gap = 3.0
    label_size = 8.0
    value_size = 8.4
    line_height = 10.5

    for label, raw_value in fields:
        value = safe_value(raw_value)

        important = label in {
            "Student Name",
            "Course Applied For",
            "Admission Fee",
            "Payment Status",
        }

        label_lines = simpleSplit(
            label.upper() + ":",
            "Helvetica-Bold",
            label_size,
            label_width
        ) or [label.upper() + ":"]

        value_font = "Helvetica-Bold" if important else "Helvetica"

        value_lines = simpleSplit(
            value,
            value_font,
            value_size,
            value_width
        ) or ["Not provided"]

        line_count = max(len(label_lines), len(value_lines))
        row_height = line_count * line_height + row_gap

        pdf.setStrokeColorRGB(*light_grey)
        pdf.setLineWidth(0.35)
        pdf.line(
            content_x,
            y - row_height + 1,
            content_right,
            y - row_height + 1
        )

        if label == "Payment Status":
            value_color = green if is_paid else red
        elif important:
            value_color = navy
        else:
            value_color = black

        for index, line in enumerate(label_lines):
            text(
                content_x,
                y - label_size - index * line_height,
                line,
                label_size,
                True,
                grey
            )

        for index, line in enumerate(value_lines):
            text(
                value_x,
                y - value_size - index * line_height,
                line,
                value_size,
                important,
                value_color
            )

        y -= row_height

    # Note and footer
    footer_line_y = 42

    note_lines = (
        [
            "IMPORTANT: Application submission does NOT mean payment is verified.",
            "Payment must be confirmed separately by the institute."
        ]
        if not is_paid
        else ["Keep this receipt for your records."]
    )

    note_size = 7
    note_line_height = 9
    note_y = min(y - 12, footer_line_y + 28)

    if note_y - (len(note_lines) - 1) * note_line_height < footer_line_y + 5:
        note_size = 6
        note_line_height = 7
        note_y = footer_line_y + 21

    pdf.setFillColorRGB(*(red if not is_paid else grey))
    pdf.setFont(
        "Helvetica-Bold" if not is_paid else "Helvetica",
        note_size
    )

    for line in note_lines:
        pdf.drawString(left + 3, note_y, line)
        note_y -= note_line_height

    pdf.setStrokeColorRGB(*orange)
    pdf.setLineWidth(1)
    pdf.line(left, footer_line_y, right, footer_line_y)

    text(
        left + 2,
        27,
        "Contact: +91 9708750305 | bcitcomputer.445@gmail.com",
        7,
        False,
        navy
    )

    pdf.setFont("Helvetica", 7)
    pdf.setFillColorRGB(*grey)
    pdf.drawRightString(
        right - 2, 27, "BCIT | Admission Receipt"
    )

    pdf.showPage()
    pdf.save()
    buffer.seek(0)

    response = send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"BCIT_Admission_Receipt_{row['id']}.pdf",
        max_age=0
    )

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )
    response.headers["Pragma"] = "no-cache"

    return response


# ---------------------------------------------------------------------------
# Gemini assistant
# ---------------------------------------------------------------------------

@app.post("/api/assistant")
def assistant_answer():
    data = request.get_json(silent=True) or {}
    question = clean(data.get("question"), 1500)

    if not question:
        return jsonify(error="Please enter a question."), 400

    courses = [
        ("ADCA", "Advanced Diploma in Computer Applications", 8000, "Duration :- 1 Year"),
        ("DCA", "Diploma in Computer Applications", 4000, "Duration :- 6 Months"),
        ("DFA", "Diploma in Financial Accounting", 6000, "Duration :- 6 Months"),
        ("PGDCA", "Post Graduate Diploma in Computer Applications", 15000, "Duration :- 2 Years"),
        ("DTP", "Diploma in Desktop Publishing", 4000, "Duration :- 3 Months"),
        ("BCC", "Basic Computer Course", 2000, "Duration :- 2 Months"),
        ("CCA", "Certificate in Computer Applications", 8000, "Duration :- 6 Months"),
        ("CIC", "Certificate in Information and Computer", 8000, "Duration :- 6 Months"),
        ("CIT", "Certificate in Information Technology", 6000, "Duration :- 6 Months"),
        ("COA", "Course in Office Automation", 6000, "Duration :- 6 Months"),
        ("CDOE", "Course in Data Entry and Office Education", 3000, "Duration :- 3 Months"),
        ("CCC", "Course on Computer Concepts", 12000, "Duration :- 2 Months"),
    ]

    q = question.lower()

    asks_courses = any(x in q for x in [
        "course", "courses", "कोर्स"
    ])

    asks_fees = any(x in q for x in [
        "fee", "fees", "फीस", "शुल्क", "price", "charges"
    ])

    asks_all = any(x in q for x in [
        "all", "every", "list", "complete",
        "sabhi", "saare", "sare", "sab",
        "puri", "पूरे", "सभी", "सारे",
        "all bcit", "total courses"
    ])

    if asks_courses and asks_fees and asks_all:
        lines = [
            "BAJRANG COMPUTER INSTITUTE OF TECHNOLOGY (BCIT)",
            "All Courses and Fees",
            ""
        ]

        for i, (code, name, fee, duration) in enumerate(courses, 1):
            lines.append(
                f"{i}. {code} - {name} - INR {fee:,} ({duration})"
            )

        lines.extend([
            "",
            "Contact: +91 9708750305",
            "Email: bcitcomputer.445@gmail.com",
            "Registration: Emax/en/92566"
        ])

        return jsonify(answer="\n".join(lines)), 200

    api_key = (
        os.environ.get("GEMINI_API_KEY", "")
        .strip().strip('"').strip("'")
    )

    if not api_key:
        return jsonify(
            error="Gemini AI is not configured. Check GEMINI_API_KEY in .env."
        ), 503

    model = (
        os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
        or "gemini-3.5-flash-lite"
    ).removeprefix("models/")

    course_information = "\n".join(
        f"{code} - {name}: INR {fee:,} ({duration})"
        for code, name, fee, duration in courses
    )

    system_prompt = f"""
You are the official AI Assistant of Bajrang Computer Institute
of Technology (BCIT).

Help students with courses, fees, admission, documents, syllabus,
computer topics and student support.

INSTITUTE ADDRESS AND LOCATION:
Institute Name: Bajrang Computer Institute of Technology (BCIT).
Official Address: West champaran Bihar Station Road Sathi Naumi Chowk.
Landmark: In front of Middle School Sathi.
City: Sathi.
When students ask about institute address, location, directions,
or how to reach the institute, provide the official address clearly.
Never provide a generic response when the official address is available.
Never invent or guess any address or landmark.

OFFICIAL COURSES AND FEES:
BCIT offers the following 12 official courses with exact fees:

1. BCC - Basic Computer Course - INR 2000.
2. CCA - Certificate in Computer Applications - INR 8000.
3. CIC - Certificate in Information and Computer - INR 8000.
4. CIT - Certificate in Information Technology - INR 6000.
5. COA - Course in Office Automation - INR 6000.
6. CDOE - Course in Data Entry and Office Education - INR 3000.
7. CCC - Course on Computer Concepts - INR 12000.
8. DCA - Diploma in Computer Applications - INR 4000.
9. ADCA - Advanced Diploma in Computer Applications - INR 8000.
10. PGDCA - Post Graduate Diploma in Computer Applications - INR 15000.
11. DFA - Diploma in Financial Accounting - INR 6000.
12. DTP - Diploma in Desktop Publishing - INR 4000.

IMPORTANT COURSE RULES:
Always use these exact 12 course names and fees.
Never use old or incorrect course fees.
When a student asks for all courses or course fees, provide
all 12 courses with their full names and exact fees.
When a student asks about one particular course, explain
its full name and correct fee.
COA and DTP are the official course codes. Never use CCOA or DTTP.

CAREER GUIDANCE AND FUTURE SCOPE:
When students ask about future scope, career opportunities,
job opportunities, employment or career options, give detailed
course-specific guidance.

Explain the future scope of all 12 courses separately
when the student asks about the future scope of BCIT courses.

BCC - Basic Computer Course: Explain basic computer operation,
computer operator, office assistant, basic data entry and
digital literacy-related opportunities.

CCA - Certificate in Computer Applications: Explain office
assistant, computer operator, documentation assistant,
data entry and computer application-related opportunities.

CIC - Certificate in Information and Computer: Explain
computer operations, IT support assistant, office computing
and information technology-related entry-level opportunities.

CIT - Certificate in Information Technology: Explain IT support
assistant, computer technician trainee, computer operator
and basic technical support opportunities.

COA - Course in Office Automation: Explain office assistant,
documentation operator, computer operator, spreadsheet operator
and administrative support opportunities.

CDOE - Course in Data Entry and Office Education: Explain
data entry operator, back-office assistant, documentation
assistant and office data management opportunities.

CCC - Course on Computer Concepts: Explain computer operator,
office assistant, digital services assistant and basic computer
application-related opportunities.

DCA - Diploma in Computer Applications: Explain computer
operator, office executive, data management assistant,
computer application assistant and IT support-related opportunities.

ADCA - Advanced Diploma in Computer Applications: Explain
advanced office work, computer application assistant,
IT support, documentation and computer training assistant
opportunities, depending on skills and qualifications.

PGDCA - Post Graduate Diploma in Computer Applications:
Explain advanced computer applications, software support,
database-related entry-level work, IT support and further
technical learning opportunities.

DFA - Diploma in Financial Accounting: Explain accounts assistant,
billing executive, junior accountant, Tally operator, GST
assistant and bookkeeping-related opportunities.

DTP - Diploma in Desktop Publishing: Explain DTP operator,
page layout assistant, graphic design trainee, printing press
operator and freelance design opportunities.

For each course, explain relevant skills, possible job roles,
private-sector opportunities, self-employment or freelancing
opportunities, and further learning or career growth.

Explain realistic career pathways with useful examples.
Do not guarantee jobs, placements, salaries or employment.
Do not claim that a certificate is government-recognized
unless officially verified. Government job eligibility depends
on the specific recruitment notification and required qualifications.

ADMISSION AND STUDENT SUPPORT:
Guide students about admission procedures, required documents,
fees, payment status, student profile and general institute support.
Never claim admission or payment is verified without confirmation.

CONTACT INFORMATION:
Phone: +91 9708750305.
Email: bcitcomputer.445@gmail.com.
Registration: Emax/en/92566.

IMPORTANT ACCURACY RULES:
Never invent course duration, eligibility, syllabus,
certificate recognition, placement, salary or job guarantees.
If information is unavailable, ask students to contact the institute.

ANSWER STYLE:
Reply in the same language as the student's question.
Use simple language, clear headings and useful bullet points.
For future scope questions, provide detailed answers instead
of short generic summaries.
For all-course questions, cover all 12 official courses.
Keep ordinary questions concise and helpful.

{course_information}

IMPORTANT RULES:
- Always use these exact 12 course names and fees.
- Never provide old or incorrect fees.
- COA and DTP are the official course codes.
- PGDCA fee is INR 15,000.
- If asked about all courses, provide all 12 courses and fees.
- Never invent duration, eligibility, syllabus or certificate recognition.
- Never claim admission or payment is verified without confirmation.
- Contact: +91 9708750305.
- Email: bcitcomputer.445@gmail.com.
- Registration: Emax/en/92566.
- Reply in the same language as the student.
- Give complete and helpful answers.
"""

    payload = json.dumps({
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": question}]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 3000
        }
    }).encode("utf-8")

    safe_model = urllib.parse.quote(model, safe=".-")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{safe_model}:generateContent"
    )

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))

        candidates = result.get("candidates", [])

        if not candidates:
            app.logger.warning(
                "Gemini returned no candidates: %s",
                str(result)[:1500]
            )
            return jsonify(
                error="AI could not generate an answer. Please try again."
            ), 502

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])

        answer_text = "\n".join(
            part.get("text", "")
            for part in parts
            if part.get("text") and not part.get("thought", False)
        ).strip()

        if not answer_text:
            return jsonify(
                error="AI returned an empty answer. Please try again."
            ), 502

        return jsonify(answer=answer_text), 200

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        app.logger.warning(
            "Gemini HTTP %s: %s", exc.code, detail[:1500]
        )

        if exc.code == 429:
            message = "AI is busy right now. Please try again shortly."
        elif exc.code in (500, 502, 503, 504):
            message = "AI server is temporarily busy. Please try again."
        elif exc.code == 404:
            message = "AI model was not found. Check GEMINI_MODEL in .env."
        else:
            message = f"Gemini API error (HTTP {exc.code})."

        return jsonify(error=message), 502

    except (urllib.error.URLError, TimeoutError):
        app.logger.warning("Gemini connection failed.", exc_info=True)
        return jsonify(
            error="AI response is taking too long. Please try again."
        ), 504

    except (KeyError, IndexError, ValueError):
        app.logger.warning(
            "Gemini response processing failed.",
            exc_info=True
        )
        return jsonify(
            error="AI response could not be processed. Please try again."
        ), 502


# ---------------------------------------------------------------------------
# Admin dashboard login and pages
# ---------------------------------------------------------------------------

@app.get("/admin")
def admin_dashboard_page():
    if not session.get("admin_authenticated"):
        return redirect("/admin/login")

    return render_template("admin.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    expected = (
        os.environ.get("ADMIN_PASSWORD")
        or DEFAULT_ADMIN_PASSWORD
    ).strip()

    if not expected:
        if request.method == "POST":
            return (
                "Admin password is not configured. "
                "Add ADMIN_PASSWORD to your .env file.",
                503
            )

        return render_template("admin-login.html")

    if request.method == "GET":
        if session.get("admin_authenticated"):
            return redirect("/admin")

        return render_template("admin-login.html")

    password = request.form.get("password", "")

    if not secrets.compare_digest(password, expected):
        return redirect("/admin/login?error=1")

    session.clear()
    session["admin_authenticated"] = True
    session.permanent = True

    return redirect("/admin")


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.get("/api/admin/enquiries")
@admin_required
def admin_enquiries():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM enquiries ORDER BY id DESC LIMIT 500"
        ).fetchall()

    return jsonify([dict(row) for row in rows])


# NEW ROUTE:
# This fixes DELETE /api/admin/enquiries returning HTTP 405.
# It requires the same authenticated admin session as the GET route.

@app.delete("/api/admin/enquiries")
@admin_required
def admin_delete_all_enquiries_route():
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM enquiries")

        return jsonify(
            success=True,
            message="All enquiries deleted successfully."
        ), 200

    except Exception:
        app.logger.exception("Failed to delete all enquiries")
        return jsonify(
            success=False,
            error="Could not delete enquiries. Please try again."
        ), 500


@app.get("/api/admin/admissions")
@admin_required
def admin_admissions():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM admissions ORDER BY id DESC LIMIT 500"
        ).fetchall()

    result = []

    for row in rows:
        item = dict(row)

        if item.get("aadhaar"):
            item["aadhaar"] = (
                "XXXX-XXXX-" + str(item["aadhaar"])[-4:]
            )

        item.pop("receipt_token", None)
        result.append(item)

    return jsonify(result)


# ---------------------------------------------------------------------------
# Error handlers and startup
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def too_large(_error):
    return jsonify(
        error=(
            "The uploaded file exceeds the allowed size. "
            "Photos must be under 2 MB; learning materials may be up to 50 MB."
        )
    ), 413


@app.errorhandler(D1Error)
def d1_error(_error):
    app.logger.exception("Cloudflare D1 request failed")
    return jsonify(
        error="Cloud database is temporarily unavailable. Please try again."
    ), 503


@app.errorhandler(500)
def server_error(_error):
    app.logger.exception("Unexpected server error")
    return jsonify(
        error="A server error occurred. Please try again."
    ), 500


# Initialize the existing public-site database tables.
init_db()


# Register portal extensions alongside the existing site routes.
# All structured records continue to use D1 and R2.
from portal_features import register_portal

register_portal(
    app,
    get_db_fn=get_db,
    storage_upload_fn=storage_upload,
    storage_bytes_fn=storage_bytes,
    storage_delete_fn=storage_delete,
    admin_activity_fn=log_admin_activity,
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )

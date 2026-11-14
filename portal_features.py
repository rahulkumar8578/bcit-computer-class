"""
BCIT Student/Admin Portal extensions.
Existing public pages and admission workflow are left intact.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import threading
import json
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from io import BytesIO
import csv
from flask import Blueprint, jsonify, request, session, redirect, render_template, send_file, Response, stream_with_context
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import urllib.parse
import urllib.request
import urllib.error
import qrcode

portal = Blueprint("portal", __name__)

DB_FACTORY = None
_STORAGE_UPLOAD = None
_STORAGE_BYTES = None
_STORAGE_DELETE = None
_ADMIN_ACTIVITY = None


def db():
    if DB_FACTORY is None:
        raise RuntimeError("Portal database is not configured. Start the application through app.py.")
    return DB_FACTORY()


def portal_object_key(filename):
    return "portal/" + Path(str(filename or "")).name


def delete_portal_file(filename):
    if filename and _STORAGE_DELETE:
        try:
            _STORAGE_DELETE(portal_object_key(filename))
        except Exception:
            # Storage cleanup must not hide the original application response.
            pass

# Server-Sent Events: notify connected student profiles immediately when
# learning content is added, edited, or deleted by an administrator.
_learning_updates = threading.Condition()
_learning_version = 0
def notify_learning_update():
    global _learning_version
    with _learning_updates:
        _learning_version += 1
        _learning_updates.notify_all()

@portal.before_request
def portal_upload_limit():
      # Allow multipart overhead while enforcing the actual file-size limit
    limits = {
        "portal.add_material": 20 * 1024 * 1024 + 256 * 1024,
        "portal.add_assignment": 5 * 1024 * 1024 + 256 * 1024,
        "portal.submit_assignment": 5 * 1024 * 1024 + 256 * 1024,
        "portal.update_assignment_question": 5 * 1024 * 1024 + 256 * 1024,
        "portal.student_fee_pay": 52 * 1024 * 1024,
    }
    if request.endpoint in limits:
        request.max_content_length = limits[request.endpoint]

BASE = Path(__file__).resolve().parent
PORTAL_ALLOWED = {".pdf", ".mp4", ".webm", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".doc", ".docx", ".zip"}

def init_portal_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS portal_students(
          admission_id INTEGER PRIMARY KEY REFERENCES admissions(id) ON DELETE CASCADE,
          student_code TEXT UNIQUE, reg_no TEXT UNIQUE, active INTEGER NOT NULL DEFAULT 1,
          deleted INTEGER NOT NULL DEFAULT 0, total_fee INTEGER NOT NULL DEFAULT 0,
          completion_date TEXT, password_hash TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fee_payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, admission_id INTEGER NOT NULL REFERENCES admissions(id),
          amount INTEGER NOT NULL CHECK(amount>0), mode TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', reference TEXT, payment_date TEXT NOT NULL,
          receipt_token TEXT UNIQUE NOT NULL, verified_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS attendance(
          id INTEGER PRIMARY KEY AUTOINCREMENT, admission_id INTEGER NOT NULL REFERENCES admissions(id),
          attendance_date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('present','absent')),
          UNIQUE(admission_id,attendance_date)
        );
        CREATE TABLE IF NOT EXISTS attendance_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS attendance_batch_students(
          batch_id INTEGER NOT NULL REFERENCES attendance_batches(id) ON DELETE CASCADE,
          admission_id INTEGER NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
          PRIMARY KEY(batch_id,admission_id)
        );
        CREATE TABLE IF NOT EXISTS exams(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_code TEXT UNIQUE, title TEXT NOT NULL, subject TEXT NOT NULL,
          exam_date TEXT, exam_type TEXT NOT NULL DEFAULT 'offline', max_marks REAL NOT NULL DEFAULT 100,
          pass_marks REAL NOT NULL DEFAULT 33, instructions TEXT NOT NULL DEFAULT '',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS exam_marks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          admission_id INTEGER NOT NULL REFERENCES admissions(id), marks REAL NOT NULL,
          UNIQUE(exam_id,admission_id)
        );
        CREATE TABLE IF NOT EXISTS online_exam_questions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          question_no INTEGER NOT NULL, question_text TEXT NOT NULL, options_json TEXT NOT NULL,
          correct_option INTEGER NOT NULL CHECK(correct_option BETWEEN 0 AND 3), marks REAL NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS online_exam_attempts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          admission_id INTEGER NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
          started_at TEXT NOT NULL, ends_at TEXT NOT NULL, submitted_at TEXT,
          answers_json TEXT NOT NULL DEFAULT '{}', marks REAL, total_marks REAL NOT NULL,
          status TEXT NOT NULL DEFAULT 'in_progress', UNIQUE(exam_id,admission_id)
        );
        CREATE TABLE IF NOT EXISTS online_exam_violations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          attempt_id INTEGER NOT NULL REFERENCES online_exam_attempts(id) ON DELETE CASCADE,
          exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          admission_id INTEGER NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
          violation_type TEXT NOT NULL,
          occurred_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_online_exam_violations_exam_time
          ON online_exam_violations(exam_id, occurred_at);
        CREATE TABLE IF NOT EXISTS materials(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT NOT NULL,
          file_name TEXT, external_url TEXT, course TEXT, admission_id INTEGER REFERENCES admissions(id), created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS assignments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT,
          file_name TEXT, course TEXT, due_date TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS submissions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id INTEGER NOT NULL REFERENCES assignments(id),
          admission_id INTEGER NOT NULL REFERENCES admissions(id), file_name TEXT NOT NULL,
          original_name TEXT, submitted_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(assignment_id,admission_id)
        );
        """)
        # Indexes for the most frequent student dashboard, attendance, fee,
        # materials, assignment and exam lookups. All are additive and safe to
        # create on existing D1 databases; no student rows are rewritten.
        c.executescript("""
        CREATE INDEX IF NOT EXISTS idx_fee_payments_admission_status_id
          ON fee_payments(admission_id, status, id DESC);
        CREATE INDEX IF NOT EXISTS idx_fee_payments_status_id
          ON fee_payments(status, id DESC);
        CREATE INDEX IF NOT EXISTS idx_exam_marks_admission_exam
          ON exam_marks(admission_id, exam_id);
        CREATE INDEX IF NOT EXISTS idx_online_attempts_admission_exam
          ON online_exam_attempts(admission_id, exam_id);
        CREATE INDEX IF NOT EXISTS idx_online_attempts_exam_status
          ON online_exam_attempts(exam_id, status);
        CREATE INDEX IF NOT EXISTS idx_submissions_admission_assignment
          ON submissions(admission_id, assignment_id);
        CREATE INDEX IF NOT EXISTS idx_materials_admission_course
          ON materials(admission_id, course);
        CREATE INDEX IF NOT EXISTS idx_assignments_course_id
          ON assignments(course, id DESC);
        CREATE INDEX IF NOT EXISTS idx_attendance_batch_students_admission
          ON attendance_batch_students(admission_id, batch_id);
        CREATE INDEX IF NOT EXISTS idx_online_violations_admission_time
          ON online_exam_violations(admission_id, occurred_at DESC);
        """)
        # Support the student online-exam list and ordered question fetches
        # without changing any existing records or API contracts.
        c.execute("CREATE INDEX IF NOT EXISTS idx_exams_type_lower_id ON exams(LOWER(exam_type), id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_online_exam_questions_exam_order ON online_exam_questions(exam_id, question_no)")
        pcols={r["name"] for r in c.execute("PRAGMA table_info(portal_students)")}
        if "student_code" not in pcols: c.execute("ALTER TABLE portal_students ADD COLUMN student_code TEXT")
        ecols={r["name"] for r in c.execute("PRAGMA table_info(exams)")}
        if "exam_code" not in ecols: c.execute("ALTER TABLE exams ADD COLUMN exam_code TEXT")
        if "duration_minutes" not in ecols: c.execute("ALTER TABLE exams ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 0")
        if "instructions" not in ecols: c.execute("ALTER TABLE exams ADD COLUMN instructions TEXT NOT NULL DEFAULT ''")
        subcols={r["name"] for r in c.execute("PRAGMA table_info(submissions)")}
        if "original_name" not in subcols: c.execute("ALTER TABLE submissions ADD COLUMN original_name TEXT")
        matcols={r["name"] for r in c.execute("PRAGMA table_info(materials)")}
        if "admission_id" not in matcols: c.execute("ALTER TABLE materials ADD COLUMN admission_id INTEGER REFERENCES admissions(id)")
        paycols={r["name"] for r in c.execute("PRAGMA table_info(fee_payments)")}
        if "proof_file" not in paycols: c.execute("ALTER TABLE fee_payments ADD COLUMN proof_file TEXT")
        if "payment_datetime" not in paycols: c.execute("ALTER TABLE fee_payments ADD COLUMN payment_datetime TEXT")
        cols={r["name"] for r in c.execute("PRAGMA table_info(admissions)")}
        additions={
          "reg_no":"TEXT","student_active":"INTEGER NOT NULL DEFAULT 1",
          "student_deleted":"INTEGER NOT NULL DEFAULT 0","student_password_hash":"TEXT",
          "total_course_fee":"INTEGER NOT NULL DEFAULT 0","completion_date":"TEXT"
        }
        for name,definition in additions.items():
            if name not in cols: c.execute(f"ALTER TABLE admissions ADD COLUMN {name} {definition}")
        # Add indexes only when existing D1 records satisfy uniqueness. Never
        # delete or rewrite conflicting records merely to force an index.
        duplicate_reg=c.execute("SELECT reg_no FROM admissions WHERE reg_no IS NOT NULL AND reg_no<>'' GROUP BY reg_no HAVING COUNT(*)>1 LIMIT 1").fetchone()
        if not duplicate_reg:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_admission_reg_no ON admissions(reg_no) WHERE reg_no IS NOT NULL AND reg_no<>''")
        duplicate_ref=c.execute("SELECT reference FROM fee_payments WHERE mode='upi' AND reference IS NOT NULL AND reference<>'' GROUP BY reference HAVING COUNT(*)>1 LIMIT 1").fetchone()
        if not duplicate_ref:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_online_reference ON fee_payments(reference) WHERE mode='upi' AND reference IS NOT NULL AND reference<>''")
        # Backfill existing D1 admissions in two set-based queries (not one
        # network request per student); this never imports or deletes local rows.
        c.execute("UPDATE portal_students SET student_code=CAST(admission_id AS TEXT) WHERE student_code IS NULL OR student_code=''")
        c.execute("""INSERT OR IGNORE INTO portal_students
            (admission_id,student_code,reg_no,active,deleted,total_fee,completion_date,password_hash)
            SELECT id,CAST(id AS TEXT),reg_no,COALESCE(student_active,1),COALESCE(student_deleted,0),
              CASE WHEN COALESCE(total_course_fee,0)>0 THEN total_course_fee
                   WHEN course='ADCA' THEN 8000 WHEN course='DCA' THEN 4000
                   WHEN course='DFA' THEN 6000 WHEN course='PGDCA' THEN 18000
                   WHEN course='DTTP' THEN 6000 ELSE 0 END,
              completion_date,student_password_hash FROM admissions""")
        duplicate_code=c.execute("SELECT student_code FROM portal_students WHERE student_code IS NOT NULL AND student_code<>'' GROUP BY student_code HAVING COUNT(*)>1 LIMIT 1").fetchone()
        if not duplicate_code:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_portal_student_code ON portal_students(student_code) WHERE student_code IS NOT NULL AND student_code<>''")

def admin_only(fn):
    @wraps(fn)
    def wrap(*a,**kw):
        if session.get("admin_authenticated") is not True: return jsonify(error="Admin login required."),401
        return fn(*a,**kw)
    return wrap

def student_only(fn):
    @wraps(fn)
    def wrap(*a,**kw):
        sid=session.get("student_admission_id")
        if not sid: return jsonify(error="Student login required."),401
        with db() as c:
            r=c.execute("""SELECT a.*,p.student_code,p.reg_no,p.active,p.deleted,p.total_fee,p.completion_date
                           FROM admissions a JOIN portal_students p ON p.admission_id=a.id
                           WHERE a.id=?""",(sid,)).fetchone()
        if not r or not r["active"] or r["deleted"]:
            session.pop("student_admission_id",None); return jsonify(error="Student account is inactive."),403
        request.student=dict(r)
        return fn(*a,**kw)
    return wrap

def log(action,detail=""):
    if _ADMIN_ACTIVITY:
        _ADMIN_ACTIVITY(action, str(detail)[:500])

def student_row(c,sid):
    return c.execute("""SELECT a.*,p.student_code,p.reg_no,p.active,p.deleted,p.total_fee,p.completion_date
                        FROM admissions a JOIN portal_students p ON p.admission_id=a.id WHERE a.id=?""",(sid,)).fetchone()

def resolve_student_id(c, value):
    raw=str(value or "").strip()
    if not raw: return None
    row=c.execute("SELECT admission_id FROM portal_students WHERE student_code=?",(raw,)).fetchone()
    if not row and raw.isdigit(): row=c.execute("SELECT admission_id FROM portal_students WHERE admission_id=?",(int(raw),)).fetchone()
    return row["admission_id"] if row else None

def attendance_score(c, sid):
    """Return attendance percentage using marked days only.

    Unmarked dates have no attendance row and are therefore excluded.
    Formula: present / (present + absent) * 100.
    """
    counts = c.execute(
        """SELECT COUNT(*) AS total_marked,
                  COALESCE(SUM(CASE WHEN LOWER(status)='present' THEN 1 ELSE 0 END), 0) AS present_days
           FROM attendance
           WHERE admission_id=? AND LOWER(status) IN ('present','absent')""",
        (sid,),
    ).fetchone()
    total_marked = int(counts["total_marked"] or 0)
    present_days = int(counts["present_days"] or 0)
    if total_marked == 0:
        return 0
    return round((present_days / total_marked) * 100, 2)

def save_upload(file, allowed=None, max_bytes=50 * 1024 * 1024):
    if not file or not file.filename:
        raise ValueError("Please choose a file.")

    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)

    if size > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        raise ValueError(f"File size must not exceed {max_mb} MB.")

    ext = Path(secure_filename(file.filename)).suffix.lower()
    if ext not in (allowed or PORTAL_ALLOWED):
        raise ValueError("Unsupported file type.")

    name = secrets.token_hex(16) + ext
    if not _STORAGE_UPLOAD:
        raise RuntimeError("Cloudflare R2 storage is not configured.")
    _STORAGE_UPLOAD(file, portal_object_key(name), category="portal-upload")
    return name
@portal.get("/student/login")
def student_login_page():
    return render_template("student-login.html")

@portal.post("/student/login")
def student_login():
    login=(request.form.get("student_id") or "").strip()
    password=request.form.get("password") or ""
    with db() as c:
        r=c.execute("""SELECT a.id,a.student_password_hash,p.student_code,p.active,p.deleted
                       FROM admissions a JOIN portal_students p ON p.admission_id=a.id
                       WHERE (CAST(a.id AS TEXT)=? OR p.student_code=?)""",(login,login)).fetchone()
    if not r or not r["active"] or r["deleted"] or not r["student_password_hash"] or not check_password_hash(r["student_password_hash"],password):
        return render_template("student-login.html",error="Invalid credentials or account inactive."),401
    session.clear(); session["student_admission_id"]=r["id"]; session.permanent=True
    return redirect("/?student_login=success#profile")


@portal.post("/api/student/login")
def student_login_api():
    d=request.get_json(silent=True) or {}
    login=(d.get("student_id") or "").strip(); password=d.get("password") or ""
    with db() as c:
        r=c.execute("SELECT a.id,a.student_password_hash,p.active,p.deleted FROM admissions a JOIN portal_students p ON p.admission_id=a.id WHERE (CAST(a.id AS TEXT)=? OR p.student_code=?)",(login,login)).fetchone()
    if not r or not r["active"] or r["deleted"] or not r["student_password_hash"] or not check_password_hash(r["student_password_hash"],password):
        return jsonify(error="Invalid credentials or account inactive."),401
    session.clear(); session["student_admission_id"]=r["id"]; session.permanent=True
    return jsonify(message="Login successful.")

@portal.get("/student/dashboard")
def student_dashboard():
    if not session.get("student_admission_id"): return redirect("/student/login")
    return render_template("student-dashboard.html")

@portal.post("/student/logout")
def student_logout():
    session.pop("student_admission_id",None); return redirect("/student/login")

@portal.get("/api/student/learning-updates")
@student_only
def student_learning_updates():
    @stream_with_context
    def events():
        # Start from the current version; only notify this browser about future changes.
        # This prevents a synthetic first event from reloading the dashboard at login.
        seen = _learning_version
        while True:
            with _learning_updates:
                if seen == _learning_version:
                    _learning_updates.wait(timeout=20)
                current = _learning_version
            if current != seen:
                seen = current
                yield f"data: {json.dumps({'type': 'learning-content-updated', 'version': current})}\n\n"
            else:
                yield ": keep-alive\n\n"
    return Response(events(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"
    })

@portal.get("/api/student/me")
@student_only
def student_me():
    s=request.student
    # These six dashboard reads are independent. Send them through one D1
    # batch call rather than paying six separate HTTPS round trips on every
    # dashboard refresh. Result ordering is identical to the query list.
    course_key = re.sub(r"[\s_-]+", "", str(s["course"] or "")).upper()
    with db() as c:
        payments_cur, attendance_totals_cur, marks_cur, materials_cur, assignments_cur, attendance_cur = c.execute_batch([
            ("SELECT id,amount,mode,status,payment_date,payment_datetime,receipt_token,reference FROM fee_payments WHERE admission_id=? ORDER BY id DESC", (s["id"],)),
            ("SELECT COUNT(*) total,SUM(status='present') present FROM attendance WHERE admission_id=?", (s["id"],)),
            ("""SELECT e.title,e.subject,e.exam_date,e.max_marks,e.pass_marks,m.marks,
                CASE WHEN m.marks>=e.pass_marks THEN 'PASS' ELSE 'FAIL' END result
                FROM exam_marks m JOIN exams e ON e.id=m.exam_id
                WHERE m.admission_id=? ORDER BY e.id DESC""", (s["id"],)),
            ("""SELECT * FROM materials
                WHERE (course IS NULL OR TRIM(course)='' OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(course),' ',''),'-',''),'_','')) IN ('ALL','ALLCOURSES','*')
                       OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(course),' ',''),'-',''),'_',''))=?)
                  AND (admission_id IS NULL OR admission_id=?) ORDER BY id DESC""", (course_key,s["id"])),
            ("""SELECT a.*,sub.id submission_id,sub.submitted_at,sub.original_name submission_file_name,sub.file_name submission_storage_name
                FROM assignments a LEFT JOIN submissions sub ON sub.assignment_id=a.id AND sub.admission_id=?
                WHERE (a.course IS NULL OR TRIM(a.course)='' OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(a.course),' ',''),'-',''),'_','')) IN ('ALL','ALLCOURSES','*')
                       OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(a.course),' ',''),'-',''),'_',''))=?)
                ORDER BY a.id DESC""", (s["id"],course_key)),
            ("SELECT attendance_date,status FROM attendance WHERE admission_id=? ORDER BY attendance_date DESC LIMIT 120", (s["id"],)),
        ])
        payments=[dict(x) for x in payments_cur.fetchall()]
        paid=sum(x["amount"] for x in payments if x["status"]=="verified")
        att=attendance_totals_cur.fetchone() or {"total":0,"present":0}
        marks=marks_cur.fetchall()
        mats=[dict(x) for x in materials_cur.fetchall()]
        ass=[dict(x) for x in assignments_cur.fetchall()]
        attendance=[dict(x) for x in attendance_cur.fetchall()]
        marked_days=int(att["total"] or 0)
        present_days=int(att["present"] or 0)
        att_score=round((present_days / marked_days) * 100, 2) if marked_days else 0
    total=int(s["total_fee"] or 0)
    pending=max(total-paid,0)
    response=jsonify(student={k:s[k] for k in ["id","student_code","student_name","father_name","mother_name","mobile","email","course","admission_date","completion_date","dob","gender","qualification","address"]},
      total_fee=pending,original_total_fee=total,paid_fee=paid,pending_fee=pending,payments=payments,
      attendance=attendance,attendance_total=att["total"] or 0,attendance_present=att["present"] or 0,
      attendance_percent=att_score,
      results=[dict(x) for x in marks],materials=mats,assignments=ass)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

@portal.get("/portal-files/<path:name>")
def portal_file(name):
    # Downloads are authorized by session; student can only access files assigned to their course.
    safe=Path(name).name
    if session.get("admin_authenticated"):
        try:
            return send_file(BytesIO(_STORAGE_BYTES(portal_object_key(safe))), download_name=safe, as_attachment=False)
        except FileNotFoundError:
            return "Not found",404
    sid=session.get("student_admission_id")
    if not sid: return redirect("/student/login")
    with db() as c:
        allowed=c.execute("""SELECT m.file_name FROM materials m JOIN portal_students p ON p.admission_id=? JOIN admissions a ON a.id=p.admission_id WHERE m.file_name=? AND (m.course IS NULL OR TRIM(m.course)='' OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(m.course),' ',''),'-',''),'_','')) IN ('ALL','ALLCOURSES','*') OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(m.course),' ',''),'-',''),'_',''))=UPPER(REPLACE(REPLACE(REPLACE(TRIM(a.course),' ',''),'-',''),'_',''))) AND (m.admission_id IS NULL OR m.admission_id=?)
                         UNION SELECT x.file_name FROM assignments x JOIN portal_students p ON p.admission_id=? JOIN admissions a ON a.id=p.admission_id WHERE x.file_name=? AND (x.course IS NULL OR TRIM(x.course)='' OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(x.course),' ',''),'-',''),'_','')) IN ('ALL','ALLCOURSES','*') OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(x.course),' ',''),'-',''),'_',''))=UPPER(REPLACE(REPLACE(REPLACE(TRIM(a.course),' ',''),'-',''),'_','')))
                         UNION SELECT file_name FROM submissions WHERE file_name=? AND admission_id=?""",(sid,safe,sid,sid,safe,safe,sid)).fetchone()
    if not allowed: return "Not found",404
    try:
        return send_file(BytesIO(_STORAGE_BYTES(portal_object_key(safe))), download_name=safe, as_attachment=False)
    except FileNotFoundError:
        return "Not found",404

@portal.post("/api/student/assignments/<int:aid>/submit")
@student_only
def submit_assignment(aid):
    try: filename=save_upload(request.files.get("file"),{".pdf",".doc",".docx",".zip",".jpg",".jpeg",".png"}, max_bytes=5 * 1024 * 1024)
    except ValueError as e: return jsonify(error=str(e)),400
    sid=request.student["id"]
    old_filename=None
    try:
        with db() as c:
            student_course=re.sub(r"[\s_-]+", "", str(request.student["course"] or "")).upper()
            exists=c.execute("""SELECT id,due_date FROM assignments WHERE id=? AND
                (course IS NULL OR TRIM(course)='' OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(course),' ',''),'-',''),'_','')) IN ('ALL','ALLCOURSES','*')
                 OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(course),' ',''),'-',''),'_',''))=?)""",(aid,student_course)).fetchone()
            if not exists: raise ValueError("Assignment not found.")
            if exists["due_date"]:
                deadline=exists["due_date"] if "T" in exists["due_date"] else exists["due_date"]+"T23:59"
                if datetime.now().isoformat(timespec="minutes") > deadline: raise ValueError("Assignment deadline has passed. Submission is closed.")
            original_name=secure_filename(request.files["file"].filename) or "assignment-submission"
            previous=c.execute("SELECT id,file_name FROM submissions WHERE assignment_id=? AND admission_id=?",(aid,sid)).fetchone()
            if previous:
                old_filename=previous["file_name"]
                # Keep exactly one submission record per student/assignment, but
                # allow the student to replace the uploaded file before deadline.
                c.execute("UPDATE submissions SET file_name=?,original_name=?,submitted_at=CURRENT_TIMESTAMP WHERE id=?",(filename,original_name,previous["id"]))
            else:
                c.execute("INSERT INTO submissions(assignment_id,admission_id,file_name,original_name) VALUES(?,?,?,?)",(aid,sid,filename,original_name))
        if old_filename and old_filename != filename:
            with db() as c:
                still_used=c.execute("SELECT 1 FROM submissions WHERE file_name=? UNION SELECT 1 FROM materials WHERE file_name=? UNION SELECT 1 FROM assignments WHERE file_name=?",(old_filename,old_filename,old_filename)).fetchone()
            if not still_used: delete_portal_file(old_filename)
    except sqlite3.IntegrityError:
        delete_portal_file(filename)
        return jsonify(error="Could not save the assignment submission. Please try again."),409
    except ValueError as e:
        delete_portal_file(filename); return jsonify(error=str(e)),404
    except Exception:
        delete_portal_file(filename)
        raise
    return jsonify(message="Assignment submitted successfully. You can replace your file any time before the deadline."),201

@portal.get("/api/student/attendance")
@student_only
def student_attendance():
    month=request.args.get("month","")
    with db() as c:
        rows=c.execute("SELECT attendance_date,status FROM attendance WHERE admission_id=? AND (?='' OR substr(attendance_date,1,7)=?) ORDER BY attendance_date DESC",(request.student["id"],month,month)).fetchall()
    return jsonify([dict(r) for r in rows])

@portal.get("/api/student/results.pdf")
@student_only
def student_results_pdf():
    with db() as c:
        offline_rows=c.execute("SELECT e.title,e.subject,e.exam_date,e.max_marks,e.pass_marks,m.marks,CASE WHEN m.marks>=e.pass_marks THEN 'PASS' ELSE 'FAIL' END result,'Offline' exam_mode FROM exam_marks m JOIN exams e ON e.id=m.exam_id WHERE m.admission_id=? ORDER BY e.id DESC",(request.student["id"],)).fetchall()
        online_rows=c.execute("SELECT e.title,e.subject,e.exam_date,t.total_marks max_marks,e.pass_marks,t.marks,CASE WHEN COALESCE(t.marks,0)>=e.pass_marks THEN 'PASS' ELSE 'FAIL' END result,'Online' exam_mode FROM online_exam_attempts t JOIN exams e ON e.id=t.exam_id WHERE t.admission_id=? AND t.status='submitted' ORDER BY t.id DESC",(request.student["id"],)).fetchall()
        rows=list(offline_rows)+list(online_rows)

    # Formal, university-style transcript layout.
    from reportlab.lib.pagesizes import A4, A5
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from datetime import datetime

    W,H=A4
    navy=colors.HexColor('#10264A')
    blue=colors.HexColor('#1E4F91')
    gold=colors.HexColor('#C69A3A')
    ink=colors.HexColor('#1E293B')
    muted=colors.HexColor('#64748B')
    pale=colors.HexColor('#F1F5F9')
    green=colors.HexColor('#16804A')
    red=colors.HexColor('#B42318')
    b=BytesIO(); p=canvas.Canvas(b,pagesize=A4)
    p.setTitle('BCIT Official Student Examination Statement')
    p.setAuthor('Bajrang Computer Institute of Technology')
    sid=request.student['student_code'] or request.student['id']
    name=request.student['student_name'] or 'Student'
    course=request.student['course'] or '—'
    total_obtained=sum(float(r['marks'] or 0) for r in rows)
    total_max=sum(float(r['max_marks'] or 0) for r in rows)
    percentage=(total_obtained/total_max*100) if total_max else 0
    passed=sum(1 for r in rows if r['result']=='PASS')
    failed=len(rows)-passed
    grade=('A+' if percentage>=90 else 'A' if percentage>=80 else 'B' if percentage>=70 else 'C' if percentage>=60 else 'D' if percentage>=50 else 'F') if rows else '—'

    def header(page_no):
        p.setFillColor(navy);p.rect(0,H-112,W,112,fill=1,stroke=0)
        p.setFillColor(gold);p.rect(0,H-116,W,4,fill=1,stroke=0)
        logo=BASE/'public'/'bcit-logo.jpeg'
        if logo.exists():
            try:p.drawImage(ImageReader(str(logo)),38,H-91,width=58,height=58,preserveAspectRatio=True,anchor='c',mask='auto')
            except Exception:pass
        p.setFillColor(colors.white);p.setFont('Helvetica-Bold',15)
        p.drawString(108,H-48,'BAJRANG COMPUTER INSTITUTE')
        p.drawString(108,H-68,'OF TECHNOLOGY')
        p.setFont('Helvetica',8);p.setFillColor(colors.HexColor('#D9E4F3'))
        p.drawString(108,H-86,'BCIT  |  STUDENT ACADEMIC RECORD')
        p.setFillColor(blue);p.roundRect(W-145,H-88,105,25,6,fill=1,stroke=0)
        p.setFillColor(colors.white);p.setFont('Helvetica-Bold',8);p.drawCentredString(W-92.5,H-79,'OFFICIAL SCORE CARD')
        p.setFillColor(ink);p.setFont('Helvetica-Bold',16);p.drawString(42,H-151,'STUDENT EXAMINATION RESULT')
        p.setFillColor(muted);p.setFont('Helvetica',8);p.drawRightString(W-42,H-149,f'Generated: {datetime.now().strftime("%d %b %Y")}')
        p.setStrokeColor(gold);p.setLineWidth(1.2);p.line(42,H-161,W-42,H-161)

    header(1)
    y=H-190
    # Student identity panel
    p.setFillColor(pale);p.roundRect(42,y-76,W-84,76,8,fill=1,stroke=0)
    p.setFillColor(blue);p.setFont('Helvetica-Bold',8);p.drawString(56,y-19,'CANDIDATE DETAILS')
    p.setFillColor(ink);p.setFont('Helvetica-Bold',11);p.drawString(56,y-39,str(name)[:55])
    p.setFont('Helvetica',9);p.setFillColor(muted)
    p.drawString(56,y-57,'Course: '+str(course)[:55])
    p.setFillColor(ink);p.setFont('Helvetica-Bold',9);p.drawString(W-205,y-39,'STUDENT ID')
    p.setFillColor(blue);p.setFont('Helvetica-Bold',12);p.drawString(W-205,y-58,str(sid)[:25])
    y-=105

    # Performance summary cards
    card_w=(W-100)/4
    metrics=[('EXAMS',str(len(rows))),('PASSED',str(passed)),('FAILED',str(failed)),('OVERALL',f'{percentage:.1f}%' if rows else '—')]
    for i,(label,value) in enumerate(metrics):
        x=42+i*(card_w+5)
        p.setFillColor(colors.white);p.setStrokeColor(colors.HexColor('#D8E0EA'));p.roundRect(x,y-54,card_w,54,7,fill=1,stroke=1)
        p.setFillColor(muted);p.setFont('Helvetica-Bold',7);p.drawString(x+9,y-17,label)
        p.setFillColor(green if label=='PASSED' else red if label=='FAILED' and failed else navy)
        p.setFont('Helvetica-Bold',15);p.drawString(x+9,y-39,value)
    y-=78

    p.setFillColor(ink);p.setFont('Helvetica-Bold',10);p.drawString(42,y,'EXAMINATION BREAKDOWN')
    y-=17
    # Table header
    cols=[42,68,235,315,375,435, W-42]
    p.setFillColor(navy);p.roundRect(42,y-25,W-84,25,4,fill=1,stroke=0)
    p.setFillColor(colors.white);p.setFont('Helvetica-Bold',7.5)
    labels=['S.NO.','EXAM / TITLE','SUBJECT','DATE','MARKS','STATUS']
    for i,label in enumerate(labels):p.drawString(cols[i]+6,y-16,label)
    y-=25
    if not rows:
        p.setFillColor(pale);p.roundRect(42,y-43,W-84,43,5,fill=1,stroke=0)
        p.setFillColor(muted);p.setFont('Helvetica',9);p.drawCentredString(W/2,y-25,'No examination results have been published yet.')
        y-=55
    else:
        for idx,r in enumerate(rows,1):
            if y<115:
                p.setFillColor(muted);p.setFont('Helvetica',7);p.drawString(42,42,'BCIT • Official student result statement')
                p.drawRightString(W-42,42,f'Page {p.getPageNumber()}')
                p.showPage();header(p.getPageNumber());y=H-190
                p.setFillColor(ink);p.setFont('Helvetica-Bold',10);p.drawString(42,y,'EXAMINATION BREAKDOWN (CONTINUED)');y-=17
                p.setFillColor(navy);p.roundRect(42,y-25,W-84,25,4,fill=1,stroke=0)
                p.setFillColor(colors.white);p.setFont('Helvetica-Bold',7.5)
                for i,label in enumerate(labels):p.drawString(cols[i]+6,y-16,label)
                y-=25
            row_h=34
            p.setFillColor(colors.white if idx%2 else pale);p.rect(42,y-row_h,W-84,row_h,fill=1,stroke=0)
            p.setFillColor(ink);p.setFont('Helvetica',7.5)
            title=str(r['title'] or '—') + ' (' + str(r['exam_mode'] or 'Offline') + ')';subject=str(r['subject'] or '—')
            p.drawString(cols[0]+7,y-20,str(idx))
            p.drawString(cols[1]+6,y-20,title[:34])
            p.drawString(cols[2]+6,y-20,subject[:19])
            p.drawString(cols[3]+6,y-20,str(r['exam_date'] or '—')[:12])
            p.drawString(cols[4]+6,y-20,f"{float(r['marks'] or 0):g}/{float(r['max_marks'] or 0):g}")
            p.setFillColor(green if r['result']=='PASS' else red);p.setFont('Helvetica-Bold',7.5)
            p.drawString(cols[5]+6,y-20,r['result'])
            y-=row_h
        y-=22
        if y<145:
            p.setFillColor(muted);p.setFont('Helvetica',7);p.drawString(42,42,'BCIT • Official student result statement')
            p.drawRightString(W-42,42,f'Page {p.getPageNumber()}');p.showPage();header(p.getPageNumber());y=H-190
        # Final aggregate block
        p.setFillColor(pale);p.roundRect(42,y-83,W-84,83,7,fill=1,stroke=0)
        p.setFillColor(navy);p.setFont('Helvetica-Bold',9);p.drawString(56,y-19,'OVERALL PERFORMANCE')
        p.setFillColor(ink);p.setFont('Helvetica',8)
        p.drawString(56,y-39,f'Total Marks: {total_obtained:g} / {total_max:g}')
        p.drawString(56,y-57,f'Overall Percentage: {percentage:.2f}%')
        p.drawString(56,y-73,f'Exams Cleared: {passed} of {len(rows)}')
        p.setFillColor(blue);p.roundRect(W-165,y-67,95,43,6,fill=1,stroke=0)
        p.setFillColor(colors.white);p.setFont('Helvetica-Bold',7);p.drawCentredString(W-117.5,y-39,'FINAL GRADE')
        p.setFont('Helvetica-Bold',16);p.drawCentredString(W-117.5,y-58,grade)
        y-=108

    # Authenticity note and signature spaces
    p.setStrokeColor(colors.HexColor('#D8E0EA'));p.line(42,83,W-42,83)
    p.setFillColor(muted);p.setFont('Helvetica-Oblique',7)
    p.drawString(42,68,'This is a computer-generated academic statement issued by BCIT.')
    p.drawString(42,56,'For any discrepancy, contact the institute office for verification.')
    p.setStrokeColor(ink);p.line(W-175,105,W-55,105)
    p.setFillColor(ink);p.setFont('Helvetica-Bold',8);p.drawCentredString(W-115,92,'AUTHORISED SIGNATORY')
    p.setFillColor(muted);p.setFont('Helvetica',7);p.drawCentredString(W-115,80,'BCIT Examination Cell')
    p.setFillColor(muted);p.setFont('Helvetica',7);p.drawString(42,35,'BAJRANG COMPUTER INSTITUTE OF TECHNOLOGY  •  STUDENT COPY')
    p.drawRightString(W-42,35,f'Page {p.getPageNumber()}')
    p.save();b.seek(0)
    return send_file(b,mimetype='application/pdf',as_attachment=True,download_name='BCIT-Official-Score-Card.pdf')

def _draw_fee_receipt_half(p, row, student, y0=0):
    """Draw one professional half-A4 landscape receipt in a 210 x 148.5 mm area."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    W, HALF = A4[0], A4[1] / 2
    navy=colors.HexColor('#12345A'); blue=colors.HexColor('#1E5AA8')
    pale=colors.HexColor('#EEF4FB'); line=colors.HexColor('#D6E0EC')
    muted=colors.HexColor('#5B6878'); ink=colors.HexColor('#182536')
    green=colors.HexColor('#16804A'); left=12*mm; right=W-12*mm
    p.saveState()
    # frame and header
    p.setStrokeColor(line); p.setLineWidth(.8)
    p.roundRect(left,y0+8*mm,right-left,HALF-16*mm,3*mm,stroke=1,fill=0)
    p.setFillColor(navy); p.roundRect(left+3*mm,y0+HALF-34*mm,right-left-6*mm,25*mm,2*mm,stroke=0,fill=1)
    p.setFillColor(colors.white); p.setFont('Helvetica-Bold',15)
    p.drawCentredString(W/2,y0+HALF-20*mm,'BAJRANG COMPUTER INSTITUTE OF TECHNOLOGY')
    p.setFillColor(colors.HexColor('#DCE8F6')); p.setFont('Helvetica',7)
    p.drawCentredString(W/2,y0+HALF-27*mm,'BCIT  |  OFFICIAL STUDENT FEE RECEIPT')
    p.setFillColor(blue); p.roundRect(left+3*mm,y0+HALF-43*mm,right-left-6*mm,7*mm,1.5*mm,stroke=0,fill=1)
    p.setFillColor(colors.white); p.setFont('Helvetica-Bold',8)
    p.drawCentredString(W/2,y0+HALF-40.5*mm,'FEE PAYMENT ACKNOWLEDGEMENT')
    # receipt number / issue date
    p.setFillColor(muted); p.setFont('Helvetica-Bold',6.5)
    p.drawString(left+4*mm,y0+HALF-52*mm,'RECEIPT NO.')
    p.drawString(W/2+4*mm,y0+HALF-52*mm,'ISSUE DATE')
    p.setFillColor(ink); p.setFont('Helvetica-Bold',8)
    p.drawString(left+4*mm,y0+HALF-58*mm,f"BCIT-FR-{row['id']:06d}")
    p.drawString(W/2+4*mm,y0+HALF-58*mm,str(row['verified_at'] or row['payment_date'] or '—')[:10])
    # student block
    block_y=y0+HALF-91*mm
    p.setFillColor(pale); p.roundRect(left+3*mm,block_y,right-left-6*mm,25*mm,2*mm,stroke=0,fill=1)
    p.setFillColor(navy); p.setFont('Helvetica-Bold',7)
    p.drawString(left+6*mm,block_y+18*mm,'STUDENT INFORMATION')
    fields=[('STUDENT NAME',student['student_name'] or '—',left+6*mm),('STUDENT ID',student['student_code'] or student['id'],left+82*mm),('COURSE / PROGRAMME',student['course'] or '—',left+142*mm)]
    for label,value,x in fields:
        p.setFillColor(muted); p.setFont('Helvetica-Bold',5.8); p.drawString(x,block_y+11*mm,label)
        p.setFillColor(ink); p.setFont('Helvetica-Bold',7); p.drawString(x,block_y+5*mm,str(value)[:38])
    # amount / transaction row
    table_y=y0+HALF-105*mm
    p.setFillColor(navy); p.roundRect(left+3*mm,table_y-7*mm,right-left-6*mm,7*mm,1*mm,stroke=0,fill=1)
    p.setFillColor(colors.white); p.setFont('Helvetica-Bold',6.5)
    p.drawString(left+6*mm,table_y-4.8*mm,'PAYMENT DESCRIPTION')
    p.drawRightString(right-6*mm,table_y-4.8*mm,'AMOUNT (INR)')
    p.setStrokeColor(line); p.rect(left+3*mm,table_y-16*mm,right-left-6*mm,9*mm,stroke=1,fill=0)
    p.setFillColor(ink); p.setFont('Helvetica',7)
    p.drawString(left+6*mm,table_y-12*mm,'Course fee payment')
    p.setFont('Helvetica-Bold',8); p.drawRightString(right-6*mm,table_y-12*mm,f"INR {float(row['amount']):,.2f}")
    # transaction metadata
    info_y=y0+HALF-126*mm
    info=[('MODE',str(row['mode']).upper()),('PAYMENT DATE',str(row['payment_datetime'] or row['payment_date'] or '—')[:19]),('UTR / REFERENCE',str(row['reference'] or '—')[:32])]
    x_positions=[left+5*mm,left+65*mm,left+130*mm]
    for (label,value),x in zip(info,x_positions):
        p.setFillColor(muted); p.setFont('Helvetica-Bold',5.7); p.drawString(x,info_y,label)
        p.setFillColor(ink); p.setFont('Helvetica-Bold',6.8); p.drawString(x,info_y-5*mm,value[:34])
    # verified strip and signature
    p.setFillColor(colors.HexColor('#EAF6EF')); p.roundRect(left+3*mm,y0+15*mm,right-left-6*mm,12*mm,1.5*mm,stroke=0,fill=1)
    p.setFillColor(green); p.setFont('Helvetica-Bold',7.5); p.drawString(left+6*mm,y0+22*mm,'PAYMENT STATUS: VERIFIED')
    p.setFillColor(ink); p.setFont('Helvetica-Bold',7); p.drawRightString(right-6*mm,y0+22*mm,f"AMOUNT RECEIVED: INR {float(row['amount']):,.2f}")
    p.setFillColor(muted); p.setFont('Helvetica',5.5); p.drawString(left+6*mm,y0+17.5*mm,'Computer-generated receipt · Valid without physical stamp')
    p.setStrokeColor(ink); p.line(right-48*mm,y0+14*mm,right-6*mm,y0+14*mm)
    p.setFillColor(ink); p.setFont('Helvetica-Bold',6); p.drawRightString(right-6*mm,y0+11*mm,'AUTHORISED SIGNATORY · BCIT ACCOUNTS OFFICE')
    p.restoreState()


def _build_fee_receipt_pdf(row, student, download_name):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    b=BytesIO(); half_size=(A4[0],A4[1]/2)
    p=canvas.Canvas(b,pagesize=half_size)
    _draw_fee_receipt_half(p,row,student,0)
    p.save(); b.seek(0)
    return send_file(b,mimetype='application/pdf',as_attachment=True,download_name=download_name)


def _build_two_fee_receipts_pdf(rows, students):
    """Print two different students' landscape half-A4 receipts on one A4 sheet."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    b=BytesIO(); p=canvas.Canvas(b,pagesize=A4); half=A4[1]/2
    _draw_fee_receipt_half(p,rows[0],students[0],half)
    _draw_fee_receipt_half(p,rows[1],students[1],0)
    p.setStrokeColorRGB(.65,.70,.78); p.setDash(3,3)
    p.line(10*mm,half,A4[0]-10*mm,half)
    p.save(); b.seek(0)
    return send_file(b,mimetype='application/pdf',as_attachment=True,download_name='BCIT-Two-Student-Fee-Receipts-A4.pdf')


@portal.get("/api/student/receipt/<token>.pdf")
@student_only
def student_receipt(token):
    with db() as c:
        r=c.execute("SELECT * FROM fee_payments WHERE receipt_token=? AND admission_id=? AND status='verified'",(token,request.student["id"])).fetchone()
    if not r:return jsonify(error="Receipt is available only after admin verifies the payment."),404
    return _build_fee_receipt_pdf(r, request.student, f"BCIT-fee-receipt-{r['id']}.pdf")

@portal.get("/api/admin/portal/students")
@admin_only
def admin_students():
    with db() as c: rows=c.execute("""SELECT a.id,a.student_name,a.father_name,a.mother_name,a.dob,a.gender,a.qualification,a.address,a.mobile,a.email,a.course,a.admission_date,a.completion_date,p.student_code,a.student_active,a.student_deleted,p.total_fee,a.student_password_hash,
      COALESCE((SELECT SUM(f.amount) FROM fee_payments f WHERE f.admission_id=a.id AND f.status='verified'),0) paid_fee
      FROM admissions a JOIN portal_students p ON p.admission_id=a.id ORDER BY a.id DESC""").fetchall()
    return jsonify([{**dict(r),"pending_fee":max((r["total_fee"] or 0)-r["paid_fee"],0),"has_password":bool(r["student_password_hash"])} for r in rows])

@portal.post("/api/admin/portal/students/<int:sid>")
@admin_only
def edit_student(sid):
    d=request.get_json(silent=True) or {}
    allowed=("student_name","father_name","mother_name","dob","gender","qualification","address","mobile","email","course","admission_date","completion_date")
    with db() as c:
        if not student_row(c,sid): return jsonify(error="Student not found."),404
        fields={k:str(d[k] or "").strip() for k in allowed if k in d}
        if "course" in fields and fields["course"] not in {"ADCA","DCA","DFA","PGDCA","DTTP","CIT","COA","BCC","CCA","CIC","CCOA","CDOE","CCC"}: return jsonify(error="Invalid course."),400
        if "student_code" in d:
            code=str(d["student_code"] if d["student_code"] is not None else "").strip()
            if not code or len(code)>64 or any(ord(ch)<32 for ch in code): return jsonify(error="Enter a Student ID (1 to 64 characters)."),400
            if c.execute("SELECT 1 FROM portal_students WHERE student_code=? AND admission_id<>?",(code,sid)).fetchone(): return jsonify(error="Student ID already exists."),409
            c.execute("UPDATE portal_students SET student_code=? WHERE admission_id=?",(code,sid))
        if "password" in d and d["password"]:
            if len(d["password"])<8:return jsonify(error="Password must be at least 8 characters."),400
            fields["student_password_hash"]=generate_password_hash(d["password"])
        if "total_fee" in d:
            try: total=max(0,int(d["total_fee"]))
            except: return jsonify(error="Invalid total fee."),400
            c.execute("UPDATE portal_students SET total_fee=? WHERE admission_id=?",(total,sid))
            c.execute("UPDATE admissions SET total_course_fee=? WHERE id=?",(total,sid))
        for key in ("student_active","student_deleted"):
            if key in d: fields[key]=1 if d[key] else 0
        if fields:
            c.execute("UPDATE admissions SET "+",".join(f"{k}=?" for k in fields)+" WHERE id=?",(*fields.values(),sid))
            sync={"active":fields.get("student_active"),"deleted":fields.get("student_deleted"),"completion_date":fields.get("completion_date"),"password_hash":fields.get("student_password_hash")}
            sync={k:v for k,v in sync.items() if v is not None}
            if sync:c.execute("UPDATE portal_students SET "+",".join(f"{k}=?" for k in sync)+" WHERE admission_id=?",(*sync.values(),sid))
    log("student_update",f"Student {sid}: {','.join(fields)}")
    return jsonify(message="Student details updated.")

@portal.post("/api/admin/portal/students")
@admin_only
def add_portal_student():
    d=request.get_json(silent=True) or {}
    code=str(d.get("student_code") or "").strip()
    name=str(d.get("student_name") or "").strip()
    course=str(d.get("course") or "").strip().upper()
    allowed_courses={"ADCA","DCA","DFA","PGDCA","DTTP","CIT","COA","BCC","CCA","CIC","CCOA","CDOE","CCC"}
    if not code or len(code)>64 or any(ord(ch)<32 for ch in code): return jsonify(error="Enter a Student ID (1 to 64 characters)."),400
    if not name:return jsonify(error="Student name is required."),400
    if course not in allowed_courses:return jsonify(error="Select a valid course."),400
    try: total=max(0,int(d.get("total_fee") or 0))
    except (TypeError,ValueError):return jsonify(error="Invalid total course fee."),400
    password=str(d.get("password") or "")
    if password and len(password)<8:return jsonify(error="Password must be at least 8 characters."),400
    today=date.today().isoformat()
    try:
        with db() as c:
            if c.execute("SELECT 1 FROM portal_students WHERE student_code=?",(code,)).fetchone():return jsonify(error="Student ID already exists."),409
            cur=c.execute("""INSERT INTO admissions(student_name,father_name,mother_name,dob,address,mobile,email,course,admission_date,gender,qualification,admission_fee,payment_status,student_active,student_deleted,total_course_fee,student_password_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,0,'verified',1,0,?,?)""",
                (name,str(d.get("father_name") or "").strip() or "-",str(d.get("mother_name") or "").strip() or "-",str(d.get("dob") or "").strip() or "-",str(d.get("address") or "").strip() or "-",str(d.get("mobile") or "").strip() or "-",str(d.get("email") or "").strip(),course,today,str(d.get("gender") or "").strip(),str(d.get("qualification") or "").strip(),total,generate_password_hash(password) if password else None))
            sid=cur.lastrowid
            c.execute("INSERT INTO portal_students(admission_id,student_code,active,deleted,total_fee,password_hash) VALUES(?,?,1,0,?,?)",(sid,code,total,generate_password_hash(password) if password else None))
    except sqlite3.IntegrityError:
        return jsonify(error="Student ID already exists or student data is invalid."),409
    log("student_add",f"Added student {sid} ({code})")
    return jsonify(message="Student added successfully.",student_id=sid),201


@portal.delete("/api/admin/portal/students/<int:sid>/permanent-delete")
@admin_only
def permanently_delete_portal_student(sid):
    try:
        with db() as c:
            student = c.execute(
                "SELECT id, photo FROM admissions WHERE id=?",
                (sid,)
            ).fetchone()

            if not student:
                return jsonify(error="Student not found."), 404

            # Delete related records first.
            child_tables = (
                "submissions",
                "exam_marks",
                "attendance_batch_students",
                "attendance",
                "fee_payments",
                "materials",
                "portal_students"
            )

            for table in child_tables:
                # These tables are part of the existing portal schema.
                c.execute(
                    f"DELETE FROM {table} WHERE admission_id=?",
                    (sid,)
                )

            # Delete the student.
            c.execute(
                "DELETE FROM admissions WHERE id=?",
                (sid,)
            )

            # Reset ID sequence only when no students remain.
            remaining = c.execute(
                "SELECT COUNT(*) AS total FROM admissions"
            ).fetchone()

            if remaining and int(remaining["total"]) == 0:
                c.execute(
                    "DELETE FROM sqlite_sequence WHERE name='admissions'"
                )

        photo = student["photo"]

        if photo and _STORAGE_DELETE:
            try:
                _STORAGE_DELETE(
                    photo if str(photo).startswith("admissions/")
                    else "admissions/" + Path(photo).name
                )
            except Exception:
                pass

        log(
            "student_permanent_delete",
            f"Permanently deleted student {sid}"
        )

        return jsonify(message="Student permanently deleted.")

    except Exception:
        return jsonify(
            error="Student deletion failed. Please try again."
        ), 500

@portal.post("/api/admin/portal/students/<int:sid>/restore")
@admin_only
def restore_student(sid):
    return edit_student_internal(sid,{"student_deleted":0,"student_active":1})

def edit_student_internal(sid,d):
    with db() as c:
        if not student_row(c,sid):return jsonify(error="Student not found."),404
        c.execute("UPDATE admissions SET student_deleted=?,student_active=? WHERE id=?",(d["student_deleted"],d["student_active"],sid))
        c.execute("UPDATE portal_students SET deleted=?,active=? WHERE admission_id=?",(d["student_deleted"],d["student_active"],sid))
    log("student_restore",f"Student {sid}")
    return jsonify(message="Student restored.")

@portal.post("/api/admin/portal/students/<sid>/fees")
@admin_only
def admin_add_payment(sid):
    d=request.get_json(silent=True) or {}
    try: amount=int(d.get("amount",0)); mode=str(d.get("mode","")).lower(); dt=date.fromisoformat(str(d.get("payment_date") or date.today().isoformat()))
    except: return jsonify(error="Enter valid amount and date."),400
    if amount<=0 or mode not in {"cash","upi","bank_transfer","cheque","other"}:return jsonify(error="Invalid amount or payment mode."),400
    reference=str(d.get("reference") or "").strip() or None
    if mode=="upi" and not reference:return jsonify(error="UPI transaction reference is required."),400
    token=secrets.token_urlsafe(32)
    status="verified" if mode!="upi" else "pending"
    try:
        with db() as c:
            sid=resolve_student_id(c,sid)
            if not sid:return jsonify(error="Student not found."),404
            total=c.execute("SELECT total_fee FROM portal_students WHERE admission_id=?",(sid,)).fetchone()["total_fee"] or 0
            paid=c.execute("SELECT COALESCE(SUM(amount),0) n FROM fee_payments WHERE admission_id=? AND status='verified'",(sid,)).fetchone()["n"]
            if amount>max(total-paid,0):return jsonify(error="The amount cannot exceed the remaining fee."),400
            cur=c.execute("INSERT INTO fee_payments(admission_id,amount,mode,status,reference,payment_date,receipt_token,verified_at) VALUES(?,?,?,?,?,?,?,?)",(sid,amount,mode,status,reference,dt.isoformat(),token,datetime.utcnow().isoformat() if status=="verified" else None))
    except sqlite3.IntegrityError:return jsonify(error="Duplicate payment reference rejected."),409
    log("fee_payment_entry",f"Student {sid}; amount {amount}; mode {mode}")
    return jsonify(message="Payment entry saved.",receipt_url=f"/api/admin/portal/receipt/{token}.pdf",status=status),201

@portal.get("/api/student/fees/qr")
@student_only
def student_fee_qr():
    try: amount=int(request.args.get("amount","0"))
    except: return jsonify(error="Invalid amount."),400
    with db() as c:
        total=int(request.student["total_fee"] or 0)
        paid=c.execute("SELECT COALESCE(SUM(amount),0) n FROM fee_payments WHERE admission_id=? AND status='verified'",(request.student["id"],)).fetchone()["n"]
    if amount<=0 or amount>total-paid:return jsonify(error="Amount must be within pending balance."),400
    upi=(os.environ.get("UPI_ID") or "").strip().strip("\\\"' ")
    if not upi:
        try:
            from dotenv import dotenv_values
            upi=str(dotenv_values(BASE/".env").get("UPI_ID") or "").strip()
        except: upi=""
    if not upi:return jsonify(error="Institute UPI_ID is not configured."),503
    uri="upi://pay?"+urllib.parse.urlencode({"pa":upi,"pn":"Bajrang Computer Institute of Technology","am":f"{amount:.2f}","cu":"INR","tn":f"BCIT Fee Student {request.student['id']}"})
    q=qrcode.QRCode(box_size=8,border=3);q.add_data(uri);q.make(fit=True);im=q.make_image(fill_color="black",back_color="white")
    b=BytesIO();im.save(b,format="PNG");b.seek(0)
    return send_file(b,mimetype="image/png",max_age=0)

@portal.post("/api/student/fees/pay")
@student_only
def student_fee_pay():
    if request.is_json: d=request.get_json(silent=True) or {}
    else: d=request.form
    try: amount=int(d.get("amount",0)); reference=str(d.get("reference"," ")).strip()
    except: return jsonify(error="Invalid payment details."),400
    raw_dt=str(d.get("payment_datetime") or "").strip()
    try:
        if not raw_dt: raise ValueError()
        parsed_dt=datetime.fromisoformat(raw_dt)
        if parsed_dt > datetime.now(): raise ValueError()
        dt=parsed_dt.date().isoformat(); payment_datetime=parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
    except: return jsonify(error="Enter the actual payment date and time."),400
    if amount<=0 or not reference:return jsonify(error="Amount and UPI reference are required."),400
    token=secrets.token_urlsafe(32)
    proof=None
    try:
        with db() as c:
            total=int(request.student["total_fee"] or 0)
            paid=c.execute("SELECT COALESCE(SUM(amount),0) n FROM fee_payments WHERE admission_id=? AND status='verified'",(request.student["id"],)).fetchone()["n"]
            waiting=c.execute("SELECT 1 FROM fee_payments WHERE admission_id=? AND status='pending' LIMIT 1",(request.student["id"],)).fetchone()
            if waiting:return jsonify(error="You already have a UPI payment awaiting verification."),409
            if amount>total-paid:return jsonify(error="Payment exceeds pending balance."),400
            if request.files.get("proof"):
                proof=save_upload(request.files.get("proof"),{".jpg",".jpeg",".png",".webp",".pdf"})
            c.execute("INSERT INTO fee_payments(admission_id,amount,mode,status,reference,payment_date,receipt_token,proof_file,payment_datetime) VALUES(?,?,'upi','pending',?,?,?,?,?)",(request.student["id"],amount,reference,dt,token,proof,payment_datetime))
    except sqlite3.IntegrityError:
        if proof:delete_portal_file(proof)
        return jsonify(error="Duplicate UPI reference rejected."),409
    except Exception:
        if proof:delete_portal_file(proof)
        raise
    return jsonify(message="Payment submitted. Admin will manually verify the UTR/proof; fee balance updates only after verification."),201

@portal.get("/api/admin/portal/payments")
@admin_only
def pending_portal_payments():
    with db() as c:
        rows=c.execute("""SELECT a.id admission_id,p.student_code,a.student_name,p.total_fee,
          COALESCE((SELECT SUM(x.amount) FROM fee_payments x WHERE x.admission_id=a.id AND x.status='verified'),0) paid,
          (SELECT f.id FROM fee_payments f WHERE f.admission_id=a.id AND f.status='pending' ORDER BY f.id DESC LIMIT 1) payment_id,
          (SELECT f.amount FROM fee_payments f WHERE f.admission_id=a.id AND f.status='pending' ORDER BY f.id DESC LIMIT 1) amount,
          (SELECT f.reference FROM fee_payments f WHERE f.admission_id=a.id AND f.status='pending' ORDER BY f.id DESC LIMIT 1) reference,
          (SELECT f.payment_date FROM fee_payments f WHERE f.admission_id=a.id AND f.status='pending' ORDER BY f.id DESC LIMIT 1) payment_date,
          (SELECT f.payment_datetime FROM fee_payments f WHERE f.admission_id=a.id AND f.status='pending' ORDER BY f.id DESC LIMIT 1) payment_datetime,
          (SELECT f.proof_file FROM fee_payments f WHERE f.admission_id=a.id AND f.status='pending' ORDER BY f.id DESC LIMIT 1) proof_file
          FROM admissions a JOIN portal_students p ON p.admission_id=a.id WHERE p.active=1 AND p.deleted=0
          AND p.total_fee>COALESCE((SELECT SUM(y.amount) FROM fee_payments y WHERE y.admission_id=a.id AND y.status='verified'),0)
          ORDER BY a.student_name""").fetchall()
    return jsonify([{**dict(r),"pending_fee":max((r["total_fee"] or 0)-(r["paid"] or 0),0)} for r in rows])

@portal.post("/api/admin/portal/payments/verify")
@admin_only
def verify_fee_payment_manual():
    d=request.get_json(silent=True) or {}
    utr=str(d.get("utr") or "").strip()
    try: amount=int(d.get("amount",0))
    except: return jsonify(error="Please enter a valid payment amount."),400
    if not utr or amount<=0:return jsonify(error="Both UTR and amount are required."),400
    try:
        with db() as c:
            sid=resolve_student_id(c,d.get("student_id"))
            if not sid:return jsonify(error="Student not found."),404
            p=c.execute("SELECT total_fee FROM portal_students WHERE admission_id=?",(sid,)).fetchone()
            paid=c.execute("SELECT COALESCE(SUM(amount),0) n FROM fee_payments WHERE admission_id=? AND status='verified'",(sid,)).fetchone()["n"]
            if amount>max((p["total_fee"] or 0)-paid,0):return jsonify(error="The amount cannot exceed the remaining fee."),400
            payment_id=d.get("payment_id")
            if payment_id:
                old=c.execute("SELECT * FROM fee_payments WHERE id=? AND admission_id=? AND status='pending'",(int(payment_id),sid)).fetchone()
                if not old:return jsonify(error="Pending payment not found."),404
                c.execute("UPDATE fee_payments SET amount=?,reference=?,status='verified',verified_at=? WHERE id=?",(amount,utr,datetime.utcnow().isoformat(),int(payment_id)))
                # If this is the UTR supplied with the original admission,
                # update the admission receipt/status as well.
                if old["reference"]:
                    c.execute(
                        "UPDATE admissions SET payment_status='paid' WHERE id=? AND payment_reference=? AND payment_status='pending'",
                        (sid, old["reference"]),
                    )
                token=old["receipt_token"]
            else:
                token=secrets.token_urlsafe(32)
                c.execute("INSERT INTO fee_payments(admission_id,amount,mode,status,reference,payment_date,receipt_token,verified_at) VALUES(?,?,'upi','verified',?,?,?,?)",(sid,amount,utr,date.today().isoformat(),token,datetime.utcnow().isoformat()))
    except (sqlite3.IntegrityError,ValueError):return jsonify(error="The UTR is a duplicate or the payment entry is invalid."),409
    log("payment_verified",f"Student {sid}; amount {amount}; UTR {utr}")
    return jsonify(message="Payment verified. Remaining fee aur receipt update ho gayi.",receipt_url=f"/api/admin/portal/receipt/{token}.pdf")

@portal.get("/api/admin/portal/receipt/<token>.pdf")
@admin_only
def admin_fee_receipt(token):
    with db() as c:
        row=c.execute("SELECT * FROM fee_payments WHERE receipt_token=? AND status='verified'",(token,)).fetchone()
        if not row:return jsonify(error="Receipt not found."),404
        student=student_row(c,row["admission_id"])
    return _build_fee_receipt_pdf(row, student, f"BCIT-fee-{row['id']}.pdf")


@portal.get("/api/admin/portal/receipts")
@admin_only
def all_portal_receipts():
    with db() as c:
        rows=c.execute("""SELECT f.id,f.amount,f.mode,f.status,f.reference,f.payment_date,f.payment_datetime,f.receipt_token,
          a.student_name,p.student_code,a.id admission_id
          FROM fee_payments f JOIN admissions a ON a.id=f.admission_id JOIN portal_students p ON p.admission_id=a.id
          ORDER BY f.id DESC""").fetchall()
    return jsonify([dict(r) for r in rows])

@portal.get("/api/admin/portal/receipts/print-two.pdf")
@admin_only
def print_two_portal_receipts():
    raw = request.args.get("ids", "")
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        return jsonify(error="Select exactly two valid receipts."), 400
    if len(ids) != 2 or ids[0] == ids[1]:
        return jsonify(error="Select exactly two different verified student receipts."), 400
    with db() as c:
        rows=[]; students=[]
        for rid in ids:
            row=c.execute("SELECT * FROM fee_payments WHERE id=? AND status='verified'",(rid,)).fetchone()
            if not row: return jsonify(error="Both receipts must be verified."),404
            student=student_row(c,row["admission_id"])
            if not student: return jsonify(error="Student record not found."),404
            rows.append(row); students.append(student)
    return _build_two_fee_receipts_pdf(rows, students)

@portal.get("/api/admin/portal/attendance/batches")
@admin_only
def attendance_batches():
    with db() as c:
        rows=c.execute("""SELECT b.id,b.name,COUNT(bs.admission_id) student_count FROM attendance_batches b
          LEFT JOIN attendance_batch_students bs ON bs.batch_id=b.id GROUP BY b.id ORDER BY b.id""").fetchall()
    return jsonify([dict(r) for r in rows])

@portal.post("/api/admin/portal/attendance/batches")
@admin_only
def create_attendance_batch():
    d=request.get_json(silent=True) or {}; name=str(d.get("name") or "").strip(); ids=d.get("student_ids") or []
    if not name or not isinstance(ids,list) or not ids or len(ids)>15 or len(set(map(str,ids)))!=len(ids):
        return jsonify(error="Batch name aur 1 se 15 unique students select karein."),400
    try:
        with db() as c:
            resolved=[]
            for value in ids:
                sid=resolve_student_id(c,value)
                if not sid: return jsonify(error=f"Student not found: {value}"),404
                resolved.append(sid)
            cur=c.execute("INSERT INTO attendance_batches(name) VALUES(?)",(name,)); bid=cur.lastrowid
            c.executemany("INSERT INTO attendance_batch_students(batch_id,admission_id) VALUES(?,?)",[(bid,x) for x in resolved])
    except sqlite3.IntegrityError:return jsonify(error="Batch name already exists or student selection is invalid."),409
    return jsonify(message="Attendance batch created.",batch_id=bid),201

@portal.post("/api/admin/portal/attendance/batches/<int:batch_id>/students")
@admin_only
def add_batch_student(batch_id):
    d=request.get_json(silent=True) or {}; value=d.get("student_id")
    with db() as c:
        batch=c.execute("SELECT id FROM attendance_batches WHERE id=?",(batch_id,)).fetchone()
        if not batch:return jsonify(error="Batch not found."),404
        count=c.execute("SELECT COUNT(*) n FROM attendance_batch_students WHERE batch_id=?",(batch_id,)).fetchone()["n"]
        if count>=15:return jsonify(error="Batch is full. Maximum 15 students allowed."),400
        sid=resolve_student_id(c,value)
        if not sid:return jsonify(error="Student not found."),404
        active=c.execute("SELECT 1 FROM portal_students WHERE admission_id=? AND active=1 AND deleted=0",(sid,)).fetchone()
        if not active:return jsonify(error="Only active students can be added."),400
        try:c.execute("INSERT INTO attendance_batch_students(batch_id,admission_id) VALUES(?,?)",(batch_id,sid))
        except sqlite3.IntegrityError:return jsonify(error="Student is already in this batch."),409
    return jsonify(message="Student added to batch.")

@portal.delete("/api/admin/portal/attendance/batches/<int:batch_id>/students")
@admin_only
def remove_batch_student(batch_id):
    d=request.get_json(silent=True) or {}; sid=None
    with db() as c:
        if not c.execute("SELECT 1 FROM attendance_batches WHERE id=?",(batch_id,)).fetchone():return jsonify(error="Batch not found."),404
        sid=resolve_student_id(c,d.get("student_id"))
        if not sid:return jsonify(error="Student not found."),404
        cur=c.execute("DELETE FROM attendance_batch_students WHERE batch_id=? AND admission_id=?",(batch_id,sid))
        if not cur.rowcount:return jsonify(error="Student is not in this batch."),404
    return jsonify(message="Student removed from batch.")

@portal.delete("/api/admin/portal/attendance/batches/<int:batch_id>")
@admin_only
def delete_attendance_batch(batch_id):
    with db() as c:
        cur=c.execute("DELETE FROM attendance_batches WHERE id=?",(batch_id,))
        if not cur.rowcount:return jsonify(error="Batch not found."),404
    return jsonify(message="Attendance batch deleted.")

@portal.get("/api/admin/portal/attendance")
@admin_only
def admin_attendance():
    today=date.today().isoformat()
    batch_id=request.args.get("batch_id","").strip()
    with db() as c:
        if batch_id:
            rows=c.execute("SELECT a.id,p.student_code,a.student_name,? attendance_date,t.status FROM attendance_batch_students bs JOIN admissions a ON a.id=bs.admission_id JOIN portal_students p ON p.admission_id=a.id LEFT JOIN attendance t ON t.admission_id=a.id AND t.attendance_date=? WHERE bs.batch_id=? AND p.active=1 AND p.deleted=0 ORDER BY a.student_name",(today,today,int(batch_id))).fetchall()
        else:
            rows=c.execute("SELECT a.id,p.student_code,a.student_name,? attendance_date,t.status FROM admissions a JOIN portal_students p ON p.admission_id=a.id LEFT JOIN attendance t ON t.admission_id=a.id AND t.attendance_date=? WHERE p.active=1 AND p.deleted=0 ORDER BY a.student_name",(today,today)).fetchall()
        result=[{**dict(r),"attendance_percent":attendance_score(c,r["id"])} for r in rows]
    return jsonify(result)

@portal.post("/api/admin/portal/attendance")
@admin_only
def mark_attendance():
    d=request.get_json(silent=True) or {}
    try:dt=date.today().isoformat();st=d["status"]
    except:return jsonify(error="Invalid attendance details."),400
    if st not in {"present","absent"}:return jsonify(error="Status must be present or absent."),400
    with db() as c:
        sid=resolve_student_id(c,d.get("student_id"))
        if not sid:return jsonify(error="Student not found."),404
        if d.get("batch_id") and not c.execute("SELECT 1 FROM attendance_batch_students WHERE batch_id=? AND admission_id=?",(int(d["batch_id"]),sid)).fetchone():return jsonify(error="Student is not part of the selected batch."),400
        c.execute("INSERT INTO attendance(admission_id,attendance_date,status) VALUES(?,?,?) ON CONFLICT(admission_id,attendance_date) DO UPDATE SET status=excluded.status",(sid,dt,st))
    log("attendance_marked",f"Student {sid} {dt} {st}")
    return jsonify(message="Attendance saved.")

@portal.get("/api/student/online-exams")
@student_only
def student_online_exams():
    with db() as c:
        rows=c.execute("""SELECT e.id,e.exam_code,e.title,e.subject,e.exam_date,e.instructions,e.max_marks,e.pass_marks,e.duration_minutes,
          t.id attempt_id,t.status attempt_status,t.started_at,t.ends_at,t.submitted_at,t.marks,t.total_marks
          FROM exams e LEFT JOIN online_exam_attempts t ON t.exam_id=e.id AND t.admission_id=?
          WHERE LOWER(e.exam_type)='online' ORDER BY e.id DESC""",(request.student['id'],)).fetchall()
    return jsonify(exams=[dict(r) for r in rows])

@portal.post("/api/student/online-exams/<int:exam_id>/start")
@student_only
def start_online_exam(exam_id):
    now=datetime.utcnow().replace(microsecond=0)
    with db() as c:
        exam=c.execute("SELECT * FROM exams WHERE id=? AND LOWER(exam_type)='online'",(exam_id,)).fetchone()
        if not exam:return jsonify(error="Online exam not found."),404
        questions=c.execute("SELECT COUNT(*) n FROM online_exam_questions WHERE exam_id=?",(exam_id,)).fetchone()['n']
        if not questions:return jsonify(error="This exam has no questions yet."),400
        attempt=c.execute("SELECT * FROM online_exam_attempts WHERE exam_id=? AND admission_id=?",(exam_id,request.student['id'])).fetchone()
        if not attempt:
            ends=now+__import__('datetime').timedelta(minutes=int(exam['duration_minutes']))
            c.execute("INSERT INTO online_exam_attempts(exam_id,admission_id,started_at,ends_at,total_marks) VALUES(?,?,?,?,?)",(exam_id,request.student['id'],now.isoformat(),ends.isoformat(),exam['max_marks']))
            attempt=c.execute("SELECT * FROM online_exam_attempts WHERE exam_id=? AND admission_id=?",(exam_id,request.student['id'])).fetchone()
    return jsonify(attempt=dict(attempt))

@portal.get("/api/student/online-exams/<int:exam_id>/questions")
@student_only
def get_online_exam_questions(exam_id):
    with db() as c:
        attempt=c.execute("SELECT * FROM online_exam_attempts WHERE exam_id=? AND admission_id=?",(exam_id,request.student['id'])).fetchone()
        if not attempt:return jsonify(error="Start this exam first."),404
        if attempt['status']=='in_progress' and datetime.utcnow()>=datetime.fromisoformat(attempt['ends_at']):
            _grade_online_attempt(c,exam_id,request.student['id'],json.loads(attempt['answers_json'] or '{}'))
            attempt=c.execute("SELECT * FROM online_exam_attempts WHERE id=?",(attempt['id'],)).fetchone()
        qs=c.execute("SELECT id,question_no,question_text,options_json,marks FROM online_exam_questions WHERE exam_id=? ORDER BY question_no",(exam_id,)).fetchall()
    return jsonify(
        attempt={k:attempt[k] for k in ['status','started_at','ends_at','submitted_at','marks','total_marks']} | {
            'answers': json.loads(attempt['answers_json'] or '{}')
        },
        questions=[{'id':q['id'],'question_no':q['question_no'],'question':q['question_text'],'options':json.loads(q['options_json']),'marks':q['marks']} for q in qs]
    )

def _grade_online_attempt(c,exam_id,admission_id,answers):
    attempt=c.execute("SELECT * FROM online_exam_attempts WHERE exam_id=? AND admission_id=?",(exam_id,admission_id)).fetchone()
    if not attempt or attempt['status']=='submitted':return attempt
    qs=c.execute("SELECT id,correct_option,marks FROM online_exam_questions WHERE exam_id=?",(exam_id,)).fetchall()
    score=0.0
    for q in qs:
        try: selected=int(answers.get(str(q['id']),-1))
        except: selected=-1
        if selected==q['correct_option']:score+=float(q['marks'])
    c.execute("UPDATE online_exam_attempts SET answers_json=?,marks=?,status='submitted',submitted_at=? WHERE id=?",(json.dumps(answers),score,datetime.utcnow().replace(microsecond=0).isoformat(),attempt['id']))
    return c.execute("SELECT * FROM online_exam_attempts WHERE id=?",(attempt['id'],)).fetchone()

@portal.post("/api/student/online-exams/<int:exam_id>/violation")
@student_only
def record_online_exam_violation(exam_id):
    data = request.get_json(silent=True) or {}
    violation_type = str(data.get("violation_type") or "other").strip().lower()
    allowed_types = {"fullscreen_exit", "tab_switch", "window_blur"}
    if violation_type not in allowed_types:
        return jsonify(error="Invalid exam violation type."), 400

    with db() as c:
        attempt = c.execute(
            "SELECT id,status FROM online_exam_attempts WHERE exam_id=? AND admission_id=?",
            (exam_id, request.student["id"])
        ).fetchone()
        if not attempt:
            return jsonify(error="Start this exam first."), 404
        if attempt["status"] != "in_progress":
            return jsonify(error="This exam has already been submitted."), 409
        occurred_at = datetime.utcnow().replace(microsecond=0).isoformat()
        c.execute(
            "INSERT INTO online_exam_violations(attempt_id,exam_id,admission_id,violation_type,occurred_at) VALUES(?,?,?,?,?)",
            (attempt["id"], exam_id, request.student["id"], violation_type, occurred_at)
        )
    return jsonify(message="Exam violation recorded.")


@portal.post("/api/student/online-exams/<int:exam_id>/answers")
@student_only
def save_online_exam_answers(exam_id):
    data=request.get_json(silent=True) or {}; answers=data.get("answers") or {}
    if not isinstance(answers,dict): return jsonify(error="Invalid answers."),400
    with db() as c:
        attempt=c.execute("SELECT * FROM online_exam_attempts WHERE exam_id=? AND admission_id=?",(exam_id,request.student["id"])).fetchone()
        if not attempt:return jsonify(error="Start this exam first."),404
        if attempt["status"]!="in_progress":return jsonify(error="This exam has already been submitted."),409
        if datetime.utcnow()>=datetime.fromisoformat(attempt["ends_at"]):
            _grade_online_attempt(c,exam_id,request.student["id"],json.loads(attempt["answers_json"] or "{}"))
            return jsonify(error="Exam time is over."),409
        valid={str(r["id"]) for r in c.execute("SELECT id FROM online_exam_questions WHERE exam_id=?",(exam_id,))}
        clean={k:int(v) for k,v in answers.items() if str(k) in valid and str(v) in {"0","1","2","3"}}
        c.execute("UPDATE online_exam_attempts SET answers_json=? WHERE id=?",(json.dumps(clean),attempt["id"]))
    return jsonify(message="Answers saved.")

@portal.post("/api/student/online-exams/<int:exam_id>/submit")
@student_only
def submit_online_exam(exam_id):
    with db() as c:
        attempt=c.execute("SELECT * FROM online_exam_attempts WHERE exam_id=? AND admission_id=?",(exam_id,request.student['id'])).fetchone()
        if not attempt:return jsonify(error="Start this exam first."),404
        if attempt['status']=='in_progress':
            # Grade only the answers already persisted by the server; never trust a client-side answer map.
            saved_answers=json.loads(attempt['answers_json'] or '{}')
            attempt=_grade_online_attempt(c,exam_id,request.student['id'],saved_answers)
    return jsonify(message="Exam submitted successfully.",marks=attempt['marks'],total_marks=attempt['total_marks'],submitted_at=attempt['submitted_at'],status=attempt['status'])


@portal.delete("/api/admin/portal/exam-violations")
@admin_only
def delete_all_online_exam_violations():
    """Permanently clear only violation history; keep exams, attempts and results."""
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM online_exam_violations").fetchone()[0]
        c.execute("DELETE FROM online_exam_violations")
    log("all_online_exam_violations_deleted", f"Deleted {count} online exam violation records")
    return jsonify(
        message=f"All online exam violation reports permanently deleted ({count} deleted).",
        deleted=count
    )


@portal.get("/api/admin/portal/exam-violations")
@admin_only
def list_online_exam_violations():
    with db() as c:
        rows = c.execute("""SELECT v.id, a.student_name, p.student_code,
            datetime(v.occurred_at, '+5 hours', '+30 minutes') AS violation_time_ist,
            e.exam_code, e.title AS exam_name, v.violation_type
            FROM online_exam_violations v
            JOIN admissions a ON a.id=v.admission_id
            JOIN portal_students p ON p.admission_id=a.id
            JOIN exams e ON e.id=v.exam_id
            ORDER BY v.occurred_at DESC, v.id DESC""").fetchall()
    return jsonify(violations=[dict(row) for row in rows])


@portal.post("/api/admin/portal/exams")
@admin_only
def create_exam():
    d=request.get_json(silent=True) or {}
    code=str(d.get("exam_code") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}",code): return jsonify(error="Enter the Exam ID using letters and numbers only."),400
    exam_type=str(d.get("exam_type","offline")).lower()
    if exam_type not in {"offline","online"}: return jsonify(error="Exam type must be Offline or Online."),400
    try:
        with db() as c:
            if c.execute("SELECT 1 FROM exams WHERE exam_code=?",(code,)).fetchone(): return jsonify(error="Exam ID already exists."),409
            questions=d.get("questions") or []
            duration=int(d.get("duration_minutes") or 0) if exam_type=="online" else 0
            if exam_type=="online":
                if not 1 <= len(questions) <= 50: return jsonify(error="Online exam must contain 1 to 50 questions."),400
                if not 1 <= duration <= 600: return jsonify(error="Set an exam duration between 1 and 600 minutes."),400
                total=0
                for q in questions:
                    opts=q.get("options") or []
                    if not str(q.get("question") or "").strip() or len(opts)!=4 or any(not str(x).strip() for x in opts): return jsonify(error="Every question needs text and exactly four non-empty options."),400
                    correct=int(q.get("correct_option",-1)); points=float(q.get("marks",1))
                    if correct not in range(4) or points<=0: return jsonify(error="Choose a correct option and positive marks for each question."),400
                    total+=points
                max_marks=total
            else:
                max_marks=float(d.get("max_marks",100))
            cur=c.execute("INSERT INTO exams(exam_code,title,subject,exam_date,exam_type,max_marks,pass_marks,instructions) VALUES(?,?,?,?,?,?,?,?)",(code,d["title"],d["subject"],d.get("exam_date"),exam_type,max_marks,float(d.get("pass_marks",33)),str(d.get("instructions") or "").strip()))
            if exam_type=="online":
                c.execute("UPDATE exams SET duration_minutes=? WHERE id=?",(duration,cur.lastrowid))
                for i,q in enumerate(questions,1):
                    c.execute("INSERT INTO online_exam_questions(exam_id,question_no,question_text,options_json,correct_option,marks) VALUES(?,?,?,?,?,?)",(cur.lastrowid,i,str(q["question"]).strip(),json.dumps(q["options"]),int(q["correct_option"]),float(q.get("marks",1))))
    except Exception:return jsonify(error="Invalid exam data."),400
    log("exam_created",str(cur.lastrowid));return jsonify(id=cur.lastrowid,exam_id=code),201

@portal.post("/api/admin/portal/exams/<eid>/marks")
@admin_only
def enter_marks(eid):
    d=request.get_json(silent=True) or {}
    try:marks=float(d["marks"])
    except:return jsonify(error="Invalid marks."),400
    with db() as c:
        eidrow=c.execute("SELECT id,max_marks FROM exams WHERE exam_code=?",(str(eid),)).fetchone()
        if not eidrow and str(eid).isdigit(): eidrow=c.execute("SELECT id,max_marks FROM exams WHERE id=?",(int(eid),)).fetchone()
        sid=resolve_student_id(c,d.get("student_id"))
        if not eidrow or marks<0 or marks>eidrow["max_marks"] or not sid:return jsonify(error="Invalid exam, student, or marks."),400
        eid=eidrow["id"]
        c.execute("INSERT INTO exam_marks(exam_id,admission_id,marks) VALUES(?,?,?) ON CONFLICT(exam_id,admission_id) DO UPDATE SET marks=excluded.marks",(eid,sid,marks))
    log("marks_updated",f"Exam {eid}; student {sid}")
    return jsonify(message="Marks saved.")

@portal.delete("/api/admin/portal/exam-results")
@admin_only
def delete_all_exam_results():
    """Delete every submitted student result, both offline and online.

    Keep exam definitions/questions and any in-progress online attempts intact.
    Remove violation rows for submitted attempts before deleting those attempts
    so this also works when SQLite foreign-key enforcement is enabled.
    """
    with db() as c:
        offline_count = c.execute("SELECT COUNT(*) FROM exam_marks").fetchone()[0]
        online_count = c.execute(
            "SELECT COUNT(*) FROM online_exam_attempts WHERE status='submitted'"
        ).fetchone()[0]
        c.execute("""DELETE FROM online_exam_violations
                     WHERE attempt_id IN (
                         SELECT id FROM online_exam_attempts WHERE status='submitted'
                     )""")
        c.execute("DELETE FROM online_exam_attempts WHERE status='submitted'")
        c.execute("DELETE FROM exam_marks")

    count = offline_count + online_count
    log(
        "all_exam_results_deleted",
        f"Deleted {count} student exam results ({offline_count} offline, {online_count} online)",
    )
    return jsonify(
        message=f"All student exam results deleted successfully ({count} deleted).",
        deleted=count,
        offline_deleted=offline_count,
        online_deleted=online_count,
    )

@portal.delete("/api/admin/portal/exam-results/<int:result_id>")
@admin_only
def delete_exam_result(result_id):
    # Online attempts and offline marks have independent ID sequences. The
    # explicit type prevents an online result ID from deleting an unrelated
    # offline result that happens to have the same numeric ID.
    result_type = (request.args.get("type") or "").strip().lower()
    with db() as c:
        if result_type == "online":
            row = c.execute(
                "SELECT id,exam_id,admission_id FROM online_exam_attempts WHERE id=? AND status='submitted'",
                (result_id,)
            ).fetchone()
            if not row:
                return jsonify(error="Online exam result not found."),404
            # Remove violation rows explicitly as well, so cleanup is safe
            # even when SQLite foreign-key cascading is not enabled.
            c.execute("DELETE FROM online_exam_violations WHERE attempt_id=?", (result_id,))
            c.execute("DELETE FROM online_exam_attempts WHERE id=?", (result_id,))
            result_label = "Online result"
        elif result_type == "offline":
            row = c.execute(
                "SELECT id,exam_id,admission_id FROM exam_marks WHERE id=?",
                (result_id,)
            ).fetchone()
            if not row:
                return jsonify(error="Offline exam result not found."),404
            c.execute("DELETE FROM exam_marks WHERE id=?", (result_id,))
            result_label = "Offline result"
        else:
            # Backward-compatible behavior for older clients.
            row = c.execute("SELECT id,exam_id,admission_id FROM exam_marks WHERE id=?",(result_id,)).fetchone()
            if row:
                c.execute("DELETE FROM exam_marks WHERE id=?",(result_id,))
                result_label = "Offline result"
            else:
                row = c.execute("SELECT id,exam_id,admission_id FROM online_exam_attempts WHERE id=? AND status='submitted'",(result_id,)).fetchone()
                if not row:
                    return jsonify(error="Exam result not found."),404
                c.execute("DELETE FROM online_exam_violations WHERE attempt_id=?",(result_id,))
                c.execute("DELETE FROM online_exam_attempts WHERE id=?",(result_id,))
                result_label = "Online result"
    log("exam_result_deleted",f"{result_label} {result_id}; exam {row['exam_id']}; student {row['admission_id']}")
    return jsonify(message="Student exam result deleted successfully.")

@portal.delete("/api/admin/portal/exams/<path:exam_code>")
@admin_only
def delete_exam(exam_code):
    with db() as c:
        row=c.execute("SELECT id,exam_code FROM exams WHERE exam_code=?",(exam_code,)).fetchone()
        if not row and str(exam_code).isdigit():row=c.execute("SELECT id,exam_code FROM exams WHERE id=?",(int(exam_code),)).fetchone()
        if not row:return jsonify(error="Exam not found."),404
        c.execute("DELETE FROM exam_marks WHERE exam_id=?",(row["id"],))
        c.execute("DELETE FROM exams WHERE id=?",(row["id"],))
    log("exam_deleted",f"Exam {row['exam_code'] or row['id']} and linked results")
    return jsonify(message="Exam and all linked student results deleted successfully.")

@portal.get("/api/admin/portal/exams")
@admin_only
def list_exams():
    with db() as c:
        exams=[dict(r) for r in c.execute("SELECT e.*,(SELECT COUNT(*) FROM online_exam_questions q WHERE q.exam_id=e.id) question_count FROM exams e ORDER BY e.id DESC")]
        marks=[dict(r) for r in c.execute("SELECT m.*,a.student_name,p.student_code,e.title,e.subject,e.pass_marks,'Offline/manual' attempt_type,'offline' result_type FROM exam_marks m JOIN admissions a ON a.id=m.admission_id JOIN portal_students p ON p.admission_id=a.id JOIN exams e ON e.id=m.exam_id ORDER BY m.id DESC")]
        online=[dict(r) for r in c.execute("SELECT t.*,a.student_name,p.student_code,e.title,e.subject,e.pass_marks,'Online' attempt_type,'online' result_type FROM online_exam_attempts t JOIN admissions a ON a.id=t.admission_id JOIN portal_students p ON p.admission_id=a.id JOIN exams e ON e.id=t.exam_id WHERE t.status='submitted' ORDER BY t.submitted_at DESC")]
    return jsonify(exams=exams,marks=marks+online,online_attempts=online)

@portal.post("/api/admin/portal/materials")
@admin_only
def add_material():
    d=request.form
    name=None
    try:
        url=(d.get("url") or "").strip()
        kind=d.get("kind","pdf")
        student_id=int(d.get("student_id")) if kind=="certificate" and d.get("student_id") else None
        if kind=="certificate" and not student_id:return jsonify(error="A Student ID is required for the certificate."),400
        if student_id:
            with db() as c:
                if not student_row(c,student_id):return jsonify(error="Student not found."),404
        course=(d.get("course") or "").strip()
        if course.lower() in {"all", "all courses", "*"}: course=""
        f=request.files.get("file")
        name=save_upload(f, max_bytes=20 * 1024 * 1024) if f and f.filename else None
        if not name and not url:return jsonify(error="Upload a file or provide a video URL."),400
        with db() as c:
            cur=c.execute("INSERT INTO materials(title,kind,file_name,external_url,course,admission_id) VALUES(?,?,?,?,?,?)",(d.get("title","Study Material").strip() or "Study Material",kind,name,url,course or None,student_id))
    except ValueError as e:
        if name:delete_portal_file(name)
        return jsonify(error=str(e)),400
    except Exception:
        if name:delete_portal_file(name)
        raise
    log("material_added",str(cur.lastrowid));notify_learning_update();return jsonify(message="Material added.",id=cur.lastrowid),201

@portal.post("/api/admin/portal/assignments")
@admin_only
def add_assignment():
    d=request.form
    due=(d.get("due_date") or "").strip()
    if due:
        try: datetime.strptime(due,"%Y-%m-%dT%H:%M")
        except ValueError:return jsonify(error="Please set both the deadline date and time."),400
    course=(d.get("course") or "").strip()
    if course.lower() in {"all", "all courses", "*"}: course=""
    name=None
    try:
        name=save_upload(request.files.get("file"),{".pdf",".doc",".docx",".ppt",".pptx",".jpg",".jpeg",".png"}, max_bytes=5 * 1024 * 1024)
        with db() as c:
            cur=c.execute("INSERT INTO assignments(title,description,file_name,course,due_date) VALUES(?,?,?,?,?)",(d.get("title","Assignment").strip() or "Assignment",d.get("description"),name,course or None,due or None))
    except ValueError as e:
        if name:delete_portal_file(name)
        return jsonify(error=str(e)),400
    except Exception:
        if name:delete_portal_file(name)
        raise
    log("assignment_added",str(cur.lastrowid));notify_learning_update();return jsonify(message="Assignment added.",id=cur.lastrowid),201


@portal.post("/api/admin/portal/assignments/<int:assignment_id>/question")
@admin_only
def update_assignment_question(assignment_id):
    try:
        filename=save_upload(request.files.get("file"),{".pdf",".doc",".docx",".ppt",".pptx",".jpg",".jpeg",".png"}, max_bytes=5 * 1024 * 1024)
    except ValueError as e:
        return jsonify(error=str(e)),400
    old_filename=None
    with db() as c:
        row=c.execute("SELECT file_name FROM assignments WHERE id=?",(assignment_id,)).fetchone()
        if not row:
            delete_portal_file(filename)
            return jsonify(error="Assignment not found."),404
        old_filename=row["file_name"]
        c.execute("UPDATE assignments SET file_name=? WHERE id=?",(filename,assignment_id))
    if old_filename and old_filename != filename:
        with db() as c:
            used=c.execute("SELECT 1 FROM materials WHERE file_name=? UNION SELECT 1 FROM assignments WHERE file_name=? UNION SELECT 1 FROM submissions WHERE file_name=?",(old_filename,old_filename,old_filename)).fetchone()
        if not used:
            delete_portal_file(old_filename)
    log("assignment_question_updated",str(assignment_id))
    notify_learning_update()
    return jsonify(message="Assignment question file saved successfully."),200

@portal.get("/api/admin/portal/learning-content")
@admin_only
def admin_learning_content():
    with db() as c:
        materials=[dict(r) for r in c.execute("SELECT id,title,kind,file_name,external_url,course,created_at FROM materials ORDER BY id DESC")]
        assignments=[dict(r) for r in c.execute("SELECT id,title,description,file_name,course,due_date,created_at FROM assignments ORDER BY id DESC")]
    return jsonify(materials=materials,assignments=assignments)

@portal.delete("/api/admin/portal/materials/<int:material_id>")
@admin_only
def delete_material(material_id):
    with db() as c:
        row=c.execute("SELECT file_name FROM materials WHERE id=?",(material_id,)).fetchone()
        if not row:return jsonify(error="Study material not found."),404
        filename=row["file_name"]
        c.execute("DELETE FROM materials WHERE id=?",(material_id,))
        if filename and not c.execute("SELECT 1 FROM materials WHERE file_name=? UNION SELECT 1 FROM assignments WHERE file_name=?",(filename,filename)).fetchone():
            try:delete_portal_file(filename)
            except OSError:pass
    log("material_deleted",str(material_id));notify_learning_update()
    return jsonify(message="Study material deleted successfully.")

@portal.delete("/api/admin/portal/assignments/<int:assignment_id>")
@admin_only
def delete_assignment(assignment_id):
    with db() as c:
        row=c.execute("SELECT file_name FROM assignments WHERE id=?",(assignment_id,)).fetchone()
        if not row:return jsonify(error="Assignment not found."),404
        filename=row["file_name"]
        c.execute("DELETE FROM submissions WHERE assignment_id=?",(assignment_id,))
        c.execute("DELETE FROM assignments WHERE id=?",(assignment_id,))
        if filename and not c.execute("SELECT 1 FROM materials WHERE file_name=? UNION SELECT 1 FROM assignments WHERE file_name=?",(filename,filename)).fetchone():
            try:delete_portal_file(filename)
            except OSError:pass
    log("assignment_deleted",str(assignment_id));notify_learning_update()
    return jsonify(message="Assignment deleted successfully.")

@portal.get("/api/admin/portal/submissions")
@admin_only
def submissions():
    with db() as c:rows=c.execute("""SELECT s.id,s.file_name,s.original_name,s.submitted_at,
        a.student_name,a.course,p.student_code,x.title assignment_title,x.course assignment_course
        FROM submissions s JOIN admissions a ON a.id=s.admission_id
        LEFT JOIN portal_students p ON p.admission_id=a.id
        JOIN assignments x ON x.id=s.assignment_id ORDER BY s.id DESC""").fetchall()
    return jsonify([dict(r) for r in rows])

@portal.get("/api/admin/portal/stats")
@admin_only
def portal_stats():
    with db() as c:
        students=c.execute("SELECT COUNT(*) n FROM portal_students WHERE deleted=0").fetchone()["n"]
        active=c.execute("SELECT COUNT(*) n FROM portal_students WHERE active=1 AND deleted=0").fetchone()["n"]
        paid=c.execute("SELECT COALESCE(SUM(amount),0) n FROM fee_payments WHERE status='verified'").fetchone()["n"]
        pending=c.execute("SELECT COALESCE(SUM(amount),0) n FROM fee_payments WHERE status='pending'").fetchone()["n"]
        due=c.execute("SELECT COUNT(*) n FROM portal_students p JOIN admissions a ON a.id=p.admission_id WHERE p.deleted=0 AND p.total_fee>(SELECT COALESCE(SUM(amount),0) FROM fee_payments f WHERE f.admission_id=a.id AND f.status='verified')").fetchone()["n"]
    return jsonify(students=students,active=active,paid=paid,pending=pending,due_students=due)

@portal.get("/api/admin/portal/submissions/<int:sid>/download")
@admin_only
def download_submission(sid):
    with db() as c:r=c.execute("SELECT file_name FROM submissions WHERE id=?",(sid,)).fetchone()
    if not r:return "Not found",404
    try:
        name = Path(r["file_name"]).name
        return send_file(BytesIO(_STORAGE_BYTES(portal_object_key(name))), download_name=name, as_attachment=True)
    except FileNotFoundError:
        return "Not found",404

@portal.get("/api/admin/portal/backup")
@admin_only
def backup_db():
    """Download a logical backup of all D1 business and storage metadata records."""
    tables = ["enquiries","admissions","portal_students","fee_payments","attendance","attendance_batches",
              "attendance_batch_students","exams","exam_marks","online_exam_questions","online_exam_attempts",
              "materials","assignments","submissions"]
    payload = {"format":"bcit-d1-json-v1", "created_at":datetime.utcnow().isoformat()+"Z", "tables":{}}
    with db() as c:
        for table in tables:
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            if exists:
                payload["tables"][table]=[dict(row) for row in c.execute(f'SELECT * FROM "{table}"')]
        for table in ("admin_activity", "file_metadata"):
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            if exists:
                order = "id" if table == "admin_activity" else "created_at"
                payload["tables"][table]=[dict(row) for row in c.execute(f'SELECT * FROM "{table}" ORDER BY "{order}"')]
    raw=json.dumps(payload,ensure_ascii=False,default=str).encode("utf-8")
    return send_file(BytesIO(raw),mimetype="application/json",as_attachment=True,
                     download_name=f"bcit-backup-{date.today().isoformat()}.json")


@portal.get("/api/admin/portal/activity")
@admin_only
def activity():
    with db() as c:
        return jsonify([dict(r) for r in c.execute("SELECT * FROM admin_activity ORDER BY id DESC LIMIT 200")])

@portal.get("/api/admin/portal/due")
@admin_only
def due_students():
    with db() as c:
        rows=c.execute("""SELECT a.id,a.student_name,a.mobile,a.course,p.total_fee,
        COALESCE((SELECT SUM(amount) FROM fee_payments f WHERE f.admission_id=a.id AND f.status='verified'),0) paid
        FROM admissions a JOIN portal_students p ON p.admission_id=a.id WHERE p.deleted=0 AND p.active=1""").fetchall()
    return jsonify([{**dict(r),"pending":max((r["total_fee"] or 0)-r["paid"],0)} for r in rows if (r["total_fee"] or 0)>r["paid"]])


# Secure, admin-only reporting: XLSX (opens directly in Excel) and printable PDF.
@portal.get("/api/admin/portal/reports/<report_type>.<file_type>")
@admin_only
def export_portal_report(report_type, file_type):
    if report_type not in {"attendance", "fees", "results"} or file_type not in {"xlsx", "pdf"}:
        return jsonify(error="Unsupported report format."), 404
    queries = {
        "attendance": ("Attendance Register", ["Student ID", "Student Name", "Date", "Status"],
            "SELECT p.student_code,a.student_name,t.attendance_date,t.status FROM attendance t JOIN admissions a ON a.id=t.admission_id JOIN portal_students p ON p.admission_id=a.id ORDER BY t.attendance_date DESC,a.student_name"),
        "fees": ("Fee Collection Report", ["Student ID", "Student Name", "Amount (INR)", "Mode", "Status", "Reference", "Payment Date"],
            "SELECT p.student_code,a.student_name,f.amount,f.mode,f.status,f.reference,f.payment_date FROM fee_payments f JOIN admissions a ON a.id=f.admission_id JOIN portal_students p ON p.admission_id=a.id ORDER BY f.id DESC"),
        "results": ("Examination Results", ["Exam ID", "Exam", "Subject", "Student ID", "Student Name", "Marks", "Maximum Marks", "Pass Marks"],
            "SELECT e.exam_code,e.title,e.subject,p.student_code,a.student_name,m.marks,e.max_marks,e.pass_marks FROM exam_marks m JOIN exams e ON e.id=m.exam_id JOIN admissions a ON a.id=m.admission_id JOIN portal_students p ON p.admission_id=a.id ORDER BY e.exam_date DESC,a.student_name")
    }
    title, headers, query = queries[report_type]
    with db() as c:
        rows = [list(r) for r in c.execute(query).fetchall()]
    stamp = date.today().isoformat()
    if file_type == "xlsx":
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            return jsonify(error="Excel export dependency missing. Install openpyxl."), 503
        wb=Workbook(); ws=wb.active; ws.title=report_type.title()
        ws.append(["BAJRANG COMPUTER INSTITUTE OF TECHNOLOGY"]); ws.append([title]); ws.append([f"Generated: {stamp}"]); ws.append(headers)
        for row in rows: ws.append(row)
        for cell in ws[1]: cell.font=Font(bold=True,size=15,color="FFFFFF"); cell.fill=PatternFill("solid",fgColor="17365D")
        for cell in ws[2]: cell.font=Font(bold=True,size=12,color="17365D")
        for cell in ws[4]: cell.font=Font(bold=True,color="FFFFFF"); cell.fill=PatternFill("solid",fgColor="245BE7"); cell.alignment=Alignment(wrap_text=True)
        ws.freeze_panes="A5"; ws.auto_filter.ref=ws.dimensions
        for col in ws.columns:
            letter=get_column_letter(col[0].column)
            ws.column_dimensions[letter].width=min(max(max(len(str(x.value or "")) for x in col)+3,14),34)
        out=BytesIO(); wb.save(out); out.seek(0)
        return send_file(out,mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",as_attachment=True,download_name=f"bcit-{report_type}-{stamp}.xlsx")
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.units import mm
    out=BytesIO(); doc=SimpleDocTemplate(out,pagesize=landscape(A4),rightMargin=12*mm,leftMargin=12*mm,topMargin=12*mm,bottomMargin=12*mm)
    styles=getSampleStyleSheet(); story=[Paragraph("BAJRANG COMPUTER INSTITUTE OF TECHNOLOGY",styles["Title"]),Paragraph(title,styles["Heading2"]),Paragraph(f"Generated: {stamp} | Records: {len(rows)}",styles["Normal"]),Spacer(1,6*mm)]
    data=[headers]+[[str(v if v is not None else "") for v in row] for row in rows]
    table=Table(data,repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#17365D")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),8),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#D8E0EA")),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F2F6FC")]),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story.append(table); doc.build(story); out.seek(0)
    return send_file(out,mimetype="application/pdf",as_attachment=True,download_name=f"bcit-{report_type}-{stamp}.pdf")



@portal.post("/api/student/ai-assistant")
@student_only
def student_ai_assistant():
    """Student-scoped BCIT assistant; only the logged-in student's own records enter context."""
    question = str((request.get_json(silent=True) or {}).get("question") or "").strip()[:1500]
    if not question:
        return jsonify(error="Please enter your question."), 400
    api_key = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
    if not api_key or api_key.lower() in {"put-your-gemini-api-key-here", "your_actual_api_key", "your-api-key"}:
        return jsonify(error="The AI Assistant is not configured yet. The website administrator must set GEMINI_API_KEY in the server .env file."), 503

    student = request.student
    sid = student["id"]
    with db() as c:
        paid_row = c.execute("SELECT COALESCE(SUM(amount),0) paid FROM fee_payments WHERE admission_id=? AND status='verified'", (sid,)).fetchone()
        payment_rows = [dict(x) for x in c.execute("SELECT amount,mode,status,payment_date,payment_datetime,reference FROM fee_payments WHERE admission_id=? ORDER BY id DESC LIMIT 20", (sid,))]
        marks = [dict(x) for x in c.execute("SELECT e.title,e.subject,e.exam_date,e.max_marks,e.pass_marks,m.marks,CASE WHEN m.marks>=e.pass_marks THEN 'PASS' ELSE 'FAIL' END result FROM exam_marks m JOIN exams e ON e.id=m.exam_id WHERE m.admission_id=? ORDER BY e.id DESC", (sid,))]
        materials = [dict(x) for x in c.execute("SELECT title,kind,course,external_url,CASE WHEN file_name IS NOT NULL THEN 1 ELSE 0 END has_file FROM materials WHERE (course IS NULL OR TRIM(course)='' OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(course),' ',''),'-',''),'_','')) IN ('ALL','ALLCOURSES','*') OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(course),' ',''),'-',''),'_',''))=UPPER(REPLACE(REPLACE(REPLACE(TRIM(?),' ',''),'-',''),'_',''))) AND (admission_id IS NULL OR admission_id=?) ORDER BY id DESC LIMIT 60", (student.get("course"),sid))]
        assignments = [dict(x) for x in c.execute("SELECT a.title,a.description,a.course,a.due_date,CASE WHEN a.file_name IS NOT NULL THEN 1 ELSE 0 END question_file_attached,sub.original_name submitted_file,sub.submitted_at FROM assignments a LEFT JOIN submissions sub ON sub.assignment_id=a.id AND sub.admission_id=? WHERE (a.course IS NULL OR TRIM(a.course)='' OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(a.course),' ',''),'-',''),'_','')) IN ('ALL','ALLCOURSES','*') OR UPPER(REPLACE(REPLACE(REPLACE(TRIM(a.course),' ',''),'-',''),'_',''))=UPPER(REPLACE(REPLACE(REPLACE(TRIM(?),' ',''),'-',''),'_',''))) ORDER BY a.id DESC LIMIT 60", (sid,student.get("course")))]
        attendance = [dict(x) for x in c.execute("SELECT attendance_date,status FROM attendance WHERE admission_id=? ORDER BY attendance_date DESC LIMIT 30", (sid,))]
        attendance_pct = attendance_score(c,sid)
    total_fee = int(student.get("total_fee") or 0)
    paid_fee = int(paid_row["paid"] or 0)
    context = {
        "student": {k: student.get(k) for k in ("student_code","student_name","course","admission_date","completion_date")},
        "fees": {"total_course_fee":total_fee,"verified_paid":paid_fee,"pending_balance":max(0,total_fee-paid_fee),"recent_payments":payment_rows},
        "attendance_percent":attendance_pct,
        "results":marks,"study_materials":materials,"assignments":assignments,"recent_attendance":attendance,
        "institute": {"name":"Bajrang Computer Institute of Technology (BCIT)","phone":"+91 9708750305","email":"bcitcomputer.445@gmail.com","address":"Exact street address is not available in the configured website data; do not guess. Ask the student to check the Contact section or contact the institute."},
        "courses_and_fees_inr":{"ADCA":8000,"DCA":4000,"DFA":6000,"PGDCA":18000,"DTTP":6000},
    }
    system_prompt = ("You are BCIT's helpful student AI assistant. ALWAYS reply in clear, natural English only, even when the student asks in Hindi or Hinglish. Never reply in Hindi or Hinglish. "
      "Answer institute, course, computer-learning, study-material, assignment, exam-result, fee, attendance and admission questions. "
      "The JSON context below contains only the currently logged-in student's own private portal records; use it for personal questions and never imply access to other students. "
      "Do not invent missing marks, fees, due dates, addresses, course durations, eligibility or certificate recognition. If a file is attached, tell the student to open it in Study Material or Assignments; do not claim you have read its contents. "
      "Pending/unverified payments must not be counted as paid. Distinguish verified paid amount from pending balance. Do not reveal passwords, secrets or internal data. "
      "Be concise, friendly and give clear next steps. Institute contact: +91 9708750305, bcitcomputer.445@gmail.com. Course fee list (INR): ADCA 8000, DCA 4000, DFA 6000, PGDCA 18000, DTTP 6000. Exact institute street address/course durations must be confirmed with BCIT.\nSTUDENT CONTEXT JSON:\n" + json.dumps(context,ensure_ascii=False,default=str))
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip().removeprefix("models/") or "gemini-3.5-flash-lite"
    url = "https://generativelanguage.googleapis.com/v1beta/models/" + urllib.parse.quote(model,safe=".-") + ":generateContent"
    payload = json.dumps({"systemInstruction":{"parts":[{"text":system_prompt}]},"contents":[{"role":"user","parts":[{"text":question}]}],"generationConfig":{"temperature":0.25,"maxOutputTokens":650}}).encode("utf-8")
    req = urllib.request.Request(url,data=payload,headers={"Content-Type":"application/json","x-goog-api-key":api_key},method="POST")
    try:
        result = None
        last_http_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                last_http_error = exc
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        if result is None and last_http_error:
            raise last_http_error
        parts=(result.get("candidates") or [{}])[0].get("content",{}).get("parts",[])
        answer="\n".join(x.get("text","") for x in parts if x.get("text")).strip()
        if not answer: return jsonify(error="AI se jawab nahi mil saka. Thodi der baad dobara try karein."),502
        return jsonify(answer=answer)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        app_logger = getattr(__import__("logging").getLogger("werkzeug"), "warning", None)
        if app_logger:
            app_logger("Gemini student endpoint HTTP %s after retry: %s" % (exc.code, detail[:1200]))
        if exc.code in (429, 500, 502, 503, 504):
            return jsonify(error="Gemini is temporarily busy. Please try again in a minute."),503
        return jsonify(error="AI service error (HTTP %s). Check API key/model configuration." % exc.code),502
    except (urllib.error.URLError,TimeoutError):
        return jsonify(error="AI service se connection nahi ho paaya. Internet connection check karke dobara try karein."),502
    except Exception:
        return jsonify(error="The AI Assistant could not respond right now. Please try again later."),502

def register_portal(app, get_db_fn=None, storage_upload_fn=None, storage_bytes_fn=None,
                    storage_delete_fn=None, admin_activity_fn=None):
    global DB_FACTORY, _STORAGE_UPLOAD, _STORAGE_BYTES, _STORAGE_DELETE, _ADMIN_ACTIVITY
    DB_FACTORY = get_db_fn
    _STORAGE_UPLOAD = storage_upload_fn
    _STORAGE_BYTES = storage_bytes_fn
    _STORAGE_DELETE = storage_delete_fn
    _ADMIN_ACTIVITY = admin_activity_fn
    init_portal_db()
    app.register_blueprint(portal)

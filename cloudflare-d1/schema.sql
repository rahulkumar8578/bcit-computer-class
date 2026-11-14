-- Generated from original BCIT SQLite database.
PRAGMA foreign_keys=ON;
CREATE TABLE admissions (

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

            , reg_no TEXT, student_active INTEGER NOT NULL DEFAULT 1, student_deleted INTEGER NOT NULL DEFAULT 0, student_password_hash TEXT, total_course_fee INTEGER NOT NULL DEFAULT 0, completion_date TEXT);

CREATE TABLE assignments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT,
          file_name TEXT, course TEXT, due_date TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE attendance(
          id INTEGER PRIMARY KEY AUTOINCREMENT, admission_id INTEGER NOT NULL REFERENCES admissions(id),
          attendance_date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('present','absent')),
          UNIQUE(admission_id,attendance_date)
        );

CREATE TABLE attendance_batch_students(
          batch_id INTEGER NOT NULL REFERENCES attendance_batches(id) ON DELETE CASCADE,
          admission_id INTEGER NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
          PRIMARY KEY(batch_id,admission_id)
        );

CREATE TABLE attendance_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE enquiries (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                name TEXT NOT NULL,

                phone TEXT NOT NULL,

                email TEXT NOT NULL,

                course TEXT,

                message TEXT,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP

            );

CREATE TABLE exam_marks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          admission_id INTEGER NOT NULL REFERENCES admissions(id), marks REAL NOT NULL,
          UNIQUE(exam_id,admission_id)
        );

CREATE TABLE exams(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_code TEXT UNIQUE, title TEXT NOT NULL, subject TEXT NOT NULL,
          exam_date TEXT, exam_type TEXT NOT NULL DEFAULT 'offline', max_marks REAL NOT NULL DEFAULT 100,
          pass_marks REAL NOT NULL DEFAULT 33, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        , duration_minutes INTEGER NOT NULL DEFAULT 0);

CREATE TABLE fee_payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, admission_id INTEGER NOT NULL REFERENCES admissions(id),
          amount INTEGER NOT NULL CHECK(amount>0), mode TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', reference TEXT, payment_date TEXT NOT NULL,
          receipt_token TEXT UNIQUE NOT NULL, verified_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        , proof_file TEXT, payment_datetime TEXT);

CREATE TABLE materials(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT NOT NULL,
          file_name TEXT, external_url TEXT, course TEXT, admission_id INTEGER REFERENCES admissions(id), created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE online_exam_attempts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          admission_id INTEGER NOT NULL REFERENCES admissions(id) ON DELETE CASCADE,
          started_at TEXT NOT NULL, ends_at TEXT NOT NULL, submitted_at TEXT,
          answers_json TEXT NOT NULL DEFAULT '{}', marks REAL, total_marks REAL NOT NULL,
          status TEXT NOT NULL DEFAULT 'in_progress', UNIQUE(exam_id,admission_id)
        );

CREATE TABLE online_exam_questions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          question_no INTEGER NOT NULL, question_text TEXT NOT NULL, options_json TEXT NOT NULL,
          correct_option INTEGER NOT NULL CHECK(correct_option BETWEEN 0 AND 3), marks REAL NOT NULL DEFAULT 1
        );

CREATE TABLE portal_students(
          admission_id INTEGER PRIMARY KEY REFERENCES admissions(id) ON DELETE CASCADE,
          student_code TEXT UNIQUE, reg_no TEXT UNIQUE, active INTEGER NOT NULL DEFAULT 1,
          deleted INTEGER NOT NULL DEFAULT 0, total_fee INTEGER NOT NULL DEFAULT 0,
          completion_date TEXT, password_hash TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE submissions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id INTEGER NOT NULL REFERENCES assignments(id),
          admission_id INTEGER NOT NULL REFERENCES admissions(id), file_name TEXT NOT NULL,
          original_name TEXT, submitted_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(assignment_id,admission_id)
        );

CREATE UNIQUE INDEX idx_admissions_receipt_token ON admissions(receipt_token);

CREATE UNIQUE INDEX uq_admission_reg_no ON admissions(reg_no) WHERE reg_no IS NOT NULL AND reg_no<>'';

CREATE UNIQUE INDEX uq_online_reference ON fee_payments(reference)
          WHERE mode='upi' AND reference IS NOT NULL AND reference<>'';

CREATE UNIQUE INDEX uq_portal_student_code ON portal_students(student_code) WHERE student_code IS NOT NULL AND student_code<>'';


-- Worker session and direct-upload control tables
CREATE TABLE IF NOT EXISTS worker_sessions (
  id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','student')),
  expires_at INTEGER NOT NULL, revoked_at INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_worker_sessions_subject ON worker_sessions(subject_id, role);
CREATE TABLE IF NOT EXISTS pending_uploads (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, subject_id TEXT NOT NULL, role TEXT NOT NULL,
  purpose TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL,
  content_type TEXT NOT NULL, expected_size INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  completed_at INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pending_uploads_session ON pending_uploads(session_id, expires_at);
CREATE TABLE IF NOT EXISTS upload_objects (
  id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, role TEXT NOT NULL, purpose TEXT NOT NULL,
  object_key TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL, content_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- Flask file metadata and administrator activity are stored in D1 (not local SQLite).
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
CREATE INDEX IF NOT EXISTS idx_admin_activity_created ON admin_activity(created_at);

-- Additional non-unique indexes for frequent BCIT portal lookups.
-- Safe to apply to an existing D1 database; these do not modify records.
CREATE INDEX IF NOT EXISTS idx_admissions_profile_lookup
  ON admissions(mobile, dob, lower(email), id DESC);
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
CREATE INDEX IF NOT EXISTS idx_exams_type_lower_id
  ON exams(lower(exam_type), id DESC);
CREATE INDEX IF NOT EXISTS idx_online_exam_questions_exam_order
  ON online_exam_questions(exam_id, question_no);

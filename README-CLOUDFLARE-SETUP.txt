BCIT – Flask + Cloudflare D1 + Cloudflare R2
===========================================

The existing website frontend, templates, Flask routes and admission workflow are retained. The backend storage wiring has been changed as follows:

1. app.py get_db() now returns the Cloudflare D1 REST adapter.
2. portal_features.py uses the same D1 connection for student, fee, attendance, exam, course, assignment, material and submission records.
3. Admission photos and portal uploads are sent from Flask to the private R2 bucket.
4. File reads and deletes go through Flask; existing admin/student authorization is retained.
5. file_metadata and admin_activity are also stored in Cloudflare D1; no persistent local SQLite database is used.
6. No Cloudflare Worker is used as the application backend. Worker source/runtime files are not included in this package.

SETUP
-----
- Copy .env.example to .env beside app.py.
- Add your private D1 API token to CLOUDFLARE_API_TOKEN (D1 Read + D1 Write permissions).
- Add your R2 S3 Access Key ID and Secret Access Key to R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY. They need Object Read & Write permission for bcit-files.
- Install: python -m pip install -r requirements.txt
- Start: python app.py
- Test D1: open /api/health. It should report D1 connected.
- Test R2: submit a test admission photo, then upload/view a portal study material while logged in.

The Account ID, D1 database ID and bucket name in .env.example are prefilled from the BCIT setup values. Never share .env or place credentials in source code.

IMPORTANT LIMITATIONS / TEST STATUS
-----------------------------------
- Python syntax compilation and isolated mocked D1/R2 adapter tests passed during preparation.
- Full Flask startup and real Cloudflare requests were NOT tested in this environment: Flask/Werkzeug were not installed here and the user's private .env credentials are not available in this session.
- Therefore do not treat this ZIP as live-verified production deployment. Run the setup and live checks above before production use.
- D1 REST API commits each query; the adapter supports the existing sqlite-style API, but several separate execute() calls inside one Flask route are not one atomic transaction. Schema scripts are sent as D1 batch requests.
- Existing D1 data is not deleted. Startup schema changes are additive. No local SQLite database is required by this deployment.
- R2 files are private and served through Flask after the route's existing authorization checks.


VERCEL
------
A vercel.json is included for the Flask Python function. Import this folder/repository
into Vercel and add the required variables from .env.example in Project Settings >
Environment Variables. Set SECRET_KEY, ADMIN_PASSWORD, Cloudflare D1 credentials,
R2 S3 credentials, and optional GEMINI_API_KEY / UPI_ID. Never upload .env.
See CLOUDFLARE-SETUP-HINDI.txt for the deployment checklist.

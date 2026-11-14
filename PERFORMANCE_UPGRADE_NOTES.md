# BCIT Performance Upgrade Notes

## Changes in this build

1. D1 REST requests use a process-wide urllib3 connection pool, reusing HTTPS/TLS connections instead of opening a fresh connection for every SQL request. Pool size is configurable with `D1_HTTP_POOL_SIZE` (default 16, safe range 2-64) and `D1_HTTP_NUM_POOLS` (default 4, safe range 1-16).
2. R2 uses one cached boto3 S3 client per process/configuration, preserving the HTTP connection pool across uploads, downloads and deletes. Optional settings: `R2_HTTP_POOL_SIZE`, `R2_CONNECT_TIMEOUT`, `R2_READ_TIMEOUT`.
3. Added non-unique indexes for common profile, payment, attendance-related portal, exam, material, assignment and submission lookups. These are additive and do not rewrite student records.
4. Public static assets receive a one-hour browser cache. Private APIs, student data and payment responses are not made publicly cacheable.
5. Added `Server-Timing` and `X-BCIT-Response-Time-Ms` response headers for diagnosing Flask-side response time in browser DevTools.
6. Added a low-memory Gunicorn configuration: one worker and four threads, suitable as a cautious Render Free trial setting for I/O-bound requests.

## Deployment

- Keep your current `.env` file and Render environment variables. This package intentionally excludes `.env`, `.venv`, runtime uploads and Python cache files.
- On Render, set Start Command to: `gunicorn -c gunicorn.conf.py app:app`
- Do not change D1/R2 credentials or database IDs.
- First deploy to a test/staging service if available, then verify student login, admin login, admission, payment, receipt PDF, attendance, exams, uploads/downloads and AI Assistant.

## Important limits

- These changes reduce connection setup overhead and improve common indexed lookups; they do not remove the network round trip for each individual D1 `execute()` call.
- Render Free has limited CPU/RAM and no horizontal autoscaling. This package does not guarantee 1,000 simultaneous active users. Capacity must be measured with staged load tests.
- A live Cloudflare/Render benchmark could not be run from this editing environment because it does not have access to the user's private production credentials or deployment.

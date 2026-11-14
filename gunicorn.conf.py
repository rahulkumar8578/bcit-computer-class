"""Gunicorn settings for the BCIT Render Free trial.

One worker avoids multiplying the 512 MB memory footprint; a few threads allow
requests to overlap while waiting on D1/R2 network I/O. This is not a 1,000-user
capacity guarantee. Increase workers/instances only after measured load tests.
"""
import os

bind = "0.0.0.0:" + os.getenv("PORT", "10000")
workers = 1
worker_class = "gthread"
threads = 4
timeout = 120
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")

"""Cloudflare D1 + R2 adapters for the BCIT Flask application.

D1 is accessed only by Flask through Cloudflare's authenticated REST API.
R2 objects are private and are read/deleted only by the Flask backend.
All structured records, including file metadata and admin activity, are stored in Cloudflare D1.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib3
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a bounded integer setting; invalid environment values use defaults."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# Reuse HTTPS/TLS connections between Flask requests instead of opening a new
# urllib connection for every D1 SQL statement. A modest pool fits Render Free.
_D1_HTTP_POOL = urllib3.PoolManager(
    num_pools=_bounded_env_int("D1_HTTP_NUM_POOLS", 4, 1, 16),
    maxsize=_bounded_env_int("D1_HTTP_POOL_SIZE", 16, 2, 64),
    block=True,
    retries=False,
)


class D1Error(RuntimeError):
    pass


class D1Row(dict):
    """sqlite3.Row-like mapping supporting both row['name'] and row[0]."""
    def __getitem__(self, key):
        if isinstance(key, int):
            try:
                return list(self.values())[key]
            except IndexError:
                raise IndexError(key)
        return super().__getitem__(key)


class D1Cursor:
    def __init__(self, rows=None, lastrowid=None, rowcount=-1):
        self._rows = [D1Row(r) for r in (rows or [])]
        self._index = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self):
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row


class D1Connection:
    """Small sqlite-compatible facade over Cloudflare D1 REST query API."""
    def __init__(self):
        self.account_id = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or os.getenv("R2_ACCOUNT_ID") or "").strip()
        self.database_id = (os.getenv("D1_DATABASE_ID") or os.getenv("CLOUDFLARE_D1_DATABASE_ID") or "").strip()
        self.token = (os.getenv("CLOUDFLARE_API_TOKEN") or os.getenv("CLOUDFLARE_D1_API_TOKEN") or os.getenv("D1_API_TOKEN") or os.getenv("CF_API_TOKEN") or "").strip()
        missing = [name for name, value in (("CLOUDFLARE_ACCOUNT_ID", self.account_id), ("D1_DATABASE_ID", self.database_id), ("CLOUDFLARE_API_TOKEN", self.token)) if not value]
        if missing:
            raise D1Error("D1 configuration missing: " + ", ".join(missing) + ". Set these values in the private .env file.")
        self.endpoint = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/d1/database/{self.database_id}/query"
        self.timeout = int(os.getenv("D1_HTTP_TIMEOUT", "30"))
        self.row_factory = None

    def _post(self, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        timeout = urllib3.Timeout(
            total=self.timeout,
            connect=min(5, self.timeout),
            read=self.timeout,
        )
        response = None
        try:
            response = _D1_HTTP_POOL.request(
                "POST",
                self.endpoint,
                body=body,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=timeout,
                retries=False,
                preload_content=True,
            )
            raw = response.data or b"{}"
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise D1Error("Cloudflare D1 returned an invalid response.") from exc

            if response.status < 200 or response.status >= 300:
                detail = raw.decode("utf-8", "replace")[:1200]
                message = f"Cloudflare D1 HTTP {response.status}: {detail}"
                if any(x in detail.lower() for x in (
                    "unique constraint", "foreign key constraint",
                    "not null constraint", "check constraint"
                )):
                    raise sqlite3.IntegrityError(message)
                raise D1Error(message)
        except sqlite3.IntegrityError:
            raise
        except D1Error:
            raise
        except (urllib3.exceptions.HTTPError, TimeoutError, OSError) as exc:
            raise D1Error(f"Could not connect to Cloudflare D1: {exc}") from exc
        finally:
            if response is not None:
                response.release_conn()

        if not result.get("success", False):
            errors = result.get("errors") or []
            detail = "; ".join(str(e.get("message", e)) for e in errors) or "Unknown D1 API error"
            if any(x in detail.lower() for x in ("unique constraint", "foreign key constraint", "not null constraint", "check constraint")):
                raise sqlite3.IntegrityError(detail)
            raise D1Error(detail)
        query_results = result.get("result") or []
        for query_result in query_results:
            if query_result.get("success") is False:
                errors = query_result.get("errors") or []
                detail = "; ".join(str(e.get("message", e)) for e in errors) or "D1 query failed"
                if any(x in detail.lower() for x in ("unique constraint", "foreign key constraint", "not null constraint", "check constraint")):
                    raise sqlite3.IntegrityError(detail)
                raise D1Error(detail)
        return query_results

    def execute(self, sql, params=()):
        try:
            result_list = self._post({"sql": str(sql), "params": list(params or ())})
        except D1Error:
            raise
        result = result_list[0] if result_list else {}
        meta = result.get("meta") or {}
        rows = result.get("results") or []
        return D1Cursor(rows, meta.get("last_row_id"), int(meta.get("changes") if meta.get("changes") is not None else -1))

    def executemany(self, sql, parameter_sets):
        batch = [{"sql": str(sql), "params": list(params or ())} for params in parameter_sets]
        if not batch:
            return D1Cursor()
        results = self._post({"batch": batch})
        last_meta = (results[-1].get("meta") or {}) if results else {}
        return D1Cursor(lastrowid=last_meta.get("last_row_id"), rowcount=sum(int((r.get("meta") or {}).get("changes", 0)) for r in results))

    def execute_batch(self, statements):
        """Run independent SQL statements in one D1 HTTP batch request.

        Each item is a (sql, params) pair. The returned cursors preserve the
        statement order, so callers can fetch each result without extra D1
        round trips. Use this for independent reads/writes only; do not use it
        where Python code needs the result of one statement to build the next.
        """
        batch = [
            {"sql": str(sql), "params": list(params or ())}
            for sql, params in statements
        ]
        if not batch:
            return []
        results = self._post({"batch": batch})
        cursors = []
        for result in results:
            meta = result.get("meta") or {}
            cursors.append(D1Cursor(
                result.get("results") or [],
                meta.get("last_row_id"),
                int(meta.get("changes") if meta.get("changes") is not None else -1),
            ))
        return cursors

    def executescript(self, script):
        # Send schema statements as one D1 batch so DDL setup is ordered.
        statements, current = [], ""
        for line in str(script).splitlines():
            current += line + "\n"
            if sqlite3.complete_statement(current):
                statement = current.strip()
                if statement:
                    statements.append({"sql": statement, "params": []})
                current = ""
        if current.strip():
            statements.append({"sql": current.strip(), "params": []})
        if statements:
            self._post({"batch": statements})

    def commit(self):
        # D1 REST commits each query; batch calls are committed by D1 as a unit.
        return None

    def rollback(self):
        # A REST query already committed cannot be rolled back here.
        return None

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


def log_admin_activity(action: str, detail: str = ""):
    with D1Connection() as conn:
        conn.execute(
            "INSERT INTO admin_activity(action,detail) VALUES(?,?)",
            (str(action)[:120], str(detail)[:500]),
        )


_R2_CLIENT = None
_R2_CLIENT_CONFIG = None
_R2_CLIENT_LOCK = threading.Lock()


def get_r2_client():
    """Return one reusable, thread-safe boto3 client per process/configuration.

    Reusing the client preserves its underlying HTTP connection pool. Creating
    a new boto3 client for every upload/download discards that pool and adds
    avoidable DNS/TLS setup latency to repeated file operations.
    """
    global _R2_CLIENT, _R2_CLIENT_CONFIG
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("R2 support dependency missing. Install boto3.") from exc

    account = (os.getenv("R2_ACCOUNT_ID") or os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    endpoint = (os.getenv("R2_ENDPOINT_URL") or (f"https://{account}.r2.cloudflarestorage.com" if account else "")).strip()
    access = (os.getenv("R2_ACCESS_KEY_ID") or os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID") or os.getenv("R2_ACCESS_KEY") or "").strip()
    secret = (os.getenv("R2_SECRET_ACCESS_KEY") or os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY") or os.getenv("R2_SECRET_KEY") or "").strip()
    if not all((endpoint, access, secret)):
        raise RuntimeError("R2 configuration missing. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY in .env.")

    config_key = (endpoint, access, secret)
    if _R2_CLIENT is not None and _R2_CLIENT_CONFIG == config_key:
        return _R2_CLIENT

    with _R2_CLIENT_LOCK:
        if _R2_CLIENT is not None and _R2_CLIENT_CONFIG == config_key:
            return _R2_CLIENT
        pool_size = _bounded_env_int("R2_HTTP_POOL_SIZE", 16, 2, 64)
        client_config = Config(
            signature_version="s3v4",
            max_pool_connections=pool_size,
            connect_timeout=_bounded_env_int("R2_CONNECT_TIMEOUT", 5, 2, 20),
            read_timeout=_bounded_env_int("R2_READ_TIMEOUT", 30, 5, 120),
            retries={"max_attempts": 3, "mode": "standard"},
        )
        _R2_CLIENT = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id=access,
            aws_secret_access_key=secret, region_name="auto", config=client_config,
        )
        _R2_CLIENT_CONFIG = config_key
        return _R2_CLIENT


def r2_bucket():
    bucket = (os.getenv("R2_BUCKET_NAME") or os.getenv("R2_BUCKET") or os.getenv("CLOUDFLARE_R2_BUCKET") or "bcit-files").strip()
    if not bucket:
        raise RuntimeError("R2_BUCKET_NAME is not configured.")
    return bucket


def storage_upload(file_storage, object_key: str, category: str = "portal"):
    """Upload to R2, then write metadata to D1; clean up the object if metadata fails."""
    if not file_storage:
        raise ValueError("Please choose a file.")
    stream = file_storage.stream
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
    except Exception:
        size = 0
    content_type = file_storage.mimetype or "application/octet-stream"
    key = str(object_key).lstrip("/")
    client = get_r2_client()
    bucket = r2_bucket()
    client.upload_fileobj(stream, bucket, key, ExtraArgs={"ContentType": content_type})
    try:
        with D1Connection() as conn:
            conn.execute("""INSERT INTO file_metadata(object_key,original_filename,content_type,size_bytes,category)
                VALUES(?,?,?,?,?) ON CONFLICT(object_key) DO UPDATE SET
                original_filename=excluded.original_filename,content_type=excluded.content_type,
                size_bytes=excluded.size_bytes,category=excluded.category""",
                (key, file_storage.filename or "", content_type, int(size), str(category)[:80]))
    except Exception:
        try:
            client.delete_object(Bucket=bucket, Key=key)
        except Exception:
            pass
        raise
    return key


def storage_bytes(object_key: str) -> bytes:
    key = str(object_key or "").lstrip("/")
    if not key or ".." in Path(key).parts:
        raise FileNotFoundError("File not found.")
    try:
        response = get_r2_client().get_object(Bucket=r2_bucket(), Key=key)
        return response["Body"].read()
    except Exception as exc:
        # Do not leak provider credentials or raw S3 responses to clients.
        raise FileNotFoundError("Stored file is unavailable.") from exc


def storage_delete(object_key: str):
    key = str(object_key or "").lstrip("/")
    if not key or ".." in Path(key).parts:
        return False
    get_r2_client().delete_object(Bucket=r2_bucket(), Key=key)
    with D1Connection() as conn:
        conn.execute("DELETE FROM file_metadata WHERE object_key=?", (key,))
    return True


def storage_metadata(object_key: str):
    with D1Connection() as conn:
        row = conn.execute("SELECT * FROM file_metadata WHERE object_key=?", (object_key,)).fetchone()
        return dict(row) if row else None

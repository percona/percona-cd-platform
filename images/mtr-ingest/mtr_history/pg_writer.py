"""Load per-build JSON artefacts into PostgreSQL (PS-10541).

The relational counterpart to ``openmetrics_exporter``: it reads the same
per-build JSON documents but writes rows into ``builds`` + ``test_results``
instead of emitting OpenMetrics samples. Build start time becomes an ordinary
column, so historical builds load without the time-series recency limits that
made the Prometheus/Mimir model awkward.

Connection: pass an explicit libpq DSN, or rely on the standard
``PGHOST`` / ``PGPORT`` / ``PGDATABASE`` / ``PGUSER`` / ``PGPASSWORD`` env vars.
``psycopg`` is an optional dependency (the ``pg`` extra); import this module
only on the Postgres ingest path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import psycopg

# Reuse the URL parsing from the exporter so instance/job_name are derived
# identically on both the metrics and relational paths.
from .openmetrics_exporter import _instance_from_url, _job_name_from_url


def _parse_ts(iso: str) -> datetime:
    """ISO 8601 (optionally ``Z``-suffixed) -> aware UTC datetime."""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Open a connection from an explicit DSN or the libpq ``PG*`` env vars."""
    return psycopg.connect(dsn) if dsn else psycopg.connect()


def init_db(conn: psycopg.Connection) -> None:
    """Apply the idempotent schema DDL bundled in the wheel."""
    ddl = resources.files("mtr_history").joinpath("sql").joinpath("schema.sql").read_text()
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def existing_build_numbers(
    conn: psycopg.Connection, instance: str, job_name: str, limit: int | None = None
) -> set[int]:
    """Build numbers already stored for ``instance``/``job_name`` (idempotency).

    ``limit`` caps to the most recent N builds so callers (the CronJob's
    ``pg-skip-list``) don't emit an unbounded argv after a large backfill.
    """
    query = (
        "SELECT build_number FROM builds WHERE instance = %s AND job_name = %s "
        "ORDER BY build_number DESC"
    )
    params: list = [instance, job_name]
    if limit:
        query += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return {row[0] for row in cur.fetchall()}


def _ingest_build(conn: psycopg.Connection, data: dict) -> int:
    """UPSERT one build and replace its test rows. Returns the count of tests."""
    url = data["url"]
    instance = _instance_from_url(url)
    job_name = _job_name_from_url(url)
    plat = data.get("platform") or {}
    src = data.get("source") or {}

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO builds (instance, job_name, build_number, build_ts,
                duration_ms, result, display_name, platform_os, platform_build,
                arch, branch, fork, build_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (instance, job_name, build_number) DO UPDATE SET
                build_ts = EXCLUDED.build_ts, duration_ms = EXCLUDED.duration_ms,
                result = EXCLUDED.result, display_name = EXCLUDED.display_name,
                platform_os = EXCLUDED.platform_os, platform_build = EXCLUDED.platform_build,
                arch = EXCLUDED.arch, branch = EXCLUDED.branch, fork = EXCLUDED.fork,
                build_url = EXCLUDED.build_url, ingested_at = now()
            RETURNING build_id
            """,
            (
                instance, job_name, data["build_number"], _parse_ts(data["timestamp"]),
                data.get("duration_ms"), data.get("result"), data.get("display_name"),
                plat.get("docker_os"), plat.get("cmake_build_type"), plat.get("arch"),
                src.get("branch"), src.get("fork"), url,
            ),
        )
        build_id = cur.fetchone()[0]

        tests = data.get("tests") or []
        # A completed build's test set is immutable; replace it wholesale so a
        # re-ingest (e.g. --force) is exact rather than additive.
        cur.execute("DELETE FROM test_results WHERE build_id = %s", (build_id,))
        if tests:
            rows = [
                (
                    build_id, t["suite"], t["name"], t["run_context"], t["worker"],
                    bool(t["big"]), t["status"], t.get("time_s"),
                    t.get("failure_message") or None,
                )
                for t in tests
            ]
            cur.executemany(
                """
                INSERT INTO test_results (build_id, suite, name, run_context,
                    worker, big, status, duration_s, failure_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (build_id, suite, name, run_context, worker, big)
                DO UPDATE SET status = EXCLUDED.status,
                    duration_s = EXCLUDED.duration_s,
                    failure_message = EXCLUDED.failure_message
                """,
                rows,
            )
    return len(tests)


def ingest_builds(json_dir: Path, dsn: str | None = None, skip_existing: bool = True) -> dict:
    """Ingest every per-build JSON under ``json_dir``. One transaction per build.

    Tombstones (``result == "FETCH_ERROR"``) and malformed docs are skipped, not
    fatal. Returns a summary dict.
    """
    jsons = sorted(Path(json_dir).glob("*.json"))
    processed = skipped = errored = total_tests = 0

    with connect(dsn) as conn:
        seen: set[tuple[str, str, int]] = set()
        if skip_existing:
            with conn.cursor() as cur:
                cur.execute("SELECT instance, job_name, build_number FROM builds")
                seen = {(r[0], r[1], r[2]) for r in cur.fetchall()}

        for p in jsons:
            try:
                data = json.loads(p.read_text())
                if data.get("build_number") is None or data.get("result") == "FETCH_ERROR":
                    skipped += 1
                    continue
                key = (
                    _instance_from_url(data["url"]),
                    _job_name_from_url(data["url"]),
                    int(data["build_number"]),
                )
                if skip_existing and key in seen:
                    skipped += 1
                    continue
                total_tests += _ingest_build(conn, data)
                conn.commit()
                processed += 1
            except Exception as exc:  # noqa: BLE001 - resilient batch loader
                conn.rollback()
                errored += 1
                print(f"  x {p.name}: {exc}")

    return {"processed": processed, "skipped": skipped, "errored": errored, "tests": total_tests}

-- MTR test-history relational schema (PS-10541).
--
-- Idempotent: safe to run repeatedly. Applied by `mtr-backfill init-db` and by
-- the EKS schema-init Job. Build start time is an ordinary TIMESTAMPTZ column,
-- so historical builds load without any time-series recency limits (the whole
-- reason this replaces the Prometheus/Mimir model).

CREATE TABLE IF NOT EXISTS builds (
    build_id        BIGSERIAL PRIMARY KEY,
    instance        TEXT        NOT NULL,            -- "ps80.cd.percona.com"
    job_name        TEXT        NOT NULL,            -- "percona-server-8.0-pipeline-parallel-mtr"
    build_number    INTEGER     NOT NULL,
    build_ts        TIMESTAMPTZ NOT NULL,            -- Jenkins build start time (historical)
    duration_ms     BIGINT,
    result          TEXT,                            -- SUCCESS | UNSTABLE | FAILURE | ABORTED
    display_name    TEXT,
    platform_os     TEXT,                            -- "ubuntu:noble"
    platform_build  TEXT,                            -- Debug | RelWithDebInfo
    arch            TEXT,                            -- x86_64 | aarch64
    branch          TEXT,
    fork            TEXT,
    build_url       TEXT,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (instance, job_name, build_number)
);

CREATE TABLE IF NOT EXISTS test_results (
    id              BIGSERIAL PRIMARY KEY,
    build_id        BIGINT      NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE,
    suite           TEXT        NOT NULL,            -- "audit_log_filter"
    name            TEXT        NOT NULL,            -- "audit_api_message_emit"
    run_context     TEXT        NOT NULL,            -- regular | ci_fs | ps_protocol | unit_tests
    worker          TEXT        NOT NULL,            -- "WORKER_1"
    big             BOOLEAN     NOT NULL DEFAULT FALSE,
    status          TEXT        NOT NULL CHECK (status IN ('pass', 'fail', 'skip')),
    duration_s      REAL,
    failure_message TEXT,
    -- Test identity matches junit_parser: (suite, name, run_context); worker+big
    -- are attributes kept distinct so regular and -big runs never merge.
    UNIQUE (build_id, suite, name, run_context, worker, big)
);

-- History of a given test across builds.
CREATE INDEX IF NOT EXISTS idx_test_results_name  ON test_results (name, suite);
-- "What failed in build N" + the FK cascade.
CREATE INDEX IF NOT EXISTS idx_test_results_build ON test_results (build_id);
-- Flaky scans only ever look at failures; keep that slice small.
CREATE INDEX IF NOT EXISTS idx_test_results_fail  ON test_results (name, suite) WHERE status = 'fail';
-- Time-ordered build scans + dashboard time filter.
CREATE INDEX IF NOT EXISTS idx_builds_ts     ON builds (build_ts DESC);
CREATE INDEX IF NOT EXISTS idx_builds_branch ON builds (branch);

-- Denormalised join used by every dashboard panel (test row + its build dims).
CREATE OR REPLACE VIEW test_runs AS
SELECT
    t.id, t.suite, t.name, t.run_context, t.worker, t.big, t.status,
    t.duration_s, t.failure_message,
    b.build_id, b.instance, b.job_name, b.build_number, b.build_ts,
    b.result AS build_result, b.platform_os, b.platform_build, b.arch,
    b.branch, b.fork, b.build_url
FROM test_results t
JOIN builds b ON b.build_id = t.build_id;

-- Cross-build flakiness: a "flip" is a pass<->fail change between consecutive
-- runs of the same test (ordered by build start time) on the same platform.
-- flips > 0 surfaces sporadic failures and the fail-then-pass-on-retry cases
-- the Jenkins UI hides (PS-10541). Within-build retry capture is a follow-up.
CREATE OR REPLACE VIEW test_flakiness AS
WITH ordered AS (
    SELECT
        suite, name, run_context, platform_os, platform_build, arch, branch,
        build_ts, status,
        lag(status) OVER (
            PARTITION BY suite, name, run_context, platform_os, platform_build, arch, branch
            ORDER BY build_ts
        ) AS prev_status
    FROM test_runs
    WHERE status IN ('pass', 'fail')
)
SELECT
    suite, name, run_context, platform_os, platform_build, arch, branch,
    count(*)                                                AS runs,
    count(*) FILTER (WHERE status = 'fail')                 AS fails,
    count(*) FILTER (WHERE status = 'pass')                 AS passes,
    count(*) FILTER (WHERE prev_status IS NOT NULL
                        AND status <> prev_status)          AS flips,
    max(build_ts)                                           AS last_seen
FROM ordered
GROUP BY suite, name, run_context, platform_os, platform_build, arch, branch;

-- Grant read access to the Grafana datasource role. On EKS this role is created
-- declaratively by the CloudNativePG managed-roles block; here we just hand it
-- SELECT. Guarded so init-db succeeds whether or not the role exists yet (local
-- dev has no such role); the ingest CronJob re-runs init-db each tick, so the
-- grant lands once the role appears. No-op everywhere the role is absent.
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mtr_reader') THEN
    GRANT USAGE ON SCHEMA public TO mtr_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO mtr_reader;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mtr_reader;
  END IF;
END $$;

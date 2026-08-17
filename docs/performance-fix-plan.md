# Performance Fix Plan & PR Tracker

Companion to [`performance-scan-analysis.md`](performance-scan-analysis.md). Each row below
is intended to become **one upstream PR** (cherry-picked from the topic commit(s) on the
`perf/scan-analysis` branch). Fixes are ordered so that each PR is independently
reviewable, independently revertable, and does not depend on later ones.

Conventions:
- One topic = one commit on this branch = one future PR.
- Every commit must keep the full test suite green (`python -m pytest tests/`, 166 tests).
- This file is updated in the same commit as the fix it records.

## Status board

| PR | Topic | Findings | Status | Commit(s) |
|----|-------|----------|--------|-----------|
| PR-1 | SQLite concurrency configuration (WAL, busy_timeout, engine options) | F1 | **done** | see `git log --grep=PR-1` |
| PR-2 | Move identification/ingest file I/O out of write transactions; per-batch failure isolation | F3 | planned | |
| PR-3 | Lock convergence: rebuild re-entrancy guard, guarded scan entry, TOCTOU fix | F4 | planned | |
| PR-4 | Shop sections cache: stop the oversized-payload thrash loop | F12 | planned | |
| PR-5 | File watcher robustness: exception guard, drop O(E²) per-event stability scan | F5 | **done** | see `git log --grep=PR-5` |
| PR-6 | Identification retry backoff + orphan-trap fix | F2 | planned | |
| PR-7 | Dirty-tracked rebuild: scoped update_titles/add_missing_apps, set-based title cleanup, diff-only owned-flag sync | F6, F7, F8 | planned | |
| PR-8 | /api/titles name sort without full materialization | F11, F25 | planned | |
| PR-9 | TitleDB lookup: thread-local connections, identify_appId memoization, real LRU, meta_only probe memo | F7, F22, F29 | planned | |
| PR-10 | Index completeness (both creation tracks) + access_events retention | F16, F17, F19 | planned | |
| PR-11 | Hot-path hygiene: state-token TTL, media index, per-file log levels, extension check | F10, F24, F26, F27 | planned | |
| PR-12 | Downloads job: title-scoped size subquery | F18 | planned | |

Batch 3 items from the analysis (watcher queueing, mtime short-circuit scanning, TitleDB
description indexing, dedicated writer thread, shop `all` cap) are **not implemented here**
— they are larger structural changes best proposed upstream as issues first.

## Per-PR details

### PR-1 — SQLite concurrency configuration
- `app/db.py`: connect hook now sets `journal_mode=WAL`, `synchronous=NORMAL`,
  `busy_timeout=30000` alongside `foreign_keys=ON`.
- `app/app.py` (`create_app`): `SQLALCHEMY_ENGINE_OPTIONS` with
  `connect_args={"timeout": 30}`, `pool_size=20`, `max_overflow=30`, `pool_pre_ping` off
  (SQLite local file; not needed).
- Rationale/evidence: analysis F1. Removes the 5s `database is locked` cliff; WAL lets
  readers proceed during writes.
- Risk notes for reviewers: WAL creates `-wal`/`-shm` sidecar files next to the DB; WAL
  requires a filesystem with working shared memory (fine for the Docker volume layouts in
  README; avoid placing the DB itself on a network mount — that was already true before).

### PR-2 — Identification/ingest transactions no longer span file I/O
- `add_files_to_library`: `get_file_info` (stat/header reads) for the whole batch is
  gathered **before** the write transaction; DB inserts happen in one short transaction per
  chunk. A failed chunk commit is retried once, then logged and skipped — a lock error now
  costs one chunk, not the whole scan.
- `identify_library_files`: `identify_file` (container open + decrypt) runs with **no
  pending writes**; results are applied in a short write phase per batch.
- Evidence: F3; production log chain "Getting file info (n/250)" → locked → batch lost.

### PR-3 — Lock convergence
- `_run_post_library_change`: non-blocking re-entrancy guard; a second rebuild request
  while one runs is skipped (the debounce ensures a trailing run).
- New `run_guarded_scan()` used by: the scheduled job, `/api/library/scan`, and the
  download-manager callbacks — all scans now respect `scan_in_progress`.
- TOCTOU at the scan API fixed (flag set inside the lock).
- The direct `_run_post_library_change()` call in the delete-content API goes through the
  guarded/debounced entry.
- Evidence: F4 (five concurrent-writer combinations listed there).

### PR-4 — Shop sections cache
- Disk-cache hit path no longer re-serializes the payload to estimate its size (size is
  stored alongside the disk cache and carried in memory).
- Storing an oversized payload no longer clears the encrypted-response cache; encrypted
  responses are cached (bounded) even when the plaintext payload is disk-only.
- `SHOP_SECTIONS_MAX_IN_MEMORY_BYTES` default raised 4MiB → 64MiB.
- Web (`limit=N`) and CyberFoil (`limit=-1`) get separate disk cache files.
- Evidence: F12; production log `Skipping in-memory shop sections cache (15747019 bytes)`.

### PR-5 — File watcher robustness
- `_track_file`: `os.path.getsize` guarded (missing file → untrack, not thread death).
- Removed the unconditional per-event `_check_file_stability()` (the `@debounce(5)` path
  already covers it) — eliminates the O(E²) stat storm on mass events.
- `tracked_files` access serialized with a lock (observer thread vs debounce runner).
- Evidence: F5.

### PR-6 — Identification backoff + orphan trap
- `_get_identification_file_ids_batch` now honors `identification_attempts` /
  `last_attempt`: failed files retry with exponential backoff (base 2h, doubling to a 7-day
  cap) instead of every cycle. Successful identification resets the counter.
- Orphan trap fixed: a file whose contents all resolve to unknown app_ids is no longer
  marked `identified=True` with zero links; it stays unidentified and follows the same
  backoff (recovering automatically when TitleDB learns the app_id, via the attempts filter
  admitting it after backoff expiry).
- Evidence: F2.

### PR-7 — Dirty-tracked rebuild
- `identify_library_files` returns the set of affected title DB ids;
  `process_library_identification` aggregates them.
- `add_missing_apps_to_db(title_pks=...)` and `update_titles(title_pks=...)` accept a scope;
  `_run_post_library_change` passes the dirty set. Full sweeps still run when the scope is
  unknown (manual rebuild, downloads-triggered rescan) — behavior-compatible.
- `remove_titles_without_owned_apps` → one set-based DELETE (was 2 queries per title).
- `_sync_apps_owned_flags` updates only rows whose `owned` value actually changes (WHERE
  owned != computed), ending the full-table rewrite.
- Evidence: F6, F7, F8; benchmark: steady-state `update_titles` 20.9s → scoped no-op.

### PR-8 — /api/titles name sort
- The name-sort path no longer loads full ORM rows and no longer calls `get_game_info` per
  row; it sorts (title_id, app_id) pairs against the cached name map from
  `titles_metadata_cache` and pages **before** hydrating the page's rows.
- The metadata cache no longer deep-copies on every hit (read-only contract documented at
  the call sites).
- Evidence: F11, F25.

### PR-9 — TitleDB lookup layer
- Thread-local connection cache for the on-disk SQLite indexes (versions/cnmts/titles);
  connections close on TitleDB unload.
- `identify_appId` memoized (pure function of an immutable index).
- Lookup cache: capacity 4096 → 32768 and true LRU (move-to-end on hit).
- The `meta_only` capability probe result is memoized per-process (one failed probe, not
  one per file, on nsz builds without meta_only support).
- Evidence: F7 (connection churn), F22, F29.

### PR-10 — Index completeness + access_events retention
- `AccessEvents` model declares `at`/`kind` indexes; `ensure_performance_schema` backfills
  them plus `idx_files_identification_type` and the model-only files/apps indexes so both
  creation tracks (fresh `create_all` and upgraded Alembic DBs) converge on the same set.
- Retention: scheduled cleanup deletes access_events older than
  `AEROFOIL_ACCESS_EVENTS_RETENTION_DAYS` (default 90; 0 disables) in bounded batches.
- Evidence: F16, F17, F19.

### PR-11 — Hot-path hygiene
- State-token TTL 1s → 30s; `is_library_unchanged` uses the cached token (explicit
  invalidation already exists at every mutation site).
- Media cache index: one `os.listdir` refresh populating the whole index instead of a
  listdir-per-miss linear scan.
- Per-file "Getting file info"/"Identifying file" logs demoted to DEBUG, with INFO progress
  summaries every 500 files.
- `is_supported_content_path`: module-level import, precomputed suffix tuple, single
  `str.endswith(tuple)`.
- Evidence: F10, F24, F26, F27.

### PR-12 — Downloads job query
- `get_all_title_apps` size subquery scoped to the title's app_ids (was: aggregate over the
  entire app_files ⋈ files join, once per title, every 5 minutes).
- Evidence: F18.

## Upstream submission checklist (per PR)

1. Cherry-pick the topic commit(s) onto a fresh branch off upstream `master`.
2. Re-run `python -m pytest tests/` and the relevant benchmark stage.
3. Open the PR referencing the corresponding issue (file issues from the analysis doc's P0
   section first — one issue per finding, with the file:line evidence).
4. PR body: symptom → root cause (file:line) → fix → measured before/after.

# AeroFoil Performance Analysis: Very Large Libraries (Scan / Identify / Rebuild / Concurrency)

> Scope: libraries of 10k–100k files, possibly on NAS/network mounts. Concurrency sources:
> 32 waitress worker threads, 2-hourly scheduled scan, watchdog file watcher, download
> manager, Tinfoil/CyberFoil shop clients. Method: code audit of three subsystems (every
> finding carries file:line evidence) cross-checked with a 20k-file synthetic benchmark.
>
> Production failures this analysis explains: `database is locked` aborting scans, the same
> file batch being re-ingested after a completed full scan, a frozen Web UI, and
> `Skipping in-memory shop sections cache (15747019 bytes > 4194304 bytes)`.

## 0. Benchmark baseline (20k files / 10k titles, local disk, empty files, no keys)

| Stage | First run | Steady state (no changes) | Notes |
|---|---|---|---|
| File discovery `scan_library_path` | 16.0s | **0.30s** | diffing works; directory walk still full |
| Identification (filename fallback) | 66.5s (3.3ms/file, pure DB/CPU) | 0.02s | **never converges with keys loaded**, see F2 |
| `add_missing_apps_to_db` | 4.4s | same order | 2+N SQLite connections per title |
| `update_titles` full sweep | 20.2s | **20.9s (full sweep with zero changes)** | runs on every rebuild |
| `generate_library` | 7.5s | 0.20s (cache hit) | cache busts too easily, see F13 |
| TitleDB load | 4.2s, **+420MB RSS** | unloaded 30s after each rebuild, reloaded next time | US.en.json 89MB + cnmts ~100MB |
| 20k INFO log lines (format+write only) | 0.8s | — | worse through Docker pipes |
| `file_exists_in_db` single call | 0.39ms | — | called per file on watchdog mass events |

Real-world numbers amplify this empty-file baseline: identification opens the container and
AES-XTS-decrypts headers (NSP ≈ 10 scattered seeks / hundreds of KB; XCI decrypts a header
per NCA), 50ms–2s per file on network mounts.

---

## 1. P0 — Directly causes production failures

### F1 No SQLite concurrency configuration (root cause of `database is locked`)
- `app/db.py:663-670`: the only connect-time pragma is `PRAGMA foreign_keys=ON`. **No WAL,
  no busy_timeout, no synchronous tuning**; live DB verified at `journal_mode=delete`.
- No `SQLALCHEMY_ENGINE_OPTIONS` anywhere (`app/app.py:1845` area); pysqlite default
  `timeout=5.0` — exactly the observed "locked after ~5 seconds".
- Default pool (`pool_size=5, max_overflow=10` = 15 connections) serves 32 waitress threads
  + 4 scheduler workers + watchdog + debounce runner + download threads; also produces
  `QueuePool limit` timeouts.
- Ironically `app/titles.py:226-228` opens its own index DB with `timeout=30` +
  `synchronous=NORMAL`; the main DB gets neither.

In DELETE journal mode a writer holds RESERVED until commit, commit needs EXCLUSIVE which
any reader blocks, and readers block behind writers → 5s then error; meanwhile all 32
request threads queue up → frozen UI.

### F2 Identification "permanent retry trap" (second source of the rescan loop)
- `app/library.py:320-334`: candidate predicate is `OR(identified=False,
  identification_type='filename' [when keys loaded], orphaned)`.
- `identification_attempts` / `last_attempt` are **written at `library.py:484-485` and never
  read as a filter** — no backoff exists. A corrupt/keyless file is fully re-opened and
  re-decrypted every 2 hours, forever.
- **Orphan trap**: `titles.py:1237` `identify_appId()` returns `(None, None)` for unknown
  app_ids / missing cnmts index; `library.py:417/440` filters that out, yet `library.py:470`
  still sets `identified=True` with **zero app_files rows** → the file matches `orphaned`
  forever and is fully re-identified every cycle. 2,000 such files × ~0.5s ≈ **17 minutes of
  random network I/O per cycle producing zero state change**.
- With keys loaded, every `identification_type='filename'` file retries each cycle
  (successful upgrade to `'cnmt'` exits; persistent failure joins the permanent loop).

### F3 Long write transactions spanning slow file I/O (locks held for minutes)
- `app/library.py:27` `_IDENTIFY_COMMIT_INTERVAL=50`: the identify loop processes 50 files
  **inside one write transaction**, each doing container open + decrypt
  (`library.py:415`); the transaction acquires the write lock at the first autoflush
  (`library.py:419`) → minutes of lock hold on network mounts; every other writer times out
  at 5s (compounds with F1 into the production failure).
- Ingest path likewise: `library.py:222` `get_file_info` runs inside the transaction,
  committed only every 100 files (`library.py:238-241`). The production log sequence
  "Getting file info (n/250)" → locked error → whole batch rolled back → re-added next
  cycle is exactly this chain.

### F4 Locks guard flags, not work — multiple full jobs write the DB concurrently
- `scan_lock` / `library_rebuild_lock` / `titledb_update_lock` (`app.py:457-469`) only wrap
  bool/dict assignments; `_run_post_library_change` (`app.py:6536-6587`) takes the lock to
  flip a flag and **releases it before doing the rebuild**.
- Confirmed concurrent-writer combinations:
  1. download manager invokes `scan_cb()` directly every 30s / 5min
     (`manager.py:1531-1535`, `app.py:283-304`), **ignoring scan_lock entirely**;
  2. request threads call `check_completed_downloads(scan_cb=scan_library)`
     (`app.py:4395,4402`);
  3. `app.py:4377` calls `_run_post_library_change()` **directly, bypassing the debounce**,
     concurrently with the debounce runner's rebuild;
  4. the maintenance job only checks the conversion flag, not the scan flag
     (`app.py:397-420`), and both it and the scan job are `run_first=True` → **guaranteed
     concurrent at startup**;
  5. the watchdog observer thread deletes files one event at a time, one commit each
     (`file_watcher.py:145`, `db.py:1126-1140`).
- Plus a TOCTOU: `app.py:6606-6611` checks `scan_in_progress` inside the lock but sets it
  **outside** — two concurrent POSTs both pass.

### F5 Watchdog O(E²) stat storm on mass events + silent watcher-thread death
- `file_watcher.py:152`: **every event** synchronously calls `_check_file_stability()`,
  which walks all `tracked_files` doing 2 stats each (`:103-121`) — moving 10k files in
  ≈ **10⁸ stat syscalls** (hours on NFS); the debounced path (`:150`) is fully defeated by
  this line.
- `file_watcher.py:94` `os.path.getsize()` is **unguarded**: a file moved between event
  emission and dispatch raises FileNotFoundError inside watchdog's dispatch loop, killing
  the emitter thread — **file watching silently stops for the rest of the process
  lifetime**. Near-certain during mass moves.
- Delete events dispatch one callback per file (`:144-145`) → 4 queries + 1 commit per
  deleted file.

---

## 2. P1 — Full-cost work per cycle / per request

### F6 `update_titles`: unconditional full-table recompute per rebuild (measured 20s/10k titles)
`library.py:664-761`: batches of 500 over **all** titles, no dirty tracking; every title row
is rewritten (even when unchanged) and flushed. `_sync_apps_owned_flags`
(`library.py:1387-1404`) runs a correlated-subquery UPDATE over the **entire apps table** —
SQLite has no unchanged-value skip, so every cycle rewrites every page. Triggered by every
rebuild (~18 call sites).

### F7 `add_missing_apps_to_db`: 2+N SQLite connections per title, 30–50k per cycle
`library.py:586-607`: per title, `get_all_existing_versions` (1 conn) +
`get_all_existing_dlc` (1 conn) + one `get_all_app_existing_versions` per DLC.
`titles.py:1293/1542/1581/1619` each do `sqlite3.connect → PRAGMA → query → close`, no
pooling or reuse. It also inserts rows for every *known* version/DLC regardless of
ownership — the source of `apps` table bloat that amplifies F6/F10.

### F8 `remove_titles_without_owned_apps`: textbook N+1
`db.py:1018-1038`: loads all Titles ORM objects then per-title `has_owned_apps`, which
**re-queries the Title the caller already holds** → 2×T round trips (16k at 10k titles).
Runs at the top of every rebuild (`library.py:669`). Expressible as one
`DELETE ... WHERE NOT EXISTS`.

### F9 Full os.walk every 2 hours, no mtime short-circuit anywhere
`library.py:120-125` is a clean generator (no sorting, no per-file stat) — the problem is
the missing mechanism: `Libraries.last_scan` is written at `library.py:306` and **never read
anywhere**. 100k files on a network mount ≈ 10k+ readdir round trips (50–200s+) even when
nothing changed. `scan_library` walks all libraries serially (`app.py:6633-6637`).

### F10 State-token fingerprint: 18 aggregate subqueries incl. full-table LENGTH math, 1s TTL
`library.py:782-839`: expression aggregates like
`SUM((LENGTH(filepath)+LENGTH(filename)+size)*((id%131)+1))` cannot use indexes and scan
files/apps/titles/app_files in full. TTL `_LIBRARY_STATE_TOKEN_CACHE_TTL_S=1.0`
(`library.py:39`), and `is_library_unchanged` bypasses the cache with `force_refresh=True`
(`:886`). It sits on the shop request path (`app.py:560/606/630/5981`) — under sustained
traffic this **full-table scan runs roughly once per second**.

### F11 `/api/titles` default sort materializes the whole table + 10k TitleDB lookups
`app.py:5503-5531`: default `title_asc` → `use_name_sort` → `query.order_by(None).all()`
loads **every** matching row, then calls `get_game_info` per distinct title/DLC, sorts in
Python, then slices. The `per_page` cap (200) buys nothing — work happens before the slice.
The non-name-sort path (`:5533`) does proper offset/limit and simply isn't the default.
**Triggered by every library page load / filter change.** It also unconditionally calls
`_get_discovery_sections` (`:5643`), dragging F12 into the same request.

### F12 The 15MB shop payload enters a worse-than-no-cache loop
- `SHOP_SECTIONS_MAX_IN_MEMORY_BYTES` defaults to **4MiB** (`app.py:910`); oversized
  payloads null the memory cache (`:967-983`). Every subsequent request: disk `json.load`
  of 15MB (`:6195`) → `json.dumps` of 15MB just to re-measure the size (`:950-954,966`) →
  **clears the encrypted-response cache as a side effect** (`:983`) → full compress+encrypt
  (`:661`). The encrypted cache is structurally unable to hit (the clear precedes the store).
- The payload is 15MB because `SHOP_SECTIONS_ALL_ITEMS_CAP` defaults to `None` (`:908`);
  the `all` section carries everything and duplicates the other sections.
- Web (limit=50) and CyberFoil (limit=-1) cache keys share **one disk file**
  (`:6166,6198`), evicting each other on alternating traffic; the rebuild pre-warms only
  the 50 key (`:6581`), so CyberFoil's first request always pays a full rebuild.

### F13 `generate_library` cache busts too easily + full-rebuild cost
`library.py:870-887`: the token includes `CONFIG_FILE` mtime — **any settings change in the
UI forces a full 10k-title regeneration** (measured 7.5s + 10k TitleDB connections,
`:990`); so does any single added file. Meanwhile TitleDB updates are **not** in the token,
diverging from the shop/metadata caches (which use the titledb-aware token) — metadata can
drift stale.

### F14 TitleDB memory and reload cycling: 0.4–0.5GB per load, continuously on active instances
`titles.py:999-1036`: description + screenshot-URL dicts are built for **every TitleDB
entry** (tens of thousands, not just owned titles) from a full `json.load` of US.en.json
(89MB); `versions.txt` also fully resident. `unload_titledb` is `@debounce(30)` (`:1105`)
→ 30s after each rebuild TitleDB unloads and both lookup caches are cleared
(`:1143-1146`); the next request reloads everything. On an active instance this 4s +
0.4GB load loops continuously. (cnmts/versions/region titles are correctly converted to
on-disk SQLite indexes — that part of the design is right.)

### F15 Synchronous DB writes on every request (shop traffic amplifier)
- `app.py:2195/2262-2268`: `before_request` → `_touch_client` updates the user row and
  **commits on every shop request with a username**, no dedupe; failures are swallowed and
  manifest as latency.
- `db.py:423`: the access-events buffer flush runs **inline on the request thread** (≥64
  rows or ≥1s) — roughly once a second some request eats a commit; during a scan that means
  a full 5s stall. Download-count flush is the same shape (`db.py:344-378`, one UPDATE per
  file_id in a Python loop).
- Both commit the **shared request-scoped session**, committing/rolling back any unrelated
  pending ORM changes as a side effect (`db.py:312,315`; `app.py:2268,2272`).

### F16 access_events: zero indexes on fresh installs, no retention
- The model declares no indexes (`db.py:233-252`); they exist only in migration
  `2c9d2a6e3b41:37-38`. Fresh installs run `db.create_all()` + `stamp head`
  (`db.py:673-677`) so **the migration never executes and the indexes are never created**
  (verified empirically: zero indexes on the live DB).
- `ORDER BY at DESC LIMIT n` → full scan + sort; the activity page reads up to 10k rows,
  CSV export more.
- **No automatic retention**; the manual clear is one unbounded DELETE holding EXCLUSIVE
  across millions of rows.
- Also: the admin activity GET triggers `flush(force=True)` — a read endpoint performs a
  write+commit (`db.py:430`).

### F17 Index "dual track": create_all and Alembic each missing half
`ensure_performance_schema` (`db.py:544-583`) backfills only 5 indexes; the model-only
indexes `idx_files_library_id/filename/identified`, `idx_apps_owned/app_id` are absent from
the migrations — **upgraded old DBs lack them** (`iter_library_file_paths` etc. become full
scans); fresh DBs have them but lack the access_events ones. Neither path yields the full
intended index set.

### F18 Downloads job O(N²): unfiltered aggregate subquery × once per title
`db.py:930-966` `get_all_title_apps` builds its size subquery as a GROUP BY over the
**entire** `app_files ⋈ files` join before filtering by title; `manager.py:937-949` calls it
once per title every 5 minutes → 20k titles = 20k full aggregations. Same shape at
`manager.py:1297`, `library.py:765`.

---

## 3. P2 — Secondary but real

| # | Issue | Location |
|---|---|---|
| F19 | Identification candidate query: OR predicate defeats indexes, 2 full scans/cycle; `identification_type` unindexed; the count query full-scans just to feed a log line | `library.py:320-334,358`; `db.py:900-905` |
| F20 | Identify-loop N+1: per-id `session.get` after batch id fetch (expunge defeats identity map); `file not in existing_app.files` hydrates the whole collection | `library.py:398,446,452` |
| F21 | Scheduler reschedules at **dispatch** time: >2h scans overlap themselves; `update_titledb_job` has no in-progress guard; 4-worker pool starvable | `scheduler.py:76-87,13` |
| F22 | `meta_only` capability probe never memoized: on an nsz build lacking it, **every file** pays a failed open + full container parse (5–20× I/O) | `titles.py:1205-1211` |
| F23 | 6-hourly missing-files sweep stats every DB row (fully redundant with the scan) | `db.py:1142-1190` |
| F24 | Media cache index cleared on every rebuild → next ~10k icon requests each listdir+linear-scan a 10k-entry dir (~10⁸ ops) | `app.py:224-229,6575` |
| F25 | Metadata cache deep-copies O(T) on every **hit** (10k-entry name map + all genre sets) | `app.py:804-812` |
| F26 | Per-file INFO logging with eager f-strings: 200k+ lines per full pass (measured 0.8s/20k lines; worse via Docker pipes) | `library.py:220,414,431` |
| F27 | `is_supported_content_path`: in-function import + ~12 string allocations per candidate; called for every walked file incl. non-game files | `utils.py:126-150` |
| F28 | Scan materializes all paths into a set (100k ≈ 25MB resident for the whole walk) | `library.py:267` |
| F29 | TitleDB lookup LRU capped at 4096 < realistic library size, FIFO (no reorder on hit) → near-zero hit rate at scale; cleared on TitleDB unload | `titles.py:134,1320-1324` |
| F30 | `load_settings()` per TitleDB lookup → 10k+ stat syscalls per full pass | `titles.py:1388,206-209` |
| F31 | Dead code: `get_files_to_identify` (the triple-materialization variant) and its helpers remain; catastrophic if ever reintroduced | `library.py:336-341`; `db.py:809-813,860` |

---

## 4. Production failure post-mortem (causal chain)

```
[2-hourly] TitleDB update + scan ──┐
[30s/5min] download jobs call scan directly ──┼── concurrent writers (F4)
[watchdog] per-file delete + commit ──┘        │
                                               ▼
     identify/ingest transactions hold locks for minutes (F3) ←→ DELETE journal + 5s timeout (F1)
                                               │
                     "database is locked" → whole scan job aborts
                                               │
        uncommitted batch rolled back → next cycle re-runs "Getting file info" (observed)
                                               │
     meanwhile: 15MB shop payload > 4MB cap (F12) → per-request 15MB read+serialize+encrypt
     + /api/titles full materialization (F11) + per-request sync writes (F15)
                                               ▼
                 all 32 threads blocked on locks/IO → frozen UI (observed)
```

## 5. Fix roadmap

**Batch 1 · Hemostatic (small diffs, eliminates the production failures)**
1. WAL + busy_timeout: connect hook gains `journal_mode=WAL`, `synchronous=NORMAL`,
   `busy_timeout=30000`; set `SQLALCHEMY_ENGINE_OPTIONS`
   (`connect_args={'timeout':30}`, `pool_size=20`, `max_overflow=30`).
2. Move identification/ingest I/O out of write transactions: gather results first, then a
   short batched write; a failure drops one file, not the whole scan.
3. Lock convergence: `_run_post_library_change` holds the rebuild lock for its whole run and
   returns early on re-entry; download-manager and `app.py:4377` go through guarded
   entry points; fix the `app.py:6611` TOCTOU.
4. Shop cache line-level fixes: skip the size re-estimate on the disk-hit path, never clear
   the encrypted cache when the payload is oversized, raise the default in-memory cap to
   64MB; separate disk files for web vs CyberFoil.
5. `file_watcher.py:94` gets try/except; drop the per-event `_check_file_stability()` call
   at `:152` (trust the debounced path).

**Batch 2 · Incrementalization (eliminates per-cycle full-cost work)**
6. Identification backoff: actually filter on `identification_attempts/last_attempt`
   (exponential backoff + cap); fix the orphan trap (no `identified=True` with zero
   content, or a terminal failure state).
7. Dirty tracking for `update_titles` / `_sync_apps_owned_flags` / `add_missing_apps_to_db`
   (only titles touched by this cycle); `remove_titles_without_owned_apps` → single
   `DELETE WHERE NOT EXISTS`.
8. `/api/titles` default sort pushed into SQL (or served from a cached name map instead of
   per-row `get_game_info`).
9. Thread-local reuse of TitleDB index connections; memoize `identify_appId`; raise the
   lookup LRU to ≥32k with true LRU behavior.
10. Index completeness (both tracks): `identification_type`, access_events `(at)`/`(kind)`,
    the five model-only files/apps indexes; access_events retention job (batched deletes).
11. Replace the state-token fingerprint with an explicit version counter (bumped on write);
    raise the TTL to ≥30s.

**Batch 3 · Structural (optional)**
12. Watcher events into a queue consumed by one background thread (batched stats, batched
    deletes, single transaction).
13. mtime short-circuit for scans: compare directory mtimes + `last_scan`, descend only
    into changed subtrees.
14. Index TitleDB descriptions/images into SQLite, ending the 0.4GB residency and the 30s
    unload/reload cycle.
15. Move access-event/download-count flushes to a dedicated writer thread; zero synchronous
    writes on request threads.
16. Default cap for the shop `all` section; paginate/stream for very large libraries.

---

*Benchmark script: synthetic 20k-file library, per-stage timing; see section 0 and the
commit history of this branch.*
*Fix progress is tracked in `docs/performance-fix-plan.md`.*

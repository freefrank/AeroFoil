#!/usr/bin/env bash
# Post batch-1 issues to upstream. Requires: gh auth login
set -euo pipefail
cd "$(dirname "$0")"

gh issue create --repo luketanti/aerofoil --title "[Bug] \"database is locked\" aborts library scans and freezes the web UI (SQLite runs without WAL/busy_timeout)" --body-file issue-1.md
gh issue create --repo luketanti/aerofoil --title "[Bug] Same files re-ingested every scan cycle after a lock error (write transaction held while reading/decrypting files)" --body-file issue-2.md
gh issue create --repo luketanti/aerofoil --title "[Bug] Failing or orphaned files are fully re-identified on every scan cycle forever (no retry backoff)" --body-file issue-3.md
gh issue create --repo luketanti/aerofoil --title "[Bug] Scheduled scan, download jobs and rebuilds can all write the database at the same time (locks only guard status flags)" --body-file issue-4.md
gh issue create --repo luketanti/aerofoil --title "[Bug] File watching silently stops during mass copies/moves (unhandled FileNotFoundError kills the watchdog thread; per-event stability scan is O(n²))" --body-file issue-5.md

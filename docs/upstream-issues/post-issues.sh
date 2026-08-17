#!/usr/bin/env bash
# Post batch-1 issues to upstream. Requires: gh auth login
set -euo pipefail
cd "$(dirname "$0")"

gh issue create --repo luketanti/aerofoil --title "database is locked during library scans: SQLite needs WAL + busy_timeout + a larger pool" --body-file issue-1.md
gh issue create --repo luketanti/aerofoil --title "Scan/identify hold a write transaction across container decryption — minutes-long DB locks" --body-file issue-2.md
gh issue create --repo luketanti/aerofoil --title "No retry backoff in identification: failing/orphaned files are fully re-decrypted every scan cycle" --body-file issue-3.md
gh issue create --repo luketanti/aerofoil --title "scan_lock/library_rebuild_lock don't prevent concurrent scans and rebuilds" --body-file issue-4.md
gh issue create --repo luketanti/aerofoil --title "Watchdog handler dies silently on vanished files and stat-storms on mass copies/moves" --body-file issue-5.md

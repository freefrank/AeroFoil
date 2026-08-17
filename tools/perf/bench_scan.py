#!/usr/bin/env python3
"""Synthetic benchmark: scan pipeline behavior with a 20k-file library."""
import logging
import os
import random
import resource
import sys
import time

sys.path.insert(0, '/home/user/AeroFoil')

SCRATCH = os.path.dirname(os.path.abspath(__file__))
SYNTH = os.path.join(SCRATCH, 'synthlib')
os.environ['AEROFOIL_DB_FILE'] = os.path.join(SCRATCH, 'bench.db')

N_TITLES = 10000  # each title gets base + update -> 20k files

def build_library():
    if os.path.exists(SYNTH):
        return sum(len(f) for _, _, f in os.walk(SYNTH))
    os.makedirs(SYNTH)
    rng = random.Random(42)
    for i in range(N_TITLES):
        tid = f"0100{rng.getrandbits(48):012X}"
        base_id = tid[:-3] + '000'
        upd_id = tid[:-3] + '800'
        d = os.path.join(SYNTH, f"Game {i:05d}")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, f"Game {i:05d} [{base_id}][v0].nsp"), 'w').close()
        open(os.path.join(d, f"Game {i:05d} [{upd_id}][v65536].nsp"), 'w').close()
    return N_TITLES * 2

def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

def timed(label, fn):
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    print(f"[BENCH] {label}: {dt:.2f}s (peak RSS {rss_mb():.0f} MB)", flush=True)
    return out, dt

nfiles = build_library()
print(f"[BENCH] synthetic library ready: {nfiles} files", flush=True)

# Silence per-file INFO logging for a fair timing baseline, measured separately below.
from app.app import app
import app.app as app_module
app_module.reload_conf()
from app.db import db, add_library, get_library_id, Files
from app import library as lib
from app import titles as titles_lib

logging.getLogger('library').setLevel(logging.WARNING)
logging.getLogger('titles').setLevel(logging.WARNING)

with app.app_context():
    db.create_all()
    add_library(SYNTH)
    lid = get_library_id(SYNTH)
    print(f"[BENCH] library id {lid}", flush=True)

    timed('initial scan (add 20k files)', lambda: lib.scan_library_path(SYNTH))
    print(f"[BENCH] Files rows: {db.session.query(Files).count()}", flush=True)
    timed('steady-state rescan (no changes)', lambda: lib.scan_library_path(SYNTH))

    timed('titles.load_titledb (no titledb files present)', titles_lib.load_titledb)
    timed('identification pass 1 (no keys -> filename)', lambda: lib.identify_library_files(SYNTH))
    timed('identification pass 2 (steady state)', lambda: lib.identify_library_files(SYNTH))
    timed('add_missing_apps_to_db', lib.add_missing_apps_to_db)
    timed('update_titles full sweep', lib.update_titles)
    timed('update_titles again (nothing changed)', lib.update_titles)
    timed('generate_library cold', lib.generate_library)
    timed('generate_library warm (cache)', lib.generate_library)

    # Cost of the per-file INFO logging alone: re-log 20k lines to a null handler.
    log = logging.getLogger('library')
    log.setLevel(logging.INFO)
    import io
    h = logging.StreamHandler(io.StringIO())
    log.addHandler(h)
    t0 = time.perf_counter()
    for n in range(20000):
        log.info(f'Getting file info ({n+1}/20000): /Game/Game [0100000000000000][v0].nsp')
    print(f"[BENCH] 20k INFO log lines alone: {time.perf_counter()-t0:.2f}s", flush=True)

    # file_exists_in_db lookup cost (used by watchdog path with check_existing=True)
    some = [r.filepath for r in db.session.query(Files).limit(500).all()]
    t0 = time.perf_counter()
    for p in some:
        lib.file_exists_in_db(p)
    print(f"[BENCH] 500x file_exists_in_db: {time.perf_counter()-t0:.3f}s -> per-call {(time.perf_counter()-t0)/500*1000:.2f}ms", flush=True)

print('[BENCH] done', flush=True)

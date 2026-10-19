"""Measure the core on a temporary snapshot, without models or platform calls.

Run with AstrBot's Python: python tools/benchmark_resources.py --data-dir <plugin data>
Only aggregate measurements are printed. The original files are never opened for writing.
This is an isolated core benchmark, not attribution of a running AstrBot process.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


def rss_kib():
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return None


def worker(args):
    if sys.platform.startswith("linux"):
        import resource
        ceiling = args.memory_mib * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    initial = rss_kib()
    sys.path.insert(0, str(args.source_root))
    started = time.perf_counter()
    from core.config_store import ConfigStore
    from core.db import AsyncDatabase
    from core.engine import MyWorldEngine
    imported = rss_kib()
    store = ConfigStore(args.data_dir)
    store.ensure_files()
    db = AsyncDatabase(store.db_path)
    engine = MyWorldEngine(store=store, db=db)
    initialized = rss_kib()
    sessions = engine.enabled_session_ids()

    async def exercise():
        checkpoints = []
        for cycle in range(args.rounds + 1):
            for session in sessions:
                with engine.config_scope(session):
                    state = await engine.load_state(session, cold_start=False)
                    engine.memory.recall(session_id=session, persona_id="", node_id=state.node_id,
                                         keyword="resource benchmark", record_hits=False)
            if cycle in {0, args.rounds // 2, args.rounds}:
                gc.collect()
                checkpoints.append({"round": cycle, "rss_kib": rss_kib()})
        await db.close()
        return checkpoints

    checkpoints = asyncio.run(exercise())
    print(json.dumps({"scope": "isolated_core_without_models_or_sandbox",
                      "address_space_limit_mib": args.memory_mib, "sessions": len(sessions),
                      "rounds": args.rounds, "baseline_rss_kib": initial,
                      "after_import_rss_kib": imported, "after_init_rss_kib": initialized,
                      "checkpoints": checkpoints, "duration_seconds": round(time.perf_counter()-started, 3)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--memory-mib", type=int, default=384)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 10000 or not 128 <= args.memory_mib <= 2048:
        parser.error("rounds must be 1..10000 and memory-mib must be 128..2048")
    if args.worker:
        worker(args)
        return
    source = args.data_dir.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="my-world-resources-") as temporary:
        target = Path(temporary)
        for name in ("world.json", "sessions.json", "schedules.json"):
            path = source / name
            if path.exists():
                shutil.copyfile(path, target / name)
        original = source / "state.db"
        if original.exists():
            from contextlib import closing
            with closing(sqlite3.connect(original.as_uri()+"?mode=ro", uri=True, timeout=3)) as src:
                with closing(sqlite3.connect(target / "state.db")) as dst:
                    src.backup(dst, pages=128, sleep=.02)
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", "--data-dir", str(target),
                        "--source-root", str(args.source_root.resolve()), "--rounds", str(args.rounds),
                        "--memory-mib", str(args.memory_mib)],
                       check=True, timeout=120)


if __name__ == "__main__":
    main()

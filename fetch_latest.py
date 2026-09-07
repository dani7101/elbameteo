"""Scarica la run ECMWF più recente che risulta completa sull'API."""
import argparse
import json
import logging
import time
from datetime import datetime, timedelta

import ecmwf9
import elbameteo as core


def recent_runs(now=None, lookback_hours=24):
    now = now or datetime.now(core.UTC)
    run = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    oldest = now - timedelta(hours=lookback_hours)
    runs = []
    while run >= oldest:
        runs.append(run)
        run -= timedelta(hours=6)
    return runs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--max-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.lookback_hours <= 0 or args.max_seconds <= 0:
        parser.error("lookback-hours e max-seconds devono essere positivi")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = core.load_config(args.config)
    cid, document = core.config_id(config)
    db = core.connect(config)
    try:
        with db:
            db.execute("INSERT OR IGNORE INTO configurations VALUES (?,?)", (cid, document))
        ecmwf9.init_db(db)
        expected = len(config["locations"])
        deadline = time.monotonic() + args.max_seconds
        for run in recent_runs(lookback_hours=args.lookback_hours):
            label = core.stamp(run)
            core.LOG.info("Verifico la run candidata %s", label)
            ecmwf9.collect(config, db, cid, [run], deadline=deadline)
            count = db.execute(
                "SELECT COUNT(*) FROM downloads9 WHERE config_id=? AND run=?", (cid, label)
            ).fetchone()[0]
            if count == expected:
                print(json.dumps({"run": label, "locations": count, "hours": config["horizon_hours"] + 1}))
                return 0
            acquired = {row[0] for row in db.execute(
                "SELECT location FROM downloads9 WHERE config_id=? AND run=?", (cid, label)
            )}
            missing = [p["id"] for p in config["locations"] if p["id"] not in acquired]
            core.LOG.warning("Run %s: salvati %s/%s punti; mancanti: %s. Provo la precedente",
                             label, count, expected, ", ".join(missing))
    finally:
        db.close()
    raise RuntimeError(f"Nessuna run ECMWF completa acquisita nelle ultime {args.lookback_hours} ore; "
                       "i punti validi restano salvati per il prossimo tentativo")


if __name__ == "__main__":
    raise SystemExit(main())

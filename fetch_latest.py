"""Scarica la run ECMWF più recente che risulta completa sull'API."""
import argparse
import json
import logging
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = core.load_config(args.config)
    cid, document = core.config_id(config)
    db = core.connect(config)
    try:
        with db:
            db.execute("INSERT OR IGNORE INTO configurations VALUES (?,?)", (cid, document))
        ecmwf9.init_db(db)
        expected = len(config["locations"])
        for run in recent_runs(lookback_hours=args.lookback_hours):
            label = core.stamp(run)
            core.LOG.info("Verifico la run candidata %s", label)
            ecmwf9.collect(config, db, cid, [run])
            count = db.execute(
                "SELECT COUNT(*) FROM downloads9 WHERE config_id=? AND run=?", (cid, label)
            ).fetchone()[0]
            if count == expected:
                print(json.dumps({"run": label, "locations": count, "hours": config["horizon_hours"] + 1}))
                return 0
            core.LOG.info("Run %s non completa (%s/%s punti); provo la precedente", label, count, expected)
    finally:
        db.close()
    raise RuntimeError("Nessuna run ECMWF completa trovata nelle ultime 24 ore")


if __name__ == "__main__":
    raise SystemExit(main())

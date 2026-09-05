"""Archivio locale ECMWF IFS Open Data, griglia 0.25 gradi, vento a 10 m."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import sqlite3
import statistics
import tempfile
import time

UTC = timezone.utc
LOG = logging.getLogger("elbameteo")


def stamp(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_run(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    if dt.hour not in (0, 6, 12, 18) or dt.minute or dt.second or dt.microsecond:
        raise ValueError("La run deve essere alle 00/06/12/18 UTC esatte")
    return dt


def steps_for(run):
    return list(range(0, 145, 3)) + (list(range(150, 361, 6)) if run.hour in (0, 12) else [])


def wind(u, v):
    speed = math.hypot(u, v)
    # Direzione meteorologica DI PROVENIENZA; calma: direzione non significativa.
    direction = math.degrees(math.atan2(-u, -v)) % 360 if speed >= 0.5 else None
    return speed, direction


def angle_delta(new, old):
    return None if new is None or old is None else (new - old + 180) % 360 - 180


def speeds_in_kmh(row):
    """Tutte le velocita esposte in km/h; archivio e calcoli interni in SI."""
    return {
        key[:-3] + "_kmh" if key.endswith("_ms") else key:
        (value * 3.6 if value is not None else None) if key.endswith("_ms") else value
        for key, value in row.items()
    }


def quantile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q
    left = int(position)
    right = min(left + 1, len(values) - 1)
    return values[left] + (values[right] - values[left]) * (position - left)


def summarize(rows):
    speeds = [r["speed_ms"] for r in rows]
    gusts = [r["gust_ms"] for r in rows if r["gust_ms"] is not None]
    angles = [math.radians(r["direction_deg"]) for r in rows if r["direction_deg"] is not None]
    resultant = None
    if angles:
        resultant = math.hypot(sum(math.cos(a) for a in angles), sum(math.sin(a) for a in angles)) / len(angles)
    sd = statistics.pstdev(speeds)
    relative_spread = sd / max(statistics.mean(speeds), 2.0)
    # Indicatore empirico versione 1, NON probabilita calibrata di correttezza.
    confidence = "non_disponibile"
    if len(rows) >= 20:
        confidence = "bassa"
        if relative_spread <= 0.25 and resultant is not None and resultant >= 0.85 and len(angles) >= 0.8 * len(rows):
            confidence = "alta"
        elif relative_spread <= 0.5 and resultant is not None and resultant >= 0.6 and len(angles) >= 0.8 * len(rows):
            confidence = "media"
    return dict(n_members=len(rows), speed_mean_ms=statistics.mean(speeds),
                speed_p10_ms=quantile(speeds, .1), speed_p50_ms=quantile(speeds, .5),
                speed_p90_ms=quantile(speeds, .9), speed_sd_ms=sd,
                gust_p10_ms=quantile(gusts, .1) if gusts else None,
                gust_p50_ms=quantile(gusts, .5) if gusts else None,
                gust_p90_ms=quantile(gusts, .9) if gusts else None,
                direction_resultant=resultant, directional_members=len(angles),
                confidence=confidence, confidence_method="ensemble_spread_v1")


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    locations = config["locations"]
    if not locations or len({p["id"] for p in locations}) != len(locations):
        raise ValueError("Localita vuote o id duplicati")
    for p in locations:
        if not p["id"] or not -90 <= p["lat"] <= 90 or not -180 <= p["lon"] <= 180:
            raise ValueError(f"Localita non valida: {p}")
    if not 0 <= config["ensemble_members"] <= 50:
        raise ValueError("ensemble_members deve essere tra 0 e 50")
    if config["poll_minutes"] <= 0 or not 6 <= config["lookback_hours"] <= 96 or config["publication_delay_hours"] < 0:
        raise ValueError("Intervallo, ritardo o finestra di recupero non validi")
    config["database"] = str(path.parent / config["database"])
    return config


def connect(config):
    path = Path(config["database"])
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS batches (
        config_id TEXT, run TEXT, product TEXT, step INTEGER,
        fetched_at TEXT, source TEXT, sha256 TEXT,
        PRIMARY KEY(config_id, run, product, step));
      CREATE TABLE IF NOT EXISTS forecasts (
        config_id TEXT, run TEXT, product TEXT, step INTEGER, member INTEGER,
        location TEXT, valid_time TEXT, requested_lat REAL, requested_lon REAL,
        grid_lat REAL, grid_lon REAL, distance_km REAL, u_ms REAL, v_ms REAL,
        speed_ms REAL, direction_deg REAL, gust_ms REAL,
        gust_start TEXT, gust_end TEXT,
        PRIMARY KEY(config_id, run, product, step, member, location));
      CREATE INDEX IF NOT EXISTS forecasts_valid ON forecasts(location,valid_time);
      CREATE TABLE IF NOT EXISTS configurations (
        id TEXT PRIMARY KEY, document TEXT NOT NULL);
    """)
    return db


def config_id(config):
    # Snapshot scientifico: un cambio di punti/membri non riscrive la storia.
    identity = dict(locations=config["locations"], members=config["ensemble_members"],
                    model="ifs", resolution="0p25", sampling="nearest", version=1)
    if config.get("forecast_provider") == "open_meteo_ecmwf_9km":
        identity.update(provider=config["forecast_provider"], resolution="O1280_9km",
                        horizon_hours=config["horizon_hours"], hourly_policy="native90_then_interpolated",
                        ensemble_resolution="0p25", version=2)
    document = json.dumps(identity, sort_keys=True)
    return hashlib.sha256(document.encode()).hexdigest()[:16], document


def decode(path, run, step, product, locations, members):
    import eccodes as ec
    data = {}
    expected_members = [0] if product == "fc" else list(range(1, members + 1))
    with open(path, "rb") as handle:
        while (gid := ec.codes_grib_new_from_file(handle)) is not None:
            try:
                param = int(ec.codes_get(gid, "paramId"))
                name = {165: "u", 166: "v", 49: "gust", 228028: "gust", 228029: "gust"}.get(param)
                if name is None:
                    raise ValueError(f"Parametro inatteso: {param}")
                origin = f"{int(ec.codes_get(gid, 'dataDate')):08d}{int(ec.codes_get(gid, 'dataTime')):04d}"
                if origin != run.strftime("%Y%m%d%H%M") or int(ec.codes_get(gid, "endStep")) != step:
                    raise ValueError("Run o scadenza GRIB diversa dalla richiesta")
                if ec.codes_get(gid, "stepUnits") not in (1, "h"):
                    raise ValueError("Unita temporale GRIB non oraria")
                if ec.codes_get(gid, "typeOfLevel") != "heightAboveGround" or ec.codes_get(gid, "level") != 10:
                    raise ValueError("Campo non riferito a 10 metri")
                for key in ("iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees"):
                    if not math.isclose(float(ec.codes_get(gid, key)), .25, abs_tol=1e-6):
                        raise ValueError("Griglia diversa da 0.25 gradi: aggiornare il programma")
                member = int(ec.codes_get(gid, "perturbationNumber")) if product == "pf" else 0
                if member not in expected_members:
                    raise ValueError("Membro ensemble inatteso")
                start = int(ec.codes_get(gid, "startStep"))
                for p in locations:
                    nearest = ec.codes_grib_find_nearest(gid, p["lat"], p["lon"] % 360, npoints=1)[0]
                    value = float(nearest["value"])
                    if not math.isfinite(value) or abs(value) >= 9999:
                        raise ValueError("Valore mancante o invalido")
                    key = (member, p["id"])
                    record = data.setdefault(key, {})
                    if name in record:
                        raise ValueError("Campo duplicato nel GRIB")
                    grid = (float(nearest["lat"]), (float(nearest["lon"]) + 180) % 360 - 180)
                    if "grid" in record and record["grid"] != grid:
                        raise ValueError("Griglie vento e raffiche non coincidenti")
                    record.update(grid=grid, distance=float(nearest["distance"]))
                    record[name] = value
                    if name == "gust":
                        if ec.codes_get(gid, "stepType") != "max" or not 0 <= start < step:
                            raise ValueError("Intervallo raffica non valido")
                        record["gust_start"] = stamp(run + timedelta(hours=start))
            finally:
                ec.codes_release(gid)
    rows = []
    valid = stamp(run + timedelta(hours=step))
    for member in expected_members:
        for p in locations:
            r = data.get((member, p["id"]), {})
            if not {"u", "v"}.issubset(r) or (step > 0 and "gust" not in r):
                raise ValueError(f"Campi incompleti: membro {member}, {p['id']}, step {step}")
            speed, direction = wind(r["u"], r["v"])
            rows.append(dict(run=stamp(run), product=product, step=step, member=member,
                             location=p["id"], valid_time=valid, requested_lat=p["lat"], requested_lon=p["lon"],
                             grid_lat=r["grid"][0], grid_lon=r["grid"][1], distance_km=r["distance"],
                             u_ms=r["u"], v_ms=r["v"], speed_ms=speed, direction_deg=direction,
                             gust_ms=r.get("gust"), gust_start=r.get("gust_start"), gust_end=valid if step else None))
    return rows


def save_batch(db, cid, run, product, step, rows, source, digest):
    with db:
        if db.execute("SELECT 1 FROM batches WHERE config_id=? AND run=? AND product=? AND step=?",
                      (cid, stamp(run), product, step)).fetchone():
            return
        for row in rows:
            row = dict(config_id=cid, **row)
            columns = ",".join(row)
            db.execute(f"INSERT INTO forecasts ({columns}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
        db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?)",
                   (cid, stamp(run), product, step, stamp(datetime.now(UTC)), source, digest))


def collect(config, db, cid, runs, selected_steps=None, only_product=None):
    from ecmwf.opendata import Client
    client = Client(source=config["source"], model="ifs", resol="0p25", infer_stream_keyword=False,
                    maximum_retries=2, retry_after=5, source_accept_ranges=True,
                    source_accept_multiple_ranges=False)
    original_request = client.session.request

    def bounded_request(method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (15, 120)
        return original_request(method, url, **kwargs)

    client.session.request = bounded_request
    failures = 0
    for run in runs:
        if run < datetime(2026, 5, 13, tzinfo=UTC):
            raise ValueError("Questa versione supporta il catalogo dal ciclo IFS 50r1 (13 maggio 2026)")
        steps = steps_for(run) if selected_steps is None else selected_steps
        if not set(steps).issubset(steps_for(run)):
            raise ValueError("Scadenze non disponibili per questa run")
        products = ["fc"] + (["pf"] if config["ensemble_members"] else [])
        if only_product:
            products = [only_product]
        for product in products:
            if product == "pf" and not config["ensemble_members"]:
                raise ValueError("Ensemble disabilitato nella configurazione")
            for step in steps:
                if db.execute("SELECT 1 FROM batches WHERE config_id=? AND run=? AND product=? AND step=?",
                              (cid, stamp(run), product, step)).fetchone():
                    continue
                request = dict(date=run.strftime("%Y%m%d"), time=run.hour, step=step,
                               stream="oper" if product == "fc" else "enfo", type=product,
                               param=["10u", "10v"] + (["10fg"] if step else []))
                if product == "pf":
                    request["number"] = list(range(1, config["ensemble_members"] + 1))
                try:
                    LOG.info("Scarico %s %s +%sh", stamp(run), product, step)
                    with tempfile.TemporaryDirectory(prefix="elbameteo-") as temp:
                        path = Path(temp) / "wind.grib2"
                        client.retrieve(**request, target=str(path))
                        rows = decode(path, run, step, product, config["locations"], config["ensemble_members"])
                        with path.open("rb") as handle:
                            digest = hashlib.file_digest(handle, "sha256").hexdigest()
                        save_batch(db, cid, run, product, step, rows, config["source"], digest)
                except Exception:
                    failures += 1
                    LOG.exception("Batch non acquisito; riprovo al prossimo ciclo")
                    # Evita decine di richieste a una run non ancora pubblicata/scaduta.
                    break
    return failures


def candidate_runs(config, now=None):
    now = now or datetime.now(UTC)
    end = now - timedelta(hours=config["publication_delay_hours"])
    start = now - timedelta(hours=config["lookback_hours"])
    run = end.replace(hour=(end.hour // 6) * 6, minute=0, second=0, microsecond=0)
    runs = []
    while run >= start:
        runs.append(run)
        run -= timedelta(hours=6)
    return runs  # Prima le run recenti, poi il recupero delle lacune.


def export_rows(db, cid):
    raw = db.execute("SELECT * FROM forecasts WHERE config_id=? ORDER BY run,step,location,product,member", (cid,))
    groups = {}
    for r in raw:
        groups.setdefault((r["run"], r["step"], r["location"]), []).append(dict(r))
    for records in groups.values():
        control = next((r for r in records if r["product"] == "fc"), None)
        ensemble = [r for r in records if r["product"] == "pf"]
        row = {k: records[0][k] for k in ("run", "valid_time", "step", "location", "grid_lat", "grid_lon")}
        for key in ("speed_ms", "direction_deg", "gust_ms", "gust_start", "gust_end"):
            row["control_" + key] = control[key] if control else None
        row.update({key: None for key in summarize([dict(speed_ms=0, gust_ms=None, direction_deg=None)])})
        row["confidence"] = "non_disponibile"
        if ensemble:
            row.update(summarize(ensemble))
        yield speeds_in_kmh(row)


def compare_rows(db, cid, older, newer, location):
    rows = db.execute("""SELECT a.*, b.step AS new_step, b.speed_ms AS new_speed,
        b.direction_deg AS new_direction, b.gust_ms AS new_gust,
        b.gust_start AS new_gust_start, b.gust_end AS new_gust_end
        FROM forecasts a JOIN forecasts b ON a.config_id=b.config_id
        AND a.location=b.location AND a.valid_time=b.valid_time AND a.product=b.product
        AND a.member=b.member WHERE a.config_id=? AND a.run=? AND b.run=?
        AND a.product='fc' AND a.location=? ORDER BY a.valid_time""",
        (cid, stamp(older), stamp(newer), location))
    for r in rows:
        comparable = r["gust_start"] is not None and r["gust_start"] == r["new_gust_start"] and r["gust_end"] == r["new_gust_end"]
        yield speeds_in_kmh(dict(location=location, valid_time=r["valid_time"], older_run=stamp(older), newer_run=stamp(newer),
                   old_lead_h=r["step"], new_lead_h=r["new_step"], old_speed_ms=r["speed_ms"], new_speed_ms=r["new_speed"],
                   delta_speed_ms=r["new_speed"] - r["speed_ms"],
                   delta_direction_deg=angle_delta(r["new_direction"], r["direction_deg"]),
                   old_gust_ms=r["gust_ms"], new_gust_ms=r["new_gust"], gust_intervals_match=comparable,
                   delta_gust_ms=r["new_gust"] - r["gust_ms"] if comparable else None))


def write_csv(rows, path):
    rows = iter(rows)
    first = next(rows, None)
    if first is None:
        raise ValueError("Nessun dato corrispondente nell'archivio")
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(first))
        writer.writeheader()
        writer.writerow(first)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("fetch", help="Acquisizione singola, riprendibile")
    once.add_argument("--run", help="YYYY-MM-DDTHH:00Z; omesso: recupero ultime 72 ore")
    once.add_argument("--steps", type=int, nargs="+", help="Solo per test; omesso: intero orizzonte")
    once.add_argument("--product", choices=["fc", "pf"], help="Solo per test; omesso: entrambi")
    sub.add_parser("watch", help="Controllo ciclico fino a Ctrl+C")
    sub.add_parser("status", help="Completezza per run e prodotto")
    export = sub.add_parser("export", help="CSV vento controllo e statistiche ensemble")
    export.add_argument("--output", default="previsioni.csv")
    compare = sub.add_parser("compare", help="Differenze nuovo-meno-vecchio, stessa validita")
    compare.add_argument("--older", required=True)
    compare.add_argument("--newer", required=True)
    compare.add_argument("--location", required=True)
    compare.add_argument("--output", default="confronto.csv")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    cid, document = config_id(config)
    db = connect(config)
    with db:
        db.execute("INSERT OR IGNORE INTO configurations VALUES (?,?)", (cid, document))
    try:
        if config.get("forecast_provider") == "open_meteo_ecmwf_9km":
            from ecmwf9 import execute
            return execute(args, config, db, cid)
        if args.command == "fetch":
            runs = [parse_run(args.run)] if args.run else candidate_runs(config)
            return 1 if collect(config, db, cid, runs, args.steps, args.product) else 0
        if args.command == "watch":
            while True:
                collect(config, db, cid, candidate_runs(config))
                LOG.info("Ciclo terminato; prossimo controllo fra %s minuti", config["poll_minutes"])
                time.sleep(config["poll_minutes"] * 60)
        elif args.command == "status":
            print(f"Configurazione: {cid}; archivio: {config['database']}")
            counts = {(r["run"], r["product"]): r["n"] for r in db.execute(
                "SELECT run,product,COUNT(*) n FROM batches WHERE config_id=? GROUP BY run,product", (cid,))}
            if not counts:
                print("Nessun batch acquisito per questa configurazione")
            for run in sorted({key[0] for key in counts}):
                expected = len(steps_for(parse_run(run)))
                for product in ["fc"] + (["pf"] if config["ensemble_members"] else []):
                    n = counts.get((run, product), 0)
                    print(f"{run} {product}: {n}/{expected} scadenze; " + ("completa" if n == expected else "PARZIALE"))
        elif args.command == "export":
            write_csv(export_rows(db, cid), args.output)
        elif args.command == "compare":
            older, newer = parse_run(args.older), parse_run(args.newer)
            if older >= newer:
                raise ValueError("La run newer deve essere successiva a older")
            write_csv(compare_rows(db, cid, older, newer, args.location), args.output)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nRaccolta interrotta; i batch completati restano salvati.")
    except Exception as exc:
        LOG.error("%s", exc)
        raise SystemExit(1)

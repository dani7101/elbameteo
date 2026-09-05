"""ECMWF IFS 9 km: singole run, archivio orario fino a +144 ore."""
from datetime import datetime, timedelta
import hashlib
import json
import math
import time

import requests

import elbameteo as core

ENDPOINT = "https://single-runs-api.open-meteo.com/v1/forecast"
VARIABLES = ("wind_speed_10m", "wind_direction_10m", "wind_gusts_10m")


def api_get(params):
    """Ritenta i rallentamenti e gli errori temporanei del servizio meteo."""
    for attempt in range(4):
        try:
            response = requests.get(ENDPOINT, params=params, timeout=(30, 180))
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 3:
                raise
            core.LOG.warning("API meteo temporaneamente irraggiungibile (%s); nuovo tentativo", exc)
            time.sleep(2 ** attempt)


def init_db(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS hourly9 (
        config_id TEXT, run TEXT, location TEXT, step INTEGER, valid_time TEXT,
        grid_lat REAL, grid_lon REAL, speed_ms REAL, direction_deg REAL,
        gust_ms REAL, native_time_step INTEGER,
        PRIMARY KEY(config_id,run,location,step));
      CREATE TABLE IF NOT EXISTS downloads9 (
        config_id TEXT, run TEXT, location TEXT, fetched_at TEXT,
        request_json TEXT, response_json TEXT, sha256 TEXT,
        PRIMARY KEY(config_id,run,location));
    """)


def normalize(payload, run, horizon):
    hourly = payload["hourly"]
    if payload["utc_offset_seconds"] != 0:
        raise ValueError("La risposta deve essere in UTC")
    units = payload["hourly_units"]
    if any(units[k] != "m/s" for k in ("wind_speed_10m", "wind_gusts_10m")):
        raise ValueError("Unita velocita inattesa")
    if units["wind_direction_10m"] != "°":
        raise ValueError("Unita direzione inattesa")
    times = hourly["time"]
    if any(len(hourly[k]) != len(times) for k in VARIABLES) or len(set(times)) != len(times):
        raise ValueError("Serie temporale incoerente")
    indexed = dict(zip(times, zip(*(hourly[k] for k in VARIABLES))))
    rows = []
    for step in range(horizon + 1):
        valid = run + timedelta(hours=step)
        values = indexed.get(valid.strftime("%Y-%m-%dT%H:%M"))
        if values is None:
            raise ValueError(f"Manca la scadenza +{step}h")
        speed, direction, gust = values
        for name, value in zip(VARIABLES, values):
            if name == "wind_gusts_10m" and step == 0 and value is None:
                continue
            if value is None or not math.isfinite(value) or value < 0:
                raise ValueError(f"Dato assente/non valido: {name} +{step}h")
        if not 0 <= direction <= 360:
            raise ValueError("Direzione non valida")
        rows.append(dict(step=step, valid_time=core.stamp(valid), grid_lat=payload["latitude"],
                         grid_lon=payload["longitude"], speed_ms=speed,
                         direction_deg=direction % 360 if speed >= .5 else None,
                         gust_ms=gust if step else None,
                         native_time_step=int(step <= 90 or step % 3 == 0)))
    return rows


def collect(config, db, cid, runs):
    errors = 0
    for run in runs:
        for location in config["locations"]:
            key = (cid, core.stamp(run), location["id"])
            if db.execute("SELECT 1 FROM downloads9 WHERE config_id=? AND run=? AND location=?", key).fetchone():
                continue
            params = dict(latitude=location["lat"], longitude=location["lon"], models="ecmwf_ifs",
                          hourly=",".join(VARIABLES), run=run.strftime("%Y-%m-%dT%H:%M"),
                          forecast_days=7, wind_speed_unit="ms", timezone="GMT",
                          cell_selection="nearest", elevation="nan")
            try:
                core.LOG.info("ECMWF 9 km: %s %s, +0..144h", key[1], key[2])
                response = api_get(params)
                rows = normalize(response.json(), run, config["horizon_hours"])
                # Una run/localita e completa solo quando tutte le 145 scadenze sono valide.
                with db:
                    if db.execute("SELECT 1 FROM downloads9 WHERE config_id=? AND run=? AND location=?", key).fetchone():
                        continue
                    db.executemany("INSERT INTO hourly9 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   [(*key, *r.values()) for r in rows])
                    db.execute("INSERT INTO downloads9 VALUES (?,?,?,?,?,?,?)",
                               (*key, core.stamp(datetime.now(core.UTC)), json.dumps(params), response.text,
                                hashlib.sha256(response.content).hexdigest()))
            except Exception:
                errors += 1
                core.LOG.exception("Run/localita non completata; riprovo al prossimo ciclo")
                break
    return errors


def export_rows(db, cid):
    ensembles = {}
    for row in db.execute("SELECT * FROM forecasts WHERE config_id=? AND product='pf'", (cid,)):
        ensembles.setdefault((row["run"], row["location"], row["step"]), []).append(dict(row))
    for record in db.execute("SELECT * FROM hourly9 WHERE config_id=? ORDER BY run,location,step", (cid,)):
        row = dict(record)
        row["model"] = "ECMWF IFS HRES"
        row["resolution_km"] = 9
        row["temporal_origin"] = "native_schedule" if row.pop("native_time_step") else "interpolated_by_provider"
        row["gust_interval"] = "provider_processed_not_reported_per_sample"
        members = ensembles.get((row["run"], row["location"], row["step"]))
        summary = core.summarize(members) if members else {k: None for k in core.summarize([
            dict(speed_ms=0, direction_deg=None, gust_ms=None)])}
        if not members:
            summary.update(confidence="non_disponibile", n_members=0)
        row.update(summary)
        row["ensemble_resolution_deg"] = .25 if members else None
        yield core.speeds_in_kmh(row)


def compare_rows(db, cid, older, newer, location):
    rows = db.execute("""SELECT a.*, b.step new_step, b.speed_ms new_speed,
        b.direction_deg new_direction, b.gust_ms new_gust, b.native_time_step new_native
        FROM hourly9 a JOIN hourly9 b ON a.config_id=b.config_id AND a.location=b.location
        AND a.valid_time=b.valid_time WHERE a.config_id=? AND a.run=? AND b.run=?
        AND a.location=? ORDER BY a.valid_time""", (cid, core.stamp(older), core.stamp(newer), location))
    for row in rows:
        yield core.speeds_in_kmh(dict(location=location, valid_time=row["valid_time"], older_run=core.stamp(older),
                   newer_run=core.stamp(newer), old_lead_h=row["step"], new_lead_h=row["new_step"],
                   old_speed_ms=row["speed_ms"], new_speed_ms=row["new_speed"],
                   delta_speed_ms=row["new_speed"] - row["speed_ms"],
                   delta_direction_deg=core.angle_delta(row["new_direction"], row["direction_deg"]),
                   old_native_time_step=row["native_time_step"], new_native_time_step=row["new_native"],
                   old_gust_ms=row["gust_ms"], new_gust_ms=row["new_gust"],
                   gust_comparison="exact_intervals_not_exposed_by_provider"))


def execute(args, config, db, cid):
    if config["horizon_hours"] != 144:
        raise ValueError("Questa configurazione richiede horizon_hours=144")
    init_db(db)

    def cycle(runs, product=None):
        errors = collect(config, db, cid, runs) if product != "pf" else 0
        if config["ensemble_members"] and product != "fc":
            # Confidenza ECMWF preesistente: griglia 0.25, soltanto alle ore native.
            errors += core.collect(config, db, cid, runs, list(range(0, 145, 3)), "pf")
        return errors

    if args.command == "fetch":
        if args.steps is not None:
            raise ValueError("Il provider 9 km acquisisce tutte le 145 scadenze; omettere --steps")
        runs = [core.parse_run(args.run)] if args.run else core.candidate_runs(config)
        return 1 if cycle(runs, args.product) else 0
    if args.command == "watch":
        while True:
            cycle(core.candidate_runs(config))
            core.LOG.info("Prossimo controllo fra %s minuti", config["poll_minutes"])
            time.sleep(config["poll_minutes"] * 60)
    elif args.command == "status":
        known = {r[0] for r in db.execute("SELECT DISTINCT run FROM downloads9 WHERE config_id=?", (cid,))}
        known.update(r[0] for r in db.execute("SELECT DISTINCT run FROM batches WHERE config_id=?", (cid,)))
        print(f"ECMWF 9 km, 144 ore, run 00/06/12/18 UTC; configurazione {cid}")
        if not known:
            print("Nessuna run acquisita")
        for run in sorted(known):
            n = db.execute("SELECT count(*) FROM downloads9 WHERE config_id=? AND run=?", (cid, run)).fetchone()[0]
            print(f"{run}: {n}/{len(config['locations'])} localita complete (145 valori ciascuna)")
            if config["ensemble_members"]:
                pf = db.execute("SELECT count(*) FROM batches WHERE config_id=? AND run=? AND product='pf'", (cid, run)).fetchone()[0]
                print(f"  Ensemble ECMWF 0.25 gradi: {pf}/49 scadenze")
    elif args.command == "export":
        core.write_csv(export_rows(db, cid), args.output)
    elif args.command == "compare":
        older, newer = core.parse_run(args.older), core.parse_run(args.newer)
        if older >= newer:
            raise ValueError("La run newer deve essere successiva a older")
        core.write_csv(compare_rows(db, cid, older, newer, args.location), args.output)
    return 0

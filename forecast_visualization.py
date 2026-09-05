"""Genera la mappa dell'ultima previsione e la sua evoluzione oraria interattiva."""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / "data/cartography/mpl-cache"))

from PIL import Image, ImageDraw, ImageFont
from matplotlib.font_manager import FontProperties, findfont
from pyproj import Transformer
import requests
import shapefile
from shapely.geometry import box, shape
from shapely.geometry.polygon import orient

import elbameteo


ROOT = Path(__file__).resolve().parent
MAP_PATH = ROOT / "output/maps/elba_aree_ecmwf_9km.png"
DEFAULT_HTML = ROOT / "output/ultima_previsione.html"
DEFAULT_PNG = ROOT / "output/ultima_previsione.png"
FIGURE = dict(left=.09, bottom=.11, width=.88, height=.85, inches_width=12, inches_height=9)
BOUNDS = (9.975, 42.605, 10.535, 42.945)
PROJECT = Transformer.from_crs(
    "EPSG:4326", "+proj=aeqd +lat_0=42.775 +lon_0=10.255 +datum=WGS84 +units=m", always_xy=True
)


def xy(lon, lat):
    x, y = PROJECT.transform(lon, lat)
    return x / 1000, y / 1000


def italy_time(value):
    """Converte UTC in CET/CEST secondo la regola europea vigente."""
    dt = value.astimezone(timezone.utc)
    march_last = max(week[-1] for week in calendar.monthcalendar(dt.year, 3))
    october_last = max(week[-1] for week in calendar.monthcalendar(dt.year, 10))
    dst_start = datetime(dt.year, 3, march_last, 1, tzinfo=timezone.utc)
    dst_end = datetime(dt.year, 10, october_last, 1, tzinfo=timezone.utc)
    summer = dst_start <= dt < dst_end
    zone = timezone(timedelta(hours=2 if summer else 1), "CEST" if summer else "CET")
    return dt.astimezone(zone)


def latest_complete_run(db, cid, locations, horizon):
    expected = len(locations) * (horizon + 1)
    row = db.execute("""
        SELECT run FROM hourly9 WHERE config_id=?
        GROUP BY run HAVING COUNT(*)=? AND COUNT(DISTINCT location)=?
        ORDER BY run DESC LIMIT 1
    """, (cid, expected, len(locations))).fetchone()
    if row is None:
        raise ValueError(
            "Nessuna run completa per i punti configurati. Eseguire prima: "
            ".\\.venv\\Scripts\\python.exe elbameteo.py fetch --product fc"
        )
    return row["run"]


def ensemble_score(speeds, directions):
    angles = [math.radians(value) for value in directions if value is not None]
    if len(speeds) < 20 or not angles:
        return None
    resultant = math.hypot(sum(math.cos(a) for a in angles), sum(math.sin(a) for a in angles)) / len(angles)
    speed_factor = math.exp(-statistics.pstdev(speeds) / max(statistics.mean(speeds), 2.0))
    return round(100 * math.sqrt(resultant * speed_factor), 1)


def fetch_hourly_predictability(config, run):
    origin = elbameteo.parse_run(run)
    params = dict(latitude=",".join(str(p["lat"]) for p in config["locations"]),
                  longitude=",".join(str(p["lon"]) for p in config["locations"]),
                  models="ecmwf_ifs_europe_ensemble",
                  hourly="wind_speed_10m,wind_direction_10m", wind_speed_unit="ms",
                  start_hour=origin.strftime("%Y-%m-%dT%H:%M"),
                  end_hour=(origin + timedelta(hours=config["horizon_hours"])).strftime("%Y-%m-%dT%H:%M"),
                  timezone="GMT", cell_selection="nearest")
    response = requests.get("https://ensemble-api.open-meteo.com/v1/ensemble", params=params, timeout=(15, 120))
    response.raise_for_status()
    payloads = response.json()
    if not isinstance(payloads, list):
        payloads = [payloads]
    scores = {}
    for location, payload in zip(config["locations"], payloads):
        hourly = payload["hourly"]
        speed_keys = sorted(k for k in hourly if k == "wind_speed_10m" or k.startswith("wind_speed_10m_member"))
        direction_keys = sorted(k for k in hourly if k == "wind_direction_10m" or k.startswith("wind_direction_10m_member"))
        for index, valid_time in enumerate(hourly["time"]):
            speeds = [hourly[k][index] for k in speed_keys if hourly[k][index] is not None]
            directions = [hourly[k][index] for k in direction_keys if hourly[k][index] is not None]
            scores[(location["id"], valid_time + ":00Z")] = ensemble_score(speeds, directions)
    return scores


def load_forecast(config_path):
    config = elbameteo.load_config(config_path)
    cid, _ = elbameteo.config_id(config)
    db = elbameteo.connect(config)
    try:
        import ecmwf9
        ecmwf9.init_db(db)
        db.execute("""CREATE TABLE IF NOT EXISTS predictability9 (
            config_id TEXT, run TEXT, location TEXT, valid_time TEXT, score REAL,
            PRIMARY KEY(config_id,run,location,valid_time))""")
        run = latest_complete_run(db, cid, config["locations"], config["horizon_hours"])
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM hourly9 WHERE config_id=? AND run=? ORDER BY step,location", (cid, run)
        )]
        ensemble = [dict(row) for row in db.execute(
            "SELECT * FROM forecasts WHERE config_id=? AND run=? AND product='pf' ORDER BY step,location,member",
            (cid, run)
        )]
        cached_scores = {(row["location"], row["valid_time"]): row["score"] for row in db.execute(
            "SELECT location,valid_time,score FROM predictability9 WHERE config_id=? AND run=?", (cid, run)
        )}
        if not cached_scores:
            cached_scores = fetch_hourly_predictability(config, run)
            with db:
                db.executemany("INSERT OR REPLACE INTO predictability9 VALUES (?,?,?,?,?)",
                               [(cid, run, location, valid, score)
                                for (location, valid), score in cached_scores.items()])
    finally:
        db.close()
    locations = {item["id"]: item for item in config["locations"]}
    native_scores = {}
    grouped = {}
    for row in ensemble:
        grouped.setdefault((row["location"], row["step"]), []).append(row)
    for key, members in grouped.items():
        speeds = [row["speed_ms"] for row in members]
        angles = [math.radians(row["direction_deg"]) for row in members if row["direction_deg"] is not None]
        if len(members) < 20 or not angles:
            continue
        native_scores[key] = ensemble_score(speeds, [row["direction_deg"] for row in members])

    def predictability(location, step):
        valid_time = rows[step * len(locations)]["valid_time"]
        if (location, valid_time) in cached_scores:
            return cached_scores[location, valid_time]
        if (location, step) in native_scores:
            return native_scores[location, step]
        lower = step - step % 3
        upper = min(lower + 3, config["horizon_hours"])
        a, b = native_scores.get((location, lower)), native_scores.get((location, upper))
        if a is None or b is None:
            return None
        return round(a + (b - a) * (step - lower) / max(upper - lower, 1), 1)

    frames = []
    for step in range(config["horizon_hours"] + 1):
        values = []
        for row in (r for r in rows if r["step"] == step):
            location = locations[row["location"]]
            values.append(dict(id=row["location"], lat=location["lat"], lon=location["lon"],
                               speed=round(row["speed_ms"] * 3.6, 1),
                               gust=round(row["gust_ms"] * 3.6, 1) if row["gust_ms"] is not None else None,
                               direction=row["direction_deg"],
                               predictability=predictability(row["location"], step)))
        frames.append(dict(step=step, valid_time=rows[step * len(locations)]["valid_time"], values=values))
    return dict(run=run, generated_at=elbameteo.stamp(datetime.now(timezone.utc)),
                frames=frames, locations=list(locations),
                coast_segments=elba_coast_segments(config["locations"]))


def projected_percent(lon, lat):
    xmin, ymin = xy(BOUNDS[0], BOUNDS[1])
    xmax, ymax = xy(BOUNDS[2], BOUNDS[3])
    x, y = xy(lon, lat)
    # set_aspect("equal") mantiene l'altezza richiesta e restringe il riquadro
    # cartografico in orizzontale, centrandolo dentro la posizione nominale.
    # Le sovrapposizioni devono quindi usare questo riquadro effettivo.
    data_aspect = float((xmax - xmin) / (ymax - ymin))
    actual_width = FIGURE["height"] * FIGURE["inches_height"] * data_aspect / FIGURE["inches_width"]
    actual_left = FIGURE["left"] + (FIGURE["width"] - actual_width) / 2
    left = 100 * (actual_left + actual_width * float((x - xmin) / (xmax - xmin)))
    top = 100 * (1 - FIGURE["bottom"] - FIGURE["height"] * float((y - ymin) / (ymax - ymin)))
    return round(left, 4), round(top, 4)


def elba_coast_segments(locations):
    roi = box(*BOUNDS)
    polygons = []
    reader = shapefile.Reader(str(ROOT / "data/cartography/ne_10m_land.zip"))
    for item in reader.iterShapes():
        if not box(*item.bbox).intersects(roi):
            continue
        clipped = shape(item.__geo_interface__).intersection(roi)
        polygons.extend(list(clipped.geoms) if hasattr(clipped, "geoms") else [clipped])
    candidates = [p for p in polygons if not p.is_empty and p.geom_type == "Polygon"
                  and 10.05 < p.centroid.x < 10.5 and 42.68 < p.centroid.y < 42.88]
    if not candidates:
        raise ValueError("Profilo costiero dell'Elba non trovato")
    island = orient(max(candidates, key=lambda p: p.area), sign=1).simplify(.0007, preserve_topology=True)
    coords = list(island.exterior.coords)
    nodes = [(p["id"], *xy(p["lon"], p["lat"])) for p in locations]
    segments = []
    for a, b in zip(coords, coords[1:]):
        ax, ay = xy(*a)
        bx, by = xy(*b)
        length = math.hypot(bx - ax, by - ay)
        if length < .01:
            continue
        # L'anello è antiorario: l'interno dell'isola è a sinistra.
        inward = (-(by - ay) / length, (bx - ax) / length)
        mx, my = (ax + bx) / 2, (ay + by) / 2
        node = min(nodes, key=lambda p: (p[1] - mx) ** 2 + (p[2] - my) ** 2)[0]
        segments.append(dict(a=projected_percent(*a), b=projected_percent(*b),
                             inward=[round(inward[0], 5), round(inward[1], 5)], node=node))
    return segments


def speed_color(speed):
    if speed < 15:
        return "#277da1"
    if speed < 30:
        return "#2a9d8f"
    if speed < 50:
        return "#f4a261"
    return "#d1495b"


def draw_static(data, step, output):
    frame = data["frames"][step]
    image = Image.open(MAP_PATH).convert("RGBA")
    draw = ImageDraw.Draw(image)
    font_path = findfont(FontProperties(family="DejaVu Sans", weight="bold"))
    font = ImageFont.truetype(font_path, 27)
    small = ImageFont.truetype(font_path, 20)
    for value in frame["values"]:
        left, top = projected_percent(value["lon"], value["lat"])
        cx, cy = image.width * left / 100, image.height * top / 100
        direction = value["direction"]
        color = speed_color(value["speed"])
        if direction is not None:
            # La direzione meteorologica indica la provenienza; la freccia mostra il moto.
            angle = math.radians(direction + 180)
            dx, dy = math.sin(angle), -math.cos(angle)
            start = (cx - dx * 34, cy - dy * 34)
            end = (cx + dx * 34, cy + dy * 34)
            draw.line((start, end), fill=color, width=10)
            px, py = -dy, dx
            head = [(end[0], end[1]),
                    (end[0] - dx * 24 + px * 14, end[1] - dy * 24 + py * 14),
                    (end[0] - dx * 24 - px * 14, end[1] - dy * 24 - py * 14)]
            draw.polygon(head, fill=color)
        label = f'{value["speed"]:.0f}'
        box = draw.textbbox((0, 0), label, font=font)
        tx, ty = cx - (box[2] - box[0]) / 2, cy + 42
        draw.rounded_rectangle((tx - 8, ty - 4, tx + box[2] - box[0] + 8, ty + box[3] - box[1] + 6),
                               radius=8, fill=(247, 249, 250, 225))
        draw.text((tx, ty), label, font=font, fill=color)
    run_local = italy_time(datetime.fromisoformat(data["run"].replace("Z", "+00:00")))
    valid_local = italy_time(datetime.fromisoformat(frame["valid_time"].replace("Z", "+00:00")))
    title = (f'Run {run_local:%d/%m/%Y %H:%M} · valida {valid_local:%d/%m/%Y %H:%M} '
             f'{valid_local:%Z} · velocità km/h')
    title_box = draw.textbbox((0, 0), title, font=small)
    draw.rounded_rectangle((150, 230, 170 + title_box[2], 270), radius=9, fill=(247, 249, 250, 232))
    draw.text((160, 238), title, font=small, fill="#173440")
    output.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output, "PNG")


def write_html(data, output):
    positions = {}
    for frame in data["frames"][:1]:
        for value in frame["values"]:
            positions[value["id"]] = projected_percent(value["lon"], value["lat"])
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    pos = json.dumps(positions, separators=(",", ":"))
    html = TEMPLATE.replace("__DATA__", payload).replace("__POSITIONS__", pos)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")


TEMPLATE = r'''<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ElbaMeteo · evoluzione del vento</title>
<style>
:root{font-family:Inter,system-ui,sans-serif;color:#173440;background:#f7f9fa}*{box-sizing:border-box}
body{margin:0;padding:20px}.shell{max-width:1180px;margin:auto}.heading{display:grid;gap:7px;margin-bottom:12px}
h1{font-size:25px;margin:0}.meta{color:#607480}.map-help{display:grid;gap:4px}.map{position:relative}.map>img{display:block;width:100%;height:auto}.coast-overlay{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}.coast-segment{fill:none;stroke-linecap:round;stroke-width:4px;vector-effect:non-scaling-stroke}
.marker{position:absolute;transform:translate(-50%,-50%);width:66px;height:90px;border:0;background:transparent;color:#173440;cursor:pointer;padding:0}
.arrow{position:absolute;left:calc(50% - 24px);top:calc(50% - 24px);width:48px;height:48px;filter:drop-shadow(0 1px 2px #fff);transform-origin:24px 24px}.arrow:before{content:"";position:absolute;left:21px;top:6px;width:6px;height:37px;border-radius:3px;background:currentColor}.arrow:after{content:"";position:absolute;left:13px;top:4px;width:17px;height:17px;border-left:6px solid currentColor;border-top:6px solid currentColor;transform:rotate(45deg)}.value{position:absolute;left:50%;top:76%;transform:translateX(-50%);display:inline-block;background:rgba(247,249,250,.9);border-radius:7px;padding:2px 5px;font-weight:700}
.marker.active .value{outline:3px solid #173440}.controls{display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:center;margin:14px 0}
button,select{font:inherit;padding:8px 12px;border:1px solid #b7cbd5;border-radius:7px;background:#fff;color:#173440}input{width:100%}
.chart{width:100%;height:210px;border:1px solid #b7cbd5;background:#fff}.direction-chart{height:170px;margin-top:8px}.legend{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0;color:#506672}.key:before{content:"";display:inline-block;width:18px;height:3px;margin:0 6px 3px 0;background:#226eab}.key.gust:before{background:#d1495b}.key.direction:before{background:#7b2cbf}.key.predictability:before{background:#2f855a}.coast-key{display:flex;gap:14px;flex-wrap:wrap}.coast-key span:before{content:"";display:inline-block;width:18px;height:4px;margin:0 6px 3px 0;background:#238636}.coast-key .onshore:before{background:#d62828}
@media(max-width:600px){body{padding:8px}.marker{width:44px;height:60px}.arrow{left:calc(50% - 15px);top:calc(50% - 15px);width:30px;height:30px;transform-origin:15px 15px}.arrow:before{left:13px;top:4px;width:4px;height:23px}.arrow:after{left:8px;top:3px;width:10px;height:10px;border-width:4px}.value{font-size:10px;top:73%;padding:1px 4px}.controls{grid-template-columns:auto 1fr}.controls select{grid-column:1/-1}.chart{height:185px}.direction-chart{height:155px}}
</style></head><body><main class="shell"><div class="heading"><h1>Vento sull’Isola d’Elba</h1><div class="meta" id="time"></div><div class="meta map-help"><div>ECMWF IFS 9 km · P05–P18: 14 nodi per isola e coste · ogni area usa il nodo più vicino</div><div>Freccia: verso del moto · numero: km/h</div><div class="coast-key"><span>Costa non esposta</span><span class="onshore">Onshore nelle ultime 6 ore</span></div></div><div class="legend"><span class="key">Velocità (km/h)</span><span class="key gust">Raffica (km/h)</span><span class="key predictability">Prevedibilità ensemble (%)</span><span class="key direction">Verso e componente nord/sud (km/h)</span></div></div>
<div class="map" id="map"><img src="maps/elba_aree_ecmwf_9km.png" alt="Aree ECMWF sull'Isola d'Elba"></div>
<div class="controls"><button id="play" type="button">▶ Riproduci</button><input id="hour" type="range" min="0" max="144" value="0" aria-label="Ora di previsione"><select id="point" aria-label="Punto per il grafico"></select></div>
<canvas class="chart" id="chart" role="img" aria-label="Velocità e raffiche ora per ora"></canvas>
<canvas class="chart direction-chart" id="directionChart" role="img" aria-label="Verso del vento e componente nord sud ora per ora"></canvas>
<canvas class="chart direction-chart" id="predictabilityChart" role="img" aria-label="Prevedibilità ensemble percentuale ora per ora"></canvas>
</main><script>
const forecast=__DATA__,positions=__POSITIONS__,map=document.getElementById('map'),slider=document.getElementById('hour'),select=document.getElementById('point'),play=document.getElementById('play'),time=document.getElementById('time'),canvas=document.getElementById('chart'),directionCanvas=document.getElementById('directionChart'),predictabilityCanvas=document.getElementById('predictabilityChart');
let timer=null,selected=forecast.locations[0];
function color(v){return v<15?'#277da1':v<30?'#2a9d8f':v<50?'#f4a261':'#d1495b'}
const localFormatter=new Intl.DateTimeFormat('it-IT',{timeZone:'Europe/Rome',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',timeZoneName:'short'}),partsFormatter=new Intl.DateTimeFormat('it-IT',{timeZone:'Europe/Rome',day:'numeric',month:'short',hour:'2-digit',hourCycle:'h23'});
function localParts(value){const parts=Object.fromEntries(partsFormatter.formatToParts(new Date(value)).filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));return{day:+parts.day,month:parts.month,hour:+parts.hour}}
const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('class','coast-overlay');svg.setAttribute('viewBox','0 0 100 100');svg.setAttribute('preserveAspectRatio','none');forecast.coast_segments.forEach((s,index)=>{const line=document.createElementNS(svg.namespaceURI,'line');line.setAttribute('class','coast-segment');line.setAttribute('x1',s.a[0]);line.setAttribute('y1',s.a[1]);line.setAttribute('x2',s.b[0]);line.setAttribute('y2',s.b[1]);line.dataset.index=index;svg.appendChild(line)});map.appendChild(svg);
forecast.locations.forEach(id=>{const o=document.createElement('option');o.value=id;o.textContent=id;select.appendChild(o);const b=document.createElement('button');b.className='marker';b.dataset.id=id;b.style.left=positions[id][0]+'%';b.style.top=positions[id][1]+'%';b.innerHTML='<span class="arrow" aria-hidden="true"></span><span class="value"></span>';b.onclick=()=>{selected=id;select.value=id;render()};map.appendChild(b)});
function coastIsOnshore(segment,index){for(let hour=Math.max(0,index-5);hour<=index;hour++){const v=forecast.frames[hour].values.find(x=>x.id===segment.node);if(v&&v.direction!=null){const flow=(v.direction+180)*Math.PI/180,east=Math.sin(flow),north=Math.cos(flow);if(east*segment.inward[0]+north*segment.inward[1]>0)return true}}return false}
function setupCanvas(target){const dpr=devicePixelRatio||1,w=target.clientWidth,h=target.clientHeight;target.width=w*dpr;target.height=h*dpr;const c=target.getContext('2d');c.scale(dpr,dpr);c.clearRect(0,0,w,h);return{c,w,h}}
function timeAxis(c,w,h,pad,x){c.font='11px sans-serif';c.textAlign='center';forecast.frames.forEach((f,i)=>{const p=localParts(f.valid_time);if(p.hour%6===0){c.strokeStyle='#e4ebee';c.lineWidth=1;c.beginPath();c.moveTo(x(i),pad.t);c.lineTo(x(i),h-pad.b);c.stroke();c.fillStyle='#607480';c.fillText(String(p.hour).padStart(2,'0'),x(i),h-27)}});forecast.frames.forEach((f,i)=>{const p=localParts(f.valid_time);if(i>0&&p.hour===0){c.strokeStyle='#718892';c.lineWidth=3;c.beginPath();c.moveTo(x(i),pad.t);c.lineTo(x(i),h-pad.b);c.stroke()}});let start=0;while(start<forecast.frames.length){const first=localParts(forecast.frames[start].valid_time);let end=start;while(end+1<forecast.frames.length&&localParts(forecast.frames[end+1].valid_time).day===first.day)end++;c.fillStyle='#173440';c.font='12px sans-serif';c.fillText(first.day+' '+first.month,x((start+end)/2),h-8);start=end+1}c.textAlign='left'}
function selectedGuide(c,h,pad,x){const i=+slider.value;c.strokeStyle='#173440';c.lineWidth=1;c.beginPath();c.moveTo(x(i),pad.t);c.lineTo(x(i),h-pad.b);c.stroke()}
function drawChart(){const {c,w,h}=setupCanvas(canvas),series=forecast.frames.map(f=>f.values.find(v=>v.id===selected)),max=Math.max(10,...series.flatMap(v=>[v.speed,v.gust||0])),pad={l:58,r:12,t:12,b:48},x=i=>pad.l+i*(w-pad.l-pad.r)/(series.length-1),y=v=>h-pad.b-v*(h-pad.t-pad.b)/max;c.strokeStyle='#d5e0e5';c.lineWidth=1;for(let i=0;i<=4;i++){const yy=pad.t+i*(h-pad.t-pad.b)/4;c.beginPath();c.moveTo(pad.l,yy);c.lineTo(w-pad.r,yy);c.stroke();c.fillStyle='#607480';c.font='12px sans-serif';c.textAlign='left';c.fillText(Math.round(max*(4-i)/4),4,yy+4)}c.textAlign='left';timeAxis(c,w,h,pad,x);function line(key,stroke){c.strokeStyle=stroke;c.lineWidth=2;c.beginPath();let drawing=false;series.forEach((v,i)=>{const val=v[key];if(val==null){drawing=false;return}if(drawing)c.lineTo(x(i),y(val));else c.moveTo(x(i),y(val));drawing=true});c.stroke()}line('speed','#226eab');line('gust','#d1495b');selectedGuide(c,h,pad,x)}
function drawDirection(){const {c,w,h}=setupCanvas(directionCanvas),series=forecast.frames.map(f=>f.values.find(v=>v.id===selected)),components=series.map(v=>v.direction==null?0:-v.speed*Math.cos(v.direction*Math.PI/180)),limit=Math.max(10,Math.ceil(Math.max(...components.map(Math.abs))/5)*5),pad={l:58,r:12,t:12,b:48},x=i=>pad.l+i*(w-pad.l-pad.r)/(series.length-1),y=v=>pad.t+(limit-v)*(h-pad.t-pad.b)/(2*limit);c.strokeStyle='#d5e0e5';c.lineWidth=1;[-limit,0,limit].forEach(v=>{const yy=y(v);c.beginPath();c.moveTo(pad.l,yy);c.lineTo(w-pad.r,yy);c.stroke();c.fillStyle='#607480';c.font='11px sans-serif';c.fillText(v===0?'0':(v>0?'N +':'S −')+Math.abs(v),3,yy+4)});timeAxis(c,w,h,pad,x);c.strokeStyle='#7b2cbf';c.fillStyle='#7b2cbf';c.lineWidth=1.5;series.forEach((v,i)=>{if(v.direction==null)return;const flow=(v.direction+180)*Math.PI/180,cx=x(i),cy=y(components[i]),dx=Math.sin(flow)*5,dy=-Math.cos(flow)*5;c.beginPath();c.moveTo(cx-dx,cy-dy);c.lineTo(cx+dx,cy+dy);c.stroke();const ex=cx+dx,ey=cy+dy,px=-dy*.55,py=dx*.55;c.beginPath();c.moveTo(ex,ey);c.lineTo(ex-dx*.8+px,ey-dy*.8+py);c.lineTo(ex-dx*.8-px,ey-dy*.8-py);c.closePath();c.fill()});selectedGuide(c,h,pad,x)}
function drawPredictability(){const {c,w,h}=setupCanvas(predictabilityCanvas),series=forecast.frames.map(f=>f.values.find(v=>v.id===selected)),pad={l:58,r:12,t:12,b:48},x=i=>pad.l+i*(w-pad.l-pad.r)/(series.length-1),y=v=>h-pad.b-v*(h-pad.t-pad.b)/100;c.strokeStyle='#d5e0e5';c.lineWidth=1;for(let v=0;v<=100;v+=25){const yy=y(v);c.beginPath();c.moveTo(pad.l,yy);c.lineTo(w-pad.r,yy);c.stroke();c.fillStyle='#607480';c.font='12px sans-serif';c.fillText(v+'%',4,yy+4)}timeAxis(c,w,h,pad,x);c.strokeStyle='#2f855a';c.lineWidth=2;c.beginPath();let drawing=false;series.forEach((v,i)=>{if(v.predictability==null){drawing=false;return}if(drawing)c.lineTo(x(i),y(v.predictability));else c.moveTo(x(i),y(v.predictability));drawing=true});c.stroke();selectedGuide(c,h,pad,x)}
function render(){const i=+slider.value,frame=forecast.frames[i];time.textContent='Run '+localFormatter.format(new Date(forecast.run))+' · valida '+localFormatter.format(new Date(frame.valid_time))+' · +'+i+' h · aggiornato '+localFormatter.format(new Date(forecast.generated_at));document.querySelectorAll('.marker').forEach(b=>{const v=frame.values.find(x=>x.id===b.dataset.id),a=b.querySelector('.arrow');a.style.transform='rotate('+((v.direction??180)+180)+'deg)';a.style.color=color(v.speed);a.style.visibility=v.direction==null?'hidden':'visible';b.querySelector('.value').textContent=Math.round(v.speed);b.querySelector('.value').style.color=color(v.speed);b.classList.toggle('active',v.id===selected)});document.querySelectorAll('.coast-segment').forEach(line=>{line.style.stroke=coastIsOnshore(forecast.coast_segments[+line.dataset.index],i)?'#d62828':'#238636'});drawChart();drawDirection();drawPredictability()}
slider.oninput=render;select.onchange=()=>{selected=select.value;render()};play.onclick=()=>{if(timer){clearInterval(timer);timer=null;play.textContent='▶ Riproduci'}else{play.textContent='❚❚ Pausa';timer=setInterval(()=>{slider.value=(+slider.value+1)%145;render()},650)}};addEventListener('resize',()=>{drawChart();drawDirection();drawPredictability()});render();
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--step", type=int, help="Ora del PNG; predefinita: prima scadenza non trascorsa")
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--png", default=str(DEFAULT_PNG))
    args = parser.parse_args()
    data = load_forecast(args.config)
    if args.step is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        step = next((f["step"] for f in data["frames"] if f["valid_time"] >= now), len(data["frames"]) - 1)
    else:
        step = args.step
    if not 0 <= step < len(data["frames"]):
        raise ValueError("step fuori dall'orizzonte disponibile")
    write_html(data, Path(args.html))
    draw_static(data, step, Path(args.png))
    print(json.dumps(dict(run=data["run"], step=step, html=args.html, png=args.png)))


if __name__ == "__main__":
    main()

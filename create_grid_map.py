"""Carta delle aree di prossimità dei nodi ECMWF O1280 sull'Elba."""
import json
import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / "data/cartography/mpl-cache"))

import eccodes
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from matplotlib.patches import Polygon as PatchPolygon
import numpy as np
from pyproj import Transformer, Geod
import shapefile
from shapely.geometry import box, shape, Point, MultiPoint
from shapely.ops import unary_union
from shapely import voronoi_polygons

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data/cartography"
OUT = ROOT / "output/maps"
OUT.mkdir(parents=True, exist_ok=True)
BOUNDS = (9.975, 42.605, 10.535, 42.945)
project = Transformer.from_crs("EPSG:4326", "+proj=aeqd +lat_0=42.775 +lon_0=10.255 +datum=WGS84 +units=m", always_xy=True)
geod = Geod(ellps="WGS84")


def xy(lon, lat):
    x, y = project.transform(lon, lat)
    return np.asarray(x) / 1000, np.asarray(y) / 1000


def coast():
    roi = box(*BOUNDS)
    parts = []
    reader = shapefile.Reader(str(DATA / "ne_10m_land.zip"))
    for item in reader.iterShapes():
        if not box(*item.bbox).intersects(roi):
            continue
        clipped = shape(item.__geo_interface__).intersection(roi)
        if not clipped.is_empty:
            parts.append(clipped)
    return unary_union(parts)


def grid():
    rows = []
    latitudes = eccodes.codes_get_gaussian_latitudes(1280)
    for index, lat in enumerate(latitudes[0:1280]):
        if BOUNDS[1] - .1 < lat < BOUNDS[3] + .1:
            count = 20 + 4 * index
            points = [(j, 360 * j / count) for j in range(count)
                      if BOUNDS[0] - .2 < 360 * j / count < BOUNDS[2] + .2]
            rows.append((index + 1, float(lat), points))
    return rows


land = coast()
grid_rows = grid()
towns = json.loads((DATA / "towns.json").read_text(encoding="utf-8"))
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})
fig = plt.figure(figsize=(16, 11), facecolor="#f7f9fa")
ax = fig.add_axes([.055, .145, .705, .70], facecolor="#e6f0f5")
side = fig.add_axes([.80, .16, .18, .67])
side.set_axis_off()
ink, blue, green = "#173440", "#226eab", "#d8e6d8"

fig.text(.055, .934, "L’Elba: un’area per ogni punto", fontsize=29, fontweight="bold", color=ink)
fig.text(.057, .891, "ECMWF IFS 9 km · aree di assegnazione al nodo più vicino", fontsize=15, color="#506672")

polygons = list(land.geoms) if hasattr(land, "geoms") else [land]
for polygon in polygons:
    if polygon.geom_type != "Polygon":
        continue
    coords = np.asarray(polygon.exterior.coords)
    ax.add_patch(PatchPolygon(np.column_stack(xy(coords[:, 0], coords[:, 1])),
                              facecolor=green, edgecolor="#789482", lw=1.15, zorder=2))

records = []
for rownum, lat, points in grid_rows:
    for j, lon in points:
        if not BOUNDS[0] <= lon <= BOUNDS[2] or not BOUNDS[1] <= lat <= BOUNDS[3]:
            continue
        point_id = f"P{len(records)+1:02}"
        on_land = land.covers(Point(lon, lat))
        records.append(dict(id=point_id, latitude=lat, longitude=lon, gaussian_row=rownum,
                            longitude_index=j, on_land_cartographic=on_land))
        x, y = xy(lon, lat)
        node_color = blue if 5 <= int(point_id[1:]) <= 18 else "#8194a0"
        ax.scatter(x, y, s=80, c=node_color, edgecolors="white", linewidths=1.1, zorder=6)
        ax.annotate(point_id, (x, y), xytext=(7, 7), textcoords="offset points", fontsize=9,
                    fontweight="bold", color=node_color, zorder=7,
                    path_effects=[pe.withStroke(linewidth=3.5, foreground="#f4f8fa")])

# Voronoi euclideo nella proiezione locale: include anche nodi esterni
# all'inquadratura, per non deformare le aree vicino ai bordi della mappa.
centers = [(r, j, *map(float, xy(lon, lat))) for r, lat, pts in grid_rows for j, lon in pts]
cells = voronoi_polygons(MultiPoint([(x, y) for _, _, x, y in centers]), ordered=True)
clip = box(*map(float, (*xy(BOUNDS[0], BOUNDS[1]), *xy(BOUNDS[2], BOUNDS[3]))))
selected_keys = {(p["gaussian_row"], p["longitude_index"])
                 for p in records if 5 <= int(p["id"][1:]) <= 18}
palette = ["#9ed2df", "#b3c1e5", "#c9e1dc", "#bbd4ec"]
visible_cells = []
for (r, j, x, y), cell in zip(centers, cells.geoms):
    assert cell.covers(Point(x, y))
    clipped = cell.intersection(clip)
    if clipped.is_empty:
        continue
    visible_cells.append(clipped)
    selected = (r, j) in selected_keys
    ax.add_patch(PatchPolygon(np.asarray(clipped.exterior.coords),
                              facecolor=palette[(r+j) % len(palette)] if selected else "#d9dfe3",
                              edgecolor="none", alpha=.38 if selected else .18, zorder=2.3))
    ax.add_patch(PatchPolygon(np.asarray(clipped.exterior.coords), fill=False,
                              edgecolor=blue if selected else "#a8b3bc", lw=1.35 if selected else .7, zorder=3))
assert abs(unary_union(visible_cells).area - clip.area) < 1e-6

# Centri abitati: coordinate GeoNames tramite Open-Meteo Geocoding.
offsets = {"Portoferraio": (-17, 24), "Porto Azzurro": (26, 8), "Capoliveri": (24, -20),
           "Marciana": (-55, 8), "Marina di Campo": (-62, -25), "Rio Marina": (25, 18)}
for name, response in towns.items():
    point = next(p for p in response["results"] if 42.65 < p["latitude"] < 42.90 and 10.05 < p["longitude"] < 10.50)
    x, y = xy(point["longitude"], point["latitude"])
    ax.scatter(x, y, s=19, c=ink, edgecolors="white", linewidths=.6, zorder=8)
    ax.annotate(name, (x, y), xytext=offsets[name], textcoords="offset points", color=ink,
                fontsize=10, zorder=9, fontweight="medium",
                arrowprops=dict(arrowstyle="-", color="#738088", lw=.65),
                path_effects=[pe.withStroke(linewidth=3.5, foreground="#f7f9fa")])

x, y = xy(10.29, 42.69)
ax.text(x, y, "I S O L A   D ’ E L B A", ha="center", color="#718d84", fontsize=13,
        fontstyle="italic", zorder=4)

# Distanze geodetiche di riferimento: passo sulla fila centrale e fra le file.
lat = next(lat for r, lat, _ in grid_rows if r == 672)
lat2 = next(lat for r, lat, _ in grid_rows if r == 673)
lon1, lon2 = 360 * 77 / 2704, 360 * 78 / 2704
dx = geod.inv(lon1, lat, lon2, lat)[2] / 1000
dy = geod.inv(lon1, lat, lon1, lat2)[2] / 1000
side.text(0, .96, "COME LEGGERE\nLA MAPPA", color=ink, fontsize=16, fontweight="bold", va="top")
side.scatter([.025], [.795], s=72, color=blue)
side.text(.11, .795, "Punto di previsione\nECMWF", va="center", fontsize=12, color=ink)
side.plot([0, .08], [.695, .695], color=blue, lw=1.4)
side.text(.11, .695, "Confine tra due aree", va="center", fontsize=10.5, color=ink)
side.text(0, .595, "STESSA AREA,\nSTESSO NODO", fontsize=15, color=blue, fontweight="bold", va="top")
side.text(0, .485, "Ogni posizione viene\nassociata al punto\npiù vicino. Il confine\npassa a metà strada\nfra nodi adiacenti.", fontsize=12, color=ink, linespacing=1.5, va="top")
side.text(0, .235, "P05–P18", fontsize=23, color=blue, fontweight="bold")
side.text(0, .192, "14 nodi proposti per\nisola e coste, evidenziati\nin azzurro.", fontsize=11.5, color=ink, linespacing=1.5, va="top")
side.text(0, .015, "Passo locale: 10,9 × 7,8 km", fontsize=10.5, color="#607480")
side.set_xlim(-.01, 1)
side.set_ylim(0, 1)

# Cornice in coordinate geografiche; proiezione locale in km, proporzioni reali.
xmin, ymin = xy(BOUNDS[0], BOUNDS[1])
xmax, ymax = xy(BOUNDS[2], BOUNDS[3])
ax.set_xlim(xmin, xmax)
ax.set_ylim(ymin, ymax)
ax.set_aspect("equal")
lon_ticks = np.arange(10.0, 10.51, .1)
lat_ticks = np.arange(42.65, 42.941, .05)
ax.set_xticks([float(xy(lon, 42.775)[0]) for lon in lon_ticks],
              [f"{lon:.2f}° E" for lon in lon_ticks])
ax.set_yticks([float(xy(10.255, lat)[1]) for lat in lat_ticks],
              [f"{lat:.2f}° N" for lat in lat_ticks])
ax.tick_params(colors="#677c87", labelsize=9, length=0, pad=8)
for spine in ax.spines.values():
    spine.set_color("#b7cbd5")

# Barra metrica e indicazione del nord.
bx, by = xmin + 2.4, ymin + 1.8
for begin, end, color in [(0, 5, ink), (5, 10, "white")]:
    ax.add_patch(plt.Rectangle((bx + begin, by), end-begin, .34,
                               facecolor=color, edgecolor=ink, lw=.8, zorder=10))
for pos in (0, 5, 10):
    ax.text(bx+pos, by-.8, str(pos) + (" km" if pos == 10 else ""), ha="center", fontsize=9, color=ink)
ax.annotate("N", xy=(xmax-1.8, ymax-3), xytext=(xmax-1.8, ymax-1.3), ha="center",
            color=ink, fontsize=12, fontweight="bold", arrowprops=dict(arrowstyle="<|-", color=ink, lw=1.6))

fig.text(.055, .091, "I punti sono all’interno delle rispettive aree; lo sfalsamento della griglia genera bordi leggermente obliqui.", fontsize=11, color=ink)
fig.text(.055, .059, "Nodi: ECMWF O1280 / ecCodes · Costa: Natural Earth 1:10m (generalizzata) · Località: GeoNames / Open-Meteo", fontsize=9, color="#6b7b84")
fig.text(.055, .038, "Aree geometriche di prossimità (Voronoi), approssimate in proiezione locale: non celle fisiche ufficiali ECMWF · 3 settembre 2026", fontsize=9, color="#6b7b84")

image_path = OUT / "elba_aree_ecmwf_9km.png"
fig.savefig(image_path, dpi=180, facecolor=fig.get_facecolor())
assert any(abs(p["latitude"]-42.77680039522012) < 1e-9 and abs(p["longitude"]-10.251479289940828) < 1e-9 for p in records)
print(json.dumps(dict(nodes=len(records), on_land=sum(p["on_land_cartographic"] for p in records),
                      east_west_km=dx, north_south_km=dy, image=str(image_path))))

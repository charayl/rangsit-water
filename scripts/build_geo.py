"""Run from anywhere: python scripts/build_geo.py
Canal geometry: OpenStreetMap (ODbL), fetched via Overpass 2026-10-01.
Build data/canals.geojson and data/zones.geojson from OSM canal geometry."""
import json
from pathlib import Path
import numpy as np
from shapely.geometry import MultiLineString, LineString, Polygon, mapping
from shapely.ops import linemerge

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
raw = json.load(open(HERE / "canals_raw.json", encoding="utf-8"))
NAMES = {
    **{str(i): f"คลอง {i}" for i in range(1, 15)},
    "R": "คลองรังสิตประยูรศักดิ์", "H": "คลองหกวา",
    "P": "คลองระพีพัฒน์แยกตก", "PS": "คลองระพีพัฒน์แยกใต้",
}
MAIN = {"R", "H", "P", "PS"}

# ---- canals.geojson
feats = []
for k, ways in raw.items():
    g = linemerge(MultiLineString(ways))
    feats.append({"type": "Feature",
                  "properties": {"id": k, "name": NAMES[k], "main": k in MAIN},
                  "geometry": mapping(g)})
json.dump({"type": "FeatureCollection",
           "attribution": "© OpenStreetMap contributors (ODbL)",
           "features": feats},
          open(ROOT / "data/canals.geojson", "w", encoding="utf-8"), ensure_ascii=False)


def pts(k):
    return np.array([p for w in raw[k] for p in w])


def ns_func(k):
    """lon as function of lat for a north-south canal"""
    a = pts(k); a = a[np.argsort(a[:, 1])]
    _, idx = np.unique(a[:, 1], return_index=True); a = a[idx]
    return lambda lat: float(np.interp(lat, a[:, 1], a[:, 0]))


def ew_func(k):
    """lat as function of lon for an east-west canal"""
    a = pts(k); a = a[np.argsort(a[:, 0])]
    _, idx = np.unique(a[:, 0], return_index=True); a = a[idx]
    return lambda lon: float(np.interp(lon, a[:, 0], a[:, 1]))


ns = {str(i): ns_func(str(i)) for i in range(1, 15)}
R, H, P0 = ew_func("R"), ew_func("H"), ew_func("P")
# east of Khlong 13 the northern edge is the end of Khlong 13/14 (Raphiphat Yaek Tai runs along Khlong 13)
def P(lon):
    if lon <= 100.8915:
        return P0(lon)
    return float(np.interp(lon, [100.8915, 100.914], [14.276, 14.2215]))
k1, k2 = ns["1"], ns["2"]
# Khlong 1 bends into Khlong 2 near 13.957; use Khlong 2 south of that
ns["1s"] = lambda lat: k2(lat) if lat < 13.958 else k1(lat)


def cross(lonf, latf, guess):
    lat = guess
    for _ in range(30):
        lat = latf(lonf(lat))
    return lonf(lat), lat


def zone_poly(w, e, top, bot, n=24):
    wl, el = ns[w], ns[e]
    wt = cross(wl, top, 14.1); et = cross(el, top, 14.1)
    wb = cross(wl, bot, 13.95); eb = cross(el, bot, 13.95)
    ring = []
    for lon in np.linspace(wt[0], et[0], n):            # top edge W->E
        ring.append((lon, top(lon)))
    for lat in np.linspace(et[1], eb[1], n):            # east edge N->S
        ring.append((el(lat), lat))
    for lon in np.linspace(eb[0], wb[0], n):            # bottom edge E->W
        ring.append((lon, bot(lon)))
    for lat in np.linspace(wb[1], wt[1], n):            # west edge S->N
        ring.append((wl(lat), lat))
    p = Polygon(ring).buffer(0)
    return p.simplify(0.0002)


ZONES = [
    # id, west, east, top, bottom
    ("n1", "1", "4", P, R), ("n2", "4", "9", P, R), ("n3", "9", "14", P, R),
    ("s1", "1s", "4", R, H), ("s2", "4", "9", R, H), ("s3", "9", "14", R, H),
]
zfeats = []
for zid, w, e, t, b in ZONES:
    zfeats.append({"type": "Feature", "properties": {"id": zid},
                   "geometry": mapping(zone_poly(w, e, t, b))})

# Zone 0: Rangsit town / canal mouth, west of Khlong 1 (rough box, adjust as needed)
west = 100.585
ring = [(west, 14.035), (k1(14.035), 14.035)]
ring += [(k1(lat), lat) for lat in np.linspace(14.035, 13.96, 12)]
ring += [(west, 13.945)]
zfeats.insert(0, {"type": "Feature", "properties": {"id": "w0"},
                  "geometry": mapping(Polygon(ring).simplify(0.0002))})

def rnd(o):
    if isinstance(o, float): return round(o, 5)
    if isinstance(o, (list, tuple)): return [rnd(x) for x in o]
    if isinstance(o, dict): return {k: rnd(v) for k, v in o.items()}
    return o

json.dump(rnd({"type": "FeatureCollection", "features": zfeats}),
          open(ROOT / "data/zones.geojson", "w", encoding="utf-8"), ensure_ascii=False)

# endpoints for gate markers
r = pts("R")
print("Rangsit W end", r[np.argmin(r[:, 0])], "E end", r[np.argmax(r[:, 0])])
p = pts("P")
print("Raphiphat W end", p[np.argmin(p[:, 0])], "E end", p[np.argmax(p[:, 1])])
for f in zfeats:
    print(f["properties"]["id"], round(Polygon(f["geometry"]["coordinates"][0]).area * 12300, 1), "km2 approx")

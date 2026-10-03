#!/usr/bin/env python3
"""
ดึงข้อมูลน้ำจากหน่วยงานรัฐ แล้วสรุปเป็น docs/data/status.json สำหรับหน้าเว็บ

แหล่งข้อมูล
  - ThaiWater (สสน.)       : ระดับน้ำ/ตลิ่ง/ปริมาณน้ำ ของสถานีโทรมาตร
  - สำนักการระบายน้ำ กทม.  : ปตร.คลองสามวา
  - docs/data/manual.json : ค่าประตูน้ำพระนารายณ์ (เจ้าของระบบกรอกเองวันละครั้ง)

กติกาทั้งหมดอยู่ใน SYSTEM_SPEC.md (สี / แนวโน้ม / น้ำอยู่ด่านไหน / ประโยคสรุป)

ใช้แค่ไลบรารีมาตรฐานของ Python 3.9+ ไม่ต้องติดตั้งอะไรเพิ่ม
  python scripts/fetch_status.py                 # ดึงข้อมูลจริง
  python scripts/fetch_status.py --fixture FILE  # ทดสอบด้วยข้อมูลตัวอย่าง (ไม่ต่อเน็ต)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=7))  # Asia/Bangkok
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data" / "status.json"
MANUAL = ROOT / "docs" / "data" / "manual.json"

THAIWATER = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public"
BMA_FLOW = "https://weather.bangkok.go.th/flow/PageMap/GetData?id=0"
# จุดอ้างอิงของทุ่งรังสิต (ใช้พยากรณ์ฝนจุดเดียว) — ฟรีสำหรับงานไม่แสวงกำไร ต้องให้เครดิต Open-Meteo (CC BY 4.0)
RAIN_LAT, RAIN_LON = 14.03, 100.62
OPEN_METEO = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
              "&hourly=precipitation&past_hours=24&forecast_hours=24&timezone=Asia%2FBangkok")
# เกณฑ์ปริมาณฝนสะสม 24 ชม. (มาตรฐานของกรมอุตุนิยมวิทยา ตามที่ผู้พัฒนาทราบ — ควรตรวจกับเอกสารทางการอีกครั้ง)
RAIN_CLASSES = [(0.1, "ไม่มีฝน"), (10.1, "ฝนเล็กน้อย"), (35.1, "ฝนปานกลาง"), (90.1, "ฝนหนัก"), (1e9, "ฝนหนักมาก")]
UA = "RangsitWaterWatch/1.0 (community flood-watch; polls every 15 min)"

# ---------------------------------------------------------------- กติกา (ตรงกับ SYSTEM_SPEC)
TREND_STEADY_M = 0.10        # เปลี่ยนน้อยกว่า 10 ซม./24 ชม. = คงที่ (เกณฑ์ตั้งเอง)
OUTLIER_JUMP_M = 5.0         # เปลี่ยนเกิน 5 ม./24 ชม. = ค่าผิดปกติจากเครื่องวัด
MANUAL_STALE_DAYS = 2        # ค่าพระนารายณ์เก่าเกิน 2 วัน = ป้ายเทา


def color_from_pct(pct):
    """% ความจุลำน้ำ → สี (เกณฑ์ สสน. + ส้มที่ 90% ซึ่งเป็นเกณฑ์ของแอป)"""
    if pct is None:
        return "gray"
    if pct > 100:
        return "red"
    if pct > 90:
        return "orange"
    if pct > 70:
        return "yellow"
    return "green"


RANK = {"gray": -1, "info": -1, "green": 0, "yellow": 1, "orange": 2, "red": 3}

# ---------------------------------------------------------------- ด่านและสถานี
STAGES = [
    {"n": 1, "id": "pasak", "name": "เขื่อนป่าสักชลสิทธิ์", "kind": "dam",
     "stations": [{"code": "S.28", "label": "ท้ายเขื่อนป่าสักชลสิทธิ์", "stale_h": 24}],
     "eta_next": "ราว 1–2 วัน", "eta_note": "จากข้อมูลย้อนหลังปี 2563–2569"},
    {"n": 2, "id": "rama6", "name": "เขื่อนพระรามหก", "kind": "dam",
     "stations": [{"code": "S.26", "label": "ท้ายเขื่อนพระรามหก", "stale_h": 24}],
     "eta_next": None, "eta_note": "ขึ้นกับการเปิดประตูพระนารายณ์"},
    {"n": 3, "id": "phranarai", "name": "ประตูน้ำพระนารายณ์", "kind": "gate",
     "qmax": 210, "eta_next": "ถ้าเปิด ราว 1–3 วัน", "eta_note": "ค่าคร่าวๆ จากข้อมูลย้อนหลัง"},
    {"n": 4, "id": "raphiphat", "name": "คลองระพีพัฒน์ (ขอบทุ่งรังสิต)", "kind": "canal", "in_field": True,
     "stations": [{"code": "CAN001", "label": "แยกตก (คลองหลวง)", "stale_h": 6},
                  {"code": "BKK013", "label": "แยกใต้ (หนองเสือ)", "stale_h": 6}]},
    {"n": 5, "id": "rangsit", "name": "คลองรังสิต", "kind": "link", "in_field": True,
     "url": "https://cdp.rangsitcity.go.th/",
     "link_label": "ดูกล้องและธงเตือน เทศบาลนครรังสิต",
     "flags": [["green", "ปกติ"], ["yellow", "เกิน 1.30 ม. เฝ้าระวัง"],
               ["orange", "เริ่มอันตราย ยกของขึ้นที่สูง"], ["red", "อันตรายสูงสุด อยู่ในที่ปลอดภัย"]]},
    {"n": 6, "id": "hokwa", "name": "คลองหกวา (ลำลูกกา)", "kind": "canal", "in_field": True,
     "stations": [{"code": "BKK015", "label": "คลอง 8 ลำลูกกา", "stale_h": 6},
                  {"code": "BKK001", "label": "ปลายคลอง 2 (ท้าย ปตร.คลอง 2)", "stale_h": 6}]},
    {"n": 7, "id": "samwa", "name": "คลองสามวา", "kind": "bma", "in_field": True,
     "bma_code": "FW.KSW.01", "label": "ประตูระบายน้ำคลองสามวา", "stale_h": 6},
]
EXTRA = [{"code": "C.13", "label": "ท้ายเขื่อนเจ้าพระยา (แม่น้ำเจ้าพระยา)", "stale_h": 24,
          "why": "ถ้าแม่น้ำเจ้าพระยาสูง น้ำในคลองรังสิตจะระบายออกได้ช้า"}]

SOURCES = [
    {"name": "สถาบันสารสนเทศทรัพยากรน้ำ (สสน.) — ThaiWater", "url": "https://www.thaiwater.net/"},
    {"name": "สำนักการระบายน้ำ กรุงเทพมหานคร", "url": "https://weather.bangkok.go.th/flow/"},
    {"name": "กรมชลประทาน — ผังน้ำเจ้าพระยาตอนล่าง (ค่าประตูพระนารายณ์)", "url": "https://water.rid.go.th/flood/plan_new/planlow.html"},
]


# ---------------------------------------------------------------- เครื่องมือ
def now_th() -> datetime:
    return datetime.now(TZ)


def parse_th(s: str | None) -> datetime | None:
    """'2026-10-01 22:20' (เวลาไทย) → datetime"""
    if not s:
        return None
    try:
        return datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return None


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def iso(dt: datetime | None):
    return dt.isoformat(timespec="minutes") if dt else None


def http_json(url: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json, text/plain, */*",
                "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
                "Origin": "https://www.thaiwater.net", "Referer": "https://www.thaiwater.net/"}
                if "thaiwater" in url else {"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except ValueError:
                raise RuntimeError(f"ไม่ใช่ JSON: {raw[:200]!r}")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url} → {last}")


# ---------------------------------------------------------------- ดึงข้อมูล
class Live:
    def __init__(self, now: datetime):
        self.now = now
        self.errors: list[str] = []

    def thaiwater_load(self):
        # ฐานข้อมูล ThaiWater บางครั้งตอบ result:"NO" (ระบบแน่น) → รอแล้วลองใหม่
        last = None
        for wait in (0, 20, 45, 90):
            time.sleep(wait)
            last = http_json(f"{THAIWATER}/waterlevel_load")
            if (last.get("waterlevel_data") or {}).get("result") != "NO":
                return last
        return last

    def thaiwater_graph(self, station_id: int):
        s = (self.now - timedelta(days=2)).strftime("%Y-%m-%d")
        e = self.now.strftime("%Y-%m-%d")
        return http_json(f"{THAIWATER}/waterlevel_graph?station_type=tele_waterlevel"
                         f"&station_id={station_id}&start_date={s}&end_date={e}")

    def bma(self):
        return http_json(BMA_FLOW, tries=2)

    def rain(self):
        return http_json(OPEN_METEO.format(lat=RAIN_LAT, lon=RAIN_LON), tries=2)


class Fixture:
    def __init__(self, path: str):
        self.d = json.loads(Path(path).read_text(encoding="utf-8"))
        self.now = datetime.fromisoformat(self.d["now"])
        self.errors: list[str] = []

    def thaiwater_load(self):
        return self.d["load"]

    def thaiwater_graph(self, station_id: int):
        return self.d["graphs"].get(str(station_id), {"data": {"graph_data": []}})

    def bma(self):
        return self.d["bma"]

    def rain(self):
        return self.d.get("rain")


# ---------------------------------------------------------------- สรุปสถานี
def value_24h_ago(graph, latest_time: datetime):
    target = latest_time - timedelta(hours=24)
    best, best_gap = None, timedelta(hours=3)
    for p in (graph.get("data") or {}).get("graph_data") or []:
        v, t = num(p.get("value")), parse_th(p.get("datetime"))
        if v is None or t is None:
            continue
        gap = abs(t - target)
        if gap <= best_gap:
            best, best_gap = v, gap
    return best


def summarize_station(src, item, cfg):
    st = item["station"]
    wl = num(item.get("waterlevel_msl"))
    bank, ground = num(st.get("min_bank")), num(st.get("ground_level"))
    t = parse_th(item.get("waterlevel_datetime"))
    age_h = round((src.now - t).total_seconds() / 3600, 1) if t else None

    valid = wl is not None and not (bank is not None and ground is not None
                                    and (wl < ground - 2 or wl > bank + 3))
    pct = None
    if valid and bank is not None and ground is not None and bank > ground:
        pct = round((wl - ground) / (bank - ground) * 100, 1)
    elif valid:
        pct = num(item.get("storage_percent"))

    stale = age_h is None or age_h > cfg["stale_h"]
    q, qmax = num(item.get("discharge")), num(st.get("qmax"))

    trend, trend_m = None, None
    if valid and t is not None:
        try:
            prev = value_24h_ago(src.thaiwater_graph(st["id"]), t)
        except Exception as e:  # noqa: BLE001
            src.errors.append(f"กราฟ {cfg['code']}: {e}")
            prev = None
        if prev is not None and abs(wl - prev) <= OUTLIER_JUMP_M:
            trend_m = round(wl - prev, 2)
            trend = "up" if trend_m >= TREND_STEADY_M else "down" if trend_m <= -TREND_STEADY_M else "steady"

    return {
        "code": cfg["code"], "label": cfg["label"],
        "official_name": (st.get("tele_station_name") or {}).get("th"),
        "wl": wl if valid else None, "bank": bank,
        "to_bank": round(bank - wl, 2) if valid and bank is not None else None,
        "pct": pct, "q": q, "qmax": qmax,
        "q_pct": round(q / qmax * 100) if q is not None and qmax else None,
        "trend": trend, "trend_m": trend_m,
        "time": iso(t), "age_h": age_h, "stale": stale,
        "level": "gray" if (stale or not valid) else color_from_pct(pct),
        "source": "ThaiWater (สสน.)",
    }


def rain_class(mm: float) -> str:
    for upper, name in RAIN_CLASSES:
        if mm < upper:
            return name
    return RAIN_CLASSES[-1][1]


def summarize_rain(src, data):
    h = (data or {}).get("hourly") or {}
    times, vals = h.get("time") or [], h.get("precipitation") or []
    past, nxt = [], []
    for ts, v in zip(times, vals):
        t, mm = parse_th(ts.replace("T", " ")), num(v)
        if t is None or mm is None:
            continue
        (past if t <= src.now else nxt).append((t, mm))
    if not nxt and not past:
        return None
    past_mm = round(sum(m for _, m in past[-24:]), 1)
    next_mm = round(sum(m for _, m in nxt[:24]), 1)
    peak = max(nxt, key=lambda x: x[1]) if nxt else None
    return {
        "past24_mm": past_mm, "past24_class": rain_class(past_mm),
        "next24_mm": next_mm, "next24_class": rain_class(next_mm),
        "peak_mm_h": peak[1] if peak else None, "peak_time": iso(peak[0]) if peak and peak[1] >= 0.5 else None,
        "model_run": iso(src.now), "source": "Open-Meteo (CC BY 4.0)",
        "lat": RAIN_LAT, "lon": RAIN_LON,
    }


def summarize_bma(src, data, cfg):
    rows = (data or {}).get("dtTableWl") or []
    row = next((r for r in rows if r.get("flow_code") == cfg["bma_code"]), None)
    if not row:
        return None
    m = re.search(r"Date\((\d+)\)", row.get("site_timestamp") or "")
    t = datetime.fromtimestamp(int(m.group(1)) / 1000, TZ) if m else None
    age_h = round((src.now - t).total_seconds() / 3600, 1) if t else None
    stale = age_h is None or age_h > cfg["stale_h"] or row.get("chkStatustxt") == "ขัดข้อง"
    return {
        "code": cfg["bma_code"], "label": cfg["label"], "official_name": row.get("flow_name"),
        "wl": num(row.get("wl")), "bank": None, "to_bank": None, "pct": None,
        "q": num(row.get("flow")), "qmax": None, "q_pct": None, "trend": None, "trend_m": None,
        "time": iso(t), "age_h": age_h, "stale": stale,
        "level": "gray" if stale else "info",  # ยังไม่มีเกณฑ์ตลิ่งจาก กทม. → แสดงค่าอย่างเดียว
        "source": "สำนักการระบายน้ำ กทม.",
    }


def load_manual(now: datetime):
    try:
        m = json.loads(MANUAL.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    p = m.get("phra_narai") or {}
    q, d = num(p.get("q")), p.get("date")
    try:
        day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return None
    age_d = (now.date() - day.date()).days
    return {"q": q, "date": d, "age_days": age_d, "stale": age_d > MANUAL_STALE_DAYS,
            "open": (q or 0) > 0, "note": p.get("note") or ""}


def worst(levels):
    real = [lv for lv in levels if lv in ("green", "yellow", "orange", "red")]
    if not real:
        return "gray" if all(lv == "gray" for lv in levels) or not levels else "info"
    return max(real, key=lambda lv: RANK[lv])


# ---------------------------------------------------------------- ประกอบผล
def build(src, previous):
    errors = src.errors
    by_code = {}
    try:
        load = src.thaiwater_load()
        try:
            items = load["waterlevel_data"]["data"]
            if not isinstance(items, list):
                raise TypeError("data ไม่ใช่รายการ")
        except (TypeError, KeyError):
            # โครงสร้างที่ได้ไม่ตรงที่คาด → แสดงตัวอย่างข้อความที่ได้รับ เพื่อใช้ตรวจสาเหตุ
            raise RuntimeError(f"รูปแบบข้อมูลไม่ตรงที่คาด: {json.dumps(load, ensure_ascii=False)[:300]}")
        for it in items:
            by_code[it["station"].get("tele_station_oldcode")] = it
    except Exception as e:  # noqa: BLE001
        errors.append(f"ThaiWater: {e}")

    try:
        bma = src.bma()
    except Exception as e:  # noqa: BLE001
        errors.append(f"กทม.: {e}")
        bma = None

    rain = None
    try:
        rain = summarize_rain(src, src.rain())
    except Exception as e:  # noqa: BLE001
        errors.append(f"ฝน: {e}")
        rain = (previous or {}).get("rain")
        if rain:
            rain = {**rain, "stale": True}

    prev_st = {}
    for s in (previous or {}).get("stages", []):
        for x in s.get("stations") or []:
            prev_st[x["code"]] = x

    def station(cfg):
        it = by_code.get(cfg["code"])
        if it:
            return summarize_station(src, it, cfg)
        old = prev_st.get(cfg["code"])  # ใช้ค่าเดิม แต่ถือว่าเก่า
        if old:
            return {**old, "stale": True, "level": "gray"}
        return {"code": cfg["code"], "label": cfg["label"], "level": "gray", "stale": True}

    manual = load_manual(src.now)
    stages = []
    for cfg in STAGES:
        s = {k: v for k, v in cfg.items() if k not in ("stations",)}
        if cfg["kind"] in ("dam", "canal"):
            s["stations"] = [station(c) for c in cfg["stations"]]
            s["level"] = worst([x["level"] for x in s["stations"]])
            s["rising"] = any(x.get("trend") == "up" and not x.get("stale") for x in s["stations"])
        elif cfg["kind"] == "gate":
            s["gate"] = manual
            s["level"] = "gray" if (not manual or manual["stale"]) else "info"
            s["rising"] = bool(manual and manual["open"])
        elif cfg["kind"] == "bma":
            b = summarize_bma(src, bma, cfg) if bma else None
            if b is None and prev_st.get(cfg["bma_code"]):
                b = {**prev_st[cfg["bma_code"]], "stale": True, "level": "gray"}
            s["stations"] = [b] if b else []
            s["level"] = b["level"] if b else "gray"
            s["rising"] = False
        else:
            s["level"] = "link"
            s["rising"] = False
        stages.append(s)

    extra = [{**station(c), "why": c["why"]} for c in EXTRA]

    # ---- น้ำอยู่ด่านไหน: ด่านปลายน้ำสุดใน ①–④ ที่กำลังขึ้น (③ = เปิดส่งน้ำ)
    position = None
    for s in stages[:4]:
        if s["rising"]:
            position = s["n"]
    for s in stages:
        s["here"] = s["n"] == position

    summary = make_summary(stages, position, manual)
    return {
        "generated_at": iso(src.now),
        "position": position,
        "summary": summary,
        "stages": stages,
        "extra": extra,
        "rain": rain,
        "rules": {
            "colors": [
                {"level": "green", "label": "ปกติ", "range": "≤ 70% ของความจุลำน้ำ", "source": "สสน."},
                {"level": "yellow", "label": "น้ำมาก", "range": "70–90%", "source": "สสน."},
                {"level": "orange", "label": "ใกล้ตลิ่ง", "range": "90–100%", "source": "เกณฑ์ของแอปนี้ (ไม่ใช่เกณฑ์ทางการ)"},
                {"level": "red", "label": "ล้นตลิ่ง", "range": "> 100%", "source": "สสน."},
                {"level": "gray", "label": "ไม่มีข้อมูลล่าสุด", "range": "", "source": ""},
            ],
            "trend": f"เทียบกับ 24 ชม. ก่อน · เปลี่ยนน้อยกว่า {int(TREND_STEADY_M*100)} ซม. = คงที่",
        },
        "sources": SOURCES,
        "errors": errors,
    }


LEVEL_WORD = {"yellow": "น้ำมาก", "orange": "ใกล้ตลิ่ง", "red": "ล้นตลิ่ง"}


def make_summary(stages, position, manual):
    # บรรทัดสถานะ: ด่านในทุ่งที่แย่ที่สุด
    field = [(s, x) for s in stages if s.get("in_field") for x in (s.get("stations") or [])
             if x and x.get("level") in RANK and x["level"] not in ("gray", "info")]
    if field:
        s, x = max(field, key=lambda p: (RANK[p[1]["level"]], -(p[1].get("to_bank") or 99)))
        lv = x["level"]
        if lv == "green":
            status = "น้ำในคลองทุ่งรังสิตอยู่ในระดับปกติ"
        else:
            name = s["name"].split(" (")[0] + ("" if len(s.get("stations", [])) < 2 else " " + x["label"].split(" (")[0])
            tb = x.get("to_bank")
            dist = (f"เกินตลิ่ง {abs(tb):.2f} ม." if tb is not None and tb < 0
                    else f"ห่างตลิ่ง {tb:.2f} ม." if tb is not None else "")
            status = f"{name} {LEVEL_WORD[lv]}" + (f" ({dist})" if dist else "")
    else:
        lv, status = "gray", "ยังไม่มีข้อมูลล่าสุดของคลองในทุ่งรังสิต"

    # บรรทัดน้ำเหนือ
    lines = []
    gate_open = bool(manual and manual["open"])
    if manual is None:
        gate_txt = "ยังไม่มีข้อมูลประตูพระนารายณ์"
    elif gate_open:
        gate_txt = None
    else:
        gate_txt = ("ประตูพระนารายณ์ยังปิด (ยังไม่ส่งน้ำเข้าทุ่งรังสิตทางนี้)" if not manual["stale"]
                    else f"ประตูพระนารายณ์ปิด ตามข้อมูลล่าสุดวันที่ {manual['date']}")

    pasak, rama6, raph = stages[0], stages[1], stages[3]
    if gate_open:
        lines.append(f"กรมชลฯ กำลังส่งน้ำเข้าคลองระพีพัฒน์ {manual['q']:,.0f} ลบ.ม./วินาที · "
                     "ระพีพัฒน์มักขึ้นตามภายในราว 1–3 วัน")
    if position == 4 and raph["rising"]:
        lines.append("ระดับน้ำคลองระพีพัฒน์ (ขอบทุ่งรังสิต) กำลังสูงขึ้น")
    if rama6["rising"] and not gate_open:
        lines.append("น้ำเหนือกำลังมาถึงเขื่อนพระรามหก")
        if gate_txt:
            lines.append(gate_txt)
            gate_txt = None
    if pasak["rising"]:
        lines.append("น้ำเหนือก้อนใหม่ออกจากเขื่อนป่าสักแล้ว · คาดถึงเขื่อนพระรามหกในราว 1–2 วัน")
    if not lines:
        upstream = [x for s in stages[:2] for x in (s.get("stations") or [])]
        if upstream and all(x.get("level") == "gray" for x in upstream):
            lines.append("ยังไม่มีข้อมูลล่าสุดจากเขื่อนต้นน้ำ")
        else:
            lines.append("ยังไม่มีน้ำเหนือก้อนใหม่กำลังมา")
    if gate_txt:
        lines.append(gate_txt)

    notes = []
    if manual and manual["stale"]:
        notes.append(f"ค่าประตูพระนารายณ์เป็นของวันที่ {manual['date']} อาจไม่เป็นปัจจุบัน")
    return {"level": lv, "status_line": status, "water_lines": lines, "notes": notes}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", help="ไฟล์ข้อมูลตัวอย่าง (ทดสอบโดยไม่ต่อเน็ต)")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    src = Fixture(a.fixture) if a.fixture else Live(now_th())
    out = Path(a.out)
    previous = None
    if out.exists():
        try:
            previous = json.loads(out.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            previous = None

    result = build(src, previous)
    if any(e.startswith("ThaiWater:") for e in result["errors"]):
        print("ดึงข้อมูล ThaiWater ไม่ได้", file=sys.stderr)
        for e in result["errors"]:
            print("  ! ", e, file=sys.stderr)
        if previous is None:
            sys.exit(1)  # ไม่เคยมีข้อมูลเลย → แจ้งล้มเหลว
        # มีข้อมูลเดิม: เผยแพร่ต่อโดยสถานีที่ดึงไม่ได้ขึ้นสีเทา/ข้อมูลเก่า
        # (ค่าประตูพระนารายณ์ที่กรอกเองยังอัปเดตตามปกติ)
        print("  → ใช้ค่าสถานีเดิม ทำเครื่องหมายว่าเก่า", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    s = result["summary"]
    print(f"[{result['generated_at']}] {s['status_line']}")
    for ln in s["water_lines"]:
        print("  -", ln)
    for e in result["errors"]:
        print("  ! ", e, file=sys.stderr)


if __name__ == "__main__":
    main()

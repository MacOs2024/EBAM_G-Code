"""v4.2.9.27 backend tools: in-app G-code post-check, wire-freeze detection,
and a two-file G-code comparison. These formalize the console checks used during
the R8/R9 review so the operator can run them from the UI.

All parsing is based on the ring/link/dwell comments the EBAM generators emit,
plus raw G0/G1/G4/M67/M68 lines. Nothing here mutates G-code; read-only analysis.
"""
from __future__ import annotations
import math
import re
from typing import Any, Dict, List, Optional, Tuple

# --- comment patterns emitted by the generators -----------------------------
_RING_RE = re.compile(
    r"\(RING\s+(\d+)/(\d+)\s+R=([\d.]+)\s+CSPD=([\d.]+)\s+E0=([\d.]+)\s+E2=([\d.]+)\s+QV=([\d.]+)\)"
)
_LINK_RE = re.compile(
    r"\(HOT LINK\s+(\d+)\s+CSPD=([\d.]+)\s+E0=([\d.]+)\s+E2=([\d.]+)\)"
)
_DWELL_RE = re.compile(r"G4\s+P([\d.]+)\s+\(THERMAL_DWELL")
_G4_ANY_RE = re.compile(r"^G4\s+P([\d.]+)", re.MULTILINE)
_WRET_RE = re.compile(r"^G1\s+W-?[\d.]+", re.MULTILINE)


def _floats(m: re.Match, *idx: int) -> Tuple[float, ...]:
    return tuple(float(m.group(i)) for i in idx)


def _rings(gcode: str) -> List[Dict[str, float]]:
    out = []
    for m in _RING_RE.finditer(gcode):
        out.append(dict(n=int(m.group(1)), total=int(m.group(2)), R=float(m.group(3)),
                        cspd=float(m.group(4)), e0=float(m.group(5)),
                        e2=float(m.group(6)), qv=float(m.group(7))))
    return out


_XYLAYER_RE = re.compile(
    r"LAYER\s+(\d+)/(\d+)\s+Z=([\d.]+)\s+I=([\d.]+)\s+F=([\d.]+)mm/min\s+WIRE=([\d.]+)mm/s"
)


def _xy_layers(gcode: str, wire_diameter_mm: float = 1.2, voltage_kv: float = 60.0,
               efficiency: float = 1.0) -> List[Dict[str, float]]:
    """Parse XY-hatch layer headers and derive per-layer QV.

    v4.2.9.30: the traffic light used to parse only ring comments, so hatch/snake
    strategies were checked for nothing at all. Layer headers carry I, F and WIRE,
    which is enough to reconstruct the volumetric energy density.
    """
    area = math.pi * (float(wire_diameter_mm) ** 2) / 4.0
    out = []
    for m in _XYLAYER_RE.finditer(gcode):
        e0 = float(m.group(4))
        e2 = float(m.group(6))
        qv = (float(voltage_kv) * e0) / max(area * e2 * float(efficiency), 1e-9)
        out.append(dict(layer=int(m.group(1)), z=float(m.group(3)), e0=e0,
                        feed=float(m.group(5)), e2=e2, qv=qv))
    return out


def _links(gcode: str) -> List[Dict[str, float]]:
    return [dict(n=int(m.group(1)), cspd=float(m.group(2)), e0=float(m.group(3)),
                 e2=float(m.group(4))) for m in _LINK_RE.finditer(gcode)]


# --- Feature #2: post-generation safety check -------------------------------
def post_generation_check(gcode: str,
                          e0_cap_ma: float = 40.0,
                          e2_cap_mm_s: float = 50.0,
                          qv_floor_j_mm3: float = 55.0,
                          qv_ceiling_j_mm3: float = 90.0,
                          c_max_deg_min: float = 600.0,
                          min_beam_power_w: float = 900.0,
                          voltage_kv: float = 60.0,
                          wire_diameter_mm: float = 1.2) -> Dict[str, Any]:
    """Traffic-light check of a finished G-code against process limits.

    Returns dict with per-item status ('ok'|'warn'|'bad'), numbers, and a list
    of human messages. Purely read-only.
    """
    rings = _rings(gcode)
    links = _links(gcode)
    xy = _xy_layers(gcode, wire_diameter_mm=wire_diameter_mm, voltage_kv=voltage_kv)
    e0_all = [r["e0"] for r in rings] + [l["e0"] for l in links] + [x["e0"] for x in xy]
    e2_all = [r["e2"] for r in rings] + [l["e2"] for l in links] + [x["e2"] for x in xy]
    qv_all = [r["qv"] for r in rings] + [x["qv"] for x in xy]
    cs_all = [r["cspd"] for r in rings]
    lines = gcode.splitlines()

    items: List[Dict[str, Any]] = []

    def add(name: str, status: str, value: str, note: str = ""):
        items.append(dict(name=name, status=status, value=value, note=note))

    # E0 vs cap
    if e0_all:
        mx = max(e0_all)
        n_over = sum(1 for x in e0_all if x > e0_cap_ma + 1e-6)
        add("Ток пучка E0", "bad" if n_over else "ok",
            f"max {mx:.2f} мА (лимит {e0_cap_ma:.1f})",
            f"{n_over} ход(ов) выше лимита" if n_over else "в пределах лимита")
    # E2 vs cap and vs runaway band (>=45 historically dangerous)
    if e2_all:
        mx = max(e2_all)
        n_over = sum(1 for x in e2_all if x > e2_cap_mm_s + 1e-6)
        n_hot = sum(1 for x in e2_all if x >= 45.0)
        st = "bad" if n_over else ("warn" if n_hot else "ok")
        note = (f"{n_over} выше границы" if n_over else
                (f"{n_hot} колец E2>=45 (зона выхода проволоки из ванны)" if n_hot else "стабильно"))
        add("Подача проволоки E2", st, f"max {mx:.2f} мм/с (граница {e2_cap_mm_s:.1f})", note)
    # QV floor
    if qv_all:
        mn = min(qv_all)
        mx = max(qv_all)
        n_low = sum(1 for x in qv_all if x < qv_floor_j_mm3 - 1e-6)
        n_high = sum(1 for x in qv_all if x > qv_ceiling_j_mm3 + 1e-6)
        # v4.2.9.30: QV is now guarded from BOTH sides. Overheating (spatter,
        # evaporation, wavy top) was previously invisible to this check.
        if mn <= 42.0:
            st = "bad"
        elif mx >= 110.0:
            st = "bad"
        elif n_low or n_high:
            st = "warn"
        else:
            st = "ok"
        notes = []
        if n_low:
            notes.append(f"{n_low} участк. ниже {qv_floor_j_mm3:.0f} (порог отказа 42 — проволока лезет из ванны)")
        if n_high:
            notes.append(f"{n_high} участк. выше {qv_ceiling_j_mm3:.0f} (перегрев, брызги; отказ от 110)")
        add("Плотность энергии QV", st, f"{mn:.1f}..{mx:.1f} Дж/мм³ (норма {qv_floor_j_mm3:.0f}–{qv_ceiling_j_mm3:.0f})",
            "; ".join(notes) if notes else "в рабочей полосе")
    # C within limits
    if cs_all:
        add("Скорость стола C", "warn" if max(cs_all) > c_max_deg_min + 1.0 else "ok",
            f"{min(cs_all):.0f}..{max(cs_all):.0f} °/мин (лимит {c_max_deg_min:.0f})")
    # Beam power on rings
    if e0_all:
        pmin = voltage_kv * min(e0_all)
        add("Мощность пучка", "bad" if pmin < min_beam_power_w else "ok",
            f"min {pmin:.0f} Вт (порог проплава {min_beam_power_w:.0f})")
    # G0 under beam: scan for G0 while last E0 setpoint > 0
    g0_hot = 0
    beam_on = False
    for l in lines:
        s = l.strip()
        m = re.match(r"M6[78]\s+E0\s+Q([\d.]+)", s)
        if m:
            beam_on = float(m.group(1)) > 1e-6
        elif s.startswith("G0 ") and beam_on and any(a in s for a in ("X", "Y", "C")):
            g0_hot += 1
    add("Быстрые ходы G0 под лучом", "bad" if g0_hot else "ok",
        f"{g0_hot} шт", "перемещение с включённым лучом" if g0_hot else "нет")
    # thermal dwell coverage
    dwells = [float(x) for x in _DWELL_RE.findall(gcode)]
    big_g4 = [float(x) for x in _G4_ANY_RE.findall(gcode) if float(x) >= 10.0]
    add("Тепловые выдержки", "ok" if dwells else "warn",
        f"{len(dwells)} шт, Σ {sum(dwells)/60.0:.1f} мин" if dwells else "нет выдержек",
        "покрыты" if dwells else "если есть короткие слои — включите мин. цикл")

    # v4.2.9.30: analog setpoint density — one M68 per motion forces the LinuxCNC
    # planner to sync on every segment, which breaks G64 blending and makes motion choppy.
    _m68 = len(re.findall(r"^M6[78]\s", gcode, re.M))
    _g1 = max(len(re.findall(r"^G1\s", gcode, re.M)), 1)
    _ratio = _m68 / _g1
    # Only meaningful on real programs; a handful of moves is not a motion problem.
    add("Плотность уставок E0/E2", "warn" if (_ratio > 0.6 and _g1 >= 50) else "ok",
        f"{_m68} команд на {_g1} движений ({_ratio:.2f}/ход)",
        ("почти на каждом ходу — планировщик рвёт сглаживание G64. Включите «Упростить рампу "
         "проволоки»: она убирает промежуточные уставки внутри дорожки. Полностью снимает "
         "проблему только переход на M67 после HAL-кита — он синхронизируется с движением и "
         "очередь не рвёт. Часть уставок на перемычках убрать нельзя: там подача выключается "
         "физически") if _ratio > 0.6 else "приемлемо")

    order = {"bad": 0, "warn": 1, "ok": 2}
    overall = min((it["status"] for it in items), key=lambda s: order[s]) if items else "warn"
    return dict(status=overall, items=items,
                rings=len(rings), links=len(links), xy_layers=len(xy),
                dwell_count=len(dwells), dwell_total_s=round(sum(dwells), 1),
                big_g4_count=len(big_g4))


# --- Feature #11: wire-freeze (dwell without W-retract) ----------------------
def wire_freeze_check(gcode: str, dwell_threshold_s: float = 60.0) -> Dict[str, Any]:
    """Find long G4 dwells (beam nominally off) that are NOT bracketed by a W
    retract/approach, i.e. the wire tip is likely frozen into the pool.

    Heuristic: for each G4 P>=threshold, look at a window of lines around it; if
    no 'G1 W' move appears within +/- window, flag it.
    """
    lines = gcode.splitlines()
    window = 6
    flagged: List[Dict[str, Any]] = []
    total_long = 0
    for i, l in enumerate(lines):
        m = re.match(r"^G4\s+P([\d.]+)", l.strip())
        if not m:
            continue
        p = float(m.group(1))
        if p < dwell_threshold_s:
            continue
        total_long += 1
        lo = max(0, i - window)
        hi = min(len(lines), i + window + 1)
        has_w = any(_WRET_RE.match(x.strip()) for x in lines[lo:hi])
        if not has_w:
            flagged.append(dict(line=i + 1, seconds=p))
    status = "bad" if flagged else ("ok" if total_long else "warn")
    if flagged:
        msg = (f"{len(flagged)} длинных выдержек (>{dwell_threshold_s:.0f} с) без W-ретракта — "
               "кончик проволоки может вмёрзнуть в ванну. Добавьте W-цикл вокруг этих пауз.")
    elif total_long:
        msg = f"Все {total_long} длинных выдержек защищены W-ретрактом."
    else:
        msg = "Длинных выдержек не найдено."
    return dict(status=status, flagged=flagged, long_dwells=total_long, message=msg)


# --- Feature #6: compare two G-code files ------------------------------------
def _profile(gcode: str) -> Dict[str, Any]:
    rings = _rings(gcode)
    links = _links(gcode)
    # active time from ring/link feed (G93 => minutes = 1/F); fallback to C-feed law
    t_ring = 0.0
    for m in re.finditer(r"G1\s+C360\.000\s+F([\d.]+)", gcode):
        t_ring += 60.0 / float(m.group(1))
    t_link = 0.0
    for m in re.finditer(r"G1\s+X[-\d.]+\s+C5\.000\s+F([\d.]+)", gcode):
        t_link += 60.0 / float(m.group(1))
    g4 = [float(x) for x in _G4_ANY_RE.findall(gcode) if float(x) >= 10.0]
    e0 = [r["e0"] for r in rings] + [l["e0"] for l in links]
    e2 = [r["e2"] for r in rings] + [l["e2"] for l in links]
    qv = [r["qv"] for r in rings]
    cs = [r["cspd"] for r in rings]
    restrikes = gcode.count("beam OFF") + len(re.findall(r"THERMAL_DWELL", gcode))
    total_s = t_ring + t_link + sum(g4)
    return dict(rings=len(rings), links=len(links),
                e0_min=min(e0) if e0 else 0.0, e0_max=max(e0) if e0 else 0.0,
                e2_min=min(e2) if e2 else 0.0, e2_max=max(e2) if e2 else 0.0,
                qv_min=min(qv) if qv else 0.0, qv_max=max(qv) if qv else 0.0,
                c_min=min(cs) if cs else 0.0, c_max=max(cs) if cs else 0.0,
                dwell_count=len(g4), dwell_total_min=sum(g4) / 60.0,
                restrikes=restrikes, total_h=total_s / 3600.0,
                w_moves=len(_WRET_RE.findall(gcode)))


def compare_gcode(gcode_a: str, gcode_b: str,
                  name_a: str = "A", name_b: str = "B") -> Dict[str, Any]:
    """Compare two G-code files field-by-field for the operator."""
    pa, pb = _profile(gcode_a), _profile(gcode_b)
    identical = (gcode_a == gcode_b)
    rows: List[Dict[str, Any]] = []
    fields = [
        ("Колец", "rings", "%.0f"), ("Линков", "links", "%.0f"),
        ("Полное время, ч", "total_h", "%.2f"),
        ("E0 max, мА", "e0_max", "%.2f"), ("E2 max, мм/с", "e2_max", "%.2f"),
        ("QV min, Дж/мм³", "qv_min", "%.1f"),
        ("C min, °/мин", "c_min", "%.0f"), ("C max, °/мин", "c_max", "%.0f"),
        ("Выдержек", "dwell_count", "%.0f"),
        ("Σ выдержек, мин", "dwell_total_min", "%.1f"),
        ("Рестрайков", "restrikes", "%.0f"), ("W-ходов", "w_moves", "%.0f"),
    ]
    for label, key, fmt in fields:
        va, vb = pa[key], pb[key]
        delta = vb - va
        rows.append(dict(field=label, a=fmt % va, b=fmt % vb,
                         delta=("—" if abs(delta) < 1e-9 else fmt % delta)))
    return dict(identical=identical, name_a=name_a, name_b=name_b,
                rows=rows, profile_a=pa, profile_b=pb)


# ============================================================================
# v4.2.9.28 data features: bead calibration wizard, profiles, deposition journal
# ============================================================================
import json as _json
import time as _time


# --- Feature #1: single-bead TEST calibration -> recommended parameters ------
_OVERLAP = {"tom": 0.738, "fom": 0.667}


def calibrate_from_bead(bead_width_mm: float,
                        bead_height_mm: float,
                        overlap_model: str = "tom",
                        layer_height_fraction: float = 0.9,
                        wire_diameter_mm: float = 1.2,
                        deposition_efficiency: float = 0.97) -> Dict[str, Any]:
    """Turn single-bead TEST measurements into recommended build parameters.

    - hatch (radial step) = overlap_factor * width  (TOM/FOM)
    - layer height = fraction * measured bead height (fraction<1 for fusion)
    - reports overlap %, beads-per-100mm, and the volume-balance E2 target for a
      chosen linear speed is left to the app (depends on F). Here we return the
      geometry basis and a sanity note.
    """
    w = max(float(bead_width_mm), 0.0)
    h = max(float(bead_height_mm), 0.0)
    of = _OVERLAP.get((overlap_model or "tom").strip().lower(), 0.738)
    hatch = of * w
    lh = max(float(layer_height_fraction), 0.05) * h
    notes: List[str] = []
    status = "ok"
    if w <= 0 or h <= 0:
        status = "bad"
        notes.append("Введите положительные ширину и высоту валика из TEST.")
    else:
        if w / max(h, 1e-9) < 1.2:
            notes.append("Валик узкий и высокий (w/h<1.2) — возможен недогрев/непровар; проверьте энергию.")
        if w / max(h, 1e-9) > 6.0:
            notes.append("Валик очень широкий и плоский (w/h>6) — возможен перегрев/растекание.")
    area = math.pi * float(wire_diameter_mm) ** 2 / 4.0
    return dict(
        status=status,
        hatch_spacing_mm=round(hatch, 3),
        layer_height_mm=round(lh, 3),
        overlap_percent=round((1.0 - of) * 100.0, 1),
        overlap_model=(overlap_model or "tom").strip().lower(),
        beads_per_100mm=round(100.0 / max(hatch, 1e-9), 1),
        wire_area_mm2=round(area, 4),
        aspect_w_over_h=round(w / max(h, 1e-9), 2),
        notes=notes,
        # E2 for a given linear speed v (mm/s): E2 = layer*hatch*v/(area*eff)
        e2_hint="E2 = слой·шаг·v / (A·η); подставляется приложением от фактической F",
    )


# --- Feature #4: named material/part profiles (save/load bundle) -------------
def make_profile(name: str, settings_json: str,
                 bead_width_mm: float = 0.0, bead_height_mm: float = 0.0,
                 note: str = "") -> str:
    """Wrap a settings JSON + optional TEST metadata into a named profile JSON."""
    try:
        settings_obj = _json.loads(settings_json) if settings_json else {}
    except Exception:
        settings_obj = {}
    payload = dict(
        kind="ebam_profile", version=1, name=str(name),
        created=_time.strftime("%Y-%m-%d %H:%M:%S"),
        bead_width_mm=float(bead_width_mm or 0.0),
        bead_height_mm=float(bead_height_mm or 0.0),
        note=str(note or ""),
        settings=settings_obj,
    )
    return _json.dumps(payload, ensure_ascii=False, indent=2)


def read_profile(profile_json: str) -> Dict[str, Any]:
    """Parse a profile bundle; returns dict with name/settings_json/meta or error."""
    try:
        d = _json.loads(profile_json)
    except Exception as e:
        return dict(ok=False, error=f"Не JSON: {e}")
    if d.get("kind") != "ebam_profile":
        # tolerate a bare settings json too
        return dict(ok=True, name="(без имени)", meta={}, settings_json=profile_json, bare=True)
    return dict(ok=True, name=d.get("name", "(без имени)"),
                meta={k: d.get(k) for k in ("created", "bead_width_mm", "bead_height_mm", "note")},
                settings_json=_json.dumps(d.get("settings", {}), ensure_ascii=False, indent=2),
                bare=False)


# --- Feature #5: deposition journal (append-only records) --------------------
def make_journal_entry(part_name: str, file_sha: str,
                        planned: Dict[str, float], measured: Dict[str, float],
                        verdict: str = "", photos: int = 0) -> Dict[str, Any]:
    """Build one journal record comparing planned vs measured dimensions."""
    deltas = {}
    for k, pv in (planned or {}).items():
        mv = (measured or {}).get(k)
        if mv is not None and pv:
            deltas[k] = round(float(mv) - float(pv), 3)
    return dict(
        date=_time.strftime("%Y-%m-%d %H:%M"),
        part=str(part_name), sha=str(file_sha)[:16],
        planned=dict(planned or {}), measured=dict(measured or {}),
        deltas=deltas, verdict=str(verdict or ""), photos=int(photos or 0),
    )


def journal_to_json(entries: List[Dict[str, Any]]) -> str:
    return _json.dumps(dict(kind="ebam_journal", version=1, entries=entries),
                       ensure_ascii=False, indent=2)


def journal_from_json(text: str) -> List[Dict[str, Any]]:
    try:
        d = _json.loads(text)
    except Exception:
        return []
    if isinstance(d, dict) and d.get("kind") == "ebam_journal":
        return list(d.get("entries", []))
    if isinstance(d, list):
        return d
    return []


# --- Feature #3: per-ring layer profile for the simulator --------------------
def layer_ring_profile(gcode: str) -> Dict[str, Any]:
    """Extract per-layer ring data (R, QV, E2, E0, CSPD) for a coloured
    layer-by-layer preview. Works on the ring comments our generators emit.

    Returns {'layers': [{'layer': n, 'z0':.., 'z1':.., 'zone':.., 'rings':[...]}, ...],
             'qv_min','qv_max','has_data'}.
    """
    lay_re = re.compile(
        r"LAYER\s+(\d+)/(\d+)\s+Z=([\d.]+)\.\.([\d.]+)\s+ZONE=(\S+)"
    )
    lines = gcode.splitlines()
    layers: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in lines:
        lm = lay_re.search(ln)
        if lm:
            cur = dict(layer=int(lm.group(1)), total=int(lm.group(2)),
                       z0=float(lm.group(3)), z1=float(lm.group(4)),
                       zone=lm.group(5), rings=[])
            layers.append(cur)
            continue
        rm = _RING_RE.search(ln)
        if rm and cur is not None:
            cur["rings"].append(dict(R=float(rm.group(3)), cspd=float(rm.group(4)),
                                     e0=float(rm.group(5)), e2=float(rm.group(6)),
                                     qv=float(rm.group(7))))
    # if no LAYER markers (e.g. XY strategies), synthesize a single bucket from rings
    if not layers:
        allr = _rings(gcode)
        if allr:
            layers = [dict(layer=1, total=1, z0=0.0, z1=0.0, zone="ALL",
                           rings=[dict(R=r["R"], cspd=r["cspd"], e0=r["e0"],
                                       e2=r["e2"], qv=r["qv"]) for r in allr])]
    qvs = [rr["qv"] for L in layers for rr in L["rings"]]
    return dict(layers=layers, qv_min=min(qvs) if qvs else 0.0,
                qv_max=max(qvs) if qvs else 0.0, has_data=bool(qvs))

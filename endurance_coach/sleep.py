"""Sleep analysis: duration, score and stages, rhythm and regularity, and what the day before does to the night.

Nights are dated by wake-up day (as Garmin and COROS do), so the night of 2026-10-24 has the record 2026-10-25 and
"the day before" is 2026-10-24. Bed and wake times are minutes from midnight of the wake-up day (23:30 = -30).

Influences compare the nights after one kind of day with the nights after other days: a difference in means with
a Welch t value turned into a plain verdict ("clear" |t| >= 2.5, "possible" >= 1.5). That is association, not proof:
hard days, late sessions and late bedtimes often come together, which the text says where it matters.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Callable

from . import athlete, health, store
from .config import NON_TRAINING_SPORTS
from .util import num, parse_local, rnd

DEFAULT_NEED_H = 8.0
_LATE_H = 3.0            # a session ending less than this before bed counts as late
_EVENING_MOBILITY_H = 3.0
_LATE_BED_MIN = 45       # bedtime this much later than usual
_MIN_N = 4               # fewer nights per group than this: no verdict
_WINDOW_DAYS = 180

# Compared per influence: key, label, unit, which way is better (+1 higher, -1 lower, 0 neutral), decimals.
METRICS = (
    ("score", "Sleep score", "", 1, 0),
    ("asleep_min", "Time asleep", "min", 1, 0),
    ("efficiency", "Efficiency", "%", 1, 0),
    ("deep_min", "Deep sleep", "min", 1, 0),
    ("rem_min", "REM sleep", "min", 1, 0),
    ("awake_min", "Awake in bed", "min", -1, 0),
    ("sleep_hr", "Sleep HR", "bpm", -1, 0),
    ("hrv", "HRV", "ms", 1, 0),
    ("rhr", "Resting HR", "bpm", -1, 0),
    ("bed_min", "Bedtime", "min", 0, 0),
)
_METRIC = {m[0]: m for m in METRICS}
# The night itself; sleep HR, HRV and resting HR are recovery markers that move after hard days even when sleep is fine.
_QUALITY = {"score", "asleep_min", "efficiency", "deep_min", "rem_min", "awake_min"}


def clock(minutes: float | None) -> str | None:
    """Minutes from midnight of the wake-up day -> 'HH:MM' (negative = the evening before)."""
    if minutes is None:
        return None
    m = round(minutes) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _rel_min(ts: str | None, day: date) -> float | None:
    dt = parse_local(ts)
    return (dt - datetime.combine(day, datetime.min.time())).total_seconds() / 60 if dt else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _vals(nights: list[dict[str, Any]], key: str) -> list[float]:
    return [n[key] for n in nights if isinstance(n.get(key), (int, float))]


# ---------------------------------------------------------------- nights

def _activities(start: date, end: date) -> list[dict[str, Any]]:
    out = []
    for a in store.activities(since=start.isoformat(), until=end.isoformat()):
        begin = parse_local(a["start_local"])
        s, m = a["summary"], a["metrics"]
        seconds = (num(s.get("total_time_s")) or num(s.get("elapsed_s")) or num(s.get("duration_s"))
                   or num(m.get("recorded_s")) or 0)
        if not begin:
            continue
        out.append({"date": a["date"], "start": begin, "end": begin + timedelta(seconds=seconds), "sport": a["sport"],
                    "type": a["type"], "name": a["name"], "minutes": seconds / 60,
                    "tss": num(m.get("tss")) or 0.0, "if": num(m.get("if")),
                    "training": a["sport"] not in NON_TRAINING_SPORTS})
    return out


def _night(rec: dict[str, Any], prev: dict[str, Any], acts: list[dict[str, Any]]) -> dict[str, Any] | None:
    day = date.fromisoformat(rec["date"])
    asleep = rec["sleep_h"] * 60 if rec["sleep_h"] else None
    if asleep is None and rec["sleep_score"] is None:
        return None
    bed, wake = _rel_min(rec["sleep_start"], day), _rel_min(rec["sleep_end"], day)
    if bed is None or wake is None or not (-720 <= bed < wake <= 1080):
        bed = wake = None
    in_bed = wake - bed if bed is not None else None

    # The day before: everything that ended between 04:00 that day and bed (or that was logged on it, without times).
    evening_start = datetime.combine(day - timedelta(days=1), datetime.min.time()) + timedelta(hours=4)
    bed_dt = datetime.combine(day, datetime.min.time()) + timedelta(minutes=bed) if bed is not None else None
    before = [a for a in acts if (evening_start <= a["end"] <= bed_dt + timedelta(minutes=30) if bed_dt
                                  else a["date"] == (day - timedelta(days=1)).isoformat())]
    training = [a for a in before if a["training"]]
    last = max(training, key=lambda a: a["end"], default=None)
    mobility = [a for a in before if a["sport"] == "mobility" and bed_dt
                and a["end"] >= bed_dt - timedelta(hours=_EVENING_MOBILITY_H)]
    stages = [rec[k] for k in ("deep_min", "rem_min", "light_min")]
    staged = sum(stages) if all(v is not None for v in stages) else asleep

    night = {
        "date": rec["date"],
        "night_of": (day - timedelta(days=1)).isoformat(),
        "weekday": (day - timedelta(days=1)).weekday(),
        "bed_min": rnd(bed), "wake_min": rnd(wake),
        "mid_min": rnd((bed + wake) / 2) if bed is not None else None,
        "bed": clock(bed), "wake": clock(wake),
        "in_bed_min": rnd(in_bed),
        "asleep_min": rnd(asleep),
        "efficiency": rnd(min(100.0, asleep / in_bed * 100), 1) if asleep and in_bed else None,
        "score": rec["sleep_score"],
        "deep_min": rec["deep_min"], "rem_min": rec["rem_min"], "light_min": rec["light_min"],
        "deep_pct": rnd(rec["deep_min"] / staged * 100, 1) if rec["deep_min"] and staged else None,
        "rem_pct": rnd(rec["rem_min"] / staged * 100, 1) if rec["rem_min"] and staged else None,
        "awake_min": rec["awake_min"], "awake_count": rec["awake_count"], "naps_min": rec["naps_min"],
        "sleep_hr": rec["sleep_hr_avg"], "sleep_hr_min": rec["sleep_hr_min"],
        "hrv": rec["hrv"], "rhr": rec["rhr"],
        "stress_prev": prev.get("stress_avg"), "steps_prev": prev.get("steps"),
        "cycle_phase": rec["cycle_phase"] or prev.get("cycle_phase"),
        "tss": round(sum(a["tss"] for a in training)),
        "train_min": round(sum(a["minutes"] for a in training)),
        "max_if": max((a["if"] for a in training if a["if"]), default=None),
    }
    if last:
        night["last_session"] = f"{last['type'] or last['sport']} {last['end']:%H:%M}"
        night["last_sport"] = last["sport"]
        if bed_dt:
            night["last_end_h"] = round((bed_dt - last["end"]).total_seconds() / 3600, 1)
    if mobility:
        night["evening_mobility"] = ", ".join(f"{a['type'] or 'Mobility'} {a['start']:%H:%M} ({a['minutes']:.0f} min)"
                                              for a in mobility)
        night["mobility_min"] = round(sum(a["minutes"] for a in mobility))
    return night


def nights(start: date, end: date) -> list[dict[str, Any]]:
    recs = health.rows(start - timedelta(days=1), end)
    acts = _activities(start - timedelta(days=1), end)
    out = []
    for prev, rec in zip(recs, recs[1:]):
        n = _night(rec, prev, acts)
        if n:
            out.append(n)
    return out


# ---------------------------------------------------------------- statistics

def _compare(group: list[dict], rest: list[dict], key: str) -> dict[str, Any]:
    _, label, unit, better, dec = _METRIC[key]
    a, b = _vals(group, key), _vals(rest, key)
    out: dict[str, Any] = {"metric": key, "label": label, "unit": unit, "n": len(a), "n_rest": len(b)}
    if not a or not b:
        return out | {"verdict": "no_data"}
    diff = statistics.fmean(a) - statistics.fmean(b)
    out |= {"mean": rnd(statistics.fmean(a), dec), "mean_rest": rnd(statistics.fmean(b), dec), "diff": rnd(diff, 1)}
    if len(a) < _MIN_N or len(b) < _MIN_N:
        return out | {"verdict": "too_few"}
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    t = diff / se if se else 0.0
    out["verdict"] = "clear" if abs(t) >= 2.5 else "possible" if abs(t) >= 1.5 else "none"
    if out["verdict"] != "none" and better:
        out["effect"] = "better" if diff * better > 0 else "worse"
    return out


def _correlation(xs: list[dict], xkey: str, ykey: str, label: str) -> dict[str, Any] | None:
    pairs = [(n[xkey], n[ykey]) for n in xs if isinstance(n.get(xkey), (int, float)) and isinstance(n.get(ykey), (int, float))]
    if len(pairs) < 10:
        return None
    try:
        r = statistics.correlation(*zip(*pairs), method="ranked")
    except statistics.StatisticsError:  # one of the series is constant
        return None
    t = r * math.sqrt((len(pairs) - 2) / max(1e-9, 1 - r * r))
    strength = "none" if abs(r) < 0.1 else "weak" if abs(r) < 0.3 else "moderate" if abs(r) < 0.5 else "strong"
    return {"x": xkey, "y": ykey, "label": label, "n": len(pairs), "rho": round(r, 2), "strength": strength,
            "clear": abs(t) >= 2.5 and strength != "none"}


def _sd(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 3 else None


def _period(ns: list[dict], need_min: float) -> dict[str, Any]:
    asleep = _vals(ns, "asleep_min")
    return {
        "nights": len(ns),
        "asleep_h": rnd(_mean(asleep) / 60, 2) if asleep else None,
        "score": rnd(_mean(_vals(ns, "score")), 1),
        "efficiency": rnd(_mean(_vals(ns, "efficiency")), 1),
        "deep_pct": rnd(_mean(_vals(ns, "deep_pct")), 1),
        "rem_pct": rnd(_mean(_vals(ns, "rem_pct")), 1),
        "awake_min": rnd(_mean(_vals(ns, "awake_min"))),
        "sleep_hr": rnd(_mean(_vals(ns, "sleep_hr")), 1),
        "hrv": rnd(_mean(_vals(ns, "hrv")), 1),
        "bed": clock(_mean(_vals(ns, "bed_min"))),
        "wake": clock(_mean(_vals(ns, "wake_min"))),
        "nights_7h_pct": rnd(sum(v >= 420 for v in asleep) / len(asleep) * 100) if asleep else None,
        "nights_need_pct": rnd(sum(v >= need_min - 15 for v in asleep) / len(asleep) * 100) if asleep else None,
    }


def _rhythm(ns: list[dict]) -> dict[str, Any]:
    timed = [n for n in ns if n["bed_min"] is not None]
    if len(timed) < 5:
        return {"nights": len(timed)}
    bed, wake, mid = _vals(timed, "bed_min"), _vals(timed, "wake_min"), _vals(timed, "mid_min")
    bed_med, wake_med = statistics.median(bed), statistics.median(wake)
    steps = [abs(b["mid_min"] - a["mid_min"]) for a, b in zip(timed, timed[1:])
             if date.fromisoformat(b["date"]) - date.fromisoformat(a["date"]) == timedelta(days=1)]
    free = [n["mid_min"] for n in timed if n["weekday"] in (4, 5)]      # Friday and Saturday nights
    work = [n["mid_min"] for n in timed if n["weekday"] not in (4, 5)]
    mid_sd = _sd(mid)
    recent = _sd(_vals(timed[-14:], "mid_min"))
    rating = (None if mid_sd is None else "very regular" if mid_sd < 30 else "fairly regular" if mid_sd < 45
              else "variable" if mid_sd < 60 else "irregular")
    return {
        "nights": len(timed),
        "bed_median": clock(bed_med), "wake_median": clock(wake_med), "mid_median": clock(statistics.median(mid)),
        "bed_median_min": rnd(bed_med), "wake_median_min": rnd(wake_med),
        "bed_sd_min": rnd(_sd(bed)), "wake_sd_min": rnd(_sd(wake)), "mid_sd_min": rnd(mid_sd),
        "mid_sd_14d_min": rnd(recent),
        "night_to_night_shift_min": rnd(_mean(steps)),
        "bed_within_30_pct": rnd(sum(abs(b - bed_med) <= 30 for b in bed) / len(bed) * 100),
        "wake_within_30_pct": rnd(sum(abs(w - wake_med) <= 30 for w in wake) / len(wake) * 100),
        "social_jetlag_min": rnd(statistics.fmean(free) - statistics.fmean(work)) if len(free) >= 2 and len(work) >= 2 else None,
        "rating": rating,
    }


def _weekdays(ns: list[dict]) -> list[dict[str, Any]]:
    out = []
    for wd, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
        g = [n for n in ns if n["weekday"] == wd]
        out.append({"night": name, "n": len(g),
                    "asleep_h": rnd(_mean(_vals(g, "asleep_min")) / 60, 2) if _vals(g, "asleep_min") else None,
                    "score": rnd(_mean(_vals(g, "score")), 1), "bed": clock(_mean(_vals(g, "bed_min"))),
                    "wake": clock(_mean(_vals(g, "wake_min"))), "tss": rnd(_mean(_vals(g, "tss")))})
    return out


def _load_bins(ns: list[dict]) -> list[dict[str, Any]]:
    bins = (("Rest day", lambda n: n["train_min"] < 15), ("< 50 TSS", lambda n: n["train_min"] >= 15 and n["tss"] < 50),
            ("50–100 TSS", lambda n: 50 <= n["tss"] < 100), ("≥ 100 TSS", lambda n: n["tss"] >= 100))
    out = []
    for label, pred in bins:
        g = [n for n in ns if pred(n)]
        out.append({"label": label, "n": len(g), **{k: rnd(_mean(_vals(g, k)), 1)
                    for k in ("score", "asleep_min", "deep_min", "awake_min", "sleep_hr", "hrv", "tss")}})
    return out


# ---------------------------------------------------------------- influences

def _influence(key: str, question: str, group_label: str, rest_label: str, group: list[dict], rest: list[dict],
               metrics: tuple[str, ...], note: str | None = None) -> dict[str, Any]:
    rows = [_compare(group, rest, m) for m in metrics]
    out: dict[str, Any] = {"key": key, "question": question, "group": group_label, "rest": rest_label,
                           "n": len(group), "n_rest": len(rest), "metrics": rows}
    for extra in ("tss", "bed_min"):
        g, r = _vals(group, extra), _vals(rest, extra)
        if g and r:
            out[f"{extra}_median"] = [rnd(statistics.median(g)), rnd(statistics.median(r))]
    out["summary"] = _influence_text(out)
    if note:
        out["note"] = note
    return out


def _fmt_diff(row: dict[str, Any]) -> str:
    d, unit = row["diff"], row["unit"]
    if row["metric"] == "bed_min":
        return f"{abs(d):.0f} min {'later' if d > 0 else 'earlier'} to bed"
    word = "higher" if d > 0 else "lower"
    if row["metric"] in ("asleep_min", "deep_min", "rem_min", "awake_min"):
        word = "more" if d > 0 else "less"
    label = row["label"] if row["label"][:3].isupper() else row["label"][0].lower() + row["label"][1:]
    return f"{label} {abs(d):.0f}{' ' + unit if unit and unit != '%' else unit} {word}"


def _influence_text(inf: dict[str, Any]) -> str:
    if inf["n"] < _MIN_N or inf["n_rest"] < _MIN_N:
        return f"Only {inf['n']} vs {inf['n_rest']} nights: too few to say yet."
    rows = [r for r in inf["metrics"] if r.get("verdict") not in ("no_data", None)]
    if not rows:
        return "No data for this comparison yet."
    clear = [r for r in rows if r["verdict"] == "clear" and r["metric"] != "bed_min"]
    possible = [r for r in rows if r["verdict"] == "possible" and r["metric"] != "bed_min"]
    if not clear and not possible:
        return f"No measurable difference ({inf['n']} vs {inf['n_rest']} nights)."
    parts = []
    if clear:
        parts.append("Clear: " + ", ".join(_fmt_diff(r) for r in clear[:4]))
    if possible:
        parts.append("Possible: " + ", ".join(_fmt_diff(r) for r in possible[:3]))
    return f"{'; '.join(parts)} ({inf['n']} vs {inf['n_rest']} nights)."


def _influences(ns: list[dict], train_days: list[dict], hard_tss: float) -> list[dict[str, Any]]:
    core = ("score", "asleep_min", "efficiency", "deep_min", "rem_min", "awake_min", "sleep_hr", "hrv", "rhr")
    out = []
    rest_days = [n for n in ns if n["train_min"] < 15]
    out.append(_influence("training", "Do training days change the night after?", "After training", "After rest days",
                          train_days, rest_days, core))
    hard = [n for n in train_days if n["tss"] >= hard_tss]
    easier = [n for n in train_days if n["tss"] < hard_tss]
    out.append(_influence("hard", f"Do your hardest days (≥ {hard_tss:.0f} TSS) change sleep?",
                          f"After ≥ {hard_tss:.0f} TSS", "After easier training days", hard, easier, core))
    timed = [n for n in train_days if n.get("last_end_h") is not None and n.get("last_sport") not in NON_TRAINING_SPORTS]
    late = [n for n in timed if n["last_end_h"] < _LATE_H]
    early = [n for n in timed if n["last_end_h"] >= _LATE_H]
    out.append(_influence("late", f"Do sessions ending less than {_LATE_H:.0f} h before bed hurt sleep?",
                          f"Session ended < {_LATE_H:.0f} h before bed", f"Ended ≥ {_LATE_H:.0f} h before bed",
                          late, early, core + ("bed_min",),
                          note="Training days only, so rest days do not blur it. Compare the TSS medians: if late "
                               "days were also the harder ones, part of the difference is load."))
    late_hard = [n for n in late if n["tss"] >= hard_tss or (n["max_if"] or 0) >= 0.85]
    out.append(_influence("late_hard", "And late sessions that were hard (IF ≥ 0.85 or a top-quarter TSS)?",
                          "Late and hard", f"Ended ≥ {_LATE_H:.0f} h before bed", late_hard, early, core))
    yoga = [n for n in ns if n.get("evening_mobility")]
    calm = [n for n in ns if not n.get("evening_mobility") and (n.get("last_end_h") is None or n["last_end_h"] >= _LATE_H)
            and n["bed_min"] is not None]
    out.append(_influence("yoga", "Does yoga or mobility in the evening help?",
                          f"Yoga / mobility in the {_EVENING_MOBILITY_H:.0f} h before bed", "Other evenings",
                          yoga, calm, core + ("bed_min",),
                          note="Other evenings exclude late training, so the yoga effect is not mixed up with it."))
    stressed = [n for n in ns if n.get("stress_prev") is not None]
    if len(stressed) >= 8:
        cut = statistics.quantiles(_vals(stressed, "stress_prev"), n=4)[2]
        out.append(_influence("stress", f"Do high-stress days (daily stress ≥ {cut:.0f}) cost sleep?",
                              f"Stress ≥ {cut:.0f}", "Calmer days", [n for n in stressed if n["stress_prev"] >= cut],
                              [n for n in stressed if n["stress_prev"] < cut], core,
                              note="The watch's all-day stress includes training, so hard days score high too."))
    timed_all = [n for n in ns if n["bed_min"] is not None]
    if len(timed_all) >= 8:
        med = statistics.median(_vals(timed_all, "bed_min"))
        out.append(_influence("late_bed", f"What does going to bed late (after {clock(med + _LATE_BED_MIN)}) cost?",
                              f"Bed after {clock(med + _LATE_BED_MIN)}", "Usual bedtime",
                              [n for n in timed_all if n["bed_min"] >= med + _LATE_BED_MIN],
                              [n for n in timed_all if n["bed_min"] < med + _LATE_BED_MIN], core))
    out.append(_influence("weekend", "Are weekend nights different?", "Friday and Saturday nights", "Sunday to Thursday",
                          [n for n in ns if n["weekday"] in (4, 5)], [n for n in ns if n["weekday"] not in (4, 5)],
                          core + ("bed_min",)))
    return out


def _cycle(ns: list[dict]) -> list[dict[str, Any]]:
    phases: dict[str, list[dict]] = {}
    for n in ns:
        if n.get("cycle_phase"):
            phases.setdefault(n["cycle_phase"], []).append(n)
    return [{"phase": p, "n": len(g), "score": rnd(_mean(_vals(g, "score")), 1),
             "asleep_h": rnd(_mean(_vals(g, "asleep_min")) / 60, 2) if _vals(g, "asleep_min") else None,
             "awake_min": rnd(_mean(_vals(g, "awake_min"))), "hrv": rnd(_mean(_vals(g, "hrv")), 1),
             "sleep_hr": rnd(_mean(_vals(g, "sleep_hr")), 1)} for p, g in phases.items()] if len(phases) >= 2 else []


# ---------------------------------------------------------------- findings

def _findings(s: dict[str, Any]) -> list[dict[str, str]]:
    """What to improve and what works, most important first."""
    out: list[dict[str, str]] = []
    need, p30, rh, inf = s["need_h"], s["periods"]["30d"], s["rhythm"], {i["key"]: i for i in s["influences"]}

    def add(level: str, title: str, text: str) -> None:
        out.append({"level": level, "title": title, "text": text})

    if p30["asleep_h"]:
        short = need - p30["asleep_h"]
        if short >= 0.5:
            target = s.get("target_bedtime")
            add("improve", f"Sleep {short * 60:.0f} min more per night",
                f"You average {p30['asleep_h']:.1f} h asleep over 30 days against a need of about {need:g} h; only "
                f"{p30['nights_need_pct']}% of nights reach it. Sleep is where training adapts: move bedtime earlier "
                f"in 15-minute steps" + (f" toward lights-out by {target} for your usual {s['target_wake']} wake-up." if target else "."))
        elif p30["asleep_h"] >= need - 0.15:
            add("good", "Enough sleep", f"{p30['asleep_h']:.1f} h asleep on average over 30 days, at your need of {need:g} h.")
    if s["debt_14d_h"] and s["debt_14d_h"] >= 5:
        add("improve", f"{s['debt_14d_h']:.0f} h sleep debt in 14 days",
            "Pay it back over the next nights (30-60 min extra, or a 20-30 min early-afternoon nap) and keep intensity "
            "moderate until it is down. A single long lie-in only shifts your rhythm.")
    if rh.get("mid_sd_min") is not None:
        if rh["mid_sd_min"] >= 45:
            add("improve", "Make the rhythm steadier",
                f"Your sleep midpoint varies by ±{rh['mid_sd_min']} min (bed ±{rh['bed_sd_min']}, wake ±{rh['wake_sd_min']}); "
                f"only {rh['bed_within_30_pct']}% of nights start within 30 min of {rh['bed_median']}. A fixed wake-up time, "
                "weekends included, is the strongest anchor; bedtime follows.")
        elif rh["mid_sd_min"] < 30:
            add("good", "Very regular rhythm", f"Sleep midpoint within ±{rh['mid_sd_min']} min: keep it.")
    if rh.get("social_jetlag_min") is not None and abs(rh["social_jetlag_min"]) >= 60:
        add("improve", f"{abs(rh['social_jetlag_min']):.0f} min weekend shift",
            "Friday and Saturday nights run that much later than the rest of the week (social jet lag), which makes "
            "Sunday night and Monday harder. Keep the weekend wake-up within an hour of the weekday one.")
    if p30["efficiency"] is not None and p30["efficiency"] < 85:
        add("improve", "Time awake in bed",
            f"Efficiency {p30['efficiency']:.0f}% (~{p30['awake_min']} min awake per night). Cool, dark room, no screens "
            "in bed, and get up if you lie awake for more than 20 min; caffeine after 14:00 and alcohol both show up here.")
    elif p30["efficiency"] is not None and p30["efficiency"] >= 90:
        add("good", "Efficient sleep", f"{p30['efficiency']:.0f}% of time in bed asleep.")

    def effect_of(key: str) -> tuple[list[dict], list[dict]]:
        rows = [r for r in inf.get(key, {}).get("metrics", []) if r.get("verdict") in ("clear", "possible")
                and r["metric"] != "bed_min"]
        return [r for r in rows if r.get("effect") == "worse"], [r for r in rows if r.get("effect") == "better"]

    for key, worse_title, worse_text, ok_title in (
        ("training", "Training days cost sleep",
         "Load and timing together, see the comparisons below: refuel after sessions and keep training evenings calm.",
         None),
        ("late_hard", "Hard sessions late in the day cost sleep",
         "Put intensity in the morning or at midday, or finish it at least 3 h before bed; late sessions should be easy.",
         None),
        ("late", "Late sessions cost sleep",
         f"Aim to finish training at least {_LATE_H:.0f} h before bed, and eat and shower soon after a late one.",
         "Late sessions don't hurt your sleep"),
        ("hard", "Hard days cost sleep",
         "Refuel well after big days (carbs and protein at dinner), keep the evening calm and protect bedtime.",
         None),
        ("stress", "Stressful days cost sleep",
         "Wind down for 30-60 min before bed on high-stress days: breathing, reading, no work in the evening.", None),
        ("late_bed", "Late nights are short nights",
         "Going to bed late is not made up in the morning. Protect your usual bedtime.", None),
    ):
        i = inf.get(key)
        if not i or i["n"] < _MIN_N or i["n_rest"] < _MIN_N:
            continue
        worse, better = effect_of(key)
        worse_q, better_q = [r for r in worse if r["metric"] in _QUALITY], [r for r in better if r["metric"] in _QUALITY]
        if worse_q and len(worse_q) >= len(better_q):
            add("improve", worse_title, f"{i['summary']} {worse_text}")
        elif worse and key in ("hard", "stress", "late", "late_hard"):
            add("info", f"{inf[key]['group']}: the body is still working",
                f"{i['summary']} Higher sleep HR or lower HRV after such days is recovery at work; the sleep itself "
                "holds up. Treat the next morning's readiness with that in mind.")
        elif ok_title and not worse:
            add("good", ok_title, i["summary"])
    yoga = inf.get("yoga")
    if yoga:
        worse, better = effect_of("yoga")
        if yoga["n"] < _MIN_N:
            add("info", "Evening yoga: not enough nights yet",
                f"{yoga['n']} evening yoga / mobility session(s) so far. Record a few more on the watch and the effect shows up here.")
        elif better and not worse:
            sure = any(r["verdict"] == "clear" for r in better)
            add("good", "Evening yoga helps" if sure else "Evening yoga looks helpful",
                f"{yoga['summary']} Keep it in the routine{'' if sure else '; a few more nights will settle it'}.")
        elif worse:
            add("info", "Evening yoga: no benefit seen", f"{yoga['summary']} Calm, slow sessions work best before bed.")
        else:
            add("info", "Evening yoga: neutral", yoga["summary"])
    days = [w for w in s["weekdays"] if w["n"] >= 3 and w["score"] is not None]
    if len(days) >= 5:
        worst = min(days, key=lambda w: w["score"])
        avg = _mean([w["score"] for w in days])
        if avg and worst["score"] <= avg - 5:
            add("info", f"{worst['night']} nights are your worst",
                f"Score {worst['score']:.0f} vs {avg:.0f} on average (bed {worst['bed']}, {worst['asleep_h']} h). "
                "Look at what that day usually holds.")
    trend = s.get("trend")
    if trend and trend.get("score_diff") is not None and abs(trend["score_diff"]) >= 3:
        add("info" if trend["score_diff"] < 0 else "good",
            f"Sleep score {'down' if trend['score_diff'] < 0 else 'up'} {abs(trend['score_diff']):.0f} points",
            "Last 30 days vs the 30 before.")
    order = {"improve": 0, "good": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["level"]])


# ---------------------------------------------------------------- report

def analysis(days: int = _WINDOW_DAYS, end: date | None = None, include_nights: bool = True) -> dict[str, Any]:
    end = end or date.today()
    ns = nights(end - timedelta(days=days - 1), end)
    profile = athlete.load()
    need = float(profile.get("sleep_need_h") or DEFAULT_NEED_H)
    if not ns:
        return {"nights": 0, "need_h": need, "empty": "No sleep data in the window."}
    since = lambda d: (end - timedelta(days=d - 1)).isoformat()  # noqa: E731
    last30 = [n for n in ns if n["date"] >= since(30)]
    prev30 = [n for n in ns if since(60) <= n["date"] < since(30)]
    last14 = [n for n in ns if n["date"] >= since(14)]
    train_days = [n for n in ns if n["train_min"] >= 15]
    train_tss = [n["tss"] for n in train_days if n["tss"]]
    hard_tss = max(60.0, statistics.quantiles(train_tss, n=4)[2]) if len(train_tss) >= 8 else 100.0

    rhythm = _rhythm(ns)
    recent_wake = _vals(last30, "wake_min")
    awake = _mean(_vals(last30, "awake_min")) or 0
    wake30 = statistics.median(recent_wake) if recent_wake else None
    target = clock(wake30 - need * 60 - awake) if wake30 is not None else None
    asleep14 = _vals(last14, "asleep_min")
    s30, sp = _period(last30, need * 60), _period(prev30, need * 60)
    scores = [n["score"] for n in ns if n["score"] is not None]
    report: dict[str, Any] = {
        "window": {"start": ns[0]["date"], "end": ns[-1]["date"], "nights": len(ns)},
        "need_h": need,
        "need_source": "athlete.json sleep_need_h" if profile.get("sleep_need_h") else f"default {DEFAULT_NEED_H:g} h (athletes in training: 8-9 h)",
        "last_night": ns[-1],
        "periods": {"7d": _period([n for n in ns if n["date"] >= since(7)], need * 60), "30d": s30,
                    "all": _period(ns, need * 60)},
        "trend": {"score_diff": rnd(s30["score"] - sp["score"], 1) if s30["score"] and sp["score"] else None,
                  "asleep_diff_min": rnd((s30["asleep_h"] - sp["asleep_h"]) * 60) if s30["asleep_h"] and sp["asleep_h"] else None},
        "debt_14d_h": rnd(max(0.0, sum(need * 60 - v for v in asleep14) / 60), 1) if asleep14 else None,
        "target_bedtime": target,
        "target_wake": clock(wake30),
        "score_bands": {"excellent (90+)": sum(v >= 90 for v in scores), "good (80-89)": sum(80 <= v < 90 for v in scores),
                        "fair (60-79)": sum(60 <= v < 80 for v in scores), "poor (<60)": sum(v < 60 for v in scores)},
        "rhythm": rhythm,
        "weekdays": _weekdays(ns),
        "load_bins": _load_bins(ns),
        "hard_tss": round(hard_tss),
        "influences": _influences(ns, train_days, hard_tss),
        "correlations": [c for c in (
            _correlation(ns, "tss", "score", "Day's training load (TSS) → sleep score"),
            _correlation(ns, "tss", "deep_min", "Day's training load → deep sleep"),
            _correlation(ns, "tss", "sleep_hr", "Day's training load → sleep HR"),
            _correlation(ns, "tss", "hrv", "Day's training load → overnight HRV"),
            _correlation(train_days, "last_end_h", "score", "Hours from last session to bed → sleep score"),
            _correlation(ns, "bed_min", "asleep_min", "Bedtime (later) → time asleep"),
            _correlation(ns, "bed_min", "score", "Bedtime (later) → sleep score"),
            _correlation(ns, "stress_prev", "score", "Day's stress → sleep score"),
            _correlation(ns, "asleep_min", "hrv", "Time asleep → overnight HRV"),
            _correlation(ns, "score", "rhr", "Sleep score → morning resting HR"),
        ) if c],
        "cycle": _cycle(ns),
    }
    report["findings"] = _findings(report)
    if include_nights:
        report["nights"] = ns
    return report

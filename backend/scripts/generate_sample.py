"""Generate obviously-fake sample data into sample_data/*.json.

60 days ending 2026-10-07 with deliberate edge cases:
- a weekly afternoon nap (must not count as a night)
- one night with overlapping stages, one unstaged night, one missing night
- a 4-day trip at UTC+01:00 (offset change) inside an otherwise +05:30 history
- some days without HRV or heart-rate data (must come back as missing, not 0)
Record ids start with "fake-" and the data origin is "com.example.fake".

Usage: python scripts/generate_sample.py
"""

import datetime as dt
import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "sample_data"
END = dt.date(2026, 10, 7)
DAYS = 60
ORIGIN = "com.example.fake"
TRIP = {END - dt.timedelta(days=d) for d in range(20, 24)}
MISSING_NIGHT = END - dt.timedelta(days=12)
UNSTAGED_NIGHT = END - dt.timedelta(days=5)
OVERLAP_NIGHT = END - dt.timedelta(days=3)
NO_HRV = {END - dt.timedelta(days=d) for d in (2, 9, 30)}
NO_HR = {END - dt.timedelta(days=d) for d in (15, 44)}


def tz(offset: str) -> dt.timezone:
    sign = -1 if offset[0] == "-" else 1
    return dt.timezone(sign * dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6])))


def iso(d: dt.datetime) -> str:
    return d.isoformat()


def offset_for(day: dt.date) -> str:
    return "+01:00" if day in TRIP else "+05:30"


def night(rng: random.Random, wake: dt.date, i: int) -> dict:
    off = offset_for(wake)
    zone = tz(off)
    bed = dt.datetime.combine(wake - dt.timedelta(days=1), dt.time(22, 30), zone) + dt.timedelta(minutes=rng.randint(0, 90))
    length = rng.randint(380, 500)
    end = bed + dt.timedelta(minutes=length)
    stages = []
    if wake != UNSTAGED_NIGHT:
        t = bed
        cycle = ["LIGHT", "DEEP", "LIGHT", "REM"]
        k = 0
        while t < end:
            name = cycle[k % 4]
            mins = {"LIGHT": rng.randint(25, 45), "DEEP": rng.randint(15, 30), "REM": rng.randint(15, 30)}[name]
            if k % 7 == 6:
                name, mins = "AWAKE", rng.randint(3, 12)
            nxt = min(t + dt.timedelta(minutes=mins), end)
            stages.append({"stage": name, "start": iso(t), "end": iso(nxt)})
            t, k = nxt, k + 1
        if wake == OVERLAP_NIGHT and len(stages) > 3:
            # Device glitch: a REM block overlapping the previous stage by 10 minutes.
            s = dt.datetime.fromisoformat(stages[2]["start"]) - dt.timedelta(minutes=10)
            stages[2]["start"] = iso(s)
    return {
        "source_record_id": f"fake-sleep-{i:04d}",
        "start_time": iso(bed),
        "end_time": iso(end),
        "start_offset": off,
        "end_offset": off,
        "stages": stages,
        "data_origin": ORIGIN,
        "source_last_modified": iso(end.astimezone(dt.UTC) + dt.timedelta(minutes=20)),
        "compute_seq": 1,
    }


def nap(rng: random.Random, day: dt.date, i: int) -> dict:
    off = offset_for(day)
    start = dt.datetime.combine(day, dt.time(14, 0), tz(off)) + dt.timedelta(minutes=rng.randint(0, 60))
    end = start + dt.timedelta(minutes=rng.randint(20, 45))
    return {
        "source_record_id": f"fake-nap-{i:04d}",
        "start_time": iso(start),
        "end_time": iso(end),
        "start_offset": off,
        "end_offset": off,
        "stages": [],
        "data_origin": ORIGIN,
        "source_last_modified": iso(end.astimezone(dt.UTC)),
        "compute_seq": 1,
    }


def daily(rng: random.Random, day: dt.date) -> dict:
    row = {"date": day.isoformat(), "steps": rng.randint(3500, 14000), "utc_offset": offset_for(day), "compute_seq": 1}
    if day not in NO_HR:
        resting = rng.randint(54, 62)
        row |= {
            "resting_hr": resting,
            "heart_rate_min": resting - rng.randint(3, 8),
            "heart_rate_max": rng.randint(115, 165),
            "heart_rate_avg": resting + rng.randint(8, 16),
        }
    if day not in NO_HRV:
        row["hrv_ms"] = round(rng.uniform(32, 62), 1)
    return row


def exercise(rng: random.Random, day: dt.date, i: int) -> dict:
    off = offset_for(day)
    etype = rng.choice(["running", "walking", "cycling", "strength_training", "yoga"])
    start = dt.datetime.combine(day, dt.time(18, 0), tz(off)) + dt.timedelta(minutes=rng.randint(0, 90))
    mins = rng.randint(25, 70)
    row = {
        "source_record_id": f"fake-exercise-{i:04d}",
        "start_time": iso(start),
        "end_time": iso(start + dt.timedelta(minutes=mins)),
        "start_offset": off,
        "end_offset": off,
        "exercise_type": etype,
        "calories": rng.randint(150, 650),
        "avg_hr": rng.randint(105, 155),
        "max_hr": rng.randint(150, 182),
        "data_origin": ORIGIN,
        "source_last_modified": iso((start + dt.timedelta(minutes=mins + 5)).astimezone(dt.UTC)),
        "compute_seq": 1,
    }
    if etype in ("running", "walking", "cycling"):
        row["distance_m"] = {"running": 140, "walking": 85, "cycling": 350}[etype] * mins
    if etype in ("running", "walking"):
        row["steps"] = mins * (165 if etype == "running" else 110)
    return row


def generate() -> dict[str, list]:
    rng = random.Random(20261007)
    sleep, days, exercises = [], [], []
    for n in range(DAYS):
        day = END - dt.timedelta(days=DAYS - 1 - n)
        if day != MISSING_NIGHT:
            sleep.append(night(rng, day, n))
        if day.weekday() == 5:
            sleep.append(nap(rng, day, n))
        days.append(daily(rng, day))
        if day.weekday() in (0, 2, 4, 6):
            exercises.append(exercise(rng, day, n))
    return {"sleep": sleep, "daily_metrics": days, "exercise": exercises}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, rows in generate().items():
        (OUT / f"{name}.json").write_text(json.dumps(rows, indent=1) + "\n")
        print(f"wrote {len(rows)} {name} rows")


if __name__ == "__main__":
    main()

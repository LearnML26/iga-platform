#!/usr/bin/env python3
"""Regenerate the JML demo fixtures with dates anchored to TODAY.

The fixtures' lifecycle behavior (REQ-COR-SRC-007/008) depends entirely on
dates relative to the day you run the demo: future joiners must actually be
in the future, and the pre-start/termination offsets must straddle the
sweep's activation window. Committed copies of the CSVs go stale the day
after they're generated — ALWAYS re-run this script before running the demo
and re-upload the fresh files. Everything except the date anchor is
deterministic (fixed RNG seed), so regenerating never changes who exists,
who transfers, or who leaves — only the dates move.

Round 1 (round1_baseline.csv, 50 rows):
  - 45 established employees with past start dates.
  - 5 future-dated joiners at +1, +2, +3, +7, +14 days. With the default
    PRE_START_ACTIVATION_DAYS=3, the first three are inside the pre-start
    window (the sweep should activate them on its first run) and the last
    two are not (they must STAY pending-start) — that split is the 007
    assertion.

Round 2 (round2_transfers_terminations.csv):
  - 2 established employees dropped entirely -> immediate terminations via
    2.3's absence detection.
  - 3 transfers (department/title changes) -> attribute updates.
  - 2 employees gain a FUTURE TerminationDate (+5, +10 days) -> scheduled
    terminations: they must stay active with terminationDate set until the
    effective date, then the sweep terminates them and dispatches
    disable-account tasks (the 008 assertion).
  - The 5 future joiners are unchanged from round 1.

Correlation keys are J-prefixed (J1001...) on purpose: correlationKey is
global per tenant, identity-service has no delete endpoint, and the dev
cluster already holds E1xxx/E2xxx identities left by smoke-test runs — an
E-prefixed fixture would silently collide with them and corrupt the demo's
add/update counts (this bit an earlier fixture revision, which shipped as
E1001-E1045).
"""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 42
KEY_PREFIX = "K"
FIRST_ID = 1002
ESTABLISHED = 45
FUTURE_JOINER_OFFSETS = [1, 2, 3, 7, 14]     # days from today; 3 inside / 2 outside the default 3-day window
DROPPED_INDEXES = [4, 11]                     # 0-based indexes into the established block -> immediate leavers
TRANSFER_INDEXES = [2, 16, 28]                # -> department/title change in round 2
SCHEDULED_TERM = {19: 5, 32: 10}              # index -> terminationDate offset in days

FIRST_NAMES = ["Alice", "Amir", "Bea", "Ben", "Carla", "Chen", "Dana", "Dev", "Elena", "Farid",
               "Grace", "Hui", "Kofi", "Marco", "Priya", "Quinn", "Sam", "Tara", "Uma", "Victor",
               "Wren", "Yara", "Zack"]
LAST_NAMES = ["Ali", "Brown", "Chen", "Cruz", "Diaz", "Haddad", "Kaur", "Meyer", "Nguyen", "Novak",
              "Osei", "Patel", "Reyes", "Rossi", "Silva", "Smith", "Tanaka", "Wong"]
DEPT_TITLES = {
    "Engineering": ["Software Engineer", "QA Engineer"],
    "Sales": ["Account Executive", "Sales Development Rep"],
    "Marketing": ["Marketing Manager", "Content Strategist"],
    "Finance": ["Accountant", "Financial Analyst"],
    "Operations": ["Operations Analyst", "Program Manager"],
    "HR": ["HR Business Partner", "Recruiter"],
    "Support": ["Customer Success Manager", "Support Engineer"],
    "Legal": ["Paralegal", "Compliance Analyst"],
}
HEADER = ["EmployeeID", "FirstName", "LastName", "DisplayName", "Department", "JobTitle",
          "StartDate", "TerminationDate"]


def build_rows(today: date) -> tuple[list[dict], list[dict]]:
    rng = random.Random(SEED)
    depts = list(DEPT_TITLES)

    round1 = []
    for i in range(ESTABLISHED + len(FUTURE_JOINER_OFFSETS)):
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        dept = rng.choice(depts)
        if i < ESTABLISHED:
            start = today - timedelta(days=rng.randint(30, 900))
        else:
            start = today + timedelta(days=FUTURE_JOINER_OFFSETS[i - ESTABLISHED])
        round1.append({
            "EmployeeID": f"{KEY_PREFIX}{FIRST_ID + i}",
            "FirstName": first, "LastName": last, "DisplayName": f"{first} {last}",
            "Department": dept, "JobTitle": rng.choice(DEPT_TITLES[dept]),
            "StartDate": start.isoformat(), "TerminationDate": "",
        })

    round2 = []
    for i, row in enumerate(round1):
        if i in DROPPED_INDEXES:
            continue  # immediate leaver: absent from the file entirely
        row2 = dict(row)
        if i in TRANSFER_INDEXES:
            new_dept = rng.choice([d for d in depts if d != row["Department"]])
            row2["Department"] = new_dept
            row2["JobTitle"] = rng.choice(DEPT_TITLES[new_dept])
        if i in SCHEDULED_TERM:
            row2["TerminationDate"] = (today + timedelta(days=SCHEDULED_TERM[i])).isoformat()
        round2.append(row2)
    return round1, round2


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    today = date.today()
    outdir = Path(__file__).parent
    round1, round2 = build_rows(today)
    write_csv(outdir / "round1_baseline.csv", round1)
    write_csv(outdir / "round2_transfers_terminations.csv", round2)
    dropped = [round1[i]["EmployeeID"] for i in DROPPED_INDEXES]
    transfers = [round1[i]["EmployeeID"] for i in TRANSFER_INDEXES]
    sched = {round1[i]["EmployeeID"]: (today + timedelta(days=off)).isoformat()
             for i, off in SCHEDULED_TERM.items()}
    joiners = [r["EmployeeID"] for r in round1[ESTABLISHED:]]
    print(f"anchored to {today.isoformat()}")
    print(f"round1: {len(round1)} rows ({ESTABLISHED} established + {len(joiners)} future joiners: {joiners})")
    print(f"round2: {len(round2)} rows — dropped {dropped}, transferred {transfers}, scheduled terminations {sched}")


if __name__ == "__main__":
    main()

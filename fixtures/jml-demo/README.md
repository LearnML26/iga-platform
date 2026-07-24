# JML end-to-end demo fixtures (Phase 2.5)

Synthetic HR extract exercising the full Joiner/Mover/Leaver pipeline:
flatfile-connector → identity-service → provisioning-service, including the
Phase 2.4 lifecycle behaviors (pending-start joiners, scheduled
terminations).

## Files

| File | What it is |
|---|---|
| `generate_fixtures.py` | Regenerates both CSVs with dates anchored to the day you run it. Deterministic apart from the date anchor (fixed seed) — same people, same transfers, same leavers every time; only dates move. |
| `round1_baseline.csv` | 50 rows: 45 established employees + 5 future-dated joiners (+1/+2/+3/+7/+14 days). The committed copy is illustrative only — see the warning below. |
| `round2_transfers_terminations.csv` | Round 1 minus 2 immediate leavers (dropped rows), with 3 transfers (department/title changes) and 2 employees given a **future** `TerminationDate` (+5/+10 days). |

## ⚠️ Regenerate before every demo run

```bash
python3 fixtures/jml-demo/generate_fixtures.py
```

The whole point of these fixtures is date-relative behavior. The committed
CSVs were anchored to the day they were last generated; run the demo a day
later with stale copies and the near-term future joiners are already in the
past (created `active` instead of `pending-start`) and the 007/008
assertions silently stop testing anything.

Correlation keys are J-prefixed because `correlationKey` is global per
tenant with no delete endpoint, and the dev cluster already holds
E1xxx/E2xxx identities from smoke-test runs — an E-prefixed fixture
collides with them (an earlier revision of these fixtures shipped as
E1001–E1045 and had exactly that problem). If you've already run this demo
against a cluster once, either bump `FIRST_ID`/`KEY_PREFIX` in the
generator or accept that round 1 reports updates instead of adds for
previously-seen keys.

## Prerequisites

1. **Phase 2.4 deployed** — the lifecycle sweep (`/lifecycle/sweep` +
   `lifecycle-sweep` CronJob) is what acts on pending-start and scheduled
   terminations. Without it, round 2's scheduled-termination rows are just
   ordinary attribute updates that never terminate anyone.
2. A source system instance with mappings for all eight columns
   (`EmployeeID→employeeId` as key, plus displayName/department/jobTitle/
   givenName/familyName/startDate/terminationDate), and
   `provisioningTargets` set (e.g. `["ad"]`) if you want to see
   disable-account dispatch — note dispatched tasks will retry and
   dead-letter downstream in dev (AD bind creds are not wired; known gap).

## Demo sequence

1. Regenerate fixtures (above); upload `round1_baseline.csv` (+ `.md5`
   sidecar) to `raw/` and ingest. Expect: `recordsAdded: 50`, with the 5
   future-dated joiners (`JOINER_KEYS` in the regenerated
   `fixture_keys.env`) created as `pending-start` and everyone else
   `active`.
2. Trigger the sweep (`POST /lifecycle/sweep`, or wait for the CronJob).
   Expect: the three joiners inside the 3-day pre-start window flip to
   `active`; the +7/+14 ones **stay** `pending-start`.
3. Upload + ingest `round2_transfers_terminations.csv`. Expect:
   `recordsUpdated: 5` (3 transfers + 2 rows that gained a
   terminationDate), `recordsTerminated: 2` (the dropped rows, immediate),
   and the 2 scheduled-termination identities still `active` with
   `terminationDate` set.
4. On/after the scheduled dates (or after temporarily editing the dates and
   re-ingesting), the sweep terminates them and dispatches disable-account
   tasks per `provisioningTargets`. Verify via
   `GET /identities/by-correlation-key/{key}` and provisioning-service
   logs.

The automated, CI-sized version of this flow (3-row fixture, per the spec)
lives in `scripts/verify.sh` — this directory is the thorough manual demo.

# Usability study: harness only, NO results

No usability study has been run. There were no participants, so there is no System Usability Scale (SUS) score, no task success rate and no task time to report, and none is claimed anywhere in this repository.

## What exists
- `docs/usability_tasks.json`: six tasks an operator should be able to do (acknowledge the most severe alarm, read a breaker state on the single-line diagram, find the worst N-1 outage, switch language and theme, issue a confirmed breaker command, write a handover note), each with a target time and a success criterion that an observer judges.
- `tools/usability.py`: records sessions (`add`), prints the SUS form in English or Hindi (`sus-form [--lang hi]`), scores SUS per Brooke (1996), reports SUS mean with a bootstrap 95 % interval, task success with Wilson intervals, median time of successful attempts, error counts, and SUS by language (`report`). `verify-tasks` is only a reachability check of the stack endpoints each task relies on (needs `GS_TOKEN`); it does not measure anything about a participant. `--selftest` verifies the arithmetic (known SUS values 100, 0, 50 and a hand-worked 85, Wilson 8/10 = 0.49 to 0.94, input validation); it uses made-up numbers and is not study data.

## Running a study
1. Start the stack (`tools/stack.sh up`) and reset it between participants (alarms and logbook state carry over otherwise).
2. Recruit dispatchers or control-room engineers; record role and years of experience. Offer English or Hindi. Counterbalance task order.
3. Observe, time and note errors for T1 to T6 (think-aloud is recommended), then give the 10 SUS statements (`tools/usability.py sus-form`).
4. Save one JSON per participant (format in the script's header) and run `tools/usability.py add`, then `tools/usability.py report`.

## Limits to keep in mind
- With fewer than about 12 participants the intervals are wide; say so when reporting.
- The Hindi SUS wording is our own translation and has not been validated by back-translation or a pilot.
- The task targets are our guesses, not benchmarks from a standard or a utility.
- Automated checks (`tools/ui_e2e.py`, axe-core) cover a fraction of accessibility rules and say nothing about whether operators can work with the interface.

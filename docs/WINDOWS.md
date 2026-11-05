# Running GridSovereign on Windows (no Ubuntu)

This runs the demo directly on Windows. It starts the same processes as `tools/stack.sh`, through `tools/stack.py`.
Only the default mode is offered: two SCADA core nodes (leader and standby). The optional Linux modes (TLS, DNP3, quorum,
mTLS, PostgreSQL, Redis, PMU) are not available here.

## What the Windows build has and has not been tested on
- The Windows zip is built and smoke-tested by GitHub Actions on a Windows runner (see the release notes for the result).
- The launcher was also run on Linux, where all 8 services started and the end-to-end suite `e2e_core` passed (78 of 78).
- Only one end-to-end suite is run on Windows. The other suites, the benchmarks and the 28-suite conformance run were done on Linux.
- Not tested on a physical Windows laptop by the author.

## One-time install (about 10 minutes)
1. **Java 21 or newer**: install Temurin 21 from https://adoptium.net (choose the .msi).
2. **Python 3.12 or 3.13**: from https://www.python.org/downloads/ . Tick **Add python.exe to PATH**.
3. **Download the zip**: GitHub repository, Releases, `windows-demo`, file `GridSovereign-windows.zip`. Unzip it to a short folder, for example `C:\GridSovereign`.
4. Double-click **setup.bat** once. It installs the Python packages into a private `.venv` folder.

## Every time
| Do this | What it does |
|---|---|
| double-click `start-demo.bat` | starts everything, prints the alice password and one-time code, opens http://127.0.0.1:8083 |
| double-click `code.bat` | prints a fresh one-time code (they change every 30 seconds). `code.bat bob` for another user |
| double-click `trip.bat` | the demo breaker trip. Then open the Alarms screen |
| double-click `status.bat` | shows which services are up |
| double-click `stop-demo.bat` | stops everything |

Logs are in `%TEMP%\gs` (paste that into File Explorer). Demo users: alice (operator), bob (viewer), carol (engineer), admin, north.

## If something goes wrong
- **"Java was not found"**: install Java, then close and reopen the window.
- **Windows Firewall asks to allow Java, Python or the gateway**: choose Private networks and Allow. Everything listens on 127.0.0.1 only.
- **Port already in use**: run `stop-demo.bat`, then start again.
- **The screen does not load**: run `status.bat`. Every line should say `ok`. Send a screenshot with the first line that says `DOWN`, and the matching file from `%TEMP%\gs`.

## Building it yourself instead of using the zip
You need Rust (with the MSVC build tools), Go 1.24, Java 21, Node 22 and Python. The steps are in `.github/workflows/windows.yml`.

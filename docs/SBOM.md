# Software bill of materials

Generated 2026-11-05T09:08:14Z by `tools/sbom.py` (machine-readable: `docs/SBOM.cdx.json`, CycloneDX 1.5). 278 components.

| Ecosystem | Required (ships or builds the product) | Optional (test/dev only) |
|---|---|---|
| cargo | 83 | 0 |
| npm | 8 | 144 |
| pypi | 29 | 5 |
| go | 1 | 0 |
| java | 1 | 0 |
| container | 7 | 0 |

## Licence policy result
* Fail (strong copyleft / non-commercial): **0**
* Needs review (weak copyleft or unknown): **5**

  * `npm:@fontsource/ibm-plex-mono 5.1.0` — OFL-1.1 → review
  * `npm:@fontsource/ibm-plex-sans 5.1.0` — OFL-1.1 → review
  * `npm:@fontsource/noto-sans-devanagari 5.1.0` — OFL-1.1 → review
  * `npm:axe-core 4.10.2` — MPL-2.0 → review
  * `pypi:certifi 2026.10.23` — MPL-2.0 → review

## Licences in use (libraries)

| Licence expression | Count |
|---|---|
| MIT | 170 |
| MIT OR Apache-2.0 | 43 |
| BSD-3-Clause | 14 |
| Apache-2.0 OR MIT | 10 |
| Apache-2.0 | 6 |
| ISC | 6 |
| MIT/Apache-2.0 | 3 |
| OFL-1.1 | 3 |
| MPL-2.0 | 2 |
| MIT-0 | 2 |
| Unlicense OR MIT | 1 |
| Apache-2.0 OR BSL-1.0 | 1 |
| MIT AND BSD-3-Clause | 1 |
| (MIT OR Apache-2.0) AND Unicode-3.0 | 1 |
| CC-BY-4.0 | 1 |
| Apache-2.0 OR BSD-3-Clause | 1 |
| BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 1 |
| Apache-2.0 OR BSD-2-Clause | 1 |
| BSD-2-Clause | 1 |
| PSF-2.0 | 1 |

## Limits
* Python versions are those installed in the build environment, not a hash-locked requirements file; `requirements.txt` uses lower bounds only.
* Container base images are named by tag, not by digest, and were never built here.
* Licence fields are as declared by the packages; no licence texts were audited and no copyright notices collected.
* The JDK is GPLv2 with the Classpath exception: fine for running our code, to be confirmed by counsel for redistribution inside an image.
* No transitive vulnerability scan beyond `npm audit` and `pip-audit` (run by `tools/sast.sh`); `cargo audit` could not be installed here.

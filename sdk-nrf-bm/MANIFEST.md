# nRF Connect SDK — Bare Metal (sdk-nrf-bm) — Pruned Reference Snapshot

A **documentation + API + examples** snapshot of `sdk-nrf-bm`, retrieved on
2026-06-15. This is a pruned subset of the upstream repository, not a full
clone — it keeps what is useful as an offline reference and drops everything
else. Same spirit as the `ncs-1.6.1-docs/` snapshot, adapted to this repo
(whose value is docs **plus** the API headers and example code).

## Provenance

| Field            | Value                                          |
|------------------|------------------------------------------------|
| Project          | nRF Connect SDK — Bare Metal                   |
| Repository       | https://github.com/nrfconnect/sdk-nrf-bm       |
| Pinned revision  | `main`                                         |
| Resolved commit  | `894fa18`                                       |
| SDK `VERSION`    | `2.0.99`                                        |
| Retrieved        | 2026-06-15                                      |

`.git` metadata was removed to leave a clean snapshot — provenance is the
commit hash above.

## What was retained (~791 files, ~9.4 MB)

Kept tree-wide, by type:
- **Narrative docs**: `*.rst` (190; 87 pages under `doc/nrf-bm/`), `*.md` (4),
  `*.txt` (57 — doc include snippets and SoftDevice license texts).
- **API reference**: `*.h` (229) — the Doxygen-commented headers in
  `include/bm/` **and** the SoftDevice API headers under
  `components/softdevice/**/include/`. These headers are the authoritative API
  docs.
- **Doxygen narrative**: `*.dox` (17) — BLE API message-sequence charts and
  doc landing pages (`mainpage.dox`).
- **Kconfig reference**: `Kconfig*` (150) — every configuration option.
- **Images**: `*.png`, `*.svg`, `*.jpg`, `*.jpeg`, `*.gif` (39).

Kept as whole example trees (source included, so they remain runnable):
- `samples/` (181 files — 30 samples)
- `applications/` (17 files — firmware_loader, installer, softdevice)

Plus top-level `LICENSE` and `VERSION`.

## What was excluded

- Source implementations outside the example trees: `*.c` in `lib/`, `subsys/`,
  `drivers/`, `boards/`, and test code in `tests/`.
- Binary blobs: SoftDevice firmware `*.hex` (~11 MB), `*.pdf` release notes /
  migration docs, `*.pem`.
- Build glue: `CMakeLists.txt` (outside `samples/`/`applications/`),
  `cmake/`, `sysbuild/` build files, `west.yml`, board `*_defconfig`/`*.dts*`,
  `*.overlay`, `*.ld`, `*.cmake`, `*.yaml`/`*.yml` CI.
- Dev/CI tooling: `.github/`, `Jenkinsfile`, `scripts/`, `doc/requirements.txt`,
  `CODEOWNERS`, `.checkpatch.conf`, `.clang-format`, `.editorconfig`,
  `.gitlint`, `.ruff.toml`, etc.

## Reproduce

```bash
git clone --filter=blob:none --no-checkout \
    https://github.com/nrfconnect/sdk-nrf-bm.git sdk-nrf-bm
git -C sdk-nrf-bm sparse-checkout init --no-cone
git -C sdk-nrf-bm sparse-checkout set --no-cone \
    'samples/' 'applications/' \
    '*.rst' '*.md' '*.txt' '*.h' '*.dox' 'Kconfig*' \
    '*.png' '*.svg' '*.jpg' '*.jpeg' '*.gif' '/LICENSE' '/VERSION'
git -C sdk-nrf-bm checkout main

# Drop build-glue collateral that the globs over-capture:
find sdk-nrf-bm -name CMakeLists.txt \
    -not -path '*/samples/*' -not -path '*/applications/*' -delete
rm -rf sdk-nrf-bm/scripts sdk-nrf-bm/doc/requirements.txt
find sdk-nrf-bm -depth -type d -empty -delete
```

Or just run `../refresh-sdk-nrf-bm.sh` from the repo root.

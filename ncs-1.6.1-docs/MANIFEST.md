# nRF Connect SDK (NCS) v1.6.1 — Documentation Sources

Markdown/RST documentation sources for the full NCS **v1.6.1** doc bundle, retrieved
on 2026-06-15. Revisions are pinned exactly as locked by `sdk-nrf`'s `west.yml`
at the `v1.6.1` tag.

## Doc sets

| Folder    | Project              | Repository                                    | Pinned revision     | Resolved commit |
|-----------|----------------------|-----------------------------------------------|---------------------|-----------------|
| `nrf/`    | nRF Connect SDK      | nrfconnect/sdk-nrf                            | `v1.6.1`            | `651d785a0fbef0f1cf38126d05713d6a18588d03` |
| `zephyr/` | Zephyr               | nrfconnect/sdk-zephyr                         | `v2.6.0-rc1-ncs1`   | `a62ea8fa297a12c2d17332218ff45c1e54c55a8e` |
| `nrfxlib/`| nrfxlib              | nrfconnect/sdk-nrfxlib                        | `v1.6.1`            | `c5efbc83787fa168123058e68e65c451e9c4345e` |
| `mcuboot/`| MCUboot              | nrfconnect/sdk-mcuboot                        | `v1.7.99-ncs2`      | `02afea39ebadbaa230887163507626cae7fc98ea` |
| `tfm/`    | Trusted Firmware-M   | nrfconnect/sdk-trusted-firmware-m            | `v1.3.99-ncs1`      | `cb1e6c2f070e68c950cd861d1bbd76b296be78b9` |

The main NCS narrative documentation lives under `nrf/doc/nrf/`. Build glue
(`conf.py`, `_extensions`, `_scripts`, `versions.json`) is under `nrf/doc/`.

## What was retrieved

For each repo, the following were extracted at the pinned revision:
- all `doc/` and `docs/` directories (full contents),
- all `*.rst`, `*.md`, `*.txt` files tree-wide (incl. scattered `README.rst`,
  `CHANGELOG.rst`, sample/application docs),
- referenced image assets (`*.png`, `*.svg`, `*.jpg`, `*.jpeg`, `*.gif`).

Source code (`.c`/`.h`/etc.) outside doc folders was intentionally excluded.
`.git` metadata was removed to leave a clean source snapshot — provenance is the
commit hashes above.

## Scope note

The other ~14 modules in the v1.6.1 west manifest (cjson, cmock, unity, mbedtls,
nanopb, openthread, matter/connectedhomeip, memfault, etc.) are code modules that
do **not** publish documentation into the NCS v1.6.1 rendered doc build, so they
were not retrieved. Ask if you also want Matter or OpenThread doc sources.

## Reproduce

```bash
git clone --depth 1 --branch <rev> --filter=blob:none --no-checkout <url> <dir>
git -C <dir> sparse-checkout init --no-cone
git -C <dir> sparse-checkout set --no-cone '/*' '!/*/' 'doc/' 'docs/' \
    '*.rst' '*.md' '*.txt' '*.png' '*.svg' '*.jpg' '*.jpeg' '*.gif'
git -C <dir> checkout
```

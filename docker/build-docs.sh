#!/usr/bin/env bash
# Build the resolved NCS v1.6.1 documentation inside the pinned toolchain image.
#
# Runs in the container started from ncs-1.6.1-docs.Dockerfile. Three mounts:
#   /work  (ro)  — this script + constraints.txt              (the docker/ dir)
#   /src   (rw)  — persisted west workspace (clone cache)     (host scratch)
#   /out   (rw)  — build output: _build/, pip-freeze.txt      (host scratch)
#
# Why clone fresh instead of mounting the local workspace: west checks out every
# project at the EXACT commit the v1.6.1 manifest pins — the same commits the
# committed `ncs-1.6.1-docs/` snapshot was extracted from — so the rendered HTML
# matches the snapshot line-for-line and citations are exact for every docset
# (zephyr included). It also removes any dependence on local workspace state.
set -euo pipefail

SRC=${SRC:-/src}
OUT=${OUT:-/out}
CONSTRAINTS=${CONSTRAINTS:-/work/constraints.txt}
NCS_REV=${NCS_REV:-v1.6.1}
SDK_NRF_URL=${SDK_NRF_URL:-https://github.com/nrfconnect/sdk-nrf}

echo "== toolchain =="
doxygen --version           # must print 1.8.13
cmake --version | head -1
ninja --version
west --version

# 1) Fresh, commit-exact workspace (idempotent: reused across iterations). Full
#    project set so Zephyr module enumeration is complete; blobless so the clone
#    is a fraction of a full ~3.7 GB checkout while keeping tag history intact.
echo "== west clone @ ${NCS_REV} (full, blobless) into ${SRC} =="
if [ ! -e "${SRC}/.west/config" ]; then
    west init -m "${SDK_NRF_URL}" --mr "${NCS_REV}" "${SRC}"
fi
cd "${SRC}"
west update --narrow --fetch-opt=--filter=blob:none
west zephyr-export   # register the Zephyr CMake package for find_package(Zephyr)

# 2) Python doc requirements, from the freshly cloned sources. Two-phase repro:
#    once constraints.txt is populated (from a prior good build's pip freeze),
#    install is byte-stable; until then it resolves the 2021 pins live.
PIP_C=()
if [ -s "${CONSTRAINTS}" ]; then
    echo "== installing doc requirements with constraints ${CONSTRAINTS} =="
    PIP_C=(-c "${CONSTRAINTS}")
else
    echo "== installing doc requirements (unpinned; will capture a freeze) =="
fi
pip install "${PIP_C[@]}" \
    -r zephyr/scripts/requirements-base.txt \
    -r zephyr/scripts/requirements-doc.txt \
    -r bootloader/mcuboot/scripts/requirements.txt \
    -r nrf/scripts/requirements-base.txt \
    -r nrf/scripts/requirements-doc.txt \
    -r nrf/scripts/requirements-build.txt

# 3) Configure + build all docsets. NOTE: no SPHINXOPTS_EXTRA=-W — warnings are
#    not errors, which is what lets the 2021 build complete.
export ZEPHYR_BASE="${SRC}/zephyr"
echo "== cmake configure =="
cmake -GNinja -S nrf/doc -B "${OUT}/_build"
echo "== ninja build-all (long pole: zephyr; ~30-90 min) =="
cmake --build "${OUT}/_build" "$@"   # extra args pass through, e.g. --target nrf-html-all

# 4) Capture provenance for reproducibility.
pip freeze > "${OUT}/pip-freeze.txt"
doxygen --version > "${OUT}/doxygen-version.txt"
echo "== done. HTML under ${OUT}/_build/html ; freeze at ${OUT}/pip-freeze.txt =="

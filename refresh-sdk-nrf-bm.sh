#!/usr/bin/env bash
# Refresh the local sdk-nrf-bm reference snapshot from upstream.
#
# This keeps a *pruned* snapshot (mirroring the treatment of ncs-1.6.1-docs):
# only documentation, API headers, Kconfig, images, and the full samples/ and
# applications/ example trees are retained. Library/driver/subsys source
# implementations, test code, build glue and binary blobs (SoftDevice .hex,
# .pdf, .pem) are dropped. The result is fetched directly via a blobless,
# sparse checkout so the bulk is never downloaded.
#
# Usage: bash refresh-sdk-nrf-bm.sh [<branch|tag|commit>]   (defaults to main)
set -euo pipefail
cd "$(dirname "$0")"

REPO_DIR="sdk-nrf-bm"
REPO_URL="https://github.com/nrfconnect/sdk-nrf-bm.git"
REF="${1:-main}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "Cloning $REPO_URL @ $REF (blobless, sparse) ..."
git clone --filter=blob:none --no-checkout "$REPO_URL" "$TMP_DIR/clone"
git -C "$TMP_DIR/clone" sparse-checkout init --no-cone
git -C "$TMP_DIR/clone" sparse-checkout set --no-cone \
    'samples/' 'applications/' \
    '*.rst' '*.md' '*.txt' '*.h' '*.dox' 'Kconfig*' \
    '*.png' '*.svg' '*.jpg' '*.jpeg' '*.gif' \
    '/LICENSE' '/VERSION'
git -C "$TMP_DIR/clone" checkout "$REF"

# The extension globs over-capture a few build/tooling files; drop them so the
# snapshot matches the documented keep set:
#   - CMakeLists.txt build glue (kept only inside the example trees)
#   - scripts/ dev tooling and the docs-build pip requirements
find "$TMP_DIR/clone" -name CMakeLists.txt \
    -not -path '*/samples/*' -not -path '*/applications/*' -delete
rm -rf "$TMP_DIR/clone/scripts" "$TMP_DIR/clone/doc/requirements.txt"
find "$TMP_DIR/clone" -depth -type d -empty -delete

COMMIT=$(git -C "$TMP_DIR/clone" rev-parse --short HEAD)
DATE=$(git -C "$TMP_DIR/clone" log -1 --format=%cd --date=short)

# Drop .git — provenance is the commit hash recorded below and in MANIFEST.md.
rm -rf "$TMP_DIR/clone/.git"
rm -rf "$REPO_DIR"
mv "$TMP_DIR/clone" "$REPO_DIR"

echo "Refreshed $REPO_DIR to $COMMIT ($DATE)."
echo "Tip: update the pinned commit in sdk-nrf-bm.md and $REPO_DIR/MANIFEST.md to match."

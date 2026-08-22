#!/usr/bin/env bash
# Prepare the locally downloaded source PDFs for import into Loreweaver.
#
#   ./prepare-sources.sh [--convert DIR] [--check-only]
#
# Default behavior: verify every PDF recorded in sources.json that exists on
# disk against its SHA-256. --convert DIR additionally runs `pdftotext` on each
# verified PDF into DIR/<id>.txt, ready for the Keeper module-management upload
# path. With --check-only, no conversion happens even if pdftotext is present.
#
# The PDFs themselves are intentionally NOT tracked in this repo (copyright);
# an operator fetches them locally and runs this script. Converted text is
# likewise not committed — inspect it and upload only material you are entitled
# to use.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCES="$LIB_DIR/sources.json"
CONVERT_DIR=""
CHECK_ONLY=0

usage() { sed -n '2,12p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --convert)
      CONVERT_DIR="$2"; shift 2 ;;
    --check-only)
      CHECK_ONLY=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! -f "$SOURCES" ]]; then
  echo "error: $SOURCES not found" >&2
  exit 1
fi

# Fail fast if a conversion was requested but the tool is unavailable.
if [[ -n "$CONVERT_DIR" && "$CHECK_ONLY" -eq 0 ]] && ! command -v pdftotext >/dev/null 2>&1; then
  echo "error: --convert requires pdftotext (poppler-utils); not found on PATH" >&2
  exit 1
fi

# Parse bundled entries: emit "local_path<TAB>sha256" lines.
mapfile -t entries < <(python3 - "$SOURCES" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for e in data.get("bundled", []):
    lp = e.get("local_path")
    sha = e.get("sha256")
    if lp and sha:
        print(f"{lp}\t{sha}")
PY
)

if [[ ${#entries[@]} -eq 0 ]]; then
  echo "no bundled entries with both local_path and sha256 in sources.json"
  exit 0
fi

status=0
missing=0
checked=0
converted=0

for line in "${entries[@]}"; do
  rel="${line%%$'\t'*}"
  expect="${line##*$'\t'}"
  file="$LIB_DIR/$rel"
  if [[ ! -f "$file" ]]; then
    echo "SKIP  missing locally: $rel (fetch it, then re-run)"
    missing=$((missing + 1))
    continue
  fi
  actual="$(sha256sum "$file" | awk '{print $1}')"
  if [[ "$actual" != "$expect" ]]; then
    echo "FAIL  SHA-256 mismatch: $rel"
    echo "      expected $expect"
    echo "      actual   $actual"
    status=1
    continue
  fi
  echo "OK    $rel"
  checked=$((checked + 1))

  if [[ -n "$CONVERT_DIR" && "$CHECK_ONLY" -eq 0 ]]; then
    id="$(basename "$rel" .pdf)"
    out="$CONVERT_DIR/$id.txt"
    mkdir -p "$CONVERT_DIR"
    pdftotext -layout "$file" "$out"
    echo "      -> converted to $out"
    converted=$((converted + 1))
  fi
done

echo "----"
echo "verified: $checked  missing: $missing  converted: $converted"
exit "$status"

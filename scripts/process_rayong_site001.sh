#!/usr/bin/env bash
set -Eeuo pipefail

# Process the archived Rayong site-001 LAS without modifying the raw source.
# Outputs: checksums + PDAL metadata + full COPC + decimated preview COPC.

SOURCE_NAME="Rayong_009s_2026_08_14_02_28_36.las"
DISCOVERED_ROOT=""

if [[ -z "${POINTCLOUD_ROOT:-}" ]]; then
  for drive_root in "$HOME"/Library/CloudStorage/GoogleDrive-*; do
    candidate="$drive_root/My Drive/PointCloud-Archive/Rayong/site-001/2026-08-17"
    if [[ -f "$candidate/raw/$SOURCE_NAME" ]]; then
      DISCOVERED_ROOT="$candidate"
      break
    fi
  done
fi

ROOT="${POINTCLOUD_ROOT:-$DISCOVERED_ROOT}"
[[ -n "$ROOT" ]] || {
  echo "ERROR: Rayong archive was not found under Google Drive for desktop."
  echo "Set POINTCLOUD_ROOT to the folder that contains raw/, metadata/ and derived/."
  exit 1
}

INPUT="${POINTCLOUD_INPUT:-$ROOT/raw/$SOURCE_NAME}"
META="$ROOT/metadata"
DERIVED="$ROOT/derived"
FULL_COPC="$DERIVED/${SOURCE_NAME%.las}.copc.laz"
PREVIEW_COPC="$DERIVED/${SOURCE_NAME%.las}.preview.copc.laz"
DRIVE_FILE_ID="1EYPxhCs_4fTpkaFj1a00nTOlEA1dfLYU"
TARGET_PREVIEW_POINTS="${TARGET_PREVIEW_POINTS:-2000000}"
FORCE="${FORCE:-0}"
CACHE="$HOME/Library/Caches/tree-pointclouds/rayong-site-001"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$META/logs/process-$RUN_ID.log"
TMP=""

mkdir -p "$META/logs" "$DERIVED" "$CACHE"
exec > >(tee -a "$LOG") 2>&1

cleanup() {
  code=$?
  [[ -n "$TMP" && -d "$TMP" ]] && rm -rf "$TMP"
  if [[ $code -ne 0 ]]; then
    echo "FAILED (exit $code). Log: $LOG"
  fi
}
trap cleanup EXIT

if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -dimsu -w $$ >/dev/null 2>&1 &
fi

echo "=== Rayong site-001 LAS processing ==="
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Input:   $INPUT"
echo "Log:     $LOG"

[[ -f "$INPUT" ]] || { echo "ERROR: source LAS not found"; exit 1; }
SOURCE_SIZE="$(stat -f '%z' "$INPUT")"
echo "Source size: $SOURCE_SIZE bytes"
if [[ "$SOURCE_SIZE" != "2777224625" ]]; then
  echo "WARNING: Drive reported 2777224625 bytes; local size differs."
fi

FREE_KB="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
FREE_GIB="$((FREE_KB / 1024 / 1024))"
echo "Free local disk: about ${FREE_GIB} GiB"
[[ "$FREE_GIB" -ge 12 ]] || { echo "ERROR: at least 12 GiB free space is required"; exit 1; }

if ! command -v pdal >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 || {
    echo "ERROR: PDAL is missing and Homebrew is unavailable"
    exit 1
  }
  echo "Installing PDAL with Homebrew…"
  brew install pdal
fi

echo "PDAL: $(pdal --version 2>&1 | head -n 1)"

echo "Computing source SHA-256…"
RAW_SHA="$(shasum -a 256 "$INPUT" | awk '{print $1}')"
printf '%s  %s\n' "$RAW_SHA" "$(basename "$INPUT")" > "$META/$(basename "$INPUT").sha256"

echo "Reading PDAL summary, metadata and schema…"
pdal info "$INPUT" --summary  > "$META/pdal-summary.json.tmp"
pdal info "$INPUT" --metadata > "$META/pdal-metadata.json.tmp"
pdal info "$INPUT" --schema   > "$META/pdal-schema.json.tmp"
mv "$META/pdal-summary.json.tmp"  "$META/pdal-summary.json"
mv "$META/pdal-metadata.json.tmp" "$META/pdal-metadata.json"
mv "$META/pdal-schema.json.tmp"   "$META/pdal-schema.json"

POINT_COUNT="$(python3 - "$META/pdal-summary.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
value = data.get('summary', {}).get('num_points')
if not isinstance(value, int) or value < 1:
    raise SystemExit('Could not read summary.num_points')
print(value)
PY
)"
STEP="$(( (POINT_COUNT + TARGET_PREVIEW_POINTS - 1) / TARGET_PREVIEW_POINTS ))"
[[ "$STEP" -ge 1 ]] || STEP=1
echo "Point count: $POINT_COUNT"
echo "Preview decimation step: $STEP (~$TARGET_PREVIEW_POINTS points maximum)"

TMP="$(mktemp -d "$CACHE/run.XXXXXX")"

if [[ ! -f "$FULL_COPC" || "$FORCE" == "1" ]]; then
  echo "Creating full-resolution COPC…"
  pdal translate "$INPUT" "$TMP/$(basename "$FULL_COPC")" \
    --writer writers.copc \
    --writers.copc.forward=all \
    --writers.copc.extra_dims=all \
    --metadata="$META/full-copc-translate.json.tmp"
  pdal info "$TMP/$(basename "$FULL_COPC")" --summary > "$META/full-copc-summary.json.tmp"
  FULL_SHA="$(shasum -a 256 "$TMP/$(basename "$FULL_COPC")" | awk '{print $1}')"
  printf '%s  %s\n' "$FULL_SHA" "$(basename "$FULL_COPC")" > "$META/$(basename "$FULL_COPC").sha256.tmp"
  mv "$TMP/$(basename "$FULL_COPC")" "$FULL_COPC"
  mv "$META/full-copc-translate.json.tmp" "$META/full-copc-translate.json"
  mv "$META/full-copc-summary.json.tmp" "$META/full-copc-summary.json"
  mv "$META/$(basename "$FULL_COPC").sha256.tmp" "$META/$(basename "$FULL_COPC").sha256"
else
  echo "Full COPC already exists; skipping (set FORCE=1 to rebuild)."
fi

if [[ ! -f "$PREVIEW_COPC" || "$FORCE" == "1" ]]; then
  echo "Creating decimated preview COPC…"
  pdal translate "$INPUT" "$TMP/$(basename "$PREVIEW_COPC")" decimation \
    --writer writers.copc \
    --filters.decimation.step="$STEP" \
    --writers.copc.forward=all \
    --writers.copc.extra_dims=all \
    --metadata="$META/preview-copc-translate.json.tmp"
  pdal info "$TMP/$(basename "$PREVIEW_COPC")" --summary > "$META/preview-copc-summary.json.tmp"
  PREVIEW_SHA="$(shasum -a 256 "$TMP/$(basename "$PREVIEW_COPC")" | awk '{print $1}')"
  printf '%s  %s\n' "$PREVIEW_SHA" "$(basename "$PREVIEW_COPC")" > "$META/$(basename "$PREVIEW_COPC").sha256.tmp"
  mv "$TMP/$(basename "$PREVIEW_COPC")" "$PREVIEW_COPC"
  mv "$META/preview-copc-translate.json.tmp" "$META/preview-copc-translate.json"
  mv "$META/preview-copc-summary.json.tmp" "$META/preview-copc-summary.json"
  mv "$META/$(basename "$PREVIEW_COPC").sha256.tmp" "$META/$(basename "$PREVIEW_COPC").sha256"
else
  echo "Preview COPC already exists; skipping (set FORCE=1 to rebuild)."
fi

python3 - "$META/pdal-summary.json" "$META/source-info.json" <<PY
import json, pathlib, sys
summary_path, output_path = map(pathlib.Path, sys.argv[1:])
summary = json.loads(summary_path.read_text(encoding='utf-8'))
payload = {
    'datasetId': 'rayong-site-001-2026-08-17',
    'status': 'PROCESSED',
    'originalFilename': pathlib.Path(r'''$INPUT''').name,
    'googleDriveFileId': '$DRIVE_FILE_ID',
    'sourceFileSizeBytes': $SOURCE_SIZE,
    'sourceSha256': '$RAW_SHA',
    'pointCount': $POINT_COUNT,
    'summary': summary.get('summary', {}),
    'rawAccess': 'RESTRICTED',
    'rawFileMustNotBeModified': True,
    'fullCopc': pathlib.Path(r'''$FULL_COPC''').name,
    'previewCopc': pathlib.Path(r'''$PREVIEW_COPC''').name,
    'previewDecimationStep': $STEP,
}
output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
PY

echo "=== Completed ==="
echo "Metadata: $META"
echo "Full COPC: $FULL_COPC"
echo "Preview:   $PREVIEW_COPC"
echo "Finished:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
#!/bin/bash

CAMERA_URL="rtsp://127.0.0.1:8554/lettuce_cam"
BASE_DIR="/media/pi/hdd"
LOCKFILE="/tmp/record_24h.lock"
SEGMENT_TIME=1800

log() {
  local LOG_DIR
  LOG_DIR="$BASE_DIR/$(date +%F)"
  mkdir -p "$LOG_DIR"
  echo "$(date '+%F %T') $1" | tee -a "$LOG_DIR/system.log"
}

if [ -f "$LOCKFILE" ]; then
  log "Recording already running. Exiting."
  exit 1
fi

trap 'rm -f "$LOCKFILE"' EXIT
touch "$LOCKFILE"

log "Segmented recording started from go2rtc."

while true; do
  if ! mountpoint -q "$BASE_DIR"; then
    echo "$(date '+%F %T') HDD not mounted at $BASE_DIR. Waiting 5 seconds..."
    sleep 5
    continue
  fi

  DATE_DIR="$BASE_DIR/$(date +%F)"
  mkdir -p "$DATE_DIR"

  TIMESTAMP=$(date '+%F_%H-%M')
  OUTPUT_FILE="$DATE_DIR/${TIMESTAMP}.mkv"
  LOG_FILE="$DATE_DIR/system.log"

  log "Recording segment: $OUTPUT_FILE"

  ffmpeg -nostdin \
    -rtsp_transport tcp \
    -fflags +genpts \
    -use_wallclock_as_timestamps 1 \
    -i "$CAMERA_URL" \
    -map 0:v:0 \
    -c:v copy \
    -an \
    -t "$SEGMENT_TIME" \
    -f matroska \
    "$OUTPUT_FILE" >> "$LOG_FILE" 2>&1

  EXIT_CODE=$?
  log "ffmpeg exited with code $EXIT_CODE"

  sleep 2
done

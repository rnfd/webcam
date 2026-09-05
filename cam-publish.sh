#!/usr/bin/env bash
# Publish one camera to MediaMTX, or a captioned placeholder in its place.
#
#   cam-publish.sh <camera-ip> <mediamtx-path> <main|sub>
#
# Three states, and the switch between them is checked every second — including
# while a stream is running, because an ffmpeg pulling a healthy camera blocks
# forever and would otherwise ignore the switch until the camera dropped:
#
#   $CAMS_STATE/disabled exists -> "CAMERAS OFF": no RTSP session is opened to
#                                  the camera at all (Telegram /disable)
#   camera unreachable          -> "CAMERA OFFLINE"
#   otherwise                   -> the camera, video stream-copied at its own
#                                  quality; only the audio becomes Opus, which
#                                  is what WebRTC needs
#
# The placeholders keep the path published, so the page shows a captioned panel
# instead of a dead player and the compositor downstream never breaks.
#
# Env: FF (ffmpeg), FONT (ttf for the captions; plain black without it),
#      CAMS_STATE, CAM_USER, CAM_PASS, RTSP_PORT.
set -u
ip="$1"; path="$2"; variant="$3"

FF="${FF:-ffmpeg}"
FONT="${FONT:-}"
STATE="${CAMS_STATE:-./state}"
off="$STATE/disabled"
user="${CAM_USER:-admin}"; pass="${CAM_PASS:-}"
port="${RTSP_PORT:-8554}"
out="rtsp://localhost:$port/$path"
cam="rtsp://$user:$pass@$ip:554/h264Preview_01_$variant"

reachable() { timeout 3 bash -c "exec 3<>/dev/tcp/$ip/554" 2>/dev/null; }
caption() {
  if [ -n "$FONT" ]; then
    echo "drawtext=text=$1:fontfile=$FONT:fontcolor=0x9ca3af:fontsize=40:x=(w-text_w)/2:y=(h-text_h)/2"
  else
    echo "null"
  fi
}

# Run a publisher in the background and stop it the moment the switch flips the
# other way, so /disable and /enable both take effect within a second.
watch_run() {
  local want="$1"; shift
  "$@" &
  local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$want" = off ] && [ ! -e "$off" ]; then break; fi
    if [ "$want" = on  ] && [   -e "$off" ]; then break; fi
    sleep 1
  done
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

placeholder() {   # <caption> <seconds> <want>
  watch_run "$3" "$FF" -hide_banner -loglevel warning -nostdin -re \
    -f lavfi -i "color=c=black:s=896x512:r=10" -f lavfi -i "anullsrc=r=48000:cl=stereo" \
    -vf "$(caption "$1")" -t "$2" \
    -c:v libx264 -preset ultrafast -tune stillimage -pix_fmt yuv420p -g 20 \
    -c:a libopus -b:a 32k -shortest -f rtsp -rtsp_transport tcp "$out"
}

while true; do
  if [ -e "$off" ]; then
    # one long still, held until /enable — no reconnect flicker while off
    placeholder "CAMERAS OFF" 3600 off
  elif reachable; then
    watch_run on "$FF" -hide_banner -loglevel warning -nostdin -rtsp_transport tcp \
      -i "$cam" -map 0 -c:v copy -c:a libopus -b:a 64k -ac 2 \
      -f rtsp -rtsp_transport tcp "$out"
  else
    placeholder "CAMERA OFFLINE" 10 on
  fi
  sleep 1
done

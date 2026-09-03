#!/usr/bin/env bash
# Start the two-camera WebRTC viewer (MediaMTX + the web app).
#
#   ./run.sh                 # page on :8088, WebRTC on :8889
#   PORT=9090 ./run.sh
#
# Ports to open in the firewall for remote/VPN access:
#   8088/tcp  (web page)   8889/tcp  (WebRTC signaling)   8189/tcp+udp (WebRTC media)

set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8088}"

command -v python3 >/dev/null || { echo "error: python3 not found" >&2; exit 1; }

# locate mediamtx (nix store or PATH)
MMX="${MEDIAMTX:-$(command -v mediamtx || true)}"
if [ -z "$MMX" ]; then
  MMX=$(ls -d /nix/store/*mediamtx*/bin/mediamtx 2>/dev/null | head -1 || true)
fi
if [ -z "$MMX" ]; then
  echo "error: mediamtx not found. Install with: nix profile install nixpkgs#mediamtx" >&2
  echo "       or set MEDIAMTX=/path/to/mediamtx" >&2
  exit 1
fi

echo "Starting MediaMTX ($MMX) ..."
"$MMX" mediamtx.yml &
MMX_PID=$!

# stop MediaMTX when this script exits
cleanup(){ kill "$MMX_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "Starting web app -> http://localhost:$PORT   (Ctrl+C to stop)"
PORT="$PORT" python3 app.py

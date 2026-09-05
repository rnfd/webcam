# webcam

Two Reolink cameras stacked into one low-latency WebRTC stream (video stacked,
audio mixed), with server-side recording, an 8-hour cap, and delivery of each
clip to Telegram (as a link) and Google Drive. Runs as NixOS services.

## Components

| File | Purpose |
|------|---------|
| `app.py` | Web page (WebRTC viewer + record button), recorder, Telegram control, Drive/Telegram delivery |
| `cams.nix` | NixOS module: MediaMTX + app services, nginx reverse proxy, HTTPS (ACME/Cloudflare DNS), firewall |
| `mediamtx.yml` | Standalone MediaMTX config for local dev (`run.sh`) |
| `run.sh` | Launch MediaMTX + the app locally without NixOS |
| `Caddyfile` | Alternative reverse-proxy config (nginx is used in the module) |
| `snap.py` | One-off JPEG snapshot from a camera |

## How it works

- **Compositing:** one ffmpeg pulls both cameras over RTSP, stacks the video
  (`vstack`) and mixes the audio (`amix`), and publishes a single `composite`
  stream to MediaMTX. The panes are time-aligned: each input is stamped with
  its arrival wall clock (`-use_wallclock_as_timestamps 1`, `-copyts`), so the
  stack pairs frames that arrived at the same moment instead of the Nth frame
  of each RTSP session, which could be seconds apart.
- **Viewing:** MediaMTX serves that stream over WebRTC (sub-second). The page is
  a WHEP client; nginx fronts it at `https://<host>` so there is no custom port.
- **Recording:** the app stream-copies the composite to an `.mkv` (no re-encode,
  no extra camera load). Stop finalizes the file, then in the background it
  remuxes to MP4, uploads to Google Drive (rclone), and posts a link to Telegram
  with an upload progress bar. Recordings auto-stop after 8 hours.
- **Control:** start/stop from the web button or the Telegram bot
  (`/record`, `/stop`, `/status`).

## Configuration

Edit the `let` block in `cams.nix` (camera IPs, credentials, stream, host).
Secrets live outside the repo, in root-only files:

- `/etc/cams/telegram.env` — `TELEGRAM_BOT_TOKEN=` and `TELEGRAM_CHAT_ID=`
- `/etc/cams/rclone.conf` — rclone Google Drive remote named `gdrive`
- `/etc/cams/acme-cloudflare.env` — `CF_DNS_API_TOKEN=` for the HTTPS cert

## Install (NixOS)

Copy `cams.nix` and `app.py` to `/etc/nixos/cams/`, add
`./cams/cams.nix` to your `imports`, then `nixos-rebuild switch`.

## Local dev (no NixOS)

`./run.sh` starts MediaMTX (`mediamtx.yml`) and the app on port 8088.

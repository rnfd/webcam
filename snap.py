#!/usr/bin/env python3
"""Grab a still image from a Reolink camera on the LAN.

Uses the camera's HTTP snapshot CGI, so it needs only the Python standard
library (no ffmpeg / OpenCV). Returns a JPEG.

Usage:
    python3 snap.py --host 192.168.1.146 --user admin --password 'secret'
    python3 snap.py --host 192.168.1.185 -u admin -p 'secret' -o cam2.jpg

Credentials can also come from env vars CAM_USER / CAM_PASS.
"""
import argparse, os, ssl, sys, time, urllib.parse, urllib.request


def snapshot(host, user, password, channel=0, https=False, timeout=10):
    scheme = "https" if https else "http"
    rs = str(int(time.time() * 1000))  # cache-buster the app uses
    qs = urllib.parse.urlencode({
        "cmd": "Snap", "channel": channel, "rs": rs,
        "user": user, "password": password,
    })
    url = f"{scheme}://{host}/cgi-bin/api.cgi?{qs}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # cameras ship self-signed certs
    req = urllib.request.Request(url, headers={"User-Agent": "snap.py"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        data = r.read()
        ctype = r.headers.get("Content-Type", "")
    if not data.startswith(b"\xff\xd8"):  # JPEG magic bytes
        raise RuntimeError(
            f"did not receive a JPEG (Content-Type={ctype!r}). "
            f"First bytes: {data[:120]!r}"
        )
    return data


def main():
    ap = argparse.ArgumentParser(description="Grab a JPEG from a Reolink camera")
    ap.add_argument("--host", required=True, help="camera IP, e.g. 192.168.1.146")
    ap.add_argument("-u", "--user", default=os.environ.get("CAM_USER", "admin"))
    ap.add_argument("-p", "--password", default=os.environ.get("CAM_PASS"))
    ap.add_argument("-c", "--channel", type=int, default=0)
    ap.add_argument("--https", action="store_true", help="use HTTPS (port 443)")
    ap.add_argument("-o", "--out", help="output file (default: snap_<host>.jpg)")
    args = ap.parse_args()

    if not args.password:
        sys.exit("no password: pass --password or set CAM_PASS")

    out = args.out or f"snap_{args.host.replace('.', '_')}.jpg"
    try:
        img = snapshot(args.host, args.user, args.password,
                       channel=args.channel, https=args.https)
    except Exception as e:
        sys.exit(f"snapshot failed: {e}")
    with open(out, "wb") as f:
        f.write(img)
    print(f"saved {len(img)} bytes -> {out}")


if __name__ == "__main__":
    main()

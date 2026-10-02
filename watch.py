#!/usr/bin/env python3
"""AICenter site watch (runs on GitHub Actions, outside the site's own server).

Checks the pages that matter: status 200, an expected marker in the body, response time, and the TLS certificate's
remaining days. A failed check is retried once a minute later (a single blip is not an outage). If anything still
fails, the job fails and GitHub emails the repository owner. No secrets needed.
"""
import socket, ssl, sys, time, urllib.request
from datetime import datetime, timezone

SITE = "https://aicenter.co.il"
CHECKS = [  # path, text that must appear in the response
    ("/", "AICenter"),
    ("/tools", "AI"),
    ("/tool/runway", "Runway"),
    ("/article/ai-regulation-israel-2026", "AI"),
    ("/sitemap.xml", "<"),
    ("/robots.txt", "Sitemap"),
    ("/api/badge.php?t=runway", "<svg"),
]
SLOW = 8.0  # seconds
CERT_MIN_DAYS = 14
HEADERS = {  # the host's firewall turns away requests that don't look like a browser
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36 aicenter-watch",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/svg+xml,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}


def check(path, marker):
    t0 = time.time()
    try:
        req = urllib.request.Request(SITE + path, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read(400000).decode("utf-8", "ignore")
            code = r.status
    except urllib.error.HTTPError as e:
        return f"{path}: HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return f"{path}: {type(e).__name__} {e}"
    dt = time.time() - t0
    if code != 200:
        return f"{path}: HTTP {code}"
    if marker not in body:
        return f"{path}: marker '{marker}' missing"
    if dt > SLOW:
        return f"{path}: slow {dt:.1f}s"
    print(f"ok  {path}  {dt:.2f}s")
    return None


def cert_days():
    host = SITE.split("//")[1]
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=15) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
        exp = datetime.strptime(ss.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (exp - datetime.now(timezone.utc)).days


def main():
    problems = []
    for path, marker in CHECKS:
        err = check(path, marker)
        if err:
            print("retry", err, flush=True)
            time.sleep(60)
            err = check(path, marker)
        if err:
            problems.append(err)
    try:
        days = cert_days()
        print(f"certificate: {days} days left")
        if days < CERT_MIN_DAYS:
            problems.append(f"certificate expires in {days} days")
    except Exception as e:  # noqa: BLE001
        problems.append(f"certificate check failed: {e}")
    if problems:
        print("\nPROBLEMS:\n- " + "\n- ".join(problems))
        sys.exit(1)
    print("all good")


if __name__ == "__main__":
    main()

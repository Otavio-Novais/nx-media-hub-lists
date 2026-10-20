#!/usr/bin/env python3
"""Builds lista_limpa.m3u: merges the public iptv-org lists, removes duplicates and hides dead channels.

The check runs from GitHub's servers (USA), not from the Switch's network, so it only hides what is
clearly dead: DNS/connection errors, timeouts, 404/410 and 5xx, after FAILS_TO_HIDE runs in a row.
403/401/451 (usually geo-blocking or datacenter IPs being refused) keep the channel.

Standard library only: python3 check.py
"""

import concurrent.futures
import http.client
import json
import sys
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

SOURCES = [
    "https://iptv-org.github.io/iptv/languages/por.m3u",
    "https://iptv-org.github.io/iptv/categories/sports.m3u",
    "https://iptv-org.github.io/iptv/categories/news.m3u",
]
OUTPUT = "lista_limpa.m3u"
STATUS = "status.json"

FAILS_TO_HIDE = 2       # consecutive failed runs (each run already retries its failures once)
TIMEOUT = 15            # seconds per request
WORKERS = 64
DEFAULT_USER_AGENT = "libmpv"  # what the app's player sends when the list sets none
MAX_PLAYLIST_BYTES = 1 << 20
MAX_FAILED_SHARE = 0.6  # above this the run is discarded (the runner's network, not the channels)

# The Switch does not verify certificates (no CA store): a bad certificate must not hide a channel
INSECURE = ssl.create_default_context()
INSECURE.check_hostname = False
INSECURE.verify_mode = ssl.CERT_NONE
OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=INSECURE))


class Dead(Exception):
    """The channel is not reachable: counts as a failure."""


# ---------- M3U ----------

class Entry:
    def __init__(self, extinf, options, url):
        self.extinf = extinf      # "#EXTINF:-1 tvg-id=... group-title="A;B",Name"
        self.options = options    # "#EXTVLCOPT:..." / "#KODIPROP:..." lines, kept as is
        self.url = url

    @property
    def name(self):
        return self.extinf.split(",", 1)[1].strip() if "," in self.extinf else self.url

    def groups(self):
        m = re.search(r'group-title="([^"]*)"', self.extinf)
        return [g.strip() for g in m.group(1).split(";") if g.strip()] if m else []

    def set_groups(self, groups):
        value = ";".join(groups)
        if 'group-title="' in self.extinf:
            self.extinf = re.sub(r'group-title="[^"]*"', lambda _: f'group-title="{value}"', self.extinf, count=1)
        elif value:
            self.extinf = self.extinf.replace(",", f' group-title="{value}",', 1)

    def headers(self):
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        for line in self.options:
            key, _, value = line.partition(":")[2].partition("=")
            key = key.strip().lower()
            if key in ("http-user-agent", "user-agent"):
                headers["User-Agent"] = value.strip()
            elif key in ("http-referrer", "http-referer", "referrer", "referer"):
                headers["Referer"] = value.strip()
        stream_url, _, params = self.url.partition("|")  # "url|User-Agent=x&Referer=y"
        for pair in filter(None, params.split("&")):
            key, _, value = pair.partition("=")
            headers[key.strip()] = value.strip()
        return stream_url, headers

    def lines(self):
        return [self.extinf, *self.options, self.url]


def parse_m3u(text):
    header, entries, extinf, options = None, [], None, []
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header = header or line
        elif line.startswith("#EXTINF:"):
            extinf, options = line, []
        elif line.startswith(("#EXTVLCOPT:", "#KODIPROP:", "#EXTGRP:")):
            options.append(line)
        elif not line.startswith("#") and "://" in line:
            entries.append(Entry(extinf or f"#EXTINF:-1,{line}", options, line))
            extinf, options = None, []
    return header, entries


def merge(sources):
    """Same URL in several lists: one entry with the categories of all of them (like the app's dedupe)."""
    header, by_url = None, {}
    for text in sources:
        h, entries = parse_m3u(text)
        header = header or h
        for entry in entries:
            kept = by_url.setdefault(entry.url, entry)
            if kept is not entry:
                groups = kept.groups()
                kept.set_groups(groups + [g for g in entry.groups() if g not in groups])
    return header or "#EXTM3U", list(by_url.values())


# ---------- check ----------

def fetch(url, headers, limit, byte_range=False):
    """GET returning (status, body); raises Dead for errors that mean the channel is gone."""
    request = urllib.request.Request(url, headers=dict(headers, **({"Range": "bytes=0-4095"} if byte_range else {})))
    try:
        with OPENER.open(request, timeout=TIMEOUT) as response:
            return response.status, response.read(limit)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410) or e.code >= 500:
            raise Dead(f"HTTP {e.code}")
        return e.code, b""  # 401/403/451/429...: blocked here, may play on the Switch
    except http.client.IncompleteRead as e:
        return 200, e.partial  # live stream cut while reading: it was sending data
    except http.client.HTTPException as e:
        raise Dead(type(e).__name__)
    except (socket.timeout, TimeoutError):
        raise Dead("timeout")
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.gaierror):
            raise Dead("DNS")
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise Dead("timeout")
        if isinstance(reason, ConnectionRefusedError):
            raise Dead("connection refused")
        raise Dead(type(reason).__name__ if not isinstance(reason, str) else reason)
    except (ConnectionError, ssl.SSLError, OSError) as e:
        raise Dead(type(e).__name__)


def first_uri(playlist, base):
    """Next URI to follow in an HLS playlist: first variant of a master playlist, else the last segment."""
    lines = [l.strip() for l in playlist.splitlines()]
    uris = [l for l in lines if l and not l.startswith("#")]
    if not uris:
        return None
    is_master = any(l.startswith("#EXT-X-STREAM-INF") for l in lines)
    return urllib.parse.urljoin(base, uris[0] if is_master else uris[-1]), is_master


def check(entry):
    """None when the channel looks alive (or blocked only here), else the failure reason."""
    url, headers = entry.headers()
    try:
        status, body = fetch(url, headers, MAX_PLAYLIST_BYTES)
        # Follow the HLS chain (master -> variant -> segment) so a playlist with dead video is caught
        for _ in range(3):
            if status >= 300 or not body.lstrip().startswith(b"#EXTM3U"):
                return None  # blocked here (403...) or a direct stream that answered: keep it
            nxt = first_uri(body.decode("utf-8", "replace"), url)
            if not nxt:
                return "empty playlist"
            url, is_master = nxt
            status, body = fetch(url, headers, MAX_PLAYLIST_BYTES if is_master else 4096, byte_range=not is_master)
            if not is_master:
                return None
        return None
    except Dead as e:
        return str(e)
    except Exception as e:  # unexpected (bad header value, odd URL...): not proof the channel is dead
        print(f"  check error, kept: {entry.name}: {type(e).__name__}: {e}")
        return None


def check_all(entries):
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        return dict(zip((e.url for e in entries), pool.map(check, entries)))


# ---------- main ----------

def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with OPENER.open(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def main():
    started = time.time()
    header, entries = merge(download(u) for u in SOURCES)
    print(f"{len(entries)} channels after merging {len(SOURCES)} lists")

    results = check_all(entries)
    failed = [e for e in entries if results[e.url]]
    print(f"first pass: {len(failed)} failed, retrying them")
    time.sleep(30)
    results.update(check_all(failed))  # transient failures do not count

    failed_count = sum(1 for r in results.values() if r)
    if failed_count > len(entries) * MAX_FAILED_SHARE:
        # Most of the list failing means the runner's network is the problem: keep the previous files
        sys.exit(f"{failed_count} of {len(entries)} failed: looks like a network problem, files not updated")

    try:
        with open(STATUS) as f:
            previous = json.load(f).get("failing", {})
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}

    # Only failing channels are stored: the file (and the commit) changes only when that set changes
    failing = {}
    for e in entries:
        if results[e.url]:
            failing[e.url] = {"name": e.name, "fails": previous.get(e.url, {}).get("fails", 0) + 1,
                              "reason": results[e.url]}
    hidden = {u for u, s in failing.items() if s["fails"] >= FAILS_TO_HIDE}
    kept = [e for e in entries if e.url not in hidden]

    with open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(header + "\n")
        for e in kept:
            f.write("\n".join(e.lines()) + "\n")
    with open(STATUS, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"sources": SOURCES, "channels": len(entries), "kept": len(kept), "hidden": len(hidden),
                   "failing": dict(sorted(failing.items()))}, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print(f"{len(kept)} kept, {len(hidden)} hidden, {len(failing) - len(hidden)} failing once "
          f"({time.time() - started:.0f} s)")


if __name__ == "__main__":
    main()

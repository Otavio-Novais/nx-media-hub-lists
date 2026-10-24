#!/usr/bin/env python3
"""Builds lista_limpa.m3u: merges the public iptv-org lists, removes duplicates and hides dead channels.

The check runs from GitHub's servers (USA), not from the Switch's network. Any HTTP error, DNS/connection
error or timeout counts as a failure, 401/403/451 included: measured on 2026-10-20, 280 of the ~310 channels
refused from the runner were refused from Brazil too (the rest were mostly foreign channels).
Like the player, it follows the highest-bandwidth variant (plus its audio playlist) down to a full video
segment: all of it must arrive within STARTUP_LIMIT, and the segment must download faster than it plays.
A channel is hidden after FAILS_TO_HIDE runs in a row.

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
    "https://iptv-org.github.io/iptv/languages/eng.m3u",
    "https://iptv-org.github.io/iptv/categories/sports.m3u",
    "https://iptv-org.github.io/iptv/categories/news.m3u",
    "https://raw.githubusercontent.com/Ramys/Iptv-Brasil-2026/refs/heads/master/CanaisBR02.m3u8",
    "https://raw.githubusercontent.com/Ramys/Iptv-Brasil-2026/refs/heads/master/CanaisBR04.m3u8",
    "https://raw.githubusercontent.com/Ramys/Iptv-Brasil-2026/refs/heads/master/CanaisBR01.m3u8"
]
OUTPUT = "lista_limpa.m3u"
STATUS = "status.json"

FAILS_TO_HIDE = 2       # consecutive failed runs (each run already retries its failures once)
STARTUP_LIMIT = 10      # seconds from the first request to the first video segment, like waiting in the app
MIN_SPEED = 1.0         # the segment must download at least this many times faster than it plays
MEASURE_LIMIT = 30      # measuring goes on until here, so slow channels still get numbers
METRICS = "metrics.json"  # every channel's numbers: uploaded as a workflow artifact, not committed
WORKERS = 64
RETRY_WORKERS = 16      # slow channels are retried with little parallelism: our own downloads must not slow them
DEFAULT_USER_AGENT = "libmpv"  # what the app's player sends when the list sets none
MAX_PLAYLIST_BYTES = 1 << 20
MAX_SEGMENT_BYTES = 16 << 20
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

    def title_comma(self):
        """Index of the comma before the title: the first one outside quotes (attributes may contain commas)."""
        quoted = False
        for i, ch in enumerate(self.extinf):
            if ch == '"':
                quoted = not quoted
            elif ch == "," and not quoted:
                return i
        return -1

    @property
    def name(self):
        i = self.title_comma()
        return self.extinf[i + 1:].strip() if i >= 0 else self.url

    def groups(self):
        m = re.search(r'group-title="([^"]*)"', self.extinf)
        return [g.strip() for g in m.group(1).split(";") if g.strip()] if m else []

    def set_groups(self, groups):
        value = ";".join(groups)
        if 'group-title="' in self.extinf:
            self.extinf = re.sub(r'group-title="[^"]*"', lambda _: f'group-title="{value}"', self.extinf, count=1)
        elif value and (i := self.title_comma()) >= 0:
            self.extinf = f'{self.extinf[:i]} group-title="{value}"{self.extinf[i:]}'

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

def fetch(url, headers, limit, deadline):
    """GET returning (status, body, final URL after redirects: the base for relative URIs) before `deadline`
    (time.monotonic()); raises Dead when gone or too slow."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Dead(f"slow start (> {MEASURE_LIMIT} s)")
    try:
        with OPENER.open(urllib.request.Request(url, headers=headers), timeout=remaining) as response:
            body = bytearray()
            while len(body) < limit:
                chunk = response.read(min(1 << 16, limit - len(body)))
                if not chunk:
                    break
                body += chunk
                if time.monotonic() > deadline:
                    raise Dead(f"slow start (> {MEASURE_LIMIT} s)")
            return response.status, bytes(body), response.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (416, 429):
            return e.code, b"", url  # range not served / rate limited by our parallel requests: not a failure
        raise Dead(f"HTTP {e.code}")
    except http.client.IncompleteRead as e:
        return 200, e.partial, url  # live stream cut while reading: it was sending data
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


def best_variant(lines, base):
    """Master playlist: the highest-bandwidth variant (what mpv picks) and its separate audio playlist, if any."""
    best, best_bandwidth, group = None, -1, None
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        uri = next((l for l in lines[i + 1:] if l and not l.startswith("#")), None)
        m = re.search(r"[:,]BANDWIDTH=(\d+)", line)
        bandwidth = int(m.group(1)) if m else 0
        if uri and bandwidth > best_bandwidth:
            m = re.search(r'AUDIO="([^"]+)"', line)
            best, best_bandwidth, group = uri, bandwidth, m.group(1) if m else None
    audio = None
    for line in lines:
        m = re.search(r'URI="([^"]+)"', line)
        if group and m and line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line and f'GROUP-ID="{group}"' in line:
            audio = m.group(1)
            if "DEFAULT=YES" in line:
                break
    return (urllib.parse.urljoin(base, best) if best else None), (urllib.parse.urljoin(base, audio) if audio else None)


def last_segment(lines, base):
    """Media playlist: the newest segment and its duration in seconds."""
    segment, duration, pending = None, 0.0, 0.0
    for line in lines:
        if line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:\s*([\d.]+)", line)
            pending = float(m.group(1)) if m else 0.0
        elif line and not line.startswith("#"):
            segment, duration = line, pending
    return (urllib.parse.urljoin(base, segment) if segment else None), duration


def check(entry):
    """(state, reason, metrics): state "ok" or "dead"; metrics {"startup": s, "speed": x} when the first
    segment arrived. Mirrors the player: everything until the first segment must arrive within STARTUP_LIMIT
    and the segment must download MIN_SPEED times faster than it plays. Measures up to MEASURE_LIMIT so the
    runner's and Brazil's numbers can be compared (see calibrate.py)."""
    url, headers = entry.headers()
    started = time.monotonic()
    deadline = started + MEASURE_LIMIT
    try:
        status, body, url = fetch(url, headers, MAX_PLAYLIST_BYTES, deadline)
        for _ in range(3):  # master playlist(s) -> media playlist
            if status in (416, 429) or not body.lstrip().startswith(b"#EXTM3U"):
                return "ok", None, None  # rate limited, or a direct stream that answered
            lines = [l.strip() for l in body.decode("utf-8", "replace").splitlines()]
            if not any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
                break
            url, audio = best_variant(lines, url)
            if not url:
                return "dead", "empty playlist", None
            if audio:
                fetch(audio, headers, MAX_PLAYLIST_BYTES, deadline)
            status, body, url = fetch(url, headers, MAX_PLAYLIST_BYTES, deadline)
        else:
            return "ok", None, None

        segment, duration = last_segment(lines, url)
        if not segment:
            return "dead", "empty playlist", None
        segment_started = time.monotonic()
        status, data, _ = fetch(segment, headers, MAX_SEGMENT_BYTES, deadline)
        now = time.monotonic()
        startup, took = now - started, now - segment_started
        complete = duration and status < 300 and len(data) < MAX_SEGMENT_BYTES
        speed = duration / max(took, 0.001) if complete else None
        metrics = {"startup": round(startup, 2), "speed": round(speed, 2) if speed else None}
        if startup > STARTUP_LIMIT:
            return "dead", f"slow start ({startup:.1f} s)", metrics
        if speed is not None and speed < MIN_SPEED:
            return "dead", f"slow ({took:.1f} s to download {duration:.0f} s of video)", metrics
        return "ok", None, metrics
    except Dead as e:
        return "dead", str(e), None
    except Exception as e:  # unexpected (bad header value, odd URL...): not proof the channel is dead
        print(f"  check error, kept: {entry.name}: {type(e).__name__}: {e}")
        return "ok", None, None


def check_all(entries, workers=WORKERS):
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
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
    # Retry the failures (transient ones do not count). Only the slow ones (they have metrics) are
    # sensitive to our own parallel downloads; errors and timeouts are retried at full parallelism.
    failed = [e for e in entries if results[e.url][0] == "dead"]
    slow = [e for e in failed if results[e.url][2]]
    print(f"first pass: {len(failed)} failed ({len(slow)} slow), retrying them")
    time.sleep(30)
    results.update(check_all([e for e in failed if not results[e.url][2]]))
    results.update(check_all(slow, RETRY_WORKERS))

    with open(METRICS, "w", encoding="utf-8") as f:
        json.dump({e.url: {"name": e.name, "state": results[e.url][0], "reason": results[e.url][1],
                           **(results[e.url][2] or {})} for e in entries}, f, ensure_ascii=False)

    failed_count = sum(1 for state, _, _ in results.values() if state == "dead")
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
        state, reason, _ = results[e.url]
        if state == "dead":
            failing[e.url] = {"name": e.name, "fails": previous.get(e.url, {}).get("fails", 0) + 1, "reason": reason}
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

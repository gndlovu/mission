"""Media stack panel for Operator OS — Radarr / Sonarr / Prowlarr / clients.

Stdlib only, matching the rest of the project.

Credentials are read server-side from the environment or $HERMES_HOME/.env and
are NEVER included in the JSON returned to the browser. The dashboard is
unauthenticated on the LAN, so this module exposes status only: what is
downloading, library counts, disk and request backlog.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT = 6
TTL = 8.0  # seconds; the dashboard polls, the *arr APIs should not be hammered

_ENV_KEYS = (
    "RADARR_URL", "RADARR_API_KEY",
    "SONARR_URL", "SONARR_API_KEY",
    "PROWLARR_URL", "PROWLARR_API_KEY",
    "SEERR_URL", "SEERR_API_KEY",
    "QBITTORRENT_URL", "TRANSMISSION_URL",
    # optional extras — shown as launcher links when present
    "JELLYFIN_URL", "PLEX_URL", "IMMICH_URL", "FLARESOLVERR_URL",
)

# name -> (env var, glyph). Base URLs are not secrets; keys never leave the host.
_LINKS = (
    ("Radarr", "RADARR_URL", "R"),
    ("Sonarr", "SONARR_URL", "S"),
    ("Prowlarr", "PROWLARR_URL", "P"),
    ("Seerr", "SEERR_URL", "J"),
    ("qBittorrent", "QBITTORRENT_URL", "Q"),
    ("Transmission", "TRANSMISSION_URL", "T"),
    ("Jellyfin", "JELLYFIN_URL", "F"),
    ("Plex", "PLEX_URL", "X"),
    ("Immich", "IMMICH_URL", "I"),
)

# Launcher targets come from the environment only. Deliberately no baked-in
# defaults: hardcoding real addresses would publish internal network topology
# in a public repo. Set JELLYFIN_URL / PLEX_URL / IMMICH_URL in $HERMES_HOME/.env
# and the tiles appear.
_LINK_FALLBACK: dict[str, str] = {}


def _load_env(hermes_home: Path) -> dict:
    """Environment first, then $HERMES_HOME/.env as a fallback."""
    out = {k: v for k, v in os.environ.items() if k in _ENV_KEYS and v}
    if all(k in out for k in _ENV_KEYS):
        return out
    envfile = Path(hermes_home) / ".env"
    try:
        for line in envfile.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in _ENV_KEYS and k not in out:
                out[k] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def _get(url: str, headers: dict | None = None, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip()[:1] in "[{" else body


def _arr(base: str, key: str, path: str, params: dict | None = None):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _get(url, {"X-Api-Key": key})


def _gb(n) -> float:
    try:
        return round(float(n) / 1e9, 2)
    except (TypeError, ValueError):
        return 0.0


def _qbit(base: str) -> list:
    out = []
    try:
        for t in _get(base.rstrip("/") + "/api/v2/torrents/info") or []:
            out.append({
                "name": t.get("name") or "?",
                "pct": round((t.get("progress") or 0) * 100, 1),
                "state": t.get("state") or "",
                "size_gb": _gb(t.get("size")),
                "done_gb": _gb(t.get("completed")),
                "dl_rate": round((t.get("dlspeed") or 0) / 1e6, 2),
                "eta": t.get("eta") if (t.get("eta") or 0) < 8640000 else None,
                "client": "qbittorrent",
            })
    except Exception:
        pass
    return out


def _transmission(base: str) -> list:
    url = base.rstrip("/") + "/transmission/rpc"
    sid = ""
    try:  # CSRF dance: bare request returns 409 + session id header
        _get(url, data=b"{}")
    except urllib.error.HTTPError as e:
        sid = e.headers.get("X-Transmission-Session-Id", "")
    except Exception:
        return []
    body = json.dumps({"method": "torrent-get", "arguments": {"fields": [
        "name", "percentDone", "status", "totalSize", "downloadedEver",
        "rateDownload", "eta"]}}).encode()
    try:
        d = _get(url, {"Content-Type": "application/json",
                       "X-Transmission-Session-Id": sid}, data=body)
    except Exception:
        return []
    if not isinstance(d, dict) or d.get("result") != "success":
        return []
    STATES = {0: "stopped", 1: "check-wait", 2: "checking", 3: "dl-wait",
              4: "downloading", 5: "seed-wait", 6: "seeding"}
    out = []
    for t in d.get("arguments", {}).get("torrents", []) or []:
        out.append({
            "name": t.get("name") or "?",
            "pct": round((t.get("percentDone") or 0) * 100, 1),
            "state": STATES.get(t.get("status"), str(t.get("status"))),
            "size_gb": _gb(t.get("totalSize")),
            "done_gb": _gb(t.get("downloadedEver")),
            "dl_rate": round((t.get("rateDownload") or 0) / 1e6, 2),
            "eta": t.get("eta") if (t.get("eta") or -1) > 0 else None,
            "client": "transmission",
        })
    return out


class MediaStack:
    """Cached, read-only view of the media stack."""

    def __init__(self, hermes_home):
        self.home = Path(hermes_home)
        self._cache = None
        self._at = 0.0

    def snapshot(self, force: bool = False) -> dict:
        now = time.time()
        if self._cache and not force and (now - self._at) < TTL:
            return self._cache
        snap = self._build()
        self._cache, self._at = snap, now
        return snap

    def _build(self) -> dict:
        env = _load_env(self.home)
        out = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "downloads": [], "library": {}, "disk": {},
            "requests": {}, "indexers": {}, "services": [], "errors": [],
            "links": [], "health": [], "recent": [], "upcoming": [],
        }

        def service(name, ok):
            out["services"].append({"name": name, "up": bool(ok)})

        # ---- download clients
        if env.get("QBITTORRENT_URL"):
            q = _qbit(env["QBITTORRENT_URL"])
            out["downloads"] += q
            service("qbittorrent", True)
        if env.get("TRANSMISSION_URL"):
            t = _transmission(env["TRANSMISSION_URL"])
            out["downloads"] += t
            service("transmission", True)

        # active first, then by progress
        act = {"downloading", "dl-wait", "checking", "stalledDL", "queuedDL", "metaDL"}
        out["downloads"].sort(
            key=lambda d: (d["state"] not in act, -(d.get("dl_rate") or 0), -d["pct"]))
        out["active"] = sum(1 for d in out["downloads"] if d["state"] in act)
        out["dl_total_mbps"] = round(sum(d.get("dl_rate") or 0 for d in out["downloads"]), 2)

        # ---- Radarr
        ru, rk = env.get("RADARR_URL"), env.get("RADARR_API_KEY")
        if ru and rk:
            try:
                movies = _arr(ru, rk, "/api/v3/movie")
                out["library"]["movies"] = len(movies)
                out["library"]["movies_missing"] = sum(
                    1 for m in movies if not m.get("hasFile"))
                service("radarr", True)
            except Exception as e:
                out["errors"].append(f"radarr: {e.__class__.__name__}")
                service("radarr", False)
            try:
                dsk = _arr(ru, rk, "/api/v3/diskspace") or []
                for d in dsk:
                    if (d.get("path") or "").startswith("/shared"):
                        free, total = d.get("freeSpace") or 0, d.get("totalSpace") or 0
                        out["disk"] = {
                            "path": d.get("path"), "free_gb": _gb(free),
                            "total_gb": _gb(total),
                            "used_pct": round((total - free) / total * 100, 1) if total else 0,
                        }
                        break
            except Exception:
                pass

        # ---- Sonarr
        su, sk = env.get("SONARR_URL"), env.get("SONARR_API_KEY")
        if su and sk:
            try:
                series = _arr(su, sk, "/api/v3/series")
                have = tot = 0
                for s in series:
                    st = s.get("statistics") or {}
                    have += st.get("episodeFileCount") or 0
                    tot += st.get("episodeCount") or 0
                out["library"].update({"series": len(series),
                                       "episodes_have": have, "episodes_total": tot})
                service("sonarr", True)
            except Exception as e:
                out["errors"].append(f"sonarr: {e.__class__.__name__}")
                service("sonarr", False)

        # ---- Prowlarr indexer health
        pu, pk = env.get("PROWLARR_URL"), env.get("PROWLARR_API_KEY")
        if pu and pk:
            try:
                idx = _arr(pu, pk, "/api/v1/indexer") or []
                bad = _arr(pu, pk, "/api/v1/indexerstatus") or []
                names = {i.get("id"): i.get("name") for i in idx}
                disabled = [{"name": names.get(b.get("indexerId"), b.get("indexerId")),
                             "till": b.get("disabledTill")} for b in bad]
                out["indexers"] = {"total": len(idx), "failing": len(bad),
                                   "disabled": disabled}
                service("prowlarr", True)
            except Exception as e:
                out["errors"].append(f"prowlarr: {e.__class__.__name__}")
                service("prowlarr", False)

        # ---- health warnings (what the services themselves complain about)
        for label, base, key, ver in (("radarr", ru, rk, "v3"),
                                      ("sonarr", su, sk, "v3"),
                                      ("prowlarr", pu, pk, "v1")):
            if not (base and key):
                continue
            try:
                h = _arr(base, key, f"/api/{ver}/health") or []
                for item in h:
                    msg = (item.get("message") or "")
                    # Known-stale: every *arr keeps its own cached copy of this
                    # list and does not clear it when an indexer recovers.
                    # indexerstatus/disabledTill is the authoritative source.
                    if msg.startswith("Indexers unavailable due to failures"):
                        continue
                    out["health"].append({
                        "service": label,
                        "type": item.get("type") or "warning",
                        "message": msg[:160],
                    })
            except Exception:
                pass

        # ---- recently imported
        if ru and rk:
            try:
                h = _arr(ru, rk, "/api/v3/history",
                         {"page": 1, "pageSize": 8, "eventType": 3,
                          "includeMovie": "true",
                          "sortKey": "date", "sortDirection": "descending"})
                for rec in (h or {}).get("records", []):
                    out["recent"].append({
                        "kind": "movie",
                        "title": (rec.get("movie") or {}).get("title") or "?",
                        "quality": (((rec.get("quality") or {}).get("quality")) or {}).get("name"),
                        "date": rec.get("date"),
                    })
            except Exception:
                pass
        if su and sk:
            try:
                h = _arr(su, sk, "/api/v3/history",
                         {"page": 1, "pageSize": 8, "eventType": 3,
                          "includeSeries": "true", "includeEpisode": "true",
                          "sortKey": "date", "sortDirection": "descending"})
                for rec in (h or {}).get("records", []):
                    ep = rec.get("episode") or {}
                    out["recent"].append({
                        "kind": "episode",
                        "title": f"{(rec.get('series') or {}).get('title','?')} "
                                 f"S{ep.get('seasonNumber',0):02d}E{ep.get('episodeNumber',0):02d}",
                        "quality": (((rec.get("quality") or {}).get("quality")) or {}).get("name"),
                        "date": rec.get("date"),
                    })
            except Exception:
                pass
        out["recent"].sort(key=lambda r: r.get("date") or "", reverse=True)
        out["recent"] = out["recent"][:10]

        # ---- upcoming episodes (next 7 days)
        if su and sk:
            try:
                start = time.strftime("%Y-%m-%d")
                end = time.strftime("%Y-%m-%d", time.localtime(time.time() + 7 * 86400))
                cal = _arr(su, sk, "/api/v3/calendar",
                           {"start": start, "end": end, "includeSeries": "true"}) or []
                for c in cal[:12]:
                    out["upcoming"].append({
                        "title": (c.get("series") or {}).get("title") or "?",
                        "ep": f"S{c.get('seasonNumber',0):02d}E{c.get('episodeNumber',0):02d}",
                        "name": c.get("title") or "",
                        "airs": c.get("airDateUtc") or c.get("airDate"),
                        "has": bool(c.get("hasFile")),
                    })
            except Exception:
                pass

        # ---- Seerr request backlog
        eu, ek = env.get("SEERR_URL"), env.get("SEERR_API_KEY")
        if eu and ek:
            try:
                r = _get(eu.rstrip("/") + "/api/v1/request?take=200",
                         {"X-Api-Key": ek}) or {}
                MS = {1: "unknown", 2: "pending", 3: "processing",
                      4: "partial", 5: "available"}
                counts = {}
                for item in r.get("results", []) or []:
                    k = MS.get((item.get("media") or {}).get("status"), "other")
                    counts[k] = counts.get(k, 0) + 1
                out["requests"] = counts
                service("seerr", True)
            except Exception as e:
                out["errors"].append(f"seerr: {e.__class__.__name__}")
                service("seerr", False)

        # ---- accurate indexer warning, from disabledTill not the health text
        for d in (out["indexers"].get("disabled") or []):
            till = (d.get("till") or "").replace("T", " ")[:16]
            out["health"].append({
                "service": "prowlarr",
                "type": "warning",
                "message": f"{d['name']} disabled until {till} UTC",
            })

        # ---- launcher links (URL only, never keys).
        # Built last so every service has already reported its up/down state.
        up_map = {s["name"]: s["up"] for s in out["services"]}
        for label, var, glyph in _LINKS:
            url = env.get(var) or _LINK_FALLBACK.get(var)
            if not url:
                continue
            out["links"].append({
                "name": label, "url": url.rstrip("/"), "glyph": glyph,
                "up": up_map.get(label.lower()),   # None = not probed
            })

        return out

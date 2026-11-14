#!/usr/bin/env python3
"""
Mission — a standalone agentic dashboard for Hermes.

Deliberately dependency-free: Python 3.9+ standard library only, no pip install,
no build step. Reads the Hermes home directory READ-ONLY and serves a small JSON
API plus a static frontend.

  python3 server.py --port 8081
  python3 server.py --port 8081 --hermes-home /home/hermes/.hermes

Binds 127.0.0.1 by default. Use --host 0.0.0.0 only on a trusted LAN.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from auth import COOKIE, TTL

HERE = Path(__file__).resolve().parent
WEB_ROOT = HERE / "web"

# Files we will never serve, even if dropped into web/.
BLOCKED_NAMES = {".env", "auth.json", "config.yaml", "state.db"}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}

STALE_DAYS = 14
DAY = 86400.0

# Artifact gallery: extension -> logical type used by the filter chips.
DOC_TYPES = {
    "html": {"html", "htm"},
    "markdown": {"md", "markdown", "mdx"},
    "code": {"py", "js", "ts", "tsx", "jsx", "sh", "rs", "go", "rb", "java",
             "c", "h", "cpp", "css", "scss", "sql", "vue", "lua", "php"},
    "data": {"json", "yaml", "yml", "csv", "tsv", "toml", "xml", "ini"},
    "image": {"png", "jpg", "jpeg", "webp", "gif", "svg", "avif"},
    "doc": {"pdf", "docx", "xlsx", "pptx", "odt", "rtf", "epub"},
    "text": {"txt", "log", "text"},
}

# Never list or serve these out of the artifact directory.
DOC_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                 ".cache", "dist", "build", ".codegraph"}
DOC_SKIP_NAMES = {".env", ".env.local", "auth.json", "id_rsa", "id_ed25519",
                  ".npmrc", ".netrc", "credentials"}

TEXTY = {"html", "markdown", "code", "data", "text"}

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _clean_cfg(value: str) -> str | None:
    """Last line of a `hermes config get`, or None when the key is unset."""
    if not value:
        return None
    line = value.strip().splitlines()[-1].strip().strip('"')
    if not line or "not set" in line.lower() or line.startswith("["):
        return None
    return line


def doc_type(suffix: str) -> str:
    s = suffix.lower().lstrip(".")
    for kind, exts in DOC_TYPES.items():
        if s in exts:
            return kind
    return "other"


# --------------------------------------------------------------------------
# Hermes data access (read-only)
# --------------------------------------------------------------------------

class HermesData:
    def __init__(self, home: Path):
        self.home = home
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key: str, ttl: float, producer):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = producer()
        self._cache[key] = (now, value)
        return value

    # -- sessions ---------------------------------------------------------

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        db_path = self.home / "state.db"
        if not db_path.exists():
            return []
        # immutable=1 guarantees we never write or lock the live DB.
        uri = f"file:{db_path}?mode=ro&immutable=1"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=2.0)
        except sqlite3.Error:
            return []
        try:
            con.row_factory = sqlite3.Row
            return list(con.execute(sql, args))
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def sessions(self, limit: int = 50) -> list[dict]:
        def build():
            rows = self._query(
                """
                SELECT id, source, display_name, title, model, message_count,
                       tool_call_count, input_tokens, output_tokens,
                       estimated_cost_usd, actual_cost_usd,
                       started_at, last_activity_at, cwd, git_branch
                FROM sessions
                ORDER BY COALESCE(last_activity_at, started_at) DESC
                LIMIT ?
                """,
                (limit,),
            )
            out = []
            for r in rows:
                d = dict(r)
                d["cost_usd"] = d.pop("actual_cost_usd", None) or d.pop(
                    "estimated_cost_usd", None
                ) or 0.0
                d.pop("estimated_cost_usd", None)
                out.append(d)
            return out

        return self._cached(f"sessions:{limit}", 5.0, build)

    def totals(self) -> dict:
        def build():
            rows = self._query(
                """
                SELECT COUNT(*) AS sessions,
                       COALESCE(SUM(message_count), 0) AS messages,
                       COALESCE(SUM(tool_call_count), 0) AS tool_calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       COALESCE(SUM(api_call_count), 0) AS api_calls,
                       COALESCE(SUM(COALESCE(actual_cost_usd,
                                             estimated_cost_usd, 0)), 0) AS cost_usd
                FROM sessions
                """
            )
            return dict(rows[0]) if rows else {}

        return self._cached("totals", 10.0, build)

    def spend_by_day(self, days: int = 14) -> list[dict]:
        def build():
            cutoff = time.time() - days * DAY
            rows = self._query(
                """
                SELECT started_at,
                       COALESCE(actual_cost_usd, estimated_cost_usd, 0) AS cost,
                       COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) AS tokens
                FROM sessions
                WHERE started_at IS NOT NULL AND started_at >= ?
                """,
                (cutoff,),
            )
            buckets: dict[str, dict] = {}
            for i in range(days):
                day = time.strftime(
                    "%Y-%m-%d", time.localtime(time.time() - (days - 1 - i) * DAY)
                )
                buckets[day] = {"day": day, "cost": 0.0, "tokens": 0}
            for r in rows:
                day = time.strftime("%Y-%m-%d", time.localtime(r["started_at"]))
                if day in buckets:
                    buckets[day]["cost"] += float(r["cost"] or 0)
                    buckets[day]["tokens"] += int(r["tokens"] or 0)
            return list(buckets.values())

        return self._cached(f"spend:{days}", 30.0, build)

    # -- skills -----------------------------------------------------------

    def skills(self) -> list[dict]:
        def build():
            root = self.home / "skills"
            if not root.is_dir():
                return []
            out = []
            for skill_md in sorted(root.rglob("SKILL.md")):
                try:
                    text = skill_md.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rel = skill_md.relative_to(root)
                parts = rel.parts
                category = parts[0] if len(parts) > 2 else "uncategorised"
                name = skill_md.parent.name
                desc = ""
                m = re.search(r"^description:\s*(.+)$", text, re.M)
                if m:
                    desc = m.group(1).strip().strip("\"'")
                try:
                    st = skill_md.stat()
                    mtime = st.st_mtime
                except OSError:
                    mtime = 0.0
                size = 0
                try:
                    size = sum(
                        f.stat().st_size
                        for f in skill_md.parent.rglob("*")
                        if f.is_file()
                    )
                except OSError:
                    pass
                age_days = (time.time() - mtime) / DAY if mtime else 999.0
                out.append(
                    {
                        "name": name,
                        "category": category,
                        "description": desc,
                        "path": str(rel),
                        "bytes": size,
                        "mtime": mtime,
                        "age_days": round(age_days, 1),
                        "stale": age_days > STALE_DAYS,
                    }
                )
            return out

        return self._cached("skills", 20.0, build)

    # -- memory -----------------------------------------------------------

    def memory_entries(self) -> list[dict]:
        def build():
            out = []
            mem_dir = self.home / "memories"
            if not mem_dir.is_dir():
                return out
            for path in sorted(mem_dir.glob("*.md")):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                # Hermes separates entries with a lone "§" line; older/other
                # files may use blank lines. Accept either, so a file with no
                # blank lines does not collapse into a single block.
                blocks = [
                    b.strip()
                    for b in re.split(r"\n\s*§[^\S\n]*\n|\n\s*\n", text)
                    if b.strip()
                ]
                for i, block in enumerate(blocks):
                    out.append(
                        {
                            "id": f"{path.stem}:{i}",
                            "file": path.name,
                            "text": block,
                            "chars": len(block),
                        }
                    )
            return out

        return self._cached("memory", 20.0, build)

    # -- graph ------------------------------------------------------------

    def graph(self) -> dict:
        """Build the node/edge set rendered as the glowing orb field."""

        def build():
            nodes: list[dict] = []
            edges: list[dict] = []

            # Centre: the agent itself.
            nodes.append(
                {"id": "agent", "label": "hermes", "kind": "agent", "weight": 10.0}
            )

            # Memory entries -> "core" (green).
            for entry in self.memory_entries():
                nodes.append(
                    {
                        "id": f"mem:{entry['id']}",
                        "label": entry["text"][:46].replace("\n", " "),
                        "kind": "core",
                        "weight": 1.2 + min(entry["chars"], 600) / 300.0,
                        "detail": entry["text"][:400],
                    }
                )
                edges.append({"source": "agent", "target": f"mem:{entry['id']}"})

            # Skills -> "workspace" (amber), grouped under a category hub.
            skills = self.skills()
            cats: dict[str, list[dict]] = {}
            for s in skills:
                cats.setdefault(s["category"], []).append(s)
            for cat, items in sorted(cats.items()):
                cat_id = f"cat:{cat}"
                nodes.append(
                    {
                        "id": cat_id,
                        "label": cat,
                        "kind": "hub",
                        "weight": 2.4 + len(items) * 0.2,
                        "detail": f"{len(items)} skills",
                    }
                )
                edges.append({"source": "agent", "target": cat_id})
                for s in items:
                    nodes.append(
                        {
                            "id": f"skill:{s['path']}",
                            "label": s["name"],
                            "kind": "stale" if s["stale"] else "workspace",
                            "weight": 1.0 + min(s["bytes"], 60000) / 30000.0,
                            "detail": s["description"][:200],
                            "age_days": s["age_days"],
                        }
                    )
                    edges.append(
                        {"source": cat_id, "target": f"skill:{s['path']}"}
                    )

            # Sessions -> "index" (blue).
            for s in self.sessions(limit=40):
                label = s.get("title") or s.get("display_name") or s["id"][:12]
                nodes.append(
                    {
                        "id": f"sess:{s['id']}",
                        "label": str(label)[:46],
                        "kind": "index",
                        "weight": 1.0 + min(int(s.get("message_count") or 0), 200) / 100.0,
                        "detail": f"{s.get('message_count') or 0} messages · {s.get('source') or '?'}",
                    }
                )
                edges.append({"source": "agent", "target": f"sess:{s['id']}"})

            counts: dict[str, int] = {}
            for n in nodes:
                counts[n["kind"]] = counts.get(n["kind"], 0) + 1

            return {
                "nodes": nodes,
                "edges": edges,
                "counts": counts,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "skills": len(skills),
                    "memory": len(self.memory_entries()),
                    "stale": counts.get("stale", 0),
                },
            }

        return self._cached("graph", 15.0, build)

    # -- status -----------------------------------------------------------

    def status(self) -> dict:
        cfg = self.home / "config.yaml"
        model = profile = theme = None
        if cfg.exists():
            try:
                text = cfg.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"^\s*name:\s*(\S+)", text, re.M)
                if m:
                    model = m.group(1)
                m = re.search(r"^\s*theme:\s*(\S+)", text, re.M)
                if m:
                    theme = m.group(1)
            except OSError:
                pass
        profile = os.environ.get("HERMES_PROFILE", "default")
        totals = self.totals()
        graph_stats = self.graph()["stats"]
        return {
            "agent": "hermes",
            "online": True,
            "home": str(self.home),
            "profile": profile,
            "model": model,
            "theme": theme,
            "totals": totals,
            "graph": graph_stats,
            "server_time": time.time(),
        }


# --------------------------------------------------------------------------
# Extra Hermes surfaces: kanban, cron, stack, insights, conversations
# --------------------------------------------------------------------------

class HermesExtras:
    """Read-only views over kanban.db, cron state, config and the Hermes CLI."""

    def __init__(self, home: Path, hermes_bin: str = "hermes"):
        self.home = home
        self.bin = hermes_bin
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key: str, ttl: float, producer):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = producer()
        self._cache[key] = (now, value)
        return value

    # -- helpers ----------------------------------------------------------

    def _db(self, name: str, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        path = self.home / name
        if not path.exists():
            return []
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error:
            return []
        try:
            con.row_factory = sqlite3.Row
            return list(con.execute(sql, args))
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def _cli(self, args: list[str], ttl: float = 60.0) -> str:
        """Run a read-only hermes CLI command. Cached; never raises."""
        key = "cli:" + " ".join(args)

        def run():
            try:
                proc = subprocess.run(
                    [self.bin, *args],
                    capture_output=True, text=True, timeout=20,
                    env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                return ANSI_RE.sub("", out).strip()
            except Exception as exc:
                return f"[unavailable: {exc.__class__.__name__}]"

        return self._cached(key, ttl, run)

    # -- mission control (kanban) -----------------------------------------

    def mission(self) -> dict:
        def build():
            tasks = []
            for r in self._db(
                "kanban.db",
                """SELECT id, title, body, status, priority, assignee, created_by,
                          created_at, started_at, completed_at, workspace_kind,
                          workspace_path, consecutive_failures, last_failure_error,
                          goal_mode, completion_contract, skills, model_override
                   FROM tasks ORDER BY CAST(created_at AS INTEGER) DESC LIMIT 60""",
            ):
                d = dict(r)
                for k in ("created_at", "started_at", "completed_at"):
                    try:
                        d[k] = float(d[k]) if d[k] not in (None, "") else None
                    except (TypeError, ValueError):
                        d[k] = None
                d["failures"] = int(d.pop("consecutive_failures") or 0)
                tasks.append(d)

            runs = [dict(r) for r in self._db(
                "kanban.db",
                """SELECT id, task_id, status, outcome, summary, error,
                          started_at, ended_at
                   FROM task_runs ORDER BY id DESC LIMIT 40""")]

            events = [dict(r) for r in self._db(
                "kanban.db",
                "SELECT COUNT(*) AS n FROM task_events")]

            by_status: dict[str, int] = {}
            for t in tasks:
                by_status[t["status"] or "?"] = by_status.get(t["status"] or "?", 0) + 1

            return {
                "tasks": tasks,
                "runs": runs,
                "by_status": by_status,
                "stats": {
                    "tasks": len(tasks),
                    "runs": len(runs),
                    "events": events[0]["n"] if events else 0,
                    "failing": sum(1 for t in tasks if t["failures"] > 0),
                },
            }

        return self._cached("mission", 10.0, build)

    # -- schedule (cron) ---------------------------------------------------

    def schedule(self) -> dict:
        def build():
            listing = self._cli(["cron", "list"], ttl=45.0)
            jobs_found = "No scheduled jobs" not in listing and bool(listing)

            execs = [dict(r) for r in self._db(
                "cron/executions.db",
                """SELECT id, job_id, source, status, started_at, finished_at
                   FROM executions ORDER BY id DESC LIMIT 30""")]

            # The ticker heartbeat proves the scheduler is actually alive.
            beat = None
            hb = self.home / "cron" / "ticker_heartbeat"
            if hb.exists():
                try:
                    # format is "<epoch_float> <pid>"
                    raw = hb.read_text(errors="replace").strip().split()
                    beat = float(raw[0]) if raw else None
                except (OSError, ValueError, IndexError):
                    pass

            age = None
            if isinstance(beat, (int, float)):
                age = round(time.time() - beat, 1)

            return {
                "listing": listing,
                "has_jobs": jobs_found,
                "executions": execs,
                "heartbeat": beat,
                "heartbeat_age_s": age,
                "scheduler_alive": age is not None and age < 180,
            }

        return self._cached("schedule", 20.0, build)

    # -- stack (connections) ----------------------------------------------

    def stack(self) -> dict:
        def build():
            # messaging channels
            channels = []
            cd = self.home / "channel_directory.json"
            if cd.exists():
                try:
                    blob = json.loads(cd.read_text(errors="replace"))
                    for plat, entries in (blob.get("platforms") or {}).items():
                        for e in entries or []:
                            channels.append({
                                "platform": plat,
                                "name": e.get("name"),
                                "type": e.get("type"),
                            })
                except (OSError, ValueError):
                    pass

            # MCP servers + plugins straight off config.yaml / disk
            mcp = []
            cfg = self.home / "config.yaml"
            if cfg.exists():
                try:
                    text = cfg.read_text(errors="replace")
                    block = re.search(r"^mcp_servers:\n((?:[ \t].*\n|\n)*)", text, re.M)
                    if block:
                        for m in re.finditer(r"^  ([A-Za-z0-9_.-]+):\s*$", block.group(1), re.M):
                            name = m.group(1)
                            tail = block.group(1)[m.end():m.end() + 400]
                            enabled = "enabled: false" not in tail.split("\n  ")[0]
                            mcp.append({"name": name, "enabled": enabled})
                except OSError:
                    pass

            plugins = []
            pdir = self.home / "plugins"
            if pdir.is_dir():
                for p in sorted(pdir.iterdir()):
                    if p.is_dir() and not p.name.startswith("."):
                        plugins.append({
                            "name": p.name,
                            "dashboard": (p / "dashboard" / "manifest.json").exists(),
                        })

            profiles = []
            prof = self.home / "profiles"
            if prof.is_dir():
                profiles = [p.name for p in sorted(prof.iterdir()) if p.is_dir()]

            model = self._cli(["config", "get", "model.default"], ttl=300.0)
            provider = self._cli(["config", "get", "model.provider"], ttl=300.0)

            skins = []
            sdir = self.home / "dashboard-themes"
            if sdir.is_dir():
                skins = [p.stem for p in sorted(sdir.glob("*.yaml"))]

            return {
                "channels": channels,
                "mcp": mcp,
                "plugins": plugins,
                "profiles": profiles or ["default"],
                "model": _clean_cfg(model),
                "provider": _clean_cfg(provider),
                "dashboard_themes": skins,
            }

        return self._cached("stack", 60.0, build)

    # -- insights (curator / "dreaming" + efficiency) ---------------------

    def insights(self, totals: dict) -> dict:
        def build():
            raw = self._cli(["curator", "status"], ttl=120.0)
            cur = {"raw": raw}
            for key, pat in (
                ("enabled", r"curator:\s*(\w+)"),
                ("runs", r"runs:\s*(\d+)"),
                ("last_run", r"last run:\s*(.+)"),
                ("interval", r"interval:\s*(.+)"),
                ("consolidate", r"consolidate:\s*(.+)"),
            ):
                m = re.search(pat, raw)
                cur[key] = m.group(1).strip() if m else None

            sessions = max(int(totals.get("sessions") or 0), 1)
            msgs = int(totals.get("messages") or 0)
            tools = int(totals.get("tool_calls") or 0)
            inp = int(totals.get("input_tokens") or 0)
            out = int(totals.get("output_tokens") or 0)
            cost = float(totals.get("cost_usd") or 0)
            cache_read = int(totals.get("cache_read_tokens") or 0)
            cache_write = int(totals.get("cache_write_tokens") or 0)
            billed = inp + cache_write
            cache_ratio = (cache_read / (cache_read + billed) * 100) if (cache_read + billed) else 0.0

            return {
                "curator": cur,
                "efficiency": {
                    "cost_per_session": round(cost / sessions, 3),
                    "messages_per_session": round(msgs / sessions, 1),
                    "tools_per_session": round(tools / sessions, 1),
                    "tokens_per_session": round((inp + out) / sessions),
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                    "cache_hit_pct": round(cache_ratio, 1),
                },
            }

        return self._cached("insights", 60.0, build)

    # -- conversations -----------------------------------------------------

    def conversation(self, session_id: str, limit: int = 120) -> dict:
        rows = self._db(
            "state.db",
            """SELECT role, content, tool_name, timestamp, token_count
               FROM messages
               WHERE session_id = ? AND role IN ('user','assistant','tool')
               ORDER BY id ASC LIMIT ?""",
            (session_id, limit),
        )
        msgs = []
        for r in rows:
            body = r["content"] or ""
            if isinstance(body, bytes):
                body = body.decode("utf-8", "replace")
            msgs.append({
                "role": r["role"],
                "tool": r["tool_name"],
                "text": str(body)[:4000],
                "timestamp": r["timestamp"],
                "tokens": r["token_count"],
            })
        return {"session_id": session_id, "messages": msgs, "count": len(msgs)}


    # -- dreams: observations engine + brief store ------------------------

    def observations(self, data: "HermesData", docs_stats: dict) -> list[dict]:
        """Deterministic findings over real state. No LLM, no tokens, no guessing."""

        def build():
            out: list[dict] = []

            def add(sev, title, detail, action=None):
                out.append({"severity": sev, "title": title,
                            "detail": detail, "action": action})

            totals = data.totals()
            sessions = data.sessions(limit=60)
            skills = data.skills()
            sched = self.schedule()
            ins = self.insights(totals)
            mission = self.mission()

            # 1. Scheduling: the single biggest idle capability.
            if not sched["has_jobs"]:
                add("high", "No scheduled jobs",
                    "The cron ticker is alive but has nothing to run, so nothing "
                    "happens unless you start it by hand.",
                    "hermes cron create")
            if sched["heartbeat_age_s"] is not None and not sched["scheduler_alive"]:
                add("high", "Scheduler heartbeat is stale",
                    f"Last tick was {sched['heartbeat_age_s']}s ago; scheduled work "
                    "will not fire.", "check the gateway is running")

            # 2. Curator: on but idle, or pruning without consolidating.
            cur = ins["curator"]
            if cur.get("enabled") == "ENABLED" and cur.get("runs") == "0":
                add("info", "Curator has never run",
                    f"Enabled on a {cur.get('interval') or '?'} interval but still "
                    "at 0 runs, so no skill has been reviewed yet.",
                    "hermes curator run --dry-run")
            if cur.get("consolidate", "").startswith("off"):
                add("info", "Curator consolidation is off",
                    "It prunes but never merges overlapping skills; the LLM merge "
                    "pass is a separate opt-in.",
                    "hermes config set curator.consolidate true")

            # 3. Stale skills.
            stale = [s for s in skills if s["stale"]]
            if stale:
                oldest = max(stale, key=lambda s: s["age_days"])
                add("info", f"{len(stale)} skills untouched for 14+ days",
                    f"Oldest is '{oldest['name']}' at {oldest['age_days']:.0f} days. "
                    "Stale skills still cost prompt budget.",
                    "review or archive them")

            # 4. Skill library shape.
            cats: dict[str, int] = {}
            for s in skills:
                cats[s["category"]] = cats.get(s["category"], 0) + 1
            if cats:
                biggest = max(cats.items(), key=lambda kv: kv[1])
                if biggest[1] >= 8:
                    add("info", f"'{biggest[0]}' holds {biggest[1]} skills",
                        "The largest category by a wide margin — a sign it may be "
                        "worth splitting or consolidating.")

            # 5. Cost outliers.
            costed = [s for s in sessions if (s.get("cost_usd") or 0) > 0]
            if costed:
                avg = sum(s["cost_usd"] for s in costed) / len(costed)
                worst = max(costed, key=lambda s: s["cost_usd"])
                if worst["cost_usd"] > avg * 2.5 and worst["cost_usd"] > 0.5:
                    add("warn", "One session cost far above average",
                        f"{money_s(worst['cost_usd'])} against a {money_s(avg)} average "
                        f"({worst.get('title') or worst['id']}).",
                        "check for a runaway loop")

            # 6. Cache efficiency.
            hit = ins["efficiency"]["cache_hit_pct"]
            if hit < 50:
                add("warn", f"Cache hit rate is {hit}%",
                    "Low reuse means you are re-paying for context. Mid-conversation "
                    "tool or prompt changes break caching.")
            else:
                add("good", f"Cache hit rate is {hit}%",
                    f"{int(ins['efficiency']['cache_read_tokens']):,} tokens served "
                    "warm instead of billed at full rate.")

            # 7. Failing kanban work.
            if mission["stats"]["failing"]:
                bad = [t for t in mission["tasks"] if t["failures"]]
                add("high", f"{len(bad)} kanban task(s) failing",
                    (bad[0].get("last_failure_error") or "repeated failures")[:160],
                    "inspect the task workspace")

            # 8. Memory budget.
            mem = data.memory_entries()
            chars = sum(m["chars"] for m in mem)
            if chars < 400:
                add("info", "Memory is nearly empty",
                    f"{len(mem)} entries, {chars} chars. Durable facts that apply to "
                    "every session belong here.")

            # 9. Artifacts.
            if not docs_stats.get("count"):
                add("info", "No artifacts saved yet",
                    f"Nothing in {docs_stats.get('root')}. Documents built in chat "
                    "are lost unless they land there.")

            order = {"high": 0, "warn": 1, "good": 2, "info": 3}
            out.sort(key=lambda o: order.get(o["severity"], 9))
            return out

        return self._cached("observations", 30.0, build)

    def dreams(self, dreams_dir: Path) -> list[dict]:
        """Nightly briefs: markdown files, newest first."""
        out = []
        if not dreams_dir.is_dir():
            return out
        for path in sorted(dreams_dir.glob("*.md"), reverse=True):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                st = path.stat()
            except OSError:
                continue
            m = re.search(r"^#\s+(.+)$", text, re.M)
            out.append({
                "file": path.name,
                "title": (m.group(1).strip() if m else path.stem),
                "body": text[:20000],
                "mtime": st.st_mtime,
                "bytes": st.st_size,
            })
        return out


def money_s(v) -> str:
    return "$" + format(float(v or 0), ".2f")


# --------------------------------------------------------------------------
# Artifact store (the one directory this app may write to)
# --------------------------------------------------------------------------

class DocStore:
    """Lists, previews and deletes files under a single artifact directory."""

    def __init__(self, root: Path, allow_delete: bool = True):
        self.root = root
        self.allow_delete = allow_delete
        root.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel: str) -> Path:
        """Resolve a client-supplied relative path, or raise."""
        if not rel:
            raise ValueError("empty path")
        target = (self.root / rel).resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            raise ValueError("path escapes artifact root")
        if target.name in DOC_SKIP_NAMES:
            raise ValueError("blocked filename")
        return target

    @staticmethod
    def _summarise(path: Path, kind: str) -> tuple[str, str]:
        """Return (title, blurb) — a 5-word-ish title and a short description."""
        title = path.stem.replace("-", " ").replace("_", " ").strip()
        blurb = ""
        if kind not in TEXTY:
            return title.title()[:60], blurb
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4000)
        except OSError:
            return title.title()[:60], blurb

        if kind == "markdown":
            m = re.search(r"^#\s+(.+)$", head, re.M)
            if m:
                title = m.group(1).strip()
        elif kind == "html":
            m = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()
            else:
                m = re.search(r"<h1[^>]*>(.*?)</h1>", head, re.S | re.I)
                if m:
                    title = re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip()

        # Blurb: first prose-looking line that is not the title or markup.
        if kind == "html":
            plain = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", head, flags=re.S | re.I)
            plain = re.sub(r"<[^>]+>", "\n", plain)
        else:
            plain = head
        for line in plain.splitlines():
            s = line.strip().lstrip("#*->|` ").strip()
            if len(s) < 25 or s == title:
                continue
            if s.startswith(("{", "[", "<", "---", "//", "/*", "import ", "from ")):
                continue
            blurb = " ".join(s.split()[:14])
            break
        return title[:72], blurb[:140]

    def list(self) -> list[dict]:
        out = []
        root = self.root.resolve()
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(p in DOC_SKIP_DIRS for p in path.relative_to(root).parts[:-1]):
                continue
            if path.name in DOC_SKIP_NAMES or path.name.startswith("."):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            kind = doc_type(path.suffix)
            title, blurb = self._summarise(path, kind)
            out.append({
                "path": str(path.relative_to(root)),
                "name": path.name,
                "title": title or path.stem,
                "blurb": blurb,
                "kind": kind,
                "ext": path.suffix.lower().lstrip("."),
                "bytes": st.st_size,
                "mtime": st.st_mtime,
                "previewable": kind in TEXTY or kind == "image",
            })
        out.sort(key=lambda d: d["mtime"], reverse=True)
        return out

    def stats(self) -> dict:
        items = self.list()
        by_kind: dict[str, int] = {}
        for i in items:
            by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
        return {
            "count": len(items),
            "bytes": sum(i["bytes"] for i in items),
            "by_kind": by_kind,
            "root": str(self.root),
        }

    def read(self, rel: str, limit: int = 400_000) -> tuple[bytes, str]:
        target = self.resolve(rel)
        if not target.is_file():
            raise FileNotFoundError(rel)
        ctype = MIME.get(target.suffix.lower())
        if not ctype:
            kind = doc_type(target.suffix)
            ctype = "text/plain; charset=utf-8" if kind in TEXTY else "application/octet-stream"
        return target.read_bytes()[:limit], ctype

    def delete(self, rel: str) -> dict:
        if not self.allow_delete:
            raise PermissionError("deletion disabled")
        target = self.resolve(rel)
        if not target.is_file():
            raise FileNotFoundError(rel)
        target.unlink()
        return {"deleted": rel}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "Mission/1.3"
    data: HermesData = None  # type: ignore[assignment]
    docs: DocStore = None  # type: ignore[assignment]
    extras: HermesExtras = None  # type: ignore[assignment]
    dreams_dir: Path = None  # type: ignore[assignment]
    media: "MediaStack" = None  # type: ignore[assignment]
    auth: "Auth" = None  # type: ignore[assignment]

    # Reachable without a session: the login page, its assets, and the login API.
    PUBLIC_PATHS = {"/login.html", "/api/login", "/favicon.svg", "/favicon.ico",
                    "/login.css", "/login.js"}

    def _https(self) -> bool:
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _set_cookie(self, value: str, max_age: int):
        flags = f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        if self._https():
            flags += "; Secure"
        self.send_header("Set-Cookie", flags)

    def _authed(self) -> bool:
        if self.auth is None or not self.auth.configured:
            return False
        return self.auth.validate(self.auth.cookie_from_header(self.headers.get("Cookie")))

    def _gate(self, path: str) -> bool:
        """True if the request may proceed. Fails CLOSED by design."""
        if path in self.PUBLIC_PATHS:
            return True
        if self.auth is None or not self.auth.configured:
            # Publicly reachable with no password set: serve nothing but the notice.
            self._json({"error": "auth_not_configured",
                        "detail": "Set OPERATOR_AUTH_EMAIL and OPERATOR_PASSWORD_HASH "
                                  "in $HERMES_HOME/.env (python3 auth.py hash), "
                                  "then restart operator-os.service."}, 503)
            return False
        if self._authed():
            return True
        if path.startswith("/api/"):
            self._json({"error": "unauthorized"}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/login.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
        return False

    def log_message(self, fmt, *args):  # quieter console
        if os.environ.get("OPERATOR_VERBOSE"):
            super().log_message(fmt, *args)

    # -- helpers ----------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    # -- routing ----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if not self._gate(parsed.path):
            return
        if parsed.path != "/api/docs":
            return self._json({"error": "not_found"}, 404)
        rel = parse_qs(parsed.query).get("path", [""])[0]
        try:
            return self._json(self.docs.delete(rel))
        except PermissionError as exc:
            return self._json({"error": "forbidden", "detail": str(exc)}, 403)
        except FileNotFoundError:
            return self._json({"error": "not_found", "path": rel}, 404)
        except ValueError as exc:
            return self._json({"error": "bad_path", "detail": str(exc)}, 400)

    def do_POST(self):
        path = urlparse(self.path).path

        # The only write action in the app. Deliberately narrow: it takes a list
        # of torrent hashes and nothing else, and media_stack re-verifies every
        # one against removable() before touching a client.
        if path == "/api/media/cleanup":
            if not self._gate(path):
                return
            if self.media is None:
                return self._json({"error": "media_disabled"}, 503)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 65536)).decode() or "{}")
            except Exception:
                return self._json({"error": "bad_request"}, 400)
            hashes = body.get("hashes")
            if not isinstance(hashes, list) or not hashes:
                return self._json({"error": "no_hashes"}, 400)
            if len(hashes) > 50:
                return self._json({"error": "too_many"}, 400)
            try:
                res = self.media.remove(hashes)
            except Exception as exc:
                return self._json({"error": exc.__class__.__name__,
                                   "detail": str(exc)}, 502)
            print(f"[cleanup] removed={res['removed']} failed={res['failed']} "
                  f"freed={res['freed_gb']}GB")
            return self._json(res)

        if path != "/api/login":
            return self._json({"error": "not_found"}, 404)
        if self.auth is None or not self.auth.configured:
            return self._json({"error": "auth_not_configured"}, 503)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 8192)).decode() or "{}")
        except Exception:
            return self._json({"error": "bad_request"}, 400)
        if not self.auth.verify(body.get("email", ""), body.get("password", "")):
            time.sleep(0.6)  # blunt the brute-force rate
            return self._json({"error": "invalid_credentials"}, 401)
        token = self.auth.issue()
        payload = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._set_cookie(token, TTL)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/logout":
            self.send_response(302)
            self.send_header("Location", "/login.html")
            self._set_cookie("", 0)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._gate(path):
            return
        try:
            if path.startswith("/api/"):
                return self._api(path)
            return self._static(path)
        except BrokenPipeError:
            return
        except Exception as exc:  # never 500 silently
            self._json({"error": exc.__class__.__name__, "detail": str(exc)}, 500)

    def _api(self, path: str):
        d = self.data
        if path == "/api/status":
            s = d.status()
            s["docs"] = self.docs.stats()
            return self._json(s)
        if path == "/api/graph":
            return self._json(d.graph())
        if path == "/api/media/removable":
            if self.media is None:
                return self._json({"error": "media_disabled"}, 503)
            try:
                return self._json(self.media.removable())
            except Exception as exc:
                return self._json({"error": exc.__class__.__name__,
                                   "detail": str(exc)}, 502)
        if path == "/api/media":
            if self.media is None:
                return self._json({"error": "media_disabled"}, 503)
            force = parse_qs(urlparse(self.path).query).get("force", ["0"])[0] == "1"
            try:
                return self._json(self.media.snapshot(force=force))
            except Exception as exc:
                return self._json({"error": exc.__class__.__name__,
                                   "detail": str(exc)}, 502)
        if path == "/api/skills":
            skills = d.skills()
            return self._json({"skills": skills, "count": len(skills)})
        if path == "/api/memory":
            entries = d.memory_entries()
            return self._json({"entries": entries, "count": len(entries)})
        if path == "/api/activity":
            return self._json(
                {"sessions": d.sessions(limit=40), "spend": d.spend_by_day(14)}
            )
        if path == "/api/dreams":
            return self._json({
                "observations": self.extras.observations(d, self.docs.stats()),
                "briefs": self.extras.dreams(self.dreams_dir),
                "dreams_dir": str(self.dreams_dir),
            })
        if path == "/api/mission":
            return self._json(self.extras.mission())
        if path == "/api/schedule":
            return self._json(self.extras.schedule())
        if path == "/api/stack":
            return self._json(self.extras.stack())
        if path == "/api/insights":
            return self._json(self.extras.insights(d.totals()))
        if path == "/api/conversation":
            sid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not sid:
                return self._json({"error": "missing id"}, 400)
            return self._json(self.extras.conversation(sid))
        if path == "/api/docs":
            items = self.docs.list()
            return self._json({"docs": items, "stats": self.docs.stats()})
        if path == "/api/docs/raw":
            rel = parse_qs(urlparse(self.path).query).get("path", [""])[0]
            try:
                body, ctype = self.docs.read(rel)
            except FileNotFoundError:
                return self._json({"error": "not_found", "path": rel}, 404)
            except ValueError as exc:
                return self._json({"error": "bad_path", "detail": str(exc)}, 400)
            # Artifacts are untrusted content: never let one run as same-origin.
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return None
        return self._json({"error": "not_found", "path": path}, 404)

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        # SPA-style: unknown non-file routes fall back to index.html
        target = (WEB_ROOT / rel).resolve()
        if not str(target).startswith(str(WEB_ROOT.resolve())):
            return self._json({"error": "forbidden"}, 403)
        if target.name in BLOCKED_NAMES:
            return self._json({"error": "forbidden"}, 403)
        if not target.is_file():
            target = WEB_ROOT / "index.html"
            if not target.is_file():
                return self._json({"error": "web/index.html missing"}, 500)
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        # .css/.js must revalidate: a 60s cache meant edits appeared to "not land"
        # for a client with the old stylesheet still in memory. index.html also
        # carries ?v=<hash> on these assets, so a changed file gets a new URL.
        if target.suffix == ".html":
            cache = "no-store"
        elif target.suffix in (".css", ".js"):
            cache = "no-cache"
        else:
            cache = "max-age=300"
        self._send(200, target.read_bytes(), ctype, cache)


def main():
    ap = argparse.ArgumentParser(description="Mission dashboard")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")),
    )
    ap.add_argument(
        "--docs-dir",
        default=os.environ.get("OPERATOR_DOCS", str(Path.home() / "Documents" / "hermes")),
        help="artifact directory shown in the Documents view",
    )
    ap.add_argument(
        "--dreams-dir",
        default=os.environ.get("OPERATOR_DREAMS", ""),
        help="nightly brief directory (default <hermes-home>/operator/dreams)",
    )
    ap.add_argument(
        "--hermes-bin",
        default=os.environ.get("OPERATOR_HERMES_BIN", "hermes"),
        help="hermes CLI used for cron/curator/config reads",
    )
    ap.add_argument(
        "--no-delete",
        action="store_true",
        help="serve the Documents view read-only (disables DELETE)",
    )
    args = ap.parse_args()

    home = Path(args.hermes_home).expanduser().resolve()
    if not home.is_dir():
        raise SystemExit(f"Hermes home not found: {home}")

    docs_root = Path(args.docs_dir).expanduser().resolve()
    Handler.data = HermesData(home)
    Handler.docs = DocStore(docs_root, allow_delete=not args.no_delete)
    Handler.extras = HermesExtras(home, hermes_bin=args.hermes_bin)
    dreams_dir = Path(args.dreams_dir).expanduser().resolve() if args.dreams_dir \
        else home / "operator" / "dreams"
    dreams_dir.mkdir(parents=True, exist_ok=True)
    Handler.dreams_dir = dreams_dir
    try:
        from auth import Auth
        Handler.auth = Auth(home)
        if Handler.auth.configured:
            print(f"Auth         →  enabled for {Handler.auth.email}"
                  + ("  (ephemeral session secret)" if Handler.auth.ephemeral_secret else ""))
        else:
            print("Auth         →  NOT CONFIGURED — all routes denied (503). "
                  "Run: python3 auth.py hash")
    except Exception as exc:
        print(f"auth unavailable, refusing to serve: {exc.__class__.__name__}: {exc}")
        raise
    try:
        from media_stack import MediaStack
        Handler.media = MediaStack(home)
    except Exception as exc:  # panel is optional, never block boot
        print(f"media panel disabled: {exc.__class__.__name__}: {exc}")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mission      →  http://{args.host}:{args.port}")
    print(f"Hermes home  →  {home}  (read-only)")
    print(f"Dreams       →  {dreams_dir}")
    print(f"Artifacts    →  {docs_root}  ({'read-write' if not args.no_delete else 'read-only'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        httpd.server_close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
dream.py — generate a nightly brief for Operator OS.

Two modes:

  --local   (default) Write a brief straight from the deterministic observations
            engine. Costs nothing, needs no model, always works.

  --agent   Ask Hermes itself to write the brief, feeding it the observations as
            grounding. Costs tokens. Requires the `hermes` CLI.

The brief lands in the dreams directory as YYYY-MM-DD.md and shows up in the
dashboard's Dreams view on next refresh.

Nightly, at 07:00:
  hermes cron create --name dream \\
    --schedule "0 7 * * *" \\
    --command "python3 /home/gladwelln/code/projects/operator-os/scripts/dream.py --agent"

or with plain cron:
  0 7 * * * python3 /path/to/operator-os/scripts/dream.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:8081"

PROMPT = """You are writing the nightly "dream" brief for my Hermes agent setup.

Below are deterministic observations computed from my real Hermes state. Use ONLY
these facts — do not invent metrics, do not pad, do not flatter.

Write markdown, no more than 250 words:
# Dream brief — {date}
Then: 2-4 bullets of what actually matters, each naming the concrete next action.
Then a "## Leave alone" line listing what is already healthy and needs no work.

Observations:
{observations}
"""


def fetch(base: str) -> dict:
    try:
        with urllib.request.urlopen(f"{base}/api/dreams", timeout=15) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.exit(f"could not reach Operator OS at {base}: {exc}")


def render_local(obs: list[dict], date: str) -> str:
    order = {"high": "Act now", "warn": "Watch", "info": "Ideas", "good": "Leave alone"}
    lines = [f"# Dream brief — {date}", "",
             f"_Generated locally from {len(obs)} observations. No model involved._", ""]
    for sev, heading in order.items():
        group = [o for o in obs if o["severity"] == sev]
        if not group:
            continue
        lines.append(f"## {heading}")
        for o in group:
            lines.append(f"- **{o['title']}** — {o['detail']}")
            if o.get("action"):
                lines.append(f"  - → `{o['action']}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_agent(obs: list[dict], date: str, hermes_bin: str) -> str:
    block = "\n".join(
        f"- [{o['severity']}] {o['title']}: {o['detail']}"
        + (f" (suggested: {o['action']})" if o.get("action") else "")
        for o in obs
    )
    prompt = PROMPT.format(date=date, observations=block)
    try:
        proc = subprocess.run(
            [hermes_bin, "chat", "-q", prompt],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"agent mode failed ({exc}); falling back to local", file=sys.stderr)
        return render_local(obs, date)
    out = (proc.stdout or "").strip()
    if not out:
        print("agent returned nothing; falling back to local", file=sys.stderr)
        return render_local(obs, date)
    if not out.lstrip().startswith("#"):
        out = f"# Dream brief — {date}\n\n{out}"
    return out + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="generate a nightly dream brief")
    ap.add_argument("--base", default=DEFAULT_BASE, help="Operator OS base URL")
    ap.add_argument("--agent", action="store_true", help="use the Hermes CLI (costs tokens)")
    ap.add_argument("--local", action="store_true", help="force local rendering")
    ap.add_argument("--hermes-bin", default="hermes")
    ap.add_argument("--out", default="", help="override the dreams directory")
    args = ap.parse_args()

    payload = fetch(args.base)
    obs = payload.get("observations") or []
    if not obs:
        print("no observations; nothing to write")
        return

    date = time.strftime("%Y-%m-%d")
    body = (render_local(obs, date) if args.local or not args.agent
            else render_agent(obs, date, args.hermes_bin))

    out_dir = Path(args.out) if args.out else Path(payload.get("dreams_dir", "."))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{date}.md"
    target.write_text(body, encoding="utf-8")
    print(f"wrote {target} ({len(body)} bytes, {len(obs)} observations)")


if __name__ == "__main__":
    main()

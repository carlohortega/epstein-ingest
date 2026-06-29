#!/usr/bin/env python3
"""Durable media-track runner — M2 transcribe → M3 frames/vision/safety → M4 caption → M5 chunk, per dataset.

Built for the multi-hour bulk (DS-08/DS-09). Resilience comes from the stages themselves: every stage skips
already-done assets and only retries the errored ones, so re-running is idempotent and **never re-spends on
completed work**. This wrapper adds:
  * **multi-pass mop-up** — runs the chain ``--passes`` times (default 2); pass 2+ skips everything done and
    retries only assets that errored (e.g. a transient network drop during pass 1). Stops early once a pass
    leaves zero errored assets.
  * **credential loading** — sources ``~/.env.local`` into the environment (the media Azure keys).
  * **per-dataset cost rollup** from the run manifests.

Safe to run under a Windows Scheduled Task (survives reboots) or re-run by hand after any interruption — it
picks up exactly where it left off. Writes ONLY local disk; Postgres/Blob Storage are untouched (that's Stage 8).

    .venv/bin/python run_media.py DataSet-08 DataSet-09 [--passes 2] [--workers 6] [--data <root>]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = "/mnt/c/Projects/Data/epstein"
STAGES = [("src.media_stage2", "transcribe"), ("src.media_stage3", "frames"),
          ("src.media_stage4", "caption"), ("src.media_stage5", "chunk")]


def _load_env(path: str) -> int:
    """Load KEY=VALUE lines from an env file into os.environ (strip quotes + trailing CR)."""
    if not os.path.isfile(path):
        return 0
    n = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip().lstrip("﻿")
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'").rstrip("\r")
            n += 1
    return n


def _errored(data: str, ds: str) -> dict:
    out = {}
    for name in ("transcribe", "frames", "caption"):
        m = os.path.join(data, ds, f"media-{name}-run-manifest.json")
        try:
            out[name] = int(json.load(open(m)).get("assets_errored", 0))
        except Exception:
            out[name] = 0
    return out


def _cost(data: str, ds: str) -> None:
    def L(name):
        try:
            return json.load(open(os.path.join(data, ds, f"media-{name}-run-manifest.json")))
        except Exception:
            return {}
    t, f, c = L("transcribe"), L("frames"), L("caption")
    tc = (t.get("usage") or {}).get("estimated_cost_usd", 0)
    cc = (c.get("usage") or {}).get("estimated_cost_usd", 0)
    kf, kfe = f.get("keyframes", 0), f.get("keyframes_embedded", 0)
    vis, cs = kfe * 0.10 / 1000, kf * 1.0 / 1000   # rough Azure list: AI Vision $0.10/1k, CS $1/1k
    usd = chr(36)
    print(f"  cost: transcribe {usd}{tc:.2f} + vision/safety ~{usd}{vis + cs:.2f} + caption {usd}{cc:.3f} "
          f"= ~{usd}{tc + cc + vis + cs:.2f}  (safety block={f.get('safety_block', 0)})")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("datasets", nargs="+", help="dataset folder names, e.g. DataSet-08 DataSet-09")
    p.add_argument("--passes", type=int, default=2, help="max chain passes per dataset (mop-up; default 2)")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--data", default=DEFAULT_DATA, help=f"corpus root (default {DEFAULT_DATA})")
    p.add_argument("--case-key", default="epstein")
    args = p.parse_args(argv)

    loaded = _load_env(os.path.expanduser("~/.env.local"))
    print(f"loaded {loaded} env vars from ~/.env.local")
    py = sys.executable

    for ds in args.datasets:
        root = os.path.join(args.data, ds)
        if not os.path.isdir(root):
            print(f"!! {ds}: not found at {root} — skipping")
            continue
        print(f"\n========== {ds} ==========")
        for p_i in range(1, args.passes + 1):
            print(f"--- {ds} pass {p_i}/{args.passes}  {time.strftime('%H:%M:%S')} ---")
            for mod, label in STAGES:
                cmd = [py, "-m", mod, root, "-j", str(args.workers)]
                if mod.endswith("stage5"):
                    cmd = [py, "-m", mod, root, "--case-key", args.case_key]
                rc = subprocess.run(cmd, cwd=REPO).returncode
                if rc != 0:
                    print(f"  !! {label} returned {rc} (continuing; resumable)")
            err = _errored(args.data, ds)
            print(f"  errored after pass {p_i}: {err}")
            if sum(err.values()) == 0:
                print(f"  {ds}: clean after pass {p_i}")
                break
        _cost(args.data, ds)
        print(f"========== {ds} DONE  {time.strftime('%H:%M:%S')} ==========")
    print("\nALL DATASETS DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

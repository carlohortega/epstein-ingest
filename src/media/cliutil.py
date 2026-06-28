"""Shared CLI helpers for the media stages: expose every Config dataclass field as a ``--flag`` and
rebuild the (frozen) Config from parsed args. Mirrors the stage1/stage2 CLI convention (underscores →
dashes; booleans get a paired ``--flag`` / ``--no-flag`` form)."""

from __future__ import annotations

import argparse
import dataclasses
import os
from datetime import datetime, timezone


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 2)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_config_args(group: argparse._ArgumentGroup, config_cls) -> None:
    for f in dataclasses.fields(config_cls):
        flag = "--" + f.name.replace("_", "-")
        if isinstance(f.default, bool):
            group.add_argument(flag, dest=f.name, action="store_true", default=None,
                               help=f"(default: {f.default})")
            group.add_argument("--no-" + f.name.replace("_", "-"), dest=f.name, action="store_false",
                               help=argparse.SUPPRESS)
        else:
            # Optional[str] fields (e.g. language) declare default None → fall back to str typing.
            argtype = type(f.default) if f.default is not None else str
            group.add_argument(flag, dest=f.name, type=argtype, default=None,
                               help=f"(default: {f.default})")


def config_from_args(args: argparse.Namespace, config_cls):
    overrides = {f.name: getattr(args, f.name)
                 for f in dataclasses.fields(config_cls) if getattr(args, f.name, None) is not None}
    return dataclasses.replace(config_cls(), **overrides)

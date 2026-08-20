#!/usr/bin/env python3
"""Install the curate-genome-annotations skill into supported client homes.

Every supported client discovers a SKILL.md with the same name/description
frontmatter, so the same folder can be copied or symlinked into several skills
directories. The script prints a plan by default, detects which clients are
present when --client is omitted, and writes only with --install.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


SKILL_NAME = "curate-genome-annotations"


def _codex_root() -> Path:
    base = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return base / "skills"


CLIENTS: dict[str, dict[str, object]] = {
    "codex": {"label": "Codex", "root": _codex_root},
    "claude-code": {"label": "Claude Code", "root": lambda: Path.home() / ".claude" / "skills"},
    "trae-work": {"label": "TRAE Work", "root": lambda: Path.home() / ".trae-cn" / "skills"},
    "workbuddy": {"label": "WorkBuddy / CodeBuddy", "root": lambda: Path.home() / ".codebuddy" / "skills"},
}


def detect_present() -> set[str]:
    """Best-effort detection of which supported clients are installed."""
    home = Path.home()
    present: set[str] = set()
    if os.environ.get("CODEX_HOME") or (home / ".codex").exists():
        present.add("codex")
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") or (home / ".claude").exists():
        present.add("claude-code")
    if (home / ".trae-cn").exists():
        present.add("trae-work")
    if (home / ".codebuddy").exists() or (home / ".workbuddy").exists():
        present.add("workbuddy")
    return present


def skill_source() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_root(client: str, dest: str | None) -> Path:
    if dest:
        return Path(dest).expanduser().resolve()
    root = CLIENTS[client]["root"]
    assert callable(root)
    return root().expanduser().resolve()


def plan_line(client: str, target: Path, symlink: bool) -> str:
    kind = "symlink" if symlink else "copy"
    state = "EXISTS" if (target.exists() or target.is_symlink()) else kind
    return f"{CLIENTS[client]['label']}: {state} -> {target}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client",
        choices=["all", *sorted(CLIENTS)],
        action="append",
        help="Client to target; repeat for several, use 'all', or omit to auto-detect",
    )
    parser.add_argument("--dest", help="Override the skills root (requires one specific --client)")
    parser.add_argument("--install", action="store_true", help="Write the install (default: print a plan)")
    parser.add_argument("--check", action="store_true", help="Print the plan without writing (overrides --install)")
    parser.add_argument("--detect", action="store_true", help="Print detected clients and exit")
    parser.add_argument("--symlink", action="store_true", help="Create a symlink instead of copying")
    parser.add_argument("--force", action="store_true", help="Replace an existing install")
    args = parser.parse_args(argv)

    if args.detect:
        detected = sorted(detect_present())
        print(", ".join(detected) if detected else "(none)")
        return 0

    if args.dest and (not args.client or len(args.client) != 1 or args.client[0] == "all"):
        print("--dest requires exactly one specific --client", file=sys.stderr)
        return 2

    if args.client:
        if "all" in args.client and len(args.client) > 1:
            print("--client all cannot be combined with specific clients", file=sys.stderr)
            return 2
        clients = list(CLIENTS) if "all" in args.client else args.client
    else:
        clients = sorted(detect_present())

    if not clients:
        print(
            "No clients detected. Pass --client all to target every supported client, "
            "or --client <id> for a specific client.",
            file=sys.stderr,
        )
        return 2

    src = skill_source()
    if not (src / "SKILL.md").is_file():
        print(f"SKILL.md not found in skill source {src}", file=sys.stderr)
        return 2

    install = args.install and not args.check
    errors: list[str] = []
    for client in clients:
        target = resolve_root(client, args.dest) / SKILL_NAME
        if not install:
            print(plan_line(client, target, args.symlink))
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if not args.force:
                errors.append(f"{client}: already installed at {target}; pass --force to replace")
                continue
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
        if args.symlink:
            target.symlink_to(src)
        else:
            shutil.copytree(src, target)
        print(f"installed {CLIENTS[client]['label']}: {target}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

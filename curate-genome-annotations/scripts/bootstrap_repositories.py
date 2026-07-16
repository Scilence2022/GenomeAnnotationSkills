#!/usr/bin/env python3
"""Clone and install CodeXomics and Deep Gene Research without mutating checkouts."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


CODEXOMICS_URL = "https://github.com/Scilence2022/CodeXomics.git"
DGR_URL = "https://github.com/Scilence2022/DeepGeneResearch.git"


def run(command: list[str], *, cwd: Path | None = None, dry_run: bool = False) -> None:
    rendered = " ".join(command)
    print(f"[{cwd or Path.cwd()}] $ {rendered}")
    if dry_run:
        return
    subprocess.run(command, cwd=cwd, check=True)


def package_name(directory: Path) -> str | None:
    package_file = directory / "package.json"
    if not package_file.is_file():
        return None
    try:
        return str(json.loads(package_file.read_text(encoding="utf-8")).get("name", ""))
    except (OSError, json.JSONDecodeError):
        return None


def ensure_checkout(
    directory: Path,
    url: str,
    expected_names: set[str],
    *,
    dry_run: bool,
) -> str:
    current_name = package_name(directory)
    if current_name in expected_names and (directory / ".git").exists():
        print(f"SKIP checkout: existing repository {directory} ({current_name})")
        return current_name
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError(
            f"Refusing to clone over non-empty directory {directory}; provide the correct existing repository path"
        )
    if shutil.which("git") is None:
        raise RuntimeError("git is required but was not found on PATH")
    directory.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", url, str(directory)], dry_run=dry_run)
    return next(iter(expected_names)) if dry_run else package_name(directory) or ""


def pnpm_command() -> list[str]:
    if shutil.which("pnpm"):
        return ["pnpm"]
    if shutil.which("corepack"):
        return ["corepack", "pnpm"]
    raise RuntimeError("pnpm is required for DGR; install it or enable it through Corepack")


def install_dependencies(codexomics: Path, dgr: Path, *, dry_run: bool) -> None:
    if shutil.which("node") is None or shutil.which("npm") is None:
        raise RuntimeError("Node.js and npm are required but were not found on PATH")
    code_command = ["npm", "ci"] if (codexomics / "package-lock.json").is_file() else ["npm", "install"]
    run(code_command, cwd=codexomics, dry_run=dry_run)
    dgr_command = pnpm_command() + ["install"]
    if (dgr / "pnpm-lock.yaml").is_file():
        dgr_command.append("--frozen-lockfile")
    run(dgr_command, cwd=dgr, dry_run=dry_run)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone missing CodeXomics/DGR repositories and install dependencies"
    )
    parser.add_argument("--codexomics-dir", type=Path, required=True)
    parser.add_argument("--dgr-dir", type=Path, required=True)
    parser.add_argument("--codexomics-url", default=CODEXOMICS_URL)
    parser.add_argument("--dgr-url", default=DGR_URL)
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codexomics = args.codexomics_dir.expanduser().resolve()
    dgr = args.dgr_dir.expanduser().resolve()
    if codexomics == dgr:
        print("ERROR: CodeXomics and DGR directories must be different", file=sys.stderr)
        return 2
    try:
        ensure_checkout(
            codexomics,
            args.codexomics_url,
            {"codexomics"},
            dry_run=args.dry_run,
        )
        ensure_checkout(
            dgr,
            args.dgr_url,
            {"deep-gene-research", "deep-gene-research-mcp"},
            dry_run=args.dry_run,
        )
        if not args.skip_install:
            install_dependencies(codexomics, dgr, dry_run=args.dry_run)
        print("Bootstrap complete. Existing checkouts were not pulled, switched, or overwritten.")
        return 0
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

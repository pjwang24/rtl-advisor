#!/usr/bin/env python3
"""Run SymbiYosys and identity queries in the pinned MVP tool image."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


IMAGE = os.environ.get(
    "RTL_ADVISOR_FORMAL_IMAGE",
    "sha256:"
    "5ed5c421cc617bd82f5440ca0201074d8b89039dd1079e747fe5d3350539c76e",
)
ROOT = Path(__file__).resolve().parents[1]


def _docker(*arguments: str) -> int:
    completed = subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            *arguments,
        ),
        check=False,
    )
    return completed.returncode


def _identity(tool: str) -> int:
    commands = {
        "sby": ("sby", "--version"),
        "yosys": ("yosys", "-V"),
        "make": ("make", "--version"),
        "abc": ("yosys-abc", "-c", "version"),
    }
    try:
        command = commands[tool]
    except KeyError:
        print(f"unknown pinned formal tool: {tool}", file=sys.stderr)
        return 2
    return _docker(IMAGE, *command)


def _translate(argument: str) -> str:
    path = Path(argument)
    if not path.is_absolute():
        return argument
    try:
        relative = path.resolve().relative_to(ROOT)
    except ValueError:
        return argument
    return f"/workspace/{relative.as_posix()}"


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--rtl-advisor-tool-version":
        return _identity(argv[1])
    if argv == ["--version"]:
        return _identity("sby")

    output_parent: Path | None = None
    if "-d" in argv:
        index = argv.index("-d")
        if index + 1 < len(argv):
            output_parent = Path(argv[index + 1]).parent
    docker_arguments = [
        "-v",
        f"{ROOT}:/workspace",
        "-w",
        "/workspace",
    ]
    if output_parent is not None:
        docker_arguments.extend(
            (
                "-v",
                f"{output_parent.resolve()}:{output_parent}",
            )
        )
    docker_arguments.extend((IMAGE, "sby"))
    docker_arguments.extend(_translate(argument) for argument in argv)
    return _docker(*docker_arguments)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

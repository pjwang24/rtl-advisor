from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ToolExecutionError(RuntimeError):
    """Raised when an external tool cannot be executed successfully."""


class DownloadError(RuntimeError):
    """Raised when a pinned project asset cannot be downloaded or verified."""


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run_command(
    command: tuple[str, ...] | list[str],
    *,
    timeout_seconds: int,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> CommandResult:
    argv = tuple(command)
    if not argv:
        raise ValueError("command must not be empty")

    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            input=input_text,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise ToolExecutionError(f"command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(
            f"command timed out after {timeout_seconds}s: {' '.join(argv)}"
        ) from exc
    except OSError as exc:
        raise ToolExecutionError(f"could not execute {argv[0]}: {exc}") from exc

    return CommandResult(
        command=argv,
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def first_output_line(result: CommandResult) -> str | None:
    output = result.stdout or result.stderr
    return output.splitlines()[0].strip() if output else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(url: str, destination: Path, expected_sha256: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "rtl-advisor/0.1"})

    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            with urlopen(request, timeout=120) as response:
                while chunk := response.read(1024 * 1024):
                    temporary.write(chunk)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        if "temporary_path" in locals():
            temporary_path.unlink(missing_ok=True)
        raise DownloadError(f"download failed for {url}: {exc}") from exc

    actual_sha256 = sha256_file(temporary_path)
    if actual_sha256 != expected_sha256:
        temporary_path.unlink(missing_ok=True)
        raise DownloadError(
            f"checksum mismatch for {url}: expected {expected_sha256}, "
            f"got {actual_sha256}"
        )

    temporary_path.replace(destination)
    return actual_sha256


def download_text(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "rtl-advisor/0.1"})
    try:
        with urlopen(request, timeout=30) as response:
            content = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise DownloadError(f"download failed for {url}: {exc}") from exc

    destination.write_bytes(content)

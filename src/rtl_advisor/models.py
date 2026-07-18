from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    version: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class SetupReport:
    project_root: str
    config_path: str
    checks: tuple[CheckResult, ...]
    environment_file: str

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload

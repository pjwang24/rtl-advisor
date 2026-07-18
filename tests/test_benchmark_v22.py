from __future__ import annotations

from pathlib import Path

import pytest

from rtl_advisor.benchmark_v22 import BenchmarkV22Error, create_benchmark_lock_v22
from rtl_advisor.config import load_config


def test_v22_failed_calibration_forbids_blind_lock() -> None:
    lock = Path("artifacts/benchmarks/v22/benchmark-lock.json")
    assert not lock.exists()
    with pytest.raises(BenchmarkV22Error, match="blind lock is forbidden"):
        create_benchmark_lock_v22(load_config("rtl-advisor.toml"))
    assert not lock.exists()

from __future__ import annotations

import pytest

from rtl_advisor.advisor_v22 import AdvisorV22Error, load_model_bundle_v22
from rtl_advisor.config import load_config


def test_v22_diagnostic_policy_cannot_load_as_live_advisor() -> None:
    with pytest.raises(AdvisorV22Error, match="diagnostic-only"):
        load_model_bundle_v22(load_config("rtl-advisor.toml"))

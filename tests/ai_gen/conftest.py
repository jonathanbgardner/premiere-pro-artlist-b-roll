from __future__ import annotations

import pytest

from aigen_helpers import write_shot_list, write_test_config


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    write_shot_list(root)
    return root


@pytest.fixture
def cfg(tmp_path):
    from pipeline.ai_gen.config import load_config
    return load_config(write_test_config(tmp_path / "test.toml"))

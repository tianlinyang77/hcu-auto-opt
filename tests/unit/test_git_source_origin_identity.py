# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import pytest

from hcuopt.adapters.git_source import GitSourceManager

EXPECTED = "git@github.com:HYGON-AI/sglang-das.git"


@pytest.mark.parametrize(
    "actual",
    [
        "https://github.com/HYGON-AI/sglang-das.git",
        "https://github.com/HYGON-AI/sglang-das",
        "https://github.com/HYGON-AI/sglang-das.git/",
        EXPECTED,
    ],
)
def test_documented_github_clone_transports_are_equivalent(actual):
    assert GitSourceManager._same_repository(actual, EXPECTED)


@pytest.mark.parametrize(
    "actual",
    [
        "https://github.com/another-owner/sglang-das.git",
        "https://github.com/HYGON-AI/another-repo.git",
        "https://github.com.evil/HYGON-AI/sglang-das.git",
        "https://github.com:444/HYGON-AI/sglang-das.git",
        "https://user@github.com/HYGON-AI/sglang-das.git",
        "https://github.com/HYGON-AI/sglang-das.git?ref=other",
        "https://github.com/HYGON-AI/../sglang-das.git",
        "git@another-host:HYGON-AI/sglang-das.git",
    ],
)
def test_origin_identity_never_discards_host_namespace_or_extra_url_fields(actual):
    assert not GitSourceManager._same_repository(actual, EXPECTED)

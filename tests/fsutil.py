"""Filesystem helpers for tests that must also run on Windows."""

import os

import pytest


def symlink_or_skip(target, link) -> None:
    """Create a symlink, or skip the test where the OS does not allow it (Windows without
    Developer Mode or administrator rights)."""
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks not available here: {exc}")

# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Explicit BW20 host runner; never provision SSH keys or a Docker socket mount."""

from __future__ import annotations

import getpass
import os
import platform
import shutil
import stat
from pathlib import Path
from uuid import UUID

from hcuopt.adapters.execution import LocalCommandRunner
from hcuopt.deployment.bw20_stage0_runtime import ROOT


class BW20LocalCommandRunner(LocalCommandRunner):
    # Endpoint identity is shared with the existing BW20 guards. Commands run on
    # that host itself, not through a second SSH connection.
    host, user, port = "10.17.1.20", "github", 22
    identity_file = None

    def __init__(self):
        self._check_host()

    @staticmethod
    def _check_host():
        if (
            platform.system() != "Linux"
            or platform.node() != "github-bw20"
            or getpass.getuser() != "github"
            or os.geteuid() == 0
        ):
            raise ValueError("local runner requires the non-root BW20 github controller")

    def wrapped_argv(self, argv):
        self._check_host()
        if not argv or any(not isinstance(item, str) or not item or "\0" in item for item in argv):
            raise ValueError("command requires non-empty argv entries")
        return tuple(argv)

    def run(self, argv, timeout=30.0):
        return super().run(self.wrapped_argv(argv), timeout=timeout)

    def popen(self, argv, *, stdout, stderr):
        return super().popen(self.wrapped_argv(argv), stdout=stdout, stderr=stderr)

    def copy_controller_archive(self, source, destination):
        """Copy only a fresh staging archive. The original unpack verifies its pin."""
        self._check_host()
        source, destination = Path(source), Path(destination)
        if (
            destination.name != "controller.tar"
            or str(destination.parent.parent) != ROOT
            or str(UUID(destination.parent.name)) != destination.parent.name
            or destination.parent.resolve(strict=True) != destination.parent
        ):
            raise ValueError("invalid local controller destination")
        before = source.lstat()
        if (
            source.resolve(strict=True) != source
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > 70 * 1024**2
        ):
            raise ValueError("invalid local controller archive")
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as reader:
            actual = os.fstat(reader.fileno())
            if (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("controller archive replaced during open")
            # Exclusive creation: never replace an existing attempt or symlink.
            with destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            after = os.fstat(reader.fileno())
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise ValueError("controller archive changed during copy")

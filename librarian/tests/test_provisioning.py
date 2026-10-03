"""Tool resolution: env override → PATH, with clear infra-failure errors."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from librarian.analyze import ToolError, resolve_all_tools, resolve_tool
from librarian.config import Config, ToolSpec

SPEC = ToolSpec(
    name="rsgain",
    env_bin="LIBRARIAN_RSGAIN_BIN",
    filename="rsgain",
)


def make_config() -> Config:
    return Config(tools=(SPEC,))


class TestResolveTool:
    def test_env_bin_wins(self, tmp_path):
        exe = tmp_path / "custom-rsgain"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        resolved = resolve_tool(SPEC, make_config(), {"LIBRARIAN_RSGAIN_BIN": str(exe)})
        assert resolved == str(exe)

    def test_env_bin_must_exist(self, tmp_path):
        with pytest.raises(ToolError, match="does not exist"):
            resolve_tool(SPEC, make_config(), {"LIBRARIAN_RSGAIN_BIN": str(tmp_path / "no")})

    def test_path_lookup(self, tmp_path, monkeypatch):
        # Any executable on a controlled PATH resolves by filename.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        exe = bin_dir / "rsgain"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        assert resolve_tool(SPEC, make_config(), {}) == str(exe)

    def test_missing_everywhere(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))  # empty dir on PATH
        with pytest.raises(ToolError, match="not on PATH"):
            resolve_tool(SPEC, make_config(), {})

    def test_env_bin_beats_path(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "rsgain").write_text("#!/bin/sh\n")
        (bin_dir / "rsgain").chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        override = tmp_path / "override"
        override.write_text("#!/bin/sh\n")
        override.chmod(0o755)
        resolved = resolve_tool(
            SPEC, make_config(), {"LIBRARIAN_RSGAIN_BIN": str(override)}
        )
        assert resolved == str(override)


class TestResolveAll:
    def test_returns_all_three_tools(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name in ("rsgain", "aubio", "keyfinder-cli"):
            exe = bin_dir / name
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        cfg = Config.from_env({})  # default tool specs
        resolved = resolve_all_tools(cfg, {})
        assert resolved == {
            "rsgain": str(bin_dir / "rsgain"),
            "aubio": str(bin_dir / "aubio"),
            "keyfinder-cli": str(bin_dir / "keyfinder-cli"),
        }

    def test_missing_tool_fails_loudly(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        with pytest.raises(ToolError, match="could not provision"):
            resolve_all_tools(Config.from_env({}), {})

"""Standalone test harness for hermes-plugin-line-whitelist.

These components run *inside* a Hermes host at runtime and import a handful of
Hermes-internal modules (``hermes_cli.config``, ``tools.registry``,
``tools.send_message_tool``, ``gateway.config``, ``gateway.session_context``,
``hermes_state``). Those only exist inside a running Hermes install. So the unit
tests can run in a plain checkout (CI, a laptop with no Hermes), this conftest:

  1. Puts ``./src`` on ``sys.path`` so ``import plugins.platforms.line.*`` and
     ``import tools.line_whitelist_tool`` resolve to the files in this repo.
  2. Installs *lightweight stubs* for the Hermes host modules — but ONLY when the
     real Hermes package is not importable. Inside a real Hermes tree the genuine
     modules win, so these tests double as a host-integration smoke check.

No plugin source is modified — the stubs live entirely in the test harness. The
``src/tools`` and ``src/plugins`` trees are real namespace packages (they carry
the shipped code), so we only inject the *missing* host submodules, never a stub
over the real ``tools``/``plugins`` package.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

# 1. Make the shipped code importable under its canonical Hermes paths.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
for _p in (str(SRC), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _new_pkg(name: str) -> types.ModuleType:
    """Create (or fetch) a stub *package* module in sys.modules."""
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        mod.__path__ = []  # mark as a package
        sys.modules[name] = mod
    return mod


def _install_host_stubs() -> None:
    # If a real Hermes is importable, use it (integration mode) and stub nothing.
    try:  # pragma: no cover - only in a real Hermes tree
        import hermes_cli.config  # noqa: F401
        import tools.registry  # noqa: F401
        return
    except Exception:
        pass

    # -- tools.registry / tools.send_message_tool -------------------------
    # ``tools`` is a REAL namespace package here (src/tools/line_whitelist_tool.py),
    # so we must NOT replace it — only inject the two host submodules it needs.
    import tools  # noqa: F401  (namespace package from ./src/tools)

    if "tools.registry" not in sys.modules:
        reg = types.ModuleType("tools.registry")

        class _Registry:
            def __init__(self) -> None:
                self._by_toolset: dict = {}

            def register(self, *, name, toolset=None, **kw):
                self._by_toolset.setdefault(toolset, []).append(name)

            def get_tool_names_for_toolset(self, toolset):
                return list(self._by_toolset.get(toolset, []))

        reg.registry = _Registry()
        reg.tool_error = lambda m, **k: json.dumps(
            {"error": str(m), **k}, ensure_ascii=False
        )
        reg.tool_result = lambda d=None, **k: json.dumps(
            d if d is not None else k, ensure_ascii=False, default=str
        )
        sys.modules["tools.registry"] = reg
        sys.modules["tools"].registry = reg

    if "tools.send_message_tool" not in sys.modules:
        smt = types.ModuleType("tools.send_message_tool")

        async def _send_to_platform(platform, pconfig, chat_id, message, **kw):
            return {"success": True, "platform": str(platform), "chat_id": chat_id}

        smt._send_to_platform = _send_to_platform
        sys.modules["tools.send_message_tool"] = smt
        sys.modules["tools"].send_message_tool = smt

    # -- gateway.config (Platform) / gateway.session_context --------------
    gw = _new_pkg("gateway")
    if "gateway.config" not in sys.modules:
        gc = types.ModuleType("gateway.config")
        gc.Platform = lambda name: str(name)  # enum-free stand-in
        sys.modules["gateway.config"] = gc
        gw.config = gc
    if "gateway.session_context" not in sys.modules:
        gsc = types.ModuleType("gateway.session_context")
        gsc.get_session_env = lambda key, default="": os.environ.get(key, default)
        sys.modules["gateway.session_context"] = gsc
        gw.session_context = gsc

    # -- hermes_cli.config ------------------------------------------------
    hc = _new_pkg("hermes_cli")
    if "hermes_cli.config" not in sys.modules:
        hcc = types.ModuleType("hermes_cli.config")
        hcc.load_config = lambda: {}

        def write_platform_config_field(platform_key, field_key, value, *, raw=False):
            return None

        hcc.write_platform_config_field = write_platform_config_field
        sys.modules["hermes_cli.config"] = hcc
        hc.config = hcc

    # -- hermes_state.SessionDB (dashboard test monkeypatches this) -------
    if "hermes_state" not in sys.modules:
        hs = types.ModuleType("hermes_state")

        class SessionDB:  # placeholder; the dashboard test swaps in a fake
            pass

        hs.SessionDB = SessionDB
        sys.modules["hermes_state"] = hs


_install_host_stubs()


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Deterministic locale/timezone, mirroring the upstream Hermes suite."""
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("PYTHONHASHSEED", "0")

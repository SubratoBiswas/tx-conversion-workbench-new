"""Test-suite-wide fixtures.

The workbench tests share one SQLite database (``test_workbench.db``)
across the whole pytest session because rebuilding the engagement seed
on every test is prohibitively expensive (rules engine, FBDI catalog,
COA template, KB hydration). The trade-off is that residual state from
a prior run can pollute the *next* run — e.g. a test that introduces a
``REMOVE_HYPHEN`` rule sticks around the next time ``pytest`` is
invoked.

This fixture deletes the file ONCE at session start so every fresh run
boots a known-clean engagement, then leaves it in place for inspection
after the run completes. Tests can still mutate state — that's fine,
because state never survives across ``pytest`` invocations.
"""
from __future__ import annotations

import os
from pathlib import Path


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Drop any stale test DB before the session begins."""
    backend_dir = Path(__file__).resolve().parent.parent
    candidates = [
        backend_dir / "test_workbench.db",
        Path("test_workbench.db"),
    ]
    for p in candidates:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            # Best-effort — if the file is locked we'd rather let the
            # suite run and let the offending test fail loudly than
            # crash collection.
            pass
    # Always force the test DB URL — the import in test_app.py also sets
    # this, but doing it here guarantees the env var is set before any
    # of the app modules import settings.
    os.environ.setdefault("DATABASE_URL", "sqlite:///./test_workbench.db")

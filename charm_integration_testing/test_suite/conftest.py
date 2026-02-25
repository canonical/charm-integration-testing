# Copyright 2024-2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Top-level test-suite conftest: registers plugins and shared fixtures.

All fixture definitions and hooks live in dedicated modules under
``fixtures/`` and ``scheduler/``; this file is intentionally minimal and
acts as the single registration point for all of them.

Module layout
-------------
fixtures/juju.py
    Core Juju fixtures: ``juju_client``, ``model``, ``bundles``,
    and supporting CLI options.

fixtures/metadata.py
    Execution-metadata fixtures that attach structured data (charm revisions,
    integrations, CI provenance, failure details) to JUnit XML reports.

fixtures/reporting.py
    Per-test Juju status logging (``print_setup_and_teardown_info``) and the
    ``pytest_runtest_makereport`` hook that stashes failure/skip information.

scheduler/plugin.py
    State-driven graph scheduler: registers the ``@pytest.mark.state`` marker,
    the ``--current-state`` CLI option, and the ``pytest_collection_modifyitems``
    hook that reorders tests and injects bridging transitions automatically.
"""

pytest_plugins = [
    "test_suite.fixtures.juju",
    "test_suite.fixtures.metadata",
    "test_suite.fixtures.reporting",
]

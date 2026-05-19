# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Verify both processor entrypoints invoke the Azure Monitor bootstrap.

This is a guard-rail test: if a future refactor accidentally removes the
``configure_azure_monitor_if_enabled()`` call from ``main.Application.initialize``
or ``main_service.QueueMigrationServiceApp.initialize``, the processor
will silently stop exporting token-usage events to Application Insights.
That regression would not be caught by any other unit test (the
underlying bootstrap function is itself well-covered in
``test_bootstrap.py``).

We bypass the real ``__init__`` of both classes via ``object.__new__``
so we don't have to stand up the whole DI container / .env loader just
to assert one call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_main_application_initialize_calls_bootstrap():
    """``main.Application.initialize`` must invoke the Azure Monitor bootstrap."""
    import main

    instance = object.__new__(main.Application)
    instance.application_context = MagicMock(configuration="<mocked>")

    with patch.object(main, "configure_azure_monitor_if_enabled") as fake_boot, \
            patch.object(instance, "register_services") as fake_register:
        instance.initialize()

    fake_boot.assert_called_once_with()
    fake_register.assert_called_once_with()


def test_main_service_initialize_calls_bootstrap():
    """``main_service.QueueMigrationServiceApp.initialize`` must invoke the bootstrap."""
    import main_service

    instance = object.__new__(main_service.QueueMigrationServiceApp)
    instance.application_context = MagicMock(configuration="<mocked>")
    instance.debug_mode = False

    with patch.object(
        main_service, "configure_azure_monitor_if_enabled"
    ) as fake_boot, \
            patch.object(instance, "register_services") as fake_register:
        instance.initialize()

    fake_boot.assert_called_once_with()
    fake_register.assert_called_once_with()

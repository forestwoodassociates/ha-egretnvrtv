from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    # The plugin's own testing_config/custom_components package is imported first and would
    # shadow this checkout's; put ours on the package path ahead of it.
    import custom_components

    ours = str(Path(__file__).parents[1] / "custom_components")
    if ours not in custom_components.__path__:
        custom_components.__path__.insert(0, ours)
    yield

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = ROOT / "frontend" / "src" / "App.tsx"


def test_header_opens_workspace_and_omnigent_in_new_tabs():
    source = APP_SOURCE.read_text()

    assert 'title="Open the Databricks workspace"' in source
    assert "href={config.workspace_url}" in source
    assert 'title="Open the dedicated Omnigent app"' in source
    assert "href={config.omnigent_remote.url}" in source
    assert source.count('target="_blank"') >= 2
    assert source.count('rel="noopener noreferrer"') >= 2


def test_the_header_labels_are_destinations_not_instructions():
    """The bar is a row of places to go; "Open" on every one of them is filler."""
    source = APP_SOURCE.read_text()

    assert "Open Workspace" not in source
    assert "Open Omnigent" not in source

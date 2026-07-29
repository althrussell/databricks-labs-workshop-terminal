from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = ROOT / "frontend" / "src" / "App.tsx"


def test_header_opens_workspace_and_omnigent_in_new_tabs():
    source = APP_SOURCE.read_text()

    assert "Open Workspace" in source
    assert "href={config.workspace_url}" in source
    assert "Open Omnigent" in source
    assert "href={config.omnigent_remote.url}" in source
    assert source.count('target="_blank"') >= 2
    assert source.count('rel="noopener noreferrer"') >= 2

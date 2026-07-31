"""The apps census counts what the attendee built, not what we deployed.

A workshop workspace always already contains this terminal, so the raw app list
credits every attendee with an app before they have opened a file. That number
reaches an account team through the impact report and the brag certificate, so
an inflated one is worse than none: it invites a conversation about an app that
does not exist.
"""

import pytest

from server import stats


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload


def _census(monkeypatch, apps: list[dict]) -> dict:
    """Run the census with a stubbed workspace containing `apps`."""

    def fake_get(url: str, **kwargs):
        if url.endswith("/api/2.0/apps"):
            return _Resp({"apps": apps})
        return _Resp({})

    monkeypatch.setattr(stats.requests, "get", fake_get)
    monkeypatch.setattr(stats.config, "databricks_host", lambda: "https://ws.example")

    class _Creds:
        @staticmethod
        def token() -> str:
            return "tok"

    monkeypatch.setattr("server.credentials.credential_manager", _Creds)
    return stats._workspace_resources()


TERMINAL_SP = "terminal-sp-client-id"


@pytest.fixture(autouse=True)
def _terminal_identity(monkeypatch):
    """This terminal's own identity, as the Apps runtime supplies it."""
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", TERMINAL_SP)
    monkeypatch.setenv("DATABRICKS_APP_URL", "https://workshop-terminal-1.example")
    monkeypatch.setattr(stats.config, "omnigent_app_url", lambda: "")


def test_the_terminal_does_not_count_itself(monkeypatch):
    census = _census(
        monkeypatch,
        [
            {
                "name": "workshop-terminal",
                "url": "https://workshop-terminal-1.example",
                "service_principal_client_id": TERMINAL_SP,
            }
        ],
    )
    assert census["apps"] == 0


def test_an_app_the_attendee_built_is_counted(monkeypatch):
    census = _census(
        monkeypatch,
        [
            {
                "name": "workshop-terminal",
                "url": "https://workshop-terminal-1.example",
                "service_principal_client_id": TERMINAL_SP,
            },
            {
                "name": "claims-triage",
                "url": "https://claims-triage-1.example",
                "service_principal_client_id": "some-other-sp",
            },
        ],
    )
    assert census["apps"] == 1


def test_omnigent_is_ours_too(monkeypatch):
    """Omnigent is deployed by Control Tower beside the terminal. It runs as its
    own service principal, so the only handle we have on it is the URL we were
    told to reach it on."""
    monkeypatch.setattr(
        stats.config, "omnigent_app_url", lambda: "https://omnigent-1.example"
    )
    census = _census(
        monkeypatch,
        [
            {
                "name": "omnigent",
                "url": "https://omnigent-1.example/",
                "service_principal_client_id": "omnigent-sp",
            },
            {
                "name": "claims-triage",
                "url": "https://claims-triage-1.example",
                "service_principal_client_id": "attendee-app-sp",
            },
        ],
    )
    assert census["apps"] == 1


def test_a_terminal_whose_own_row_hides_its_principal_still_excludes_itself(
    monkeypatch,
):
    """Some census rows come back without the service principal field. Falling
    back to the app's own URL keeps the count honest rather than letting the
    terminal reappear as attendee work."""
    census = _census(
        monkeypatch,
        [{"name": "workshop-terminal", "url": "https://workshop-terminal-1.example"}],
    )
    assert census["apps"] == 0


def test_a_broken_omnigent_url_does_not_take_the_census_down(monkeypatch):
    """The census is best-effort. A stats call that raised on one bad env var
    would cost Control Tower the entire harvest for that attendee."""

    def explode() -> str:
        raise ValueError("OMNIGENT_APP_URL must be an absolute URL")

    monkeypatch.setattr(stats.config, "omnigent_app_url", explode)
    census = _census(
        monkeypatch,
        [
            {
                "name": "claims-triage",
                "url": "https://claims-triage-1.example",
                "service_principal_client_id": "attendee-app-sp",
            }
        ],
    )
    assert census["apps"] == 1


def test_a_workspace_with_no_apps_reports_none(monkeypatch):
    assert _census(monkeypatch, [])["apps"] == 0


def test_junk_rows_are_ignored_rather_than_counted(monkeypatch):
    """A malformed row must not become a phantom app in someone's brief."""
    census = _census(monkeypatch, ["not-a-dict", None])  # type: ignore[list-item]
    assert census["apps"] == 0

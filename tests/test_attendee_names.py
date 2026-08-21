"""Sourced attendee names: append, never overwrite, always say where from.

An attendee routinely lands on a workspace nobody assigned to them. The two
places that already ask a human to type their own name — the opening wizard and
the certificate — are the only first-hand evidence of who is really there, and
both used to throw the answer away.
"""

from __future__ import annotations

import pytest

from server import attendee_names


@pytest.fixture()
def home(tmp_path):
    return str(tmp_path)


@pytest.fixture()
def captured(monkeypatch):
    """What would have been buffered for Control Tower."""
    from server import event_emitter as emitter_module

    records: list[tuple[str, str, dict]] = []

    def capture(event_type, attendee, payload=None, **_kwargs):
        records.append((event_type, attendee, payload or {}))

    monkeypatch.setattr(emitter_module.event_emitter, "emit", capture)
    return records


# -- storage ----------------------------------------------------------------

def test_a_second_name_is_appended_not_written_over(home):
    """Two names on one workspace is either corroboration or two people. A
    last-write-wins field renders both identically, and the second is exactly
    what an operator needs to see."""
    attendee_names.observe(home, "Priya Raman", attendee_names.SOURCE_WIZARD)
    observations = attendee_names.observe(
        home, "Tom Weller", attendee_names.SOURCE_CERTIFICATE
    )

    assert [o.name for o in observations] == ["Priya Raman", "Tom Weller"]
    assert [o.source for o in observations] == ["wizard", "certificate"]


def test_the_same_name_from_a_second_source_is_kept(home):
    """It is corroboration, and weighing the two sources differently is the
    whole reason source travels with the name."""
    attendee_names.observe(home, "Priya Raman", attendee_names.SOURCE_WIZARD)
    observations = attendee_names.observe(
        home, "Priya Raman", attendee_names.SOURCE_CERTIFICATE
    )

    assert len(observations) == 2


def test_regenerating_a_certificate_is_not_new_evidence(home):
    attendee_names.observe(home, "Priya Raman", attendee_names.SOURCE_CERTIFICATE)
    attendee_names.observe(home, "priya raman", attendee_names.SOURCE_CERTIFICATE)
    observations = attendee_names.observe(
        home, "Priya Raman", attendee_names.SOURCE_CERTIFICATE
    )

    assert len(observations) == 1


def test_whitespace_is_collapsed_so_a_stray_keystroke_is_not_corroboration(home):
    attendee_names.observe(home, "Priya  Raman", attendee_names.SOURCE_WIZARD)
    observations = attendee_names.observe(
        home, "Priya Raman", attendee_names.SOURCE_WIZARD
    )

    assert len(observations) == 1
    assert observations[0].name == "Priya Raman"


def test_an_empty_name_records_nothing(home):
    assert attendee_names.observe(home, "   ", attendee_names.SOURCE_WIZARD) == []
    assert attendee_names.read(home) == []


def test_observations_are_bounded(home):
    for i in range(attendee_names.MAX_OBSERVATIONS + 5):
        attendee_names.observe(home, f"Person {i}", attendee_names.SOURCE_WIZARD)

    assert len(attendee_names.read(home)) == attendee_names.MAX_OBSERVATIONS


def test_an_unreadable_file_is_not_a_500(home, monkeypatch):
    path = attendee_names.names_path(home)
    import os

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ not json")

    assert attendee_names.read(home) == []


# -- emission ---------------------------------------------------------------

def test_nothing_leaves_the_instance_without_capture(home, captured, monkeypatch):
    """A typed human name is new PII — the contract only ever promised pooled
    lab identities. An operator who has not arranged consent gets nothing by
    doing nothing."""
    monkeypatch.setattr(
        attendee_names.config, "insight_capture_enabled", lambda: False
    )

    recorded = attendee_names.capture(
        "labuser017@example.com", home, "Priya Raman", attendee_names.SOURCE_WIZARD
    )

    assert recorded is True  # still held locally, for their own certificate
    assert captured == []


def test_capture_on_emits_the_whole_picture_each_time(home, captured, monkeypatch):
    """Carrying the full list means a consumer takes the latest event and needs
    no merge logic."""
    monkeypatch.setattr(
        attendee_names.config, "insight_capture_enabled", lambda: True
    )

    attendee_names.capture(
        "labuser017@example.com", home, "Priya Raman", attendee_names.SOURCE_WIZARD
    )
    attendee_names.capture(
        "labuser017@example.com",
        home,
        "P. Raman",
        attendee_names.SOURCE_CERTIFICATE,
    )

    assert [t for t, _a, _p in captured] == ["attendee.identity"] * 2
    latest = captured[-1][2]
    assert [n["name"] for n in latest["names"]] == ["Priya Raman", "P. Raman"]
    assert latest["attendee"] == "labuser017@example.com"
    # The names are evidence about exactly this.
    assert latest["binding_source"]


def test_a_repeat_burns_no_event(home, captured, monkeypatch):
    monkeypatch.setattr(
        attendee_names.config, "insight_capture_enabled", lambda: True
    )

    attendee_names.capture(
        "labuser017@example.com", home, "Priya", attendee_names.SOURCE_CERTIFICATE
    )
    recorded = attendee_names.capture(
        "labuser017@example.com", home, "Priya", attendee_names.SOURCE_CERTIFICATE
    )

    assert recorded is False
    assert len(captured) == 1


def test_an_unknown_source_is_refused(home, captured, monkeypatch):
    """Source is the field that makes one observation weigh more than another.
    A producer that invents one has broken that, and silently accepting it
    would flatten the distinction downstream."""
    monkeypatch.setattr(
        attendee_names.config, "insight_capture_enabled", lambda: True
    )

    assert (
        attendee_names.capture(
            "labuser017@example.com", home, "Priya", "some-new-form"
        )
        is False
    )
    assert attendee_names.read(home) == []
    assert captured == []


# -- the two capture points -------------------------------------------------


@pytest.fixture()
def unbriefed():
    """Leave the shared attendee as we found them.

    These tests save a wizard brief to exercise the capture on the way past. The
    brief lives in the attendee's home, which outlives the request, so leaving
    it behind tells every later test that this attendee has already been through
    the wizard.
    """
    from server.users import user_manager
    from server import wizard

    def clear() -> None:
        import os

        try:
            os.remove(wizard.brief_path(user_manager.get("bob@example.com")))
        except FileNotFoundError:
            pass

    clear()
    yield
    clear()


def test_the_certificate_records_the_name_on_it(client, monkeypatch, captured):
    from tests.conftest import BOB

    monkeypatch.setattr(
        attendee_names.config, "insight_capture_enabled", lambda: True
    )
    resp = client.get("/api/certificate?name=Priya%20Raman", headers=BOB)

    assert resp.status_code == 200
    identity = [p for t, _a, p in captured if t == "attendee.identity"]
    assert identity, "the strongest name evidence in the system was discarded"
    assert identity[-1]["names"][-1]["source"] == "certificate"
    assert identity[-1]["names"][-1]["name"] == "Priya Raman"


def test_the_wizard_records_an_optional_typed_name(
    client, monkeypatch, captured, unbriefed
):
    from tests.conftest import BOB

    monkeypatch.setattr(
        attendee_names.config, "insight_capture_enabled", lambda: True
    )
    resp = client.post(
        "/api/wizard",
        headers=BOB,
        json={"what_building": "a dashboard", "display_name": "Priya Raman"},
    )

    assert resp.status_code == 200
    identity = [p for t, _a, p in captured if t == "attendee.identity"]
    assert identity
    assert identity[-1]["names"][-1]["source"] == "wizard"


def test_a_wizard_save_with_no_name_records_no_identity(
    client, monkeypatch, captured, unbriefed
):
    from tests.conftest import BOB

    monkeypatch.setattr(
        attendee_names.config, "insight_capture_enabled", lambda: True
    )
    client.post("/api/wizard", headers=BOB, json={"what_building": "a dashboard"})

    assert [p for t, _a, p in captured if t == "attendee.identity"] == []

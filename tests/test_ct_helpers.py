import json

import pytest

from scripts import ct_fleet, ct_verify


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _apps():
    return [
        {"name": "b", "url": "https://b.example", "token": "bearer-b"},
        {"name": "a", "url": "https://a.example", "token": "bearer-a"},
    ]


def _setup(*, omnigent=False):
    return {
        "steps": {"skills": {"status": "complete"}},
        "release_manifest": {
            "databricks_agent_skills": {"match": True, "source": "prewarmed"},
            "omnigent": {
                "enabled": omnigent,
                "match": True if omnigent else None,
            },
        },
    }


def _prewarm(commit="a" * 40, checksum="b" * 64, *, omnigent=False):
    names = ["node", "claude", "codex", "databricks"]
    if omnigent:
        names.extend(["omnigent", "tmux"])
    return {
        "reusable": True,
        "manifest": {
            "expected_binaries": sorted(names),
            "binaries": {
                name: {
                    "expected": "1.2.3",
                    "actual": "1.2.3",
                    "actual_checksum": "c" * 64,
                    "source": "persistent",
                    "reusable": True,
                }
                for name in names
            },
            "databricks_agent_skills": {
                "expected_ref": "v1.2.3",
                "actual_ref": "v1.2.3",
                "resolved_commit": commit,
                "expected_checksum": checksum,
                "actual_checksum": checksum,
                "source": "persistent",
                "reusable": True,
            },
        },
    }


def _mirror(*, configured=True, served=6, from_network=(), error=None):
    return {
        "configured": configured,
        "path": "/Volumes/main/central/toolchain" if configured else "",
        "strict": False,
        "served": served,
        "bypassed": bool(configured and from_network),
        "from_network": list(from_network),
        "error": error,
    }


def _advancing_clock():
    """A clock that moves, so a case that never goes green still terminates."""
    ticks = iter(range(0, 10_000))
    return lambda: float(next(ticks))


def _verify_with_mirror(mirror, *, require_mirror=True):
    def get(url, **kwargs):
        if url.endswith("/readyz"):
            return Response(200, {"ready": True})
        if url.endswith("/api/admin/prewarm-status"):
            return Response(200, _prewarm())
        payload = dict(_setup())
        if mirror is not None:
            payload["toolchain_mirror"] = mirror
        return Response(200, payload)

    return ct_verify.verify_apps(
        _apps(),
        get=get,
        timeout=10,
        poll_interval=0,
        sleep=lambda _: None,
        monotonic=_advancing_clock(),
        require_mirror=require_mirror,
    )


def test_the_mirror_is_reported_even_when_it_is_not_being_enforced():
    """A bypassed mirror is otherwise invisible: the app is healthy, every
    checksum matches, and the only symptom is that boot got slow again."""
    report = _verify_with_mirror(
        _mirror(served=4, from_network=["codex_npm_launcher_package"]),
        require_mirror=False,
    )

    assert report["exit_code"] == 0
    assert report["require_mirror"] is False
    summary = report["apps"][0]["toolchain_mirror"]
    assert summary["bypassed"] is True
    assert summary["from_network"] == ["codex_npm_launcher_package"]


def test_require_mirror_fails_an_otherwise_healthy_app_that_used_the_internet():
    report = _verify_with_mirror(
        _mirror(served=4, from_network=["codex_npm_launcher_package"])
    )

    assert report["exit_code"] == 1
    assert report["status"] == "mirror_bypassed"


def test_require_mirror_passes_when_every_artifact_came_off_the_volume():
    report = _verify_with_mirror(_mirror())

    assert report["exit_code"] == 0
    assert report["status"] == "ready"


def test_require_mirror_rejects_an_app_that_has_no_mirror_configured():
    """Otherwise an event that forgot to set the path would sail through."""
    report = _verify_with_mirror(_mirror(configured=False, served=0))

    assert report["exit_code"] == 1
    assert report["status"] == "mirror_bypassed"


def test_require_mirror_rejects_a_release_too_old_to_report_provenance():
    """No evidence is not the same as evidence of success."""
    report = _verify_with_mirror(None)

    assert report["exit_code"] == 1
    assert report["apps"][0]["toolchain_mirror"]["reported"] is False


def test_require_mirror_rejects_a_configured_but_rejected_path():
    report = _verify_with_mirror(
        _mirror(served=0, error="path is not an absolute /Volumes/... address")
    )

    assert report["exit_code"] == 1


def test_a_mirror_bypass_is_distinguished_from_an_app_that_is_simply_not_ready():
    """The remedies differ: one is a resync, the other a redeploy."""
    bypassed = _verify_with_mirror(_mirror(from_network=["node_linux_x64"]))

    def not_ready(url, **kwargs):
        if url.endswith("/readyz"):
            return Response(503, {"ready": False})
        if url.endswith("/api/admin/prewarm-status"):
            return Response(200, _prewarm())
        return Response(200, dict(_setup(), toolchain_mirror=_mirror()))

    unready = ct_verify.verify_apps(
        _apps(),
        get=not_ready,
        timeout=10,
        poll_interval=0,
        sleep=lambda _: None,
        monotonic=_advancing_clock(),
        require_mirror=True,
    )

    assert bypassed["status"] == "mirror_bypassed"
    assert unready["status"] == "not_ready"


def test_require_mirror_passes_an_app_that_had_nothing_left_to_fetch():
    """An app redeployed onto its existing shared prefix installs entirely from
    prewarmed binaries and fetches nothing, so it serves zero artifacts off the
    volume while taking zero from the internet. Demanding a positive count would
    fail the instances that needed the mirror least -- and fail them slowly, by
    exhausting the poll timeout waiting for a count that nobody will raise."""
    report = _verify_with_mirror(_mirror(served=0))

    assert report["status"] == "ready"
    assert report["exit_code"] == 0


def test_an_app_still_booting_is_not_ready_rather_than_bypassed():
    """Mid-bootstrap nothing has been served yet, which looks identical to a
    bypass. Calling it one sends the operator to rebuild a volume that was fine,
    when all they had to do was wait."""

    def still_booting(url, **kwargs):
        if url.endswith("/readyz"):
            return Response(503, {"ready": False})
        if url.endswith("/api/admin/prewarm-status"):
            return Response(200, _prewarm())
        return Response(200, dict(_setup(), toolchain_mirror=_mirror(served=0)))

    report = ct_verify.verify_apps(
        _apps(),
        get=still_booting,
        timeout=10,
        poll_interval=0,
        sleep=lambda _: None,
        monotonic=_advancing_clock(),
        require_mirror=True,
    )

    assert report["status"] == "not_ready"


def test_a_settled_bypass_does_not_burn_the_whole_timeout():
    """The mirror verdict is final once bootstrap finishes, so continuing to poll
    re-asks a settled question and delays the answer by the full timeout."""
    polls = {"count": 0}

    def get(url, **kwargs):
        if url.endswith("/readyz"):
            polls["count"] += 1
            return Response(200, {"ready": True})
        if url.endswith("/api/admin/prewarm-status"):
            return Response(200, _prewarm())
        return Response(
            200,
            dict(_setup(), toolchain_mirror=_mirror(from_network=["node_linux_x64"])),
        )

    report = ct_verify.verify_apps(
        _apps(),
        get=get,
        timeout=10_000,
        poll_interval=0,
        sleep=lambda _: None,
        monotonic=_advancing_clock(),
        require_mirror=True,
    )

    assert report["status"] == "mirror_bypassed"
    # Two apps, one pass each -- not one pass per second until the deadline.
    assert polls["count"] == 2


def test_mirror_state_is_not_folded_into_cross_instance_manifest_matching():
    """One app prewarmed and another served from the volume is a legitimate
    fleet, so provenance must not make two healthy apps look divergent."""
    assert "source" in ct_verify._VOLATILE_RELEASE_FIELDS


def test_verify_waits_for_two_apps_and_returns_sorted_deterministic_report():
    calls = {}

    def get(url, **kwargs):
        assert kwargs["headers"]["Authorization"].startswith("Bearer ")
        calls[url] = calls.get(url, 0) + 1
        if url.endswith("/readyz"):
            if calls[url] == 1:
                return Response(503, {"ready": False})
            return Response(200, {"ready": True})
        if url.endswith("/api/admin/prewarm-status"):
            return Response(200, _prewarm())
        assert url.endswith("/api/admin/setup-status")
        return Response(200, _setup())

    report = ct_verify.verify_apps(
        _apps(),
        get=get,
        timeout=10,
        poll_interval=0,
        sleep=lambda _: None,
        monotonic=lambda: 0,
    )

    assert report["exit_code"] == 0
    assert report["status"] == "ready"
    assert [app["name"] for app in report["apps"]] == ["a", "b"]
    assert all(app["ready"] for app in report["apps"])
    assert "bearer-" not in json.dumps(report)


def test_verify_fails_closed_when_setup_manifest_is_mismatched():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()

    def get(url, **kwargs):
        if url.endswith("/readyz"):
            return Response(200, {"ready": True})
        if url.endswith("/api/admin/prewarm-status"):
            return Response(200, _prewarm())
        return Response(
            200,
            {
                "steps": {"skills": {"status": "complete"}},
                "release_manifest": {
                    "databricks_agent_skills": {
                        "match": False,
                        "source": "vendored_fallback",
                    }
                },
            },
        )

    report = ct_verify.verify_apps(
        _apps(),
        get=get,
        timeout=1,
        poll_interval=0,
        sleep=lambda _: setattr(clock, "value", 2),
        monotonic=clock,
    )

    assert report["exit_code"] == 1
    assert report["status"] == "not_ready"
    assert all(not app["ready"] for app in report["apps"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda proof: proof["manifest"]["binaries"].pop("node"),
        lambda proof: proof["manifest"]["binaries"].update({
            "extra": {
                "expected": "1.2.3",
                "actual": "1.2.3",
                "actual_checksum": "c" * 64,
                "source": "persistent",
            }
        }),
        lambda proof: proof["manifest"]["binaries"]["claude"].update(
            {"actual": "9.9.9"}
        ),
        lambda proof: proof["manifest"]["binaries"]["codex"].update(
            {"actual_checksum": None}
        ),
        lambda proof: proof["manifest"]["binaries"]["databricks"].update(
            {"source": "network"}
        ),
    ],
)
def test_verify_recomputes_exact_binary_reusability(mutate):
    proof = _prewarm()
    mutate(proof)

    assert ct_verify._prewarm_valid(proof) is False


def test_verify_rejects_top_level_reusable_false():
    proof = _prewarm()
    proof["reusable"] = False

    assert ct_verify._prewarm_valid(proof) is False


def test_verify_rejects_tmux_reusable_false():
    proof = _prewarm(omnigent=True)
    proof["manifest"]["binaries"]["tmux"]["reusable"] = False

    assert ct_verify._prewarm_valid(proof) is False


def test_verify_rejects_omnigent_stamp_failure():
    proof = _prewarm(omnigent=True)
    proof["manifest"]["binaries"]["omnigent"]["reusable"] = False

    assert ct_verify._prewarm_valid(proof) is False


def test_verify_accepts_only_complete_optional_omnigent_binary_pair():
    complete = _prewarm(omnigent=True)
    contradictory = _prewarm()
    contradictory["manifest"]["expected_binaries"].append("tmux")
    contradictory["manifest"]["binaries"]["tmux"] = {
        "expected": "a" * 64,
        "actual": "a" * 64,
        "actual_checksum": "a" * 64,
        "source": "persistent",
    }

    assert ct_verify._prewarm_valid(complete) is True
    assert ct_verify._prewarm_valid(contradictory) is False


def test_verify_requires_omnigent_pair_when_release_manifest_enables_it():
    def get(url, **kwargs):
        if url.endswith("/readyz"):
            return Response(200, {"ready": True})
        if url.endswith("/api/admin/setup-status"):
            return Response(200, _setup(omnigent=True))
        return Response(200, _prewarm(omnigent=False))

    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    report = ct_verify.verify_apps(
        _apps(),
        get=get,
        timeout=1,
        poll_interval=0,
        monotonic=clock,
        sleep=lambda _: setattr(clock, "value", 2),
    )

    assert report["exit_code"] == 1
    assert report["status"] == "not_ready"


def test_verify_requires_identical_release_and_prewarm_manifests():
    def get(url, **kwargs):
        if url.endswith("/readyz"):
            return Response(200, {"ready": True})
        if url.endswith("/api/admin/setup-status"):
            setup = _setup()
            setup["release_manifest"]["databricks_agent_skills"]["source"] = (
                "network" if url.startswith("https://a.") else "prewarmed"
            )
            return Response(200, setup)
        checksum = "a" * 64 if url.startswith("https://a.") else "b" * 64
        return Response(200, _prewarm(checksum=checksum))

    report = ct_verify.verify_apps(
        _apps(),
        get=get,
        timeout=1,
        poll_interval=0,
        sleep=lambda _: None,
        monotonic=lambda: 0,
    )

    assert report["exit_code"] == 1
    assert report["status"] == "manifest_mismatch"


def test_verify_rejects_incomplete_prewarm_proof_even_if_flag_says_reusable():
    def get(url, **kwargs):
        if url.endswith("/readyz"):
            return Response(200, {"ready": True})
        if url.endswith("/api/admin/setup-status"):
            return Response(200, _setup())
        return Response(200, {"reusable": True, "manifest": {"binaries": {}}})

    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    report = ct_verify.verify_apps(
        _apps(),
        get=get,
        timeout=1,
        poll_interval=0,
        monotonic=clock,
        sleep=lambda _: setattr(clock, "value", 2),
    )

    assert report["exit_code"] == 1
    assert report["status"] == "not_ready"


def test_verify_inventory_contract_requires_exactly_two_apps():
    try:
        ct_verify.verify_apps(_apps()[:1], get=lambda *a, **k: None)
    except ValueError as error:
        assert "exactly two" in str(error)
    else:
        raise AssertionError("one app must not satisfy the two-instance contract")


def test_fleet_pause_posts_each_instance_without_exposing_tokens():
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response(200, {"agents_enabled": False})

    report = ct_fleet.execute(
        _apps(),
        action="pause",
        request=request,
        timeout=5,
    )

    assert report["exit_code"] == 0
    assert [result["name"] for result in report["apps"]] == ["a", "b"]
    assert all(call[0] == "POST" for call in calls)
    assert all(call[2]["json"] == {"enabled": False} for call in calls)
    assert "bearer-" not in json.dumps(report)


def test_fleet_dry_run_and_teardown_report_never_delete_apps():
    calls = []

    report = ct_fleet.execute(
        _apps(),
        action="resume",
        request=lambda *args, **kwargs: calls.append((args, kwargs)),
        dry_run=True,
    )

    assert report["exit_code"] == 0
    assert calls == []
    assert all(app["status"] == "dry_run" for app in report["apps"])

    def request(method, url, **kwargs):
        calls.append(((method, url), kwargs))
        return Response(200, {"ok": True})

    ct_fleet.execute(
        _apps(),
        action="teardown-report",
        request=request,
    )
    assert calls
    assert all(method == "GET" for (method, _), _kwargs in calls)
    assert not any("delete" in url for (_, url), _kwargs in calls)


def test_fleet_repushes_content_before_phase():
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return Response(200, {"status": "ok"})

    report = ct_fleet.execute(
        _apps()[:1],
        action="repush",
        request=request,
        content_pack={"phases": ["intro"]},
        phase="intro",
    )

    assert report["exit_code"] == 0
    assert [url.rsplit("/", 1)[-1] for _, url, _ in calls] == [
        "content-pack",
        "phase",
    ]


def test_fleet_status_rollup_reports_readiness_without_response_secrets():
    def request(method, url, **kwargs):
        if url.endswith("/readyz"):
            return Response(200, {"ready": True, "token": "hidden"})
        if url.endswith("/api/admin/setup-status"):
            return Response(200, _setup())
        if url.endswith("/api/admin/prewarm-status"):
            return Response(200, _prewarm())
        if url.endswith("/state"):
            return Response(200, {"phase": "build"})
        return Response(200, {"agents_enabled": False})

    report = ct_fleet.execute(
        _apps()[:1],
        action="status",
        request=request,
    )

    rollup = report["apps"][0]["rollup"]
    assert rollup == {
        "ready": True,
        "setup_verified": True,
        "prewarm_reusable": True,
        "phase": "build",
        "agents_enabled": False,
    }
    assert "hidden" not in json.dumps(report)


def test_fleet_status_fails_closed_when_setup_is_not_verified():
    def request(method, url, **kwargs):
        if url.endswith("/readyz"):
            return Response(200, {"ready": True})
        if url.endswith("/api/admin/setup-status"):
            return Response(200, {"steps": {}, "release_manifest": {}})
        if url.endswith("/api/admin/prewarm-status"):
            return Response(200, _prewarm())
        return Response(200, {})

    report = ct_fleet.execute(_apps()[:1], action="status", request=request)

    assert report["exit_code"] == 1
    assert report["apps"][0]["status"] == "error"


@pytest.mark.parametrize(
    "apps",
    [
        [
            {"name": "same", "url": "https://a.example", "token": "one"},
            {"name": "SAME", "url": "https://b.example", "token": "two"},
        ],
        [
            {"name": "a", "url": "https://A.example/", "token": "one"},
            {"name": "b", "url": "https://a.example", "token": "two"},
        ],
        [
            {"name": "a", "url": "http://a.example", "token": "one"},
            {"name": "b", "url": "https://b.example", "token": "two"},
        ],
        [
            {"name": "a", "url": "https://user:pass@a.example", "token": "one"},
            {"name": "b", "url": "https://b.example", "token": "two"},
        ],
        [
            {"name": "a", "url": "https://a.example?token=secret", "token": "one"},
            {"name": "b", "url": "https://b.example", "token": "two"},
        ],
    ],
)
def test_inventory_rejects_duplicates_insecure_or_credential_bearing_urls(apps):
    with pytest.raises(ValueError) as error:
        ct_verify.verify_apps(apps, get=lambda *args, **kwargs: None)

    message = str(error.value)
    assert "pass" not in message
    assert "secret" not in message
    assert "one" not in message


def test_inventory_allows_explicit_localhost_http_for_tests():
    apps = [
        {"name": "a", "url": "http://localhost:8000", "token": "one"},
        {"name": "b", "url": "http://127.0.0.1:8001/", "token": "two"},
    ]

    with pytest.raises(ValueError):
        ct_verify.verify_apps(apps, get=lambda *args, **kwargs: None)
    report = ct_verify.verify_apps(
        apps,
        get=lambda *args, **kwargs: Response(503, {}),
        allow_local_http=True,
        timeout=0.1,
        poll_interval=0,
        monotonic=iter([0, 1]).__next__,
        sleep=lambda _: None,
    )
    assert report["exit_code"] == 1


def test_verify_uses_true_remaining_deadline_for_each_request():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["timeout"]))
        clock.value += 0.6
        return Response(200, {"ready": True} if url.endswith("/readyz") else _setup())

    report = ct_verify.verify_apps(
        _apps(),
        get=get,
        timeout=1,
        poll_interval=0,
        monotonic=clock,
        sleep=lambda _: None,
    )

    assert report["exit_code"] == 1
    assert [round(timeout, 1) for _, timeout in calls] == [1.0, 0.4]
    assert not any(url.endswith("/prewarm-status") for url, _ in calls)


def test_verify_caps_poll_sleep_to_remaining_deadline():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.value += seconds

    report = ct_verify.verify_apps(
        _apps(),
        get=lambda *args, **kwargs: Response(503, {"ready": False}),
        timeout=1,
        poll_interval=5,
        monotonic=clock,
        sleep=sleep,
    )

    assert report["exit_code"] == 1
    assert sleeps == [1]


def test_fleet_stops_before_operation_when_deadline_is_exhausted():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    calls = []

    def request(method, url, **kwargs):
        calls.append((url, kwargs["timeout"]))
        clock.value += 1.1
        return Response(200, {"status": "ok"})

    report = ct_fleet.execute(
        _apps()[:1],
        action="repush",
        request=request,
        timeout=1,
        content_pack={"phases": ["intro"]},
        phase="intro",
        monotonic=clock,
    )

    assert report["exit_code"] == 1
    assert len(calls) == 1
    assert calls[0][1] == 1


def test_teardown_report_retains_only_bounded_operational_summary():
    def request(method, url, **kwargs):
        if url.endswith("/presence"):
            return Response(200, {
                "users": [{"email": "secret@example.com"}, {"email": "other@example.com"}],
                "session_count": 3,
                "credential": {"state": "rotating", "token": "hidden"},
                "entitlements": {"ok": False, "last_error": "private"},
            })
        if url.endswith("/state"):
            return Response(200, {"phase": "build", "content": "private"})
        return Response(200, {"agents_enabled": False, "attendees": ["private"]})

    report = ct_fleet.execute(
        _apps()[:1],
        action="teardown-report",
        request=request,
    )

    assert report["apps"][0]["summary"] == {
        "presence_count": 2,
        "session_count": 3,
        "phase": "build",
        "credential_state": "rotating",
        "entitlements_ok": False,
        "agents_enabled": False,
    }
    assert "secret@example.com" not in json.dumps(report)
    assert "hidden" not in json.dumps(report)
    assert "private" not in json.dumps(report)


def test_teardown_summary_bounds_labels_and_credential_state():
    def request(method, url, **kwargs):
        if url.endswith("/presence"):
            return Response(200, {
                "users": [],
                "session_count": -10,
                "credential": {"state": "bearer-secret"},
                "entitlements": {},
            })
        if url.endswith("/state"):
            return Response(200, {"phase": "x" * 500})
        return Response(200, {})

    report = ct_fleet.execute(
        _apps()[:1],
        action="teardown-report",
        request=request,
    )
    summary = report["apps"][0]["summary"]

    assert summary["session_count"] == 0
    assert summary["credential_state"] == "unknown"
    assert summary["phase"] == "x" * 128

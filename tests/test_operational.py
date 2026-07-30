import asyncio

from starlette.responses import Response

from server import operational


def test_process_metrics_are_bounded_and_track_http_and_websockets():
    metrics = operational.ProcessMetrics(max_counter=2)

    for code in (409, 409, 409, 429, 503, 200):
        metrics.record_http(code)
    metrics.websocket_attached()
    metrics.websocket_attached()
    metrics.websocket_attached()
    metrics.websocket_detached()

    assert metrics.snapshot() == {
        "http_responses": {"409": 2, "429": 1, "503": 1},
        "websockets": {"current": 1, "total": 2},
    }


def test_operational_snapshot_is_secret_free_and_uses_injected_sources():
    snapshot = operational.build_snapshot(
        installer_status_fn=lambda: {
            "steps": {
                "node": {"status": "complete", "duration_ms": 10},
                "skills": {"status": "error", "duration_ms": 20},
            }
        },
        credential_status_fn=lambda: {
            "state": "rotating",
            "source": "app_identity_oauth",
            "token_expires_in": 500,
            "last_successful_at": 100.0,
            "token": "must-not-leak",
        },
        entitlement_status_fn=lambda: {
            "handoff": {"summary": {"failed": 3}},
            "last_error": "secret-bearing detail",
            "last_reconcile": 115.0,
            "enabled": True,
            "ok": False,
        },
        session_snapshot_fn=lambda: {
            "pty_processes": 2,
            "terminal_subscribers": 1,
            "terminal_overflows": 4,
        },
        event_snapshot_fn=lambda: {"subscribers": 2, "overflows": 5},
        process_rss_fn=lambda: 123456,
        process_metrics=operational.ProcessMetrics(),
        now=125.0,
    )

    assert snapshot["bootstrap"] == {
        "duration_ms": 20,
        "errors": 1,
        "complete": 1,
        "total": 2,
    }
    assert snapshot["pty"]["processes"] == 2
    assert snapshot["process"]["rss_bytes"] == 123456
    assert snapshot["credentials"]["state"] == "rotating"
    assert snapshot["credentials"]["source"] == "app_identity_oauth"
    assert snapshot["credentials"]["freshness_seconds"] == 25
    assert snapshot["entitlements"]["handoff_failures"] == 3
    assert snapshot["entitlements"] == {
        "enabled": True,
        "ok": False,
        "failure": True,
        "error_present": True,
        "freshness_seconds": 10,
        "handoff_failures": 3,
    }
    assert snapshot["websockets"]["current"] == 1
    assert snapshot["websockets"]["overflows"] == 4
    assert snapshot["websockets"]["event_subscribers"] == 2
    assert snapshot["websockets"]["event_overflows"] == 5
    assert "must-not-leak" not in repr(snapshot)
    assert "secret-bearing" not in repr(snapshot)


def test_entitlement_reconcile_failure_surfaces_without_failed_ledger_entries():
    snapshot = operational.build_snapshot(
        installer_status_fn=lambda: {"steps": {}},
        credential_status_fn=lambda: {},
        entitlement_status_fn=lambda: {
            "enabled": True,
            "ok": False,
            "last_error": "catalog list failed with bearer-secret",
            "last_reconcile": 110.0,
            "handoff": {"summary": {"failed": 0}},
        },
        session_snapshot_fn=lambda: {},
        event_snapshot_fn=lambda: {},
        process_rss_fn=lambda: None,
        process_metrics=operational.ProcessMetrics(),
        now=125.0,
    )

    assert snapshot["entitlements"] == {
        "enabled": True,
        "ok": False,
        "failure": True,
        "error_present": True,
        "freshness_seconds": 15,
        "handoff_failures": 0,
    }
    assert "bearer-secret" not in repr(snapshot)


def test_bootstrap_duration_uses_wall_clock_across_parallel_steps():
    snapshot = operational.build_snapshot(
        installer_status_fn=lambda: {
            "steps": {
                "node": {
                    "status": "complete",
                    "started_at": 10.0,
                    "completed_at": 20.0,
                    "duration_ms": 10000,
                },
                "skills": {
                    "status": "complete",
                    "started_at": 20.0,
                    "completed_at": 60.0,
                    "duration_ms": 40000,
                },
            }
        },
        credential_status_fn=lambda: {},
        entitlement_status_fn=lambda: {},
        session_snapshot_fn=lambda: {},
        event_snapshot_fn=lambda: {},
        process_rss_fn=lambda: None,
        process_metrics=operational.ProcessMetrics(),
        now=100.0,
    )

    assert snapshot["bootstrap"]["duration_ms"] == 50000


def test_reporter_emits_periodic_operational_health_with_injected_snapshot():
    emitted = []

    class Emitter:
        enabled = True

        @staticmethod
        def emit(event_type, attendee, payload):
            emitted.append((event_type, attendee, payload))

    reporter = operational.OperationalHealthReporter(
        Emitter(),
        snapshot_fn=lambda: {"bounded": True},
        interval=60,
    )

    assert reporter.emit_once() is True
    assert emitted == [
        ("operational.health", "system", {"bounded": True})
    ]


def test_reporter_is_noop_when_control_tower_ingest_is_disabled():
    class Emitter:
        enabled = False

        @staticmethod
        def emit(*args):
            raise AssertionError("disabled emitter must not be called")

    reporter = operational.OperationalHealthReporter(
        Emitter(), snapshot_fn=lambda: {}, interval=60
    )

    assert reporter.emit_once() is False


def test_http_middleware_records_operational_response_codes(client, monkeypatch):
    from server import main

    fresh = operational.ProcessMetrics()
    monkeypatch.setattr(main.operational, "metrics", fresh)
    monkeypatch.setattr(
        main.readiness,
        "evaluate_runtime",
        lambda: {"ready": False, "status": "not_ready", "checks": {}},
    )

    assert client.get("/readyz").status_code == 503
    assert fresh.snapshot()["http_responses"]["503"] == 1


def test_http_middleware_counts_409_429_and_503(monkeypatch):
    from server import main

    fresh = operational.ProcessMetrics()
    monkeypatch.setattr(main.operational, "metrics", fresh)

    async def exercise(status_code):
        async def call_next(_request):
            return Response(status_code=status_code)

        return await main.record_operational_http_status(None, call_next)

    for status_code in (409, 429, 503):
        assert asyncio.run(exercise(status_code)).status_code == status_code

    assert fresh.snapshot()["http_responses"] == {
        "409": 1,
        "429": 1,
        "503": 1,
    }


def test_metric_recording_failure_never_breaks_http_response(monkeypatch):
    from server import main

    class BrokenMetrics:
        @staticmethod
        def record_http(_status):
            raise RuntimeError("metrics unavailable")

    monkeypatch.setattr(main.operational, "metrics", BrokenMetrics())

    async def call_next(_request):
        return Response(status_code=204)

    response = asyncio.run(main.record_operational_http_status(None, call_next))
    assert response.status_code == 204


def test_control_tower_threads_are_retained_and_joined_on_shutdown():
    from server import main

    created = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            self.joined = []
            created.append(self)

        def start(self):
            self.started = True

        def join(self, timeout=None):
            self.joined.append(timeout)

    class Emitter:
        can_push = True
        run_id = "run"

    stop = __import__("threading").Event()
    threads = main._start_control_tower_threads(
        Emitter(),
        stop,
        thread_factory=FakeThread,
    )

    assert threads == created
    assert len(threads) == 2
    assert all(thread.started for thread in threads)

    main._stop_control_tower_threads(stop, threads, join_timeout=0.25)

    assert stop.is_set()
    assert all(thread.joined == [0.25] for thread in threads)


def test_health_sampling_runs_without_a_push_sink():
    """Delivery is by collection, so the sampler has to run regardless.

    The flusher is the only push-dependent thread: with no ingest endpoint it
    would wake every 15 seconds to POST at nothing, while the health snapshots it
    would have carried are still collected off the buffer by Control Tower.
    """
    from server import main

    created = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

        def start(self):
            pass

    class PullOnly:
        can_push = False
        run_id = ""

    threads = main._start_control_tower_threads(
        PullOnly(),
        __import__("threading").Event(),
        thread_factory=FakeThread,
    )

    assert [thread.kwargs["name"] for thread in threads] == ["operational-health"]


def test_lifespan_joins_control_tower_threads_when_app_exits_with_error(
    monkeypatch, tmp_path
):
    from server import main

    stopped = []
    monkeypatch.setattr(main.config, "local_dev", lambda: False)
    monkeypatch.setattr(main.config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(main.config, "session_state_path", lambda: "")
    monkeypatch.setattr(main, "initialize_app_identity", lambda: None)
    monkeypatch.setattr(main.install, "run_in_background", lambda: None)
    monkeypatch.setattr(main.credential_manager, "start", lambda: None)
    monkeypatch.setattr(main.entitlement_manager, "start", lambda: None)
    monkeypatch.setattr(main.entitlement_manager, "stop", lambda: None)
    monkeypatch.setattr(
        main,
        "event_emitter",
        type("Emitter", (), {"can_push": True, "run_id": "run", "stream_id": "s"})(),
    )
    monkeypatch.setattr(
        main,
        "_start_control_tower_threads",
        lambda emitter, stop: ["flusher", "reporter"],
    )
    monkeypatch.setattr(
        main,
        "_stop_control_tower_threads",
        lambda stop, threads: stopped.append((stop, threads)),
    )

    async def exercise():
        context = main.lifespan(main.app)
        await context.__aenter__()
        try:
            await context.__aexit__(RuntimeError, RuntimeError("boom"), None)
        except RuntimeError:
            pass

    asyncio.run(exercise())

    assert len(stopped) == 1
    assert stopped[0][1] == ["flusher", "reporter"]

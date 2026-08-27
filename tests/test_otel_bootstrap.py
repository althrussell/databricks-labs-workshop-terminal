from server import otel_bootstrap


def test_resource_attributes_merge_platform_and_workshop_identity():
    env = {
        "OTEL_RESOURCE_ATTRIBUTES": "workspace.id=123,app.name=wt,platform.key=kept",
        "OTEL_SERVICE_NAME": "wt",
        "WORKSHOP_RUN_ID": "run-1",
        "WORKSHOP_UNIT_ID": "unit-2",
        "EVENT_NAME": "BNE AI Dev Day",
        "WORKSHOP_RELEASE_SHA": "abc123",
    }

    merged = otel_bootstrap.parse_resource_attributes(
        otel_bootstrap.merged_resource_attributes(env)
    )

    assert merged == {
        "workspace.id": "123",
        "app.name": "wt",
        "platform.key": "kept",
        "workshop.run_id": "run-1",
        "workshop.unit_id": "unit-2",
        "workshop.event_name": "BNE AI Dev Day",
        "workshop.release_sha": "abc123",
        "databricks.workspace.id": "123",
        "service.name": "wt",
    }


def test_resource_values_are_percent_encoded_without_losing_platform_values():
    encoded = otel_bootstrap.merged_resource_attributes(
        {
            "OTEL_RESOURCE_ATTRIBUTES": "workspace.id=123,app.name=wt",
            "OTEL_SERVICE_NAME": "wt",
            "EVENT_NAME": "Retail, AI=Day",
        }
    )

    assert "Retail%2C%20AI%3DDay" in encoded
    assert otel_bootstrap.parse_resource_attributes(encoded)["workshop.event_name"] == (
        "Retail, AI=Day"
    )


def test_disabled_preview_starts_plain_uvicorn_without_inventing_a_collector():
    env = {"DATABRICKS_APP_PORT": "9001"}

    assert not otel_bootstrap.instrumentation_enabled(env)
    assert otel_bootstrap.uvicorn_command(env) == [
        "uvicorn",
        "server.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "9001",
        "--workers",
        "1",
    ]


def test_databricks_collector_environment_enables_documented_instrumentation():
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4314",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    }

    command = otel_bootstrap.uvicorn_command(env)

    assert command[0] == "opentelemetry-instrument"
    assert command[1:3] == ["uvicorn", "server.main:app"]
    assert "http://localhost:4314" not in command
    assert "grpc" not in command


def test_partial_or_unknown_collector_configuration_stays_fail_soft():
    assert not otel_bootstrap.instrumentation_enabled(
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4314"}
    )
    assert not otel_bootstrap.instrumentation_enabled(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4314",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "invented",
        }
    )
    assert not otel_bootstrap.instrumentation_enabled(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4314",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        }
    )

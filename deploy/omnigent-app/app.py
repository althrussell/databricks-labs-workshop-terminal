"""Dedicated Omnigent control plane for Databricks Apps.

This is a thin adaptation of upstream ``deploy/databricks/src/app.py`` from
Omnigent v0.7.0. It serves the published upstream UI and durable stores only;
native harnesses, PTYs, and working directories belong on an external host.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from volume_probe import probe_artifact_volume

logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
logger = logging.getLogger("omnigent-workshop-app")

if sys.version_info < (3, 12):
    raise RuntimeError("Omnigent 0.7.0 requires Python 3.12 or newer")

# Fallback lifetime when the credential response carries no usable expiry.
_TOKEN_TTL_SECONDS = 50 * 60
# Renew this far ahead of the reported expiry so an in-flight connect cannot
# present a credential that expires mid-handshake.
_TOKEN_RENEW_MARGIN_SECONDS = 5 * 60
_token_cache: dict[str, tuple[str, float]] = {}
_token_cache_lock = threading.Lock()


def _cache_ttl(expiration_time: str | None) -> float:
    """Seconds to trust a Lakebase credential, honoring its reported expiry.

    ``expiration_time`` is an optional ISO-8601 instant. A missing or malformed
    value falls back to the fixed lifetime; a credential already inside the
    renewal margin returns 0 so it is used once and never cached. Reading the
    real expiry keeps a shortened credential from silently breaking every later
    connection.
    """
    if not expiration_time:
        return _TOKEN_TTL_SECONDS
    try:
        expires_at = datetime.fromisoformat(expiration_time.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable Lakebase credential expiry; using fixed TTL")
        return _TOKEN_TTL_SECONDS
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    remaining = (
        expires_at - datetime.now(timezone.utc)
    ).total_seconds() - _TOKEN_RENEW_MARGIN_SECONDS
    if remaining <= 0:
        return 0.0
    return min(remaining, _TOKEN_TTL_SECONDS)


def _register_polly_variants(agent_store, artifact_store, agent_cache) -> None:
    """Register the workshop's model-set Polly variants as template agents.

    Registration is what puts a variant in the App's new-session picker: the
    picker lists ``kind=template`` rows, and only startup registration creates
    them. Stock ``polly`` is untouched and remains the default.

    Deliberately fail-soft. App health gates the whole attendee deployment, and
    a bad model pin or an upstream bundle move must degrade the workshop to
    "stock polly only" rather than take the control plane down with it.

    This reuses Omnigent's own private ``_preregister_agent`` rather than
    reimplementing it. That function's idempotency is load-bearing in ways worth
    inheriting instead of paraphrasing: it reuses an existing row's ``agent_id``
    (a delete/recreate would cascade through the tasks FK and break
    ``--continue``), only rewrites ``bundle_location`` when the content hash
    actually changed so restarts are no-ops, and swaps the agent cache's
    extracted bundle in lockstep so the next request cannot serve a stale spec.
    The coupling to a private symbol is acceptable only because this deployment
    pins Omnigent to exactly 0.7.0; the guard below is what keeps a future
    upgrade from turning a moved symbol into a failed boot.
    """
    if os.environ.get("WORKSHOP_POLLY_VARIANTS", "true").strip().lower() != "true":
        logger.info("Polly variants disabled; stock polly remains the only agent")
        return
    try:
        from omnigent.cli import _preregister_agent

        import polly_variants

        with tempfile.TemporaryDirectory(prefix="polly_variants_") as tmp:
            for bundle in polly_variants.build(Path(tmp)):
                _preregister_agent(bundle, agent_store, artifact_store, agent_cache)
    except Exception:  # noqa: BLE001 — a variant must never fail App startup
        logger.warning(
            "Polly variant registration failed; stock polly still available:\n%s",
            traceback.format_exc(),
        )


try:
    import sqlalchemy
    import uvicorn
    from databricks.sdk import WorkspaceClient

    from omnigent.db.utils import _run_migrations as _run_alembic_upgrade
    from omnigent.runtime import init as init_runtime
    from omnigent.runtime import telemetry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.app import create_app
    from omnigent.server.auth import create_auth_provider
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.databricks_volumes import (
        DatabricksVolumesArtifactStore,
    )
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )
    from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    LAKEBASE_ENDPOINT = os.environ["AP_LAKEBASE_ENDPOINT"]
    VOLUME_PATH = os.environ["AP_ARTIFACT_VOLUME_PATH"]
    PGHOST = os.environ["PGHOST"]
    PGDATABASE = os.environ["PGDATABASE"]
    PGUSER = os.environ["PGUSER"]
    PGPORT = os.environ.get("PGPORT", "5432")
    PGSSLMODE = os.environ.get("PGSSLMODE", "require")
    PORT = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    POOL_RECYCLE_SECONDS = int(os.environ.get("AP_POOL_RECYCLE_SECONDS", "300"))

    _workspace_client = WorkspaceClient()

    def _get_cached_token(endpoint: str) -> str:
        now = time.monotonic()
        with _token_cache_lock:
            cached = _token_cache.get(endpoint)
            if cached is not None and cached[1] > now:
                return cached[0]
        credential = _workspace_client.postgres.generate_database_credential(
            endpoint=endpoint,
        )
        if credential.token is None:
            raise RuntimeError("Lakebase credential response did not include a token")
        ttl = _cache_ttl(getattr(credential, "expiration_time", None))
        with _token_cache_lock:
            if ttl > 0:
                _token_cache[endpoint] = (credential.token, now + ttl)
            else:
                # Already inside the renewal margin: use it once, never cache it.
                _token_cache.pop(endpoint, None)
        return credential.token

    @sqlalchemy.event.listens_for(sqlalchemy.engine.Engine, "do_connect")
    def _inject_lakebase_credentials(_dialect, _conn_rec, _cargs, cparams):
        if cparams.get("host") != PGHOST:
            return
        cparams["password"] = _get_cached_token(LAKEBASE_ENDPOINT)
        cparams["sslmode"] = PGSSLMODE

    telemetry.init()

    DB_URI = f"postgresql+psycopg://{PGUSER}@{PGHOST}:{PGPORT}/{PGDATABASE}"
    ARTIFACT_URI = f"dbfs:{VOLUME_PATH}"
    CACHE_DIR = Path(tempfile.mkdtemp(prefix="omnigent_control_plane_"))

    # Health is meaningful only after the app's own SP identity has proven
    # durable write access to the configured artifact Volume. Apps does not mount
    # volumes, so this goes through the Files API rather than the filesystem.
    probe_artifact_volume(VOLUME_PATH, _workspace_client)

    migration_engine = sqlalchemy.create_engine(
        DB_URI,
        pool_recycle=POOL_RECYCLE_SECONDS,
    )
    try:
        _run_alembic_upgrade(migration_engine, DB_URI)
    finally:
        migration_engine.dispose()

    agent_store = SqlAlchemyAgentStore(DB_URI)
    file_store = SqlAlchemyFileStore(DB_URI)
    conversation_store = SqlAlchemyConversationStore(DB_URI)
    artifact_store = DatabricksVolumesArtifactStore(ARTIFACT_URI)
    comment_store = SqlAlchemyCommentStore(DB_URI)
    permission_store = SqlAlchemyPermissionStore(DB_URI)
    policy_store = SqlAlchemyPolicyStore(DB_URI)
    scheduled_task_store = SqlAlchemyScheduledTaskStore(DB_URI)
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=CACHE_DIR)

    # HostStore is the durable control-plane registry/router for external
    # hosts. Constructing it does not start a local host or runner process.
    host_store = HostStore(DB_URI)

    init_runtime(
        agent_cache=agent_cache,
        caps=RuntimeCaps(),
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
        policy_store=policy_store,
    )

    _register_polly_variants(agent_store, artifact_store, agent_cache)

    # Safe only behind the Databricks Apps proxy, which strips client-supplied
    # identity headers and injects the authenticated workspace identity.
    os.environ["OMNIGENT_AUTH_PROVIDER"] = "header"
    auth_provider = create_auth_provider()
    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        comment_store=comment_store,
        permission_store=permission_store,
        policy_store=policy_store,
        host_store=host_store,
        scheduled_task_store=scheduled_task_store,
        auth_provider=auth_provider,
    )

    if __name__ == "__main__":
        logger.info(
            "Starting Omnigent control plane on 0.0.0.0:%d",
            PORT,
        )
        uvicorn.run(app, host="0.0.0.0", port=PORT)

except Exception:  # noqa: BLE001 - startup failures must reach Apps logs
    logger.error("FATAL: Omnigent failed to start:\n%s", traceback.format_exc())
    time.sleep(30)
    raise

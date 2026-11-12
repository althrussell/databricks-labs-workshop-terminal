# Calling models from an App

An App calls models as the signed-in attendee, never as its own service principal.
The attendee's token (OBO) is what the Unity AI Gateway checks, and the attendee already
holds EXECUTE on the event's model pool. The App's identity has no such grant.

## What the deploy sets up

- `workshop-app-deploy` declares the `ai-gateway` and `genie` user scopes
  (`user_api_scopes`) on the App before its first deploy, so the attendee's one Allow click covers both.
- An existing App that lacks either scope gets it on the next deploy. Tell the attendee:
  reopen the App and click Allow. Model and Genie calls fail until they do.
- Declare no `uc_securable` MODEL_SERVICE resource. It grants the App's service principal,
  which is not the caller.

## Environment

- `DATABRICKS_SERVING_BASE_URL`: `https://<workspace>/ai-gateway/mlflow/v1`.
- `DATABRICKS_MODEL`: `system.ai.claude-sonnet-5-5`, the workshop default for App features (pick another
  `system.ai.<name>` only if the attendee asks). Set it in `app.yml` (and `.env` for local `apx dev`).

## APX backend (FastAPI)

The `Dependencies.UserClient` dependency is the attendee's client. Use its token for the
gateway call; the OpenAI client is OpenAI-compatible and runs on the gateway path.

```python
import os

from openai import OpenAI
from pydantic import BaseModel

from .core import Dependencies, create_router

router = create_router()


class QuestionIn(BaseModel):
    question: str


class AnswerOut(BaseModel):
    answer: str


@router.post("/ask", response_model=AnswerOut, operation_id="askModel")
def ask(body: QuestionIn, user_ws: Dependencies.UserClient) -> AnswerOut:
    # The signed-in attendee's bearer token, not the App's credentials.
    token = user_ws.config.authenticate()["Authorization"].removeprefix("Bearer ")
    client = OpenAI(
        api_key=token,
        base_url=f"{user_ws.config.host.rstrip('/')}/ai-gateway/mlflow/v1",
    )
    reply = client.chat.completions.create(
        model=os.environ.get("DATABRICKS_MODEL", "system.ai.claude-sonnet-5-5"),
        messages=[{"role": "user", "content": body.question}],
        max_tokens=512,
    )
    return AnswerOut(answer=reply.choices[0].message.content or "")
```

Keep the OpenAI call inside the route, and add `openai` with `uv add openai`.

## Other frameworks

Read the forwarded header `X-Forwarded-Access-Token` and use it as the bearer token for
`<host>/ai-gateway/mlflow/v1`. A complete Streamlit example is in the
`databricks-apps-python` skill: `examples/fm-minimal-chat.py` and `examples/llm_config.py`
(pass the token as `user_token`).

## Genie

Call Genie with the attendee's token, the same way as a model: `Dependencies.UserClient`
in APX, or `X-Forwarded-Access-Token` elsewhere. The deploy tool declares the `genie` scope.
What Genie returns is then governed by the attendee's own grants.

## Background work and Agent Bricks

- Background work with no signed-in user runs as the App. The automatic access Control Tower
  grants the App is what it relies on, the same as Agent Bricks. Do not add a grant to the App.
- Agent Bricks agents (`agentbricks deploy`) call models as the App. If the model does not
  exist right after deploy, wait 2 minutes and retry. Never grant it yourself.

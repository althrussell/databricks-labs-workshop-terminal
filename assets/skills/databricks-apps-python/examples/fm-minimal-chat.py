"""
Minimal Databricks Foundation Model Chat App

A complete, deployable Streamlit app that calls a Unity Catalog model service
as the signed-in user. Workshop edition: model calls use the attendee's token,
never the App's service principal, and the app declares no MODEL_SERVICE resource.

Features:
- The attendee's token from the X-Forwarded-Access-Token header (OBO)
- OpenAI SDK wired to the Unity AI Gateway at /ai-gateway/mlflow/v1
- Multi-turn chat with conversation history
- Viewer identity display
- Latency tracking

Local Development (no signed-in user, so set a token for yourself):
    export DATABRICKS_TOKEN="dapi..."
    export DATABRICKS_SERVING_BASE_URL="https://<workspace>/ai-gateway/mlflow/v1"
    export DATABRICKS_MODEL="system.ai.claude-sonnet-5-5"
    streamlit run fm-minimal-chat.py

Databricks Apps deployment:
    1. Declare the ai-gateway user scope on the app (workshop-app-deploy does this
       for databricks.yml; the attendee clicks Allow once on first open):
       resources:
         apps:
           chat_app:
             name: chat-app
             source_code_path: ./
             user_api_scopes:
               - ai-gateway

       Do NOT add a uc_securable MODEL_SERVICE resource. The app's service principal
       is not the caller, so a grant to it does not authorise these calls.

    2. Set the environment in app.yaml. DATABRICKS_MODEL is
       system.ai.claude-sonnet-5-5, the workshop default for App features. The Apps
       runtime sets DATABRICKS_HOST for you.
       command: ["streamlit", "run", "fm-minimal-chat.py"]
       env:
         - name: DATABRICKS_SERVING_BASE_URL
           value: "https://<workspace>/ai-gateway/mlflow/v1"
         - name: DATABRICKS_MODEL
           value: "system.ai.claude-sonnet-5-5"

    3. Create requirements.txt:
       streamlit>=1.38,<2.0
       openai>=1.30,<2.0

    4. Deploy, then start the app:
       workshop-app-deploy (the deploy_databricks_app MCP tool)
"""

import os
import time
from typing import Dict, List, Optional, Tuple

import streamlit as st
from openai import OpenAI

from llm_config import create_foundation_model_client, get_model_name


def _get_forwarded_headers() -> Dict[str, str]:
    try:
        return dict(getattr(st, "context").headers)
    except Exception:
        return {}


def get_viewer_identity() -> Tuple[Optional[str], Optional[str]]:
    """The signed-in user's email and access token, as forwarded by the Apps proxy."""
    headers = _get_forwarded_headers()
    email = headers.get("X-Forwarded-Email") or headers.get("x-forwarded-email")
    token = headers.get("X-Forwarded-Access-Token") or headers.get(
        "x-forwarded-access-token"
    )
    return email, token


# =============================================================================
# LLM Helper
# =============================================================================
def llm_chat(
    client: OpenAI,
    *,
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int = 1000,
    temperature: float = 0.7,
) -> Tuple[str, int]:
    """Call the model and return (response, latency_ms)."""
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    content = resp.choices[0].message.content or ""
    return content, elapsed_ms


# =============================================================================
# Streamlit App
# =============================================================================
def main():
    st.set_page_config(
        page_title="Databricks Foundation Model Chat",
        page_icon="💬",
        layout="centered",
    )

    st.title("💬 Foundation Model Chat")
    st.caption("Powered by Databricks Apps")

    # Sidebar: viewer identity
    viewer_email, viewer_token = get_viewer_identity()
    if viewer_email:
        st.sidebar.success(f"Logged in as: {viewer_email}")
    else:
        st.sidebar.info("Local dev mode (no viewer identity)")

    # Sidebar: model config
    with st.sidebar:
        st.subheader("Configuration")
        st.code(f"Model: {get_model_name()}", language=None)

        if st.button("🗑️ Clear Chat History"):
            st.session_state.messages = []
            st.rerun()

        with st.expander("ℹ️ About"):
            st.markdown(
                """
                This app calls a Unity Catalog model service as the signed-in
                user, with the token Databricks Apps forwards for that user.
                """
            )

    # Initialize chat history
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("latency_ms"):
                st.caption(f"⏱️ {message['latency_ms']}ms")

    # Chat input
    if prompt := st.chat_input("Ask me anything..."):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Generate assistant response
        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    if viewer_token:
                        # The attendee's token, not the app's service principal.
                        client = create_foundation_model_client(user_token=viewer_token)
                    elif os.environ.get("DATABRICKS_TOKEN"):
                        # Local development only: a personal token stands in for the signed-in user.
                        client = create_foundation_model_client()
                    else:
                        raise RuntimeError(
                            "No signed-in token was forwarded. Open the app from its "
                            "Databricks Apps URL, then try again."
                        )

                    # Call foundation model
                    response, latency_ms = llm_chat(
                        client,
                        model=get_model_name(),
                        messages=st.session_state.messages,
                        max_tokens=1000,
                        temperature=0.7,
                    )

                    # Display response
                    st.markdown(response)
                    st.caption(f"⏱️ {latency_ms}ms")

                    # Add to chat history
                    st.session_state.messages.append(
                        {
                            "role": "assistant",
                            "content": response,
                            "latency_ms": latency_ms,
                        }
                    )

                except Exception as e:
                    st.error(f"Error calling foundation model: {e}")
                    st.session_state.messages.pop()  # Remove failed user message


if __name__ == "__main__":
    main()

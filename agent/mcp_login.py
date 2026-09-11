"""One-time browser authorization for the hosted Grafana Cloud MCP endpoint.

    uv run python -m agent.mcp_login

The hosted ``https://mcp.grafana.com/mcp`` endpoint is OAuth 2.1 only -- there is
no service-account token (PROJECT.md, "Grafana MCP: two modes"). This runs the interactive
Authorization Code + PKCE flow with dynamic client registration, using the
``mcp`` library's own OAuth client, and drops the resulting bearer token at
``.secrets/grafana-cloud-mcp-token`` so ``TURNAROUND_MCP_MODE=hosted`` runs pick
it up (``agent/config.py``). The default ``oss`` mode never needs this.

The flow, on one machine with a browser:

1. dynamic client registration against mcp.grafana.com
2. browser opens the Grafana consent screen (choose read-only or read-write)
3. Grafana redirects to ``http://localhost:41999/callback`` with the code
4. the code is exchanged for tokens; the access token is written to disk
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

from agent.config import CLOUD_MCP_TOKEN_FILE, HOSTED_MCP_URL

_CALLBACK_PORT = 41999
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}/callback"
_STORE_DIR = Path(__file__).resolve().parent.parent / ".secrets"
_CLIENT_FILE = _STORE_DIR / "grafana-cloud-mcp-client.json"
_TOKENS_FILE = _STORE_DIR / "grafana-cloud-mcp-tokens.json"


class _FileTokenStorage:
    """``mcp.client.auth.TokenStorage`` backed by two JSON files under .secrets/."""

    def __init__(self) -> None:
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

        self._Token = OAuthToken
        self._Client = OAuthClientInformationFull

    async def get_tokens(self):
        if _TOKENS_FILE.is_file():
            return self._Token.model_validate_json(_TOKENS_FILE.read_text())
        return None

    async def set_tokens(self, tokens) -> None:
        _STORE_DIR.mkdir(parents=True, exist_ok=True)
        _TOKENS_FILE.write_text(tokens.model_dump_json())
        CLOUD_MCP_TOKEN_FILE.write_text(tokens.access_token.strip() + "\n")

    async def get_client_info(self):
        if _CLIENT_FILE.is_file():
            return self._Client.model_validate_json(_CLIENT_FILE.read_text())
        return None

    async def set_client_info(self, client_info) -> None:
        _STORE_DIR.mkdir(parents=True, exist_ok=True)
        _CLIENT_FILE.write_text(client_info.model_dump_json())


def _wait_for_callback() -> tuple[str, str | None]:
    """Serve exactly one request on the redirect URI and return (code, state)."""
    captured: dict[str, str | None] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            captured["code"] = (params.get("code") or [""])[0]
            captured["state"] = (params.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            ok = bool(captured["code"])
            self.wfile.write(
                b"<h3>Authorized. You can close this tab and return to the terminal.</h3>"
                if ok else b"<h3>No authorization code in the redirect.</h3>"
            )

        def log_message(self, *_args) -> None:  # keep the console clean
            return

    server = http.server.HTTPServer(("localhost", _CALLBACK_PORT), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        while "code" not in captured:
            pass
    finally:
        server.server_close()
    if not captured["code"]:
        raise RuntimeError("authorization was cancelled or returned no code")
    return captured["code"], captured["state"]


async def _run() -> int:
    try:
        from mcp import ClientSession
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.shared.auth import OAuthClientMetadata
    except Exception as exc:  # noqa: BLE001
        print(f"the installed 'mcp' package does not expose the OAuth client: {exc}",
              file=sys.stderr)
        return 2

    async def redirect_handler(url: str) -> None:
        print("\nOpen this URL to authorize (choose read-only unless you need "
              "the Remediator to write):\n\n  " + url + "\n")
        webbrowser.open(url)

    async def callback_handler() -> tuple[str, str | None]:
        print(f"waiting for the redirect to {_REDIRECT_URI} ...")
        return await asyncio.get_event_loop().run_in_executor(None, _wait_for_callback)

    provider = OAuthClientProvider(
        server_url=HOSTED_MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Turnaround",
            redirect_uris=[_REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="read",
        ),
        storage=_FileTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    async with (
        streamablehttp_client(HOSTED_MCP_URL, auth=provider) as (r, w, _),
        ClientSession(r, w) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
    print(f"\nauthorized. {len(tools.tools)} tools visible. token written to "
          f"{CLOUD_MCP_TOKEN_FILE.relative_to(Path.cwd())}")
    print("now run with:  TURNAROUND_MCP_MODE=hosted uv run python -m agent.run \"...\"")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()

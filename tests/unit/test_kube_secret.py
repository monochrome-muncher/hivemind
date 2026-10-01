"""The in-cluster Secret client the key bootstrap uses (ADR 0044).

Runs against a local plain-HTTP stand-in for the API server: no cluster,
no network. Pins the request shape, the status mapping (an error is never
read as "absent"), and that no Secret value ever reaches an exception.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from hivemind.store.kube_secret import KubeApiError, KubeSecrets


class _Api:
    def __init__(self) -> None:
        self.status = 200
        self.requests: list[dict[str, Any]] = []


@pytest.fixture
def api() -> Iterator[tuple[_Api, str]]:
    state = _Api()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            state.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(body) if body else None,
                }
            )
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"kind":"Status","message":"secret value hm_should_not_leak"}')

        do_GET = do_POST = do_DELETE = _answer

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _client(base: str) -> KubeSecrets:
    return KubeSecrets(base, "hivemind", "sa-token")


def test_exists_maps_200_and_404(api: tuple[_Api, str]) -> None:
    state, base = api
    assert _client(base).exists("hivemind-keys") is True
    state.status = 404
    assert _client(base).exists("hivemind-keys") is False
    assert state.requests[0] == {
        "method": "GET",
        "path": "/api/v1/namespaces/hivemind/secrets/hivemind-keys",
        "auth": "Bearer sa-token",
        "body": None,
    }


@pytest.mark.parametrize("status", [401, 403, 500])
def test_exists_raises_on_any_other_status(api: tuple[_Api, str], status: int) -> None:
    # "Absent" on an RBAC or API error would start a second bootstrap.
    state, base = api
    state.status = status
    with pytest.raises(KubeApiError, match=f"HTTP {status}"):
        _client(base).exists("hivemind-keys")


def test_create_posts_an_opaque_secret(api: tuple[_Api, str]) -> None:
    state, base = api
    state.status = 201
    _client(base).create("hivemind-keys", {"ADMIN_KEY": "hm_a"}, {"app": "x"})
    (req,) = state.requests
    assert req["method"] == "POST"
    assert req["path"] == "/api/v1/namespaces/hivemind/secrets"
    assert req["body"] == {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": "hivemind-keys", "labels": {"app": "x"}},
        "stringData": {"ADMIN_KEY": "hm_a"},
    }


@pytest.mark.parametrize(("status", "reason"), [(409, "already exists"), (403, "unexpected")])
def test_create_failure_never_carries_secret_data(
    api: tuple[_Api, str], status: int, reason: str
) -> None:
    state, base = api
    state.status = status
    with pytest.raises(KubeApiError) as info:
        _client(base).create("hivemind-keys", {"ADMIN_KEY": "hm_secret_value"}, {})
    assert reason in str(info.value)
    assert "hm_" not in str(info.value)


@pytest.mark.parametrize("status", [200, 202, 404])
def test_delete_accepts_done_and_gone(api: tuple[_Api, str], status: int) -> None:
    state, base = api
    state.status = status
    _client(base).delete("hivemind-keys")
    assert state.requests[0]["method"] == "DELETE"


def test_unreachable_api_is_an_error_not_absent() -> None:
    with pytest.raises(KubeApiError, match="no response"):
        KubeSecrets("http://127.0.0.1:9", "ns", "t").exists("hivemind-keys")


def test_in_cluster_reads_the_service_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: tuple[_Api, str]
) -> None:
    state, base = api
    (tmp_path / "token").write_text("projected-token\n")
    (tmp_path / "namespace").write_text("hm-prod\n")
    (tmp_path / "ca.crt").write_text("unused")
    cafiles: list[str] = []

    def fake_context(*, cafile: str) -> None:
        cafiles.append(cafile)

    monkeypatch.setattr("hivemind.store.kube_secret.ssl.create_default_context", fake_context)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "6443")
    client = KubeSecrets.in_cluster(tmp_path)
    assert cafiles == [str(tmp_path / "ca.crt")]
    assert client._url("x") == "https://10.0.0.1:6443/api/v1/namespaces/hm-prod/secrets/x"
    # The token and namespace drive the request (pointed at the stand-in API).
    client._base = base
    client._ssl = None
    client.exists("hivemind-keys")
    assert state.requests[0]["auth"] == "Bearer projected-token"
    assert state.requests[0]["path"] == "/api/v1/namespaces/hm-prod/secrets/hivemind-keys"


def test_in_cluster_without_a_token_says_where_to_run(tmp_path: Path) -> None:
    with pytest.raises(KubeApiError, match="bootstrap Job"):
        KubeSecrets.in_cluster(tmp_path)

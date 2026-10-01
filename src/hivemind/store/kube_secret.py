"""A minimal in-cluster client for one Kubernetes Secret (ADR 0044).

The first-run key bootstrap (``hivemind-keys bootstrap-secret``) writes the
raw admin and org keys straight into the ``hivemind-keys`` Secret instead
of printing them to its pod log, where a cluster log shipper would keep
them. This module is the only Kubernetes API code in the image: stdlib
``urllib`` against the API server, authenticated by the pod's projected
ServiceAccount token, so the image gains no client library.

It deliberately never puts a Secret's data in an exception or a log line:
errors carry the HTTP status and the API's ``reason`` only.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_TIMEOUT_SECONDS = 30.0


class KubeApiError(Exception):
    """A Kubernetes API call failed (status and reason only, never data)."""

    def __init__(self, action: str, status: int | None, reason: str) -> None:
        where = f"HTTP {status}" if status is not None else "no response"
        super().__init__(f"Kubernetes API {action} failed: {where} ({reason})")
        self.status = status


class KubeSecrets:
    """Get / create / delete Secrets in one namespace."""

    def __init__(
        self,
        api_base: str,
        namespace: str,
        token: str,
        *,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._base = api_base.rstrip("/")
        self._namespace = namespace
        self._token = token
        self._ssl = ssl_context

    @classmethod
    def in_cluster(cls, sa_dir: Path = SERVICE_ACCOUNT_DIR) -> KubeSecrets:
        """Build from the pod's mounted ServiceAccount (token, namespace,
        cluster CA) and the ``KUBERNETES_SERVICE_*`` variables the kubelet
        always injects (``enableServiceLinks: false`` does not remove them)."""
        token_file = sa_dir / "token"
        if not token_file.is_file():
            raise KubeApiError(
                "setup",
                None,
                f"no ServiceAccount token at {token_file}: run this inside the bootstrap Job "
                "(deploy/kubernetes/bootstrap/), which mounts one",
            )
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # an IPv6 service address
        context = ssl.create_default_context(cafile=str(sa_dir / "ca.crt"))
        return cls(
            f"https://{host}:{port}",
            (sa_dir / "namespace").read_text().strip(),
            token_file.read_text().strip(),
            ssl_context=context,
        )

    def _url(self, name: str | None = None) -> str:
        url = f"{self._base}/api/v1/namespaces/{self._namespace}/secrets"
        return f"{url}/{name}" if name is not None else url

    def _call(self, action: str, method: str, url: str, body: dict[str, Any] | None) -> int:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS, context=self._ssl) as r:
                return int(r.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise KubeApiError(action, None, type(reason).__name__) from None

    def exists(self, name: str) -> bool:
        """``True`` for 200, ``False`` for 404; anything else raises, so an
        API or RBAC error never reads as "absent" (which would re-issue keys)."""
        status = self._call("get secret", "GET", self._url(name), None)
        if status == 200:
            return True
        if status == 404:
            return False
        raise KubeApiError("get secret", status, "unexpected status")

    def create(self, name: str, string_data: dict[str, str], labels: dict[str, str]) -> None:
        """Create an Opaque Secret; a 409 (it already exists) raises."""
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {"name": name, "labels": labels},
            "stringData": string_data,
        }
        status = self._call("create secret", "POST", self._url(), body)
        if status != 201:
            reason = "already exists" if status == 409 else "unexpected status"
            raise KubeApiError("create secret", status, reason)

    def delete(self, name: str) -> None:
        """Delete a Secret; a 404 counts as done."""
        status = self._call("delete secret", "DELETE", self._url(name), None)
        if status not in (200, 202, 404):
            raise KubeApiError("delete secret", status, "unexpected status")

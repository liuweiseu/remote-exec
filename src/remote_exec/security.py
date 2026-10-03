"""Authentication and transport security helpers shared by server and client.

Two independent mechanisms, which can be combined:

* Bearer token: a shared secret read from a file, sent by the client as
  ``authorization: Bearer <token>`` metadata and checked in constant time.
* TLS / mTLS: server certificate, optionally requiring client certificates
  signed by a given CA.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import stat
from pathlib import Path

import grpc

log = logging.getLogger(__name__)
audit = logging.getLogger("remote_exec.audit")

AUTH_METADATA_KEY = "authorization"
BEARER_PREFIX = "Bearer "


class SecurityError(Exception):
    pass


def read_secret_file(path: str | os.PathLike, what: str = "token") -> str:
    p = Path(path).expanduser()
    try:
        st = p.stat()
        value = p.read_text().strip()
    except OSError as exc:
        raise SecurityError(f"cannot read {what} file {p}: {exc.strerror or exc}") from exc
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("%s file %s is accessible by group/others; consider chmod 600", what, p)
    if not value:
        raise SecurityError(f"{what} file {p} is empty")
    return value


def read_bytes(path: str | os.PathLike, what: str) -> bytes:
    p = Path(path).expanduser()
    try:
        return p.read_bytes()
    except OSError as exc:
        raise SecurityError(f"cannot read {what} {p}: {exc.strerror or exc}") from exc


def is_loopback_host(host: str) -> bool:
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------- server side


def server_credentials(cert: str, key: str, client_ca: str | None) -> grpc.ServerCredentials:
    """TLS credentials; with ``client_ca`` client certificates are required (mTLS)."""
    return grpc.ssl_server_credentials(
        [(read_bytes(key, "TLS key"), read_bytes(cert, "TLS certificate"))],
        root_certificates=read_bytes(client_ca, "client CA") if client_ca else None,
        require_client_auth=bool(client_ca),
    )


class TokenAuthInterceptor(grpc.aio.ServerInterceptor):
    """Rejects calls that do not carry the expected bearer token."""

    def __init__(self, token: str):
        self._expected = (BEARER_PREFIX + token).encode()

    def _authorized(self, metadata) -> bool:
        for key, value in metadata or ():
            if key == AUTH_METADATA_KEY:
                got = value.encode() if isinstance(value, str) else value
                return hmac.compare_digest(got, self._expected)
        return False

    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        if handler is None or self._authorized(handler_call_details.invocation_metadata):
            return handler
        audit.warning("auth_failed method=%s", handler_call_details.method)
        return _deny_handler(handler)


def _deny_handler(handler: grpc.RpcMethodHandler) -> grpc.RpcMethodHandler:
    async def deny(request_or_iterator, context):
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid authentication token")

    kwargs = dict(
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )
    if handler.unary_unary:
        return grpc.unary_unary_rpc_method_handler(deny, **kwargs)
    if handler.unary_stream:
        return grpc.unary_stream_rpc_method_handler(deny, **kwargs)
    if handler.stream_unary:
        return grpc.stream_unary_rpc_method_handler(deny, **kwargs)
    return grpc.stream_stream_rpc_method_handler(deny, **kwargs)


def peer_identity(context: grpc.aio.ServicerContext) -> str:
    """Best-effort description of the caller for audit logs."""
    peer = context.peer() or "unknown"
    try:
        auth = context.auth_context() or {}
    except Exception:  # pragma: no cover - defensive
        auth = {}
    cn = auth.get("x509_common_name")
    if cn:
        return f"{peer} cn={cn[0].decode(errors='replace')}"
    return peer


# ---------------------------------------------------------------- client side


def channel_credentials(
    ca_cert: str | None, client_cert: str | None, client_key: str | None
) -> grpc.ChannelCredentials:
    if bool(client_cert) != bool(client_key):
        raise SecurityError("client_cert and client_key must be given together")
    return grpc.ssl_channel_credentials(
        root_certificates=read_bytes(ca_cert, "CA certificate") if ca_cert else None,
        private_key=read_bytes(client_key, "client key") if client_key else None,
        certificate_chain=read_bytes(client_cert, "client certificate") if client_cert else None,
    )


def auth_metadata(token: str | None) -> tuple[tuple[str, str], ...]:
    return ((AUTH_METADATA_KEY, BEARER_PREFIX + token),) if token else ()

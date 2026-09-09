"""Browser-origin trust decisions for live transcript WebSocket handshakes."""

from dataclasses import dataclass
from ipaddress import IPv6Address
import re


@dataclass(frozen=True)
class WebSocketOriginPolicy:
    """Trust the request Host and explicitly configured, exact HTTP(S) origins."""

    additional_origins: frozenset[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "additional_origins", frozenset(
            self._normalize_origin(origin) for origin in self.additional_origins
        ))

    @staticmethod
    def _normalize_origin(origin: str) -> str:
        # Match the entire serialized origin before parsing. URL parsers can
        # silently strip controls or tolerate malformed IPv6 authority suffixes.
        match = re.fullmatch(
            r"(https?)://(\[[0-9a-f:.]+\]|[a-z0-9_.-]+)(?::([0-9]+))?",
            origin, flags=re.IGNORECASE | re.ASCII,
        )
        if match is None:
            raise ValueError("Expected an exact HTTP(S) origin without a path")
        scheme, host, port_text = match.groups()
        scheme, host = scheme.lower(), host.lower()
        if host.startswith("["):
            host = f"[{IPv6Address(host[1:-1]).compressed}]"
        port = int(port_text) if port_text is not None else (443 if scheme == "https" else 80)
        if not 0 <= port <= 65535:
            raise ValueError("Origin port must be between 0 and 65535")
        return f"{scheme}://{host}:{port}"

    def allows(self, scope) -> bool:
        """Reject ambiguous browser headers; absent Origin supports native clients."""
        headers = scope.get("headers", [])
        origins = [value for name, value in headers if name.lower() == b"origin"]
        if not origins:
            return True
        hosts = [value for name, value in headers if name.lower() == b"host"]
        if len(origins) != 1 or len(hosts) != 1:
            return False
        try:
            origin = self._normalize_origin(origins[0].decode("ascii"))
            # Use the browser's scheme for defaults, including TLS termination
            # at a proxy. Forwarded headers and the internal server do not establish trust.
            scheme = origin.split(":", 1)[0]
            host_origin = self._normalize_origin(f"{scheme}://{hosts[0].decode('ascii')}")
        except (ValueError, UnicodeDecodeError):
            return False
        return origin == host_origin or origin in self.additional_origins

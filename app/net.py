"""Fetching a URL that came from outside, without letting it point the server at this network.

The gateway is public, and the machine it runs on sits in a private network next to services that
trust anyone who can reach them: Ollama, ComfyUI, the database, Jenkins. Any URL the server fetches
because a user, a search result, a web page or a model named it is a way in unless every hop of it
is checked. This is the one place that does that; nothing that fetches such a URL should use httpx
directly.

For every hop - the first URL and each redirect - the scheme must be http or https, the port 80 or
443, and every address the name resolves to a public one. The connection then goes to the address
that was checked (the name is kept for the Host header, TLS and the certificate check), so a DNS
answer that changes between the check and the connection - rebinding - cannot reach the LAN either.
Redirects are followed here, one at a time, never by httpx. Bodies are read up to a limit.
"""
import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass

import httpx

log = logging.getLogger("net")

PORTS = {80, 443}
HOPS = 3
# NAT64 prefixes put an IPv4 address inside a global-looking IPv6 one
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


class Refused(Exception):
    """The URL leads somewhere the server must not go."""


@dataclass
class Fetched:
    url: str                 # where the body came from, after redirects
    status_code: int
    headers: httpx.Headers
    content: bytes
    encoding: str
    truncated: bool

    @property
    def content_type(self):
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8", errors="replace")


def is_public_address(ip):
    ip = ipaddress.ip_address(str(ip).split("%")[0])
    if ip.version == 6:
        if ip.ipv4_mapped:
            return is_public_address(ip.ipv4_mapped)
        if ip.sixtofour or ip.teredo or any(ip in net for net in _NAT64):
            return False
    return ip.is_global and not ip.is_multicast


def _resolve_blocking(host):
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except (socket.gaierror, UnicodeError, OSError):
        return set()


async def resolve(host):
    """One public address for this host name, or Refused. Every address it has must be public: a
    name that resolves to a public and a private address is refused rather than half-trusted."""
    addresses = await asyncio.to_thread(_resolve_blocking, host)
    if not addresses:
        raise Refused(f"{host} does not resolve")
    for address in addresses:
        if not is_public_address(address):
            raise Refused(f"{host} resolves to a private address")
    # IPv4 first: this network's outside connectivity is IPv4
    return sorted(addresses, key=lambda a: ":" in a)[0]


def is_public_host(host):
    """For callers that only need a yes or no (blocking; run it in a thread)."""
    addresses = _resolve_blocking(host)
    return bool(addresses) and all(is_public_address(a) for a in addresses)


def check_url(url):
    """The URL parsed, if its scheme and port may be fetched at all; Refused otherwise."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError) as e:
        raise Refused(f"not a URL: {e}")
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise Refused("only http and https URLs")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in PORTS:
        raise Refused(f"port {port} is not fetched")
    if parsed.userinfo:
        raise Refused("URLs with credentials are not fetched")
    return parsed


def client(timeout=10.0, headers=None):
    """The client to pass to get(): redirects are never followed by httpx itself."""
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers or {})


async def get(session, url, max_bytes, truncate=False, hops=HOPS, headers=None):
    """GETs a URL from outside: Fetched, or Refused when it leads anywhere it must not.

    `max_bytes` bounds the body: beyond it the body is cut there (truncate=True) or the fetch is
    refused. httpx errors (timeouts, refused connections) are raised as they are.
    """
    for _ in range(hops + 1):
        parsed = check_url(url)
        host = parsed.host
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not is_public_address(literal):
                raise Refused("private address")
            address = host
        else:
            address = await resolve(host)
        # To the address that was checked, under the name that was asked for
        pinned = parsed.copy_with(host=address)
        default_port = 443 if parsed.scheme == "https" else 80
        host_header = host if (parsed.port in (None, default_port)) else f"{host}:{parsed.port}"
        request = session.build_request("GET", pinned, headers={**(headers or {}), "Host": host_header},
                                        extensions={"sni_hostname": host} if parsed.scheme == "https" else {})
        response = await session.send(request, stream=True)
        try:
            if response.is_redirect and response.headers.get("location"):
                url = str(parsed.join(response.headers["location"]))
                continue
            body = bytearray()
            truncated = False
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    if not truncate:
                        raise Refused(f"larger than {max_bytes} bytes")
                    del body[max_bytes:]
                    truncated = True
                    break
            return Fetched(url=str(parsed), status_code=response.status_code, headers=response.headers,
                           content=bytes(body), encoding=response.encoding or "utf-8", truncated=truncated)
        finally:
            await response.aclose()
    raise Refused(f"more than {hops} redirects")

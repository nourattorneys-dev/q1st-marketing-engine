"""One guard for every outside address the engine calls (client Strapi, client website pages):
https only (setting), public IPs only — never our own server, the LAN or cloud metadata."""
from __future__ import annotations
import asyncio, ipaddress, socket
from urllib.parse import urlparse


class Blocked(Exception):
    pass


async def check(url: str, require_https: bool = True) -> str:
    u = urlparse((url or "").strip())
    if u.scheme not in ("https", "http") or not u.hostname:
        raise Blocked("not a web address")
    if require_https and u.scheme != "https":
        raise Blocked("the address must start with https://")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Blocked(f"{u.hostname} cannot be found (DNS)")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise Blocked("this address points to a private network")
    return url

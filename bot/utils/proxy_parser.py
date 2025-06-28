# bot/utils/proxy_parser.py
import re
from typing import Optional

def parse_proxy_string(proxy_string: str) -> Optional[dict]:
    """
    Parses a proxy string into a dictionary suitable for Pyrogram.
    Expected format: protocol://user:pass@host:port or protocol://host:port
    Supported protocols: http, socks4, socks5.
    Returns a dictionary with proxy components or None if the format is invalid.
    """
    pattern = re.compile(
        r"^(?P<scheme>socks4|socks5|http)://"
        r"(?:(?P<username>[^:]+):(?P<password>[^@]+)@)?"
        r"(?P<hostname>[^:]+):"
        r"(?P<port>\d{1,5})$"
    )
    match = pattern.match(proxy_string.strip())
    if not match:
        return None

    data = match.groupdict()

    # Convert port to int and validate
    port = int(data['port'])
    if not 1 <= port <= 65535:
        return None
    data['port'] = port

    # Remove keys with None values so Pyrogram doesn't complain
    return {k: v for k, v in data.items() if v is not None}
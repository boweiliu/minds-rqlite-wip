"""Public tunnel for the data-store API, with a stable and a fallback mode.

Three modes, chosen at startup:

- **ngrok (stable, recommended).** If ``data/.secrets/data_store_ngrok.env``
  provides ``NGROK_AUTHTOKEN`` and ``NGROK_DOMAIN``, the tunnel runs ngrok bound
  to that reserved domain. The public URL is then permanently fixed --
  ``https://<domain>`` -- across restarts and container moves. ngrok's free tier
  includes one static domain, which is enough for this.

- **cloudflare quick tunnel (default fallback, zero-setup).** With no ngrok
  config, the tunnel runs an anonymous ``cloudflared`` quick tunnel, giving a
  ``https://<random>.trycloudflare.com`` URL. No account needed and more reliable
  than localtunnel (no visitor interstitial), but the hostname is *not* fixed
  across restarts -- a stopgap, not a permanent address.

- **localtunnel (opt-in fallback).** Set ``DATA_STORE_TUNNEL_PROVIDER=localtunnel``
  to use localtunnel instead. It works with no account but its free relay does not
  reliably hold a fixed subdomain either.

Any mode forwards only this one local port and security rests on the service's
bearer token. The resolved URL is written to ``DATA_DIR/public_url.txt`` for the
API/viewer to display.
"""

import logging
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_STORE_DATA_DIR", "data/.apps/data_store"))
PORT = int(os.environ.get("DATA_STORE_PORT", "8080"))
PUBLIC_URL_FILE = DATA_DIR / "public_url.txt"
SUBDOMAIN_FILE = DATA_DIR / "tunnel_subdomain.txt"
NGROK_SECRETS_FILE = Path("data/.secrets/data_store_ngrok.env")

_LOCALTUNNEL_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.loca\.lt")
_CLOUDFLARE_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_NGROK_AUTHTOKEN_PATTERN = re.compile(r"""^export\s+NGROK_AUTHTOKEN=["']?([^"'\s]+)["']?""", re.MULTILINE)
_NGROK_DOMAIN_PATTERN = re.compile(r"""^export\s+NGROK_DOMAIN=["']?([^"'\s]+)["']?""", re.MULTILINE)

logger = logging.getLogger("data-store-tunnel")


def _record_public_url(url: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_URL_FILE.write_text(url)
    logger.info("public URL: %s", url)


def _read_ngrok_config() -> tuple[str, str] | None:
    """Return (authtoken, domain) if both are configured, else None."""
    if not NGROK_SECRETS_FILE.exists():
        return None
    text = NGROK_SECRETS_FILE.read_text()
    token_match = _NGROK_AUTHTOKEN_PATTERN.search(text)
    domain_match = _NGROK_DOMAIN_PATTERN.search(text)
    if token_match and domain_match:
        return token_match.group(1), domain_match.group(1)
    return None


def _load_or_create_subdomain() -> str:
    """Return the persisted localtunnel subdomain, generating one on first use."""
    if SUBDOMAIN_FILE.exists():
        existing = SUBDOMAIN_FILE.read_text().strip()
        if existing:
            return existing
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    subdomain = f"minds-ds-{secrets.token_hex(4)}"
    SUBDOMAIN_FILE.write_text(subdomain)
    return subdomain


def _forward_output_until_exit(process: subprocess.Popen[str], url_pattern: re.Pattern[str] | None) -> int:
    """Forward a child's output to our log, recording the first matching URL."""
    assert process.stdout is not None
    recorded = url_pattern is None
    for line in process.stdout:
        logger.info(line.rstrip())
        if not recorded and url_pattern is not None:
            match = url_pattern.search(line)
            if match:
                _record_public_url(match.group(0))
                recorded = True
    return process.wait()


def _run_ngrok(authtoken: str, domain: str) -> int:
    """Run ngrok against the reserved domain; the URL is fixed and known upfront."""
    logger.info("using ngrok with stable domain %s", domain)
    _record_public_url(f"https://{domain}")
    child_env = dict(os.environ, NGROK_AUTHTOKEN=authtoken)
    process = subprocess.Popen(
        ["ngrok", "http", f"--domain={domain}", "--log=stdout", "--log-format=logfmt", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=child_env,
    )
    return _forward_output_until_exit(process, url_pattern=None)


def _run_cloudflare() -> int:
    """Run an anonymous cloudflared quick tunnel; parse the trycloudflare URL."""
    logger.info("using cloudflare quick tunnel (fallback); hostname is not fixed across restarts")
    process = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return _forward_output_until_exit(process, url_pattern=_CLOUDFLARE_URL_PATTERN)


def _run_localtunnel() -> int:
    """Run localtunnel; parse the assigned loca.lt URL from its output."""
    subdomain = _load_or_create_subdomain()
    logger.info("using localtunnel (fallback); requesting subdomain %s", subdomain)
    process = subprocess.Popen(
        ["lt", "--port", str(PORT), "--subdomain", subdomain],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return _forward_output_until_exit(process, url_pattern=_LOCALTUNNEL_URL_PATTERN)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[data-store-tunnel] %(message)s", stream=sys.stderr)

    # Clear any stale URL so the viewer never shows a dead hostname before the
    # tunnel has reconnected.
    if PUBLIC_URL_FILE.exists():
        PUBLIC_URL_FILE.unlink()

    ngrok_config = _read_ngrok_config()
    if ngrok_config is not None:
        exit_code = _run_ngrok(*ngrok_config)
    elif os.environ.get("DATA_STORE_TUNNEL_PROVIDER") == "localtunnel":
        exit_code = _run_localtunnel()
    else:
        exit_code = _run_cloudflare()
    # Provider exited; propagate so supervisord restarts us.
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

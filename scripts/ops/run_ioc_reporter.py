#!/usr/bin/env python3
"""Run docs/references/typstPipeline/ioc_reporter_typst.py with VirusTotal through the proxy.

The AutoWork VirusTotal proxy (VIRUSTOTAL_PROXY_URL, plain-HTTP forward proxy) holds the API key
and appends ``&apikey=<key>`` to the raw URL, so every request must carry a query string
(sentinel ``x=1``) and no key is sent from here. VirusTotal is used ONLY to check that a file
hash is present (``/files/<hash>``; needs httpx); domain lookups are skipped unless
``--vt-domains`` is given.
All other arguments are forwarded to the reporter. Reads VIRUSTOTAL_PROXY_URL from the environment.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPORTER = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "references"
    / "typstPipeline"
    / "ioc_reporter_typst.py"
)


def load_reporter():
    spec = importlib.util.spec_from_file_location("ioc_reporter_typst", REPORTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ioc_reporter_typst"] = module
    spec.loader.exec_module(module)
    return module


def patch_virustotal(module, proxy_url: str, vt_domains: bool) -> None:
    """Replace the VirusTotal transport by an httpx client bound to the AutoWork proxy.

    requests/urllib3 cannot skip certificate checks of an HTTPS proxy with a self-signed
    certificate, httpx can (VIRUSTOTAL_PROXY_INSECURE=true, same as the backend).
    """
    import re
    import ssl

    import httpx

    insecure = os.environ.get("VIRUSTOTAL_PROXY_INSECURE", "").lower() in {"1", "true", "yes"}
    proxy: str | httpx.Proxy = proxy_url
    if insecure and proxy_url.startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        proxy = httpx.Proxy(url=proxy_url, ssl_context=context)
    client = httpx.Client(proxy=proxy, timeout=30.0)
    provider = module.VirusTotalProvider

    def get(self, endpoint, cache_key, allow_404=True):  # type: ignore[no-untyped-def]
        cache = self.http.cache
        cached = cache.get(self.name, cache_key) if cache_key else None
        if cached is not None:
            if isinstance(cached, dict) and cached.get("_ioc_http_status") == 404:
                return None, 404, None
            return cached, 200, None
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        last_error = "unknown error"
        for attempt in range(self.http.retries + 1):
            self.http.limiter.wait(self.name, self.delay)
            try:
                response = client.get(url, params={"x": "1"})
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
                continue
            if response.status_code == 404 and allow_404:
                if cache_key:
                    cache.set(self.name, cache_key, {"_ioc_http_status": 404})
                return None, 404, None
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"_raw": ""}
                if cache_key:
                    cache.set(self.name, cache_key, payload)
                return payload, 200, None
            # The proxy can echo the API key in error bodies: report the status only.
            last_error = f"HTTP {response.status_code}"
            if response.status_code == 429 and attempt < self.http.retries:
                import time

                time.sleep(self.http.backoff * (2**attempt) + 30)
        return None, None, re.sub(r"apikey=[^&\s]+", "apikey=***", last_error)

    provider._get = get
    if not vt_domains:
        provider.domain = lambda self, domain: {"found": False}


def main() -> int:
    argv = sys.argv[1:]
    vt_domains = "--vt-domains" in argv
    argv = [item for item in argv if item != "--vt-domains"]
    proxy_url = os.environ.get("VIRUSTOTAL_PROXY_URL", "").strip()
    module = load_reporter()
    if proxy_url:
        patch_virustotal(module, proxy_url, vt_domains)
    else:
        print("VIRUSTOTAL_PROXY_URL is empty: VirusTotal availability will not be checked.")
    sys.argv = [str(REPORTER), *argv]
    return int(module.main())


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run docs/references/typstPipeline/ioc_reporter_typst.py with VirusTotal through the proxy.

The AutoWork VirusTotal proxy (VIRUSTOTAL_PROXY_URL, plain-HTTP forward proxy) holds the API key
and appends ``&apikey=<key>`` to the raw URL, so every request must carry a query string
(sentinel ``x=1``) and no key is sent from here. VirusTotal is used ONLY to check that a file
hash is present (``/files/<hash>``); domain lookups are skipped unless ``--vt-domains`` is given.
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
    original_init = module.HttpClient.__init__

    def init_with_proxy(self, cfg, cache):  # type: ignore[no-untyped-def]
        original_init(self, cfg, cache)
        # Only plain-HTTP URLs use the proxy: that is the VirusTotal base URL, nothing else.
        self.session.proxies.update({"http": proxy_url})

    module.HttpClient.__init__ = init_with_proxy
    provider = module.VirusTotalProvider

    def get(self, endpoint, cache_key, allow_404=True):  # type: ignore[no-untyped-def]
        return self.http.request_json(
            self.name,
            "GET",
            f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}",
            params={"x": "1"},
            cache_key=cache_key,
            rate_delay=self.delay,
            allow_404=allow_404,
        )

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

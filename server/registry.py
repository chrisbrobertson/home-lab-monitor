"""Registry health and catalog helpers.

Caches the last health check for 30 seconds to avoid hammering the registry
on every /api/capabilities request.
"""
import time
from typing import Optional, Tuple

import httpx

CACHE_TTL = 30  # seconds


class RegistryClient:
    def __init__(self, url: str, catalog_url: str, v2_url: str):
        self._url = url
        self._catalog_url = catalog_url
        self._v2_url = v2_url
        self._cache_ts: float = 0.0
        self._cache_result: Optional[dict] = None

    async def health(self) -> dict:
        """Return cached registry health + catalog info, refreshed every 30s."""
        now = time.monotonic()
        if self._cache_result is not None and (now - self._cache_ts) < CACHE_TTL:
            return self._cache_result

        result = await self._fetch()
        self._cache_result = result
        self._cache_ts = now
        return result

    async def _fetch(self) -> dict:
        try:
            async with httpx.AsyncClient() as client:
                v2_resp = await client.get(self._v2_url, timeout=5.0)
                healthy = v2_resp.status_code == 200

                repositories = []
                if healthy:
                    try:
                        cat_resp = await client.get(self._catalog_url, timeout=5.0)
                        if cat_resp.status_code == 200:
                            repositories = cat_resp.json().get("repositories", [])
                    except Exception:
                        pass

                return {
                    "url": self._url,
                    "healthy": healthy,
                    "repositories": repositories,
                    "repository_count": len(repositories),
                }
        except Exception as exc:
            return {
                "url": self._url,
                "healthy": False,
                "repositories": [],
                "repository_count": 0,
                "error": str(exc),
            }


def make_registry_client(registry_cfg) -> Optional[RegistryClient]:
    """Return a RegistryClient from a RegistryConfig, or None if unconfigured."""
    if registry_cfg is None:
        return None
    return RegistryClient(
        url=registry_cfg.url,
        catalog_url=registry_cfg.catalog_url,
        v2_url=registry_cfg.v2_url,
    )

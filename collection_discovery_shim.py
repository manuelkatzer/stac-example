"""
Adds the endpoints stac-collection-discovery's frontend expects but the
currently-published federated-collection-discovery backend (0.1.9)
doesn't implement yet:

  - /_mgmt/health : the frontend expects
    {status, lifespan: {status}, upstream_apis: {url: {healthy,
    collection_search_conformance}}} - a materially different, richer
    shape than this backend version's own /health, which just returns a
    flat {url: status_string}. So this isn't a plain alias like the
    others below - it reshapes the real per-catalog health check output
    plus each API's real /conformance into what the frontend expects.
  - /collections  : the frontend's actual search request goes here, not
    /search, and it expects the results under a "collections" key - the
    real search_collections() function returns them under "results"
    instead, so this wraps it and renames the field rather than aliasing
    it directly.
  - /api          : the frontend only checks for a 200 + valid JSON body
    here (used for an "API docs" link); FastAPI already generates a full
    OpenAPI schema for free, so we just expose that.
  - /conformance  : not implemented upstream at all yet. Every real STAC
    API already exposes this (it's a STAC API Core requirement), so we
    fetch it from whichever APIs are configured and union the results
    rather than inventing new data.

This module is run instead of federated_collection_discovery.main
directly (see Dockerfile.collection-discovery-api) - it imports the real
app and adds routes to it, rather than forking/patching the package.
"""
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, List, Optional

import httpx
from fastapi import Depends, Query
from pydantic import PositiveInt

from federated_collection_discovery.cmr_collection_search import CMRCollectionSearch
from federated_collection_discovery.collection_search import check_health
from federated_collection_discovery.main import (
    DEFAULT_LIMIT,
    app,
    get_executor,
    get_settings,
    search_collections,
)
from federated_collection_discovery.models import Settings
from federated_collection_discovery.stac_api_collection_search import STACAPICollectionSearch

# --- /collections --------------------------------------------------------
@app.get("/collections", summary="Federated collection search")
async def collections_search(
    settings: Annotated[Settings, Depends(get_settings)],
    executor: Annotated[ThreadPoolExecutor, Depends(get_executor)],
    bbox: Annotated[Optional[str], Query()] = None,
    datetime: Annotated[Optional[str], Query()] = None,
    q: Annotated[Optional[str], Query()] = None,
    limit: Annotated[PositiveInt, Query()] = DEFAULT_LIMIT,
):
    response = await search_collections(
        settings=settings, executor=executor, bbox=bbox, datetime=datetime, q=q, limit=limit
    )
    # search_collections() returns {results, errors} - the frontend reads
    # {collections, errors}, so rename rather than alias directly.
    #
    # The frontend also determines which upstream API a collection came
    # from by looking for a links[] entry with rel="root" - real STAC
    # Collection shape. CollectionMetadata doesn't have links at all
    # (it already tells you the source via catalog_url directly), so
    # without this the frontend finds no match, silently groups every
    # collection under nothing, and the search looks like it returned 0
    # results even though real data came back.
    collections = []
    for item in response.results:
        collection = item.model_dump()
        collection["links"] = [{"rel": "root", "href": collection["catalog_url"]}]
        collections.append(collection)

    return {"collections": collections, "errors": response.errors}


# --- /api ----------------------------------------------------------------
@app.get("/api", summary="API documentation (OpenAPI schema)")
async def api_docs():
    return app.openapi()


async def _conformance_for(client: httpx.AsyncClient, url: str) -> list[str]:
    try:
        resp = await client.get(f"{url.rstrip('/')}/conformance")
        resp.raise_for_status()
        return resp.json().get("conformsTo", [])
    except Exception:
        # One unreachable/non-conformant upstream shouldn't break the
        # whole response - it just contributes nothing.
        return []


# --- /conformance --------------------------------------------------------
@app.get("/conformance", summary="Conformance classes across configured APIs")
async def conformance(
    settings: Annotated[Settings, Depends(get_settings)],
    apis: Annotated[Optional[List[str]], Query()] = None,
):
    urls = apis or settings.stac_api_urls
    conforms_to: set[str] = set()

    async with httpx.AsyncClient(timeout=5.0) as client:
        for url in urls:
            conforms_to.update(await _conformance_for(client, url))

    return {"conformsTo": sorted(conforms_to)}


# --- /_mgmt/health ---------------------------------------------------------
@app.get("/_mgmt/health", summary="Health + per-API conformance, in the shape the frontend expects")
async def mgmt_health(
    settings: Annotated[Settings, Depends(get_settings)],
    executor: Annotated[ThreadPoolExecutor, Depends(get_executor)],
    apis: Annotated[Optional[List[str]], Query()] = None,
):
    urls = apis or settings.stac_api_urls
    catalogs = [STACAPICollectionSearch(base_url=u) for u in urls] + [
        CMRCollectionSearch(base_url=u) for u in settings.cmr_urls
    ]
    raw_status = await check_health(executor, catalogs)  # {url: "healthy" | <error string>}

    upstream_apis: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for url, status in raw_status.items():
            upstream_apis[url] = {
                "healthy": status == "healthy",
                "collection_search_conformance": await _conformance_for(client, url),
            }

    overall = "UP" if all(v["healthy"] for v in upstream_apis.values()) else "DEGRADED"

    return {
        "status": overall,
        "lifespan": {"status": "UP"},
        "upstream_apis": upstream_apis,
    }

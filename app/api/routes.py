"""HTTP layer. Routes stay thin: validation in, SearchService out."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.core.models import Listing, SearchRequest, SearchResponse
from app.data.repository import get_listing, load_listings

router = APIRouter(prefix="/api")


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "listings": len(load_listings()),
        "llm_configured": s.llm_enabled,
        "llm_model": s.llm_model if s.llm_enabled else None,
        "explain_with_llm": s.explain_with_llm and s.llm_enabled,
        "version": (s.app_version or "dev")[:12],
    }


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> SearchResponse:
    service = request.app.state.search_service
    return await service.search(body, request_id=request.state.request_id)


@router.get("/listings", response_model=list[Listing])
async def listings() -> list[Listing]:
    return list(load_listings())


@router.get("/listings/{listing_id}", response_model=Listing)
async def listing(listing_id: str) -> Listing:
    item = get_listing(listing_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"listing {listing_id} not found")
    return item

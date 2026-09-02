"""HTTP boundary for immutable attribution manifests and dry-run previews."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.attribution.domain import AttributionDomainError
from app.attribution.service import AttributionService
from app.attribution.store import (
    AttributionConflictError,
    AttributionNotFoundError,
    AttributionStoreError,
)
from app.config import settings
from app.schemas.attribution import (
    AttributionManifest,
    CreateManifestRequest,
    CreateOverlayRequest,
    PayoutPreview,
    PayoutPreviewRequest,
    ReviewOverlay,
    Sha256Hex,
    StoredManifestResponse,
    StoredOverlayResponse,
)


class AttributionValidationRoute(APIRoute):
    """Give this API a stable, non-disclosing validation error envelope."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def validation_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "code": "invalid_attribution_request",
                            "message": "attribution request validation failed",
                        }
                    },
                )

        return validation_handler


router = APIRouter(
    prefix="/attribution",
    tags=["attribution"],
    route_class=AttributionValidationRoute,
)


def get_attribution_service() -> AttributionService:
    # Import lazily so the pure domain and API contract remain testable without
    # constructing the repository's environment-driven database engine.
    from app.attribution.postgres import PostgresAttributionStore

    return AttributionService(PostgresAttributionStore())


def require_attribution_admin(
    supplied_token: Annotated[
        str | None,
        Header(alias="X-Attribution-Admin-Token"),
    ] = None,
) -> None:
    configured_secret = settings.attribution_admin_token
    configured_token = configured_secret.get_secret_value() if configured_secret is not None else ""
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "attribution_admin_not_configured",
                "message": "the attribution API is disabled",
            },
        )
    if supplied_token is None or not hmac.compare_digest(
        supplied_token.encode("utf-8"),
        configured_token.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_attribution_admin_token",
                "message": "a valid attribution admin token is required",
            },
        )


def _raise_api_error(error: Exception) -> NoReturn:
    if isinstance(error, AttributionDomainError):
        # Use the stable wire value across Starlette versions; the historical
        # constant was renamed while the HTTP status code remained 422.
        status_code = 422
    elif isinstance(error, AttributionNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, AttributionConflictError):
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(error, AttributionStoreError):
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    else:  # pragma: no cover - callers only pass structured attribution errors
        raise error
    raise HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": error.detail},
    ) from error


@router.post(
    "/manifests",
    response_model=StoredManifestResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_attribution_admin)],
)
async def create_manifest(
    request: CreateManifestRequest,
    response: Response,
    service: Annotated[AttributionService, Depends(get_attribution_service)],
) -> StoredManifestResponse:
    try:
        manifest, created = await service.create_manifest(request)
    except (AttributionDomainError, AttributionStoreError) as error:
        _raise_api_error(error)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return StoredManifestResponse(manifest=manifest, created=created)


@router.get(
    "/manifests/{manifest_hash}",
    response_model=AttributionManifest,
    dependencies=[Depends(require_attribution_admin)],
)
async def get_manifest(
    manifest_hash: Sha256Hex,
    service: Annotated[AttributionService, Depends(get_attribution_service)],
) -> AttributionManifest:
    try:
        return await service.get_manifest(manifest_hash)
    except AttributionStoreError as error:
        _raise_api_error(error)


@router.post(
    "/manifests/{manifest_hash}/overlays",
    response_model=StoredOverlayResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_attribution_admin)],
)
async def create_overlay(
    manifest_hash: Sha256Hex,
    request: CreateOverlayRequest,
    response: Response,
    service: Annotated[AttributionService, Depends(get_attribution_service)],
) -> StoredOverlayResponse:
    try:
        overlay, created = await service.create_overlay(manifest_hash, request)
    except (AttributionDomainError, AttributionStoreError) as error:
        _raise_api_error(error)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return StoredOverlayResponse(overlay=overlay, created=created)


@router.get(
    "/overlays/{overlay_hash}",
    response_model=ReviewOverlay,
    dependencies=[Depends(require_attribution_admin)],
)
async def get_overlay(
    overlay_hash: Sha256Hex,
    service: Annotated[AttributionService, Depends(get_attribution_service)],
) -> ReviewOverlay:
    try:
        return await service.get_overlay(overlay_hash)
    except AttributionStoreError as error:
        _raise_api_error(error)


@router.post(
    "/manifests/{manifest_hash}/payout-previews",
    response_model=PayoutPreview,
    dependencies=[Depends(require_attribution_admin)],
)
async def preview_payout(
    manifest_hash: Sha256Hex,
    request: PayoutPreviewRequest,
    service: Annotated[AttributionService, Depends(get_attribution_service)],
) -> PayoutPreview:
    try:
        return await service.preview_payout(manifest_hash, request)
    except (AttributionDomainError, AttributionStoreError) as error:
        _raise_api_error(error)

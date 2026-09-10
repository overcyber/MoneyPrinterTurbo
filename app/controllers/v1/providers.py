"""Provider discovery, health and protocol bridge endpoints."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.services import provider_gateway
from app.utils import utils


class ProviderInvokeRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class OpenAIImageRequest(BaseModel):
    model: str = ""
    prompt: str
    n: int = Field(default=1, ge=1, le=16)
    size: str = "auto"
    quality: str | None = None
    style: str | None = None
    response_format: str | None = None


router = new_router(dependencies=[Depends(base.verify_token)])


def _gateway_error(request: Request, exc: Exception):
    raise HttpException(
        task_id=base.get_task_id(request),
        status_code=400,
        message=str(exc),
    ) from exc


@router.get("/providers", summary="List configured runtime providers")
def list_providers(request: Request):
    try:
        return utils.get_response(
            200,
            {
                "config_file": str(provider_gateway.provider_config_path()),
                "providers": provider_gateway.list_providers(enabled_only=False),
            },
        )
    except provider_gateway.ProviderGatewayError as exc:
        _gateway_error(request, exc)


@router.get("/providers/health", summary="Health-check all configured providers")
def providers_health(request: Request):
    try:
        results = provider_gateway.health_all()
        return utils.get_response(
            200,
            {
                "ok": all(item.get("ok") for item in results) if results else True,
                "providers": results,
            },
        )
    except provider_gateway.ProviderGatewayError as exc:
        _gateway_error(request, exc)


@router.get("/providers/{provider_id}/health", summary="Health-check one provider")
def provider_health(provider_id: str, request: Request):
    try:
        return utils.get_response(200, provider_gateway.health(provider_id))
    except provider_gateway.ProviderGatewayError as exc:
        _gateway_error(request, exc)


@router.post(
    "/providers/{provider_id}/invoke/{action_name}",
    summary="Invoke a predeclared provider action",
)
def invoke_provider(
    provider_id: str,
    action_name: str,
    body: ProviderInvokeRequest,
    request: Request,
):
    try:
        return utils.get_response(
            200,
            provider_gateway.invoke(provider_id, action_name, payload=body.payload),
        )
    except provider_gateway.ProviderGatewayError as exc:
        _gateway_error(request, exc)


# Existing MoneyPrinterTurbo openai_image calls use Bearer auth while the main
# API uses x-api-key. For the protocol bridge accept either representation of
# the same configured app.api_key. With an empty key, preserve local no-auth.
bridge_router = APIRouter(prefix="/api/v1", tags=["V1 Providers"])


def verify_provider_bridge_token(request: Request):
    configured_key = config.app.get("api_key", "")
    if configured_key in (None, ""):
        return None
    if not isinstance(configured_key, str):
        raise HttpException(
            task_id=base.get_task_id(request),
            status_code=500,
            message="API authentication is misconfigured",
        )
    x_api_key = request.headers.get("x-api-key")
    authorization = request.headers.get("authorization", "")
    bearer = ""
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    candidates = [v for v in (x_api_key, bearer) if isinstance(v, str) and v]
    if len(candidates) != 1 or not secrets.compare_digest(
        candidates[0].encode("utf-8"), configured_key.encode("utf-8")
    ):
        raise HttpException(
            task_id=base.get_task_id(request),
            status_code=401,
            message="invalid API key",
        )
    return None


@bridge_router.post(
    "/providers/{provider_id}/images/generations",
    dependencies=[Depends(verify_provider_bridge_token)],
    summary="OpenAI-compatible image bridge for a configured provider",
)
def provider_openai_image_bridge(
    provider_id: str,
    body: OpenAIImageRequest,
    request: Request,
):
    try:
        return provider_gateway.openai_image_generation(
            provider_id,
            body.model_dump(exclude_none=True),
        )
    except provider_gateway.ProviderGatewayError as exc:
        _gateway_error(request, exc)

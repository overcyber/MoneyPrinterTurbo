"""Runtime provider registry for local, Docker and HTTP backends.

Providers live in providers.docker.json (or MPT_PROVIDER_CONFIG). Only actions
predeclared by the server can be invoked; clients never submit arbitrary URLs.
Secrets are read from environment variables at request time and never returned
by discovery endpoints.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests

from app.utils import utils

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ALLOWED_METHODS = {"GET", "POST"}
_DEFAULT_CONFIG_NAME = "providers.docker.json"
_registry_lock = threading.RLock()
_registry_cache = None


class ProviderGatewayError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderAction:
    name: str
    method: str
    path: str
    timeout_s: float


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider_id: str
    kind: str
    protocol: str
    label: str
    base_url: str
    base_url_env: str
    enabled: bool
    auth_type: str
    auth_env: str
    auth_optional: bool
    actions: dict[str, ProviderAction]
    defaults: dict[str, Any]

    def resolve_base_url(self) -> str:
        override = os.environ.get(self.base_url_env, "").strip() if self.base_url_env else ""
        return _normalize_base_url(override or self.base_url)

    def public_dict(self) -> dict[str, Any]:
        auth_configured = self.auth_type == "none"
        if self.auth_env:
            auth_configured = bool(os.environ.get(self.auth_env, "").strip())
        return {
            "id": self.provider_id,
            "kind": self.kind,
            "protocol": self.protocol,
            "label": self.label,
            "enabled": self.enabled,
            "base_url": self.resolve_base_url(),
            "base_url_env": self.base_url_env,
            "auth": {
                "type": self.auth_type,
                "configured": auth_configured,
                "optional": self.auth_optional,
            },
            "actions": {
                name: {
                    "method": action.method,
                    "path": action.path,
                    "timeout_s": action.timeout_s,
                }
                for name, action in self.actions.items()
            },
            "defaults": _safe_defaults(self.defaults),
        }


def provider_config_path() -> Path:
    configured = os.environ.get("MPT_PROVIDER_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(utils.root_dir(), _DEFAULT_CONFIG_NAME).resolve()


def _normalize_base_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if not value or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderGatewayError("provider base_url must be an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderGatewayError("provider base_url must not embed credentials")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _relative_path(value: str) -> str:
    value = str(value or "").strip()
    parsed = urlsplit(value)
    if not value.startswith("/") or parsed.scheme or parsed.netloc or ".." in Path(parsed.path).parts:
        raise ProviderGatewayError("provider action path must be a safe relative path")
    return value


def _safe_defaults(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe = {}
    for key, item in value.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in ("token", "secret", "password", "api_key")):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            safe[str(key)] = item
    return safe


def _parse_action(name: str, raw: Any) -> ProviderAction:
    if not isinstance(raw, dict):
        raise ProviderGatewayError(f"provider action {name!r} must be an object")
    method = str(raw.get("method", "GET")).upper().strip()
    if method not in _ALLOWED_METHODS:
        raise ProviderGatewayError(f"unsupported provider method: {method}")
    timeout_s = float(raw.get("timeout_s", 60))
    if timeout_s <= 0 or timeout_s > 7200:
        raise ProviderGatewayError("provider timeout must be within 0..7200 seconds")
    return ProviderAction(name, method, _relative_path(raw.get("path", "")), timeout_s)


def _parse_provider(provider_id: str, raw: Any) -> ProviderSpec:
    if not _PROVIDER_ID_RE.fullmatch(provider_id):
        raise ProviderGatewayError(f"invalid provider id: {provider_id!r}")
    if not isinstance(raw, dict):
        raise ProviderGatewayError(f"provider {provider_id!r} must be an object")
    auth = raw.get("auth") or {}
    if not isinstance(auth, dict):
        raise ProviderGatewayError("provider auth must be an object")
    auth_type = str(auth.get("type", "none")).lower().strip()
    if auth_type not in {"none", "bearer_env", "header_env"}:
        raise ProviderGatewayError(f"unsupported provider auth type: {auth_type}")
    auth_env = str(auth.get("env", "")).strip()
    if auth_type != "none" and not auth_env:
        raise ProviderGatewayError("provider auth env is required")
    actions_raw = raw.get("actions") or {}
    if not isinstance(actions_raw, dict) or not actions_raw:
        raise ProviderGatewayError("provider must declare at least one action")
    base_url = str(raw.get("base_url", "")).strip()
    base_url_env = str(raw.get("base_url_env", "")).strip()
    if not base_url and not base_url_env:
        raise ProviderGatewayError("provider needs base_url or base_url_env")
    if base_url:
        _normalize_base_url(base_url)
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ProviderGatewayError("provider defaults must be an object")
    return ProviderSpec(
        provider_id=provider_id,
        kind=str(raw.get("kind", "generic")).strip().lower() or "generic",
        protocol=str(raw.get("protocol", "json_http")).strip().lower() or "json_http",
        label=str(raw.get("label", provider_id)).strip() or provider_id,
        base_url=base_url,
        base_url_env=base_url_env,
        enabled=bool(raw.get("enabled", True)),
        auth_type=auth_type,
        auth_env=auth_env,
        auth_optional=bool(auth.get("optional", False)),
        actions={str(k): _parse_action(str(k), v) for k, v in actions_raw.items()},
        defaults=defaults,
    )


def _load_registry(path: Path) -> dict[str, ProviderSpec]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderGatewayError(f"invalid provider JSON: {exc}") from exc
    if not isinstance(raw, dict) or int(raw.get("schema_version", 1)) != 1:
        raise ProviderGatewayError("unsupported provider configuration")
    providers = raw.get("providers") or {}
    if not isinstance(providers, dict):
        raise ProviderGatewayError("providers must be an object keyed by provider id")
    return {str(k): _parse_provider(str(k), v) for k, v in providers.items()}


def load_registry(*, force_reload: bool = False) -> dict[str, ProviderSpec]:
    global _registry_cache
    path = provider_config_path()
    mtime = path.stat().st_mtime_ns if path.exists() else -1
    key = str(path)
    with _registry_lock:
        if not force_reload and _registry_cache and _registry_cache[0] == key and _registry_cache[1] == mtime:
            return dict(_registry_cache[2])
        registry = _load_registry(path)
        _registry_cache = (key, mtime, registry)
        return dict(registry)


def list_providers(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    return [
        spec.public_dict()
        for spec in load_registry().values()
        if not enabled_only or spec.enabled
    ]


def get_provider(provider_id: str, *, require_enabled: bool = True) -> ProviderSpec:
    spec = load_registry().get(str(provider_id or "").strip())
    if spec is None:
        raise ProviderGatewayError(f"unknown provider: {provider_id}")
    if require_enabled and not spec.enabled:
        raise ProviderGatewayError(f"provider is disabled: {provider_id}")
    return spec


def _headers(spec: ProviderSpec) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if spec.auth_type == "none":
        return headers
    value = os.environ.get(spec.auth_env, "").strip()
    if not value:
        if spec.auth_optional:
            return headers
        raise ProviderGatewayError(f"provider {spec.provider_id} requires {spec.auth_env}")
    if spec.auth_type == "bearer_env":
        headers["Authorization"] = f"Bearer {value}"
    else:
        name = str(spec.defaults.get("auth_header", "x-api-key")).strip()
        if not name or any(ch in name for ch in "\r\n:"):
            raise ProviderGatewayError("invalid provider auth_header")
        headers[name] = value
    return headers


def invoke(provider_id: str, action_name: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = get_provider(provider_id)
    action = spec.actions.get(str(action_name or "").strip())
    if action is None:
        raise ProviderGatewayError(f"provider {provider_id} does not declare action {action_name!r}")
    headers = _headers(spec)
    if action.method == "POST":
        headers["Content-Type"] = "application/json"
    started = time.monotonic()
    try:
        response = requests.request(
            action.method,
            urljoin(spec.resolve_base_url() + "/", action.path.lstrip("/")),
            headers=headers,
            json=(payload or {}) if action.method == "POST" else None,
            timeout=action.timeout_s,
        )
    except requests.RequestException as exc:
        raise ProviderGatewayError(
            f"provider {provider_id}/{action_name} connection failed: {type(exc).__name__}"
        ) from exc
    try:
        body = response.json() if "json" in str(response.headers.get("content-type", "")).lower() else response.text[:16384]
    except ValueError as exc:
        raise ProviderGatewayError("provider returned invalid JSON") from exc
    if response.status_code >= 400:
        detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        raise ProviderGatewayError(
            f"provider {provider_id}/{action_name} returned HTTP {response.status_code}: {detail[:1000]}"
        )
    return {
        "provider": provider_id,
        "action": action_name,
        "status_code": response.status_code,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "data": body,
    }


def health(provider_id: str) -> dict[str, Any]:
    spec = get_provider(provider_id, require_enabled=False)
    if not spec.enabled:
        return {"provider": provider_id, "ok": False, "enabled": False, "detail": "disabled"}
    if "health" not in spec.actions:
        return {"provider": provider_id, "ok": False, "enabled": True, "detail": "health action is not configured"}
    try:
        result = invoke(provider_id, "health")
        return {
            "provider": provider_id,
            "ok": True,
            "enabled": True,
            "elapsed_ms": result["elapsed_ms"],
            "data": result["data"],
        }
    except ProviderGatewayError as exc:
        return {"provider": provider_id, "ok": False, "enabled": True, "detail": str(exc)}


def health_all() -> list[dict[str, Any]]:
    return [health(item["id"]) for item in list_providers(enabled_only=False)]


def _image_size(value: Any, spec: ProviderSpec) -> tuple[int, int]:
    text = str(value or "").strip().lower()
    if not text or text == "auto":
        width = int(spec.defaults.get("width", 1024))
        height = int(spec.defaults.get("height", 1024))
    else:
        match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", text)
        if not match:
            raise ProviderGatewayError("image size must use WIDTHxHEIGHT")
        width, height = map(int, match.groups())
    minimum = int(spec.defaults.get("min_size", 256))
    maximum = int(spec.defaults.get("max_size", 4096))
    multiple = int(spec.defaults.get("size_multiple", 1))
    for label, number in (("width", width), ("height", height)):
        if number < minimum or number > maximum:
            raise ProviderGatewayError(f"{label} must be within {minimum}..{maximum}")
        if multiple > 1 and number % multiple:
            raise ProviderGatewayError(f"{label} must be a multiple of {multiple}")
    return width, height


def _download_relative_file(spec: ProviderSpec, relative_path: str) -> bytes:
    clean = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
    if not clean or ".." in Path(clean).parts:
        raise ProviderGatewayError("provider returned an unsafe relative_path")
    prefix = _relative_path(str(spec.defaults.get("files_path", "/files/")))
    url = spec.resolve_base_url().rstrip("/") + "/" + prefix.strip("/") + "/" + quote(clean, safe="/")
    try:
        response = requests.get(
            url,
            headers=_headers(spec),
            timeout=float(spec.defaults.get("download_timeout_s", 300)),
        )
    except requests.RequestException as exc:
        raise ProviderGatewayError(f"provider artifact download failed: {type(exc).__name__}") from exc
    if response.status_code >= 400 or not response.content:
        raise ProviderGatewayError(f"provider artifact download returned HTTP {response.status_code}")
    return response.content


def openai_image_generation(provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Translate OpenAI Images into the native ``mpt_image_v1`` protocol."""
    spec = get_provider(provider_id)
    if spec.kind != "image" or spec.protocol != "mpt_image_v1":
        raise ProviderGatewayError(f"provider {provider_id} has no mpt_image_v1 bridge")
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        raise ProviderGatewayError("image prompt is required")
    if int(body.get("n", 1) or 1) != 1:
        raise ProviderGatewayError("mpt_image_v1 bridge currently supports n=1")
    width, height = _image_size(body.get("size"), spec)
    payload = {
        "prompt": prompt,
        "save_dir": str(spec.defaults.get("save_dir", "moneyprinterturbo")).strip() or "moneyprinterturbo",
        "width": width,
        "height": height,
    }
    negative = str(spec.defaults.get("negative_prompt", "") or "").strip()
    if negative:
        payload["negative_prompt"] = negative
    seed = spec.defaults.get("seed")
    if isinstance(seed, int) and seed >= 0:
        payload["seed"] = seed
    result = invoke(provider_id, "generate_image", payload=payload)
    data = result.get("data")
    if not isinstance(data, dict):
        raise ProviderGatewayError("image provider returned a non-object response")
    relative_path = str(data.get("relative_path", "") or "").strip()
    if relative_path:
        image_bytes = _download_relative_file(spec, relative_path)
    else:
        image_url = str(data.get("url", "") or "").strip()
        parsed = urlsplit(image_url)
        base = urlsplit(spec.resolve_base_url())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ProviderGatewayError("image provider returned neither relative_path nor a valid url")
        safe_url = image_url
        if parsed.hostname in {"localhost", "127.0.0.1"} and base.hostname not in {"localhost", "127.0.0.1"}:
            safe_url = urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, ""))
        try:
            response = requests.get(safe_url, headers=_headers(spec), timeout=float(spec.defaults.get("download_timeout_s", 300)))
        except requests.RequestException as exc:
            raise ProviderGatewayError(f"provider image download failed: {type(exc).__name__}") from exc
        if response.status_code >= 400 or not response.content:
            raise ProviderGatewayError(f"provider image download returned HTTP {response.status_code}")
        image_bytes = response.content
    return {
        "created": int(time.time()),
        "data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii"), "revised_prompt": prompt}],
        "provider": {
            "id": spec.provider_id,
            "protocol": spec.protocol,
            "model": data.get("model"),
            "elapsed_seconds": data.get("elapsed_seconds"),
            "gpu": data.get("gpu"),
            "seed": data.get("seed"),
        },
    }

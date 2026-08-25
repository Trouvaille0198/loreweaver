"""Image generation over provider HTTP endpoints.

OpenAI-compatible providers use ``/images/generations`` without native SDKs.
Provider-specific surfaces such as MiniMax Image Generation and xAI's dimension
fields are translated here so runtime switching stays deterministic and testable.
"""

from __future__ import annotations

import base64
import binascii
import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse

import httpx

from infra.config import ImageGenSettings, Settings
from infra.oauth_flows import XAI_API_BASE, XAI_DEFAULT_IMAGE_MODEL

if TYPE_CHECKING:
    from infra.runtime_config import CredentialBook

OPENAI_IMAGE_BASE_URL = "https://api.openai.com/v1"
MINIMAX_IMAGE_URL = "https://api.minimaxi.com/v1/image_generation"
SILICONFLOW_IMAGE_BASE_URL = "https://api.siliconflow.cn/v1"
IMAGEGEN_OVERRIDE_FIELDS: tuple[str, ...] = ("provider", "base_url", "api_key", "model", "size")

# Provider presets: base_url + default model. ``supergrok`` reuses the SuperGrok
# subscription token from the LLM credential book (not a separate image key).
IMAGEGEN_PRESETS: dict[str, dict[str, str]] = {
    "openai": {"base_url": OPENAI_IMAGE_BASE_URL, "model": "gpt-image-1"},
    "minimax-cn": {"base_url": MINIMAX_IMAGE_URL, "model": "image-01"},
    "siliconflow": {"base_url": SILICONFLOW_IMAGE_BASE_URL, "model": "Kwai-Kolors/Kolors"},
    "supergrok": {"base_url": XAI_API_BASE, "model": XAI_DEFAULT_IMAGE_MODEL},
    "qwen": {
        "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1",
        "model": "qwen-image-3.0-pro",
    },
    "qwen-coding-plan": {
        "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1",
        "model": "qwen-image-3.0-pro",
    },
    # 千问AI平台按量付费（通用 API Key sk-ws-）；DashScope 原生文生图端点
    "qwen-api": {
        "base_url": "https://dashscope.aliyuncs.com/api/v1",
        "model": "qwen-image-3.0-pro",
    },
}

TokenProvider = Callable[[], Awaitable[str]]

_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8ffff3f0005fe02fea7a0a5810000000049454e44ae426082"
)

_XAI_ASPECT_RATIOS: tuple[tuple[str, float], ...] = (
    ("1:1", 1.0),
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
    ("4:3", 4 / 3),
    ("3:4", 3 / 4),
    ("3:2", 3 / 2),
    ("2:3", 2 / 3),
    ("2:1", 2.0),
    ("1:2", 0.5),
    ("19.5:9", 19.5 / 9),
    ("9:19.5", 9 / 19.5),
    ("20:9", 20 / 9),
    ("9:20", 9 / 20),
)

_MINIMAX_ASPECT_RATIOS: tuple[tuple[str, float], ...] = (
    ("1:1", 1.0),
    ("16:9", 16 / 9),
    ("4:3", 4 / 3),
    ("3:2", 3 / 2),
    ("2:3", 2 / 3),
    ("3:4", 3 / 4),
    ("9:16", 9 / 16),
    ("21:9", 21 / 9),
)


class ImageGenError(RuntimeError):
    """Stable image-generation error code plus optional detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


class ImageGen(Protocol):
    # Which `.image` kinds this provider can anchor with a reference image. MiniMax's
    # subject_reference only supports `type: character`, so a scene/item illustration
    # would be misinterpreted as a person's identity anchor; image-to-image providers
    # (OpenAI-compatible edits, SiliconFlow) accept any reference. The caller must not
    # hand a kind outside this set a reference.
    reference_kinds: frozenset[str] = frozenset({"scene", "portrait", "clue"})

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        reference: bytes | None = None,
        reference_mime: str = "",
    ) -> tuple[bytes, str]:
        """Generate one image. Returns ``(bytes, mime)``.

        ``reference`` is an optional 定妆 reference image (M19): consistency across a
        module's art is the hard part, so the Stage Director sends the author's fixed
        portrait with every request. Providers that expose an image-edit endpoint
        condition on it; the rest ignore it and fall back to prompt-only generation —
        the structural half of the discipline (no reference → no portrait) is enforced
        by the caller regardless.
        """


class OpenAICompatImageGen:
    """HTTP client for OpenAI-compatible image generation endpoints."""

    # image-edits is a real image-to-image surface: any `.image` kind can anchor a
    # reference.
    reference_kinds: frozenset[str] = frozenset({"scene", "portrait", "clue"})

    def __init__(
        self,
        settings: ImageGenSettings,
        *,
        client: httpx.AsyncClient | None = None,
        token_provider: TokenProvider | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._settings = settings
        self._client = client
        self._token_provider = token_provider
        self._timeout = timeout

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        reference: bytes | None = None,
        reference_mime: str = "",
    ) -> tuple[bytes, str]:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ImageGenError("imagegen_bad_prompt")
        if not self._settings.model or not self._settings.provider:
            raise ImageGenError("imagegen_not_configured")
        if self._token_provider is not None:
            api_key = await self._token_provider()
        else:
            api_key = self._settings.api_key
        if not api_key:
            raise ImageGenError("imagegen_missing_key")

        close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            close_client = True
        requested_size = size or self._settings.size or "1024x1024"
        request_body = {
            "model": self._settings.model,
            "prompt": prompt,
            "response_format": "b64_json",
        }
        if (self._settings.provider or "").casefold() == "supergrok":
            # xAI's Imagine API uses aspect_ratio + 1k/2k resolution rather
            # than OpenAI's pixel-based `size` field.
            request_body.update(_xai_dimensions(requested_size))
        else:
            request_body["size"] = requested_size

        base = _base_url(self._settings).rstrip("/")
        try:
            if reference:
                # A 定妆 reference means image-to-image, which is a DIFFERENT endpoint
                # and a multipart body on the OpenAI-compatible surface. `response_format`
                # is not accepted there; edits answer b64 by default.
                request_body.pop("response_format", None)
                response = await client.post(
                    f"{base}/images/edits",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={key: str(value) for key, value in request_body.items()},
                    files={"image": ("reference.png", reference, reference_mime or "image/png")},
                )
            else:
                response = await client.post(
                    f"{base}/images/generations",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=request_body,
                )
        except httpx.TimeoutException as exc:
            raise ImageGenError("imagegen_timeout") from exc
        except httpx.HTTPError as exc:
            raise ImageGenError("imagegen_http_error") from exc
        finally:
            if close_client:
                await client.aclose()

        if response.status_code < 200 or response.status_code >= 300:
            raise ImageGenError("imagegen_http_error", str(response.status_code))

        try:
            payload = response.json()
            entry = payload["data"][0]
            b64 = entry["b64_json"]
            data = base64.b64decode(str(b64), validate=True)
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
            raise ImageGenError("imagegen_bad_response") from exc
        if not data:
            raise ImageGenError("imagegen_bad_response")
        declared_mime = entry.get("mime_type") if isinstance(entry, dict) else None
        return data, _detect_image_mime(data, declared_mime)


class MiniMaxImageGen:
    """MiniMax's JSON Image Generation surface (``/v1/image_generation``)."""

    # `subject_reference` is documented as `type: character`, but real-world testing
    # shows MiniMax accepts ANY image as a reference: environment/atmosphere and
    # composition from a scene reference are absorbed into the result, and only a
    # person/face present IN the reference is extracted as the identity anchor. So
    # scene/clue references work too — pass them through rather than refusing.
    reference_kinds: frozenset[str] = frozenset({"scene", "portrait", "clue"})

    def __init__(
        self,
        settings: ImageGenSettings,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._settings = settings
        self._client = client
        self._timeout = timeout

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        reference: bytes | None = None,
        reference_mime: str = "",
    ) -> tuple[bytes, str]:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ImageGenError("imagegen_bad_prompt")
        if not self._settings.model or not self._settings.api_key:
            raise ImageGenError("imagegen_not_configured")
        # MiniMax documents subject references as ``image_file`` values that may be a
        # public URL OR a base64 Data URL (character-subject consistency for i2i). Our
        # reference comes from local kit/room bytes, so inline it as a Data URL rather
        # than refusing: discarding the identity anchor would silently break the
        # consistency the caller asked for. Only `character` references are documented;
        # anything else (scene/item) has no documented subject_reference type, so we
        # skip it and fall back to prompt-only rather than invent a contract.
        subject_reference = None
        if reference:
            mime = _detect_image_mime(reference, reference_mime)
            if mime in ("image/jpeg", "image/png"):
                encoded = base64.b64encode(reference).decode("ascii")
                subject_reference = [{"type": "character", "image_file": f"data:{mime};base64,{encoded}"}]

        close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            close_client = True
        request_body = {
            "model": self._settings.model,
            "prompt": prompt[:1500],
            "aspect_ratio": _nearest_aspect_ratio(size, _MINIMAX_ASPECT_RATIOS),
            "response_format": "base64",
            "n": 1,
        }
        if subject_reference is not None:
            request_body["subject_reference"] = subject_reference
        try:
            response = await client.post(
                _minimax_image_url(self._settings.base_url),
                headers={"Authorization": f"Bearer {self._settings.api_key}"},
                json=request_body,
            )
        except httpx.TimeoutException as exc:
            raise ImageGenError("imagegen_timeout") from exc
        except httpx.HTTPError as exc:
            raise ImageGenError("imagegen_http_error") from exc
        finally:
            if close_client:
                await client.aclose()

        if response.status_code < 200 or response.status_code >= 300:
            raise ImageGenError("imagegen_http_error", str(response.status_code))
        try:
            payload = response.json()
            encoded = payload["data"]["image_base64"][0]
            data = base64.b64decode(str(encoded), validate=True)
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
            raise ImageGenError("imagegen_bad_response") from exc
        if not data:
            raise ImageGenError("imagegen_bad_response")
        return data, _detect_image_mime(data)


class SiliconFlowImageGen:
    """SiliconFlow's native image request and ``images[].url`` response surface."""

    # Native image-to-image: any `.image` kind can anchor a reference.
    reference_kinds: frozenset[str] = frozenset({"scene", "portrait", "clue"})

    def __init__(
        self,
        settings: ImageGenSettings,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._settings = settings
        self._client = client
        self._timeout = timeout

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        reference: bytes | None = None,
        reference_mime: str = "",
    ) -> tuple[bytes, str]:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ImageGenError("imagegen_bad_prompt")
        if not self._settings.model or not self._settings.api_key:
            raise ImageGenError("imagegen_not_configured")

        request_body = {
            "model": self._settings.model,
            "prompt": prompt,
            "image_size": size or self._settings.size or "1024x1024",
        }
        if reference:
            mime = reference_mime if reference_mime in {"image/jpeg", "image/png", "image/webp"} else "image/png"
            encoded_reference = base64.b64encode(reference).decode("ascii")
            request_body["image"] = f"data:{mime};base64,{encoded_reference}"

        close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            close_client = True
        try:
            response = await client.post(
                _siliconflow_image_url(self._settings.base_url),
                headers={"Authorization": f"Bearer {self._settings.api_key}"},
                json=request_body,
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise ImageGenError("imagegen_http_error", str(response.status_code))
            try:
                entry = response.json()["images"][0]
                image_url = str(entry["url"])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ImageGenError("imagegen_bad_response") from exc
            data, declared_mime = await _siliconflow_image_bytes(client, image_url)
        except httpx.TimeoutException as exc:
            raise ImageGenError("imagegen_timeout") from exc
        except httpx.HTTPError as exc:
            raise ImageGenError("imagegen_http_error") from exc
        finally:
            if close_client:
                await client.aclose()

        if not data:
            raise ImageGenError("imagegen_bad_response")
        return data, _detect_image_mime(data, declared_mime)


# 通义千问 Token Plan 的文生图走 DashScope 原生多模态接口，不是 OpenAI 兼容的
# `/images/generations`：请求体是 `{model, input.messages[*].content[*].text,
# parameters.size}`（尺寸用 `宽*高` 星号分隔），响应里图片以 URL 形式放在
# `output.choices[*].message.content[*].image`。详见
# https://platform.qianwenai.com/docs/api-reference/image-generation/qwen-text-to-image.md
QWEN_IMAGE_ENDPOINT = "/services/aigc/multimodal-generation/generation"


class QwenImageGen:
    """通义千问多模态文生图（DashScope 原生协议）HTTP client."""

    # qwen-image-3.0 / qwen-image-3.0-pro 同时支持文生图（T2I）和图生图/图像编辑（I2I）：
    # `input.messages[0].content` 可放 1-3 个 `{"image": ...}`（公网 URL 或 data:base64）
    # + 1 个 `{"text": ...}`。参考图来自本地模组/定妆字节，内联为 data URL。
    reference_kinds: frozenset[str] = frozenset({"scene", "portrait", "clue"})

    def __init__(
        self,
        settings: ImageGenSettings,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 180.0,
    ) -> None:
        self._settings = settings
        self._client = client
        self._timeout = timeout

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        reference: bytes | None = None,
        reference_mime: str = "",
    ) -> tuple[bytes, str]:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ImageGenError("imagegen_bad_prompt")
        if not self._settings.model or not self._settings.api_key:
            raise ImageGenError("imagegen_not_configured")

        # DashScope 的 size 用 `宽*高`（星号），OpenAI 兼容的 `1024x1024` 需转换。
        qwen_size = str(size or self._settings.size or "1024x1024").casefold().replace("x", "*")
        content: list[dict[str, str]] = []
        if reference:
            # I2I：参考图以 data URL 内联（本地字节，无公网 URL）。I2I 允许的格式
            # JPG/JPEG/PNG/BMP/TIFF/WEBP/GIF 覆盖 magic-bytes 检测的全部结果。
            mime = _detect_image_mime(reference, reference_mime)
            encoded = base64.b64encode(reference).decode("ascii")
            content.append({"image": f"data:{mime};base64,{encoded}"})
        content.append({"text": prompt})
        request_body = {
            "model": self._settings.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": content,
                    }
                ]
            },
            "parameters": {
                "size": qwen_size,
                "watermark": False,
                "prompt_extend": True,
            },
        }

        close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            close_client = True
        try:
            base = _base_url(self._settings).rstrip("/")
            response = await client.post(
                f"{base}{QWEN_IMAGE_ENDPOINT}",
                headers={"Authorization": f"Bearer {self._settings.api_key}"},
                json=request_body,
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise ImageGenError("imagegen_http_error", str(response.status_code))
            try:
                payload = response.json()
                content = payload["output"]["choices"][0]["message"]["content"]
                image_url = next(
                    str(item["image"]) for item in content if isinstance(item, dict) and item.get("image")
                )
            except (KeyError, IndexError, StopIteration, TypeError, ValueError) as exc:
                raise ImageGenError("imagegen_bad_response") from exc
            data, declared_mime = await _siliconflow_image_bytes(client, image_url)
        except httpx.TimeoutException as exc:
            raise ImageGenError("imagegen_timeout") from exc
        except httpx.HTTPError as exc:
            raise ImageGenError("imagegen_http_error") from exc
        finally:
            if close_client:
                await client.aclose()

        if not data:
            raise ImageGenError("imagegen_bad_response")
        return data, _detect_image_mime(data, declared_mime)


class FakeImageGen:
    """Deterministic offline image generator for tests.

    Records whether a 定妆 reference rode along (`calls[i]["reference"]` is its byte
    length as a string, `"0"` for a prompt-only request), so the M19 image discipline
    is testable without a provider."""

    def __init__(self, data: bytes = _PNG_1X1, mime: str = "image/png") -> None:
        self.data = data
        self.mime = mime
        self.calls: list[dict[str, str]] = []
        self.reference_kinds: frozenset[str] = frozenset({"scene", "portrait", "clue"})

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        reference: bytes | None = None,
        reference_mime: str = "",
    ) -> tuple[bytes, str]:
        self.calls.append(
            {
                "prompt": str(prompt),
                "size": str(size),
                "reference": str(len(reference or b"")),
                "reference_mime": reference_mime,
            }
        )
        return self.data, self.mime


def build_imagegen(
    settings: Settings,
    *,
    credentials: CredentialBook | None = None,
) -> ImageGen | None:
    """Build the configured image generator, or ``None`` when incomplete.

    For ``supergrok``, credentials come from the LLM SuperGrok subscription
    (same token as chat) — no separate imagegen key is required.
    """
    cfg = _apply_imagegen_preset(settings.imagegen)
    provider = (cfg.provider or "").casefold()

    if provider == "supergrok":
        return _build_supergrok_imagegen(cfg, credentials=credentials)

    if not cfg.provider or not cfg.model or not cfg.api_key:
        return None
    if provider == "minimax-cn":
        return MiniMaxImageGen(cfg)
    if provider == "siliconflow":
        return SiliconFlowImageGen(cfg)
    # qwen / qwen-api both speak the DashScope native multimodal-generation protocol
    # (NOT OpenAI-compatible `/images/generations`); the `qwen` Token-Plan endpoint and
    # the pay-as-you-go `qwen-api` DashScope endpoint share the same request/response shape.
    if provider in {"qwen", "qwen-api"}:
        return QwenImageGen(cfg)
    return OpenAICompatImageGen(cfg)


def _build_supergrok_imagegen(
    cfg: ImageGenSettings,
    *,
    credentials: CredentialBook | None,
) -> ImageGen | None:
    if credentials is None:
        return None
    manager = credentials.subscription_manager_sync("supergrok")
    if manager is None:
        return None
    filled = cfg.model_copy(
        update={
            "provider": "supergrok",
            # Subscription tokens must never be sent to a remembered proxy.
            "base_url": XAI_API_BASE,
            "model": cfg.model or XAI_DEFAULT_IMAGE_MODEL,
            "api_key": "",  # token_provider supplies the bearer
        }
    )
    return OpenAICompatImageGen(filled, token_provider=manager.access_token)


def _apply_imagegen_preset(cfg: ImageGenSettings) -> ImageGenSettings:
    provider = (cfg.provider or "").casefold()
    preset = IMAGEGEN_PRESETS.get(provider)
    if not preset:
        return cfg
    updates: dict[str, str] = {}
    # Use the repo-defined image endpoint/model only as DEFAULTS when the keeper left
    # them blank. A base_url the keeper actually supplied is a deliberate custom endpoint
    # (self-hosted compatible service etc.) and must be respected, never overridden.
    if not cfg.base_url:
        updates["base_url"] = preset["base_url"]
    if not cfg.model:
        updates["model"] = preset["model"]
    return cfg.model_copy(update=updates) if updates else cfg


def apply_imagegen_overrides(base: Settings, overrides: dict) -> Settings:
    filtered = {
        key: value
        for key, value in (overrides or {}).items()
        if key in IMAGEGEN_OVERRIDE_FIELDS and value is not None
    }
    if not filtered:
        return base.model_copy(deep=True)
    return base.model_copy(update={"imagegen": base.imagegen.model_copy(update=filtered)})


def describe_imagegen_settings(settings: ImageGenSettings, *, configured: bool = False) -> dict[str, object]:
    filled = _apply_imagegen_preset(settings)
    has_key = bool(filled.api_key) or (filled.provider or "").casefold() == "supergrok" and configured
    return {
        "provider": filled.provider,
        "base_url": _base_url(filled) if filled.provider else filled.base_url,
        "model": filled.model,
        "size": filled.size,
        "api_key_masked": mask_secret_tail(filled.api_key),
        "has_key": has_key,
        "configured": configured,
    }


def mask_secret_tail(value: str) -> str:
    if not value:
        return ""
    tail = value[-4:]
    return f"{'*' * max(4, len(value) - 4)}{tail}"


def _base_url(settings: ImageGenSettings) -> str:
    if settings.base_url:
        return settings.base_url
    provider = (settings.provider or "").casefold()
    preset = IMAGEGEN_PRESETS.get(provider)
    if preset:
        return preset["base_url"]
    if provider == "openai":
        return OPENAI_IMAGE_BASE_URL
    return settings.base_url


def _detect_image_mime(data: bytes, declared: object = None) -> str:
    """Return the actual image MIME from magic bytes, then a safe declaration."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if isinstance(declared, str) and declared.casefold() in {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }:
        return declared.casefold()
    # Preserve compatibility with providers that omit MIME metadata.
    return "image/png"


def _xai_dimensions(size: str) -> dict[str, str]:
    """Translate a pixel size to xAI Imagine's nearest supported dimensions."""
    try:
        width_raw, height_raw = str(size).casefold().split("x", 1)
        width, height = int(width_raw), int(height_raw)
        if width <= 0 or height <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return {"aspect_ratio": "1:1", "resolution": "1k"}

    ratio = width / height
    # Log distance treats portrait and landscape deviations symmetrically.
    aspect_ratio = min(
        _XAI_ASPECT_RATIOS,
        key=lambda item: abs(math.log(ratio / item[1])),
    )[0]
    return {
        "aspect_ratio": aspect_ratio,
        "resolution": "2k" if max(width, height) > 1024 else "1k",
    }


def _nearest_aspect_ratio(size: str, choices: tuple[tuple[str, float], ...]) -> str:
    """Translate a pixel size to the nearest provider-supported aspect ratio."""
    try:
        width_raw, height_raw = str(size).casefold().split("x", 1)
        width, height = int(width_raw), int(height_raw)
        if width <= 0 or height <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return "1:1"
    ratio = width / height
    return min(choices, key=lambda item: abs(math.log(ratio / item[1])))[0]


def _minimax_image_url(base_url: str) -> str:
    """Accept either MiniMax's API root or its documented exact image endpoint."""
    base = str(base_url or MINIMAX_IMAGE_URL).rstrip("/")
    if base.endswith("/image_generation"):
        return base
    return f"{base}/image_generation" if base.endswith("/v1") else f"{base}/v1/image_generation"


def _siliconflow_image_url(base_url: str) -> str:
    """Accept SiliconFlow's API root or its exact image-generation endpoint."""
    base = str(base_url or SILICONFLOW_IMAGE_BASE_URL).rstrip("/")
    suffix = "/images/generations"
    if base.casefold().endswith(suffix):
        return base
    return f"{base}{suffix}" if base.casefold().endswith("/v1") else f"{base}/v1{suffix}"


async def _siliconflow_image_bytes(client: httpx.AsyncClient, image_url: str) -> tuple[bytes, str]:
    """Decode a data URL or download SiliconFlow's short-lived HTTPS result."""
    if image_url.startswith("data:image/"):
        try:
            header, encoded = image_url.split(",", 1)
            if ";base64" not in header.casefold():
                raise ValueError
            mime = header[5:].split(";", 1)[0].casefold()
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ImageGenError("imagegen_bad_response") from exc
        return data, mime

    parsed = urlparse(image_url)
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        raise ImageGenError("imagegen_bad_response")
    response = await client.get(image_url, follow_redirects=True)
    if response.status_code < 200 or response.status_code >= 300:
        raise ImageGenError("imagegen_http_error", str(response.status_code))
    if len(response.content) > 32 * 1024 * 1024:
        raise ImageGenError("imagegen_bad_response")
    return response.content, response.headers.get("content-type", "").partition(";")[0]

import base64
import json
import time

import httpx
import pytest

from infra.config import ImageGenSettings, Settings
from infra.imagegen import (
    IMAGEGEN_PRESETS,
    ImageGenError,
    MiniMaxImageGen,
    OpenAICompatImageGen,
    QwenImageGen,
    SiliconFlowImageGen,
    build_imagegen,
)
from infra.oauth_flows import XAI_API_BASE, XAI_DEFAULT_IMAGE_MODEL, SubscriptionToken
from infra.runtime_config import CredentialBook
from infra.store import Store


async def test_openai_compat_imagegen_posts_expected_shape_and_decodes_b64():
    image_bytes = b"png-bytes"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = request.read()
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii")}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gen = OpenAICompatImageGen(
        ImageGenSettings(provider="openai", base_url="https://example.test/v1", api_key="secret", model="img"),
        client=client,
    )
    try:
        data, mime = await gen.generate("a portrait", size="512x512")
    finally:
        await client.aclose()

    assert data == image_bytes
    assert mime == "image/png"
    assert seen["url"] == "https://example.test/v1/images/generations"
    assert seen["auth"] == "Bearer secret"
    assert b'"model":"img"' in seen["json"]
    assert b'"response_format":"b64_json"' in seen["json"]


async def test_minimax_imagegen_uses_native_endpoint_shape_and_decodes_base64():
    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {"image_base64": [base64.b64encode(image_bytes).decode("ascii")]}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gen = MiniMaxImageGen(
        ImageGenSettings(
            provider="minimax-cn",
            base_url="https://api.minimaxi.com/v1/image_generation",
            api_key="secret",
            model="image-01",
        ),
        client=client,
    )
    try:
        data, mime = await gen.generate("a moonlit library", size="1792x1024")
    finally:
        await client.aclose()

    assert data == image_bytes
    assert mime == "image/png"
    assert seen["url"] == "https://api.minimaxi.com/v1/image_generation"
    assert seen["auth"] == "Bearer secret"
    assert seen["json"] == {
        "model": "image-01",
        "prompt": "a moonlit library",
        "aspect_ratio": "16:9",
        "response_format": "base64",
        "n": 1,
    }


async def test_minimax_imagegen_inlines_base64_reference():
    ref_bytes = b"\x89PNG\r\n\x1a\nportrait-bytes"
    class _Transport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.seen = {}
        async def handle_async_request(self, request):
            self.seen["url"] = str(request.url)
            self.seen["auth"] = request.headers.get("authorization")
            self.seen["json"] = json.loads(request.content)
            payload = {
                "data": {"image_base64": [base64.b64encode(b"\x89PNG\r\n\x1a\nout").decode()]},
                "metadata": {"success_count": 1, "failed_count": 0},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
            return httpx.Response(200, json=payload, request=request)

    t = _Transport()
    client = httpx.AsyncClient(transport=t)
    gen = MiniMaxImageGen(ImageGenSettings(provider="minimax-cn", api_key="secret", model="image-01"), client=client)
    try:
        data, mime = await gen.generate("portrait", reference=ref_bytes, reference_mime="image/png")
    finally:
        await client.aclose()

    assert mime == "image/png"
    # The reference rides as a base64 Data URL in subject_reference (character).
    assert t.seen["json"]["subject_reference"] == [
        {"type": "character", "image_file": "data:image/png;base64,iVBORw0KGgpwb3J0cmFpdC1ieXRlcw=="}
    ]


async def test_siliconflow_imagegen_uses_native_shape_and_decodes_data_url():
    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return httpx.Response(200, json={"images": [{"url": f"data:image/png;base64,{encoded}"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gen = SiliconFlowImageGen(
        ImageGenSettings(
            provider="siliconflow",
            base_url="https://api.siliconflow.cn/v1",
            api_key="secret",
            model="Kwai-Kolors/Kolors",
        ),
        client=client,
    )
    try:
        data, mime = await gen.generate(
            "a lighthouse",
            size="1024x1024",
            reference=b"reference",
            reference_mime="image/jpeg",
        )
    finally:
        await client.aclose()

    assert data == image_bytes
    assert mime == "image/png"
    assert seen["url"] == "https://api.siliconflow.cn/v1/images/generations"
    assert seen["json"]["image_size"] == "1024x1024"
    assert seen["json"]["image"].startswith("data:image/jpeg;base64,")
    assert "response_format" not in seen["json"]


async def test_siliconflow_imagegen_downloads_short_lived_https_result():
    image_bytes = b"\xff\xd8\xff\xe0jpeg"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"images": [{"url": "https://cdn.example.test/result"}]})
        return httpx.Response(200, content=image_bytes, headers={"content-type": "image/jpeg"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gen = SiliconFlowImageGen(
        ImageGenSettings(provider="siliconflow", api_key="secret", model="Kwai-Kolors/Kolors"),
        client=client,
    )
    try:
        data, mime = await gen.generate("a lighthouse")
    finally:
        await client.aclose()

    assert data == image_bytes
    assert mime == "image/jpeg"


async def test_siliconflow_imagegen_refuses_non_https_result_url():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"images": [{"url": "http://127.0.0.1/private"}]})
        )
    )
    gen = SiliconFlowImageGen(
        ImageGenSettings(provider="siliconflow", api_key="secret", model="Kwai-Kolors/Kolors"),
        client=client,
    )
    try:
        with pytest.raises(ImageGenError) as exc:
            await gen.generate("bad response")
    finally:
        await client.aclose()

    assert exc.value.code == "imagegen_bad_response"


async def test_openai_compat_imagegen_uses_magic_bytes_before_declared_mime():
    image_bytes = b"\xff\xd8\xff\xe0jpeg"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "b64_json": base64.b64encode(image_bytes).decode("ascii"),
                            "mime_type": "image/png",
                        }
                    ]
                },
            )
        )
    )
    gen = OpenAICompatImageGen(
        ImageGenSettings(provider="openai", api_key="secret", model="img"),
        client=client,
    )
    try:
        data, mime = await gen.generate("a portrait")
    finally:
        await client.aclose()

    assert data == image_bytes
    assert mime == "image/jpeg"


async def test_token_provider_preferred_over_api_key():
    seen = {}

    async def provider() -> str:
        return "oauth-bearer"

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"x").decode("ascii")}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gen = OpenAICompatImageGen(
        ImageGenSettings(provider="supergrok", base_url=XAI_API_BASE, api_key="static-key", model="grok-imagine-image"),
        client=client,
        token_provider=provider,
    )
    try:
        await gen.generate("a scene")
    finally:
        await client.aclose()
    assert seen["auth"] == "Bearer oauth-bearer"


async def test_supergrok_uses_xai_dimensions_instead_of_openai_size():
    seen = {}

    async def provider() -> str:
        return "oauth-bearer"

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(b"x").decode("ascii")}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gen = OpenAICompatImageGen(
        ImageGenSettings(
            provider="supergrok",
            base_url=XAI_API_BASE,
            model="grok-imagine-image",
        ),
        client=client,
        token_provider=provider,
    )
    try:
        await gen.generate("a landscape", size="1792x1024")
    finally:
        await client.aclose()

    assert "size" not in seen["json"]
    assert seen["json"]["aspect_ratio"] == "16:9"
    assert seen["json"]["resolution"] == "2k"


async def test_supergrok_preset_build_uses_llm_subscription():
    store = Store(":memory:")
    book = CredentialBook(store)
    await book.save_subscription(
        "supergrok",
        SubscriptionToken("gat", "grt", time.time() + 3600),
    )
    settings = Settings(
        imagegen=ImageGenSettings(provider="supergrok", base_url="https://stale-proxy.example/v1")
    )
    gen = build_imagegen(settings, llm_credentials=book)
    assert gen is not None
    assert isinstance(gen, OpenAICompatImageGen)
    assert gen._settings.model == XAI_DEFAULT_IMAGE_MODEL
    assert gen._settings.base_url == XAI_API_BASE
    assert gen._token_provider is not None
    assert IMAGEGEN_PRESETS["supergrok"]["model"] == XAI_DEFAULT_IMAGE_MODEL


def test_build_imagegen_selects_qwen_dashscope_adapter_from_preset():
    """qwen 的 imagegen preset 指向 Token Plan 多模态端点，构建时选择 QwenImageGen
    （DashScope 原生协议），而不是 OpenAI 兼容适配器。"""
    settings = Settings(
        imagegen=ImageGenSettings(provider="qwen", api_key="sk-sp-secret", model="qwen-image-3.0-pro")
    )
    gen = build_imagegen(settings)

    assert isinstance(gen, QwenImageGen)
    assert IMAGEGEN_PRESETS["qwen"]["base_url"] == (
        "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1"
    )


async def test_openai_compat_imagegen_maps_bad_response_to_error_code():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"data": [{}]})))
    gen = OpenAICompatImageGen(
        ImageGenSettings(provider="openai", base_url="https://example.test/v1", api_key="secret", model="img"),
        client=client,
    )
    try:
        with pytest.raises(ImageGenError) as exc:
            await gen.generate("bad")
    finally:
        await client.aclose()

    assert exc.value.code == "imagegen_bad_response"


async def test_openai_compat_imagegen_maps_http_failure_to_error_code():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500, text="nope")))
    gen = OpenAICompatImageGen(
        ImageGenSettings(provider="openai", base_url="https://example.test/v1", api_key="secret", model="img"),
        client=client,
    )
    try:
        with pytest.raises(ImageGenError) as exc:
            await gen.generate("bad")
    finally:
        await client.aclose()

    assert exc.value.code == "imagegen_http_error"


def test_build_imagegen_returns_none_when_incomplete():
    # An explicit empty block, not the developer's .env: "incomplete" is what is under test.
    assert build_imagegen(Settings(imagegen=ImageGenSettings())) is None
    assert build_imagegen(Settings(imagegen=ImageGenSettings(provider="openai", model="img"))) is None


async def test_qwen_imagegen_posts_dashscope_shape_and_downloads_image_url():
    """Token Plan 文生图走 DashScope 原生多模态接口（不是 OpenAI /images/generations）：
    请求体为 `input.messages[*].content[*].text` + `parameters.size`（宽*高），图片以 URL
    返回在 `output.choices[*].message.content[*].image`，需要二次下载。"""
    image_bytes = b"\x89PNG\r\n\x1a\nqwen-image"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/services/aigc/multimodal-generation/generation"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "choices": [
                            {
                                "message": {
                                    "content": [
                                        {"image": "https://cdn.example.test/img.png"},
                                        {"text": "done"},
                                    ]
                                }
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/img.png"):
            return httpx.Response(200, content=image_bytes, headers={"content-type": "image/png"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gen = QwenImageGen(
        ImageGenSettings(
            provider="qwen",
            base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1",
            api_key="sk-sp-secret",
            model="qwen-image-3.0-pro",
        ),
        client=client,
    )
    try:
        data, mime = await gen.generate("一只猫", size="1024x768")
    finally:
        await client.aclose()

    assert data == image_bytes
    assert mime == "image/png"


async def test_qwen_imagegen_posts_expected_json_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={"output": {"choices": [{"message": {"content": [{"image": "https://cdn.example.test/a.png"}]}}]}},
        )

    async def image_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nbytes", headers={"content-type": "image/png"})

    def router(request: httpx.Request) -> httpx.Response:
        return image_handler(request) if request.url.path.endswith("/a.png") else handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(router))
    gen = QwenImageGen(
        ImageGenSettings(
            provider="qwen",
            base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1",
            api_key="sk-sp-secret",
            model="qwen-image-3.0-pro",
        ),
        client=client,
    )
    try:
        await gen.generate("a cat", size="512x512")
    finally:
        await client.aclose()

    assert seen["url"] == (
        "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1"
        "/services/aigc/multimodal-generation/generation"
    )
    assert seen["auth"] == "Bearer sk-sp-secret"
    body = seen["json"]
    assert body["model"] == "qwen-image-3.0-pro"
    assert body["input"]["messages"][0]["role"] == "user"
    assert body["input"]["messages"][0]["content"][0]["text"] == "a cat"
    assert body["parameters"]["size"] == "512*512"  # DashScope 用星号，非 OpenAI 的 x


async def test_qwen_imagegen_maps_http_failure_to_error_code():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500, text="nope")))
    gen = QwenImageGen(
        ImageGenSettings(provider="qwen", base_url="https://example.test/api/v1", api_key="secret", model="img"),
        client=client,
    )
    try:
        with pytest.raises(ImageGenError) as exc:
            await gen.generate("bad")
    finally:
        await client.aclose()

    assert exc.value.code == "imagegen_http_error"

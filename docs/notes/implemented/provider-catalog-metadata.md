# Provider catalog metadata

The engine owns the model-provider catalog and returns it in every keeper
`admin_config` frame. Each entry contains the stable provider ID, the effective
default Base URL, and the authentication mode. Clients render this metadata and
do not carry provider-specific endpoint tables.

`providers` is the ID-only projection of `provider_catalog` for clients that
only need provider names. Provider behavior, metadata, validation, and the OpenAI-compatible factory share
`infra/providers.py` as their source.

MiniMax's China endpoint uses the OpenAI-compatible API at
`https://api.minimaxi.com/v1`. The endpoint and `/models` behavior are pinned by
tests and sourced from the official MiniMax API documentation:
<https://platform.minimaxi.com/docs/api-reference/text-openai-api>.

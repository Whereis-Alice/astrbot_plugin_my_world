"""Reuse AstrBot embedding providers without copying credentials or clients."""
from __future__ import annotations

import hashlib
import json


class AstrBotEmbedding:
    def __init__(self, plugin):
        self.plugin = plugin
        self.update_schema()

    def describe(self, provider_id: str = "") -> dict:
        selected = str(provider_id or self.plugin.config.get("embedding_provider_id") or "").strip()
        try:
            provider = self.plugin.context.get_provider_by_id(selected) if selected else None
        except Exception:
            provider = None
        if not provider or not callable(getattr(provider, "get_embeddings", None)):
            return {"id": selected, "model": "", "ready": False, "fingerprint": selected}
        config = getattr(provider, "provider_config", {}) or {}
        try:
            model = str(provider.get_model() or config.get("embedding_model") or config.get("model") or "")
        except Exception:
            return {"id": selected, "model": "", "ready": False, "fingerprint": selected}
        identity = {key: config.get(key) for key in ("type", "api_base", "base_url", "dimensions", "embedding_dimensions")}
        fingerprint = hashlib.sha256(json.dumps([selected, model, identity], sort_keys=True, default=str).encode()).hexdigest()
        return {"id": selected, "model": model, "ready": True, "fingerprint": fingerprint}

    def choices(self) -> list[dict]:
        getter = getattr(self.plugin.context, "get_all_embedding_providers", None)
        rows = []
        try:
            providers = getter() if callable(getter) else []
        except Exception:
            providers = []
        for provider in providers:
            try:
                meta = provider.meta()
                rows.append({"id": str(meta.id), "model": str(meta.model or (getattr(provider, "provider_config", {}) or {}).get("embedding_model") or "")})
            except Exception:
                continue
        return rows

    def update_schema(self) -> None:
        """Dashboard reads this in-memory schema; never rewrite the schema file."""
        schema = getattr(self.plugin.config, "schema", None)
        if isinstance(schema, dict) and "embedding_provider_id" in schema:
            current = str(self.plugin.config.get("embedding_provider_id") or "")
            schema["embedding_provider_id"]["options"] = list(dict.fromkeys(["", *[row["id"] for row in self.choices()], *([current] if current else [])]))

    async def embed(self, texts: list[str], provider_id: str = "") -> list[list[float]]:
        info = self.describe(provider_id)
        if not info["ready"]:
            raise ValueError("未选择可用的 AstrBot 嵌入模型")
        return await self.plugin.context.get_provider_by_id(info["id"]).get_embeddings(texts)

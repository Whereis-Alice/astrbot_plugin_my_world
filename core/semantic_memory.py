"""Optional OpenAI-compatible embeddings. Facts are primary; vectors are rebuildable cache."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
import urllib.request
from uuid import uuid4

from .llm_usage import normalized_usage, record_llm_usage
from .model_calls import call_context, model_context


def revision(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def cosine(a: list, b: list) -> float:
    if len(a) != len(b) or not a:
        return 0
    divisor = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / divisor if divisor else 0


class SemanticMemory:
    def __init__(self, memory):
        self.memory = memory
        self.scores: dict[tuple, dict[int, float]] = {}
        self.score_versions: dict[tuple, dict[int, str]] = {}
        self.score_times: dict[tuple, float] = {}
        self.locks: dict[tuple, asyncio.Lock] = {}
        self.retry_at = 0.0
        self.status = "未启用"

    def _key(self, session: str, persona: str, query: str, mode: str = "", *, automatic: bool = False) -> tuple:
        config = self.memory.world.memory
        return session, persona, revision(query), config.semantic_endpoint, config.semantic_model, mode or self.memory.world.memory_scope_mode, config.automatic_recall if automatic else "explicit"

    def cached(self, session: str, persona: str, query: str, *, mode: str = "", automatic: bool = False) -> dict[int, float]:
        key = self._key(session, persona, query, mode, automatic=automatic)
        scores = self.scores.get(key, {})
        if not scores or time.time() - self.score_times.get(key, 0) > 300:
            return {}
        marks = ",".join("?" for _ in scores)
        rows = self.memory.db._query(f"SELECT id,content FROM node_memory WHERE id IN ({marks})", list(scores))
        versions = self.score_versions.get(key, {})
        return {row["id"]: scores[row["id"]] for row in rows if versions.get(row["id"]) == revision(row["content"])}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        config = self.memory.world.memory
        url = config.semantic_endpoint.rstrip("/")
        if not url.endswith("/embeddings"):
            url += "/embeddings"
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get(config.semantic_api_key_env, "") if config.semantic_api_key_env else ""
        if secret:
            headers["Authorization"] = "Bearer " + secret
        body = json.dumps({"model": config.semantic_model, "input": texts}).encode()
        started, clock = time.time(), time.monotonic()
        data, status, error = {}, "success", ""
        def request():
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=8) as response:
                data = json.load(response)
            return data
        try:
            data = await asyncio.to_thread(request)
            values = [item["embedding"] for item in sorted(data["data"], key=lambda item: item["index"])]
            if len(values) != len(texts) or not values or not values[0] or any(
                    not isinstance(vector, list) or len(vector) != len(values[0]) or len(vector) > 16384
                    or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector) for vector in values):
                raise ValueError("嵌入响应格式无效")
            if isinstance(data.get("usage"), dict):
                data["usage"] = {**data["usage"], "output_tokens": 0}
            record_llm_usage(data.get("usage"))
        except asyncio.CancelledError:
            status, error = "cancelled", "嵌入请求等待被取消；上游可能已经处理"
            raise
        except Exception:
            status, error = "error", "嵌入服务未返回有效响应，使用关键词召回"
            raise
        finally:
            context = call_context()
            usage = normalized_usage(data.get("usage"))
            row = {"started_at": started, "duration_ms": round((time.monotonic() - clock) * 1000),
                   "session_id": context.get("session_id", ""), "provider_id": "semantic_embedding", "model": config.semantic_model,
                   "purpose": "embedding", "reason": context.get("reason") or "可选语义记忆召回与索引更新",
                   "chain": context.get("chain", []), "role": "embedding", "trace_id": context.get("trace_id") or uuid4().hex,
                   "status": status, "error": error, "coverage": "direct", "usage": usage, "attempt": 1}
            try:
                self.memory.db.append_model_call(row)
            except Exception:
                pass  # A ledger storage failure must not discard facts or disable lexical recall.
        return values

    async def prepare(self, session: str, persona: str, query: str, *, mode: str = "", automatic: bool = False) -> None:
        config = self.memory.world.memory
        if not config.semantic_enabled or not config.semantic_endpoint or not config.semantic_model or not query.strip() or time.time() < self.retry_at:
            return
        if automatic and (config.automatic_recall == "manual" or config.recall_max_chars == 0):
            return
        key = self._key(session, persona, query, mode, automatic=automatic)
        if self.cached(session, persona, query, mode=mode, automatic=automatic):
            return
        scope = key[:2]
        lock = self.locks.setdefault(scope, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            try:
                # Apply the exact same privacy rules BEFORE sending any text to an embedding provider.
                candidates = await asyncio.to_thread(self.memory._query, session, persona, mode or self.memory.world.memory_scope_mode,
                    session_ids=self.memory.sessions(session))
                if not candidates:
                    return
                model_key = revision(config.semantic_endpoint + "|" + config.semantic_model)
                known = {int(row["memory_id"]): dict(row) for row in self.memory.db._query(
                    "SELECT memory_id,revision,vector FROM memory_vector WHERE model_key=?", (model_key,))}
                missing = [row for row in candidates if row["id"] not in known or known[row["id"]]["revision"] != revision(row["content"])]
                # Bounded rebuild each call; lexical recall continues during a larger rebuild.
                missing = missing[:32]
                with model_context("embedding", "按当前会话范围召回记忆并补充可重建的语义索引", session):
                    vectors = await self.embed([query, *(str(row["content"])[:4000] for row in missing)])
                query_vector = vectors[0]
                with self.memory.db.atomic():
                    for row, vector in zip(missing, vectors[1:]):
                        digest = revision(row["content"])
                        self.memory.db._execute("INSERT INTO memory_vector(memory_id,model_key,revision,vector) VALUES(?,?,?,?) "
                            "ON CONFLICT(memory_id,model_key) DO UPDATE SET revision=excluded.revision,vector=excluded.vector",
                            (row["id"], model_key, digest, json.dumps(vector)))
                        known[row["id"]] = {"revision": digest, "vector": json.dumps(vector)}
                self.scores[key] = {row["id"]: cosine(query_vector, json.loads(known[row["id"]]["vector"])) for row in candidates
                    if row["id"] in known and known[row["id"]]["revision"] == revision(row["content"])}
                self.scores = dict(list(self.scores.items())[-64:])
                self.score_versions[key] = {row["id"]: revision(row["content"]) for row in candidates}
                self.score_times[key] = time.time()
                self.score_versions = {k: v for k, v in self.score_versions.items() if k in self.scores}
                self.score_times = {k: v for k, v in self.score_times.items() if k in self.scores}
                self.status = f"已就绪 · 本次可检索 {len(self.scores[key])} 条"
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status = "嵌入暂不可用，已使用关键词召回"
                self.retry_at = time.time() + 60

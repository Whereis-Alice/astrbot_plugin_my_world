"""Optional native or compatible embeddings; vectors are a rebuildable cache."""
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


def valid_vector(vector) -> bool:
    if not isinstance(vector, list) or not 0 < len(vector) <= 16384:
        return False
    try:
        return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vector)
    except (ValueError, OverflowError):
        return False


def cosine(a: list, b: list) -> float:
    if not valid_vector(a) or not valid_vector(b) or len(a) != len(b):
        return 0
    # Scale first: finite provider values can still overflow squared norms.
    scale_a, scale_b = max(map(abs, a)), max(map(abs, b))
    if not scale_a or not scale_b:
        return 0
    a, b = [x / scale_a for x in a], [x / scale_b for x in b]
    divisor = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return max(-1.0, min(1.0, sum(x*y for x, y in zip(a, b)) / divisor)) if divisor else 0


class SemanticMemory:
    def __init__(self, memory):
        self.memory = memory
        self.scores: dict[tuple, dict[int, float]] = {}
        self.score_versions: dict[tuple, dict[int, str]] = {}
        self.score_times: dict[tuple, float] = {}
        self.locks: dict[tuple, asyncio.Lock] = {}
        self.retry_at = 0.0
        self.status = "未启用"
        self.provider = None

    def model_identity(self) -> str:
        config = self.memory.world.memory
        if config.semantic_source == "provider":
            info = self.provider.describe(config.semantic_provider_id) if self.provider else {}
            return "provider:" + str(info.get("fingerprint") or config.semantic_provider_id)
        return config.semantic_endpoint + "|" + config.semantic_model

    def _key(self, session: str, persona: str, query: str, mode: str = "", *, automatic: bool = False,
             life_persona_key: str | None = None) -> tuple:
        config = self.memory.world.memory
        return session, persona, revision(query), self.model_identity(), mode or self.memory.world.memory_scope_mode, config.automatic_recall if automatic else "explicit", life_persona_key

    def cached(self, session: str, persona: str, query: str, *, mode: str = "", automatic: bool = False,
               life_persona_key: str | None = None) -> dict[int, float]:
        now = time.time()
        for old, at in list(self.score_times.items()):
            if now - at > 300:
                self.scores.pop(old, None)
                self.score_versions.pop(old, None)
                self.score_times.pop(old, None)
        key = self._key(session, persona, query, mode, automatic=automatic, life_persona_key=life_persona_key)
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
        native = config.semantic_source == "provider" and self.provider is not None
        info = self.provider.describe(config.semantic_provider_id) if native else {}
        started, clock = time.time(), time.monotonic()
        data, status, error = {}, "success", ""
        def request():
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=8) as response:
                data = json.load(response)
            return data
        try:
            if config.semantic_source == "provider":
                if not native:
                    raise ValueError("宿主未提供嵌入模型")
                async with asyncio.timeout(8):
                    values = await self.provider.embed(texts, config.semantic_provider_id)
            else:
                data = await asyncio.to_thread(request)
                values = [item["embedding"] for item in sorted(data["data"], key=lambda item: item["index"])]
            if len(values) != len(texts) or not values or any(
                    not valid_vector(vector) or len(vector) != len(values[0]) for vector in values):
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
                   "session_id": context.get("session_id", ""), "provider_id": info.get("id") or "semantic_embedding", "model": info.get("model") or config.semantic_model,
                   "purpose": "embedding", "reason": context.get("reason") or "可选语义记忆召回与索引更新",
                   "chain": context.get("chain", []), "role": "embedding", "trace_id": context.get("trace_id") or uuid4().hex,
                   "status": status, "error": error, "coverage": "direct", "usage": usage, "attempt": 1}
            try:
                self.memory.db.append_model_call(row)
            except Exception:
                pass  # A ledger storage failure must not discard facts or disable lexical recall.
        return values

    async def prepare(self, session: str, persona: str, query: str, *, mode: str = "", automatic: bool = False,
                      life_persona_key: str | None = None) -> None:
        config = self.memory.world.memory
        if not config.semantic_enabled or not query.strip() or time.time() < self.retry_at:
            return
        if config.semantic_source == "provider":
            if not self.provider or not self.provider.describe(config.semantic_provider_id).get("ready"):
                self.status = "未选择可用的 AstrBot 嵌入模型，使用关键词召回"
                return
        elif not config.semantic_endpoint or not config.semantic_model:
            self.status = "嵌入服务配置不完整，使用关键词召回"
            return
        if automatic and (config.automatic_recall == "manual" or config.recall_max_chars == 0):
            return
        key = self._key(session, persona, query, mode, automatic=automatic, life_persona_key=life_persona_key)
        scope = key[:2]
        lock = self.locks.setdefault(scope, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            try:
                # Apply the exact same privacy rules BEFORE sending any text to an embedding provider.
                candidates = await asyncio.to_thread(self.memory._query, session, persona, mode or self.memory.world.memory_scope_mode,
                    session_ids=self.memory.sessions(session), life_persona_key=life_persona_key)
                if not candidates:
                    return
                model_key = revision(self.model_identity())
                ids = [row["id"] for row in candidates]
                known = {}
                # Read revisions only, and only for the already authorised
                # candidates. A large vector library must not be loaded per query.
                for start in range(0, len(ids), 128):
                    batch = ids[start:start+128]
                    marks = ",".join("?" for _ in batch)
                    known.update({int(row["memory_id"]): row["revision"] for row in self.memory.db._query(
                        f"SELECT memory_id,revision FROM memory_vector WHERE model_key=? AND memory_id IN ({marks})",
                        (model_key, *batch))})
                versions = {row["id"]: revision(row["content"]) for row in candidates}
                missing = [row for row in candidates if known.get(row["id"]) != versions[row["id"]]]
                if (not missing and versions == self.score_versions.get(key)
                        and self.cached(session, persona, query, mode=mode, automatic=automatic,
                                        life_persona_key=life_persona_key)):
                    return
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
                del vectors
                scores = {}
                # Read at most sixteen encoded vectors per batch. Keep
                # only scalar similarities in the short-lived query cache.
                for start in range(0, len(ids), 16):
                    batch = ids[start:start+16]
                    marks = ",".join("?" for _ in batch)
                    rows = self.memory.db._query(
                        f"SELECT memory_id,revision,vector FROM memory_vector WHERE model_key=? AND memory_id IN ({marks})",
                        (model_key, *batch))
                    for row in rows:
                        if row["revision"] == versions[row["memory_id"]]:
                            try:
                                vector = json.loads(row["vector"])
                            except (ValueError, TypeError):
                                vector = None
                            if not valid_vector(vector) or len(vector) != len(query_vector):
                                # Only this rebuildable cache row is invalid; keep
                                # other semantic candidates and rebuild it next time.
                                self.memory.db._execute(
                                    "DELETE FROM memory_vector WHERE memory_id=? AND model_key=? AND revision=?",
                                    (row["memory_id"], model_key, row["revision"]))
                                continue
                            scores[row["memory_id"]] = cosine(query_vector, vector)
                self.scores.pop(key, None)
                self.scores[key] = scores
                self.scores = dict(list(self.scores.items())[-16:])
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
            finally:
                if self.locks.get(scope) is lock:
                    self.locks.pop(scope, None)

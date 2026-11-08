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
        self.query_vectors: dict[tuple, list[float]] = {}
        self.query_vector_times: dict[tuple, float] = {}
        self.context_scores: dict[tuple, dict[str, float]] = {}
        self.context_vectors: dict[tuple, list[float]] = {}
        self.context_vector_times: dict[tuple, float] = {}
        # Short-lived scores for ephemeral context (for example an active
        # visit transcript). These rows are deliberately not persisted: only
        # durable life memories get a rebuildable SQLite vector index.
        self.relevance_scores: dict[tuple, float] = {}
        self.relevance_times: dict[tuple, float] = {}
        self.locks: dict[tuple, asyncio.Lock] = {}
        self.retry_at = 0.0
        self.status = "未启用"
        self.provider = None

    def model_identity(self) -> str:
        config = self.memory.world.memory
        if config.semantic_source == "provider":
            try:
                info = self.provider.describe(config.semantic_provider_id) if self.provider else {}
            except Exception:
                info = {}
            return "provider:" + str(info.get("fingerprint") or config.semantic_provider_id)
        return config.semantic_endpoint + "|" + config.semantic_model

    def _key(self, session: str, persona: str, query: str, mode: str = "", *, automatic: bool = False,
             life_persona_key: str | None = None, node_id: str = "") -> tuple:
        config = self.memory.world.memory
        return (session, persona, revision(self._query_text(query)), self.model_identity(),
                mode or self.memory.world.memory_scope_mode,
                config.automatic_recall if automatic else "explicit", life_persona_key, str(node_id or ""))

    def cached(self, session: str, persona: str, query: str, *, mode: str = "", automatic: bool = False,
               life_persona_key: str | None = None, node_id: str = "") -> dict[int, float]:
        now = time.time()
        for old, at in list(self.score_times.items()):
            if now - at > 300:
                self.scores.pop(old, None)
                self.score_versions.pop(old, None)
                self.score_times.pop(old, None)
                self.query_vectors.pop(old, None)
                self.query_vector_times.pop(old, None)
                self.context_scores.pop(old, None)
        key = self._key(session, persona, query, mode, automatic=automatic,
                        life_persona_key=life_persona_key, node_id=node_id)
        scores = self.scores.get(key, {})
        if not scores or time.time() - self.score_times.get(key, 0) > 300:
            return {}
        marks = ",".join("?" for _ in scores)
        rows = self.memory.db._query(f"SELECT id,content FROM node_memory WHERE id IN ({marks})", list(scores))
        versions = self.score_versions.get(key, {})
        return {row["id"]: scores[row["id"]] for row in rows if versions.get(row["id"]) == revision(row["content"])}

    def cached_context(self, session: str, persona: str, queries: list[str], *,
                       life_persona_key: str = "", node_id: str = "") -> dict[str, float]:
        """Reuse source scores without carrying source text into another turn."""
        result: dict[str, float] = {}
        threshold = self.memory.world.memory.semantic_min_similarity
        for query in queries:
            key = self._key(session, persona, query, automatic=True,
                            life_persona_key=life_persona_key, node_id=node_id)
            if time.time() - self.score_times.get(key, 0) > 300:
                continue
            for identity, score in self.context_scores.get(key, {}).items():
                if score >= threshold:
                    result[identity] = max(score, result.get(identity, 0))
        return result

    def _ready(self) -> bool:
        """Return whether the configured embedding route can be called."""

        config = self.memory.world.memory
        if not config.semantic_enabled:
            return False
        if config.semantic_source == "provider":
            if not self.provider:
                self.status = "未选择可用的 AstrBot 嵌入模型，使用关键词召回"
                return False
            try:
                ready = bool(self.provider.describe(config.semantic_provider_id).get("ready"))
            except Exception:
                ready = False
            if not ready:
                self.status = "未选择可用的 AstrBot 嵌入模型，使用关键词召回"
            return ready
        if not config.semantic_endpoint or not config.semantic_model:
            self.status = "嵌入服务配置不完整，使用关键词召回"
            return False
        return True

    async def related(self, session: str, query: str, candidates: list[str] | tuple[str, ...], *,
                      threshold: float | None = None, cache_key: str = "") -> bool:
        """Check whether an ephemeral context is semantically about ``query``.

        This is intentionally separate from :meth:`prepare`: active scenes and
        visit transcripts are not durable memories and must not be inserted into
        the SQLite vector index. A small in-memory cache prevents repeated hook
        passes for the same message from issuing another embedding request.
        Callers should perform a cheap lexical gate first when possible.
        """

        config = self.memory.world.memory
        query = self._query_text(query)
        texts = [" ".join(str(item or "").split()).strip() for item in (candidates or ())]
        texts = [item[:4000] for item in texts if item]
        if not query or not texts or time.time() < self.retry_at or not self._ready():
            return False
        threshold = config.semantic_min_similarity if threshold is None else float(threshold)
        digest = (session, self.model_identity(), revision(query),
                  tuple(revision(item) for item in texts), str(cache_key or ""),
                  round(float(threshold), 4))
        now = time.time()
        if now - self.relevance_times.get(digest, 0.0) <= 300:
            return self.relevance_scores.get(digest, 0.0) >= threshold
        try:
            with model_context("embedding", "判断本轮消息是否与当前生活片段相关", session):
                vectors = await self.embed([query, *texts])
            score = max((cosine(vectors[0], vector) for vector in vectors[1:]), default=0.0)
            self.relevance_scores[digest] = score
            self.relevance_times[digest] = now
            # Keep this cache bounded independently from durable-memory scores.
            if len(self.relevance_scores) > 64:
                keep = sorted(self.relevance_times, key=self.relevance_times.get, reverse=True)[:64]
                self.relevance_scores = {key: self.relevance_scores[key] for key in keep}
                self.relevance_times = {key: self.relevance_times[key] for key in keep}
            return score >= threshold
        except asyncio.CancelledError:
            raise
        except Exception:
            self.status = "嵌入暂不可用，已使用关键词召回"
            self.retry_at = time.time() + 60
            return False

    @staticmethod
    def _query_text(query: str) -> str:
        """Keep embedding input bounded while retaining the newest chat turns."""

        text = " ".join(str(query or "").split()).strip()
        return text[-2000:]

    async def prepare(self, session: str, persona: str, query: str, *, mode: str = "", automatic: bool = False,
                      life_persona_key: str | None = None, node_id: str = "",
                      nodes: list[str] | None = None, memory_type: str = "",
                      participants: list[str] | None = None, exclude_source_scoped: bool = False) -> None:
        await self.prepare_many(session, persona, [query], mode=mode, automatic=automatic,
                                life_persona_key=life_persona_key, node_id=node_id,
                                nodes=nodes, memory_type=memory_type, participants=participants,
                                exclude_source_scoped=exclude_source_scoped)

    async def prepare_many(self, session: str, persona: str, queries: list[str] | tuple[str, ...], *,
                           mode: str = "", automatic: bool = False,
                           life_persona_key: str | None = None, node_id: str = "",
                           task: str = "reply", people: tuple[str, ...] = (),
                           context_texts: dict[str, str] | None = None,
                           nodes: list[str] | None = None, memory_type: str = "",
                           participants: list[str] | None = None, exclude_source_scoped: bool = False) -> None:
        """Embed a request and related speaker queries with one provider call."""

        config = self.memory.world.memory
        context_texts = {key: str(value).strip()[:4000] for key, value in (context_texts or {}).items()
                         if str(value).strip()}
        context_texts = dict(list(context_texts.items())[:64])
        unique: list[str] = []
        seen: set[str] = set()
        for query in queries:
            text = self._query_text(query)
            if text and text not in seen:
                unique.append(text)
                seen.add(text)
            if len(unique) >= 7:
                break
        if (not unique or time.time() < self.retry_at or not self._ready()
                or automatic and (config.automatic_recall == "manual" or config.recall_max_chars == 0)):
            return
        keys = [(query, self._key(session, persona, query, mode, automatic=automatic,
                                  life_persona_key=life_persona_key, node_id=node_id)) for query in unique]
        scope = keys[0][1][:2]
        lock = self.locks.setdefault(scope, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            try:
                # Candidate text is read only after the same session, persona
                # and life ownership filters used by normal memory recall.
                candidates = await asyncio.to_thread(
                    self.memory._query, session, persona,
                    mode or self.memory.world.memory_scope_mode,
                    session_ids=self.memory.sessions(session),
                    life_persona_key=life_persona_key, nodes=nodes, memory_type=memory_type,
                    participants=participants, exclude_source_scoped=exclude_source_scoped)
                if node_id:
                    node_rows = await asyncio.to_thread(
                        self.memory.db.query_memories,
                        source_session=session, session_ids=self.memory.sessions(session),
                        scope="node", node_id=node_id, life_persona_key=life_persona_key,
                        memory_type=memory_type or None, participants=participants,
                        exclude_source_scoped=exclude_source_scoped)
                    existing_ids = {int(row["id"]) for row in candidates}
                    candidates.extend(
                        row for row in node_rows
                        if int(row["id"]) not in existing_ids
                        and (not persona or not row.get("persona_id") or row.get("persona_id") == persona)
                    )
                if automatic:
                    from .life_memory import scoped_emotion
                    present_people = {str(item) for item in people if str(item)}
                    filtered = []
                    for row in candidates:
                        related = {str(item) for item in
                                   (*row.get("participants", []), *row.get("related_users", [])) if str(item)}
                        if scoped_emotion(str(row.get("source") or ""), row.get("context")):
                            if task in {"plan", "sleep", "diary"} or related and not present_people.intersection(related):
                                continue
                        if row.get("source") == "life_matter" and related:
                            if task in {"plan", "sleep", "diary"} or not present_people.intersection(related):
                                continue
                        filtered.append(row)
                    candidates = filtered
                if not candidates and not context_texts:
                    return
                model_key = revision(self.model_identity())
                ids = [int(row["id"]) for row in candidates]
                known: dict[int, str] = {}
                for start in range(0, len(ids), 128):
                    batch = ids[start:start + 128]
                    marks = ",".join("?" for _ in batch)
                    known.update({int(row["memory_id"]): row["revision"] for row in self.memory.db._query(
                        f"SELECT memory_id,revision FROM memory_vector WHERE model_key=? AND memory_id IN ({marks})",
                        (model_key, *batch))})
                versions = {int(row["id"]): revision(str(row.get("content") or "")) for row in candidates}
                missing = [row for row in candidates
                           if known.get(int(row["id"])) != versions[int(row["id"])]]
                missing = missing[:32]

                context_scope = (session, persona, life_persona_key, self.model_identity())
                material_keys = {identity: (*context_scope, revision(value))
                                 for identity, value in context_texts.items()}
                context_identities = set(material_keys)
                vectors_by_key: dict[tuple, list[float]] = {}
                pending_queries: list[tuple[str, tuple]] = []
                memory_score_keys: set[tuple] = set()
                context_score_keys: set[tuple] = set()
                for query, key in keys:
                    vector = self.query_vectors.get(key)
                    vector_fresh = (vector is not None
                                    and time.time() - self.query_vector_times.get(key, 0) <= 300
                                    and valid_vector(vector))
                    cached_scores = self.cached(session, persona, query, mode=mode, automatic=automatic,
                                                life_persona_key=life_persona_key, node_id=node_id)
                    score_fresh = time.time() - self.score_times.get(key, 0) <= 300
                    memory_scores_current = (score_fresh and self.score_versions.get(key) == versions
                                             and (bool(cached_scores) or not candidates))
                    context_scores_current = (score_fresh and context_identities.issubset(
                        self.context_scores.get(key, {})))
                    update_memory_scores = bool(missing) or not memory_scores_current
                    update_context_scores = bool(context_identities) and not context_scores_current
                    if update_memory_scores:
                        memory_score_keys.add(key)
                    if update_context_scores:
                        context_score_keys.add(key)
                    if vector_fresh:
                        vectors_by_key[key] = vector
                    elif update_memory_scores or update_context_scores:
                        pending_queries.append((query, key))

                if not memory_score_keys and not context_score_keys:
                    return
                context_vectors = {key: self.context_vectors[key] for key in set(material_keys.values())
                                   if key in self.context_vectors
                                   and time.time() - self.context_vector_times.get(key, 0) <= 300
                                   and valid_vector(self.context_vectors[key])}
                missing_context = dict(list({material_keys[identity]: value for identity, value in context_texts.items()
                                            if material_keys[identity] not in context_vectors}.items())[:32])
                # Durable memories and transient source records share one
                # bounded material budget. Give both a chance during rebuilds.
                context_limit = min(len(missing_context), 32 - min(len(missing), 16))
                missing = missing[:32 - context_limit]
                missing_context = dict(list(missing_context.items())[:context_limit])
                bounded = [self._query_text(query) for query, _ in pending_queries]
                # A single turn can rebuild at most 32 memory vectors even when
                # it carries several participant queries.
                material_texts = [str(row.get("content") or "")[:4000] for row in missing]
                embedded = []
                if bounded or material_texts or missing_context:
                    with model_context("embedding", "按当前生活范围召回多条语义记忆并补充可重建的索引", session):
                        embedded = await self.embed([*bounded, *material_texts, *missing_context.values()])
                for (query, key), vector in zip(pending_queries, embedded[:len(pending_queries)]):
                    vectors_by_key[key] = vector
                    self.query_vectors[key] = vector
                    self.query_vector_times[key] = time.time()
                memory_vectors = embedded[len(pending_queries):len(pending_queries) + len(missing)]
                for key, vector in zip(missing_context, embedded[len(pending_queries) + len(missing):]):
                    context_vectors[key] = vector
                    self.context_vectors[key] = vector
                    self.context_vector_times[key] = time.time()
                with self.memory.db.atomic():
                    for row, vector in zip(missing, memory_vectors):
                        digest = versions[int(row["id"])]
                        self.memory.db._execute(
                            "INSERT INTO memory_vector(memory_id,model_key,revision,vector) VALUES(?,?,?,?) "
                            "ON CONFLICT(memory_id,model_key) DO UPDATE SET revision=excluded.revision,vector=excluded.vector",
                            (row["id"], model_key, digest, json.dumps(vector)))

                indexed_scores: dict[tuple, dict[int, float]] = {key: {} for key in memory_score_keys}
                # Read no more than sixteen vectors at a time and retain only
                # scalar scores, keeping the in-memory query cache small.
                if memory_score_keys:
                    for start in range(0, len(ids), 16):
                        batch = ids[start:start + 16]
                        marks = ",".join("?" for _ in batch)
                        rows = self.memory.db._query(
                            f"SELECT memory_id,revision,vector FROM memory_vector WHERE model_key=? AND memory_id IN ({marks})",
                            (model_key, *batch))
                        for row in rows:
                            memory_id = int(row["memory_id"])
                            if row["revision"] != versions.get(memory_id):
                                continue
                            try:
                                vector = json.loads(row["vector"])
                            except (ValueError, TypeError):
                                vector = None
                            if not valid_vector(vector) or any(
                                    len(vector) != len(vectors_by_key[key]) for key in memory_score_keys
                                    if key in vectors_by_key):
                                self.memory.db._execute(
                                    "DELETE FROM memory_vector WHERE memory_id=? AND model_key=? AND revision=?",
                                    (memory_id, model_key, row["revision"]))
                                continue
                            for key in memory_score_keys:
                                query_vector = vectors_by_key.get(key)
                                if query_vector is not None and len(vector) == len(query_vector):
                                    indexed_scores[key][memory_id] = cosine(query_vector, vector)

                for query, key in keys:
                    if key not in vectors_by_key or key not in memory_score_keys | context_score_keys:
                        continue
                    if key in memory_score_keys:
                        self.scores.pop(key, None)
                        self.scores[key] = indexed_scores.get(key, {})
                        self.score_versions[key] = versions
                    self.score_times[key] = time.time()
                    if key in context_score_keys:
                        self.context_scores[key] = {
                            identity: cosine(vectors_by_key[key], context_vectors[material_key])
                            for identity, material_key in material_keys.items() if material_key in context_vectors}
                keep = list(self.scores)[-16:]
                self.scores = {key: self.scores[key] for key in keep}
                self.score_versions = {key: self.score_versions[key] for key in keep if key in self.score_versions}
                self.score_times = {key: self.score_times[key] for key in keep if key in self.score_times}
                self.query_vectors = {key: self.query_vectors[key] for key in keep if key in self.query_vectors}
                self.query_vector_times = {key: self.query_vector_times[key] for key in keep
                                           if key in self.query_vector_times}
                self.context_scores = {key: self.context_scores[key] for key in keep if key in self.context_scores}
                context_keep = sorted(self.context_vector_times, key=self.context_vector_times.get, reverse=True)[:64]
                self.context_vectors = {key: self.context_vectors[key] for key in context_keep}
                self.context_vector_times = {key: self.context_vector_times[key] for key in context_keep}
                ready = sum(bool(self.scores.get(key) or self.context_scores.get(key)) for _, key in keys)
                self.status = f"已就绪 · 本次可检索 {ready} 组查询"
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status = "嵌入暂不可用，已使用关键词召回"
                self.retry_at = time.time() + 60
            finally:
                if self.locks.get(scope) is lock:
                    self.locks.pop(scope, None)

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

"""Native embedding selection, scope filtering and unknown usage remain truthful."""
import types
import unittest
from unittest.mock import AsyncMock

from core.model_calls import model_context
from tests import test_scope_api as fixture


class EmbeddingProviderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ScopeApiTests.asyncSetUp

    def install_provider(self):
        self.provider = types.SimpleNamespace(
            provider_config={"id": "embed-a", "embedding_model": "bge", "api_key": "never-copy-this"},
            get_model=lambda: self.provider.provider_config["embedding_model"],
            meta=lambda: types.SimpleNamespace(id="embed-a", model="bge"),
            get_embeddings=AsyncMock(side_effect=lambda texts: [[1., 0.] for _ in texts]))
        self.plugin.context.get_provider_by_id = lambda key: self.provider if key == "embed-a" else None
        self.plugin.context.get_all_embedding_providers = lambda: [self.provider]
        self.plugin.config["embedding_provider_id"] = "embed-a"
        return self.plugin.engine.memory.semantic

    async def test_default_provider_is_reused_and_tokens_are_unknown_when_host_reports_none(self):
        semantic = self.install_provider()
        with model_context("embedding", "查找自己的生活", self.session):
            self.assertEqual(await semantic.embed(["读书"]), [[1., 0.]])
        self.provider.get_embeddings.assert_awaited_once_with(["读书"])
        row = self.plugin.db.raw.model_calls()["items"][0]
        self.assertEqual((row["provider_id"], row["model"], row["purpose"]), ("embed-a", "bge", "embedding"))
        self.assertFalse(row["usage"]["available"])
        self.assertNotIn("never-copy-this", str(row))

    async def test_scope_is_filtered_before_native_embedding_and_model_change_invalidates_index(self):
        semantic = self.install_provider()
        world = self.plugin.engine.world
        world.memory.semantic_enabled = True
        memory = self.plugin.engine.memory
        memory.remember(session_id=self.session, persona_id="", node_id="study", content="看过风车图册")
        memory.remember(session_id="test:PrivateMessage:secret", persona_id="", node_id="study", content="不能外传的私下想法")
        before = semantic.model_identity()
        await semantic.prepare(self.session, "", "风车")
        self.assertTrue(self.provider.get_embeddings.await_count)
        self.assertNotIn("不能外传", str(self.provider.get_embeddings.await_args))
        self.provider.provider_config["embedding_model"] = "different-dimension"
        self.assertNotEqual(before, semantic.model_identity())

    async def test_native_config_dropdown_contains_only_embedding_ids_and_no_credentials(self):
        self.install_provider()
        config = self.plugin.config
        config.schema = {"embedding_provider_id": {"type": "string", "default": ""}}
        self.plugin.embedding.update_schema()
        self.assertEqual(config.schema["embedding_provider_id"]["options"], ["", "embed-a"])
        self.assertNotIn("never-copy-this", str(self.plugin.embedding.choices()))
        self.assertNotIn("never-copy-this", str(config.schema))

    async def test_missing_or_wrong_provider_falls_back_without_calling_a_chat_model(self):
        semantic = self.install_provider()
        self.plugin.engine.world.memory.semantic_enabled = True
        self.plugin.engine.world.memory.semantic_provider_id = "removed-model"
        await semantic.prepare(self.session, "", "自己的生活")
        self.assertIn("关键词", semantic.status)
        self.provider.get_embeddings.assert_not_awaited()

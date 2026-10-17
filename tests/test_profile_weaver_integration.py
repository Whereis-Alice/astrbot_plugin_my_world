"""Native Profile Weaver integration: migration, scoped keys and LLM tools."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(HERE))

import fake_astrbot  # noqa: E402

fake_astrbot.install()

from core.profile_weaver_service import ProfileWeaverService  # noqa: E402


def legacy_record(
    *,
    user_id: str = "42",
    name: str = "阿岚",
    hobby: str = "摄影",
    updated_at: str = "2026-10-12 20:00:00",
) -> dict:
    return {
        "subject_user_id": user_id,
        "subject_name": name,
        "created_at": "2026-10-01 10:00:00",
        "updated_at": updated_at,
        "fields": {"昵称": name, "爱好": hobby, "备注": ["在准备考试"]},
        "custom_fields": {},
        "field_meta": {
            "昵称": {
                "updated_at": updated_at,
                "updated_by": "user_command",
                "actor_name": name,
                "source_kind": "manual_edit",
                "evidence": f"叫我{name}",
            }
        },
        "locked_fields": ["昵称"],
    }


class _Logger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class _Event:
    unified_msg_origin = "aiocqhttp:GroupMessage:1001"
    message_str = "我叫阿岚，最喜欢摄影"

    def get_sender_id(self):
        return "42"

    def get_sender_name(self):
        return "阿岚"

    def is_private_chat(self):
        return False


class ProfileWeaverIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.legacy_dir = self.root / "astrbot_plugin_profile_weaver"
        self.legacy_dir.mkdir(parents=True)
        self.profiles_path = self.legacy_dir / "profiles.json"
        self.profiles_path.write_text(
            json.dumps(
                {"aiocqhttp:GroupMessage:1001_42": legacy_record()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.legacy_dir / "audit.log").write_text('{"action":"upsert_field"}\n', "utf-8")
        (self.legacy_dir / "backups").mkdir()
        (self.legacy_dir / "backups" / "profiles-manual-20261012.json").write_text(
            "{}",
            encoding="utf-8",
        )
        self.plugin = types.SimpleNamespace(
            config={},
            store=types.SimpleNamespace(data_dir=self.root / "astrbot_plugin_my_world"),
            logger=_Logger(),
            context=types.SimpleNamespace(),
        )

    def service(self, config: dict | None = None) -> ProfileWeaverService:
        self.plugin.config = dict(config or {})
        return ProfileWeaverService(self.plugin, str(self.root))

    def test_inherits_files_and_makes_legacy_keys_clickable(self):
        service = self.service({"weaver_session_based": False})
        self.assertTrue(service.store.profiles_path.exists())
        self.assertTrue((service.data_dir / "audit.log").exists())
        self.assertTrue((service.data_dir / "backups").exists())
        # 默认跨会话共享时，旧插件按会话存的 key 会收敛到用户 key。
        detail = service.detail_for("aiocqhttp:GroupMessage:1001", "42")
        self.assertEqual(detail["display_name"], "阿岚")
        self.assertEqual(detail["fields"][0]["value"], "阿岚")
        self.assertEqual(detail["locked_fields"], ["昵称"])

    def test_later_legacy_writes_are_merged_on_restart(self):
        service = self.service({"weaver_session_based": False})
        service.store.upsert_field(
            user_id="42",
            subject_name="阿岚",
            field_name="爱好",
            value="摄影、爬山",
            session_id=None,
            actor=service.actor(_Event(), "user_command"),
            source_kind="manual_edit",
            evidence="我喜欢爬山",
            allow_custom_field=True,
        )
        self.profiles_path.write_text(
            json.dumps(
                {
                    "aiocqhttp:GroupMessage:1001_42": legacy_record(
                        hobby="摄影、爬山、露营",
                        updated_at="2099-10-13 09:00:00",
                    )
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        restarted = self.service({"weaver_session_based": False})
        detail = restarted.detail_for("", "42")
        values = {item["name"]: item["value"] for item in detail["fields"]}
        self.assertEqual(values["爱好"], "摄影、爬山、露营")

    def test_upgrade_repairs_legacy_cross_session_collapse(self):
        collapsed = self.service({"weaver_session_based": False})
        self.assertIn("42", collapsed.store.profiles)

        isolated = self.service({"weaver_session_based": True})
        self.assertNotIn("42", isolated.store.profiles)
        self.assertTrue(Path(isolated.scope_recovery_archive).exists())
        detail = isolated.detail_for("aiocqhttp:GroupMessage:1001", "42")
        self.assertEqual(detail["record_count"], 1)
        self.assertEqual(detail["display_name"], "阿岚")

    def test_sync_llm_handlers_work_inside_async_tools(self):
        service = self.service({})
        path = Path(__file__).resolve().parents[1] / "adapters" / "profile_weaver_tools.py"
        spec = importlib.util.spec_from_file_location("profile_weaver_tools_test", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        self.assertEqual(
            {
                module.VIEW_TOOL_NAME,
                module.REMEMBER_TOOL_NAME,
                module.FORGET_TOOL_NAME,
            },
            {
                "profileweaver_view_profile",
                "profileweaver_remember_field",
                "profileweaver_forget_field",
            },
        )
        wrapper = module.ProfileWeaverRememberTool(plugin=service)
        context = types.SimpleNamespace(
            context=types.SimpleNamespace(event=_Event())
        )
        result = asyncio.run(
            wrapper.call(
                context,
                field_name="爱好",
                value="摄影",
                evidence="最喜欢摄影",
                source_kind="self_preference",
                create_custom_field=False,
            )
        )
        self.assertIn("爱好", result)

    def test_strict_evidence_mode_rejects_a_reworded_quote(self):
        service = self.service({"weaver_evidence_match_mode": "strict"})
        result = service.handle_llm_remember(
            _Event(),
            field_name="爱好",
            value="摄影",
            evidence="摄影是我的爱好",
            source_kind="self_preference",
        )
        self.assertIn("evidence", result)

    def test_contact_view_merges_scoped_and_global_profile_records(self):
        service = self.service({"weaver_session_based": True})
        service.store.upsert_field(
            user_id="42",
            subject_name="阿岚",
            field_name="职业",
            value="学生",
            session_id=None,
            actor=service.actor(_Event(), "user_command"),
            source_kind="manual_edit",
            evidence="我是学生",
            allow_custom_field=False,
        )
        service.store.upsert_field(
            user_id="42",
            subject_name="阿岚",
            field_name="所在地",
            value="另一个会话",
            session_id="aiocqhttp:GroupMessage:2002",
            actor=service.actor(_Event(), "user_command"),
            source_kind="manual_edit",
            evidence="我住在另一个会话",
            allow_custom_field=False,
        )
        detail = service.detail_for("aiocqhttp:GroupMessage:1001", "42")
        values = {item["name"]: item["value"] for item in detail["fields"]}
        self.assertEqual(values["爱好"], "摄影")
        self.assertEqual(values["职业"], "学生")
        self.assertNotIn("所在地", values)
        self.assertEqual(detail["record_count"], 2)
        self.assertIn("昵称", detail["locked_fields"])

        contact = service.people_for_session("aiocqhttp:GroupMessage:1001")["42"]
        self.assertEqual(contact["record_count"], 2)
        self.assertEqual(contact["locked_count"], 1)

        removed, failures = service.delete_contact_records(
            "42",
            service.actor(_Event(), "admin_command"),
            session_id="aiocqhttp:GroupMessage:1001",
        )
        self.assertEqual(removed, 1)
        self.assertEqual(failures, [])
        other_values = {
            item["name"]: item["value"]
            for item in service.detail_for("aiocqhttp:GroupMessage:2002", "42")[
                "fields"
            ]
        }
        self.assertEqual(other_values["所在地"], "另一个会话")
        self.assertEqual(other_values["职业"], "学生")


if __name__ == "__main__":
    unittest.main()

"""Top-level commands inherited from the original Profile Weaver plugin."""

from __future__ import annotations

import re
import shlex
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter

from ..core.profile_weaver_store import NOTES_FIELD_NAME


class ProfileCommandsMixin:
    """Thin command adapters over ``ProfileWeaverService`` and the unified contact view."""

    @staticmethod
    def _profile_command_rest(event: AstrMessageEvent, command: str) -> str:
        raw = str(event.get_message_str() or "").strip()
        body = re.sub(r"^[/!！。#]+", "", raw).strip()
        if not body.startswith(command):
            return ""
        return body[len(command) :].strip()

    @staticmethod
    def _profile_command_args(rest: str) -> list[str]:
        try:
            return shlex.split(str(rest or "").strip())
        except ValueError:
            return str(rest or "").split()

    def _profile_service_or_error(self):
        service = getattr(self, "profile_weaver_service", None)
        if service is None or not service.enabled:
            return None, "通讯录画像内核当前未启用。"
        return service, ""

    def _profile_admin_denied(self, event: AstrMessageEvent) -> str:
        return "" if self._can_admin(event) else "只有管理员可以使用这条画像指令。"

    def _sync_profile_identity(
        self,
        event: AstrMessageEvent,
        *,
        user_id: str,
        field_name: str,
        value: str,
    ) -> None:
        store = getattr(self.engine, "profiles", None)
        if store is None:
            return
        canonical = self.profile_weaver_service.store.canonical_field_name(field_name)
        if canonical == "昵称":
            store.set_call_names(
                event.unified_msg_origin,
                user_id,
                call_me=value,
                explicit=True,
            )
        elif canonical == NOTES_FIELD_NAME:
            store.set_note(event.unified_msg_origin, user_id, value)

    def _format_profile_help(self, event: AstrMessageEvent) -> str:
        service, error = self._profile_service_or_error()
        if service is None:
            return error
        catalog = service.command_catalog()
        lines = ["心迹画像指令已并入通讯录："]
        legacy_user = (
            ("我的画像", "查看自己的完整画像"),
            ("画像字段", "列出基础字段与自定义字段"),
            ("画像完整度", "查看填写进度、字段锁和缺口"),
            ("设置画像 <字段> <值>", "写入或更新自己的字段"),
            ("删除画像 <字段|备注序号>", "删除自己的字段或备注"),
            ("锁定画像 <字段>", "锁定字段，阻止自动覆写"),
            ("解锁画像 <字段>", "解除字段锁"),
            ("画像历史 <字段> [条数]", "查看字段变更历史"),
            ("清空画像", "清空自己的详细画像"),
            ("画像帮助", "显示这页帮助"),
        )
        lines.extend(f"· {name} —— {desc}" for name, desc in legacy_user)
        if self._can_admin(event):
            lines.extend(
                [
                    "",
                    "管理员：",
                    "· 查询画像 <用户|@某人>",
                    "· 修改画像 <用户> <字段> <值>",
                    "· 删除画像字段 <用户> <字段>",
                    "· 清空用户画像 <用户>",
                    "· 锁定用户画像 / 解锁用户画像 <用户> <字段>",
                    "· 画像字段历史 <用户> <字段> [条数]",
                    "· 画像审计 <用户> [条数]",
                    "· 画像统计 / 画像备份 [标签] / 画像备份列表",
                    "· 画像恢复备份 <文件名> / 合并画像 <源key> <目标key>",
                    "· 画像面板",
                ]
            )
        lines.extend(
            [
                "",
                "等价的统一入口：/mw profile <子命令>",
                f"字段锁范围：{service.store.field_lock_scope_label()}",
            ]
        )
        return "\n".join(lines)

    async def inherited_show_my_profile(self, event: AstrMessageEvent):
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        denied = service.group_privacy_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        yield event.plain_result(
            service.profile_status_text(
                str(event.get_sender_id()),
                event.unified_msg_origin,
            )
        )

    async def inherited_profile_fields(self, event: AstrMessageEvent):
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        yield event.plain_result(
            service.field_catalog_text(
                str(event.get_sender_id()),
                event.unified_msg_origin,
            )
        )

    async def inherited_profile_completeness(self, event: AstrMessageEvent):
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        denied = service.group_privacy_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        user_id = str(event.get_sender_id())
        detail = service.detail_for(event.unified_msg_origin, user_id)
        if not detail:
            yield event.plain_result("还没有你的详细画像记录。")
            return
        fields = {str(item.get("name") or "") for item in detail.get("fields") or []}
        missing = [
            name
            for name in service.store.builtin_field_map
            if name != NOTES_FIELD_NAME and name not in fields
        ]
        lines = [
            f"画像完整度：{float(detail.get('completeness') or 0):.1f}%",
            (
                f"已填字段 {len(fields)} 个 · 自定义字段 "
                f"{sum(1 for item in detail.get('fields') or [] if item.get('is_custom'))} 个"
                f" · 备注 {len(detail.get('notes') or [])} 条"
            ),
        ]
        if detail.get("locked_fields"):
            lines.append("已锁定：" + "、".join(detail["locked_fields"]))
        if missing:
            lines.append("还没填：" + "、".join(missing[:15]))
        yield event.plain_result("\n".join(lines))

    async def inherited_set_profile(self, event: AstrMessageEvent):
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        args = self._profile_command_args(
            self._profile_command_rest(event, "设置画像")
        )
        if len(args) < 2:
            yield event.plain_result("用法：设置画像 <字段> <值>")
            return
        field_name, value = args[0], " ".join(args[1:])
        result = service.store.upsert_field(
            user_id=str(event.get_sender_id()),
            subject_name=str(event.get_sender_name()),
            field_name=field_name,
            value=value,
            session_id=event.unified_msg_origin if service.session_based else None,
            actor=service.actor(event, "user_command"),
            source_kind="manual_edit",
            evidence=str(event.message_str or ""),
            allow_custom_field=service.allow_user_custom_fields,
            field_description="用户通过原画像指令创建的自定义字段",
        )
        if result.ok:
            self._sync_profile_identity(
                event,
                user_id=str(event.get_sender_id()),
                field_name=field_name,
                value=value,
            )
        yield event.plain_result(("✅ " if result.ok else "❌ ") + result.message)

    async def inherited_delete_profile_field(self, event: AstrMessageEvent):
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        selector = self._profile_command_rest(event, "删除画像")
        if not selector:
            yield event.plain_result("用法：删除画像 <字段|备注序号>")
            return
        result = service.store.delete_field(
            user_id=str(event.get_sender_id()),
            session_id=event.unified_msg_origin if service.session_based else None,
            field_selector=selector,
            actor=service.actor(event, "user_command"),
            source_kind="manual_delete",
            evidence=str(event.message_str or ""),
        )
        yield event.plain_result(("✅ " if result.ok else "❌ ") + result.message)

    async def inherited_lock_profile_field(self, event: AstrMessageEvent):
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        yield event.plain_result(
            service.set_field_lock(
                user_id=str(event.get_sender_id()),
                session_id=event.unified_msg_origin,
                field_name=self._profile_command_rest(event, "锁定画像"),
                locked=True,
                actor=service.actor(event, "user_command"),
                subject_name=str(event.get_sender_name()),
            )
        )

    async def inherited_unlock_profile_field(self, event: AstrMessageEvent):
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        yield event.plain_result(
            service.set_field_lock(
                user_id=str(event.get_sender_id()),
                session_id=event.unified_msg_origin,
                field_name=self._profile_command_rest(event, "解锁画像"),
                locked=False,
                actor=service.actor(event, "user_command"),
                subject_name=str(event.get_sender_name()),
            )
        )

    async def inherited_profile_history(self, event: AstrMessageEvent):
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        denied = service.group_privacy_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        args = self._profile_command_args(
            self._profile_command_rest(event, "画像历史")
        )
        if not args:
            yield event.plain_result("用法：画像历史 <字段> [条数]")
            return
        limit = 10
        if len(args) > 1 and args[-1].isdigit():
            limit = max(1, min(int(args.pop()), 50))
        yield event.plain_result(
            service.field_history_text(
                user_id=str(event.get_sender_id()),
                session_id=event.unified_msg_origin,
                field_name=" ".join(args),
                limit=limit,
            )
        )

    async def inherited_clear_my_profile(self, event: AstrMessageEvent):
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        result = service.store.clear_profile(
            user_id=str(event.get_sender_id()),
            session_id=event.unified_msg_origin if service.session_based else None,
            actor=service.actor(event, "user_command"),
        )
        yield event.plain_result(("✅ " if result.ok else "❌ ") + result.message)

    async def inherited_profile_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._format_profile_help(event))

    async def inherited_admin_query_profile(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        target = service.resolve_target_user_id(
            event, self._profile_command_rest(event, "查询画像")
        )
        if not target:
            yield event.plain_result("用法：查询画像 <用户|@某人>")
            return
        yield event.plain_result(
            service.profile_status_text(
                target,
                event.unified_msg_origin,
                subject_label=f"用户 {target} ",
            )
        )

    async def _admin_set_contact_field(
        self, event: AstrMessageEvent, command: str
    ) -> str:
        denied = self._profile_admin_denied(event)
        if denied:
            return denied
        service, error = self._profile_service_or_error()
        if service is None:
            return error
        args = self._profile_command_args(self._profile_command_rest(event, command))
        if len(args) < 3:
            return f"用法：{command} <用户> <字段> <值>"
        target = service.resolve_target_user_id(event, args[0])
        result = service.store.upsert_field(
            user_id=target,
            subject_name=str(
                (service.detail_for(event.unified_msg_origin, target) or {}).get(
                    "display_name"
                )
                or target
            ),
            field_name=args[1],
            value=" ".join(args[2:]),
            session_id=event.unified_msg_origin if service.session_based else None,
            actor=service.actor(event, "admin_command"),
            source_kind="admin_edit",
            evidence=str(event.message_str or ""),
            allow_custom_field=True,
            field_description="管理员通过原画像指令创建的自定义字段",
        )
        if result.ok:
            self._sync_profile_identity(
                event,
                user_id=target,
                field_name=args[1],
                value=" ".join(args[2:]),
            )
        return ("✅ " if result.ok else "❌ ") + result.message

    async def inherited_admin_set_profile(self, event: AstrMessageEvent):
        yield event.plain_result(
            await self._admin_set_contact_field(event, "修改画像")
        )

    async def inherited_admin_delete_field(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        args = self._profile_command_args(
            self._profile_command_rest(event, "删除画像字段")
        )
        if len(args) < 2:
            yield event.plain_result("用法：删除画像字段 <用户> <字段>")
            return
        target = service.resolve_target_user_id(event, args[0])
        result = service.store.delete_field(
            user_id=target,
            session_id=event.unified_msg_origin if service.session_based else None,
            field_selector=" ".join(args[1:]),
            actor=service.actor(event, "admin_command"),
            source_kind="admin_delete",
            evidence=str(event.message_str or ""),
        )
        yield event.plain_result(("✅ " if result.ok else "❌ ") + result.message)

    async def inherited_admin_clear_profile(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        target = service.resolve_target_user_id(
            event, self._profile_command_rest(event, "清空用户画像")
        )
        result = service.store.clear_profile(
            user_id=target,
            session_id=event.unified_msg_origin if service.session_based else None,
            actor=service.actor(event, "admin_command"),
        )
        yield event.plain_result(("✅ " if result.ok else "❌ ") + result.message)

    async def _admin_lock_contact_field(
        self, event: AstrMessageEvent, command: str, locked: bool
    ) -> str:
        denied = self._profile_admin_denied(event)
        if denied:
            return denied
        service, error = self._profile_service_or_error()
        if service is None:
            return error
        args = self._profile_command_args(self._profile_command_rest(event, command))
        if len(args) < 2:
            return f"用法：{command} <用户> <字段>"
        target = service.resolve_target_user_id(event, args[0])
        return service.set_field_lock(
            user_id=target,
            session_id=event.unified_msg_origin,
            field_name=" ".join(args[1:]),
            locked=locked,
            actor=service.actor(event, "admin_command"),
        )

    async def inherited_admin_lock_field(self, event: AstrMessageEvent):
        yield event.plain_result(
            await self._admin_lock_contact_field(event, "锁定用户画像", True)
        )

    async def inherited_admin_unlock_field(self, event: AstrMessageEvent):
        yield event.plain_result(
            await self._admin_lock_contact_field(event, "解锁用户画像", False)
        )

    async def inherited_admin_field_history(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        args = self._profile_command_args(
            self._profile_command_rest(event, "画像字段历史")
        )
        if len(args) < 2:
            yield event.plain_result("用法：画像字段历史 <用户> <字段> [条数]")
            return
        target = service.resolve_target_user_id(event, args[0])
        limit = 10
        if len(args) > 2 and args[-1].isdigit():
            limit = max(1, min(int(args.pop()), 50))
        yield event.plain_result(
            service.field_history_text(
                user_id=target,
                session_id=event.unified_msg_origin,
                field_name=" ".join(args[1:]),
                limit=limit,
                subject_label=f"用户 {target} 的",
            )
        )

    async def inherited_admin_audit(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        args = self._profile_command_args(self._profile_command_rest(event, "画像审计"))
        limit = 10
        if args and args[-1].isdigit():
            limit = max(1, min(int(args.pop()), 50))
        target = (
            service.resolve_target_user_id(event, " ".join(args)) if args else ""
        )
        page = service.audit(
            user_id=target,
            session_id=event.unified_msg_origin if service.session_based else None,
            limit=limit,
            offset=0,
            include_rotated=True,
        )
        entries = page.get("items") or []
        if not entries:
            yield event.plain_result("没有找到画像审计记录。")
            return
        lines = [f"最近 {len(entries)} 条画像审计："]
        for entry in entries:
            lines.append(
                f"- [{entry.get('at')}] {entry.get('action') or '变更'} · "
                f"{entry.get('field_name') or '整条画像'} · "
                f"{entry.get('subject_name') or entry.get('subject_user_id') or '-'}"
            )
            lines.append(
                f"  {entry.get('old_value') or '（空）'} → "
                f"{entry.get('new_value') or '（空）'}"
            )
        yield event.plain_result("\n".join(lines))

    async def inherited_admin_stats(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        yield event.plain_result(service.stats_text())

    async def inherited_admin_backup(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        rest = self._profile_command_rest(event, "画像备份")
        if rest.startswith("列表"):
            return
        tag = rest or "manual"
        path = service.store.create_backup(tag[:40])
        yield event.plain_result(
            f"✅ 已创建备份：{path.name}"
            if path is not None
            else "❌ 备份失败，请检查数据目录写入权限。"
        )

    async def inherited_admin_backups(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        backups = service.backups()
        if not backups:
            yield event.plain_result("暂无画像备份。")
            return
        lines = [f"共 {len(backups)} 份画像备份："]
        for item in backups[:30]:
            size = float(item.get("size") or 0) / 1024
            lines.append(
                f"- {item.get('name')}（{size:.1f} KB，{item.get('modified_at') or '未知'}）"
            )
        yield event.plain_result("\n".join(lines))

    async def inherited_admin_restore(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        name = self._profile_command_rest(event, "画像恢复备份")
        if not name:
            yield event.plain_result("用法：画像恢复备份 <文件名>")
            return
        result = service.store.restore_backup(
            name, service.actor(event, "admin_command")
        )
        yield event.plain_result(("✅ " if result.ok else "❌ ") + result.message)

    async def inherited_admin_merge(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        args = self._profile_command_args(self._profile_command_rest(event, "合并画像"))
        if len(args) < 2:
            yield event.plain_result("用法：合并画像 <源key> <目标key>")
            return
        result = service.store.merge_profile_records(
            source_key=args[0],
            target_key=args[1],
            actor=service.actor(event, "admin_command"),
        )
        yield event.plain_result(("✅ " if result.ok else "❌ ") + result.message)

    async def inherited_admin_panel(self, event: AstrMessageEvent):
        denied = self._profile_admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        service, error = self._profile_service_or_error()
        if service is None:
            yield event.plain_result(error)
            return
        stats = service.stats()
        yield event.plain_result(
            "通讯录画像已并入「小世界 → 通讯录」。\n"
            f"当前 {int(stats.get('profile_count') or 0)} 条详细画像记录，"
            f"{int(stats.get('user_count') or 0)} 个联系人。\n"
            f"写操作：{'开启' if service.webui_allow_edit else '只读'}。"
        )


LEGACY_PROFILE_COMMANDS: dict[str, str] = {
    "我的画像": "inherited_show_my_profile",
    "画像字段": "inherited_profile_fields",
    "画像完整度": "inherited_profile_completeness",
    "设置画像": "inherited_set_profile",
    "删除画像": "inherited_delete_profile_field",
    "锁定画像": "inherited_lock_profile_field",
    "解锁画像": "inherited_unlock_profile_field",
    "画像历史": "inherited_profile_history",
    "清空画像": "inherited_clear_my_profile",
    "画像帮助": "inherited_profile_help",
    "查询画像": "inherited_admin_query_profile",
    "修改画像": "inherited_admin_set_profile",
    "删除画像字段": "inherited_admin_delete_field",
    "清空用户画像": "inherited_admin_clear_profile",
    "锁定用户画像": "inherited_admin_lock_field",
    "解锁用户画像": "inherited_admin_unlock_field",
    "画像字段历史": "inherited_admin_field_history",
    "画像审计": "inherited_admin_audit",
    "画像统计": "inherited_admin_stats",
    "画像备份": "inherited_admin_backup",
    "画像备份列表": "inherited_admin_backups",
    "画像恢复备份": "inherited_admin_restore",
    "合并画像": "inherited_admin_merge",
    "画像面板": "inherited_admin_panel",
}


def install_profile_commands(plugin_cls: type[Any]) -> None:
    """Register wrappers in the plugin's main module so AstrBot can activate them."""

    def make_handler(method_name: str):
        original = getattr(ProfileCommandsMixin, method_name)

        async def handler(self, event: AstrMessageEvent):
            async for item in original(self, event):
                yield item

        handler.__name__ = f"cmd_{method_name}"
        handler.__qualname__ = f"{plugin_cls.__name__}.cmd_{method_name}"
        handler.__doc__ = original.__doc__
        handler.__module__ = plugin_cls.__module__
        return handler

    for command_name, method_name in LEGACY_PROFILE_COMMANDS.items():
        handler = make_handler(method_name)
        setattr(plugin_cls, f"cmd_{method_name}", filter.command(command_name)(handler))

"""Resource limits must survive cancellation and never manage a QQ environment."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location("life_sandbox_resources_under_test",
    Path(__file__).resolve().parents[1] / "adapters" / "life_sandbox.py")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
LifeSandboxPool, PREFIX = _module.LifeSandboxPool, _module.PREFIX


class Booter:
    def __init__(self):
        self.closed = asyncio.Event()
        self.deleted = False
        self.fail = False

    async def shutdown(self, *, delete_sandbox=False):
        if self.fail:
            raise RuntimeError("synthetic cleanup failure")
        self.deleted = delete_sandbox
        self.closed.set()


class Client:
    def __init__(self):
        self.qq = Booter()
        self.session_booter = {"qq:FriendMessage:123": self.qq}
        self.created = []
        self.started = asyncio.Event()
        self.release = None

    async def get_booter(self, context, key):
        self.started.set()
        if self.release:
            await self.release.wait()
        if key not in self.session_booter:
            self.session_booter[key] = Booter()
            self.created.append(self.session_booter[key])
        return self.session_booter[key]


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Client()
        self.pool = LifeSandboxPool(client=self.client)
        self.addAsyncCleanup(self.pool.close)

    async def test_active_slot_blocks_another_life_without_touching_qq(self):
        async with self.pool.lease(PREFIX+"a", None, idle_seconds=0) as first:
            with self.assertRaisesRegex(RuntimeError, "名额"):
                async with self.pool.lease(PREFIX+"b", None):
                    self.fail("overbooked")
        self.assertEqual(len(self.client.created), 1)
        async with self.pool.lease(PREFIX+"b", None, idle_seconds=0):
            self.assertTrue(first.deleted)
        self.assertFalse(self.client.qq.closed.is_set())
        self.assertIs(self.client.session_booter["qq:FriendMessage:123"], self.client.qq)

    async def test_same_project_reuses_environment_and_idle_release_reclaims_it(self):
        async with self.pool.lease(PREFIX+"a", None, idle_seconds=.02) as first:
            await asyncio.sleep(.03)
            self.assertFalse(first.closed.is_set())
        async with self.pool.lease(PREFIX+"a", None, idle_seconds=.02) as second:
            self.assertIs(first, second)
        await asyncio.wait_for(first.closed.wait(), 1)
        self.assertNotIn(PREFIX+"a", self.client.session_booter)
        self.assertEqual(self.pool._entries, {})
        self.assertEqual(len(self.client.created), 1)

    async def test_cancelled_startup_keeps_slot_until_native_boot_can_be_disposed(self):
        self.client.release = asyncio.Event()

        async def run():
            async with self.pool.lease(PREFIX+"a", None):
                self.fail("cancelled startup entered execution")

        task = asyncio.create_task(run())
        await self.client.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaisesRegex(RuntimeError, "名额"):
            async with self.pool.lease(PREFIX+"b", None):
                self.fail("opening slot was lost")
        self.client.release.set()
        await asyncio.gather(*self.pool._cleanup_tasks)
        self.assertTrue(self.client.created[0].deleted)
        self.assertEqual(self.pool._entries, {})

    async def test_failed_cleanup_does_not_free_a_slot_and_create_more_vms(self):
        async with self.pool.lease(PREFIX+"a", None, idle_seconds=0) as first:
            first.fail = True
        with self.assertRaisesRegex(RuntimeError, "名额"):
            async with self.pool.lease(PREFIX+"b", None):
                self.fail("leaked environment no longer counted")
        self.assertEqual(len(self.client.created), 1)
        first.fail = False

    async def test_close_waits_for_active_lease_to_finish_before_disposal(self):
        async with self.pool.lease(PREFIX+"a", None, idle_seconds=0) as first:
            await self.pool.close()
            self.assertFalse(first.closed.is_set())
        self.assertTrue(first.closed.is_set())
        with self.assertRaisesRegex(RuntimeError, "停止"):
            async with self.pool.lease(PREFIX+"b", None):
                self.fail("closed pool reopened")

    async def test_previous_plugin_environments_are_adopted_but_qq_is_not(self):
        previous = self.client.session_booter[PREFIX+"old"] = Booter()
        async with self.pool.lease(PREFIX+"new", None, idle_seconds=0):
            self.assertTrue(previous.closed.is_set())
        await self.pool.close()
        self.assertFalse(self.client.qq.closed.is_set())

    async def test_qq_session_cannot_be_passed_to_pool(self):
        with self.assertRaises(ValueError):
            async with self.pool.lease("qq:FriendMessage:123", None):
                self.fail("accepted QQ namespace")

    async def test_legacy_shutdown_without_delete_keyword_is_supported(self):
        closed = []

        async def shutdown():
            closed.append(True)

        self.client.session_booter[PREFIX+"old"] = SimpleNamespace(shutdown=shutdown)
        async with self.pool.lease(PREFIX+"new", None, idle_seconds=0):
            self.assertEqual(closed, [True])

    async def test_native_renewal_is_reclaimed_instead_of_old_client(self):
        replacement = Booter()
        async with self.pool.lease(PREFIX+"a", None, idle_seconds=0) as first:
            await first.shutdown(delete_sandbox=True)
            self.client.session_booter[PREFIX+"a"] = replacement
        await self.pool.close()
        self.assertTrue(replacement.deleted)
        self.assertNotIn(PREFIX+"a", self.client.session_booter)

    async def test_lowered_limit_does_not_interrupt_existing_active_environment(self):
        async with self.pool.lease(PREFIX+"a", None, maximum=2, idle_seconds=0) as first:
            async with self.pool.lease(PREFIX+"b", None, maximum=2, idle_seconds=0) as second:
                with self.assertRaisesRegex(RuntimeError, "名额"):
                    async with self.pool.lease(PREFIX+"c", None, maximum=1):
                        self.fail("new task exceeded lowered limit")
                self.assertFalse(first.closed.is_set() or second.closed.is_set())

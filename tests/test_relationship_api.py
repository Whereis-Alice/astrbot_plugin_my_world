"""Exercise public contact routes with actual stores and lifecycle state."""

import unittest

from tests import test_scope_api as scope


class RelationshipApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp
    call = scope.ScopeApiTests.call

    async def test_relation_mutations_cannot_target_another_contact_or_life(self):
        store = self.plugin.engine.profiles
        store.touch(self.session, '42')
        bond = store.note_bond(self.session, '42', type='男友')
        other = 'test:GroupMessage:200'
        self.plugin.store.add_session(other)
        self.plugin.engine.reload_config()
        for session_id, uid in [(self.session, '7'), (other, '42')]:
            for action in ['close', 'accept', 'delete']:
                result = await self.call('profile/bond', method='POST', session=session_id,
                                         user_id=uid, action=action, id=bond['id'])
                self.assertEqual(result['status_code'], 400)
        self.assertEqual(store.db.get_user_bond(bond_id=bond['id'])['status'], 'current')

    async def test_experience_mutations_check_both_owner_and_life(self):
        store = self.plugin.engine.profiles
        store.touch(self.session, '42')
        fact = store.note_fact(self.session, '42', text='周末一起看星星', evidence='周末一起看星星')
        other = 'test:GroupMessage:200'
        self.plugin.store.add_session(other)
        self.plugin.engine.reload_config()
        for session_id, uid in [(self.session, '7'), (other, '42')]:
            for action in ['update', 'delete']:
                result = await self.call('profile/fact', method='POST', session=session_id,
                    user_id=uid, action=action, id=fact['id'], text='不属于这个人的改动')
                self.assertEqual(result['status_code'], 400)
        self.assertEqual(store.db.get_user_fact(fact_id=fact['id'])['text'], '周末一起看星星')

    async def test_manual_special_relation_works_while_natural_formation_is_off(self):
        store = self.plugin.engine.profiles
        store.touch(self.session, '42')
        self.assertFalse(store.config.bond_by_name('男友').auto_form)
        result = await self.call('profile/bond', method='POST', session=self.session,
                                 user_id='42', type='男友')
        self.assertEqual(result['status_code'], 200)
        self.assertEqual(result['data']['person']['affinities'], ['男友'])
        self.assertEqual(store.bonds(self.session, '42', statuses=['current'])[0]['asserted_by'], '管理员设置')

    async def test_detail_and_prompt_share_temporary_distance_and_personal_intentions(self):
        engine = self.plugin.engine
        engine.profiles.touch(self.session, '42')
        engine.profiles.note_bond(self.session, '42', type='男友')
        engine.profiles.adjust_affinity(self.session, '42', 100)
        engine.world.state_dynamics.own_topic_max = 5
        async with engine.session_state(self.session) as state:
            engine.note_grudge(state, '说了伤人的话', user_id='42', force=True)
            engine.note_own_topic(state, '一起看星星', user_id='42')
            engine.note_own_topic(state, '自己修理旧风车')
            engine.note_own_topic(state, '一起种花', user_id='7')
        result = await self.call('profile/contact', session=self.session, user_id='42')
        self.assertEqual(result['status_code'], 200)
        data = result['data']
        self.assertEqual(data['relationship']['base_level'], '特别的人')
        self.assertEqual(data['relationship']['effective_level'], '亲近')
        self.assertIn('你对他：亲近', data['model_context'])
        self.assertEqual([item['who'] for item in data['own_topics']], ['42'])

    async def test_invalid_record_id_returns_readable_error(self):
        for endpoint in ['profile/bond', 'profile/fact']:
            result = await self.call(endpoint, method='POST', session=self.session,
                                     user_id='42', action='delete', id='invalid')
            self.assertEqual(result['status_code'], 400)

"""Exercise public contact routes with actual stores and lifecycle state."""

import unittest

from core.defaults import default_profile
from tests import test_scope_api as scope


class RelationshipApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp
    call = scope.ScopeApiTests.call

    async def test_five_stage_preset_is_shared_by_read_only_editor_endpoints(self):
        before = self.plugin.store.raw_world()
        rules = (await self.call('relationships/rules'))['data']
        defaults = (await self.call('defaults'))['data']
        self.assertEqual(rules['friendship_preset'], defaults['friendship_preset'])
        preset = rules['friendship_preset']
        self.assertEqual([preset['default_bond'], *[stage['bond'] for stage in preset['stages']]],
                         ['陌生人', '群友', '朋友', '好友', '挚友'])
        self.assertEqual(preset['special_bonds'], [
            {'name': '闺蜜', 'slot': 'bestie'}, {'name': '主人', 'slot': 'guardian'},
            {'name': '男友', 'slot': 'romance'}, {'name': '女友', 'slot': 'romance'},
            {'name': '家人', 'slot': 'family'}, {'name': '讨厌的人', 'slot': 'rival'},
            {'name': '敌人', 'slot': 'enemy'},
        ])
        self.assertEqual(self.plugin.store.raw_world(), before)

    async def test_global_five_stage_upgrade_preserves_independent_rules_and_people(self):
        raw = self.plugin.store.raw_world()
        raw['profile']['bonds'] = [b for b in raw['profile']['bonds'] if b['name'] not in {'好友', '挚友'}]
        raw['profile'].pop('friendship_stages', None)
        self.plugin.store.save_world(raw, snapshot=False)
        self.plugin.engine.reload_config()
        self.plugin.engine.profiles.touch(self.session, '42')
        self.plugin.engine.profiles.note_bond(self.session, '42', type='主人', asserted_by='管理员设置')
        person = self.plugin.engine.profiles.profile(self.session, '42')
        bonds = self.plugin.engine.profiles.bonds(self.session, '42', statuses=['current'])
        raw['profile']['relationship_auto_enabled'] = False
        raw['profile']['friendship_stages'] = [{'bond': '朋友', 'min_talk_days': 30, 'min_affinity': 40}]
        self.plugin.store.save_scope(self.session, raw, self.plugin.store.raw_schedules(), ['settings'])
        self.plugin.engine.reload_config()
        independent = self.plugin.store.raw_sessions()
        initial = (await self.call('relationships/rules'))['data']
        result = await self.call('relationships/rules', method='POST', profile=default_profile(),
                                 fingerprint=initial['fingerprint'])
        self.assertEqual(result['status_code'], 200)
        self.plugin.engine.reload_config()
        self.assertEqual([s.bond for s in self.plugin.engine.world.profile.friendship_stages],
                         ['群友', '朋友', '好友', '挚友'])
        self.assertEqual(self.plugin.store.raw_sessions(), independent)
        self.assertEqual(self.plugin.engine.profiles.profile(self.session, '42'), person)
        self.assertEqual(self.plugin.engine.profiles.bonds(self.session, '42', statuses=['current']), bonds)
        with self.plugin.engine.config_scope(self.session):
            self.assertFalse(self.plugin.engine.world.profile.relationship_auto_enabled)
            self.assertEqual([s.model_dump(mode='json') for s in self.plugin.engine.world.profile.friendship_stages],
                             raw['profile']['friendship_stages'])

    async def test_familiarity_switch_round_trip_changes_context_without_erasing_description(self):
        store = self.plugin.engine.profiles
        store.touch(self.session, '42')
        store.note_bond(self.session, '42', type='朋友')
        before = (await self.call('profile/contact', session=self.session, user_id='42'))['data']['model_context']
        self.assertNotIn('当前熟悉程度', before)
        initial = (await self.call('relationships/rules'))['data']
        profile = initial['profile']
        self.assertFalse(profile['familiarity_descriptions_enabled'])
        profile['familiarity_descriptions_enabled'] = True
        level = next(item for item in profile['levels'] if item['name'] == '熟人')
        level['familiarity_description'] = '彼此已有一些了解。'
        result = await self.call('relationships/rules', method='POST', profile=profile, fingerprint=initial['fingerprint'])
        self.assertEqual(result['status_code'], 200)
        self.plugin.engine.reload_config()
        current = (await self.call('profile/contact', session=self.session, user_id='42'))['data']
        self.assertIn('当前熟悉程度：彼此已有一些了解。', current['model_context'])
        initial = (await self.call('relationships/rules'))['data']
        initial['profile']['familiarity_descriptions_enabled'] = False
        result = await self.call('relationships/rules', method='POST', profile=initial['profile'], fingerprint=initial['fingerprint'])
        self.assertEqual(result['status_code'], 200)
        self.plugin.engine.reload_config()
        after = (await self.call('profile/contact', session=self.session, user_id='42'))['data']['model_context']
        self.assertEqual(after, before)
        saved = (await self.call('relationships/rules'))['data']['profile']
        self.assertEqual(next(item for item in saved['levels'] if item['name'] == '熟人')['familiarity_description'], '彼此已有一些了解。')

    async def test_optional_notes_and_explicit_boundaries_survive_save_and_reload(self):
        initial = (await self.call('relationships/rules'))['data']
        self.assertTrue(initial['actions'])
        self.assertTrue(all('enabled' in action for action in initial['actions']))
        profile = initial['profile']
        level = next(item for item in profile['levels'] if item['name'] == '朋友')
        self.assertEqual((level['prompt'], level['deny']), ('', []))
        level.update(prompt='以前一起照料过花园', prompt_customized=True,
                     deny=['hug', 'kiss', 'leg_photo'], deny_customized=True)
        next(item for item in profile['bonds'] if item['name'] == '朋友')['prompt'] = '住在同一条街'
        saved = await self.call('relationships/rules', method='POST', profile=profile,
                                fingerprint=initial['fingerprint'])
        self.assertEqual(saved['status_code'], 200)
        self.plugin.engine.reload_config()
        current = (await self.call('relationships/rules'))['data']['profile']
        level = next(item for item in current['levels'] if item['name'] == '朋友')
        self.assertEqual(level['deny'], ['hug', 'kiss', 'leg_photo'])
        self.assertEqual(level['prompt'], '以前一起照料过花园')
        self.assertEqual(next(item for item in current['bonds'] if item['name'] == '朋友')['prompt'], '住在同一条街')
        self.assertNotIn('address', level)
        self.assertNotIn('proactive_per_day', level)

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
            await engine.note_own_topic(state, '一起看星星', user_id='42')
            await engine.note_own_topic(state, '自己修理旧风车')
            await engine.note_own_topic(state, '一起种花', user_id='7')
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

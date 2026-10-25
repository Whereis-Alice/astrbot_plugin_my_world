"""Batch captions must identify their image before entering a per-image cache."""
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from core.state import WorldState
from tests import test_dialogue_inject as fixture
from tests import test_integration_integrity as integration
from core.media_provenance import image_origin_labels, native_image_note
from core.participation_opportunity import media_scene_allowed

vision = importlib.import_module(fixture.MODULE.__name__ + '.adapters.vision')


class CaptionIdentityTests(unittest.TestCase):
    def test_intro_and_out_of_order_numbers_cannot_shift_cached_descriptions(self):
        self.assertEqual(vision._split_multi_caption('这里是图片说明\n图2：海报\n图1：杯子', 2), ['杯子', '海报'])

    def test_duplicate_missing_or_unnumbered_batch_is_not_assigned_by_line_position(self):
        for text in ('图1：杯子\n图1：海报', '杯子\n海报', '图1：杯子\n图3：海报',
                     '图1：杯子\n图2：海报\n图1：'):
            self.assertEqual(vision._split_multi_caption(text, 2), [], text)

    def test_bare_video_is_kept_without_becoming_the_authors_text(self):
        state = WorldState(session_id='test:GroupMessage:1')
        row = state.note_chat(user_id='42', name='朋友', text='', has_videos=True, now=100,
                              origin=state.session_id, message_id='video')
        self.assertIsNotNone(row)
        self.assertTrue(row['has_videos'])
        self.assertEqual(row['reading_text'], '')
        self.assertFalse(media_scene_allowed(rows=[row], new=[row], now=100))
        self.assertTrue(media_scene_allowed(rows=[row], new=[row], now=100, mode='normal'))

    def test_current_and_quoted_images_have_separate_source_labels(self):
        Image = type('Image', (), {})
        Reply = type('Reply', (), {})
        own, quoted, reply = Image(), Image(), Reply()
        own.url, quoted.url = 'own.png', 'quoted.png'
        reply.chain, reply.id, reply.sender_nickname = [quoted], 'older-message', '原发送者'
        event = SimpleNamespace(message_obj=SimpleNamespace(message=[own, reply]))
        row = {'origin': 'test:GroupMessage:1', 'message_id': 'current', 'name': '当前发送者'}
        labels = image_origin_labels(event, [own.url, quoted.url], row)
        self.assertIn('本条发送', labels[own.url])
        self.assertIn('原消息 older-message（原发送者）', labels[quoted.url])
        self.assertNotIn('本条发送', labels[quoted.url])

    def test_transformed_native_attachment_does_not_inherit_the_current_authors_identity(self):
        event = fixture.Event('看看这张', session='test:GroupMessage:1')
        picture = type('Image', (), {'url': 'https://img/current.jpg', 'file': ''})()
        event.message_obj.message.append(picture)
        note = native_image_note(event, {'prepared.png': '图1'})
        self.assertIn('来源尚未确认', note)
        self.assertNotIn('本条发送', note)

    def test_native_quote_label_names_the_original_message(self):
        event = fixture.Event('看看这张', session='test:GroupMessage:1')
        image = type('Image', (), {'url': 'quoted.png', 'file': ''})()
        reply = type('Reply', (), {'chain': [image], 'id': 'original', 'sender_nickname': '发图的人'})()
        event.message_obj.message.append(reply)
        note = native_image_note(event, {image.url: '图1'})
        self.assertIn('原消息 original（发图的人）', note)
        self.assertNotIn('本条发送', note)


class VideoArrivalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = integration.IntegrationIntegrityTests.asyncSetUp

    async def test_video_only_arrival_reaches_presence_and_recent_chat(self):
        event = fixture.Event('', session=self.session, wake=False, mention=False)
        event.message_obj.message = [type('Video', (), {})()]
        await self.plugin.on_any_message(event)
        state = await self.plugin.engine.load_state(self.session, cold_start=False)
        row = next(row for row in state.recent_chat if row.get('message_id') == '4711')
        self.assertTrue(row['has_videos'])
        self.assertEqual(row['reading_text'], '')
        self.assertIn('视频', row['text'])


class NativeImageIdentityTests(unittest.IsolatedAsyncioTestCase):
    module = fixture.MODULE
    _plugin = fixture.DialogueInjectTests._plugin

    async def test_native_request_does_not_claim_history_images_are_current_attachments(self):
        session = 'test:GroupMessage:native-image-identity'
        plugin, _ = self._plugin(session)
        self.addCleanup(plugin.db.raw.close)
        plugin._chat_images_inline = AsyncMock(return_value=True)
        plugin.engine.world.context.image_max = 3
        plugin.engine.chat_images_for_reply = AsyncMock(return_value=[
            {'url': 'https://img/history-0.jpg'}, {'url': 'https://img/history-1.jpg'}])
        state = await plugin.engine.load_state(session)
        for index in range(2):
            state.note_chat(user_id='other', name='另一位群友', text='历史图片',
                            images=[f'https://img/history-{index}.jpg'], now=plugin.engine._now(),
                            message_id=f'history-{index}', origin=session)
        event = fixture.Event('看看这张', session=session)
        picture = type('Image', (), {'url': 'https://img/current.jpg', 'file': ''})()
        event.message_obj.message.append(picture)
        req = fixture.Request('看看这张')
        req.image_urls = [picture.url]
        prepare = plugin._reply_images
        plugin._reply_images = AsyncMock(wraps=prepare)
        await plugin.on_llm_request(event, req)
        self.assertEqual(req.image_urls, [picture.url])
        args, kwargs = plugin._reply_images.call_args
        delivered, marks, notes = await prepare(*args, **kwargs)
        self.assertEqual(delivered, req.image_urls)
        self.assertEqual(marks, {picture.url: '图1'})
        self.assertFalse(notes)
        legacy, marks, _ = await prepare(event, [picture.url], extra=req.image_urls)
        self.assertEqual(legacy, ['https://img/history-0.jpg', 'https://img/history-1.jpg', picture.url])
        self.assertEqual(marks[picture.url], '图3')

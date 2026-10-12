"""Unit tests for TikHub Threads service and parser."""

from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase, override_settings

from video_management.services.tikhub_threads import (
    parse_threads_user_info,
    parse_threads_posts,
    fetch_user_info,
    fetch_threads_posts,
    search_top_threads,
)


class TikHubThreadsParserTests(SimpleTestCase):
    def test_parse_threads_user_info(self):
        raw_info = {
            'pk': '12345678',
            'username': 'creator_vn',
            'full_name': 'Creator Viet Nam',
            'biography': 'Kênh chia sẻ nội dung công nghệ',
            'profile_pic_url': 'https://scontent.cdninstagram.com/avatar.jpg',
            'follower_count': 50000,
            'is_verified': True,
        }
        parsed = parse_threads_user_info('creator_vn', raw_info)
        self.assertEqual(parsed['threads_user_id'], '12345678')
        self.assertEqual(parsed['username'], 'creator_vn')
        self.assertEqual(parsed['name'], 'Creator Viet Nam')
        self.assertEqual(parsed['followers_count'], 50000)
        self.assertTrue(parsed['is_verified'])
        self.assertEqual(parsed['url'], 'https://www.threads.net/@creator_vn')

    def test_parse_threads_posts(self):
        raw_posts = [
            {
                'id': '3349029093483693129',
                'code': 'C5abcd123',
                'caption': {'text': 'Video chia sẻ mẹo làm việc hiệu quả #productivity'},
                'video_versions': [{'url': 'https://video.cdninstagram.com/v.mp4'}],
                'image_versions2': {'candidates': [{'url': 'https://image.cdninstagram.com/thumb.jpg'}]},
                'like_count': 1500,
                'text_post_app_info': {
                    'direct_reply_count': 85,
                    'repost_count': 42,
                    'quote_count': 10,
                },
                'taken_at': 1713000000,
                'user': {
                    'username': 'creator_vn',
                    'full_name': 'Creator Viet Nam',
                    'profile_pic_url': 'https://scontent.cdninstagram.com/avatar.jpg',
                },
            }
        ]
        parsed = parse_threads_posts(raw_posts)
        self.assertEqual(len(parsed), 1)
        p = parsed[0]
        self.assertEqual(p['post_id'], '3349029093483693129')
        self.assertEqual(p['shortcode'], 'C5abcd123')
        self.assertEqual(p['media_type'], 'VIDEO')
        self.assertEqual(p['video_url'], 'https://video.cdninstagram.com/v.mp4')
        self.assertEqual(p['thumbnail_url'], 'https://image.cdninstagram.com/thumb.jpg')
        self.assertEqual(p['likes_count'], 1500)
        self.assertEqual(p['replies_count'], 85)
        self.assertEqual(p['reposts_count'], 42)
        self.assertIn('#productivity', p['text'])
        self.assertEqual(p['author_username'], 'creator_vn')

    @override_settings(TIKHUB_API_KEY='test_token', TIKHUB_API_BASE_URL='https://api.tikhub.io')
    @patch('requests.get')
    def test_fetch_user_info_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            'code': 200,
            'data': {
                'pk': '998877',
                'username': 'test_user',
                'full_name': 'Test User',
                'follower_count': 100,
            }
        }
        mock_get.return_value = mock_resp

        result = fetch_user_info('test_user')
        self.assertIsNotNone(result)
        self.assertEqual(result['pk'], '998877')

    @patch('video_management.services.tikhub_threads.search_threads_web')
    def test_search_top_threads_keyword(self, mock_web_search):
        mock_web_search.return_value = [
            {'post': {'id': '111', 'code': 'xyz', 'like_count': 50}}
        ]
        results = search_top_threads('vàng bạc', count=10)
        mock_web_search.assert_called_once_with('vàng bạc', count=10)
        self.assertEqual(len(results), 1)

    def test_is_vietnamese_text(self):
        from video_management.services.tikhub_threads import is_vietnamese_text
        self.assertTrue(is_vietnamese_text('Topic: toàn bộ kiến thức Lịch Sử để thi THPTQG'))
        self.assertTrue(is_vietnamese_text('Dạo này thấy nhiều chủ đề hay quá'))
        self.assertFalse(is_vietnamese_text('So if a man is wearing a football jersey can crash tackle him in the street?'))
        self.assertFalse(is_vietnamese_text('В Вильнюсе конфликт дошёл до ножа.'))

    def test_parse_threads_posts_vietnamese_priority_and_strict_match(self):
        raw_items = [
            # Bài nước ngoài có like rất cao (27k like) nhưng không liên quan từ khoá
            {'post': {'id': '1', 'caption': {'text': 'Football jersey post in English'}, 'like_count': 27000}},
            # Bài nước ngoài chứa từ khoá (500 like)
            {'post': {'id': '2', 'caption': {'text': 'This is a trending topic about cars in English'}, 'like_count': 500}},
            # Bài tiếng Việt chứa từ khoá (100 like)
            {'post': {'id': '3', 'caption': {'text': 'Topic: Lí do nhạc buitruong nghe rất cuốn'}, 'like_count': 100}},
            # Bài tiếng Việt chứa từ khoá (3000 like)
            {'post': {'id': '4', 'caption': {'text': 'Topic: toàn bộ kiến thức Lịch Sử ôn thi'}, 'like_count': 3000}},
        ]
        # Tìm kiếm 'topic:' (có dấu hai chấm)
        parsed = parse_threads_posts(raw_items, query='topic:')

        # Bài 1 (football jersey) hoàn toàn không có 'topic' -> BỊ LOẠI BỎ
        self.assertNotIn('1', [p['post_id'] for p in parsed])
        self.assertEqual(len(parsed), 3)

        # Bài tiếng Việt phải được xếp lên ĐẦU TIÊN (bất kể bài nước ngoài like cao hơn)
        # Thứ tự đúng: Bài 4 (VN, 3000 like) -> Bài 3 (VN, 100 like) -> Bài 2 (Nước ngoài, 500 like)
        self.assertEqual(parsed[0]['post_id'], '4')
        self.assertEqual(parsed[1]['post_id'], '3')
        self.assertEqual(parsed[2]['post_id'], '2')
        self.assertTrue(parsed[0]['is_vietnamese'])
        self.assertTrue(parsed[1]['is_vietnamese'])
        self.assertFalse(parsed[2]['is_vietnamese'])



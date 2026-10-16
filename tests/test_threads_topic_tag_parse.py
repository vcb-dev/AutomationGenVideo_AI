"""Đọc TAG CHỦ ĐỀ của bài Threads ("người đăng › trang sức") từ dữ liệu crawl tìm kiếm.

Tag nằm ở post.text_post_app_info.tag_header (kiểm chứng thật 2026-10-01). parse_threads_posts trả
thêm topic_tag để BE lưu và lọc; bộ lọc từ khoá giữ nguyên hành vi cũ (phần tag là làm THÊM).

Chạy: python manage.py test tests.test_threads_topic_tag_parse
"""

from django.test import SimpleTestCase

from video_management.services.tikhub_threads import extract_topic_tag, parse_threads_posts

TAG_HEADER = {
    'display_name': 'trang sức',
    'id': '18320871415120224',
    'is_community': False,
    'tag_cluster_name': 'trang sức',
    'community_emoji': None,
}


def _web_post(pk, text, tag_header=None, likes=0):
    """Một thread item như trang tìm kiếm Threads trả về."""
    return {
        'post': {
            'pk': pk,
            'code': f'C{pk}',
            'caption': {'text': text},
            'like_count': likes,
            'taken_at': 1790000000,
            'user': {'username': f'user{pk}', 'full_name': f'User {pk}'},
            'text_post_app_info': {'tag_header': tag_header, 'direct_reply_count': 1},
        }
    }



class TopicTagFromCrawlTests(SimpleTestCase):
    def test_reads_tag_from_tag_header(self):
        """Tag lấy từ text_post_app_info.tag_header.display_name."""
        self.assertEqual(extract_topic_tag(_web_post('1', 'x', TAG_HEADER)['post']), 'trang sức')

    def test_untagged_post(self):
        """Bài không gắn tag / dữ liệu thiếu → ''."""
        self.assertEqual(extract_topic_tag(_web_post('1', 'x', None)['post']), '')
        self.assertEqual(extract_topic_tag({}), '')
        self.assertEqual(extract_topic_tag({'text_post_app_info': None}), '')

    def test_parse_threads_posts_includes_topic_tag(self):
        """parse_threads_posts trả thêm topic_tag cho từng bài."""
        posts = parse_threads_posts([_web_post('1', 'nhẫn bạc', TAG_HEADER), _web_post('2', 'áo dài')])
        by_id = {p['post_id']: p for p in posts}
        self.assertEqual(by_id['1']['topic_tag'], 'trang sức')
        self.assertEqual(by_id['2']['topic_tag'], '')

    def test_keyword_search_unchanged(self):
        """Phần tag là LÀM THÊM — bộ lọc từ khoá không đổi: bài không chứa từ khoá vẫn bị bỏ."""
        posts = parse_threads_posts(
            [_web_post('1', 'nhẫn OVal 3ct D vvs2', TAG_HEADER), _web_post('2', 'mua trang sức ở đâu')],
            query='trang sức',
        )
        self.assertEqual([p['post_id'] for p in posts], ['2'])

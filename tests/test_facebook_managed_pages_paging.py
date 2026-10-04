"""/me/accounts phải lật trang — không được rơi page thứ 101 trở đi.

Graph API trả tối đa 100 page mỗi lượt kèm `paging.next` + `paging.cursors.after`. Trước bản
sửa, get_my_managed_pages chỉ đọc lượt đầu; tài khoản hệ thống đã quản lý 113 page (Kênh nội bộ,
02/10/2026) nên mỗi lần "Đồng bộ từ Facebook" đều mất phần dư, page mới thêm dễ rơi đúng vào đó.

Chạy: python manage.py test tests.test_facebook_managed_pages_paging
"""

from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase

from video_management.services.facebook_graph_service import FacebookGraphService


def page(n):
    return {'id': str(1000 + n), 'name': f'Page {n}', 'access_token': f'tok{n}'}


def graph_response(pages, after=None, has_next=False):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    body = {'data': pages}
    if after:
        body['paging'] = {'cursors': {'after': after}}
        if has_next:
            body['paging']['next'] = f'https://graph.facebook.com/me/accounts?after={after}'
    resp.json.return_value = body
    return resp


def make_service():
    service = FacebookGraphService.__new__(FacebookGraphService)
    service.access_token = 'system-token'
    return service


class ManagedPagesPagingTests(SimpleTestCase):
    def test_follows_next_page_until_the_end(self):
        first = [page(i) for i in range(100)]
        second = [page(i) for i in range(100, 113)]
        responses = [graph_response(first, after='CUR1', has_next=True), graph_response(second, after='CUR2')]
        with patch('video_management.services.facebook_graph_service.requests.get', side_effect=responses) as get:
            pages = make_service().get_my_managed_pages('user-token')

        self.assertEqual(len(pages), 113)
        self.assertEqual(get.call_count, 2)
        second_params = get.call_args_list[1].kwargs['params']
        self.assertEqual(second_params['after'], 'CUR1')
        self.assertEqual(second_params['access_token'], 'user-token')
        self.assertEqual(second_params['limit'], 100)

    def test_single_page_makes_one_call(self):
        with patch('video_management.services.facebook_graph_service.requests.get',
                   return_value=graph_response([page(1), page(2)], after='CUR1')) as get:
            pages = make_service().get_my_managed_pages()

        self.assertEqual([p['id'] for p in pages], ['1001', '1002'])
        self.assertEqual(get.call_count, 1)

    def test_repeated_cursor_stops_instead_of_looping_forever(self):
        loop = graph_response([page(1)], after='SAME', has_next=True)
        with patch('video_management.services.facebook_graph_service.requests.get', side_effect=[loop, loop, loop]) as get:
            pages = make_service().get_my_managed_pages('user-token')

        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(pages), 2)

    def test_error_on_later_page_keeps_pages_already_fetched(self):
        first = graph_response([page(i) for i in range(100)], after='CUR1', has_next=True)
        with patch('video_management.services.facebook_graph_service.requests.get',
                   side_effect=[first, requests.exceptions.ConnectionError('reset')]):
            pages = make_service().get_my_managed_pages('user-token')

        self.assertEqual(len(pages), 100)

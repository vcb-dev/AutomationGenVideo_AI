"""Giữ cho hoá đơn TikHub không phình lại.

Mỗi lượt gọi TikHub là một lượt tính phí. Số liệu thật lấy từ hoá đơn ngày 2026-07-30:
146 lượt trong ngày, 0,785 USD (~0,0055 USD/lượt), trong đó:

    xiaohongshu/app_v2/get_user_posted_notes   50 luot  (34%)
    tiktok/app/v3/fetch_one_video              18 luot  (12%)
    douyin/web/fetch_one_video                 16 luot  (11%)

Test này khoá lại hai khoản tiết kiệm đã sửa, để lần sau ai đó gỡ ra là biết ngay.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase


def _hoi_dap(body, status=200):
    class R:
        status_code = status
        ok = status == 200
        text = ''

        def json(self):
            return body

    return R()


class ChanSoTrang(SimpleTestCase):
    """Vòng lặp phân trang phải có trần, nếu không một lần cào ngốn vài chục lượt."""

    def test_xiaohongshu_khong_lat_trang_vo_han(self):
        from video_management.services import tikhub_xiaohongshu as xhs

        # Trang nào cũng báo "còn nữa" nhưng KHÔNG có note video nào — đúng tình huống thật:
        # Xiaohongshu là nền tảng ảnh, tài khoản đăng chủ yếu ảnh thì điều kiện dừng đếm
        # video không bao giờ đủ, vòng lặp cứ lật mãi.
        trang = {'data': {'data': {
            'notes': [{'type': 'normal', 'id': f'n{i}', 'cursor': f'c{i}'} for i in range(20)],
            'has_more': True,
        }}}

        # side_effect có HẠN, không dùng return_value: nếu thiếu trần số trang thì vòng lặp
        # chạy mãi và test sẽ TREO thay vì báo đỏ — người chạy chỉ thấy nó đứng im, không
        # biết vì sao (đã bị đúng như vậy một lần). Hết 10 hồi đáp là StopIteration.
        with patch.object(xhs, '_tikhub_base', return_value='https://x'), \
             patch.object(xhs.settings, 'TIKHUB_API_KEY', 'k', create=True), \
             patch.object(xhs.requests, 'get', side_effect=[_hoi_dap(trang)] * 10) as goi:
            try:
                xhs.fetch_xhs_user_video_notes('u1', count=100)
            except StopIteration:
                self.fail('Xin qua 10 trang cho MOT lan cao — dung la thu da lam het sach so du.')

        self.assertLessEqual(goi.call_count, 10)

    def test_instagram_khong_lat_trang_vo_han(self):
        from video_management.services import tikhub_instagram as ig

        trang = {'data': {'items': [], 'pagination_token': 'tiep'}, 'pagination_token': 'tiep'}
        with patch.object(ig, '_tikhub_base', return_value='https://x'), \
             patch.object(ig.settings, 'TIKHUB_API_KEY', 'k', create=True), \
             patch.object(ig.requests, 'get', side_effect=[_hoi_dap(trang)] * 10) as goi:
            try:
                ig.fetch_instagram_reels('ai_do', count=100)
            except StopIteration:
                self.fail('Xin qua 10 trang cho mot lan cao.')

        self.assertLessEqual(goi.call_count, 10)


class BoDemDungChung(SimpleTestCase):
    """play-url và video-detail gọi CÙNG endpoint — chỉ được tính phí một lần."""

    def setUp(self):
        cache.clear()

    def test_hai_dich_vu_chung_mot_luot_goi(self):
        from video_management.services import tikhub_cache

        # Hồi đáp giả mang đủ cả link phát lẫn chỉ số, đúng như TikHub trả về thật:
        # một hồi đáp phục vụ được cả hai nhu cầu.
        body = {'code': 200, 'data': {'aweme_detail': {
            'video': {'play_addr_h264': {'url_list': ['https://cdn/a.mp4']}},
            'statistics': {'digg_count': 5},
        }}}

        with patch.object(tikhub_cache.requests, 'get', return_value=_hoi_dap(body)) as goi:
            duong = '/api/v1/douyin/web/fetch_one_video'
            tham_so = {'aweme_id': '123'}
            a = tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k')
            b = tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k')

        self.assertEqual(goi.call_count, 1, 'Van goi TikHub hai lan cho cung mot video.')
        self.assertEqual(a.json(), b.json())

    def test_video_khac_nhau_van_goi_rieng(self):
        """Đệm theo endpoint mà quên tham số thì video B sẽ nhận dữ liệu của video A."""
        from video_management.services import tikhub_cache

        with patch.object(tikhub_cache.requests, 'get', return_value=_hoi_dap({'code': 200})) as goi:
            duong = '/api/v1/douyin/web/fetch_one_video'
            tikhub_cache.goi_co_dem('https://x', duong, {'aweme_id': '111'}, 'k')
            tikhub_cache.goi_co_dem('https://x', duong, {'aweme_id': '222'}, 'k')

        self.assertEqual(goi.call_count, 2)

    def test_khong_dem_lan_that_bai(self):
        """Đệm cả lỗi thì TikHub trục trặc một giây, video chết nguyên một tiếng."""
        from video_management.services import tikhub_cache

        hong = _hoi_dap({'detail': 'loi'}, status=500)
        tot = _hoi_dap({'code': 200, 'data': {}})
        with patch.object(tikhub_cache.requests, 'get', side_effect=[hong, tot]) as goi:
            duong = '/api/v1/douyin/web/fetch_one_video'
            tham_so = {'aweme_id': '333'}
            tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k')
            lan_hai = tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k')

        self.assertEqual(goi.call_count, 2, 'Lan hong bi dong lai trong bo dem.')
        self.assertEqual(lan_hai.status_code, 200)

    def test_han_dem_ngan_hon_han_song_cua_link_phat(self):
        """Link phát Douyin hết hạn ~3 giờ; đệm lâu hơn là phát ra link đã chết."""
        from video_management.services import tikhub_cache

        self.assertLess(tikhub_cache.TTL_MAC_DINH, 3 * 3600)


class BayDaGapKhiRaSoat(SimpleTestCase):
    """Ba lỗi tìm ra khi rà soát lại chính hai bản vá tiết kiệm ở trên."""

    def setUp(self):
        cache.clear()

    def test_khong_dem_hoi_dap_loi_cua_tikhub(self):
        """TikHub trả HTTP 200 nhưng gắn mã lỗi TRONG THÂN — đệm là video chết cả tiếng."""
        from video_management.services import tikhub_cache

        loi = _hoi_dap({'code': 404, 'message': 'video khong ton tai'})
        tot = _hoi_dap({'code': 200, 'data': {'x': 1}})
        with patch.object(tikhub_cache.requests, 'get', side_effect=[loi, tot]) as goi:
            duong = '/api/v1/douyin/web/fetch_one_video'
            tham_so = {'aweme_id': '444'}
            tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k')
            lan_hai = tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k')

        self.assertEqual(goi.call_count, 2, 'Hoi dap LOI bi dong lai trong bo dem mot tieng.')
        self.assertEqual(lan_hai.json()['code'], 200)

    def test_lam_moi_bo_qua_bo_dem(self):
        """Không có cửa này thì vòng thử-lại-khi-403 của BE nhận lại đúng link vừa hỏng."""
        from video_management.services import tikhub_cache

        cu = _hoi_dap({'code': 200, 'data': 'link-het-han'})
        moi = _hoi_dap({'code': 200, 'data': 'link-moi'})
        with patch.object(tikhub_cache.requests, 'get', side_effect=[cu, moi]) as goi:
            duong = '/api/v1/douyin/web/fetch_one_video'
            tham_so = {'aweme_id': '555'}
            tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k')
            lam_moi = tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k', lam_moi=True)

        self.assertEqual(goi.call_count, 2, 'lam_moi=True van bi bo dem chan lai.')
        self.assertEqual(lam_moi.json()['data'], 'link-moi')

    def test_lam_moi_xong_thi_ghi_de_bo_dem(self):
        """Lấy mới xong phải thay luôn bản cũ, không thì lượt sau lại nhận link hỏng."""
        from video_management.services import tikhub_cache

        cu = _hoi_dap({'code': 200, 'data': 'link-het-han'})
        moi = _hoi_dap({'code': 200, 'data': 'link-moi'})
        with patch.object(tikhub_cache.requests, 'get', side_effect=[cu, moi]) as goi:
            duong = '/api/v1/douyin/web/fetch_one_video'
            tham_so = {'aweme_id': '666'}
            tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k')
            tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k', lam_moi=True)
            sau_do = tikhub_cache.goi_co_dem('https://x', duong, tham_so, 'k')

        self.assertEqual(goi.call_count, 2, 'Luot thu ba dang le phai lay tu bo dem.')
        self.assertEqual(sau_do.json()['data'], 'link-moi', 'Bo dem van giu link cu da hong.')

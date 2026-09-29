"""Video → kịch bản cho Bộ Sưu Tập: tải video (miễn phí trước, TikHub dự phòng) rồi Gemini viết.

Khoá phần logic: chọn đường tải, khi nào mới gọi TikHub (tính phí), báo lỗi rõ từng đường, engine
tắt thì chỉ báo kết quả tải, luôn dọn file tạm, và các bản sửa trình tải đo được trên image
Railway (link iesdouyin đi nhầm yt-dlp, bộ lọc định dạng làm hỏng Bilibili).

Phần tải THẬT (yt-dlp, trình duyệt ẩn Douyin, CDN TikHub) và gọi Gemini THẬT chỉ kiểm chứng được
bằng mạng thật nên đã chạy tay trên image dựng từ Dockerfile.railway — mock lại chính trình tải
ở đây chỉ khẳng định lại giả định của người viết test.

Chạy: python manage.py test tests.test_video_script_pipeline
"""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services import gemini_video_script
from video_management.services import tikhub_download_source as dls
from video_management.services import video_script_pipeline as pipeline
from video_management.views import scraped_video_script_views as views
from video_management.views import video_downloader_views as dl


def _video(path='/tmp/v.mp4', source='free'):
    return pipeline.DownloadedVideo(path=path, duration=36.0, has_audio=True, size_bytes=5_700_000, source=source)


class DetectPlatformTests(SimpleTestCase):
    def test_nhan_dien_nen_tang_theo_ten_mien(self):
        self.assertEqual(pipeline.detect_platform('https://www.douyin.com/video/1'), 'douyin')
        self.assertEqual(pipeline.detect_platform('https://www.iesdouyin.com/share/video/1/'), 'douyin')
        self.assertEqual(pipeline.detect_platform('http://xhslink.com/a/b'), 'xiaohongshu')
        self.assertEqual(pipeline.detect_platform('https://vt.tiktok.com/ZS/'), 'tiktok')
        self.assertEqual(pipeline.detect_platform('https://www.reddit.com/r/Jewelry/comments/1ojnh50/x/'), 'reddit')
        self.assertEqual(pipeline.detect_platform('https://v.redd.it/abc123'), 'reddit')
        self.assertEqual(pipeline.detect_platform('https://example.com/video/1'), '')


class DownloaderFixTests(SimpleTestCase):
    def test_link_chia_se_iesdouyin_quy_ve_douyin_video(self):
        url = dl._extract_url('https://www.iesdouyin.com/share/video/7104199576621567269/?region=US&mid=1')
        self.assertEqual(url, 'https://www.douyin.com/video/7104199576621567269')
        self.assertTrue(dl._douyin_platform(url))  # → đi đường trình duyệt ẩn, không phải yt-dlp

    def test_link_modal_douyin_van_quy_doi_nhu_cu(self):
        self.assertEqual(dl._extract_url('https://www.douyin.com/jingxuan?modal_id=7676122785118164474'),
                         'https://www.douyin.com/video/7676122785118164474')

    def test_reddit_khong_tai_chia_khuc_cac_nen_tang_khac_van_giu(self):
        self.assertEqual(dl.ytdlp_chunk_args('https://www.reddit.com/r/a/comments/1wojzwk/x/'), [])
        self.assertEqual(dl.ytdlp_chunk_args('https://v.redd.it/dnph4pusgcrh1'), [])
        self.assertEqual(dl.ytdlp_chunk_args('https://www.tiktok.com/@a/video/1'), ['--http-chunk-size', '1M'])

    def test_chon_dinh_dang_uu_tien_chu_khong_loc_cung(self):
        args = dl.ytdlp_video_format_args('480')
        self.assertEqual(args[args.index('--format') + 1], 'bv*+ba/b')
        self.assertTrue(args[args.index('--format-sort') + 1].startswith('res:480,'))
        self.assertNotIn('height<=', ' '.join(args))
        best = dl.ytdlp_video_format_args('best')
        self.assertTrue(best[best.index('--format-sort') + 1].startswith('res,'))


class DownloadForAnalysisTests(SimpleTestCase):
    def test_tai_mien_phi_duoc_thi_khong_goi_tikhub(self):
        with mock.patch.object(pipeline, '_download_free', return_value=('/tmp/a.mp4', '')), \
             mock.patch.object(pipeline, '_describe', side_effect=lambda p, s: _video(p, s)), \
             mock.patch('video_management.services.tikhub_download_source.fetch_download_source') as tikhub:
            video = pipeline.download_for_analysis('https://www.douyin.com/video/1', video_id='1')
        self.assertEqual(video.source, 'free')
        tikhub.assert_not_called()

    def test_mien_phi_hong_thi_xin_link_tikhub_moi_roi_tai_thang(self):
        with mock.patch.object(pipeline, '_download_free', return_value=(None, 'Douyin chặn')), \
             mock.patch('video_management.services.tikhub_download_source.fetch_download_source', return_value=dls.DownloadSource(['https://cdn/x.mp4'])) as tikhub, \
             mock.patch.object(pipeline, '_download_direct', return_value=('/tmp/b.mp4', '')) as direct, \
             mock.patch.object(pipeline, '_describe', side_effect=lambda p, s: _video(p, s)):
            video = pipeline.download_for_analysis('https://www.douyin.com/video/7676', video_id='7676')
        self.assertEqual(video.source, 'tikhub')
        tikhub.assert_called_once_with('douyin', video_id='7676', video_url='https://www.douyin.com/video/7676')
        direct.assert_called_once_with('https://cdn/x.mp4', {})

    def test_facebook_khong_co_du_phong_thi_bao_loi_ngay(self):
        with mock.patch.object(pipeline, '_download_free', return_value=(None, 'Facebook chặn')), \
             mock.patch('video_management.services.tikhub_download_source.fetch_download_source') as tikhub:
            with self.assertRaises(pipeline.VideoDownloadError) as ctx:
                pipeline.download_for_analysis('https://www.facebook.com/reel/123456789')
        tikhub.assert_not_called()
        self.assertIn('Facebook chặn', str(ctx.exception))

    def test_bilibili_du_phong_qua_ffmpeg_kem_referer(self):
        source = dls.DownloadSource(['https://upos/v.m4s', 'https://upos/a.m4s'], via='ffmpeg', headers={'Referer': dls.BILIBILI_REFERER})
        with mock.patch.object(pipeline, '_download_free', return_value=(None, 'hỏng')), \
             mock.patch('video_management.services.tikhub_download_source.fetch_download_source', return_value=source), \
             mock.patch.object(pipeline, '_download_ffmpeg', return_value=('/tmp/c.mp4', '')) as ff, \
             mock.patch.object(pipeline, '_describe', side_effect=lambda p, s: _video(p, s)):
            video = pipeline.download_for_analysis('https://www.bilibili.com/video/BV1qaqwBfEpQ', video_id='BV1qaqwBfEpQ')
        self.assertEqual(video.source, 'tikhub')
        ff.assert_called_once_with(['https://upos/v.m4s', 'https://upos/a.m4s'], {'Referer': dls.BILIBILI_REFERER})

    def test_tikhub_het_tien_bao_ro_ly_do(self):
        with mock.patch.object(pipeline, '_download_free', return_value=(None, 'lỗi')), \
             mock.patch('video_management.services.tikhub_download_source.fetch_download_source', side_effect=dls.TikHubOutOfCredit()):
            with self.assertRaises(pipeline.VideoDownloadError) as ctx:
                pipeline.download_for_analysis('https://www.kuaishou.com/short-video/5211')
        self.assertIn('hết tiền', str(ctx.exception))

    def test_file_tai_ve_hong_thi_don_file_va_thu_duong_khac(self):
        with mock.patch.object(pipeline, '_download_free', return_value=('/tmp/bad.mp4', '')), \
             mock.patch.object(pipeline, '_describe', side_effect=[ValueError('không có luồng hình'), _video('/tmp/ok.mp4', 'tikhub')]), \
             mock.patch.object(pipeline, 'cleanup_path') as cleanup, \
             mock.patch('video_management.services.tikhub_download_source.fetch_download_source', return_value=dls.DownloadSource(['https://cdn/y.mp4'])), \
             mock.patch.object(pipeline, '_download_direct', return_value=('/tmp/ok.mp4', '')):
            video = pipeline.download_for_analysis('https://www.tiktok.com/@a/video/1', video_id='1')
        self.assertEqual(video.source, 'tikhub')
        cleanup.assert_called_once_with('/tmp/bad.mp4')


class DownloadSourcePickerTests(SimpleTestCase):
    """Instagram/Bilibili/Reddit đã đối chiếu hồi đáp thật (2026-09-27, cả ba tải được, có tiếng).
    Dữ liệu mẫu ở đây rút gọn theo đúng cấu trúc đó."""

    def test_youtube_khong_co_du_phong_tikhub(self):
        # Link TikHub gắn cứng IP máy chủ của họ (ip=... trong sparams) → luôn 403 từ máy mình.
        self.assertNotIn('youtube', pipeline.TIKHUB_FALLBACK_PLATFORMS)
        self.assertNotIn('youtube', dls.SUPPORTED_PLATFORMS)
        with mock.patch.object(dls, '_call') as call:
            self.assertIsNone(dls.fetch_download_source('youtube', video_id='OOIs3kky2wc'))
        call.assert_not_called()

    def test_instagram_lay_video_url_o_moi_do_sau(self):
        self.assertEqual(dls.pick_instagram({'data': {'xdt_shortcode_media': {'video_url': 'https://ig/v.mp4'}}}).inputs, ['https://ig/v.mp4'])
        self.assertEqual(dls.pick_instagram({'items': [{'video_versions': [{'url': 'https://ig/w.mp4'}]}]}).inputs, ['https://ig/w.mp4'])

    def test_bilibili_dash_ghep_hinh_h264_toi_480_voi_tieng_nhe_nhat(self):
        data = {'data': {'dash': {
            'video': [{'baseUrl': 'https://up/1080', 'height': 1080, 'codecid': 7},
                      {'baseUrl': 'https://up/480', 'height': 480, 'codecid': 7},
                      {'baseUrl': 'https://up/480hevc', 'height': 480, 'codecid': 12}],
            'audio': [{'baseUrl': 'https://up/a-high', 'bandwidth': 320000}, {'baseUrl': 'https://up/a-low', 'bandwidth': 64000}],
        }}}
        src = dls.pick_bilibili_playurl(data)
        self.assertEqual((src.via, src.inputs, src.headers['Referer']), ('ffmpeg', ['https://up/480', 'https://up/a-low'], dls.BILIBILI_REFERER))

    def test_reddit_uu_tien_hls_co_tieng(self):
        data = {'data': {'media': {'reddit_video': {'fallback_url': 'https://v.redd.it/x/DASH_720.mp4', 'hls_url': 'https://v.redd.it/x/HLSPlaylist.m3u8?a=1'}}}}
        src = dls.pick_reddit(data)
        self.assertEqual((src.via, src.inputs), ('ffmpeg', ['https://v.redd.it/x/HLSPlaylist.m3u8?a=1']))
        only_mp4 = dls.pick_reddit({'media': {'reddit_video': {'fallback_url': 'https://v.redd.it/x/DASH_480.mp4'}}})
        self.assertEqual((only_mp4.via, only_mp4.inputs), ('file', ['https://v.redd.it/x/DASH_480.mp4']))

    def test_ma_bai_reddit_tu_link(self):
        self.assertEqual(dls.reddit_post_id('', 'https://www.reddit.com/r/Jewelry/comments/1OJNH50/ring/'), '1ojnh50')
        self.assertEqual(dls.reddit_post_id('', 'https://redd.it/1ojnh50'), '1ojnh50')
        self.assertEqual(dls.reddit_post_id('', 'https://v.redd.it/abc123xyz'), '')  # mã media, không phải mã bài

    def test_goi_dung_endpoint_reddit_voi_tien_to_t3(self):
        with mock.patch.object(dls, '_call', return_value={'hls_url': 'https://v.redd.it/x/HLS.m3u8'}) as call:
            src = dls.fetch_download_source('reddit', video_id='1ojnh50')
        call.assert_called_once_with('/api/v1/reddit/app/fetch_post_details', {'post_id': 't3_1ojnh50'})
        self.assertEqual(src.via, 'ffmpeg')

    def test_bon_nen_tang_cu_van_dung_fetch_play_url(self):
        with mock.patch.object(dls, 'fetch_play_url', return_value='https://cdn/d.mp4') as play:
            src = dls.fetch_download_source('douyin', video_id='7676')
        play.assert_called_once_with('douyin', video_id='7676', video_url='')
        self.assertEqual((src.via, src.inputs), ('file', ['https://cdn/d.mp4']))

    def test_het_tien_bao_ngoai_le_rieng(self):
        from video_management.services.tikhub_play_url import HET_TIEN
        with mock.patch.object(dls, 'fetch_play_url', return_value=HET_TIEN):
            with self.assertRaises(dls.TikHubOutOfCredit):
                dls.fetch_download_source('tiktok', video_id='1')


class YoutubeDetailTests(SimpleTestCase):
    """Số liệu YouTube khi đề xuất: đường cũ web/get_video_info_v2 trả 404 (đo 2026-09-27)."""

    def test_dung_endpoint_web_v2(self):
        from video_management.services import tikhub_video_detail as tvd
        self.assertEqual(tvd.PLATFORM_ENDPOINTS['youtube']['path'], '/api/v1/youtube/web_v2/get_video_info_v2')

    def test_boc_du_lieu_phang_cua_web_v2(self):
        from video_management.services import tikhub_video_detail as tvd
        # Rút gọn từ hồi đáp thật của web_v2/get_video_info_v2
        data = {'video_id': 'OOIs3kky2wc', 'title': 'Vẫn đang #bangquacaugiay đâyyy', 'description': 'mô tả',
                'author': 'Wxrdie', 'channel_id': 'UCAhfSPCb_HzvHSI54YCZ6GA', 'channel_handle': '@wxrdie',
                'view_count': '7744', 'like_count': '226', 'comment_count': '2',
                'thumbnail_url': 'https://i.ytimg.com/vi/OOIs3kky2wc/maxres2.jpg'}
        shaped = tvd._shape_youtube(data)
        self.assertEqual((shaped['title'], shaped['author_name'], shaped['author_username']), ('Vẫn đang #bangquacaugiay đâyyy', 'Wxrdie', '@wxrdie'))
        self.assertEqual((shaped['views_count'], shaped['likes_count'], shaped['comments_count']), (7744, 226, 2))
        self.assertEqual(shaped['thumbnail_url'], 'https://i.ytimg.com/vi/OOIs3kky2wc/maxres2.jpg')

    def test_van_doc_duoc_dang_player_response_cu(self):
        from video_management.services import tikhub_video_detail as tvd
        shaped = tvd._shape_youtube({'videoDetails': {'title': 'T', 'author': 'A', 'channelId': 'C', 'viewCount': '9'}})
        self.assertEqual((shaped['title'], shaped['views_count']), ('T', 9))


class BilibiliDetailTests(SimpleTestCase):
    # Rút gọn từ hồi đáp THẬT của TikHub /api/v1/bilibili/web/fetch_one_video (BV1qaqwBfEpQ, 2026-09-28)
    BODY = {'code': 200, 'data': {'code': 0, 'message': '0', 'data': {
        'bvid': 'BV1qaqwBfEpQ', 'title': '蝴蝶刀 绕指下旋2教程 高观赏性教学 动作帅气 不难 讲解非常详细！', 'desc': '-',
        'owner': {'mid': 12345, 'name': '大海蝴蝶花式'}, 'pic': 'http://i0.hdslb.com/bfs/archive/x.jpg',
        'stat': {'view': 31953, 'like': 2137, 'reply': 51, 'share': 30}}}}

    def _fetch(self, body):
        from django.test import override_settings
        from video_management.services import tikhub_video_detail as tvd
        resp = SimpleNamespace(status_code=200, json=lambda: body, text='')
        with override_settings(TIKHUB_API_KEY='k'), mock.patch.object(tvd, 'goi_co_dem', return_value=resp):
            return tvd.fetch_video_detail('bilibili', video_id='BV1qaqwBfEpQ')

    def test_tieu_de_lay_title_khong_lay_mo_ta_trong(self):
        # Trước đây ra "-" (mô tả trống của Bilibili) → mọi đề xuất Bilibili mang tiêu đề "-"
        d = self._fetch(self.BODY)
        self.assertEqual(d['title'], '蝴蝶刀 绕指下旋2教程 高观赏性教学 动作帅气 不难 讲解非常详细！')
        self.assertEqual(d['description'], '')
        self.assertEqual((d['author_name'], d['views_count'], d['likes_count'], d['comments_count'], d['shares_count']),
                         ('大海蝴蝶花式', 31953, 2137, 51, 30))

    def test_co_mo_ta_that_thi_giu_mo_ta_thieu_title_thi_dung_mo_ta(self):
        import copy
        body = copy.deepcopy(self.BODY)
        body['data']['data']['desc'] = 'Hướng dẫn chi tiết'
        self.assertEqual(self._fetch(body)['description'], 'Hướng dẫn chi tiết')
        body['data']['data']['title'] = ''
        self.assertEqual(self._fetch(body)['title'], 'Hướng dẫn chi tiết')


class FfmpegDownloadTests(SimpleTestCase):
    def test_het_han_thi_cuu_phan_dau(self):
        import subprocess
        with mock.patch.object(pipeline.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffmpeg', 1)), \
             mock.patch.object(pipeline.os.path, 'isfile', return_value=True), \
             mock.patch.object(pipeline, '_salvage_partial', return_value='/tmp/x.part.mp4') as salvage, \
             mock.patch.object(pipeline.os, 'remove'):
            path, err = pipeline._download_ffmpeg(['https://v.redd.it/x/HLS.m3u8'])
        self.assertEqual((path, err), ('/tmp/x.part.mp4', ''))
        salvage.assert_called_once()

    def test_ghep_hai_luong_map_hinh_va_tieng_kem_header(self):
        runs = []
        def fake_run(cmd, **kw):
            runs.append(cmd)
            return mock.MagicMock(returncode=0, stderr='')
        with mock.patch.object(pipeline.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(pipeline.os.path, 'isfile', return_value=True), \
             mock.patch.object(pipeline, '_salvage_partial', return_value='/tmp/y.part.mp4'), \
             mock.patch.object(pipeline.os, 'remove'):
            pipeline._download_ffmpeg(['https://up/v', 'https://up/a'], {'Referer': dls.BILIBILI_REFERER})
        cmd = runs[0]
        self.assertEqual(cmd.count('-i'), 2)
        self.assertIn('-map', cmd)
        self.assertIn('Referer: https://www.bilibili.com/', cmd[cmd.index('-headers') + 1])


class DirectDownloadDeadlineTests(SimpleTestCase):
    """Link TikHub của Kuaishou là bản gốc >100MB, CDN nhả ~300KB/s (đo trên image Railway):
    phải dừng theo hạn cho CẢ lượt tải và cứu phần đầu, không chờ vô hạn."""

    def _fake_get(self, chunks):
        resp = mock.MagicMock(status_code=200)
        resp.iter_content.return_value = iter(chunks)
        resp.__enter__.return_value = resp
        return mock.patch.object(pipeline.requests, 'get', return_value=resp)

    def test_qua_han_thi_dung_va_cuu_phan_dau(self):
        import os
        clock = iter([0, 10, pipeline.DIRECT_DOWNLOAD_DEADLINE + 1, 999])
        with self._fake_get([b'a' * 10, b'b' * 10, b'c' * 10]), \
             mock.patch.object(pipeline.time, 'monotonic', side_effect=lambda: next(clock)), \
             mock.patch.object(pipeline, '_salvage_partial', side_effect=lambda p: p + '.part.mp4') as salvage, \
             mock.patch.object(pipeline.os, 'remove') as remove:
            path, err = pipeline._download_direct('https://cdn/k.mp4')
        self.assertEqual(err, '')
        self.assertTrue(path.endswith('.part.mp4'))
        salvage.assert_called_once()
        # Chỉ xoá đúng file dở, KHÔNG xoá file .part vừa cứu được
        removed = [c.args[0] for c in remove.call_args_list]
        self.assertNotIn(path, removed)
        os.path.exists(path[:-len('.part.mp4')]) and os.remove(path[:-len('.part.mp4')])

    def test_qua_han_ma_khong_cuu_duoc_thi_bao_ro_ly_do(self):
        clock = iter([0, pipeline.DIRECT_DOWNLOAD_DEADLINE + 1, 999])
        with self._fake_get([b'a' * 10, b'b' * 10]), \
             mock.patch.object(pipeline.time, 'monotonic', side_effect=lambda: next(clock)), \
             mock.patch.object(pipeline, '_salvage_partial', return_value=None):
            path, err = pipeline._download_direct('https://cdn/k.mp4')
        self.assertIsNone(path)
        self.assertIn('CDN quá chậm', err)


@mock.patch.object(views.VideoScriptThrottle, 'allow_request', return_value=True)
class ScriptFromVideoViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(pk='leader-1', is_authenticated=True)

    def _post(self, body, auth=True):
        request = self.factory.post('/api/scraped-video/script-from-video/', body, format='json')
        if auth:
            force_authenticate(request, user=self.user)
        return views.generate_script_from_video(request)

    def test_chua_dang_nhap_bi_chan(self, _throttle):
        res = self._post({'video_url': 'https://www.tiktok.com/@a/video/1'}, auth=False)
        self.assertIn(res.status_code, (401, 403))

    def test_thieu_link_bao_400(self, _throttle):
        self.assertEqual(self._post({'video_url': 'abc'}).status_code, 400)

    def test_engine_tat_chi_bao_ket_qua_tai_va_don_file(self, _throttle):
        video = _video()
        with mock.patch.object(pipeline, 'download_for_analysis', return_value=video), \
             mock.patch.object(pipeline, 'script_engine', return_value='off'), \
             mock.patch.object(pipeline, 'engine_disabled_reason', return_value='Chưa cấu hình GEMINI_API_KEY'), \
             mock.patch.object(pipeline, 'cleanup_video') as cleanup:
            res = self._post({'video_url': 'https://www.douyin.com/video/1', 'platform': 'douyin'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'ENGINE_DISABLED')
        self.assertEqual(res.data['reason'], 'Chưa cấu hình GEMINI_API_KEY')  # BE ghi vào lỗi nếu cách cũ hỏng
        self.assertEqual(res.data['download'], {'ok': True, 'source': 'free', 'duration': 36.0, 'has_audio': True,
                                                'size_mb': 5.7, 'trimmed': False, 'error': None})
        cleanup.assert_called_once_with(video)

    def test_engine_tat_tai_hong_van_tra_200_kem_ly_do(self, _throttle):
        with mock.patch.object(pipeline, 'download_for_analysis', side_effect=pipeline.VideoDownloadError(['tải miễn phí: chặn', 'TikHub: hết tiền'])), \
             mock.patch.object(pipeline, 'script_engine', return_value='off'):
            res = self._post({'video_url': 'https://www.douyin.com/video/1'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['download'], {'ok': False, 'error': 'tải miễn phí: chặn; TikHub: hết tiền'})

    def test_engine_gemini_tra_kich_ban(self, _throttle):
        result = {'has_voice': True, 'language': 'zh', 'transcript': '大家好', 'script_text': '【HOOK】\nXin chào', 'source': 'gemini_video'}
        with mock.patch.object(pipeline, 'download_for_analysis', return_value=_video()), \
             mock.patch.object(pipeline, 'script_engine', return_value='gemini'), \
             mock.patch.object(gemini_video_script, 'generate_script', return_value=result) as gen:
            res = self._post({'video_url': 'https://www.douyin.com/video/1', 'title': 'T', 'description': 'D'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'DONE')
        self.assertEqual(res.data['transcript'], '大家好')
        # clip 36s có tiếng → phần tiếng tính theo giá âm thanh
        gen.assert_called_once_with('/tmp/v.mp4', 'T', 'D', audio_seconds=36.0)

    def test_tai_hong_thi_gemini_viet_tu_tieu_de(self, _throttle):
        result = {'has_voice': False, 'language': '', 'transcript': '', 'script_text': 'x', 'source': 'gemini_text'}
        with mock.patch.object(pipeline, 'download_for_analysis', side_effect=pipeline.VideoDownloadError(['hỏng'])), \
             mock.patch.object(pipeline, 'script_engine', return_value='gemini'), \
             mock.patch.object(gemini_video_script, 'generate_script', return_value=result) as gen:
            res = self._post({'video_url': 'https://www.xiaohongshu.com/explore/1', 'title': 'T'})
        self.assertEqual(res.data['status'], 'DONE')
        self.assertEqual(res.data['source'], 'gemini_text')
        gen.assert_called_once_with(None, 'T', '', audio_seconds=0)

    def test_gemini_loi_tra_502_va_van_don_file(self, _throttle):
        video = _video()
        with mock.patch.object(pipeline, 'download_for_analysis', return_value=video), \
             mock.patch.object(pipeline, 'script_engine', return_value='gemini'), \
             mock.patch.object(gemini_video_script, 'generate_script', side_effect=RuntimeError('quota')), \
             mock.patch.object(pipeline, 'cleanup_video') as cleanup:
            res = self._post({'video_url': 'https://www.douyin.com/video/1'})
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.data['status'], 'FAILED')
        cleanup.assert_called_once_with(video)


class GeminiPromptTests(SimpleTestCase):
    def test_prompt_dung_dinh_dang_json_mau(self):
        text = gemini_video_script.VIDEO_PROMPT.format(title='Nhẫn', description='Mô tả')
        self.assertIn('Tiêu đề gốc: Nhẫn', text)
        self.assertIn('{"has_voice": true/false', text)
        self.assertIn('"script": {"hook"', text)
        self.assertIn('"script": {"hook"', gemini_video_script.TEXT_PROMPT.format(title='a', description='b'))

    def test_boc_json_ke_ca_khi_boc_trong_code_fence(self):
        self.assertEqual(gemini_video_script._parse_json('```json\n{"a": 1}\n```'), {'a': 1})

    def test_ghep_kich_ban_3_phan(self):
        text = gemini_video_script.format_script({'hook': 'Mở', 'body': 'Thân', 'cta': 'Mua ngay'})
        self.assertEqual(text, '【HOOK】\nMở\n\n【NỘI DUNG】\nThân\n\n【KÊU GỌI HÀNH ĐỘNG】\nMua ngay')

    def test_model_mac_dinh_la_flash_lite_va_khong_theo_GEMINI_MODEL(self):
        from django.test import override_settings
        with override_settings(VIDEO_SCRIPT_GEMINI_MODEL='', GEMINI_MODEL='gemini-3.8-flash'), \
             mock.patch.dict('os.environ', {'VIDEO_SCRIPT_GEMINI_MODEL': '', 'GEMINI_MODEL': 'gemini-3.8-flash'}):
            self.assertEqual(gemini_video_script._model_name(), 'gemini-3.1-flash-lite')
        with override_settings(VIDEO_SCRIPT_GEMINI_MODEL='gemini-3.5-flash'):
            self.assertEqual(gemini_video_script._model_name(), 'gemini-3.5-flash')

    def test_ten_model_khong_ton_tai_thi_dung_flash_lite_moi_nhat(self):
        # "gemini-3.1-flash" không tồn tại — thông báo lỗi nguyên văn từ SDK (đo thật 2026-09-28);
        # phải rơi về flash-lite, không được âm thầm sang model đắt hơn
        calls = []
        class FakeModel:
            def __init__(self, name, generation_config=None):
                self.name = name
            def generate_content(self, contents, request_options=None):
                calls.append(self.name)
                if self.name == 'gemini-3.1-flash':
                    raise Exception('404 models/gemini-3.1-flash is not found for API version v1beta, or is not supported for generateContent.')
                return 'ok'
        with mock.patch.object(gemini_video_script, '_model_name', return_value='gemini-3.1-flash'):
            gemini_video_script._generate(SimpleNamespace(GenerativeModel=FakeModel), 'x', 10)
        self.assertEqual(calls, ['gemini-3.1-flash', 'gemini-flash-lite-latest'])

    def test_model_bi_ngung_thi_thu_lai_voi_flash_latest(self):
        calls = []
        class FakeModel:
            def __init__(self, name, generation_config=None):
                self.name = name
            def generate_content(self, contents, request_options=None):
                calls.append(self.name)
                if self.name == 'gemini-2.0-flash':
                    raise Exception('404 This model models/gemini-2.0-flash is no longer available.')
                return 'ok'
        genai = SimpleNamespace(GenerativeModel=FakeModel)
        with mock.patch.object(gemini_video_script, '_model_name', return_value='gemini-2.0-flash'):
            self.assertEqual(gemini_video_script._generate(genai, 'x', 10), 'ok')
        self.assertEqual(calls, ['gemini-2.0-flash', gemini_video_script.FALLBACK_MODEL])

    def test_vuot_han_muc_429_cho_roi_thu_lai_mot_lan(self):
        attempts = []
        class FakeModel:
            def __init__(self, name, generation_config=None): pass
            def generate_content(self, contents, request_options=None):
                attempts.append(1)
                if len(attempts) == 1:
                    raise Exception('429 You exceeded your current quota ... Please retry in 12.5s.')
                return 'ok'
        with mock.patch.object(gemini_video_script.time, 'sleep') as sleep:
            self.assertEqual(gemini_video_script._generate(SimpleNamespace(GenerativeModel=FakeModel), 'x', 10), 'ok')
        sleep.assert_called_once_with(13.5)
        self.assertEqual(len(attempts), 2)

    def _flaky_model(self, errors):
        attempts = []
        class FakeModel:
            def __init__(self, name, generation_config=None): pass
            def generate_content(self, contents, request_options=None):
                attempts.append(request_options)
                if len(attempts) <= len(errors):
                    raise errors[len(attempts) - 1]
                return 'ok'
        return SimpleNamespace(GenerativeModel=FakeModel), attempts

    def test_loi_tam_thoi_504_503_500_thu_lai_ngay_mot_lan(self):
        # Lỗi thật khi test toàn luồng (2026-09-28): một lượt treo 180s rồi "504 Deadline Exceeded"
        from google.api_core import exceptions as gexc
        for err in (gexc.DeadlineExceeded('Deadline Exceeded'), gexc.ServiceUnavailable('overloaded'),
                    gexc.InternalServerError('An internal error has occurred'), Exception('503 The model is overloaded')):
            genai, attempts = self._flaky_model([err])
            with mock.patch.object(gemini_video_script.time, 'sleep') as sleep:
                self.assertEqual(gemini_video_script._generate(genai, 'x', 10), 'ok')
            sleep.assert_not_called()  # lỗi tạm thời: thử lại ngay, không chờ
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[1], {'timeout': 10})  # lượt thử lại vẫn có giới hạn thời gian

    def test_loi_tam_thoi_lap_lai_thi_bao_loi_sau_dung_hai_luot(self):
        from google.api_core import exceptions as gexc
        genai, attempts = self._flaky_model([gexc.DeadlineExceeded('x'), gexc.DeadlineExceeded('y')])
        with self.assertRaises(gexc.DeadlineExceeded):
            gemini_video_script._generate(genai, 'x', 10)
        self.assertEqual(len(attempts), 2)

    def test_loi_do_du_lieu_400_khong_thu_lai(self):
        from google.api_core import exceptions as gexc
        genai, attempts = self._flaky_model([gexc.InvalidArgument('Request contains an invalid argument.')])
        with self.assertRaises(gexc.InvalidArgument):
            gemini_video_script._generate(genai, 'x', 10)
        self.assertEqual(len(attempts), 1)

    def test_429_bat_cho_qua_lau_thi_bao_loi_ngay(self):
        class FakeModel:
            def __init__(self, name, generation_config=None): pass
            def generate_content(self, contents, request_options=None):
                raise Exception('429 quota exceeded. Please retry in 3600s.')
        with mock.patch.object(gemini_video_script.time, 'sleep') as sleep, self.assertRaises(gemini_video_script.GeminiQuotaExceeded):
            gemini_video_script._generate(SimpleNamespace(GenerativeModel=FakeModel), 'x', 10)
        sleep.assert_not_called()

    def test_het_han_muc_theo_ngay_khong_cho_va_bao_ro_tieng_viet(self):
        # Rút gọn từ lỗi thật (2026-09-28): gói miễn phí 20 lượt/ngày/model.
        err = ('429 You exceeded your current quota ... Quota exceeded for metric: '
               'generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20 ... '
               'quota_id: "GenerateRequestsPerDayPerProjectPerModel-FreeTier" ... Please retry in 58.2s.')
        class FakeModel:
            def __init__(self, name, generation_config=None): pass
            def generate_content(self, contents, request_options=None):
                raise Exception(err)
        with mock.patch.object(gemini_video_script.time, 'sleep') as sleep:
            with self.assertRaises(gemini_video_script.GeminiQuotaExceeded) as ctx:
                gemini_video_script._generate(SimpleNamespace(GenerativeModel=FakeModel), 'x', 10)
        sleep.assert_not_called()
        self.assertIn('hết hạn mức trong ngày (gói miễn phí)', str(ctx.exception))
        self.assertIn('billing', str(ctx.exception))

    def test_loi_khac_khong_thu_lai(self):
        class FakeModel:
            def __init__(self, name, generation_config=None): pass
            def generate_content(self, contents, request_options=None):
                raise Exception('500 Internal error')
        with mock.patch.object(gemini_video_script.time, 'sleep') as sleep, self.assertRaises(Exception):
            gemini_video_script._generate(SimpleNamespace(GenerativeModel=FakeModel), 'x', 10)
        sleep.assert_not_called()

    def test_engine_tu_bat_khi_co_khoa_gemini(self):
        from django.test import override_settings
        with override_settings(VIDEO_SCRIPT_ENGINE='', GEMINI_API_KEY='k'), mock.patch.dict('os.environ', {'VIDEO_SCRIPT_ENGINE': ''}):
            self.assertEqual(pipeline.script_engine(), 'gemini')

    def test_khong_co_khoa_thi_tat(self):
        from django.test import override_settings
        with override_settings(VIDEO_SCRIPT_ENGINE='', GEMINI_API_KEY=''), \
             mock.patch.dict('os.environ', {'VIDEO_SCRIPT_ENGINE': '', 'GEMINI_API_KEY': ''}):
            self.assertEqual(pipeline.script_engine(), 'off')

    def test_dung_chung_GEMINI_API_KEY_model_rieng(self):
        # Khoá chung cả AI service; tính năng này chỉ tách MODEL (không theo GEMINI_MODEL của ảnh thẻ)
        from django.test import override_settings
        with override_settings(GEMINI_API_KEY='shared-key', GEMINI_MODEL='gemini-3.1-flash-image', VIDEO_SCRIPT_GEMINI_MODEL=''), \
             mock.patch.dict('os.environ', {'VIDEO_SCRIPT_GEMINI_MODEL': ''}):
            self.assertEqual(gemini_video_script._api_key(), 'shared-key')
            self.assertEqual(gemini_video_script._model_name(), 'gemini-3.1-flash-lite')
        with override_settings(GEMINI_API_KEY=''), mock.patch.dict('os.environ', {'GEMINI_API_KEY': ''}):
            with self.assertRaisesMessage(gemini_video_script.GeminiNotConfigured, 'GEMINI_API_KEY'):
                gemini_video_script._api_key()

    def test_ly_do_tat_chi_bao_khi_thieu_khoa(self):
        # thiếu khoá = cấu hình sai → BE cần biết để hiện cho người dùng; cố ý tắt thì không phải lỗi
        from django.test import override_settings
        with override_settings(VIDEO_SCRIPT_ENGINE='', GEMINI_API_KEY=''), \
             mock.patch.dict('os.environ', {'VIDEO_SCRIPT_ENGINE': '', 'GEMINI_API_KEY': ''}):
            self.assertIn('GEMINI_API_KEY', pipeline.engine_disabled_reason())
        with override_settings(VIDEO_SCRIPT_ENGINE='off', GEMINI_API_KEY='k'):
            self.assertEqual(pipeline.engine_disabled_reason(), '')

    def test_dat_off_thi_tat_du_co_khoa(self):
        from django.test import override_settings
        with override_settings(VIDEO_SCRIPT_ENGINE='off', GEMINI_API_KEY='k'):
            self.assertEqual(pipeline.script_engine(), 'off')

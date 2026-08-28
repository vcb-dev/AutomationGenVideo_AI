"""Bộ test gộp cho toàn bộ `PaastAnalysisService` + view PAAST — trước đây rải ra 6 file
test_paast_*.py riêng lẻ (mỗi lần sửa 1 khía cạnh lại tạo file mới), nay gom về 1 nơi cho dễ tra
cứu, mỗi khía cạnh vẫn giữ nguyên 1 TestCase riêng để không lẫn lộn:

- PreferSelectionTests        — `_select_prefer_statuses` (chọn primary/secondary/off theo level)
- ClassifyGroupRetryTests     — `_classify_group` tự thử lại tại chỗ khi lỗi ngẫu nhiên
- ScoringTests                — `compute_scores`/`compute_verdict` (mô hình chấm GRADED patch v3)
- WriteUpgradeContentRetryTests — `_write_upgrade_content` tự thử lại tại chỗ
- TransparencyOverrideTests   — `_normalize_classification` không phạt oan tiêu chí TRANSPARENCY
- NoMaxLengthGateTests        — view PAAST không tự chặn content > 3000 ký tự

Chạy: python manage.py test tests.test_paast
"""

import time
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from video_management.services.content_generation_service import DeepSeekError
from video_management.services.paast_analysis_service import (
    LEVEL_PASS_THRESHOLD,
    MAX_GROUP_ATTEMPTS,
    MAX_SCRIPTED_WRITE_ATTEMPTS,
    PREFER_PRIMARY_LEVEL_THRESHOLD,
    PREFER_SECONDARY_LEVEL_THRESHOLD,
    PaastAnalysisService,
)
from video_management.views import paast_analysis_views


# ---------------------------------------------------------------------------
# PreferSelectionTests — `_select_prefer_statuses` chọn primary/secondary/off CHO Prefer THUẦN
# theo level (0-5) đã chấm, không phó thác cho LLM tự gán nhãn (patch v3, business §Q1: giữ cơ
# chế "1 chủ + 1 phụ + coherence gate" của v2.1 nhưng chọn bằng điểm số thay vì lý luận định tính).
# ---------------------------------------------------------------------------

def _levels(**kwargs):
    """Mặc định level=0 cho code nào không truyền — vd _levels(R=5, E=3)."""
    base = {"C": 0, "R": 0, "A": 0, "V": 0, "E": 0, "S": 0}
    base.update(kwargs)
    return base


class PreferSelectionTests(SimpleTestCase):
    def test_chon_dung_1_primary_va_1_secondary_ro_rang(self):
        statuses = PaastAnalysisService._select_prefer_statuses(_levels(C=2, R=5, A=1, E=3, S=1))
        self.assertEqual(statuses["R"], "primary")
        self.assertEqual(statuses["E"], "secondary")
        self.assertEqual(statuses["C"], "off")
        self.assertEqual(statuses["A"], "off")
        self.assertEqual(statuses["S"], "off")

    def test_dong_hang_cao_nhat_gan_ca_hai_primary(self):
        """2 insight cùng level cao nhất (đạt ngưỡng primary) -> cả 2 đều "primary", để tầng gọi
        (_normalize_classification) tự phát hiện primary_count > 1 và ép coherence=false."""
        statuses = PaastAnalysisService._select_prefer_statuses(_levels(C=5, R=5, A=1))
        self.assertEqual(statuses["C"], "primary")
        self.assertEqual(statuses["R"], "primary")
        primary_count = sum(1 for s in statuses.values() if s == "primary")
        self.assertEqual(primary_count, 2)

    def test_khong_insight_nao_du_nguong_thi_khong_co_primary(self):
        """Level cao nhất vẫn dưới PREFER_PRIMARY_LEVEL_THRESHOLD -> tất cả off, không ép content
        yếu phải có 1 primary miễn cưỡng."""
        levels = _levels(C=2, R=1, E=2)
        self.assertLess(max(levels.values()), PREFER_PRIMARY_LEVEL_THRESHOLD)
        statuses = PaastAnalysisService._select_prefer_statuses(levels)
        self.assertTrue(all(s == "off" for s in statuses.values()))

    def test_co_primary_nhung_khong_co_secondary_neu_hang_nhi_qua_yeu(self):
        levels = _levels(C=5, R=1, E=1)
        self.assertLess(max(levels["R"], levels["E"]), PREFER_SECONDARY_LEVEL_THRESHOLD)
        statuses = PaastAnalysisService._select_prefer_statuses(levels)
        self.assertEqual(statuses["C"], "primary")
        self.assertNotIn("secondary", statuses.values())

    def test_dong_hang_o_vi_tri_phu_tro_thi_bo_trong_secondary(self):
        """Đồng hạng thứ nhì (cả 2 cùng đạt ngưỡng secondary) KHÔNG phải lỗi hội tụ như đồng hạng
        primary — chỉ đơn giản bỏ trống secondary để tránh chọn tuỳ tiện giữa 2 lựa chọn ngang nhau."""
        statuses = PaastAnalysisService._select_prefer_statuses(_levels(C=5, R=3, A=3))
        self.assertEqual(statuses["C"], "primary")
        self.assertEqual(statuses["R"], "off")
        self.assertEqual(statuses["A"], "off")
        self.assertNotIn("secondary", statuses.values())

    def test_toan_bo_level_0_khong_crash_tat_ca_off(self):
        statuses = PaastAnalysisService._select_prefer_statuses(_levels())
        self.assertTrue(all(s == "off" for s in statuses.values()))

    def test_dung_dung_nguong_primary_van_duoc_chon(self):
        """Level ĐÚNG BẰNG ngưỡng (không chỉ cao hơn) vẫn đủ tư cách làm primary."""
        statuses = PaastAnalysisService._select_prefer_statuses(_levels(V=PREFER_PRIMARY_LEVEL_THRESHOLD))
        self.assertEqual(statuses["V"], "primary")

    def test_dung_dung_nguong_secondary_van_duoc_chon(self):
        statuses = PaastAnalysisService._select_prefer_statuses(
            _levels(V=5, S=PREFER_SECONDARY_LEVEL_THRESHOLD)
        )
        self.assertEqual(statuses["V"], "primary")
        self.assertEqual(statuses["S"], "secondary")


# ---------------------------------------------------------------------------
# ClassifyGroupRetryTests — `_classify_group` phải tự thử lại TẠI CHỖ tối đa MAX_GROUP_ATTEMPTS
# lượt, đúng nhóm hỏng, thay vì để BE chạy lại cả 5 nhóm phân tích PAAST (2c8c744).
#
# Retry ở BE (tầng gọi HTTP) vứt bỏ cả 4 nhóm vừa thành công rồi tung lại xúc xắc 5 mặt — lỗi ở
# đây thường là lỗi ngẫu nhiên của riêng 1 nhóm (429, JSON hỏng), thử lại đúng nhóm đó rẻ hơn
# nhiều. Test này khoá: lỗi ngẫu nhiên (timeout/429/5xx/mạng/parse JSON) phải thử lại; lỗi tất
# định (client/no_key) phải NÉM NGAY không tốn lượt thử; và sau khi thử hết MAX_GROUP_ATTEMPTS
# lượt, phải báo lỗi cuối cùng thay vì lỗi đầu tiên.
# ---------------------------------------------------------------------------

_GROUP_ITEMS = [{'code': 'STOP', 'name_en': 'Stop', 'name_vi': 'Dừng lại', 'signal': 'hook mạnh'}]
_GROUP_ARGS = dict(
    content='nội dung mẫu',
    group_key='action',
    group_label='Action',
    items=_GROUP_ITEMS,
    status_options='pass|miss',
    max_tokens=2048,
)


def _far_deadline():
    return time.monotonic() + 120  # dư ngân sách, không chạm nhánh "hết thời gian"


class ClassifyGroupRetryTests(SimpleTestCase):
    def setUp(self):
        self.service = PaastAnalysisService()

    def test_loi_ngau_nhien_thu_lai_roi_thanh_cong_khong_nem_len_tren(self):
        """timeout ở lượt 1, thành công ở lượt 2 — kết quả phải là kết quả THÀNH CÔNG, không
        phải lỗi của lượt 1."""
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call, \
                patch.object(self.service._gen, '_extract_json_dict') as mock_extract:
            mock_call.side_effect = [
                DeepSeekError('timeout', 'DeepSeek không trả lời trong 45s'),
                '{"action": [{"code": "STOP", "status": "pass", "evidence": "..."}]}',
            ]
            mock_extract.return_value = {'action': [{'code': 'STOP', 'status': 'pass', 'evidence': '...'}]}

            result = self.service._classify_group(deadline=_far_deadline(), **_GROUP_ARGS)

        self.assertEqual(result, [{'code': 'STOP', 'status': 'pass', 'evidence': '...'}])
        self.assertEqual(mock_call.call_count, 2)

    def test_loi_client_nem_ngay_khong_thu_lai(self):
        """4xx (request sai)/thiếu API key là lỗi TẤT ĐỊNH — thử lại chỉ tốn ngân sách của 4
        nhóm còn lại mà kết quả không đổi."""
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            mock_call.side_effect = DeepSeekError('client', 'DeepSeek từ chối request (400)', 400)

            with self.assertRaises(RuntimeError):
                self.service._classify_group(deadline=_far_deadline(), **_GROUP_ARGS)

        self.assertEqual(mock_call.call_count, 1)

    def test_loi_no_key_nem_ngay_khong_thu_lai(self):
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            mock_call.side_effect = DeepSeekError('no_key', 'Chưa cấu hình DEEPSEEK_API_KEY')

            with self.assertRaises(RuntimeError):
                self.service._classify_group(deadline=_far_deadline(), **_GROUP_ARGS)

        self.assertEqual(mock_call.call_count, 1)

    def test_json_hong_khong_tat_dinh_duoc_thu_lai(self):
        """LLM trả text không phải JSON hợp lệ — lỗi ngẫu nhiên (lượt sau thường ổn), phải
        được coi như lỗi retriable, không phải lỗi tất định."""
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call, \
                patch.object(self.service._gen, '_extract_json_dict') as mock_extract:
            mock_call.side_effect = ['not json trước', '{"action": [...]}']
            mock_extract.side_effect = [{}, {'action': [{'code': 'STOP', 'status': 'pass', 'evidence': 'e'}]}]

            result = self.service._classify_group(deadline=_far_deadline(), **_GROUP_ARGS)

        self.assertEqual(result, [{'code': 'STOP', 'status': 'pass', 'evidence': 'e'}])
        self.assertEqual(mock_call.call_count, 2)

    def test_het_MAX_GROUP_ATTEMPTS_luot_thi_nem_loi_CUOI_CUNG(self):
        """Ưu tiên lỗi gần nhất — dễ chẩn đoán hơn lỗi của lượt đầu tiên đã lỗi thời."""
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            mock_call.side_effect = [
                DeepSeekError('timeout', 'lỗi lượt 1'),
                DeepSeekError('server', 'lỗi lượt 2'),
                DeepSeekError('network', 'lỗi lượt cuối'),
            ]

            with self.assertRaises(RuntimeError) as ctx:
                self.service._classify_group(deadline=_far_deadline(), **_GROUP_ARGS)

        self.assertEqual(mock_call.call_count, MAX_GROUP_ATTEMPTS)
        self.assertIn('lỗi lượt cuối', str(ctx.exception))
        self.assertNotIn('lỗi lượt 1', str(ctx.exception))

    def test_khong_con_ngan_sach_thi_dung_ngay_khong_goi_them(self):
        """Deadline đã qua trước cả lượt đầu — không được cố gọi thêm 1 lượt vô ích."""
        past_deadline = time.monotonic() - 1

        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            with self.assertRaises(RuntimeError) as ctx:
                self.service._classify_group(deadline=past_deadline, **_GROUP_ARGS)

        mock_call.assert_not_called()
        self.assertIn('hết ngân sách thời gian', str(ctx.exception))


# ---------------------------------------------------------------------------
# ScoringTests — `compute_scores`/`compute_verdict` đối chiếu với mô hình chấm GRADED patch v3
# (thang level 0-5/tiêu chí, thay pass/miss nhị phân của patch v2.1).
#
# Trọng số 5 lớp GIỮ NGUYÊN v2.1 (25/25/20/15/15) và Prefer GIỮ NGUYÊN cơ chế "1 chủ + 1 phụ +
# coherence hard-gate" — chỉ đổi CÁCH tính điểm bên trong mỗi lớp/khe từ đếm pass sang tổng level.
#
# Case F là case QUAN TRỌNG NHẤT của bộ test này: minh chứng bằng số cho lý do đổi công thức — 1
# content mà mọi tiêu chí đều "vừa đạt" (level=3/5, tức status=pass ở cả 2 mô hình) giờ chỉ được
# 60% điểm mỗi lớp, KHÔNG còn tự động full điểm như hệ pass/miss cũ (vốn coi "Khá" và "Xuất sắc"
# là cùng 1 kết quả "pass" — đúng lỗi "có nhưng yếu ≠ điểm cao" mà patch v3 sửa).
# ---------------------------------------------------------------------------

def _prefer(primary_count=0, primary_level=0, secondary_count=0, secondary_level=0, is_coherent=True):
    insights = []
    if primary_count >= 1:
        insights.append({"status": "primary", "level": primary_level})
    if secondary_count >= 1:
        insights.append({"status": "secondary", "level": secondary_level})
    return {
        "primary_count": primary_count,
        "secondary_count": secondary_count,
        "coherence": {"is_coherent": is_coherent},
        "insights": insights,
    }


def _criteria(levels):
    """levels: list các level 0-5, mỗi phần tử ra 1 tiêu chí — status suy ra như normalize_criteria_group thật."""
    return [{"status": "pass" if lv >= 3 else "miss", "level": lv} for lv in levels]


def _uniform(level, n=6):
    return _criteria([level] * n)


def _stick(level_a=0, level_b=0):
    text = _criteria([level_a, level_b])
    na = [{"status": "na", "level": None}] * 4
    return text + na


# (name, prefer(primary_count, primary_level, secondary_count, secondary_level, is_coherent),
#  action_levels(6), acknowledge_levels(6), stick_levels(2), trust_levels(6),
#  expected_total, expected_band)
_SCORING_CASES = [
    (
        "Case A — mọi tiêu chí Xuất sắc (level=5): full điểm 100",
        (1, 5, 1, 5, True), [5]*6, [5]*6, (5, 5), [5]*6, 100, "ready",
    ),
    (
        "Case B — chỉ có chủ đạo Xuất sắc, không có phụ trợ, 4 lớp khác full",
        (1, 5, 0, 0, True), [5]*6, [5]*6, (5, 5), [5]*6, 88, "close",
    ),
    (
        "Case C — coherence=false chặn Prefer về 0/25 dù primary/secondary đều level=5",
        (1, 5, 1, 5, False), [5]*6, [5]*6, (5, 5), [5]*6, 75, "close",
    ),
    (
        "Case D — 2 primary cùng lúc (chưa hội tụ) vẫn phải bị chặn 0/25",
        (2, 5, 0, 0, False), [5]*6, [5]*6, (5, 5), [5]*6, 75, "close",
    ),
    (
        "Case E — Prefer đạt full nhưng 4 lớp khác rỗng hoàn toàn (level=0)",
        (1, 5, 1, 5, True), [0]*6, [0]*6, (0, 0), [0]*6, 25, "not-ready",
    ),
    (
        "Case F — QUAN TRỌNG: mọi tiêu chí chỉ 'Khá' (level=3, vừa đạt ngưỡng pass) → 45/100, "
        "KHÔNG full điểm dù tất cả đều status=pass (đây là điểm khác biệt cốt lõi với hệ pass/miss cũ)",
        (0, 0, 0, 0, True), [3]*6, [3]*6, (3, 3), [3]*6, 45, "not-ready",
    ),
    (
        "Case G — level không đồng đều trong 1 lớp (nửa Xuất sắc, nửa Không có) vẫn cộng đúng theo tỷ lệ",
        (0, 0, 0, 0, True), [5, 5, 5, 0, 0, 0], [5]*6, (5, 5), [5]*6, 62, "needs-work",
    ),
    (
        "Case H — chủ đạo chỉ ở mức Khá (level=3, vẫn đủ ngưỡng để được CHỌN làm primary) không "
        "còn ăn trọn 12.5đ như công thức v2.1 cũ — chỉ còn 7.5đ",
        (1, 3, 0, 0, True), [0]*6, [0]*6, (0, 0), [0]*6, 8, "not-ready",
    ),
]

# (name, prefer, expect_passed) — compute_verdict: Prefer chỉ "đạt" khi primary_count==1 VÀ
# is_coherent==true (§3.1/§6) — không đổi so với v2.1, chỉ CÁCH primary_count được chọn (theo
# level) là đổi ở normalize.
_VERDICT_CASES = [
    ("2 primary (chưa hội tụ) không được coi là Prefer đạt", _prefer(2, 5, 0, 0, False), False),
    ("1 primary + coherent → Prefer đạt", _prefer(1, 5, 0, 0, True), True),
    ("0 primary → Prefer chưa đạt", _prefer(0, 0, 1, 5, True), False),
    ("1 primary nhưng coherence=false → Prefer chưa đạt", _prefer(1, 5, 0, 0, False), False),
]


class ScoringTests(SimpleTestCase):
    def test_compute_scores_khop_mo_hinh_cham_GRADED_v3(self):
        for name, prefer_args, action_lv, ack_lv, stick_lv, trust_lv, expected_total, expected_band in _SCORING_CASES:
            with self.subTest(name):
                classification = {
                    "prefer": _prefer(*prefer_args),
                    "action": _criteria(action_lv),
                    "acknowledge": _criteria(ack_lv),
                    "stick": _stick(*stick_lv),
                    "trust": _criteria(trust_lv),
                }
                result = PaastAnalysisService.compute_scores(classification)
                self.assertEqual(result["total_score"], expected_total, name)
                self.assertEqual(result["band"], expected_band, name)

    def test_compute_verdict_prefer_passed_layers(self):
        for name, prefer, expect_passed in _VERDICT_CASES:
            with self.subTest(name):
                classification = {
                    "prefer": prefer,
                    "action": _uniform(5), "acknowledge": _uniform(5), "stick": _stick(5, 5), "trust": _uniform(5),
                }
                scores = PaastAnalysisService.compute_scores(classification)
                verdict = PaastAnalysisService.compute_verdict(scores)
                self.assertEqual("prefer" in verdict["passed_layers"], expect_passed, name)


# ---------------------------------------------------------------------------
# WriteUpgradeContentRetryTests — `_write_upgrade_content` phải tự thử lại TẠI CHỖ tối đa
# MAX_SCRIPTED_WRITE_ATTEMPTS lượt, giống hệt nguyên tắc của `_classify_group`/
# `_write_scripted_upgrade` — thay cho `_call_deepseek_raw` cũ (1 lượt DUY NHẤT, nuốt mọi lỗi
# thành None, không thử lại).
#
# Bối cảnh: đối chiếu bảng `paast_analysis_histories` thực tế cho thấy 100% lượt "Nâng cấp" từ
# task-auto hỏng trong nhiều ngày, luôn dừng ở ~40s (đúng bằng write_budget cũ khi BE không gửi
# timeout_seconds) với lỗi "DeepSeek không phản hồi" — timeout thật của 1 lệnh reasoning-enabled +
# max_tokens=16000, không có lượt thử lại nào nên hỏng là hỏng hẳn. Sửa 2 phía: BE tăng timeout
# (xem paast.service.ts) VÀ ở đây thêm thử lại cho lỗi ngẫu nhiên (timeout/429/5xx/JSON hỏng).
# ---------------------------------------------------------------------------

def _far_deadline_budget():
    return 120  # dư ngân sách, không chạm nhánh "hết thời gian"


class WriteUpgradeContentRetryTests(SimpleTestCase):
    def setUp(self):
        self.service = PaastAnalysisService()

    def test_timeout_thu_lai_roi_thanh_cong_tra_ve_dict_da_parse(self):
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call, \
                patch.object(self.service._gen, '_extract_json_dict') as mock_extract:
            mock_call.side_effect = [
                DeepSeekError('timeout', 'DeepSeek không trả lời trong 40s'),
                '{"upgraded_content": "noi dung moi", "changes_added": []}',
            ]
            mock_extract.return_value = {'upgraded_content': 'noi dung moi', 'changes_added': []}

            result = self.service._write_upgrade_content('prompt', 'system', _far_deadline_budget())

        self.assertEqual(result, {'upgraded_content': 'noi dung moi', 'changes_added': []})
        self.assertEqual(mock_call.call_count, 2)

    def test_loi_client_nem_ngay_khong_thu_lai(self):
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            mock_call.side_effect = DeepSeekError('client', 'DeepSeek từ chối request (400)', 400)

            with self.assertRaises(RuntimeError):
                self.service._write_upgrade_content('prompt', 'system', _far_deadline_budget())

        self.assertEqual(mock_call.call_count, 1)

    def test_loi_no_key_nem_ngay_khong_thu_lai(self):
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            mock_call.side_effect = DeepSeekError('no_key', 'Chưa cấu hình DEEPSEEK_API_KEY')

            with self.assertRaises(RuntimeError):
                self.service._write_upgrade_content('prompt', 'system', _far_deadline_budget())

        self.assertEqual(mock_call.call_count, 1)

    def test_json_hong_khong_tat_dinh_duoc_thu_lai(self):
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call, \
                patch.object(self.service._gen, '_extract_json_dict') as mock_extract:
            mock_call.side_effect = ['not json truoc', '{"upgraded_content": "ok"}']
            mock_extract.side_effect = [{}, {'upgraded_content': 'ok'}]

            result = self.service._write_upgrade_content('prompt', 'system', _far_deadline_budget())

        self.assertEqual(result, {'upgraded_content': 'ok'})
        self.assertEqual(mock_call.call_count, 2)

    def test_thieu_upgraded_content_trong_json_cung_duoc_thu_lai(self):
        """JSON hợp lệ về mặt cú pháp nhưng thiếu đúng field cần dùng (`upgraded_content`) vẫn
        phải coi là lượt hỏng, thử lại — không trả về dict rỗng cho caller."""
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call, \
                patch.object(self.service._gen, '_extract_json_dict') as mock_extract:
            mock_call.side_effect = ['{"changes_added": []}', '{"upgraded_content": "ok", "changes_added": []}']
            mock_extract.side_effect = [
                {'changes_added': []},
                {'upgraded_content': 'ok', 'changes_added': []},
            ]

            result = self.service._write_upgrade_content('prompt', 'system', _far_deadline_budget())

        self.assertEqual(result['upgraded_content'], 'ok')
        self.assertEqual(mock_call.call_count, 2)

    def test_het_MAX_SCRIPTED_WRITE_ATTEMPTS_luot_thi_nem_loi(self):
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            mock_call.side_effect = [
                DeepSeekError('timeout', 'lỗi lượt 1'),
                DeepSeekError('server', 'lỗi lượt 2'),
                DeepSeekError('network', 'lỗi lượt cuối'),
            ]

            with self.assertRaises(RuntimeError) as ctx:
                self.service._write_upgrade_content('prompt', 'system', _far_deadline_budget())

        self.assertEqual(mock_call.call_count, MAX_SCRIPTED_WRITE_ATTEMPTS)
        self.assertIn('lỗi lượt cuối', str(ctx.exception))

    def test_khong_con_ngan_sach_thi_dung_ngay_khong_goi_them(self):
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            with self.assertRaises(RuntimeError) as ctx:
                self.service._write_upgrade_content('prompt', 'system', budget_s=-1)

        mock_call.assert_not_called()
        self.assertIn('hết ngân sách thời gian', str(ctx.exception))


# ---------------------------------------------------------------------------
# TransparencyOverrideTests — `_normalize_classification` không được phạt oan tiêu chí
# TRANSPARENCY (Trust) khi kịch bản chỉ có câu chữ. Transparency chủ yếu thể hiện qua hình ảnh/
# hậu trường khi lên video (cảnh quay xưởng, quy trình thật...) nên không thể đòi hỏi một kịch
# bản chữ thuần phải tự chứng minh điều này — model chấm level thấp (hoặc không trả) tiêu chí này
# phải bị ép lên đúng LEVEL_PASS_THRESHOLD ngay tại _normalize_classification, không phó thác
# hoàn toàn cho prompt tự giác (cùng nguyên tắc override coherence khi primary_count > 1). Các
# tiêu chí Trust khác, và trường hợp model tìm được bằng chứng thật (level đã ≥ ngưỡng), không bị
# ảnh hưởng.
# ---------------------------------------------------------------------------

def _raw_trust(transparency_level=None, transparency_evidence="", transparency_reasoning="", other_level=0):
    items = [{
        "code": "TRANSPARENCY",
        "level": transparency_level,
        "evidence": transparency_evidence,
        "reasoning": transparency_reasoning,
    }]
    for code in ("RESPONSIBILITY", "UNBIASED_AUTHORITY", "SOCIAL_PROOF", "TANGIBLE_EVIDENCE",
                 "STORYTELLING_HUMAN_TOUCH"):
        items.append({"code": code, "level": other_level, "evidence": "", "reasoning": ""})
    return items


class TransparencyOverrideTests(SimpleTestCase):
    def setUp(self):
        self.service = PaastAnalysisService()

    def _normalize(self, trust_items):
        raw = {
            "prefer": [], "prefer_meta": {},
            "action": [], "acknowledge": [], "stick": [],
            "trust": trust_items,
        }
        return self.service._normalize_classification(raw)

    def test_transparency_level_thap_bi_ep_len_nguong_pass(self):
        result = self._normalize(_raw_trust(transparency_level=0))
        transparency = next(c for c in result["trust"] if c["code"] == "TRANSPARENCY")
        self.assertEqual(transparency["level"], LEVEL_PASS_THRESHOLD)
        self.assertEqual(transparency["status"], "pass")
        self.assertTrue(transparency["evidence"])

    def test_transparency_khong_tra_level_hop_le_cung_bi_ep_len_nguong(self):
        """Model không trả level hợp lệ (thiếu/None) -> _parse_level mặc định 0, override vẫn
        phải ép lên đúng LEVEL_PASS_THRESHOLD chứ không giữ nguyên 0."""
        result = self._normalize(_raw_trust(transparency_level=None))
        transparency = next(c for c in result["trust"] if c["code"] == "TRANSPARENCY")
        self.assertEqual(transparency["level"], LEVEL_PASS_THRESHOLD)
        self.assertEqual(transparency["status"], "pass")

    def test_transparency_khong_bi_ep_vuot_qua_nguong_len_muc_toi_da(self):
        """Override là MIỄN TRỪ Ở MỨC ĐẠT, không phải xác nhận triển khai xuất sắc — level bị ép
        đúng bằng LEVEL_PASS_THRESHOLD, không nhảy thẳng lên 5."""
        result = self._normalize(_raw_trust(transparency_level=1))
        transparency = next(c for c in result["trust"] if c["code"] == "TRANSPARENCY")
        self.assertEqual(transparency["level"], LEVEL_PASS_THRESHOLD)

    def test_transparency_level_that_cao_giu_nguyen_evidence_goc(self):
        """Model tìm thấy bằng chứng thật trong text, chấm level ≥ ngưỡng -> giữ nguyên level/
        evidence/reasoning gốc, KHÔNG bị ghi đè bởi câu mặc định của override."""
        real_evidence = "Đây là 3 tháng chúng tôi mài thử, hỏng 12 viên đá mới ra được sản phẩm này."
        real_reasoning = "Chia sẻ thật quá trình thử/hỏng, đúng tinh thần minh bạch."
        items = _raw_trust(
            transparency_level=5,
            transparency_evidence=real_evidence,
            transparency_reasoning=real_reasoning,
        )
        result = self._normalize(items)
        transparency = next(c for c in result["trust"] if c["code"] == "TRANSPARENCY")
        self.assertEqual(transparency["level"], 5)
        self.assertEqual(transparency["status"], "pass")
        self.assertEqual(transparency["evidence"], real_evidence)
        self.assertEqual(transparency["reasoning"], real_reasoning)

    def test_cac_tieu_chi_trust_khac_khong_bi_anh_huong(self):
        """Override CHỈ áp dụng cho TRANSPARENCY — 5 tiêu chí Trust còn lại vẫn miss bình thường,
        không bị ép pass theo."""
        result = self._normalize(_raw_trust(transparency_level=0, other_level=0))
        others = [c for c in result["trust"] if c["code"] != "TRANSPARENCY"]
        self.assertEqual(len(others), 5)
        self.assertTrue(all(c["status"] == "miss" for c in others))
        self.assertTrue(all(c["level"] == 0 for c in others))


# ---------------------------------------------------------------------------
# NoMaxLengthGateTests — Content dài không còn bị chặn cứng 3000 ký tự ở tầng view PAAST.
#
# Trước đây `analyze_content`/`analyze_content_v2` tự chặn content > MAX_CONTENT_LENGTH=3000
# (bb0cd90, e55ac15). Bước viết kịch bản của content-transform chạy với max_tokens=16000 nên
# output vượt 3000 ký tự là chuyện thường — endpoint /rescore truyền thẳng output_text vào đây
# và ăn 400 CHẮC CHẮN xảy ra, trong khi BE vẫn retry đủ 3 lượt rồi thay message thật bằng
# "có thể do timeout" (xem comment ở đầu paast_analysis_views.py). Test này khoá lại: content
# dài bao nhiêu cũng phải đi tới service, không được view tự chặn.
# ---------------------------------------------------------------------------

_LONG_CONTENT = ("Nội dung kịch bản dài. " * 300).strip()  # ~6.900 ký tự, vượt xa mốc 3000 cũ


class NoMaxLengthGateTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def test_analyze_content_khong_chan_content_tren_3000_ky_tu(self):
        self.assertGreater(len(_LONG_CONTENT), 3000)
        request = self.factory.post('/api/ai/paast/analyze/', {'content': _LONG_CONTENT}, format='json')

        with patch.object(paast_analysis_views, 'PaastAnalysisService') as ServiceCls:
            ServiceCls.return_value.analyze.return_value = {'total_score': 80}
            response = paast_analysis_views.analyze_content(request)

        self.assertEqual(response.status_code, 200)
        ServiceCls.return_value.analyze.assert_called_once()
        called_content = ServiceCls.return_value.analyze.call_args.args[0]
        self.assertEqual(len(called_content), len(_LONG_CONTENT))

    def test_analyze_content_v2_khong_chan_content_tren_3000_ky_tu(self):
        request = self.factory.post('/api/ai/paast/analyze-v2/', {'content': _LONG_CONTENT}, format='json')

        with patch.object(paast_analysis_views, 'PaastAnalysisServiceV2') as ServiceCls:
            ServiceCls.return_value.analyze_v2.return_value = {'layers': {}}
            response = paast_analysis_views.analyze_content_v2(request)

        self.assertEqual(response.status_code, 200)
        ServiceCls.return_value.analyze_v2.assert_called_once_with(_LONG_CONTENT)

    def test_analyze_content_van_giu_chan_duoi_MIN_CONTENT_LENGTH(self):
        """Chỉ bỏ trần trên — sàn dưới (100 ký tự) vẫn phải còn, tránh chấm content rỗng/vô nghĩa."""
        request = self.factory.post('/api/ai/paast/analyze/', {'content': 'quá ngắn'}, format='json')

        response = paast_analysis_views.analyze_content(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn('quá ngắn', str(response.data['error']))

    def test_analyze_content_v2_van_giu_chan_duoi_MIN_CONTENT_LENGTH(self):
        request = self.factory.post('/api/ai/paast/analyze-v2/', {'content': 'ngắn'}, format='json')

        response = paast_analysis_views.analyze_content_v2(request)

        self.assertEqual(response.status_code, 400)

    def test_module_khong_con_dinh_nghia_MAX_CONTENT_LENGTH(self):
        """Chốt luôn cả hằng số — tránh ai đó lỡ tay khai báo lại rồi quên gắn vào view."""
        self.assertFalse(hasattr(paast_analysis_views, 'MAX_CONTENT_LENGTH'))

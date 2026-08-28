"""Vòng đời job nền của module content-transform (content_transform_job_views).

Khoá lại KHUNG mà PR2 (transcribe/upgrade start) và BE (pollContentTransformJob +
reconciliation) sẽ dựa vào:

  1. work_fn chạy xong -> status 'completed', payload nằm ở `result`.
  2. work_fn trả {'success': False, 'error_message'} -> status 'error', message giữ nguyên
     (đây là đường mà run_transcribe_upload_core báo lỗi 400/504 — KHÔNG được nuốt thành
     "lỗi không xác định").
  3. work_fn ném exception -> status 'error', không nuốt im.
  4. Huỷ giữa chừng -> status 'cancelled', KẾT QUẢ BỊ BỎ kể cả khi work_fn đã chạy xong
     (client đã thôi chờ — ghi completed lên nữa là hiện kết quả cho lượt đã bị huỷ).
  5. status/cancel cho job_id lạ -> 404 (BE dịch thành "job mất dấu").
  6. cancel job đã ở trạng thái cuối -> no-op, trả nguyên trạng thái.

Chạy: python manage.py test tests.test_content_transform_job_lifecycle
"""
import time

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.views import content_transform_job_views as jobs


def _wait_for(job_id, statuses, timeout=5.0):
    """Chờ tới khi job vào một trong `statuses`, trả data cuối. Fail rõ nếu quá giờ."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = jobs.get_job(job_id)
        if data and data.get("status") in statuses:
            return data
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} không vào {statuses} trong {timeout}s (hiện: {jobs.get_job(job_id)})")


class _Authed:
    """User giả tối thiểu: IsAuthenticated chỉ cần is_authenticated; throttle mặc định
    của DRF lại đọc `.pk` để dựng cache key nên phải có luôn."""
    is_authenticated = True
    is_active = True
    is_staff = False
    pk = "test-user"
    id = "test-user"


class SpawnJobTests(SimpleTestCase):
    def test_work_fn_thanh_cong_thi_completed_va_giu_payload(self):
        job_id = jobs.spawn_job("test", lambda _cc, _hb: {"success": True, "transcript": "xin chào", "char_count": 8})
        data = _wait_for(job_id, {jobs.JOB_COMPLETED, jobs.JOB_ERROR})
        self.assertEqual(data["status"], jobs.JOB_COMPLETED)
        self.assertEqual(data["result"]["transcript"], "xin chào")
        self.assertEqual(data["kind"], "test")

    def test_work_fn_tra_success_false_thi_error_giu_nguyen_message(self):
        job_id = jobs.spawn_job(
            "test",
            lambda _cc, _hb: {"success": False, "error_message": "Thời lượng file quá dài (900 giây > 600 giây).", "status_code": 400},
        )
        data = _wait_for(job_id, {jobs.JOB_COMPLETED, jobs.JOB_ERROR})
        self.assertEqual(data["status"], jobs.JOB_ERROR)
        self.assertEqual(data["message"], "Thời lượng file quá dài (900 giây > 600 giây).")
        self.assertEqual(data["error"], data["message"])
        # Giữ luôn dict gốc để BE lấy status_code nếu cần.
        self.assertEqual(data["result"]["status_code"], 400)

    def test_work_fn_nem_exception_thi_error_khong_nuot_im(self):
        def boom(_cc, _hb):
            raise RuntimeError("Gemini sinh transcript quá lâu")

        job_id = jobs.spawn_job("test", boom)
        data = _wait_for(job_id, {jobs.JOB_COMPLETED, jobs.JOB_ERROR})
        self.assertEqual(data["status"], jobs.JOB_ERROR)
        self.assertIn("Gemini sinh transcript quá lâu", data["message"])

    def test_huy_giua_chung_thi_cancelled_va_bo_ket_qua(self):
        started = {"v": False}

        def slow(check_cancel, _hb):
            started["v"] = True
            for _ in range(200):
                if check_cancel():
                    # work_fn tự thoát sớm — nhưng dù nó chạy hết thì kết quả vẫn phải bị bỏ.
                    return {"success": True, "transcript": "không nên thấy"}
                time.sleep(0.02)
            return {"success": True, "transcript": "không nên thấy"}

        job_id = jobs.spawn_job("test", slow)
        _wait_for(job_id, {jobs.JOB_RUNNING})
        # đợi work_fn thực sự vào vòng lặp rồi mới huỷ
        for _ in range(100):
            if started["v"]:
                break
            time.sleep(0.02)
        jobs._write(job_id, status=jobs.JOB_CANCELLED)

        data = _wait_for(job_id, {jobs.JOB_CANCELLED})
        self.assertEqual(data["status"], jobs.JOB_CANCELLED)
        self.assertNotIn("result", data if data else {})

    def test_heartbeat_updated_at_tang_theo_moi_lan_ghi(self):
        job_id = jobs.spawn_job("test", lambda _cc, _hb: {"success": True})
        first = jobs.get_job(job_id)["updated_at"]
        data = _wait_for(job_id, {jobs.JOB_COMPLETED})
        self.assertGreaterEqual(data["updated_at"], first)

    def test_heartbeat_doi_message_va_bump_updated_at(self):
        seen = {}

        def work(_cc, heartbeat):
            time.sleep(0.05)
            heartbeat("Đang sinh transcript (lần thử 1)...")
            seen["after_beat"] = jobs.get_job(job_id)
            time.sleep(0.05)
            return {"success": True}

        job_id = jobs.spawn_job("test", work)
        data = _wait_for(job_id, {jobs.JOB_COMPLETED})
        self.assertEqual(data["status"], jobs.JOB_COMPLETED)
        self.assertEqual(seen["after_beat"]["message"], "Đang sinh transcript (lần thử 1)...")
        self.assertGreater(seen["after_beat"]["updated_at"], seen["after_beat"]["created_at"])

    def test_watchdog_ket_job_treo_thanh_error_bo_thread_nen(self):
        """work_fn không bao giờ trả về (mô phỏng Gemini/DeepSeek treo) → watchdog tự đánh error."""
        def hang(_cc, _hb):
            time.sleep(30)  # daemon thread — chết theo process test
            return {"success": True}

        job_id = jobs.spawn_job("test", hang, hard_timeout_s=0.4)
        data = _wait_for(job_id, {jobs.JOB_ERROR}, timeout=5)
        self.assertEqual(data["status"], jobs.JOB_ERROR)
        self.assertIn("watchdog", data.get("error", ""))
        self.assertIn("Xử lý quá lâu", data["message"])

    def test_watchdog_khong_dong_vao_job_da_xong_truoc_deadline(self):
        job_id = jobs.spawn_job("test", lambda _cc, _hb: {"success": True, "x": 1}, hard_timeout_s=2)
        data = _wait_for(job_id, {jobs.JOB_COMPLETED})
        self.assertEqual(data["status"], jobs.JOB_COMPLETED)
        time.sleep(2.2)  # để watchdog chạy qua mốc
        self.assertEqual(jobs.get_job(job_id)["status"], jobs.JOB_COMPLETED)  # vẫn completed, không bị ghi đè


class JobEndpointTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _get_status(self, job_id):
        req = self.factory.get(f"/api/content/transform-jobs/{job_id}/")
        force_authenticate(req, user=_Authed())
        return jobs.transform_job_status(req, job_id=job_id)

    def _post_cancel(self, job_id):
        req = self.factory.post(f"/api/content/transform-jobs/{job_id}/cancel/")
        force_authenticate(req, user=_Authed())
        return jobs.transform_job_cancel(req, job_id=job_id)

    def test_status_job_khong_ton_tai_tra_404(self):
        resp = self._get_status("khong-co-that")
        self.assertEqual(resp.status_code, 404)

    def test_cancel_job_khong_ton_tai_tra_404(self):
        resp = self._post_cancel("khong-co-that")
        self.assertEqual(resp.status_code, 404)

    def test_status_tra_ve_du_field_khi_job_hoan_tat(self):
        job_id = jobs.spawn_job("transcribe", lambda _cc, _hb: {"success": True, "transcript": "abc"})
        _wait_for(job_id, {jobs.JOB_COMPLETED})
        resp = self._get_status(job_id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["success"])
        self.assertEqual(resp.data["status"], "completed")
        self.assertEqual(resp.data["kind"], "transcribe")
        self.assertEqual(resp.data["result"]["transcript"], "abc")

    def test_cancel_job_da_o_trang_thai_cuoi_la_no_op(self):
        job_id = jobs.spawn_job("test", lambda _cc, _hb: {"success": True})
        _wait_for(job_id, {jobs.JOB_COMPLETED})
        resp = self._post_cancel(job_id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("already_final"))
        self.assertEqual(resp.data["status"], "completed")

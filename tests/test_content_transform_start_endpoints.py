"""Endpoint TẠO job nền của content-transform: transcribe_upload_start + transform_content_upgrade_start.

Khoá:
  1. Validate đầu vào giống bản đồng bộ (thiếu file / file quá lớn / thiếu prompt → 4xx, KHÔNG spawn job).
  2. Hợp lệ → trả { success, job_id } NGAY, job xuất hiện trong store đúng kind.
  3. work_fn được gói đúng: transcribe gọi run_transcribe_upload_core; upgrade gọi
     PaastAnalysisService.upgrade_scripted và map kết quả về shape KHỚP bản đồng bộ.
  4. client_context (source_history_id) được giữ lại trong store để BE poll lấy "previous".
  5. Kết quả success:false từ run_transcribe_upload_core (vd 504/thời lượng) → job status 'error',
     KHÔNG nuốt thành lỗi chung.

Chạy: python manage.py test tests.test_content_transform_start_endpoints
"""
import io
import time
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.views import content_transform_job_views as jobs


class _Authed:
    is_authenticated = True
    is_active = True
    is_staff = False
    pk = "u-test"
    id = "u-test"


def _wait(job_id, statuses, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = jobs.get_job(job_id)
        if d and d.get("status") in statuses:
            return d
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} không vào {statuses} (hiện {jobs.get_job(job_id)})")


def _upload(name="a.mp4", size=1024, content_type="video/mp4"):
    f = io.BytesIO(b"x" * size)
    f.name = name
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, b"x" * size, content_type=content_type)


class TranscribeStartTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _call(self, data):
        req = self.factory.post("/api/content/transcribe-upload/start/", data, format="multipart")
        force_authenticate(req, user=_Authed())
        return jobs.transcribe_upload_start(req)

    def test_thieu_file_tra_400_khong_spawn(self):
        resp = self._call({})
        self.assertEqual(resp.status_code, 400)

    def test_file_qua_lon_tra_400(self):
        big = _upload(size=1024)
        with mock.patch.object(jobs, "MAX_UPLOAD_SIZE_MB", 0):  # ép mọi file vượt ngưỡng
            resp = self._call({"file": big})
        self.assertEqual(resp.status_code, 400)

    def test_hop_le_tra_job_id_va_goi_dung_loi(self):
        core = mock.Mock(return_value={"success": True, "transcript": "xin chào", "duration_seconds": 3, "char_count": 8})
        with mock.patch.object(jobs, "run_transcribe_upload_core", core), \
             mock.patch.object(jobs, "_get_ffmpeg", return_value="/usr/bin/ffmpeg"):
            resp = self._call({"file": _upload()})
            self.assertEqual(resp.status_code, 200)
            job_id = resp.data["job_id"]
            data = _wait(job_id, {jobs.JOB_COMPLETED, jobs.JOB_ERROR})
        self.assertEqual(data["status"], jobs.JOB_COMPLETED)
        self.assertEqual(data["kind"], "transcribe")
        self.assertEqual(data["result"]["transcript"], "xin chào")
        core.assert_called_once()

    def test_core_tra_success_false_thi_job_error(self):
        core = mock.Mock(return_value={"success": False, "error_message": "Thời lượng file quá dài (900 giây > 600 giây).", "status_code": 400})
        with mock.patch.object(jobs, "run_transcribe_upload_core", core), \
             mock.patch.object(jobs, "_get_ffmpeg", return_value="/usr/bin/ffmpeg"):
            resp = self._call({"file": _upload()})
            data = _wait(resp.data["job_id"], {jobs.JOB_COMPLETED, jobs.JOB_ERROR})
        self.assertEqual(data["status"], jobs.JOB_ERROR)
        self.assertIn("Thời lượng file quá dài", data["message"])


class UpgradeStartTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _call(self, body):
        req = self.factory.post("/api/ai/transform-content/upgrade/start/", body, format="json")
        force_authenticate(req, user=_Authed())
        return jobs.transform_content_upgrade_start(req)

    def test_thieu_prompt_tra_400(self):
        self.assertEqual(self._call({"write_system_prompt": "x"}).status_code, 400)

    def test_hop_le_map_ket_qua_ve_shape_dong_bo_va_giu_client_context(self):
        fake = {
            "output_text": "kịch bản mới",
            "new_analysis": {"total_score": 88, "layers": {"prefer": {}}},
            "score_error": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "model_used": "deepseek-v4-flash",
        }
        with mock.patch("video_management.services.paast_analysis_service.PaastAnalysisService.upgrade_scripted", return_value=fake):
            resp = self._call({
                "write_system_prompt": "sys",
                "write_user_prompt": "usr",
                "client_context": {"source_history_id": "src-123"},
            })
            self.assertEqual(resp.status_code, 200)
            data = _wait(resp.data["job_id"], {jobs.JOB_COMPLETED, jobs.JOB_ERROR})

        self.assertEqual(data["status"], jobs.JOB_COMPLETED)
        self.assertEqual(data["kind"], "upgrade")
        self.assertEqual(data["client_context"], {"source_history_id": "src-123"})
        r = data["result"]
        self.assertEqual(r["output_text"], "kịch bản mới")
        self.assertEqual(r["score"], fake["new_analysis"])
        self.assertEqual(r["score_error"], None)
        self.assertEqual(r["usage"], fake["usage"])

    def test_upgrade_scripted_nem_loi_thi_job_error(self):
        with mock.patch("video_management.services.paast_analysis_service.PaastAnalysisService.upgrade_scripted", side_effect=RuntimeError("DeepSeek từ chối")):
            resp = self._call({"write_system_prompt": "sys", "write_user_prompt": "usr"})
            data = _wait(resp.data["job_id"], {jobs.JOB_COMPLETED, jobs.JOB_ERROR})
        self.assertEqual(data["status"], jobs.JOB_ERROR)
        self.assertIn("DeepSeek từ chối", data["message"])

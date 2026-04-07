"""
Submit checklist công việc vào LarkSuite Bitable.
Frontend gửi payload JSON (checklist + chi tiết), backend lưu vào 1 field dạng JSON string.
"""
import json
import logging
import requests
import pytz
from datetime import datetime
from django.conf import settings
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
import uuid
from rest_framework.permissions import AllowAny
from ..models import LarkReport, AppUser, ReportOutstanding, LarkPermission

# NOTE: On NAS deployments that overwrite code folders, these optional modules
# may be missing temporarily. Keep imports lazy to avoid crashing the whole worker.
try:
    from ..models import ReportSettings  # type: ignore
except Exception:  # pragma: no cover
    ReportSettings = None  # type: ignore

try:
    from ..utils.lark_utils import get_lark_tenant_access_token, create_bitable_record  # type: ignore
except Exception:  # pragma: no cover
    get_lark_tenant_access_token = None  # type: ignore
    create_bitable_record = None  # type: ignore

try:
    from ..tasks import push_report_to_lark_task  # type: ignore
except Exception:  # pragma: no cover
    push_report_to_lark_task = None  # type: ignore

logger = logging.getLogger(__name__)


LARK_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_BITABLE_APP_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}"
LARK_BITABLE_RECORDS_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
LARK_BITABLE_FIELDS_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
LARK_BITABLE_SEARCH_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"




# Lark helpers moved to utils/lark_utils.py


def search_user_by_email(tenant_access_token: str, email: str) -> dict:
    """
    Tìm kiếm user trong bảng Lark theo email để lấy Role, Team, Nhân viên.
    Trả về dict với các field: Role, Team, Nhân viên (nếu tìm thấy), hoặc dict rỗng.
    """
    app_token = getattr(settings, "LARK_BASE_ID", "") or ""
    table_id = getattr(settings, "LARK_TABLE_ID", "") or ""
    LARK_BITABLE_SEARCH_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"

    if not app_token or not table_id:
        logger.warning("Không có LARK_BASE_ID hoặc LARK_TABLE_ID để search")
        return {}
    
    url = LARK_BITABLE_SEARCH_URL.format(app_token=app_token, table_id=table_id)
    headers = {
        "Authorization": f"Bearer {tenant_access_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    
    # Search filter: Email field equals email
    payload = {
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": "Email",
                    "operator": "is",
                    "value": [email]
                }
            ]
        },
        "field_names": ["Role", "Team", "Nhân viên"],  # Chỉ lấy các field cần
        "page_size": 1  # Chỉ cần 1 record mới nhất
    }
    
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        data = resp.json()
        
        if not resp.ok or data.get("code") != 0:
            logger.warning("Lark search failed: %s", data)
            return {}
        
        items = data.get("data", {}).get("items", [])
        if not items:
            logger.info("Không tìm thấy user với email: %s", email)
            return {}
        
        # Lấy record đầu tiên
        record = items[0]
        fields = record.get("fields", {})
        
        result = {}
        if "Role" in fields:
            result["Role"] = fields["Role"]
        if "Team" in fields:
            result["Team"] = fields["Team"]
        if "Nhân viên" in fields:
            result["Nhân viên"] = fields["Nhân viên"]
        
        logger.info("Tìm thấy user info cho %s: %s", email, result)
        return result
        
    except Exception as e:
        logger.exception("Error searching user: %s", e)
        return {}


def _mask(s: str, show_last: int = 4) -> str:
    """Che đầu chuỗi, chỉ giữ vài ký tự cuối."""
    if not s or len(s) <= show_last:
        return "***"
    return "*" * (len(s) - show_last) + s[-show_last:]



def is_reporting_open(settings_obj, user_email=None):
    """
    Cho phép gửi checklist theo lịch ngày trong tuần (nếu có) và one_report_per_day.

    Không còn kiểm tra khung giờ trong ngày (vd. 08:00–10:00 hay random window).

    Nếu schedule rỗng hoặc không có ngày nào bật enabled → không chặn theo lịch.
    Nếu có ngày enabled → kiểm tra hôm nay có trong lịch không, trừ Chủ nhật (luôn cho phép).
    """
    if not settings_obj:
        return True, ""

    schedule = getattr(settings_obj, "schedule", None) or {}
    if not isinstance(schedule, dict):
        schedule = {}

    def _schedule_has_any_enabled() -> bool:
        for v in schedule.values():
            if isinstance(v, dict) and v.get("enabled"):
                return True
        return False

    tz_name = getattr(settings_obj, "timezone", None) or "Asia/Ho_Chi_Minh"
    vn_tz = pytz.timezone(tz_name)

    def _one_report_today_block():
        if not settings_obj.one_report_per_day or not user_email:
            return False, ""
        now = datetime.now(vn_tz)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        already = LarkReport.objects.filter(
            email__iexact=user_email,
            date__gte=today_start,
        ).exists()
        if already:
            return True, "Bạn đã gửi báo cáo ngày hôm nay rồi."
        return False, ""

    # Chưa bật khung giờ nào → không kiểm tra giờ; vẫn có thể giới hạn 1 báo cáo/ngày
    if not _schedule_has_any_enabled():
        blocked, msg = _one_report_today_block()
        if blocked:
            return False, msg
        return True, ""

    now = datetime.now(vn_tz)
    # 0=Thứ Hai … 6=Chủ nhật — không phụ thuộc locale của strftime %A.
    is_sunday = now.weekday() == 6
    day_key = now.strftime("%A").lower()

    # Chủ nhật luôn được báo cáo (tránh chặn khi lịch chỉ bật T2–T7).
    if not is_sunday:
        day_sched = schedule.get(day_key)
        if not day_sched or not day_sched.get("enabled"):
            return (
                False,
                "Hôm nay không nằm trong ngày được phép báo cáo theo lịch đã cấu hình.",
            )

    # Không validate start/end trong ngày — cho gửi bất kỳ giờ nào trong ngày được phép.
    blocked, msg = _one_report_today_block()
    if blocked:
        return False, msg
    return True, ""


class ChecklistReportingStatusView(APIView):
    """
    Kiểm tra xem hiện tại user có được phép báo cáo hay không.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        if ReportSettings is None:
            return Response(
                {"error": "Checklist module chưa sẵn sàng (thiếu ReportSettings)."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        user_email = request.query_params.get('email', '')
        settings_obj = ReportSettings.objects.first()
        
        is_open, message = is_reporting_open(settings_obj, user_email if user_email else None)
        
        return Response({
            "is_open": is_open,
            "message": message,
            "can_report": is_open,
            "one_report_per_day": settings_obj.one_report_per_day if settings_obj else True
        })


class ChecklistCheckView(APIView):
    """
    GET: Kiểm tra cấu hình Lark (không lộ secret). Gọi thử token + quyền Base.
    Dùng để debug 403: xem app_id/base_id đang dùng và Lark trả gì.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        app_id = (getattr(settings, "LARK_APP_ID", "") or "").strip()
        app_secret = getattr(settings, "LARK_APP_SECRET", "") or ""
        base_id = (getattr(settings, "LARK_BASE_ID", "") or "").strip()
        table_id = (getattr(settings, "LARK_TABLE_ID", "") or "").strip()
        field_id = (getattr(settings, "LARK_FIELD_ID", "") or "").strip()

        result = {
            "config": {
                "LARK_APP_ID": _mask(app_id, 8) if app_id else "(chưa cấu hình)",
                "LARK_BASE_ID": base_id or "(chưa cấu hình)",
                "LARK_TABLE_ID": table_id or "(chưa cấu hình)",
                "LARK_FIELD_ID": field_id or "(chưa cấu hình)",
            },
            "token_ok": False,
            "base_access_ok": False,
            "lark_status": None,
            "lark_msg": None,
        }

        if not app_id or not app_secret:
            result["lark_msg"] = "Thiếu LARK_APP_ID hoặc LARK_APP_SECRET trong .env"
            return Response(result, status=status.HTTP_200_OK)

        try:
            token = get_lark_tenant_access_token()
            result["token_ok"] = True
        except Exception as e:
            result["lark_msg"] = f"Lấy token thất bại: {e}"
            return Response(result, status=status.HTTP_200_OK)

        if not base_id:
            result["lark_msg"] = "Thiếu LARK_BASE_ID"
            return Response(result, status=status.HTTP_200_OK)

        LARK_BITABLE_APP_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}"
        url = LARK_BITABLE_APP_URL.format(app_token=base_id)
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        try:
            body = resp.json()
        except Exception:
            body = {}
        result["lark_status"] = resp.status_code
        result["lark_msg"] = body.get("msg", body.get("error", resp.reason))

        if resp.ok and body.get("code") == 0:
            result["base_access_ok"] = True
            result["lark_msg"] = "OK — App có quyền truy cập Base này."
        elif resp.status_code == 403:
            result["lark_msg"] = (
                body.get("msg", "403 Forbidden") + " — App (LARK_APP_ID) chưa được quyền vào Base (LARK_BASE_ID), "
                "hoặc LARK_BASE_ID không đúng Base token. Kiểm tra .env và quyền Base trên Lark."
            )

        return Response(result, status=status.HTTP_200_OK)



class ChecklistSubmitView(APIView):
    """
    POST: Nhận payload checklist từ frontend, lưu vào Lark Bitable (một field JSON).
    Body mong đợi: JSON với các key checklist (boolean) và chi tiết (string), có thể có isLate.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        if ReportSettings is None:
            return Response(
                {"error": "Checklist module chưa sẵn sàng (thiếu ReportSettings)."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            payload = request.data
            # 0. Check settings
            settings_obj = ReportSettings.objects.first()
            user_email = (payload.get("userEmail", "") or "").strip().lower()  # normalize before status check
            
            is_open, err_msg = is_reporting_open(settings_obj, user_email)
            if not is_open:
                return Response(
                    {"error": err_msg},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if not isinstance(payload, dict):
                return Response(
                    {"error": "Body phải là JSON object"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Lấy thông tin user từ payload
            user_email = (payload.get("userEmail", "") or "").strip().lower()  # normalize to lowercase
            user_name = payload.get("userName", "")
            
            if not user_email or not user_name:
                return Response(
                    {"error": "Thiếu thông tin userEmail hoặc userName"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            
            # Thời gian báo cáo (Asia/Ho_Chi_Minh)
            vietnam_tz = pytz.timezone('Asia/Ho_Chi_Minh')
            now_vn = datetime.now(vietnam_tz)
            today_vn = now_vn.date()

            # Kiểm tra xem có reportDate tùy chỉnh từ frontend không
            custom_report_date = payload.get("reportDate")
            if custom_report_date:
                try:
                    # Chấp nhận định dạng ISO hoặc YYYY-MM-DD
                    if 'T' in custom_report_date:
                        current_datetime = datetime.fromisoformat(custom_report_date.replace('Z', '+00:00')).astimezone(vietnam_tz)
                    else:
                        current_datetime = datetime.strptime(custom_report_date, "%Y-%m-%d")
                        current_datetime = vietnam_tz.localize(current_datetime)

                    # Không cho báo cáo ngày tương lai
                    if current_datetime.date() > today_vn:
                        return Response(
                            {"error": "Không thể gửi báo cáo cho ngày trong tương lai."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                except Exception as e:
                    logger.warning("Lỗi parse custom_report_date '%s', dùng giờ HT: %s", custom_report_date, e)
                    current_datetime = now_vn
            else:
                current_datetime = now_vn
            
            # Loại bỏ user metadata khỏi payload checklist
            checklist_data = {k: v for k, v in payload.items() if k not in ["userEmail", "userName", "reportDate", "userTeam", "userRoles", "userRole"]}
            
            # 1 & 2. Lookup AppUser theo email (ưu tiên) rồi mới theo tên
            # Email là định danh duy nhất khi login Google – không dùng tên để tránh trùng/sai
            user_role = payload.get("userRole") 
            if not user_role and payload.get("userRoles"):
                urs = payload.get("userRoles")
                if isinstance(urs, list) and urs:
                    user_role = urs[0]
                elif isinstance(urs, str):
                    user_role = urs

            user_team = payload.get("userTeam")
            user_emp_data = None
            
            try:
                # Ưu tiên email (chính xác, không phụ thuộc cách nhập tên)
                app_user = AppUser.objects.filter(email__iexact=user_email).first()
                if not app_user and user_name:
                    # Fallback theo tên khi không tìm được qua email
                    app_user = AppUser.objects.filter(full_name__iexact=user_name.strip()).first()
                
                if app_user:
                    if not user_role and app_user.roles:
                        # Parse first role (postgres array format: {role1,role2})
                        user_role = str(app_user.roles).replace("{", "").replace("}", "").split(",")[0].strip('"').strip("'")
                    if not user_team:
                        user_team = app_user.team
                    user_emp_data = app_user.employee_data
                    logger.info("Tìm thấy AppUser cho %s: team=%s, role=%s", user_email, user_team, user_role)
                else:
                    logger.warning("Không tìm thấy AppUser cho email=%s name=%s. Sử dụng thông tin từ payload: team=%s", user_email, user_name, user_team)
            except Exception as e:
                logger.warning("Lỗi lookup AppUser: %s", e)

            # Tạo ID ngẫu nhiên cho bản ghi Local
            main_record_id = f"local_{uuid.uuid4().hex[:12]}"

            # --- LƯU VÀO DATABASE (Postgres) ---
            report = LarkReport.objects.create(
                id=main_record_id,
                name=user_name,
                email=user_email,
                team=user_team,
                role=user_role,
                employee=user_emp_data,
                answers=checklist_data,
                date=current_datetime,
            )
            
            # --- LƯU VÀO ReportOutstanding (LOCAL) ---
            # 1. Tìm team và role từ LarkPermission nếu chưa có team
            if not user_team or not user_role:
                try:
                    perm = LarkPermission.objects.filter(email__iexact=user_email).first()
                    if perm:
                        if not user_team:
                            user_team = perm.team
                        if not user_role:
                            user_role = perm.role
                except Exception as e:
                    logger.warning("Lỗi lookup LarkPermission cho ReportOutstanding: %s", e)
            
            # Map role string representation if needed
            if user_role and (user_role.startswith('{') or user_role.startswith('[')):
                user_role = user_role.replace('{', '').replace('}', '').replace('[', '').replace(']', '').split(',')[0].strip('"').strip("'")

            # 2. Map các câu hỏi sang ReportOutstanding
            outstanding_mappings = [
                ("4. Bạn có đóng góp ý tưởng hay đề xuất gì không?", "Ý KIẾN ĐÓNG GÓP CẢI TIẾN MỚI"),
                ("3. Bạn có gặp khó khăn nào cần hỗ trợ không?", "KHÓ KHĂN CẦN HỖ TRỢ"),
                ("5. Bạn có sản phẩm (A4 - A5) nào win mới không? (>5k view - >10 cmt hỏi giá?)", "VIDEO SẢN PHẨM WIN"),
                ("2. Hôm qua có đổi mới sáng tạo gì được áp dụng vào công việc của bạn không?", "Ý KIẾN ĐÓNG GÓP CẢI TIẾN MỚI"),
                ("2. Team bạn hôm qua có thành viên nào có video Win nhất?", "VIDEO WIN"),
                ("3. Team bạn hôm qua có gì đổi mới được áp dụng không?", "Ý KIẾN ĐÓNG GÓP CẢI TIẾN MỚI"),
                ("5. Team bạn hôm qua có sản phẩm nào win mới không? Đã thông tin lên Group New Product chưa?", "VIDEO SẢN PHẨM WIN"),
            ]

            for q_key, content_label in outstanding_mappings:
                answer = checklist_data.get(q_key)
                if answer and isinstance(answer, str) and answer.strip() and answer.strip().lower() not in ["không", "không có", "k", "no", "none", ".", "không ạ"]:
                    try:
                        ReportOutstanding.objects.create(
                            id=f"out_{uuid.uuid4().hex[:12]}",
                            name=user_name,
                            date=current_datetime.strftime("%d/%m/%Y"),
                            team=user_team,
                            role=user_role,
                            category=content_label,
                            content=answer.strip(),
                            email=user_email,
                            status=None 
                        )

                    except Exception as e:
                        logger.error("Lỗi lưu ReportOutstanding local cho câu hỏi '%s': %s", q_key, e)

            # --- TRIGGER BACKGROUND SYNC TO LARK DISABLED ---
            # try:
            #     # Gửi task vào Celery để xử lý việc đẩy dữ liệu lên Lark ở background
            #     push_report_to_lark_task.delay(main_record_id)
            #     logger.info("Triggered background sync to Lark for %s", user_email)
            # except Exception as e:
            #     logger.error("Could not trigger background task for Lark sync: %s", e)

            return Response({
                "success": True,
                "message": "Báo cáo thành công",
                "record_id": main_record_id,
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.exception("Checklist submit error: %s", e)
            return Response(
                {"error": "Lỗi xử lý báo cáo", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # NOTE: các except bên dưới đã được xử lý trong khối try/except phía trên.
        # Giữ lại comment để rõ ràng, không cần thêm handler trùng lặp.

class ChecklistSettingsView(APIView):
    """
    GET: Lấy cấu hình khung giờ báo cáo.
    PUT: Cập nhật cấu hình khung giờ báo cáo.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        if ReportSettings is None:
            return Response(
                {"error": "Checklist module chưa sẵn sàng (thiếu ReportSettings)."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        settings_obj = ReportSettings.objects.first()
        if not settings_obj:
            # Tạo default nếu chưa có
            default_schedule = {
                day: {"start": "08:00", "end": "10:00", "enabled": True}
                for day in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday']
            }
            default_schedule['sunday'] = {"start": "08:00", "end": "10:00", "enabled": False}
            
            settings_obj = ReportSettings.objects.create(
                schedule=default_schedule,
                one_report_per_day=True,
                timezone='Asia/Ho_Chi_Minh'
            )

        return Response({
            "schedule": settings_obj.schedule,
            "one_report_per_day": settings_obj.one_report_per_day,
            "timezone": settings_obj.timezone,
            "is_random": settings_obj.is_random,
            "random_minutes": settings_obj.random_minutes,
            "updated_at": settings_obj.updated_at,
            "updated_by": settings_obj.updated_by
        })


    def put(self, request):
        if ReportSettings is None:
            return Response(
                {"error": "Checklist module chưa sẵn sàng (thiếu ReportSettings)."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            data = request.data
            settings_obj = ReportSettings.objects.first()
            if not settings_obj:
                settings_obj = ReportSettings()

            if 'schedule' in data:
                settings_obj.schedule = data['schedule']
            if 'one_report_per_day' in data:
                settings_obj.one_report_per_day = data['one_report_per_day']
            if 'timezone' in data:
                settings_obj.timezone = data['timezone']
            if 'is_random' in data:
                settings_obj.is_random = data['is_random']
            if 'random_minutes' in data:
                settings_obj.random_minutes = data['random_minutes']
            if 'updated_by' in data:
                settings_obj.updated_by = data['updated_by']

            settings_obj.save()
            return Response({"message": "Lưu cấu hình thành công", "success": True})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

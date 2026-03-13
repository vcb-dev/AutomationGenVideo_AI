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
from ..models import LarkReport, AppUser, LarkEmployee, ReportOutstanding, LarkPermission, ReportSettings
from ..utils.lark_utils import get_lark_tenant_access_token, create_bitable_record
from ..tasks import push_report_to_lark_task

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
    if not settings_obj:
        return True, ""

    vn_tz = pytz.timezone(settings_obj.timezone)
    now = datetime.now(vn_tz)
    day_key = now.strftime('%A').lower()
    
    schedule = settings_obj.schedule.get(day_key)
    if not schedule or not schedule.get('enabled'):
        return False, f"Hôm nay ({day_key.capitalize()}) không có lịch báo cáo."

    start_str = schedule.get('start', '00:00')
    end_str = schedule.get('end', '23:59')
    
    try:
        # Base range
        start_time_obj = datetime.strptime(start_str, "%H:%M").time()
        end_time_obj = datetime.strptime(end_str, "%H:%M").time()
    except Exception:
        return True, "" # Fallback if time format is broken
    
    current_time = now.time()

    if settings_obj.is_random:
        # Randomization logic
        import random
        from django.conf import settings as django_settings
        seed_str = f"{now.strftime('%Y-%m-%d')}_{django_settings.SECRET_KEY}"
        random.seed(seed_str)
        
        # Calculate total minutes in base range
        start_min = start_time_obj.hour * 60 + start_time_obj.minute
        end_min = end_time_obj.hour * 60 + end_time_obj.minute
        total_range = end_min - start_min
        
        if total_range > settings_obj.random_minutes:
            offset = random.randint(0, total_range - settings_obj.random_minutes)
            actual_start_min = start_min + offset
            actual_end_min = actual_start_min + settings_obj.random_minutes
            
            curr_min = current_time.hour * 60 + current_time.minute
            if curr_min < actual_start_min or curr_min > actual_end_min:
                return False, "Ngoài khung giờ báo cáo ngẫu nhiên của ngày hôm nay."
        else:
            if current_time < start_time_obj or current_time > end_time_obj:
                return False, f"Ngoài khung giờ báo cáo ({start_str} - {end_str})."
    else:
        if current_time < start_time_obj or current_time > end_time_obj:
            return False, f"Ngoài khung giờ báo cáo ({start_str} - {end_str})."

    # Check if already reported today
    if settings_obj.one_report_per_day and user_email:
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Chuyển về UTC để query database (nếu date lưu UTC)
        # LarkReport.date có vẻ là DateTimeField
        already_reported = LarkReport.objects.filter(
            email=user_email,
            date__gte=today_start
        ).exists()
        if already_reported:
            return False, "Bạn đã gửi báo cáo ngày hôm nay rồi."

    return True, ""


class ChecklistReportingStatusView(APIView):
    """
    Kiểm tra xem hiện tại user có được phép báo cáo hay không.
    """
    permission_classes = [AllowAny]

    def get(self, request):
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
        try:
            payload = request.data
            # 0. Check settings
            settings_obj = ReportSettings.objects.first()
            user_email = payload.get("userEmail", "")
            
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
            user_email = payload.get("userEmail", "")
            user_name = payload.get("userName", "")
            
            if not user_email or not user_name:
                return Response(
                    {"error": "Thiếu thông tin userEmail hoặc userName"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            
            # Thời gian báo cáo (Asia/Ho_Chi_Minh)
            vietnam_tz = pytz.timezone('Asia/Ho_Chi_Minh')
            current_datetime = datetime.now(vietnam_tz)
            
            # Loại bỏ userEmail và userName khỏi payload checklist
            checklist_data = {k: v for k, v in payload.items() if k not in ["userEmail", "userName"]}
            
            # 1. Lookup thông tin từ table AppUser (users)
            user_role = None
            try:
                user_record = AppUser.objects.filter(email=user_email).first()
                if user_record and user_record.roles:
                    # Parse first role if it's a string representation of postgres array
                    user_role = str(user_record.roles).replace("{", "").replace("}", "").split(",")[0]
            except Exception as e:
                logger.warning("Lỗi lookup AppUser: %s", e)


            # 2. Lookup thông tin từ table LarkEmployee (nếu có để lấy Team)
            user_team = None
            user_emp_data = None
            try:
                emp_record = LarkEmployee.objects.filter(name=user_name).first()
                if emp_record:
                    user_team = emp_record.team
                    user_emp_data = emp_record.employee_data
            except Exception as e:
                logger.warning("Lỗi lookup LarkEmployee: %s", e)

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
            # 1. Tìm team từ LarkPermission nếu chưa có team
            if not user_team:
                try:
                    perm = LarkPermission.objects.filter(email__iexact=user_email).first()
                    if perm:
                        user_team = perm.team
                except Exception as e:
                    logger.warning("Lỗi lookup LarkPermission cho ReportOutstanding: %s", e)

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

        except ValueError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except requests.HTTPError as e:
            # Lark trả 400/403/4xx/5xx: đọc body và trả lại cho client
            logger.warning("Lark API HTTP error: %s", e)
            detail = str(e)
            if e.response is not None:
                try:
                    body = e.response.json()
                    detail = body.get("msg", body.get("error", detail))
                except Exception:
                    pass
            # 403 Forbidden = app chưa được quyền truy cập Base/table này
            if e.response is not None and e.response.status_code == 403:
                return Response(
                    {
                        "error": "Lark từ chối truy cập (403 Forbidden).",
                        "detail": detail,
                        "hint": (
                            "Kiểm tra: (1) LARK_APP_ID trong .env có đúng là app bạn đã cấp quyền Base không? "
                            "Vào Developer Console → app → xem App ID. (2) LARK_BASE_ID phải là Base token của đúng Base "
                            "(mở Base trực tiếp từ Lark Base, URL dạng .../base/XXXX — XXXX là Base token). "
                            "(3) Base đó phải được chia sẻ/ủy quyền cho đúng app. (4) Restart Django để lấy token mới."
                        ),
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            return Response(
                {"error": "Lark API lỗi (Bad Request hoặc từ chối request).", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except requests.RequestException as e:
            logger.exception("Lark API request failed: %s", e)
            return Response(
                {"error": "Không kết nối được Lark API", "detail": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception as e:
            logger.exception("Checklist submit error: %s", e)
            return Response(
                {"error": "Lỗi xử lý báo cáo", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

class ChecklistSettingsView(APIView):
    """
    GET: Lấy cấu hình khung giờ báo cáo.
    PUT: Cập nhật cấu hình khung giờ báo cáo.
    """
    permission_classes = [AllowAny]

    def get(self, request):
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

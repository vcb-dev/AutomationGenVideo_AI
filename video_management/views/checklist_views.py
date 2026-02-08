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
from rest_framework.permissions import AllowAny

logger = logging.getLogger(__name__)


LARK_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_BITABLE_APP_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}"
LARK_BITABLE_RECORDS_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
LARK_BITABLE_FIELDS_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
LARK_BITABLE_SEARCH_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"




def get_lark_tenant_access_token():
    """Lấy tenant_access_token từ Lark (cache đơn giản trong process, expire 2h)."""
    app_id = getattr(settings, "LARK_APP_ID", "") or ""
    app_secret = getattr(settings, "LARK_APP_SECRET", "") or ""
    if not app_id or not app_secret:
        raise ValueError("LARK_APP_ID và LARK_APP_SECRET phải được cấu hình trong .env")
    resp = requests.post(
        LARK_TOKEN_URL,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise ValueError(data.get("msg", "Lark token error"))
    return data["tenant_access_token"]


def create_bitable_record(tenant_access_token: str, fields: dict) -> dict:
    app_token = getattr(settings, "LARK_BASE_ID", "") or ""
    table_id = getattr(settings, "LARK_TABLE_ID", "") or ""
    if not app_token or not table_id:
        raise ValueError("LARK_BASE_ID và LARK_TABLE_ID phải được cấu hình")
    url = LARK_BITABLE_RECORDS_URL.format(app_token=app_token, table_id=table_id)
    headers = {
        "Authorization": f"Bearer {tenant_access_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {"fields": fields}
    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    try:
        body = resp.json()
    except Exception:
        body = {}
    if not resp.ok:
        err_msg = body.get("msg", body.get("error", resp.text or resp.reason))
        logger.warning("Lark Bitable %s: %s", resp.status_code, body)
        raise requests.HTTPError(
            f"Lark Bitable {resp.status_code}: {err_msg}",
            response=resp,
        )
    return body


def search_user_by_email(tenant_access_token: str, email: str) -> dict:
    """
    Tìm kiếm user trong bảng Lark theo email để lấy Role, Team, Nhân viên.
    Trả về dict với các field: Role, Team, Nhân viên (nếu tìm thấy), hoặc dict rỗng.
    """
    app_token = getattr(settings, "LARK_BASE_ID", "") or ""
    table_id = getattr(settings, "LARK_TABLE_ID", "") or ""
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


class ReportSettingsView(APIView):
    """
    GET: Lấy cấu hình khung giờ báo cáo hiện tại
    PUT: Cập nhật cấu hình (chỉ Manager)
    """
    permission_classes = [AllowAny]  # TODO: Thêm IsManager permission sau
    
    def get(self, request):
        """Lấy settings hiện tại"""
        try:
            from video_management.models import ReportSettings
            
            settings = ReportSettings.get_settings()
            
            return Response({
                "schedule": settings.schedule,
                "one_report_per_day": settings.one_report_per_day,
                "timezone": settings.timezone,
                "updated_at": settings.updated_at.isoformat() if settings.updated_at else None,
                "updated_by": settings.updated_by,
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.exception("Error getting report settings: %s", e)
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
    
    def put(self, request):
        """Cập nhật settings (Manager only)"""
        try:
            from video_management.models import ReportSettings
            
            settings = ReportSettings.get_settings()
            
            # Validate và update schedule
            if "schedule" in request.data:
                new_schedule = request.data["schedule"]
                # TODO: Validate schedule format
                settings.schedule = new_schedule
            
            if "one_report_per_day" in request.data:
                settings.one_report_per_day = request.data["one_report_per_day"]
            
            if "timezone" in request.data:
                settings.timezone = request.data["timezone"]
            
            # Track who updated
            user_email = request.data.get("updated_by", "unknown")
            settings.updated_by = user_email
            
            settings.save()
            
            logger.info("Report settings updated by %s", user_email)
            
            return Response({
                "success": True,
                "message": "Cập nhật cấu hình thành công",
                "schedule": settings.schedule,
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.exception("Error updating report settings: %s", e)
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ChecklistFieldsView(APIView):
    """
    GET: Lấy danh sách tất cả fields trong table để debug Field IDs
    """
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            app_token = getattr(settings, "LARK_BASE_ID", "") or ""
            table_id = getattr(settings, "LARK_TABLE_ID", "") or ""
            
            if not app_token or not table_id:
                return Response(
                    {"error": "LARK_BASE_ID và LARK_TABLE_ID phải được cấu hình"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            token = get_lark_tenant_access_token()
            url = LARK_BITABLE_FIELDS_URL.format(app_token=app_token, table_id=table_id)
            
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            
            data = resp.json()
            
            if not resp.ok or data.get("code") != 0:
                return Response(
                    {"error": data.get("msg", "Lỗi khi lấy fields"), "detail": data},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            
            # Format kết quả dễ đọc
            fields_list = []
            for field in data.get("data", {}).get("items", []):
                fields_list.append({
                    "field_id": field.get("field_id"),
                    "field_name": field.get("field_name"),
                    "type": field.get("type"),
                    "type_name": {
                        1: "Text",
                        2: "Number",
                        3: "Single Select",
                        4: "Multiple Select",
                        5: "Date",
                        7: "Checkbox",
                        11: "Person",
                        13: "Phone",
                        15: "URL",
                        17: "Attachment",
                        18: "Single Link",
                        19: "Formula",
                        20: "Duplex Link",
                        21: "Location",
                        22: "GroupChat",
                        23: "Created Time",
                        1001: "Created User",
                        1002: "Modified Time",
                        1003: "Modified User",
                    }.get(field.get("type"), f"Unknown({field.get('type')})")
                })
            
            return Response({
                "total_fields": len(fields_list),
                "fields": fields_list,
                "raw_response": data
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.exception("Error getting fields: %s", e)
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ChecklistSubmitView(APIView):
    """
    POST: Nhận payload checklist từ frontend, lưu vào Lark Bitable (một field JSON).
    Body mong đợi: JSON với các key checklist (boolean) và chi tiết (string), có thể có isLate.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            payload = request.data
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
            
            # ============================================
            # VALIDATION 1: Kiểm tra khung giờ cho phép
            # ============================================
            from video_management.models import ReportSettings
            
            settings = ReportSettings.get_settings()
            vietnam_tz = pytz.timezone(settings.timezone)
            current_datetime = datetime.now(vietnam_tz)
            
            # Check thời gian
            is_allowed, reason = settings.is_report_allowed(current_datetime)
            if not is_allowed:
                return Response(
                    {"error": "Không thể báo cáo"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            
            # ============================================
            # VALIDATION 2: Kiểm tra đã báo cáo hôm nay chưa (nếu bật one_report_per_day)
            # ============================================
            if settings.one_report_per_day:
                token = get_lark_tenant_access_token()
                
                # Tính timestamp đầu ngày và cuối ngày hôm nay
                today_start = current_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
                today_end = current_datetime.replace(hour=23, minute=59, second=59, microsecond=999999)
                
                start_timestamp = int(today_start.timestamp() * 1000)
                end_timestamp = int(today_end.timestamp() * 1000)
                
                # Search xem user đã submit hôm nay chưa
                from django.conf import settings as django_settings
                app_token = getattr(django_settings, "LARK_BASE_ID", "") or ""
                table_id = getattr(django_settings, "LARK_TABLE_ID", "") or ""
                
                if app_token and table_id:
                    url = LARK_BITABLE_SEARCH_URL.format(app_token=app_token, table_id=table_id)
                    headers = {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    }
                    
                    # Search filter: Email = user_email AND Date >= today_start AND Date <= today_end
                    search_payload = {
                        "filter": {
                            "conjunction": "and",
                            "conditions": [
                                {
                                    "field_name": "Email",
                                    "operator": "is",
                                    "value": [user_email]
                                },
                                {
                                    "field_name": "Date",
                                    "operator": "isGreater",
                                    "value": [start_timestamp]
                                },
                                {
                                    "field_name": "Date",
                                    "operator": "isLess",
                                    "value": [end_timestamp]
                                }
                            ]
                        },
                        "page_size": 1
                    }
                    
                    try:
                        resp = requests.post(url, json=search_payload, headers=headers, timeout=10)
                        data = resp.json()
                        
                        if resp.ok and data.get("code") == 0:
                            items = data.get("data", {}).get("items", [])
                            if items:
                                logger.info("User %s đã báo cáo hôm nay rồi", user_email)
                                return Response(
                                    {"error": "Bạn đã báo cáo hôm nay rồi. Mỗi ngày chỉ được báo cáo 1 lần."},
                                    status=status.HTTP_400_BAD_REQUEST,
                                )
                    except Exception as e:
                        logger.warning("Không check được duplicate report: %s", e)
                        # Không block user nếu check bị lỗi

            # Loại bỏ userEmail và userName khỏi payload checklist
            checklist_data = {k: v for k, v in payload.items() if k not in ["userEmail", "userName"]}
            
            # Convert checklist data thành JSON string
            checklist_json = json.dumps(checklist_data, ensure_ascii=False)
            
            # Date field (type: 5) cần timestamp milliseconds
            date_timestamp = int(current_datetime.timestamp() * 1000)
            
            # CÁCH 2: Lookup user info từ bảng Lark dựa vào email
            existing_user_info = search_user_by_email(token, user_email)
            
            # Tạo fields object cơ bản
            fields = {
                "Email": user_email,        
                "Answers": checklist_json,   
                "HoTen": user_name,         
                "Date": date_timestamp,
            }
            
            # Tự động điền Role, Team, Nhân viên nếu tìm thấy từ record cũ
            if existing_user_info:
                logger.info("Auto-fill từ record cũ: %s", existing_user_info)
                if "Role" in existing_user_info:
                    fields["Role"] = existing_user_info["Role"]
                if "Team" in existing_user_info:
                    fields["Team"] = existing_user_info["Team"]
                if "Nhân viên" in existing_user_info:
                    fields["Nhân viên"] = existing_user_info["Nhân viên"]
            else:
                logger.info("User mới hoặc không tìm thấy info trong Lark - chỉ điền Email, HoTen, Answers, Date")

            # Debug logging
            logger.info("Sending to Lark Bitable with fields: %s", list(fields.keys()))
            logger.info("Field values types: %s", {k: type(v).__name__ for k, v in fields.items()})

            # Token đã lấy ở trên, dùng luôn để create record
            result = create_bitable_record(token, fields)

            if result.get("code") != 0:
                logger.warning("Lark Bitable response: %s", result)
                logger.warning("Fields sent: %s", fields)
                return Response(
                    {"error": result.get("msg", "Lark Bitable lỗi"), "detail": result, "fields_sent": list(fields.keys())},
                    status=status.HTTP_502_BAD_GATEWAY,
                )


            return Response({
                "success": True,
                "message": "Báo cáo thành công",
                "record_id": result.get("data", {}).get("record", {}).get("record_id"),
            }, status=status.HTTP_201_CREATED)

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

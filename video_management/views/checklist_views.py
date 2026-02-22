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
from ..models import LarkReport, AppUser, LarkEmployee

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
            
            # Thời gian báo cáo (Asia/Ho_Chi_Minh)
            vietnam_tz = pytz.timezone('Asia/Ho_Chi_Minh')
            current_datetime = datetime.now(vietnam_tz)
            
            # Loại bỏ userEmail và userName khỏi payload checklist
            checklist_data = {k: v for k, v in payload.items() if k not in ["userEmail", "userName"]}
            
            # 1. Lookup thông tin từ table AppUser (users)
            user_role = None
            try:
                user_record = AppUser.objects.filter(email=user_email).first()
                if user_record:
                    user_role = user_record.role
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

            # Tạo ID ngẫu nhiên cho bản ghi (thay vì lấy từ Lark)
            record_id = f"local_{uuid.uuid4().hex[:12]}"
            
            # --- LƯU VÀO DATABASE (Postgres) ---
            report = LarkReport.objects.create(
                id=record_id,
                name=user_name,
                email=user_email,
                team=user_team,
                role=user_role,
                employee=user_emp_data,
                answers=checklist_data,
                date=current_datetime,
            )
            
            logger.info("Đã lưu báo cáo LOCAL %s cho %s vào database", record_id, user_email)

            return Response({
                "success": True,
                "message": "Báo cáo thành công (Lưu Database nội bộ)",
                "record_id": record_id,
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

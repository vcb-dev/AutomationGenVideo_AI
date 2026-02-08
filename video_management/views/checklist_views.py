"""
Submit checklist công việc vào LarkSuite Bitable.
Frontend gửi payload JSON (checklist + chi tiết), backend lưu vào 1 field dạng JSON string.
"""
import json
import logging
import requests
from django.conf import settings
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

logger = logging.getLogger(__name__)

LARK_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_BITABLE_APP_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}"
LARK_BITABLE_RECORDS_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"


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

            # Field ID lưu toàn bộ checklist dạng JSON string
            field_id = getattr(settings, "LARK_FIELD_ID", "") or ""
            if not field_id:
                return Response(
                    {"error": "LARK_FIELD_ID chưa cấu hình"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # Giữ nguyên payload frontend gửi, chỉ cần stringify để ghi vào Bitable
            json_string = json.dumps(payload, ensure_ascii=False)
            fields = {field_id: json_string}

            token = get_lark_tenant_access_token()
            result = create_bitable_record(token, fields)

            if result.get("code") != 0:
                logger.warning("Lark Bitable response: %s", result)
                return Response(
                    {"error": result.get("msg", "Lark Bitable lỗi"), "detail": result},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            return Response({
                "success": True,
                "message": "Đã lưu báo cáo vào Lark Bitable",
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

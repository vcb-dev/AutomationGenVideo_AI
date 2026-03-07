import logging
import requests
import json
from django.conf import settings

logger = logging.getLogger(__name__)

LARK_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_BITABLE_RECORDS_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"

def get_lark_tenant_access_token():
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

def create_bitable_record(tenant_access_token: str, fields: dict, table_id: str = None) -> dict:
    app_token = getattr(settings, "LARK_BASE_ID", "") or ""
    if not table_id:
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
        logger.warning("Lark Bitable %s (Table %s): %s", resp.status_code, table_id, body)
        raise requests.HTTPError(
            f"Lark Bitable {resp.status_code}: {err_msg}",
            response=resp,
        )
    return body

"""
Analytics Chatbot endpoint.
Endpoint: POST /api/chat/analytics/
Tối ưu: 1 lần gọi DeepSeek duy nhất, cache metadata DB, giảm context.
"""
import os, re, json, logging, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from difflib import SequenceMatcher
import unicodedata

_answer_cache = {}
_CACHE_TTL = 3600

def _clean_text(text):
    text = unicodedata.normalize('NFKC', text).lower()
    return re.sub(r'[^\w\s]', '', text).strip()

def _get_cached_answer(question):
    global _answer_cache
    now = time.time()
    _answer_cache = {k: v for k, v in _answer_cache.items() if now - v['ts'] < _CACHE_TTL}
    cq = _clean_text(question)
    for cached_q, v in _answer_cache.items():
        if SequenceMatcher(None, cq, cached_q).ratio() > 0.90:
            return v['response']
    return None

def _set_cached_answer(question, response):
    _answer_cache[_clean_text(question)] = {
        'ts': time.time(),
        'response': response
    }

import openai
import psycopg2, psycopg2.extras
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.conf import settings

logger = logging.getLogger(__name__)

DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
TZ_VN = timezone(timedelta(hours=7))
SKILLS_REPO = Path(os.getenv('SKILLS_REPO_PATH', '')) or \
    Path(__file__).resolve().parents[2] / 'skills'

# ── Skills ─────────────────────────────────────────────────────────────────────

SKILL_ROUTES = [
    {
        'keywords': ['ads', 'camp', 'mess', 'spend', 'chi phí', 'quảng cáo', 'roas', 'cpm', 'cpc'],
        'files': ['skills/03-danh-gia-hieu-suat.md'],
    },
    {
        'keywords': ['kênh', 'channel', 'view', 'follower', 'traffic', 'flow', 'organic',
                     'content', 'a1', 'a2', 'a3', 'a4', 'a5', 'video', 'góc độ'],
        'files': ['references/channel-system.md', 'references/content-angles.md'],
    },
]
CORE_SKILL_FILES = ['references/kpi-formulas.md']

_skill_cache: dict = {}

def _load_skill(f: str) -> str:
    if f not in _skill_cache:
        p = SKILLS_REPO / f
        _skill_cache[f] = p.read_text('utf-8')[:3000] if p.exists() else ''  # giới hạn 3000 ký tự/file
    return _skill_cache[f]

def _load_skills(question: str) -> str:
    q = question.lower()
    files = set(CORE_SKILL_FILES)
    for route in SKILL_ROUTES:
        if any(kw in q for kw in route['keywords']):
            files.update(route['files'])
            break  # chỉ load 1 route phù hợp nhất
    return '\n\n'.join(c for f in files if (c := _load_skill(f)))

# ── DB metadata cache ──────────────────────────────────────────────────────────

_meta_cache: dict = {}
_meta_ts: float = 0
_META_TTL = 300  # cache 5 phút

def _db_url():
    return os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL', '').replace('?pgbouncer=true', '')

def _get_metadata() -> dict:
    global _meta_cache, _meta_ts
    if time.time() - _meta_ts < _META_TTL and _meta_cache:
        return _meta_cache

    try:
        conn = psycopg2.connect(_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT team FROM ads_campaign_stats WHERE team IS NOT NULL ORDER BY team")
                teams_ads = [r['team'] for r in cur.fetchall()]
                cur.execute("SELECT DISTINCT team FROM social_video_report WHERE team IS NOT NULL ORDER BY team")
                teams_social = [r['team'] for r in cur.fetchall()]
                # Khoảng dữ liệu live (tháng đang chạy)
                cur.execute("SELECT MIN(year) min_y, MIN(month) min_m, MAX(year) max_y, MAX(month) max_m FROM social_video_report")
                dr = dict(cur.fetchone() or {})
                # Khoảng dữ liệu đã chốt (snapshot) — có thể chưa có nếu chưa chạy lock
                snap_dr = {}
                try:
                    cur.execute("SELECT MIN(report_year) min_y, MIN(report_month) min_m, MAX(report_year) max_y, MAX(report_month) max_m FROM social_video_snapshot")
                    snap_dr = dict(cur.fetchone() or {})
                except Exception:
                    pass  # Bảng chưa tồn tại → bỏ qua
        finally:
            conn.close()
        _meta_cache = {
            'teams_ads':    teams_ads,
            'teams_social': teams_social,
            'dr':           dr,      # live range
            'snap_dr':      snap_dr, # snapshot range
        }
        _meta_ts = time.time()
    except Exception as e:
        logger.error(f'[Meta cache] {e}')
        _meta_cache = {'teams_ads': [], 'teams_social': [], 'dr': {}, 'snap_dr': {}}
    return _meta_cache

def _query(sql: str) -> list:
    try:
        conn = psycopg2.connect(_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 15000;") # Giới hạn 15s cho mỗi truy vấn
                cur.execute(sql)
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.error(f'[DB] {e} | SQL: {sql[:200]}')
        raise

# ── DeepSeek ──────────────────────────────────────────────────────────────────

def _key():
    return str(getattr(settings, 'DEEPSEEK_API_KEY', '') or os.getenv('DEEPSEEK_API_KEY', '')).strip()

def _call(messages: list, json_mode=False, max_tokens=2000) -> str:
    payload = {
        'model': 'deepseek-chat',
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': max_tokens,
    }
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    r = requests.post(
        DEEPSEEK_URL, json=payload,
        headers={'Authorization': f'Bearer {_key()}', 'Content-Type': 'application/json'},
        timeout=55,
    )
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']

def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith('```'):
        lines = text.split('\n')
        text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:]).strip()
    m = re.search(r'\{.*\}', text, re.DOTALL)
    return json.loads(m.group(0) if m else text)

# ── Main endpoint ──────────────────────────────────────────────────────────────

@api_view(['POST'])
def analytics_chat(request):
    """POST /api/chat/analytics/"""
    message = (request.data.get('message') or '').strip()
    history = request.data.get('history') or []
    if not message:
        return Response({'error': 'message is required'}, status=400)
    if not _key():
        return Response({'error': 'DEEPSEEK_API_KEY not configured'}, status=503)

    t0 = time.time()
    try:
        cached_resp = _get_cached_answer(message)
        if cached_resp:
            logger.info(f'[Analytics Cache Hit] {message[:50]}')
            return Response(cached_resp)
        now = datetime.now(TZ_VN)
        cy, cm = now.year, now.month
        py = cy if cm > 1 else cy - 1
        pm = cm - 1 if cm > 1 else 12

        meta = _get_metadata()
        teams_ads    = meta['teams_ads']
        teams_social = meta['teams_social']
        dr           = meta['dr']
        snap_dr      = meta['snap_dr']
        skills       = _load_skills(message)

        # Mô tả khoảng snapshot để đưa vào prompt
        if snap_dr.get('max_y'):
            snap_range = f"{snap_dr.get('min_m')}/{snap_dr.get('min_y')}→{snap_dr.get('max_m')}/{snap_dr.get('max_y')}"
        else:
            snap_range = "chưa có (chưa chạy lock_monthly_report.py)"

        recent = '\n'.join(
            f"{'User' if h['role']=='user' else 'AI'}: {h['content'][:200]}"
            for h in history[-4:]
        )

        # ── Prompt duy nhất: routing + SQL + analysis trong 1 call ───────────
                # Dynamic Schema
        q_lower = message.lower()
        is_ads = any(kw in q_lower for kw in ['ads', 'quảng cáo', 'chi tiêu', 'camp', 'spend'])
        is_social = any(kw in q_lower for kw in ['video', 'view', 'kênh', 'tương tác', 'follower', 'like', 'share', 'mạng xã hội'])
        if not is_ads and not is_social:
            is_ads = True
            is_social = True

        ads_schema = f"ads_campaign_stats: spend, impressions, mess_count, like_count, clicks, cost_per_mess, camp_type, content_type, team, owner, year, month. Teams: {', '.join(teams_ads[:15])}" if is_ads else ""
        social_schema = f"""social_video_snapshot: title, video_url, views_locked, likes_locked, comments_locked, shares_locked, followers_locked, channel_name, platform, team, owner, report_year, report_month, locked_at
  ✅ Dữ liệu ĐÃ CHỐT ngày 7 hàng tháng — CHÍNH XÁC & CÔNG BẰNG. Có snapshot: {snap_range}
  ⚠️ DÙNG BẢNG NÀY khi hỏi tháng đã kết thúc (report_year/report_month thay vì year/month)

social_video_report: title, video_url, views, likes, comments, shares, followers, channel_name (tên kênh), platform(facebook|instagram), team, owner (tên nhân viên), year, month, is_active(bool), deleted_at(timestamp). Dữ liệu: {dr.get('min_m')}/{dr.get('min_y')}→{dr.get('max_m')}/{dr.get('max_y')}. Teams: {', '.join(teams_social[:15])}
  ⚠️ CHỈ dùng bảng này khi hỏi tháng hiện tại ({cm}/{cy}). Luôn thêm is_active=TRUE

huyk_channels: name, platform, team_traffic, owner, status""" if is_social else ""

        schema_text = "\n\n".join(filter(None, [ads_schema, social_schema]))

        prompt = f"""Bạn là VCB Studio AI Analyst. Truy cập database PostgreSQL:

{schema_text}

Hôm nay: {now.day}/{cm}/{cy} | "tháng này"={cm}/{cy} | "tháng trước"={pm}/{py}
SQL: dùng LOWER(), LIMIT 20
{f'Kiến thức: {skills[:2000]}' if skills else ''}

QUY TẮC CHỌN BẢNG (BẮT BUỘC):
- Hỏi tháng {cm}/{cy} (tháng hiện tại, đang chạy):
    → query social_video_report WHERE is_active=TRUE AND year={cy} AND month={cm}
- Hỏi tháng < {cm}/{cy} (tháng đã kết thúc):
    → query social_video_snapshot WHERE report_year=Y AND report_month=M
    → KHÔNG dùng social_video_report cho tháng đã qua (số chưa chốt, không công bằng)

VÍ DỤ SQL ĐÚNG:
  Tháng đã qua: SELECT platform, SUM(views_locked) FROM social_video_snapshot WHERE report_year={py} AND report_month={pm} GROUP BY platform
  Tháng hiện tại: SELECT platform, SUM(views) FROM social_video_report WHERE is_active=TRUE AND year={cy} AND month={cm} GROUP BY platform

KHÔNG GROUP BY tất cả các cột — chỉ GROUP BY team/platform/camp_type.

Lịch sử: {recent}
Câu hỏi: {message}
"""

        agent_system = prompt + f"""
QUAN TRỌNG DÀNH CHO AGENT:
1. Nhiệm vụ của bạn là dùng công cụ `query_postgres` để tìm dữ liệu. 
2. NẾU CÂU HỎI MƠ HỒ (VD: "so sánh team k1 và team japan" mà không nói rõ là số liệu QUẢNG CÁO hay VIDEO): KHÔNG ĐƯỢC tự ý đoán bảng. Hãy hỏi ngược lại người dùng (VD: "Bạn muốn xem số liệu về Tiền Quảng Cáo hay Lượt View Video?").
3. BẮT BUỘC: LUÔN PHẢI THÊM ĐIỀU KIỆN `year={cy} AND month={cm}` (hoặc report_year/report_month) vào mệnh đề WHERE. KHÔNG ĐƯỢC QUÊN! Nếu quên, server sẽ bị sập (timeout).
4. NẾU LỖI HOẶC 0 DÒNG: Tự động thử lại với câu lệnh khác (VD: thay `=` bằng `ILIKE '%...%'`). NẾU BỊ LỖI TIMEOUT, CHẮC CHẮN BẠN ĐÃ QUÊN FILTER THEO THÁNG NĂM!
5. KHÔNG trả về kết quả cho user cho đến khi bạn lấy được dữ liệu hoặc đã thử 3 lần.
6. Nếu câu hỏi chỉ là chào hỏi/trò chuyện hoặc hỏi ngược lại người dùng để làm rõ, KHÔNG cần gọi hàm `query_postgres`, hãy trả lời luôn.
"""

        client = openai.OpenAI(api_key=_key(), base_url="https://api.deepseek.com", timeout=30.0)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "query_postgres",
                    "description": "Thực thi truy vấn SQL để lấy dữ liệu. Luôn dùng ILIKE cho chuỗi.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sql": {
                                "type": "string",
                                "description": "Câu lệnh SQL hoàn chỉnh (PostgreSQL)"
                            }
                        },
                        "required": ["sql"]
                    }
                }
            }
        ]

        messages = [
            {'role': 'system', 'content': agent_system},
            {'role': 'user', 'content': f"Hãy tìm dữ liệu cho câu hỏi này: {message}"}
        ]

        last_rows = []
        is_sql = False
        final_message = ""

        for step in range(2):
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                tools=tools,
                temperature=0.1
            )
            msg = resp.choices[0].message

            if msg.tool_calls:
                is_sql = True
                messages.append(msg)
                for tc in msg.tool_calls:
                    if tc.function.name == "query_postgres":
                        args = json.loads(tc.function.arguments)
                        sql = args.get('sql', '').strip()
                        logger.info(f"[Agent SQL Step {step+1}] {sql[:200]}")
                        
                        try:
                            rows = _query(sql)
                            last_rows = rows
                            content = f"Thành công ({len(rows)} dòng). Mẫu 5 dòng đầu: {json.dumps(rows[:5], ensure_ascii=False, default=str)}"
                        except Exception as e:
                            err_msg = str(e)[:500]
                            if "timeout" in err_msg.lower():
                                content = f"LỖI SQL: {err_msg}. Dữ liệu quá lớn gây timeout! BẠN PHẢI THÊM `year={cy} AND month={cm}` VÀO WHERE ĐỂ LỌC VÀ THỬ LẠI!"
                            else:
                                content = f"LỖI SQL: {err_msg}. Hãy sửa lỗi và thử lại!"
                            
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": content
                        })
            else:
                final_message = msg.content
                break

        # Nếu không bao giờ gọi SQL, nghĩa là chat thuần tuý
        if not is_sql:
            logger.info(f'[Chat Agent] {time.time()-t0:.1f}s')
            result = {'message': final_message, 'dashboard': None, 'suggestions': []}
            _set_cached_answer(message, result)
            return Response(result)

        rows = last_rows

        if not rows:
            suggestions = [
                f'Báo cáo ads tháng {dr.get("max_m")}/{dr.get("max_y")}',
                f'Top kênh view tháng {dr.get("max_m")}/{dr.get("max_y")}',
                'So sánh hiệu suất các team',
            ]
            return Response({
                'message': f'Không tìm thấy dữ liệu (tháng {cm}/{cy}) cho yêu cầu này. Đã thử tìm kiếm nhiều lần nhưng không có.',
                'dashboard': None,
                'suggestions': suggestions,
            })

        # Phân tích + dashboard — lần gọi thứ 2
        keys = list(rows[0].keys())
        analysis_prompt = f"""Câu hỏi: {message}
Dữ liệu ({len(rows)} dòng, keys: {keys}):
{json.dumps(rows[:25], ensure_ascii=False, default=str)}

Trả về JSON với format sau (FE dùng trực tiếp):
{{
  "message": "<phân tích 2-4 câu tiếng Việt, có nhận xét + đề xuất cụ thể>",
  "dashboard": {{
    "layout": "mixed",
    "blocks": [
      {{"type": "kpi_card", "data": [{{"label": "...", "value": "..."}}]}},
      {{"type": "table", "title": "...", "columns": ["<key tiếng Việt>", ...], "data": [{{"<key>": "..."}}]}}
    ]
  }}
}}
Quy tắc bắt buộc:
- columns trong table phải là tên tiếng Việt và KHỚP ĐÚNG key trong data objects
- Format số trong table/kpi_card: 1200000→"1.2M", 45000→"45K", tiền→"29.5M đ"
- KHÔNG dùng null/N/A/undefined
- Nếu ≥3 nhóm so sánh → thêm block bar chart: {{"type":"bar","title":"...","xKey":"<key>","yKey":"<key>","data":[...]}}
- QUAN TRỌNG: Giá trị yKey trong bar/line chart data PHẢI là số nguyên (raw number), KHÔNG được format thành string. Ví dụ đúng: {{"Nền tảng":"Facebook","Tổng views":5900000}}. Ví dụ SAI: {{"Tổng views":"5.9M"}}
- LƯU Ý: Nếu người dùng hỏi về một người (vd: "Tuấn Dũng"), đó là tên nhân viên (cột `owner`), ĐỪNG nhầm lẫn với tên kênh (`channel_name`). Hãy trả lời tập trung vào người đó.
Chỉ JSON."""

        raw2 = _call([
            {'role': 'system', 'content': 'Chỉ trả về JSON.'},
            {'role': 'user', 'content': analysis_prompt},
        ], json_mode=True, max_tokens=2500)

        try:
            result = _parse_json(raw2)
        except Exception:
            result = {
                'message': raw2[:500],
                'dashboard': {
                    'layout': 'mixed',
                    'blocks': [{'type': 'table', 'title': 'Kết quả', 'columns': keys, 'data': rows}]
                },
            }


        result.setdefault('suggestions', [])
        result.setdefault('dashboard', None)
        _set_cached_answer(message, result)
        logger.info(f'[Analytics] {time.time()-t0:.1f}s | rows={len(rows)}')
        return Response(result)

    except Exception as e:
        logger.error(f'[Analytics Chat] {e}', exc_info=True)
        return Response({'message': 'Xin lỗi, có lỗi xảy ra. Vui lòng thử lại.', 'dashboard': None, 'suggestions': []})

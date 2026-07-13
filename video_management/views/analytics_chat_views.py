"""
Analytics Chatbot endpoint.
Endpoint: POST /api/chat/analytics/
Tối ưu: 1 lần gọi DeepSeek duy nhất, cache metadata DB, giảm context.
"""
import os, re, json, logging, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
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
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT team FROM ads_campaign_stats WHERE team IS NOT NULL ORDER BY team")
            teams_ads = [r['team'] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT team FROM social_video_report WHERE team IS NOT NULL ORDER BY team")
            teams_social = [r['team'] for r in cur.fetchall()]
            cur.execute("SELECT MIN(year) min_y, MIN(month) min_m, MAX(year) max_y, MAX(month) max_m FROM social_video_report")
            dr = dict(cur.fetchone() or {})
        conn.close()
        _meta_cache = {'teams_ads': teams_ads, 'teams_social': teams_social, 'dr': dr}
        _meta_ts = time.time()
    except Exception as e:
        logger.error(f'[Meta cache] {e}')
        _meta_cache = {'teams_ads': [], 'teams_social': [], 'dr': {}}
    return _meta_cache

def _query(sql: str) -> list:
    try:
        conn = psycopg2.connect(_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
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
        timeout=30,
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
        now = datetime.now(TZ_VN)
        cy, cm = now.year, now.month
        py = cy if cm > 1 else cy - 1
        pm = cm - 1 if cm > 1 else 12

        meta = _get_metadata()
        teams_ads    = meta['teams_ads']
        teams_social = meta['teams_social']
        dr           = meta['dr']
        skills       = _load_skills(message)

        recent = '\n'.join(
            f"{'User' if h['role']=='user' else 'AI'}: {h['content'][:200]}"
            for h in history[-4:]
        )

        # ── Prompt duy nhất: routing + SQL + analysis trong 1 call ───────────
        prompt = f"""Bạn là VCB Studio AI Analyst. Truy cập database PostgreSQL:

ads_campaign_stats: spend, impressions, mess_count, like_count, clicks, cost_per_mess, camp_type, content_type, team, owner, year, month. Teams: {', '.join(teams_ads[:15])}
social_video_report: views, likes, comments, shares, followers, channel_name, platform(facebook|instagram), team, owner, year, month, is_active(bool), deleted_at(timestamp). Dữ liệu: {dr.get('min_m')}/{dr.get('min_y')}→{dr.get('max_m')}/{dr.get('max_y')}. Teams: {', '.join(teams_social[:15])}
  - is_active=TRUE: video còn tồn tại | is_active=FALSE: đã bị xóa khỏi platform
  - Mặc định query: WHERE is_active=TRUE. Trừ khi hỏi về "video bị xóa" mới dùng is_active=FALSE
huyk_channels: name, platform, team_traffic, owner, status

Hôm nay: {now.day}/{cm}/{cy} | "tháng này"={cm}/{cy} | "tháng trước"={pm}/{py}
SQL: dùng LOWER(), LIMIT 20, chỉ year+month (không có cột day)
{f'Kiến thức: {skills[:2000]}' if skills else ''}

Lịch sử: {recent}
Câu hỏi: {message}

Nếu câu hỏi cần dữ liệu DB → trả về JSON: {{"need_sql": true, "sql": "<SQL hoàn chỉnh>"}}
Nếu là hội thoại/meta → trả về JSON: {{"need_sql": false, "reply": "<trả lời tiếng Việt>"}}
Chỉ JSON, không giải thích."""

        raw = _call([
            {'role': 'system', 'content': 'Chỉ trả về JSON.'},
            {'role': 'user', 'content': prompt},
        ], json_mode=False, max_tokens=600)

        try:
            step1 = _parse_json(raw)
        except Exception:
            return Response({'message': raw, 'dashboard': None, 'suggestions': []})

        # Chat thuần → trả luôn
        if not step1.get('need_sql'):
            logger.info(f'[Chat] {time.time()-t0:.1f}s')
            return Response({'message': step1.get('reply', ''), 'dashboard': None, 'suggestions': []})

        # Chạy SQL
        sql = step1.get('sql', '').strip()
        if not sql:
            return Response({'message': 'Không thể tạo câu truy vấn.', 'dashboard': None, 'suggestions': []})

        try:
            rows = _query(sql)
        except Exception as e:
            return Response({'message': f'Lỗi truy vấn: {str(e)[:200]}', 'dashboard': None, 'suggestions': []})

        if not rows:
            suggestions = [
                f'Báo cáo ads tháng {dr.get("max_m")}/{dr.get("max_y")}',
                f'Top kênh view tháng {dr.get("max_m")}/{dr.get("max_y")}',
                'So sánh hiệu suất các team',
            ]
            return Response({
                'message': f'Không tìm thấy dữ liệu (tháng {cm}/{cy}). Dữ liệu có từ {dr.get("min_m")}/{dr.get("min_y")} → {dr.get("max_m")}/{dr.get("max_y")}.',
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
- Format số: 1200000→"1.2M", 45000→"45K", tiền→"29.5M đ"
- KHÔNG dùng null/N/A/undefined
- Nếu ≥3 nhóm so sánh → thêm block bar chart: {{"type":"bar","title":"...","xKey":"<key>","yKey":"<key>","data":[...]}}
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
        logger.info(f'[Analytics] {time.time()-t0:.1f}s | rows={len(rows)}')
        return Response(result)

    except Exception as e:
        logger.error(f'[Analytics Chat] {e}', exc_info=True)
        return Response({'message': 'Xin lỗi, có lỗi xảy ra. Vui lòng thử lại.', 'dashboard': None, 'suggestions': []})

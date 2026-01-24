# AutomationGenVideo_AI

Django REST Framework backend cho hệ thống tự động hóa tạo video từ TikTok data.

## 🚀 Tính năng

- Scrape video data từ TikTok (sử dụng TikHub API)
- Quản lý tracked channels
- Tìm kiếm video theo keyword, likes, views
- Download video URLs
- Background tasks với Celery
- REST API với Django REST Framework

## 📋 Yêu cầu hệ thống

- Python 3.9+
- Redis (cho Celery background tasks)
- SQLite (mặc định) hoặc PostgreSQL (production)

## 🛠️ Cài đặt

### 1. Tạo virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 2. Cài đặt dependencies

```bash
pip install -r requirements.txt
```

### 3. Cấu hình environment variables

Sao chép file `.env.example` thành `.env`:

```bash
# Windows
copy .env.example .env

# Linux/Mac
cp .env.example .env
```

Chỉnh sửa file `.env` với các giá trị của bạn:

```env
# Django
SECRET_KEY=your-secret-key-here
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# TikHub API
TIKHUB_API_KEY=your-tikhub-api-key

# Celery
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

# Telegram (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### 4. Chạy migrations

```bash
python manage.py migrate
```

### 5. Tạo superuser (tùy chọn)

```bash
python manage.py createsuperuser
```

## 🎮 Chạy ứng dụng

### Development server

```bash
python manage.py runserver
```

Server sẽ chạy tại `http://localhost:8000`

### Celery Worker (cho background tasks)

Trong terminal riêng:

```bash
celery -A core worker --loglevel=info
```

### Celery Beat (cho scheduled tasks)

Trong terminal riêng:

```bash
celery -A core beat --loglevel=info
```

## 📡 API Endpoints

### Root
- `GET /` - API information và danh sách endpoints

### Channels
- `GET /api/channels/` - Lấy danh sách tracked channels
- `POST /api/channels/` - Tạo tracked channel mới
- `GET /api/channels/{id}/` - Chi tiết channel
- `PUT /api/channels/{id}/` - Cập nhật channel
- `DELETE /api/channels/{id}/` - Xóa channel

### Search
- `POST /api/search` - Tìm kiếm video
  ```json
  {
    "keyword": "string (required)",
    "min_likes": "integer (optional)",
    "min_views": "integer (optional)",
    "sort_by": "likes|views (optional)"
  }
  ```
- `GET /api/search/status/{task_id}` - Kiểm tra trạng thái search task

### Music
- `POST /api/music/posts` - Lấy posts theo music ID
  ```json
  {
    "music_id": "string (required)",
    "count": "integer (optional, default: 30)",
    "cursor": "integer (optional, default: 0)"
  }
  ```

### Download
- `POST /api/download` - Lấy download URL
  ```json
  {
    "url": "string (required)"
  }
  ```

### Admin
- `GET /admin/` - Django admin panel

## 📁 Cấu trúc dự án

```
AutomationGenVideo_AI/
├── core/                  # Django project settings
│   ├── settings.py        # Cấu hình chính
│   ├── urls.py            # URL routing
│   ├── celery.py          # Celery configuration
│   ├── wsgi.py            # WSGI config
│   └── asgi.py            # ASGI config
├── video_management/      # Main Django app
│   ├── models.py          # Database models
│   ├── views.py           # API views
│   ├── serializers.py     # DRF serializers
│   ├── tasks.py           # Celery tasks
│   ├── urls.py            # App URLs
│   ├── admin.py           # Admin config
│   └── services/          # External service clients
│       ├── scraper_service.py
│       ├── telegram_utils.py
│       └── ...
├── scripts/               # Helper scripts
├── tests/                 # Test files
├── static/                # Static files
├── media/                 # User uploaded files
├── manage.py              # Django CLI
├── requirements.txt       # Python dependencies
├── .env.example           # Environment template
└── README.md              # This file
```

## 🔒 Bảo mật (Production)

⚠️ **QUAN TRỌNG**: Trước khi deploy production:

1. ✅ Đặt `DEBUG=False` trong `.env`
2. ✅ Tạo `SECRET_KEY` mới và bảo mật
3. ✅ Cấu hình `ALLOWED_HOSTS` đúng với domain
4. ✅ Đặt `CORS_ALLOW_ALL_ORIGINS=False`
5. ✅ Cấu hình `CORS_ALLOWED_ORIGINS` với domain cụ thể
6. ✅ Sử dụng PostgreSQL thay vì SQLite
7. ✅ Cấu hình HTTPS
8. ✅ Sử dụng environment variables cho sensitive data
9. ✅ Cấu hình proper logging
10. ✅ Setup monitoring và error tracking

## 🐳 Docker

```bash
# Build image
docker build -t automation-gen-video-ai .

# Run container
docker run -p 8000:8000 automation-gen-video-ai
```

## 🧪 Testing

```bash
python manage.py test
```

## 📝 License

Private project - All rights reserved

## 👥 Team

VietChiBao Project Team
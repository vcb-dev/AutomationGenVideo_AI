# CI/CD Deploy AutomationGenVideo_AI (Docker Hub → Railway)

Railway chạy image **`viejhaf/automationgenvideo-ai:latest`** (không build từ GitHub).

```text
push main
  → GitHub Actions: docker build (Dockerfile.railway, linux/amd64) → push Docker Hub
  → railway redeploy web (+ worker, beat nếu có)  (kéo lại :latest)
```

## GitHub Secrets

| Secret | Bắt buộc | Giá trị |
|--------|----------|---------|
| `RAILWAY_TOKEN` | ✅ | Project Token (Railway → Project → Settings → Tokens) |
| `RAILWAY_SERVICE_WEB` | ✅ | Service ID của service web (gunicorn) |
| `RAILWAY_SERVICE_WORKER` | ⬜ | Service ID celery worker (nếu tách service) |
| `RAILWAY_SERVICE_BEAT` | ⬜ | Service ID celery beat (nếu tách service) |
| `DOCKERHUB_USERNAME` | ✅ | Username Docker Hub (vd `viejhaf`) |
| `DOCKERHUB_TOKEN` | ✅ | Access Token Docker Hub (Account Settings → Security) |

## Workflows

| File | Khi chạy |
|------|----------|
| `ci.yml` | PR / push `main`,`truqhieu` — docker build (không push) để verify |
| `deploy-railway.yml` | Push `main` hoặc **Actions → Deploy Railway → Run workflow** |

### Test thủ công
1. Actions → **Deploy Railway** → **Run workflow**
2. `skip_docker_push` = `false` (build + push + redeploy) hoặc `true` (chỉ redeploy Railway)

## Railway (dùng chung 1 image cho 3 service)

Tất cả service trỏ **Source Image**: `viejhaf/automationgenvideo-ai:latest`, chỉ khác **Custom Start Command**:

| Service | Start Command | Ghi chú |
|---------|---------------|---------|
| web | *(mặc định theo Dockerfile)* `gunicorn core.wsgi:application --bind 0.0.0.0:$PORT ...` | Healthcheck `/api/health/` |
| worker | `celery -A core worker -l info` | |
| beat | `celery -A core beat -l info` | |

- **Redis**: thêm Railway Redis plugin, gán `REDIS_URL` / `CELERY_BROKER_URL`.
- **Env bắt buộc**: `SECRET_KEY`, `DEBUG=False`, `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET` (khớp BE), `ALLOWED_HOSTS` (thêm domain Railway/Cloudflare), các API key (Minimax, OpenAI, HeyGen…). Xem `.env`.
- `.env` đã được đưa vào `.dockerignore` nên **không** bị bake vào image — Railway dùng biến env riêng.

## Ghi chú
- Dockerfile deploy Railway là **`Dockerfile.railway`** (self-contained, bind `$PORT`). Flow NAS/compose vẫn dùng `./Dockerfile` như cũ.
- `git` được cài trong image (cần cho `yt-dlp` từ GitHub trong `requirements_cloud.txt`).

# Production Deploy

This setup runs:

- `frontend`: React build served by nginx
- `backend`: FastAPI
- `postgres`: PostgreSQL with persistent Docker volume

## 1. Configure environment

Copy the example file:

```powershell
Copy-Item .env.prod.example .env.prod
```

Edit `.env.prod` and set strong values:

```env
POSTGRES_PASSWORD=...
AI_AUDITOR_AUTH_SECRET=...
SETUP_INTEGRATIONS_TOKEN=...
FRONTEND_PORT=8080
```

## 2. Start services

```powershell
docker compose --env-file .env.prod up -d --build
```

Open:

```text
http://localhost:8080
```

## 3. Create admin

```powershell
docker compose --env-file .env.prod exec backend python setup_auth_user.py `
  --username admin@example.com `
  --tenant default `
  --role admin
```

## 4. Create a client tenant

```powershell
docker compose --env-file .env.prod exec backend python setup_tenant_integrations.py `
  --tenant sapaplast `
  --name "Sapaplast" `
  --bitrix-webhook "https://..." `
  --whatsapp-webhook "https://..." `
  --openai-key "sk-..."
```

```powershell
docker compose --env-file .env.prod exec backend python setup_auth_user.py `
  --username client@sapaplast.kz `
  --tenant sapaplast `
  --role client
```

## 5. Backups

Back up PostgreSQL:

```powershell
docker compose --env-file .env.prod exec postgres pg_dump -U ai_auditor ai_auditor > backup.sql
```

Also back up:

```text
storage/
```

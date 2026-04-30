FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY setup_auth_user.py setup_tenant_integrations.py ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[api,postgres]"

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "bitrix_ingest.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12.3-slim
ARG GIT_VERSION_HASH

WORKDIR /app
COPY requirements.lock ./
RUN PYTHONDONTWRITEBYTECODE=1 pip install --no-cache-dir -r requirements.lock

COPY app .
ENV GIT_VERSION_HASH=${GIT_VERSION_HASH}
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

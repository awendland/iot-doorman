FROM python:3.12.3-slim

WORKDIR /app
COPY requirements.lock ./
RUN PYTHONDONTWRITEBYTECODE=1 pip install --no-cache-dir -r requirements.lock

COPY app .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

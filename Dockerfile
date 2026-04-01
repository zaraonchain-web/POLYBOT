FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "apps/run_flash_crash.py", "--coin", "BTC", "--size", "1.0"]

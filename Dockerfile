FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY integration ./integration
COPY contracts ./contracts
COPY sql ./sql
RUN useradd --uid 10001 --no-create-home appuser
USER 10001:10001
EXPOSE 8791
CMD ["python","-m","uvicorn","integration.api:app","--host","0.0.0.0","--port","8791","--workers","1","--limit-concurrency","32","--timeout-keep-alive","5"]

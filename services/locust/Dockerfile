FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir locust requests

COPY services/locust/locustfile.py /app/locustfile.py

EXPOSE 8089

CMD ["locust", "-f", "/app/locustfile.py", "--host=http://orchestrator:5000"]

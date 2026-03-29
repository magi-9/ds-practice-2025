FROM python:3.9-slim

WORKDIR /app

COPY services/order_queue/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pre-generated proto files
COPY services/order_queue/src/order_queue/ src/order_queue/

COPY services/order_queue/src src/

CMD ["python", "src/app.py"]

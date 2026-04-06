FROM python:3.11

WORKDIR /app

COPY services/order_queue/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pre-generated proto files
COPY services/order_queue/src/order_queue/ src/order_queue/

COPY services/order_queue/src src/

CMD python utils/other/hotreload.py "services/order_queue/src/app.py"

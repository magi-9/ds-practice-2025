FROM python:3.11

WORKDIR /app

COPY ./services/order_executor/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

CMD python services/order_executor/src/app.py
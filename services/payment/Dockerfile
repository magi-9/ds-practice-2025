FROM python:3.11

WORKDIR /app

COPY ./services/payment/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

CMD python utils/other/hotreload.py "services/payment/src/app.py"

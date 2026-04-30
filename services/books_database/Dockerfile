FROM python:3.11

WORKDIR /app

COPY ./services/books_database/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

CMD python services/books_database/src/app.py
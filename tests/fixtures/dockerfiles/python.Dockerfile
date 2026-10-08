FROM python:3.4-slim
MAINTAINER migration-framework
LABEL project="web-portal"

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 80 443 8080

CMD ["gunicorn", "app:app"]

FROM python:3.4

ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN curl -fsSL -o /tmp/tf.zip \
        https://releases.hashicorp.com/terraform/0.6.3/terraform_0.6.3_linux_amd64.zip \
    && echo "0160fcdb7f0d00948d52912df0626a2e49db958b6df2c6108cbd8b3527ce1144  /tmp/tf.zip" | sha256sum -c - \
    && unzip -o /tmp/tf.zip -d /usr/local/bin \
    && rm /tmp/tf.zip

COPY requirements*.txt ./
RUN pip install --no-cache-dir --no-deps ply==3.4
RUN pip install --no-cache-dir --no-deps -r requirements.txt -r requirements-dev.txt

COPY . .

ENTRYPOINT ["python", "src/migration_engine.py"]

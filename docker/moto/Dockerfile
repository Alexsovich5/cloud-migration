FROM python:3.4

# moto builds its Flask url map from a dict, so rule order follows the string
# hash seed. With some seeds the s3bucket_path rule "/<bucket>/" is tried
# before "/<bucket>", and PUT /<bucket> fails with FormDataRoutingRedirect (500).
# A fixed seed makes the order deterministic; 0 gives the working order.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.txt

# moto 0.4.14, pinned to the commit the tag points at.
RUN curl -fsSL -o /tmp/moto.tar.gz \
        https://github.com/getmoto/moto/archive/967c778390a8d5991a475fa92856e627d3116b1b.tar.gz \
    && echo "ddf4f4c72dc614c29475f811bdb25e30b2039baa56a628af63bc0284ad433445  /tmp/moto.tar.gz" | sha256sum -c - \
    && pip install --no-cache-dir --no-deps /tmp/moto.tar.gz \
    && rm /tmp/moto.tar.gz

ENTRYPOINT ["moto_server"]

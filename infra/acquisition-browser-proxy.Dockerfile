FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/browser-egress-proxy
COPY sidecars/browser_egress_proxy/server.py /srv/browser-egress-proxy/server.py

RUN useradd --system --uid 10001 --create-home proxy \
    && chown -R 10001:10001 /srv/browser-egress-proxy

USER 10001:10001
EXPOSE 8791
ENTRYPOINT ["python", "/srv/browser-egress-proxy/server.py"]

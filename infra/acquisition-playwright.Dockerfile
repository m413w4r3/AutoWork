FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /srv/playwright-acquirer
COPY sidecars/playwright_acquirer/server.py /srv/playwright-acquirer/server.py

RUN useradd --system --uid 10001 --create-home renderer \
    && chown -R 10001:10001 /srv/playwright-acquirer

USER 10001:10001
EXPOSE 8790
ENTRYPOINT ["python", "/srv/playwright-acquirer/server.py"]

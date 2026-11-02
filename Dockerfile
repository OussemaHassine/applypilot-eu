# ApplyPilot (EU edition) — web UI + full pipeline in one container.
#   docker compose up -d          then open http://localhost:8765 (user: any, password: $APPLYPILOT_UI_PASSWORD)
FROM python:3.12-slim

ARG WITH_LATEX=1
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    APPLYPILOT_DIR=/data \
    CHROME_PATH=/usr/bin/chromium \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium zsh procps sqlite3 curl ca-certificates gnupg fonts-dejavu fonts-liberation \
    && if [ "$WITH_LATEX" = "1" ]; then apt-get install -y --no-install-recommends \
        texlive-latex-base texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended lmodern; fi \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code @playwright/mcp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY daily_run.sh ./
COPY examples ./
RUN pip install --no-cache-dir -e . \
    && pip install --no-cache-dir --no-deps python-jobspy \
    && pip install --no-cache-dir tls-client requests markdownify regex curl_cffi beautifulsoup4 \
    && python -m playwright install --with-deps chromium \
    && printf '#!/bin/sh\nexec python -m applypilot.cli "$@"\n' > /usr/local/bin/applypilot && chmod +x /usr/local/bin/applypilot

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
VOLUME ["/data"]
EXPOSE 8765
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["applypilot", "ui", "--host", "0.0.0.0", "--port", "8765", "--no-open"]

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY gpusim ./gpusim

RUN useradd --system --no-create-home simulator
USER simulator

EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/healthz', timeout=2)"

CMD ["python", "-m", "gpusim"]

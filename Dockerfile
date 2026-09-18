FROM python:3.12-slim-bookworm

# fonts-dejavu-core no es opcional: report.py busca DejaVuSans-Bold.ttf por
# nombre para numerar los marcadores de los planos, y sin ella Pillow cae a su
# fuente de mapa de bits y los números salen ilegibles en el Excel.
# tini reparte las señales, para que gunicorn pare limpio en un redespliegue.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates fonts-dejavu-core tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

# Explícito y no `*.py`: desktop.py, main.py y los run*.sh son de la versión de
# escritorio y de la etapa CLI anterior, y no pintan nada en el servidor.
COPY app.py casambi_api.py cobertura.py config.py credentials.py report.py wsgi.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY logos/ ./logos/

ENV CASAMBI_HOME=/data \
    CASAMBI_MODE=web \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Los datos escribibles (anotaciones, planos, informes, credenciales cifradas)
# viven en el volumen, nunca en la imagen.
RUN useradd --uid 1000 --create-home casambi \
 && mkdir -p /data && chown -R casambi:casambi /data /app
USER casambi
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "8", \
     "--timeout", "300", "--graceful-timeout", "30", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "wsgi:application"]

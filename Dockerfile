FROM python:3.13.9-alpine3.22
RUN adduser -D -u 1000 app && mkdir -p /data /config && chown app /data
COPY app /app
USER app
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
CMD ["python", "-I", "/app/server.py"]

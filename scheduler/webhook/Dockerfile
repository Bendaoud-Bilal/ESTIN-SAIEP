FROM python:3.12-slim
WORKDIR /app
COPY app.py /app/app.py
USER 10001:10001
EXPOSE 8443
ENTRYPOINT ["python", "-u", "/app/app.py"]

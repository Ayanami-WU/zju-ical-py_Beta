FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt \
    && groupadd --gid 10001 zjuical \
    && useradd --uid 10001 --gid 10001 --no-create-home zjuical \
    && mkdir /data && chown 10001:10001 /data
COPY course course
COPY exam exam
COPY ical ical
COPY main main
COPY todos todos
COPY utils utils
COPY zjuam zjuam
COPY server server
COPY configs configs
COPY LICENSE README.md ./
USER 10001:10001
EXPOSE 5273
CMD ["python", "-m", "server"]

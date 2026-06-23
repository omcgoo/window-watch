FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY window_watch.py runner.py ./
RUN mkdir -p /data
CMD ["python", "-u", "runner.py"]

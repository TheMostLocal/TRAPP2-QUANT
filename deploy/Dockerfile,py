# z163 - the always-on option: one small VPS near Alpaca's servers (Ashburn, VA / us-east) runs the engine
# all session, no 6-hour job limit, no scheduler. Build: docker build -t trapp2-quant -f deploy/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quant ./quant
ENV QUANT_BUDGET_MIN=480 PYTHONUNBUFFERED=1
CMD ["python", "-m", "quant.run"]

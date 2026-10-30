FROM python:3.12-slim

WORKDIR /srv
COPY pyproject.toml README.md ./
COPY app ./app
COPY client ./client
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["signal-gateway"]

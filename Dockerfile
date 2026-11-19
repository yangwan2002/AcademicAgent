FROM python:3.11-slim

WORKDIR /workspace

COPY pyproject.toml requirements.lock ./
COPY src ./src
COPY tests ./tests
COPY evalcaliper ./evalcaliper

RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-deps -e .

ENV PYTHONPATH=/workspace/src:/workspace \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["python", "/workspace/evalcaliper/run_case.py", \
     "--input", "/eval/input.json", \
     "--output", "/eval/output.json", \
     "--trace", "/eval/trace.jsonl"]

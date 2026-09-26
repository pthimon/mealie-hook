FROM docker.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    DEFAULT_RULES_DIR=/app/rules

WORKDIR /app
COPY pyproject.toml ./
COPY mealie_toolkit ./mealie_toolkit
RUN pip install .
# The shipped default rules. On first start they are copied to /data/rules, which is the
# live copy edited from the rules page; later image rebuilds never overwrite it.
COPY rules ./rules

USER 1000:1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "mealie_toolkit"]
CMD ["serve"]

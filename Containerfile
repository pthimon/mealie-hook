FROM docker.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PROMPTS_DIR=/app/prompts

WORKDIR /app
COPY pyproject.toml ./
COPY mealie_hook ./mealie_hook
RUN pip install .
# Prompts are read on every call, so they can also be bind-mounted to edit without a rebuild.
COPY prompts ./prompts

USER 1000:1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "mealie_hook"]
CMD ["serve"]

# Imagen del motor para la validación en una instancia real (GUIA_DOS_MAQUINAS.md): el motor y el
# subidor corren en Docker, como en producción. También trae `informe`.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# Primero las dependencias, así cambiar el código no las vuelve a instalar.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src src
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH MPLCONFIGDIR=/tmp/matplotlib
CMD ["motor", "--help"]

# ─── Imagem base ─────────────────────────────────────────────────────────────
# python:3.11.9-slim-bookworm pina Python E Debian (bookworm = Debian 12).
# Necessário porque a URL do repositório Microsoft usa o codinome do Debian.
FROM python:3.11.9-slim-bookworm

# Evita prompts interativos do apt e habilita logs Python sem buffer
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ─── ODBC Driver 18 for SQL Server ───────────────────────────────────────────
# Instala via repositório oficial Microsoft para Debian 12 (bookworm).
#
# Para descobrir a versão exata disponível após o primeiro build e pinnar:
#   docker run --rm datamart-copilot-api \
#     bash -c "apt-cache policy msodbcsql18 | head -5"
# Em seguida substitua "msodbcsql18" por "msodbcsql18=<versão>" abaixo.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      gnupg2 \
      apt-transport-https \
 && curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64,arm64,armhf signed-by=/usr/share/keyrings/microsoft-prod.gpg] \
      https://packages.microsoft.com/debian/12/prod bookworm main" \
    > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
      msodbcsql18 \
      unixodbc-dev \
 && apt-get purge -y curl gnupg2 apt-transport-https \
 && apt-get autoremove -y \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# ─── Dependências Python ─────────────────────────────────────────────────────
# Copia requirements antes do código para aproveitar cache de camadas Docker:
# uma mudança em src/ não reexecuta o pip install.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ─── Código da aplicação ─────────────────────────────────────────────────────
COPY . .

# ─── Porta e comando de produção ─────────────────────────────────────────────
# docker-compose sobrescreve o CMD para --reload em desenvolvimento.
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

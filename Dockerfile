FROM python:3.10-slim

# Evitar que Python escriba archivos .pyc y activar unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    HOME=/home/user

# Crear un usuario no root con UID 1000
RUN useradd -m -u 1000 user

WORKDIR $HOME/app

# Copiar requirements de la carpeta backend-python e instalar dependencias
COPY --chown=user backend-python/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar el contenido de backend-python al contenedor
COPY --chown=user backend-python/ .

# Asegurar permisos correctos para el usuario
RUN chmod -R 777 $HOME/app

# Exponer el puerto de Hugging Face
EXPOSE 7860

# Ejecutar FastAPI usando uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]

# TrendWatcher

TrendWatcher es una aplicación web autoalojable para vigilar productos de Stradivarius y Amazon. Revisa precio, stock, tallas, medidas, imágenes y cambios relevantes de forma automática, y avisa por email o Telegram cuando detecta una bajada de precio o una reposición.

## Qué puedes hacer

- Guardar enlaces de productos de Stradivarius y Amazon.
- Ver un panel personal con productos propios, estado de stock, precio, tallas, medidas, imágenes y última revisión.
- Filtrar productos por tienda, disponibilidad y búsqueda por nombre, referencia o código.
- Consultar el histórico de lecturas de cada producto.
- Activar o desactivar alertas por producto: bajada de precio y reposición.
- Crear usuarios nuevos desde registro público o desde el panel admin.
- Recuperar contraseña por email.
- Configurar SMTP y Telegram desde el panel admin.
- Ejecutar revisión manual como admin.
- Mantener un escaneo automático cada 5 minutos.

## Captura funcional

La interfaz está pensada como un panel de seguimiento: tarjetas de producto, filtros rápidos, detalle con histórico y un área de administración para catálogo, usuarios, eventos y notificaciones.

## Tecnologías

- Python 3
- Flask
- SQLite
- Playwright
- playwright-stealth
- APScheduler
- Gunicorn
- Docker Compose

## Estructura principal

```text
.
├── app.py                    # Web Flask, usuarios, rutas, panel y alertas
├── stradivarius_monitor.py   # Motor de lectura de productos
├── templates/                # Pantallas HTML
├── static/                   # CSS y JavaScript
├── data/                     # Base de datos y logs persistentes
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Despliegue con Docker

Arrancar o reconstruir:

```bash
docker compose up -d --build
```

Ver logs:

```bash
docker logs -f stradivarius-monitor
```

Parar:

```bash
docker compose down
```

La aplicación queda publicada en el puerto configurado en `docker-compose.yml`. En el despliegue actual:

```text
http://192.168.1.66:8081
```

## Configuración

Crea un archivo `.env` a partir de `.env.example` y revisa estas variables:

```text
ADMIN_USERNAME=admin
ADMIN_PASSWORD=cambia-esta-contrasena
SECRET_KEY=clave-segura
DATA_DIR=/data
DATABASE_PATH=/data/stradivarius_monitor.db
CHECK_INTERVAL_MINUTES=5
EMAIL_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
EMAIL_FROM=
EMAIL_TO=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

También puedes cambiar la mayoría de opciones desde el panel admin.

## Email y recuperación de contraseña

Para Gmail necesitas una contraseña de aplicación. No uses la contraseña normal de la cuenta.

En el panel admin puedes:

- Activar o desactivar email.
- Configurar servidor SMTP.
- Guardar usuario, contraseña, remitente y destinatario por defecto.
- Enviar un email de prueba.

Si el campo `SMTP contraseña` se deja vacío al guardar la configuración, TrendWatcher conserva la contraseña que ya estaba guardada.

## Telegram

Para activar Telegram necesitas:

- Token del bot.
- Chat ID de destino.

Ambos se configuran desde `.env` o desde el panel admin.

## Uso

1. Entra con el usuario admin inicial.
2. Configura email o Telegram si quieres alertas.
3. Añade productos desde el panel principal o desde admin.
4. Espera la primera lectura automática o lanza una revisión manual como admin.
5. Usa filtros por tienda, disponibilidad y búsqueda para organizar la vista.
6. Entra en el histórico de un producto para ver sus lecturas anteriores.

## URLs soportadas

TrendWatcher acepta enlaces completos o incompletos. Por ejemplo:

```text
https://www.amazon.es/dp/B005I5M2F8
amazon.es/Mattel-Games-classic-cartas-W2087/dp/B005I5M2F8
https://www.stradivarius.com/es/producto-l01234567
```

Los enlaces de Amazon se normalizan automáticamente a una URL limpia por ASIN:

```text
https://amazon.es/dp/B005I5M2F8
```

Esto evita errores de navegación, duplicados y enlaces demasiado largos con parámetros de seguimiento.

## Datos persistentes

Los datos se guardan en:

```text
data/stradivarius_monitor.db
data/app.log
```

No borres la carpeta `data` si quieres conservar usuarios, productos, histórico y configuración.

## Mantenimiento

Comprobar estado del contenedor:

```bash
docker ps --filter name=stradivarius-monitor
```

Comprobar salud:

```bash
curl http://localhost:8081/health
```

Recrear la app tras cambios:

```bash
docker compose up -d --build
```

## Notas importantes

- Amazon y Stradivarius pueden limitar o bloquear lecturas automatizadas en algunos momentos.
- Si una tienda bloquea una lectura, el producto queda marcado como bloqueado en el panel.
- El escaneo automático evita que los usuarios normales tengan que lanzar revisiones manuales.
- Solo el admin puede ejecutar una revisión manual global.

## Seguridad

- No subas `.env` a GitHub.
- Usa una `SECRET_KEY` propia en producción.
- Cambia la contraseña inicial del admin.
- Usa contraseñas de aplicación para SMTP.
- Mantén restringido el acceso al servidor Docker.

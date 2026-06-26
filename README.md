# TrendWatcher

Aplicacion web autoalojable para vigilar productos de Stradivarius y Amazon: precio, stock, tallas, color, referencia, medidas e imagenes.

## Funciones

- Panel de usuario con productos propios, disponibilidad, tallas, medidas e imagenes.
- Soporte para enlaces de Stradivarius y Amazon.
- Registro de nuevos usuarios y recuperacion de contrasena por email.
- Panel admin con revision manual, usuarios, configuracion SMTP/Telegram y prueba de email.
- Escaneo automatico cada 5 minutos.
- Alertas por Telegram y SMTP cuando baja el precio o vuelve el stock.
- SQLite persistente en `./data`.
- Playwright headless con `playwright-stealth` para leer datos dinamicos de las tiendas soportadas.

## Acceso

URL de despliegue:

```text
http://192.168.1.66:8081
```

Usuario inicial:

```text
admin
```

La contrasena inicial se define en `.env` con `ADMIN_PASSWORD`.

## Docker

Arrancar o actualizar:

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

Los datos se guardan en:

```text
./data/stradivarius_monitor.db
./data/app.log
```

## Configuracion

Edita `.env` antes de arrancar, o usa el panel admin para cambiar:

- Intervalo de revision.
- Token y chat ID de Telegram.
- SMTP de correo.
- Productos vigilados.

Para Gmail usa una contrasena de aplicacion. El campo de contrasena SMTP en Admin puede dejarse vacio para conservar la contrasena ya guardada.

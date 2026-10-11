# Catálogos diarios de partidos V6

El módulo `v6.fixture_catalogues` obtiene los catálogos públicos disponibles y prepara un registro local de enlaces para los partidos del día. Su programación es **02:00 de Perú (`America/Lima`)**, con refrescos por lotes cada seis horas y recuperación del lote pendiente al arrancar después de esa hora. El ciclo revisa el horario cada minuto; la finalización depende de los proveedores.

## Servicios independientes

| Servicio | Función | CPU / RAM máximas |
| --- | --- | --- |
| `links-worker` | Lee los fixtures fuente, obtiene el calendario 365Scores y cruza los catálogos con los partidos | 0,10 CPU / 128 MiB |
| `betano-web` | Obtiene en un navegador anónimo el catálogo público destacado de Betano y lo publica en PostgreSQL V6 | 0,50 CPU / 768 MiB |

Ambos usan `restart: unless-stopped`, logs rotativos y las etiquetas `com.sportedge.integration=analytics-v6` y `com.sportedge.component=fixture-catalogues`. El navegador Betano no recibe credenciales ni conexiones de la base fuente. La consulta fuente es una transacción de solo lectura con límite de 1.500 ms; su conexión se cierra antes de descargar catálogos.

El horario se comparte en `integration/link_schedule.py` y el bloque `x-link-schedule` de Compose. Configuración local en `.env`: `V6_LINK_BUILD_HOUR=2`, `V6_LINK_BUILD_MINUTE=0`, `V6_LINK_REFRESH_SECONDS=21600`. Los valores predeterminados también son 02:00. La ejecución requiere que el host y Docker estén encendidos.

## Enlaces en alertas

Al reclamar una alerta, el worker Telegram consulta únicamente `v6.fixture_link_registry`, con timeout de 100 ms. Exige el mismo fixture, día de Perú, equipos, liga, inicio y vigencia. Persiste los enlaces utilizados en `telegram_outbox.payload.fixture_links` antes del envío. No consulta proveedores ni la base fuente por alerta.

El formato HTML añade **Ver partido en 365Scores** y **Abrir mercados en Betano**. Se reutiliza en texto, foto, álbum y respaldo a texto cuando Telegram rechaza imágenes. Si el caché falla, un savepoint permite continuar el envío; los enlaces ausentes se omiten.

Las URL provienen de metadatos del proveedor. 365Scores requiere un ID del encuentro específico; Betano requiere el ID en su ruta de partido. Se rechazan dominios ajenos, credenciales en URL y enlaces genéricos. El cruce exige equipos, liga y hora; si ambos proveedores aportan país, también debe coincidir. Las equivalencias de nombres se registran explícitamente por ID. Los casos ambiguos no generan enlaces.

Los enlaces HTTPS abren el encuentro en web. La apertura de las apps Android/iOS depende del dispositivo y los enlaces universales oficiales; no está verificada. Betano abre los mercados; el usuario elige y confirma la apuesta.

## Cobertura y recuperación

365Scores usa el calendario público diario de su web. Su contrato puede cambiar y no constituye una API contractual oficial. Betano obtiene eventos destacados de una respuesta pública recibida al cargar la página: **su cobertura automática es parcial**. El calendario completo puede responder 403; el servicio registra el fallo sin afectar inferencia o Telegram. No utiliza cuentas, selecciones, apuestas, proxies ni modificaciones para ocultar la automatización.

Cada proveedor conserva estados `matched`, `not_found`, `ambiguous` y `provider_error`. Un fallo conserva enlaces aún vigentes para la misma identidad. Se reintenta cada quince minutos con un máximo de cuatro fallos consecutivos; el día siguiente inicia otro lote. Un catálogo actualizado reemplaza el registro del proveedor/día atómicamente. Un advisory lock serializa las reconstrucciones. Los catálogos importados por operadores se conservan.

El proceso añade una lectura local pequeña por alerta y consume recursos limitados durante los lotes; no se promete impacto cero en un host compartido. Para aislamiento completo, usar otra máquina y una réplica.

## API y operación

Endpoints con el Bearer de integración:

- `GET /v1/fixture-links/status`: módulo, programación, próxima preparación diaria, ejecuciones, catálogos, errores y cobertura. `match_day` es opcional y usa Perú por defecto. No inicia descargas.
- `GET /v1/fixture-links?match_day=YYYY-MM-DD`: registro paginado, con filtros por fixture y proveedor.
- `PUT /v1/fixture-links/catalogues/{provider}`: importa un catálogo validado y programa la reconstrucción.
- `PUT /v1/fixture-links/aliases/{provider}`: registra una equivalencia por ID fuente. Después, pedir reconstrucción.
- `POST /v1/fixture-links/rebuild?match_day=YYYY-MM-DD`: solicita el lote al servicio separado; 202 no confirma que ya esté resuelto.

En Swagger (`/docs`) figuran bajo **Catálogos diarios de partidos**. `scripts/ops.ps1 -Action StatusLinks|LogsLinks|PauseLinks|ResumeLinks` permite operar el módulo por separado.

```powershell
docker compose --profile links --profile links-web up -d --no-deps --build api links-worker betano-web
# Conservar V6_TELEGRAM_ENABLED al actualizar el worker existente.
docker compose --profile telegram up -d --no-deps --build telegram-worker
```

Migración nueva: `004_fixture_links.sql`. Se usa PostgreSQL V6; las migraciones previas, el modelo y su umbral se conservan.

## Validación

La suite incluye **54 pruebas** para identidad, zona horaria y límite de las 02:00, catálogos, ambigüedad, errores, expiración, persistencia y entrega. Los casos completos de Telegram comprueban ambos enlaces en texto, foto, álbum y respaldo a texto, confirmación de outbox y ausencia de solicitudes a proveedores. Usan únicamente `sportedge_v6_test` y Telegram simulado.

La prueba manual `python scripts/test_fixture_link_telegram.py --fixture-id ID` prepara el mensaje sin enviar. Con `--send` usa el chat de prueba ya configurado y autorizado, exige ambos enlaces reales y registra la respuesta de Telegram. No crea inferencias, señales ni outbox; evita repetir un envío confirmado, intentado o incierto del día. La evidencia operativa y los catálogos capturados se conservan localmente y están excluidos de Git.

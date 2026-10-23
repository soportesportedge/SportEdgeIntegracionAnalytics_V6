# Catálogos diarios de partidos V6

El módulo `v6.fixture_catalogues` prepara un registro local de enlaces de los catálogos públicos disponibles. A las **02:00 de Perú (`America/Lima`)** intenta resolver **todos los partidos programados para el día**. Los siguientes ciclos son **cada dos horas** (04:00, 06:00, …, 22:00 y 00:00) y seleccionan únicamente enlaces no resueltos por proveedor cuya hora de inicio esté entre **ahora menos una hora y ahora más dos horas**, incluidos los límites. Los enlaces válidos de un proveedor no se repiten aunque el otro siga pendiente. Se omiten partidos fuera de la ventana y se conserva el registro ya resuelto.

Los horarios se anclan a las 02:00 y no a la hora de finalización del lote, por lo que no acumulan retraso. El ciclo revisa pendientes cada minuto. Tras reiniciar recupera el ciclo actual; si aún no se intentó la carga completa del día después de las 02:00, recupera ese primer intento. Si el lote diario falló, los reintentos técnicos dentro del mismo ciclo pueden completarlo, pero los ciclos posteriores siguen siendo incrementales y respetan la ventana. El ciclo de medianoche contempla tanto los encuentros recientes del día anterior como los próximos del nuevo día y no sustituye la carga completa de las 02:00.

## Servicios independientes

| Servicio | Función | CPU / RAM máximas |
| --- | --- | --- |
| `links-worker` | Selecciona el lote completo o los pendientes de la ventana, obtiene 365Scores y solicita Betano solo cuando hace falta | 0,10 CPU / 128 MiB |
| `betano-web` | Consume solicitudes persistidas, obtiene el catálogo público destacado y lo publica en PostgreSQL V6 | 0,50 CPU / 768 MiB |

Ambos usan `restart: unless-stopped`, logs rotativos y las etiquetas `com.sportedge.integration=analytics-v6` y `com.sportedge.component=fixture-catalogues`. El navegador Betano no recibe credenciales ni conexiones de la base fuente. La consulta fuente es una transacción de solo lectura con límite de 1.500 ms; su conexión se cierra antes de descargar catálogos.

El horario se comparte en `integration/link_schedule.py` y el bloque `x-link-schedule` de Compose. Configuración local en `.env`: `V6_LINK_BUILD_HOUR=2`, `V6_LINK_BUILD_MINUTE=0`, `V6_LINK_REFRESH_SECONDS=7200`. Los valores predeterminados también son 02:00 y dos horas. La ejecución requiere que el host y Docker estén encendidos.

Sin candidatos pendientes no se descargan catálogos ni se abre el navegador. Cuando el proveedor solo publica un catálogo por lotes, puede devolver metadatos de más encuentros; el incremental cruza y escribe únicamente los fixtures seleccionados. No se rastrean páginas individuales de partidos. Las equivalencias, la identidad, el inicio y la vigencia siguen siendo obligatorios; no se generan enlaces por aproximación.

## Enlaces en alertas

Al reclamar una alerta, el worker Telegram consulta únicamente `v6.fixture_link_registry`, con timeout de 100 ms. Exige el mismo fixture, día de Perú, equipos, liga, inicio y vigencia. Persiste los enlaces utilizados en `telegram_outbox.payload.fixture_links` antes del envío. No consulta proveedores ni la base fuente por alerta.

El formato HTML añade **Ver partido en 365Scores**, **Abrir mercados en Betano** y **Ver partido en SportEdge**. Se reutiliza en texto, foto, álbum y respaldo a texto cuando Telegram rechaza imágenes. Si el caché falla, un savepoint permite continuar el envío; los enlaces externos ausentes se omiten y SportEdge sigue disponible.

SportEdge utiliza el ID fuente, sin descarga ni registro diario: `https://portal.sportedge.online/matches/{fixture_id}`. Para el fixture `19713074`, el enlace es `https://portal.sportedge.online/matches/19713074`. Se aceptan únicamente IDs enteros positivos válidos; la URL se construye con el dominio oficial y no se toma de una URL suministrada en el payload. Se conserva en `fixture_links.sportedge` antes de enviar. El portal puede requerir el acceso autenticado habitual.

Las URL provienen de metadatos del proveedor. 365Scores requiere un ID del encuentro específico; Betano requiere el ID en su ruta de partido. Se rechazan dominios ajenos, credenciales en URL y enlaces genéricos. El cruce exige equipos, liga y hora; si ambos proveedores aportan país, también debe coincidir. Las equivalencias de nombres se registran explícitamente por ID. Los casos ambiguos no generan enlaces.

La migración `005_fixture_link_mls_aliases.sql` incorpora once equivalencias verificadas de equipos MLS (Inter Miami, New York City, Toronto, Orlando City, Cincinnati y New England), además de las dos equivalencias de liga necesarias en una instalación nueva. Conserva cualquier equivalencia ya administrada por el operador. Se aplica con `python -m integration.migrate`; después se solicita el lote del día con `POST /v1/fixture-links/rebuild`. Los workers de `home_to_increase` y `result_to_draw` leen el mismo registro, por lo que no requieren reinicio. El horario de las 02:00 de Perú y la consulta local por alerta se conservan. Los catálogos y mensajes históricos no se reenvían ni se prolonga su vigencia.

Los enlaces HTTPS abren el encuentro en web. La apertura de las apps Android/iOS depende del dispositivo y los enlaces universales oficiales; no está verificada. Betano abre los mercados; el usuario elige y confirma la apuesta.

## Cobertura y recuperación

365Scores usa el calendario público diario de su web. Su contrato puede cambiar y no constituye una API contractual oficial. Betano obtiene eventos destacados de una respuesta pública recibida al cargar la página: **su cobertura automática es parcial**. El calendario completo puede responder 403; el servicio registra el fallo sin afectar inferencia o Telegram. No utiliza cuentas, selecciones, apuestas, proxies ni modificaciones para ocultar la automatización.

Cada proveedor conserva estados `matched`, `not_found`, `ambiguous` y `provider_error`. Un fallo o una respuesta parcial conserva enlaces aún vigentes para la misma identidad. Los incrementales actualizan solo los candidatos; nunca borran el registro completo del día. Un evento ya asociado a otro fixture se marca ambiguo. Un advisory lock serializa las escrituras. Los catálogos importados por operadores se conservan.

Los fallos técnicos tienen reintentos cada quince minutos, hasta cuatro por ciclo; el siguiente ciclo reabre los pendientes que aún cumplan la ventana. La espera de una captura Betano se revisa cada minuto sin consumir esos intentos. `fixture_link_catalogue_requests` registra solicitudes, intentos y resultados del navegador; una respuesta antigua no puede reemplazar una solicitud nueva. El navegador no captura si la ventana no produjo una solicitud. No se reenvían alertas tras completar un enlace.

El proceso añade una lectura local pequeña por alerta y consume recursos limitados durante los lotes; no se promete impacto cero en un host compartido. Para aislamiento completo, usar otra máquina y una réplica.

## API y operación

Endpoints con el Bearer de integración:

- `GET /v1/fixture-links/status`: módulo, programación, siguiente ciclo, última carga completa, modo `full`/`incremental`, ventana, candidatos, enlaces conservados, solicitudes del navegador, errores y cobertura. `match_day` es opcional y usa Perú por defecto. No inicia descargas.
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

Migraciones del componente: `004_fixture_links.sql` (catálogos y registro), `005_fixture_link_mls_aliases.sql` (equivalencias MLS verificadas), `006_incremental_fixture_links.sql` (progreso de cargas completas/ciclos y solicitudes del navegador) y `007_fixture_link_daily_attempt.sql` (intento diario separado de su éxito). La actualización reconoce las cargas completas previas para no repetirlas. Aplicar migraciones antes de actualizar los dos servicios de catálogos. Se usa PostgreSQL V6; las migraciones previas, el modelo y su umbral se conservan.

## Validación

La suite valida identidad, zona horaria y límite de las 02:00, catálogos, ambigüedad, errores, expiración, persistencia y entrega. Los casos completos de Telegram comprueban los tres enlaces en texto, foto, álbum y respaldo a texto, confirmación de outbox, SportEdge ante fallo de caché y ausencia de solicitudes a proveedores. Las regresiones de `tests/test_fixture_link_mls_aliases.py` reproducen los tres partidos MLS afectados, verifican el registro y los tres enlaces tras aplicar la migración, y comprueban que repetirla conserva los alias del operador. Usan únicamente `sportedge_v6_test` y Telegram simulado.

`tests/test_incremental_fixture_links.py` comprueba la carga completa, los límites inclusivos -1/+2 horas, la exclusión de enlaces ya resueltos y partidos fuera de ventana, la ausencia de descargas sin candidatos, los dos lados de medianoche, la permanencia del lote de las 02:00, la cola Betano, los reintentos y la conservación de enlaces ante colisiones o respuestas antiguas.

La prueba manual `python scripts/test_fixture_link_telegram.py --fixture-id ID` prepara el mensaje sin enviar. Con `--send` usa el chat de prueba ya configurado y autorizado, exige ambos enlaces reales y registra la respuesta de Telegram. No crea inferencias, señales ni outbox; evita repetir un envío confirmado, intentado o incierto del día. La evidencia operativa y los catálogos capturados se conservan localmente y están excluidos de Git.

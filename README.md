# SportEdge Integración Analytics V6

Stack independiente que recibe snapshots, los registra antes de invocar Analytics V6 y gestiona Telegram mediante una bandeja durable. El backend, sus workers, sus colas y su frontend actuales no se modifican. PostgreSQL V6 usa su propia instancia, volumen, red, migraciones y credenciales.

La versión 1.1.0 invoca por defecto `P15_logistic_002_20261011_recent_cal1`: 44,92% de precisión y 15,85% de recall en test histórico reciente, con estabilidad futura sin verificar. El usuario autorizó su implementación experimental. [Contrato, registro y reversión](docs/PRECISION15_RELEASE.md).

## Catálogos diarios de partidos V6

El módulo **`v6.fixture_catalogues`** prepara los enlaces de todos los partidos del día a las **02:00 de Perú (`America/Lima`)**. Cada dos horas procesa únicamente enlaces pendientes, por proveedor, de encuentros que empiezan en las siguientes dos horas o empezaron en la última hora. Los ciclos incluyen medianoche y conservan los enlaces resueltos. `links-worker` y `betano-web` son servicios Docker separados, con límites de recursos, reinicio automático y controles propios; las alertas leen el registro local. El navegador solo se activa ante una solicitud del worker. El catálogo automático de Betano contiene eventos destacados y su cobertura es parcial.

Estado autenticado: `GET /v1/fixture-links/status`, con horario, próxima preparación diaria, ejecuciones, errores y cobertura; en `/docs` aparece como **Catálogos diarios de partidos**. Operación: `scripts/ops.ps1 -Action StatusLinks|LogsLinks|PauseLinks|ResumeLinks`. [Configuración y evidencia](docs/FIXTURE_LINKS.md).

Al enviar una alerta, Telegram añade **Ver partido en 365Scores** y **Abrir mercados en Betano** cuando existen enlaces vigentes para la misma identidad del fixture. También incluye **Ver partido en SportEdge**, usando `https://portal.sportedge.online/matches/{fixture_id}` directamente desde el ID del snapshot, incluso si falta el caché externo. El formato es común a texto, foto, álbum y respaldo a texto. Los tres enlaces usados se guardan en `telegram_outbox.payload.fixture_links`; el envío nunca descarga catálogos en línea.

## Instalar y encender

Clonar este repositorio y [SportEdgeAnalyticsV6](https://github.com/soportesportedge/SportEdgeAnalyticsV6) en carpetas hermanas:

```powershell
python scripts/init_secrets.py
docker compose up -d --build api inference-worker analytics
```

La migración se ejecuta antes de los servicios. API de recepción: `http://127.0.0.1:8791`; API analítica: `http://127.0.0.1:8790`; PostgreSQL nuevo: `127.0.0.1:15436`. Las conexiones internas usan la red nueva `sportedge_v6`. Ningún servicio reutiliza los volúmenes, colas o red V2.

Los secretos nuevos están en `secrets/*.txt`, fuera de Git y del contexto de construcción. `db_password`, `model_api_key` e `integration_api_key` se generan aleatoriamente. No rotar `db_password` sobre un volumen ya inicializado sin modificar también la credencial en el PostgreSQL nuevo.

## Captura sin cambiar el backend

El recolector opcional lee `match_centre_snapshots` por PK; consulta únicamente los nuevos IDs y una superposición de 128 IDs para commits tardíos. Construye las variables con el query temporal auditado: máximo tres snapshots por ID, H2H más reciente anterior y standings previos al partido; usa `captured_at <= snapshot` y conserva NULL/ceros. Añade nombre de liga y escudos disponibles. La etiqueta futura queda null.

Para una fuente existente, preferir una réplica de lectura. En la instalación local se puede crear SOLO un rol nuevo con SELECT sobre siete tablas, límite de dos conexiones y tiempos acotados:

```powershell
python scripts/provision_source_reader.py --container sportedge_v2_db --database sportedge_v2 --owner sportedge --port 5401
docker compose --profile collector up -d collector
```

Este script añade `sportedge_v6_ro`; no modifica filas, tablas, índices ni roles existentes. En otra instalación, poner un DSN de solo lectura en `secrets/source_database_url.txt` y omitir el provisionador. El collector abre una única sesión fuente, transacciones READ ONLY, timeout de 1.500 ms, lock timeout de 100 ms y JIT desactivado solo en su sesión. Consulta cada cinco segundos, máximo diez nuevos snapshots, y pausa ante acumulación de trabajo. Empieza al final de la fuente: no hace un barrido histórico. El cursor solo avanza tras registrar los IDs considerados; se conserva al reiniciar. Rechazos de extracción se registran y quedan disponibles para revisión.

La superposición es finita: una inserción con commit demorado más allá de 128 IDs requiere reconciliación explícita. La captura no es CDC ni usa triggers sobre la aplicación actual. Para volúmenes mayores, ajustar el lote/poll dentro de sus límites, usar una réplica o entregar snapshots a la API con el cliente opcional `integration/client.py`.

## Registro síncrono y asíncrono

`POST /v1/invocations?mode=async` es la vía recomendada. Usa `Authorization: Bearer <integration_api_key>` e `Idempotency-Key`. Recibe snapshot, family opcional, notify y source. El snapshot requiere 183 claves predictoras y contexto; todas las variables adicionales se conservan. Devuelve 202 únicamente DESPUÉS de guardar el registro. Si la base nueva está caída o la cola está llena, devuelve error; no simula aceptación.

`mode=sync` registra primero y espera como máximo diez segundos. Si todavía no finalizó, devuelve 202 con el mismo ID; el proceso durable continúa mediante el worker. La espera pertenece exclusivamente al nuevo API; no se introducen llamadas en el camino de respuesta del backend existente.

`GET /v1/invocations/{id}` devuelve entradas, columnas tipadas, ingeniería, resultado y estado. `GET /v1/status` informa pendientes, antigüedad y estado de Telegram. Una misma clave con distinto payload produce 409; entradas inválidas quedan registradas como rejected. `/v1/invocations/{id}/retry` solo reabre trabajos failed. La capacidad predeterminada es de 5.000 pendientes/procesando.

La tabla `v6.match_centre_invocations` contiene 222 columnas originales, 78 columnas de ingeniería, liga/escudos/raw de fuente y metadatos de ejecución. `input_values` conserva el JSON de invocación y `result` incluye todas las características físicas utilizadas. Las columnas tipadas facilitan consulta; no sustituyen el original. `v6.invocation_events` registra transiciones y errores. `v6.collector_cursor` conserva la lectura incremental.

El worker usa `FOR UPDATE SKIP LOCKED`, lease de 60 segundos y token de propiedad. Una respuesta tardía de un trabajador antiguo no puede sobrescribir al nuevo. Inferencia y outbox se confirman en una transacción. Hay cinco intentos, backoff acotado y reutilización de conexiones HTTP.

## Telegram de pruebas V6

La integración 1.3.1 mantiene el registro diario de enlaces de 365Scores y Betano y añade ciclos incrementales cada dos horas, con ventanas de inicio y solicitudes persistidas para el navegador. Los ciclos posteriores siguen siendo incrementales aunque el intento diario haya fallado. Telegram incorpora los enlaces exactos y vigentes del registro local, además del enlace directo de SportEdge. [Configuración, contrato, endpoints y límites de acceso/app](docs/FIXTURE_LINKS.md).

La familia predeterminada es ahora `logistic`, versión `P15_logistic_002_20261011_recent_cal1`. El resultado registra score original, probabilidad ajustada, versión del calibrador, umbral equivalente y regla de decisión. El integrador verifica SHA/versiones y selecciona con score >= 0,608364981863603; no confunde el umbral con la escala calibrada. La migración 003 añade columnas de metadatos a la instancia V6. Telegram identifica el ajuste histórico y mantiene el aviso experimental. [Comparación, protocolo y reversión](docs/PRECISION15_RELEASE.md).

El destino acordado es `-5538403637`, separado del chat actual. Configurar token y chat en los secretos correspondientes. Si se reutiliza el bot actual:

```powershell
python scripts/configure_telegram.py --source-env RUTA_AL_ENV_ACTUAL --chat-id=-5538403637
$env:V6_TELEGRAM_ENABLED='true'
docker compose --profile telegram up -d telegram-worker
```

El bot debe pertenecer al chat y tener permiso de envío. Solo las señales elegibles del umbral CONGELADO generan aviso; ocultar el umbral en el texto no cambia la selección. El formato mantiene encabezado, evento, equipos, marcador, minuto e identificación del modelo; añade la liga cuando existe y omite el umbral. Para scores sin calibrar se muestra una puntuación orientativa de 0 a 100 y se indica que aún no equivale a una probabilidad real calibrada. Para probabilidades calibradas se muestra porcentaje con explicación en lenguaje natural. Siempre se identifica V6 como experimental.

La deduplicación permite una alerta por partido/target/versión/marcador. Los avisos con snapshot o creación de más de cinco minutos se suprimen. Hay un envío por segundo y respeto de `retry_after`. Timeout de lectura o caída durante el envío quedan `uncertain`, sin reenvío automático: Telegram no ofrece una clave de idempotencia y no se puede prometer exactamente una entrega ante esas fallas. Revisar esos casos manualmente antes de reenviar. Los IDs de mensaje confirmado se registran en `v6.telegram_outbox`.

Los escudos HTTPS del proveedor Sportmonks se conservan cuando están disponibles: álbum con dos imágenes, foto con una o texto como respaldo si Telegram rechaza la imagen. El texto de la alerta mantiene las mismas reglas en los tres formatos.

## Apagar, cambiar o retirar

`scripts/ops.ps1 -Action Start|Stop|Status|PauseCollector|ResumeCollector|PauseTelegram|ResumeTelegram|Remove` actúa solo sobre el proyecto nuevo. `Remove` elimina sus contenedores y red, conservando el volumen de datos. Antes de retirar definitivamente: apagar collector/Telegram, ejecutar `python scripts/backup.py`, retirar el stack y verificar el backup. La eliminación posterior del volumen `sportedge-v6_v6_data`, imágenes V6, secretos y rol `sportedge_v6_ro` se hace explícitamente cuando se decide descartar los datos. No usar comandos globales prune.

Una actualización de API/worker puede reconstruirse y recrearse sin reiniciar V2. Apagar Telegram conserva pendientes, pero los antiguos se suprimen al encender. Apagar Analytics conserva las invocaciones y registra reintentos; la interrupción larga puede dejarlas failed, disponibles para retry. Versionar artefactos/contratos junto con migraciones nuevas; no editar una migración ya aplicada.

## Desempeño y verificación

Límites: Analytics 1 CPU/1 GiB; PostgreSQL 0,5 CPU/512 MiB; API, inferencia, collector y Telegram 0,25 CPU/192 MiB cada uno. Pools PostgreSQL de dos conexiones por proceso, un worker de inferencia, una conexión HTTP a Analytics y cola acotada. Los logs rotan. No hay entrenamiento en el stack ni consultas ilimitadas contra V2.

El query acotado reprodujo 1.105 valores de cinco snapshots de entrenamiento sin diferencias; su tiempo medido está en `docs/source_adapter_validation.json`. Estas mediciones no garantizan latencia cero bajo cualquier carga compartida. Para aislamiento de recursos absoluto, desplegar el stack en otra máquina y leer una réplica. El estudio estadístico sigue en estado experimental.

Las pruebas usan SOLO `sportedge_v6_test`, nunca la base productiva. Crear esa base en el PostgreSQL NUEVO, configurar `V6_DATABASE_URL`, `V6_DB_PASSWORD_FILE`, ejecutar `python -m integration.migrate` y `pytest tests -q`. Cubren durabilidad, claims concurrentes, leases, deduplicación, errores de inferencia, timeout/rate-limit Telegram, supresión de avisos antiguos y API.

Referencias: [PostgreSQL SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html), [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage).

Evidencia de ejecución y límites pendientes: [docs/VERIFICATION.md](docs/VERIFICATION.md).

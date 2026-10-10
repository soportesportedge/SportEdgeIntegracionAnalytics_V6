# Verificación local V6 — 10 de octubre de 2026

## Resultado comprobado

Los dos servicios se construyeron y ejecutaron en Docker Desktop Linux con imágenes propias. El API analítico responde en localhost:8790 y el integrador en localhost:8791. PostgreSQL V6 tiene instancia, volumen, esquema y credenciales propios. La aplicación V2 y su frontend no fueron modificados.

Las 32 pruebas automatizadas pasaron: 15 del servicio analítico y 17 del integrador. Cubren las siete familias y equivalencia con la transformación congelada, validación del contrato, registro durable, idempotencia, leases concurrentes, resultados tardíos, outbox atómico, timeout/rate-limit Telegram, escudos, recuperación a texto cuando la imagen falla y avisos antiguos. Se usan exclusivamente tablas de la base nueva de pruebas `sportedge_v6_test`.

`container_validation.json` registra ejecución real Linux de las siete familias, equivalencia numérica con la predicción original, invocación síncrona y asíncrona, conservación del input y persistencia de ingeniería. Sus tiempos son una muestra local de veinte aceptaciones, no un SLA ni una prueba de carga de producción.

`recovery_validation.json` registra la prueba de apagado de Analytics V6: el integrador aceptó y conservó una invocación, dejó un reintento durable y la completó al encender el modelo. La salud profunda de V2 respondió OK antes, durante y después; los IDs y fechas de inicio de sus contenedores API, PostgreSQL y runner permanecieron iguales. Esta prueba no demuestra ausencia de impacto bajo cualquier carga compartida.

`source_adapter_validation.json` y `source_adapter_explain.json` contienen una lectura limitada a cinco snapshots: 1.105 valores comparados, cero diferencias, ejecución de 104,483 ms y planificación de 30,527 ms. Se aplicaron transacción READ ONLY, JIT desactivado en la sesión y timeout de 1.500 ms. No se modificó la fuente.

## Telegram y respaldo

Telegram confirmó el mensaje de prueba al chat separado `-5538403637`. La fila de `v6.telegram_outbox` para la invocación `41c42fac-f8ed-48af-aaa9-ca463ff042ec` quedó `sent`, con identificador de mensaje `2955`, sin error. El mensaje usa un ejemplo histórico explícitamente marcado como prueba técnica; no demuestra una señal en vivo ni efectividad predictiva.

Se generó un backup PostgreSQL en formato custom de la base nueva V6. Los respaldos y secretos están fuera de Git. El script `scripts/backup.py` permite repetirlo; la retirada con `scripts/ops.ps1 -Action Remove` conserva el volumen por defecto.

## Límites pendientes

El usuario autorizó explícitamente crear `sportedge_v6_ro` el 10 de octubre de 2026. Se creó ese rol nuevo y se activó `sportedge-v6-collector-1`. La verificación confirmó SELECT sobre exactamente las siete tablas fuente, sin permisos de escritura ni administración, máximo dos conexiones, transacciones de lectura por defecto y tiempos acotados. El recolector mantiene una sola sesión fuente. La credencial permanece fuera de Git.

Ya se completaron dos invocaciones reales: snapshots `103774` y `103783`, partido `19609847`, minutos 95 y 96, liga Major League Soccer. Cada registro conserva las 78 ingenierías y 171 campos del vector físico utilizado por Random Forest. Su score fue 0,1380438412; no generaron señales ni avisos Telegram. No se forzó un envío para comprobar la captura. El cursor quedó registrado en la base nueva.

La salud profunda de V2 siguió OK después de activar el recolector; sus contenedores API, PostgreSQL y runner conservaron los mismos IDs y fechas de inicio. Se midieron 31,1 MiB de memoria en el recolector, dentro de su límite de 192 MiB. `source_reader_validation.json` documenta los permisos; `scripts/verify_source_reader.py --host 127.0.0.1` permite repetir esa comprobación desde el host local.

El candidato conserva `research_only=true` y `actionable=false`: no alcanzó precisión >50% y recall >25% en evaluación histórica. El Random Forest `v6_precision_20261010_cal1` conserva el score y la política de señales originales; añade una probabilidad ajustada con calibración histórica, con su método, versión y límites. [Verificación de calibración](CALIBRATION.md).

Para independencia absoluta de recursos, usar una máquina separada y una réplica de lectura. En el host compartido se aplican límites de CPU/memoria, pools pequeños, un worker de inferencia, consultas incrementales acotadas y backpressure; no hay entrenamiento en los contenedores de servicio.

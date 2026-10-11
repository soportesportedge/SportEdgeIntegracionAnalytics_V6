# Integración del candidato reciente V6 — versión 1.1.0

El modelo predeterminado es ahora `family=logistic`, versión `P15_logistic_002_20261011_recent_cal1`. API, cliente opcional y collector resuelven ese valor desde el mismo contrato. Las invocaciones que pidan explícitamente otra familia conservan esa selección. Los resultados históricos ya completados no se recalculan ni sobrescriben.

El integrador exige el SHA del artefacto, versión del modelo, calibrador y política de decisión. Rechaza señales que no coincidan con `raw_model_score >= 0,608364981863603`. La probabilidad calibrada tiene escala y umbral equivalente separados, 0,47671556713917673. Ambas magnitudes quedan guardadas desde la invocación y el resultado completo conserva el vector utilizado.

La migración aditiva **003**, únicamente en PostgreSQL V6, incorpora columnas consultables de score, candidato, hash, probabilidad/calibración y política/umbral de decisión. No rellena ni modifica resultados históricos. Las migraciones 001/002, sus checksums y la base fuente V2 permanecen intactos. Siguen las 222 columnas originales y las 78 de ingeniería, con JSON original y metadatos adicionales.

La ruta habitual es asíncrona, con aceptación solo después del INSERT durable. La ruta síncrona espera un máximo acotado y devuelve 202 si el trabajo sigue en curso. No se introducen llamadas ni consultas en los componentes existentes del backend. Se mantienen los límites de CPU, memoria, concurrencia, conexiones, lote y tiempo del collector.

Telegram continúa en el chat separado **-5538403637**, conservando bot y configuración existentes. Mantiene evento, equipos, marcador, minuto, escudos y liga disponible. Oculta el umbral y explica la probabilidad como estimación del modelo por cada 100 situaciones similares. Identifica su carácter experimental y su validación futura pendiente. La deduplicación usa la nueva política, con una alerta por partido/target/política/marcador; solo se notifican snapshots recientes elegibles.

El usuario autorizó este despliegue experimental el 11/10/2026. El candidato obtuvo 44,92% de precisión y 15,85% de recall en el test reciente, pero 27,70%/37,80% en backtest retrospectivo de configuración fija. El mapa final empeoró el Brier en el test histórico reutilizado. La implementación no afirma estabilidad futura ni aprobación estadística. [Métricas completas](precision15_metrics.json).

## Reversión local

Se preservan las imágenes anteriores 1.0.0 y un backup de la base V6 previo a la actualización. La migración 003 es aditiva y compatible con el código anterior. No restaurar un backup encima de registros nuevos salvo decisión explícita.

```powershell
$env:V6_TELEGRAM_ENABLED='true'
docker compose --profile collector --profile telegram stop collector api inference-worker telegram-worker analytics
docker compose -f compose.yaml -f compose.rollback-20261011.yaml --profile collector --profile telegram up -d --no-build analytics api inference-worker telegram-worker collector
```

Volver a usar solo `compose.yaml` y las imágenes 1.1.0 para reactivar el candidato nuevo. Estos comandos actúan sobre el proyecto `sportedge-v6`; no ejecutan acciones globales ni reinician V2. No retirar imágenes anteriores antes de cerrar el período de reversión.

Las pruebas de persistencia, claims, leases, outbox, deduplicación, rechazo de contratos y Telegram se ejecutan en `sportedge_v6_test`. La verificación del despliegue registra ejemplos síncronos/asíncronos con `notify=false`, comprueba el destino/configuración de Telegram y contrasta IDs/timestamps de todos los contenedores V2 antes/después. Los avisos reales siguen el flujo existente.

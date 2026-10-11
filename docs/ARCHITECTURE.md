# Aislamiento y flujo

```mermaid
flowchart LR
  S[(PostgreSQL V2 o replica)] -->|SELECT acotado| C[Collector V6 opcional]
  E[Cliente externo opcional] -->|202 async / espera sync| I[API Integracion V6]
  C --> Q[(PostgreSQL V6: entrada + cola durable)]
  I --> Q
  Q --> W[Worker inferencia V6]
  W -->|HTTP autenticado| M[API Analytics V6: modelo congelado]
  M --> W
  W -->|Resultado + outbox atomicos| Q
  Q --> T[Worker Telegram V6]
  S -->|Un SELECT acotado por lote| L[Links worker V6]
  D[Programacion compartida 02:00 America/Lima] --> L
  D --> B
  P[Catalogos diarios 365Scores y Betano] -->|Descarga bulk / importacion| L
  B[Betano web: navegador anonimo por lote] -->|Catalogo publico destacado| Q
  Q -->|Catalogo local / reconstruccion pendiente| L
  L -->|Registro por fixture / proveedor| Q
  T --> G[Chat de pruebas V6]
```

V2 no depende de ninguno de estos procesos. La caída de Analytics, Telegram o PostgreSQL V6 no bloquea los workers actuales. El collector y el constructor de enlaces acceden a la fuente con sesiones de solo lectura y límites de tiempo; ambos se apagan de forma independiente. El navegador Betano solo accede a PostgreSQL V6 y no recibe el secreto fuente. Telegram obtiene los enlaces del registro local, sin descargas externas. No hay triggers, cambios en modelos ORM V2 ni reutilización de sus colas. El frontend no interviene.

La confirmación de recepción ocurre después del commit de entrada. La confirmación de inferencia ocurre junto con la creación de outbox. La confirmación Telegram se registra solo con `ok=true` y `message_id`. La incertidumbre de entrega no se presenta como envío confirmado.

Los leases y tokens impiden que un trabajador antiguo guarde un resultado tras perder su turno. SKIP LOCKED permite consumidores separados sin bloquear otros trabajos. Una clave de idempotencia con cuerpo diferente es un conflicto, no una actualización silenciosa del snapshot.

La política actual de notificación usa la señal congelada y un chat de pruebas. El mensaje no muestra el umbral, pero este se conserva para auditoría. El score sin calibrar no se describe como frecuencia real de ocurrencia.

## Catálogos diarios desacoplados

El módulo de catálogos se identifica como `v6.fixture_catalogues`, con etiquetas Docker `com.sportedge.component=fixture-catalogues` y roles diferentes para `links-worker` y `betano-web`. Su horario común vive en `integration/link_schedule.py` y `x-link-schedule`: 02:00 `America/Lima`, refrescos cada seis horas y recuperación al arrancar. El estado autenticado `/v1/fixture-links/status` solo consulta configuración y registros V6. Los comandos `StatusLinks`, `LogsLinks`, `PauseLinks` y `ResumeLinks` permiten operarlo por separado. [Detalle de catálogos](FIXTURE_LINKS.md).

## Retirada

Detener los perfiles collector/telegram, exportar la base nueva, ejecutar `ops.ps1 -Action Remove`. El volumen permanece hasta su eliminación explícita. La cuenta fuente V6 puede retirarse después de cerrar su conexión; las cuentas y tablas de la aplicación actual permanecen independientes. Eliminar imágenes/volumen únicamente por sus nombres V6, nunca por prune global.

# Calibración del Random Forest publicado V6

Se calibró exclusivamente la salida del Random Forest `P26_random_forest_028`. El bosque y la transformación de variables se conservaron; el ajuste modifica la escala de probabilidad y conserva la regla original de selección de señales.

Versión: `v6_precision_20261010_cal1`. Calibrador: Platt regularizado con logit del score y minuto, penalización 0,1 hacia la identidad. Los coeficientes se ajustan con un peso total igual por partido, sin balancear clases.

## Comparación auditable

| Fase | Partidos / filas | Precisión antes y después | Recall antes y después | Brier antes → después | ECE antes → después |
|---|---:|---:|---:|---:|---:|
| Entrenamiento (dentro de muestra) | 529 / 11583 | 66.64% | 41.04% | 0.13532 → 0.12880 | 13.66% → 10.25% |
| Ajuste del calibrador | 116 / 2809 | 30.36% | 22.19% | 0.18273 → 0.17044 | 9.93% → 4.99% |
| Validación temporal de selección | 113 / 2686 | 50.69% | 25.14% | 0.18342 → 0.18075 | 6.33% → 4.15% |
| Evaluación histórica reutilizada | 218 / 5507 | 31.82% | 23.03% | 0.16692 → 0.15148 | 12.77% → 6.85% |

En evaluación histórica, la pérdida logarítmica bajó de 0.51021 a 0.47594. La probabilidad media bajó de 30.99% a 25.05%, frente a una frecuencia real de 18.21%. Entre las señales, la estimación media pasó de 55.39% a 45.37%, mientras su precisión observada permaneció en 31.82%.

Se preservaron las decisiones en las 23.600 filas. La comprobación comparó el artefacto nuevo con los scores originales y encontró una diferencia numérica máxima inferior a 1e-12. Se verificó también la estructura de los 300 árboles y la transformación congelada. La política de señal permanece `raw_model_score >= 0.4983797123424291`. El umbral equivalente de probabilidad depende del minuto; el API conserva ambos valores y su escala.

## Protocolo y evidencia

Se ajustaron mapas sobre 116 partidos / 2.809 filas no utilizados para ajustar el bosque y se seleccionó el método sobre 113 partidos / 2.686 filas posteriores. La evaluación histórica comprende 218 partidos / 5.507 filas y no se usó para escoger el método en este trabajo. Los grupos de partidos son disjuntos; se comprobó que las etiquetas de ajuste estaban disponibles antes de validar.

La primera comparación de 19 mapas con intercepto sin penalización no mejoró el Brier de validación. Se conservó ese intento y, antes de calcular las métricas de evaluación histórica, se añadió regularización de todos los coeficientes hacia la identidad. La selección final comparó 27 alternativas, incluida la identidad, una probabilidad constante e isotónica. No se reentrenaron clasificadores ni se utilizaron nuevos fixtures del recolector como etiquetas.

El bootstrap pareado de 1.000 réplicas por partido dio un intervalo del 95% de [-0,02094; -0,00918] para el cambio del Brier y [-0,04679; -0,01980] para la pérdida logarítmica. Son intervalos condicionados a esta evaluación histórica y no sustituyen una cohorte futura independiente. Las métricas por liga, cluster, minuto y subconjunto FT verificado están disponibles en `subgroup_metrics.csv` y `metrics.csv`.

## Límites

El modelo conserva `research_only=true` y `actionable=false`. La precisión histórica 31,82% y el recall 23,03% siguen por debajo de los mínimos solicitados. La calibración no resuelve la discriminación insuficiente ni las dudas de etiquetado: hay 7.578 registros sin evidencia de cierre suficiente y 280 discrepancias de política en la base original. Esa base y sus etiquetas se conservaron.

La evaluación histórica ya se había observado en investigaciones anteriores; se informa expresamente como reutilizada. Continúa pendiente una cohorte futura con etiquetas reconciliadas. El error ECE aún es 6,85 puntos porcentuales: la nueva salida es una estimación calibrada experimental, no una frecuencia garantizada para cada señal.

## Publicación y operación

Se actualizaron exclusivamente Analytics V6 y su integración V6. Las otras seis familias mantienen sus artefactos y versiones originales, verificadas individualmente. La tabla nueva guarda ambas escalas, el método y la versión dentro del resultado; los registros anteriores se conservan. La deduplicación usa la versión de la política de decisión original para que este ajuste no repita avisos del mismo marcador. Telegram muestra la estimación ajustada con datos históricos y conserva la identificación experimental.

El cambio incluye pruebas positivas y negativas reales, equivalencia de todas las familias, contrato entre servicios y deduplicación entre versiones. La actualización se realiza con pausa breve de procesos V6, preservando la cola y el cursor, sin reiniciar V2. La evidencia de despliegue se registra por separado después de comprobar la respuesta del contenedor activo.

## Archivos y método

[Curvas y distribución](https://github.com/soportesportedge/SportEdgeAnalyticsV6/blob/main/docs/Calibracion_V6.html) · [Métricas](https://github.com/soportesportedge/SportEdgeAnalyticsV6/blob/main/docs/calibration_metrics.csv) · [Auditoría por fila](https://github.com/soportesportedge/SportEdgeAnalyticsV6/blob/main/docs/calibration_row_audit.csv) · [Candidatos](https://github.com/soportesportedge/SportEdgeAnalyticsV6/blob/main/docs/calibration_candidates.csv) · [Protocolo](https://github.com/soportesportedge/SportEdgeAnalyticsV6/blob/main/docs/calibration_protocol.json) · [Bootstrap](https://github.com/soportesportedge/SportEdgeAnalyticsV6/blob/main/docs/calibration_bootstrap.json)

El método sigue la separación entre clasificador y calibrador de [scikit-learn](https://scikit-learn.org/stable/modules/calibration.html); la comparación beta toma como referencia [Kull et al., 2017](https://proceedings.mlr.press/v54/kull17a.html). Brier y log loss evalúan la calidad probabilística completa; la curva de confiabilidad, ECE y sesgo de medias complementan la comparación.

## Despliegue comprobado

La versión calibrada quedó activa y se comprobó mediante peticiones HTTP reales a las siete familias. La pausa y disponibilidad tomaron 10.3 segundos. Se conservaron los 70 resultados anteriores y el cursor del recolector. Se completaron pruebas síncrona y asíncrona con los scores y las probabilidades esperados; se desactivó la notificación de esas invocaciones técnicas.

Pasaron 38 pruebas automatizadas: 18 del API analítico y 20 de integración. La salud profunda de V2 permaneció OK y los IDs y fechas de inicio de sus contenedores API, PostgreSQL y runner no cambiaron. [Evidencia de despliegue](https://github.com/soportesportedge/SportEdgeAnalyticsV6/blob/main/docs/calibration_deployment_validation.json).

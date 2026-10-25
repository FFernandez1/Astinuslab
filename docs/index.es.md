# AstinusLab

AstinusLab es un playground de ataque y análisis forense para investigación de seguridad local. Crea laboratorios de aplicaciones web vulnerables, conecta VMs atacantes, ejecuta ataques, captura evidencia de RAM/disco y analiza la memoria con Volatility 3.

Usa primero Playground para laboratorios interactivos guiados. Usa Jobs y las recetas actuales de Playground en JSON para adquisiciones repetibles basadas en escenarios, con metadatos reproducibles, manifiestos y checksums SHA-256.

---

## Empezar

*   [Playground](user/PLAYGROUND.md) - Crea laboratorios vulnerables, conecta VMs atacantes y captura evidencia bajo demanda.
*   [Primeros pasos](usage/getting-started.md) - Instala los prerrequisitos, inicia los servicios y ejecuta tu primera adquisición.
*   [Ejecución local](usage/running-locally.md) - Runbook completo de desarrollo y publicación.

## Guías

*   [Configuración](usage/configuration.md) - Ajustes de la app, entorno del lanzador y catálogos.
*   [Ejemplos](usage/examples.md) - Flujos de trabajo comunes de UI y API.
*   [Resolución de problemas](usage/troubleshooting.md) - Resuelve problemas comunes de configuración, red y permisos.
*   [Imágenes pre-horneadas](user/PREBAKED_IMAGES.md) - Optimiza el rendimiento integrando workloads en las imágenes base.
*   [Volatility 3](user/VOLATILITY3.md) - Construye ISFs reutilizables y ejecuta análisis de memoria local al job.

## Referencia

*   [Visión general de la API](api/overview.md) - URLs base, esquemas y modelo de seguridad.
*   [Endpoints de la API](api/endpoints.md) - Endpoints REST verificados contra el código fuente.
*   [Errores de la API](api/errors.md) - Estados HTTP comunes y comportamiento de las respuestas.
*   [Alcance V1 y hoja de ruta](development/ROADMAP.md) - Límites actuales del producto y trabajo diferido.
*   [Configuración avanzada](reference/ADVANCED_CONFIG.md) - Referencia detallada de configuración.
*   [Soporte de distros](reference/DISTRO_SUPPORT.md) - Familias Linux x86_64 soportadas para símbolos de Volatility 3.
*   [Integridad forense](reference/FORENSIC_INTEGRITY.md) - Hashing, nombres de archivo canónicos y detalles del manejo de evidencia.

## Arquitectura

*   [Visión general de la arquitectura](architecture/overview.md) - Responsabilidades y límites del sistema.
*   [Componentes](architecture/components.md) - Módulos de backend, runner, catálogo, almacenamiento y UI.
*   [Flujo de datos](architecture/data-flow.md) - Flujos de jobs, playground y artefactos.
*   [Decisiones](architecture/decisions.md) - Decisiones de diseño para mantenedores.

## Desarrollo

*   [Configuración de desarrollo](development/setup.md) - Entorno local y flujo de trabajo del repositorio.
*   [Pruebas](development/testing.md) - Comandos de test, lint, type-check y CI.
*   [Contribuir](development/contributing.md) - Flujo y estándares de contribución.
*   [Desarrollo de workloads](development/WORKLOADS.md) - Empaqueta escenarios de laboratorio repetibles y stacks de servicios.

## Comunidad

*   [Gobernanza](community/GOVERNANCE.md) — Cómo se toman las decisiones del proyecto.

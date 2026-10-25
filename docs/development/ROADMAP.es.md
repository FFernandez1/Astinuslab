# Alcance V1 y hoja de ruta

AstinusLab V1 es un playground de ataque y forense para investigación de seguridad local. Se centra en construir labs de aplicaciones web vulnerables, conectar VMs atacantes, capturar evidencia y analizar memoria con Volatility 3.

## Alcance de V1

- Operación localhost/local-confiable con autenticación opcional de un administrador mediante bearer token y sesión de navegador. Las instalaciones release la activan por defecto. Multiusuario y RBAC siguen fuera del alcance de V1.
- UI guiada del Playground como flujo de lab de entrada principal.
- Importación/exportación de recetas JSON para labs de Playground repetibles; la creación más amplia de recetas YAML sigue siendo roadmap.
- Labs de aplicaciones web vulnerables primero: WordPress, DVWA, Juice Shop, servicios estilo GitLab y Workloads personalizados.
- Soporte de VM atacante para generación de tráfico estilo pentest.
- Captura de artefactos de memoria y disco desde el lado del host.
- Sidecars de evidencia: los manifiestos y checksums SHA-256 deberían ser estándar en los artefactos de Job y Playground.
- Volatility 3 es el camino central de análisis de V1 para memoria de Linux, usando ISFs de Linux. Las bases de PDB/catálogo de Windows son experimentales y quedan fuera de los flujos soportados de 1.0.0.
- **Capacidad**: varios Jobs pueden conservar targets vivos y atacantes opcionales dentro de los límites agregados de RAM, disco y número de VMs. El único worker de ciclo de vida serializa operaciones; no limita el sistema a un único Job vivo. Un Playground ordinario es exclusivo con Jobs y solo admite una sesión activa. Las instancias experimentales de Packages tienen su propio ciclo de vida y quedan fuera del alcance soportado de 1.0.0. Consulta [Configuración](../usage/configuration.md).
- Distribución desde el checkout del código fuente más `ops/deploy/install.sh`.

## Postura de seguridad

V1 asume un operador local confiable y acceso por localhost. Vincula a `127.0.0.1` para uso normal. No expongas la API/UI a LAN o Internet sin autenticación externa y controles de reverse proxy.

Los workloads guest son intencionadamente vulnerables. El endurecimiento de guest hostil debería poder seleccionarse durante la creación del lab a medida que los controles maduren; usa redes aisladas cuando pruebes cadenas de exploit.

## Hoja de ruta

- Paridad de aprovisionamiento de targets Windows automatizado.
- Telemetría en vivo continua más allá de las capturas discretas.
- Informes generados más completos, más allá de los resúmenes de Volatility y los informes de plugins ya publicados.
- Presets más ricos de endurecimiento de guest hostil.
- Soporte de recetas YAML y una UX de creación de recetas más completa más allá de la importación/exportación JSON actual.

## Fuera de alcance para V1

- Paquetes de laboratorio portátiles: desactivados, experimentales y sin aceptación hasta que P1–P3 pasen en un mismo candidato. Sin garantías del ciclo completo, entre dos hosts, sin red ni de rendimiento.
- Autenticación multiusuario/RBAC dentro de AstinusLab.
- Despliegue multi-tenant alojado en cloud.
- Reemplazo de SIEM o plataforma de monitorización siempre activa.

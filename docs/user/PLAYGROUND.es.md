# Modo Playground

Usa Playground para modificar el laboratorio de forma interactiva y capturar evidencias bajo demanda; usa [Jobs](../usage/examples.md) para ejecuciones repetibles de Scenario. Si no hay imágenes para un rol, sigue **Gestionar imágenes** o la **Guía de imágenes**. Si falla la carga del catálogo, **Reintentar carga de imágenes** conserva el formulario y no crea un laboratorio. **Formulario completo — preparación del host no comprobada** solo confirma el formulario; la presencia de una imagen no demuestra que estén preparados los archivos de Workload ni los requisitos del host.

Consulta también: [Referencia de la API REST de Playground](../api/playground.md).

El Playground es el flujo de entrada principal de AstinusLab para labs de ataque y forense. Te permite levantar targets vulnerables, VMs atacantes y Network Nodes, conectarlos mediante redes multi-segmento, ejecutar experimentos de seguridad, capturar evidencia bajo demanda y desmontarlo todo cuando termines — todo en una sola sesión.

Los ejemplos de API de abajo usan el helper reutilizable `api_curl` en Bash documentado en [Ejemplos](../usage/examples.md), que gestiona la autenticación Bearer en modo release y el token CSRF en mutaciones de desarrollo.


Para la primera prueba, usa **+ Target → No workload** y captura una VM básica. La sesión se crea automáticamente. Para una carga vulnerable en la red aislada predeterminada, prepara antes una caché de imagen/carga coincidente en Jobs o usa una imagen preparada sin conexión. La instalación online necesita una red con salida elegida explícitamente; el asistente no concede Internet ni comprueba la caché. Consulta [Preparar cargas](PREBAKED_IMAGES.md) antes de lanzar. Review revisa el formulario, no la preparación del host.

## ¿Qué es el Playground?

Piensa en el Playground como un lab efímero donde puedes:

- **Levantar targets, atacantes y Network Nodes** sin crear primero un Job. Elige imágenes del catálogo, etiquétalas y arráncalas.
- **Construir redes** — aisladas o enrutadas, con CIDRs personalizados. Usa múltiples redes para modelar topologías de pivote.
- **Capturar evidencia** — volcados de memoria y disco en cualquier VM en ejecución, en cualquier momento, con sidecars de manifiesto y SHA-256.
- **Trabajar de forma interactiva** — inspecciona VMs, ejecuta ataques o comprobaciones defensivas, dispara capturas sobre la marcha.
- La limpieza es asíncrona; cerrar la sesión libera VMs y redes y conserva evidencias completadas en Storage. Espera el estado final y resuelve cualquier fallo.

### Restricciones clave

- **Capacidad**: varios Jobs pueden conservar targets vivos y atacantes opcionales dentro de los límites agregados de RAM, disco y número de VMs. El único worker de ciclo de vida serializa operaciones; no limita el sistema a un único Job vivo. Un Playground ordinario es exclusivo con Jobs y solo admite una sesión activa. Las instancias experimentales de Packages tienen su propio ciclo de vida y quedan fuera del alcance soportado de 1.0.0. Consulta [Configuración](../usage/configuration.md).
- El efecto de un reinicio depende del servicio y del propietario de la sesión; consulta la sección de reinicios y limpieza.
- **Importación/exportación de recetas y paquetes** — la creación guiada de labs en la UI es la puerta de entrada de V1. Las recetas actuales de Playground son JSON (`schema_version: 1`) y se pueden exportar desde la sesión activa o importar en una sesión. Los paquetes de escenario envuelven esa receta YAML, metadatos y scripts opcionales en un zip para compartir fácilmente. Los flujos YAML escritos a mano siguen siendo el camino de repetibilidad a largo plazo.

---

## Iniciar una sesión de Playground

1. Abre **Playground** desde la barra lateral.
2. Usa **+ Target**, **+ Attacker**, **+ Network Node** o importa una receta.
3. La primera acción crea o reutiliza la sesión automáticamente; la UI no tiene un botón Start Session.

El lienzo muestra la topología. Al seleccionar una VM, el panel de detalles muestra estado, IPs, NICs, SSH, servicios y controles de captura. El pie muestra la sesión y **End Session** cuando existe. Los artefactos se conservan en **Storage**. La API permite crear una sesión vacía explícita con `POST /api/v1/playground/sessions`.

---

## Crear redes

Las redes definen la topología de conectividad de tus VMs. Cada red tiene un nombre, un rango CIDR y un flag de aislamiento.

### Redes aisladas vs. enrutadas

- **Aislada** (`isolated=true`) — esta red libvirt no reenvía tráfico a redes externas. Los invitados pueden comunicarse entre sí y con el anfitrión mediante su dirección de puente, según las reglas de firewall. Esto no aísla los servicios del anfitrión: restringe sus puertos de escucha por separado.
- **Enrutada** (`isolated=false`) — las VMs de esta red pueden alcanzar el host y otras redes enrutadas vía NAT/enrutamiento. Úsala para conectividad saliente o para puentear segmentos.

### Crear una red

1. Abre **+ Target**, **+ Attacker** o **+ Network Node** en la cabecera. En el paso de redes del asistente, pulsa **+ Nueva** (**+ New** en inglés) para abrir **Añadir red a la VM**.
2. Introduce un nombre (p. ej., `internal`, `dmz`, `guest`) o acepta el sugerido.
3. Introduce un **CIDR** o acepta el siguiente rango libre `10.42.x.0/24` sugerido. Los CIDR de una sesión no pueden solaparse; una petición con solapamiento se sustituye por un rango libre y muestra un aviso.
4. Mantén marcada la opción de aislamiento para desactivar el reenvío externo; desmárcala para conectividad enrutada (NAT).
5. Confirma la red y completa el asistente para crear la VM. La red pasa de `pending` a `active` durante la creación del laboratorio; añadirla al formulario todavía no inicia recursos.

---

## Desplegar VMs

Una VM se arranca desde una imagen del catálogo y se conecta a una o más redes vía NICs (interfaces de red). Targets, atacantes y Network Nodes usan todos este flujo de VM.

### Network Nodes

Los Network Nodes son VMs ligeras con varias NIC para una imagen que ya proporcione funciones de router, firewall, proxy, VPN o IDS. AstinusLab conecta las NIC y aplica los recursos predeterminados (1 vCPU, 512 MB de RAM, 4 GB de disco); la configuración del reenvío, las reglas y los servicios de red corresponde al operador. Conecta dos o más NIC cuando quieras situar el nodo entre segmentos.

Las capturas están desactivadas por defecto para los Network Nodes; captura las VMs target cuando necesites evidencia forense.

### Desplegar una VM

1. En la cabecera, pulsa **+ Target**, **+ Attacker** o **+ Network Node**.
2. Introduce una etiqueta única en la sesión y elige una imagen de SO del catálogo. Solo se muestran imágenes aptas para Playground.
3. Para un Target, elige opcionalmente un Workload compatible. Revisa sus requisitos de preparación antes de continuar.
4. Selecciona las redes: cada una añade una NIC. Para un pivote o un Network Node entre segmentos, selecciona dos o más. Sin selección, se crea y utiliza una red aislada `default` (`10.42.1.0/24`). Los Targets permiten ajustar vCPU, memoria y disco; los otros roles aplican sus valores predeterminados.
5. Revisa el formulario y confirma la creación. La primera acción de laboratorio crea o reutiliza automáticamente la sesión; abrir o completar el formulario no comprueba la preparación del host.

AstinusLab arranca la VM y actualiza sus IP a medida que están disponibles. Selecciona la máquina para abrir el panel de detalles, que en estado `running` muestra:

- **Label** e **Image**
- **State** (`pending`, `booting`, `running`, `destroying`, `failed`)
- **IPs por NIC** — la IP del guest asignada a cada interfaz de red
- **Botones de acción** — reiniciar, apagar, arrancar, destruir la VM, **Capturar RAM** y **Capturar disco**

---

## Acceder a las VMs vía SSH

Hasta que el acceso a la consola VNC/SPICE esté disponible en una versión futura, debes interactuar con las VMs vía SSH. AstinusLab inyecta automáticamente una clave SSH en las VMs que inicia.

Para acceder por SSH a una VM en una red **enrutada o aislada** desde tu máquina anfitriona, usa la clave gestionada del espacio de trabajo y la IP del invitado que se muestra en la tarjeta de la VM:

Usa el comando SSH mostrado para esa VM y su IP/usuario. La clave seleccionada se registra por nombre en `<storage_dir>/ssh_keys/.active`; el archivo privado es `<storage_dir>/ssh_keys/<nombre-activo>`, sin sufijo. El nombre inicial del lanzador es `astinuslab-default`, pero puede cambiar en **Security**. Resuelve `storage_dir` desde la configuración: en una instalación suele estar bajo la raíz de datos, no bajo `current`. Una VM existente puede conservar la clave inyectada al crearla aunque cambie la selección; conserva esa clave. Con identidades de servicio separadas, usa el acceso autorizado y no copies claves privadas a ubicaciones públicas.
*(Consulta la página del catálogo de Imágenes si no estás seguro del `<username>` predeterminado para una imagen específica.)*

**Redes aisladas:** el anfitrión tiene una dirección en el puente libvirt. Puedes
usar la IP y la clave indicadas para SSH directo si SSH y los firewalls lo permiten.
Una VM pivote es opcional para acceder desde otro segmento. Los invitados también
pueden alcanzar servicios del anfitrión; desactivar el reenvío no sustituye al firewall.
Consulta la [semántica de libvirt](https://libvirt.org/formatnetwork.html#isolated-network-config).

---

## Ver la Topología

El Playground muestra siempre el **mapa del laboratorio**, con VMs, redes y sus conexiones. Arrastra los nodos para organizarlo; la disposición se guarda automáticamente. Selecciona una máquina para abrir su panel de detalles. Usa el mapa para revisar las conexiones entre segmentos antes de capturar.

---

## Importar y exportar recetas y paquetes

Playground soporta dos formatos para compartir:

- **Receta JSON** — `PlaygroundRecipe` sin envolver desde la sesión activa.
- **Zip de paquete de escenario** — archivo compartible con `metadata.yaml`, `scenario.yaml` y entradas opcionales de scripts relativas y seguras.

### Exportar una receta

1. Inicia o abre una sesión de Playground.
2. Construye redes, despliega VMs target, atacante y Network Node, y organiza la topología.
3. Haz clic en **Export Recipe**.
4. AstinusLab descarga `<session-id>.recipe.json`.

Las recetas exportadas incluyen:

- `schema_version: 1`
- `name`
- `networks` con nombres, CIDRs y flags de aislamiento
- `vms` con etiquetas, roles, IDs de imagen, IDs de workload opcionales, `vcpus`, `memory_mb`, `disk_gb` opcionales y nombres de red
- posiciones de nodos de `topology`

### Importar una receta

1. Haz clic en **Import Recipe**.
2. Pega el JSON de la receta.
3. Envía la importación.

AstinusLab valida nombres de red y etiquetas de VM únicos, rechaza referencias a redes desconocidas, crea las redes que faltan, despliega VMs y restaura las posiciones de topología. La importación en una sesión existente falla con `409 Conflict` si los nombres de la receta colisionan con redes o VMs existentes.

### Exportar un paquete de escenario

Usa la API REST para descargar el Playground activo como paquete compartible de un clic:

```bash
api_curl -OJ "$BASE_URL/api/v1/scenario-packages/playground/current"
```

Contenido del paquete:

- `metadata.yaml` — tipo de paquete, versión de esquema y metadatos visibles.
- `scenario.yaml` — datos de la receta de Playground.
- Rutas de script opcionales — entradas relativas seguras incluidas por los helpers de paquete del backend cuando flujos futuros de catálogo/workload las referencien.

### Importar un paquete de escenario con recursos ajustados

Sube el zip a otra instancia de AstinusLab:

```bash
api_curl -X POST "$BASE_URL/api/v1/scenario-packages/import" \
  -F "file=@dvwa-lab.zip"
```

Para redimensionar máquinas sin editar el archivo, pasa `resource_overrides` como JSON indexado por etiqueta de VM:

```bash
api_curl -X POST "$BASE_URL/api/v1/scenario-packages/import" \
  -F "file=@dvwa-lab.zip" \
  -F 'resource_overrides={"target-1":{"vcpus":4,"memory_mb":8192,"disk_gb":80}}'
```

Los campos permitidos son `vcpus`, `memory_mb` y `disk_gb`. La importación valida las sobreescrituras mediante el mismo esquema de receta de Playground antes de crear redes o desplegar VMs.

---

## Capturar artefactos forenses

Puedes disparar capturas de memoria y disco en VMs target y atacante en ejecución. Las capturas están desactivadas por defecto para los Network Nodes; captura las VMs target cuando necesites evidencia forense.

### Captura de memoria

1. Selecciona la VM en el mapa para abrir su panel de detalles.
2. Haz clic en **Capturar RAM** (**Capture RAM** en inglés).
3. AstinusLab adquiere la RAM del guest vía `virDomainCoreDump` (formato ELF, sin modificación del guest).
4. El progreso aparece en la consola de captura. Al terminar, la evidencia aparece en el panel de detalles y en **Storage**, con enlaces al artefacto, el manifiesto y el SHA-256.

### Captura de disco

1. Selecciona la VM en el mapa para abrir su panel de detalles.
2. Haz clic en **Capturar disco** (**Capture Disk** en inglés).
3. AstinusLab pausa el dominio brevemente, convierte el disco a un artefacto qcow2 sin compresión y reanuda la VM.
4. El artefacto aparece en **Storage**.

### Acceder a los artefactos capturados

Todas las capturas de la sesión aparecen en **Storage** con enlaces de descarga y metadatos (tipo, tamaño, marca de tiempo). Haz clic en el enlace para descargar directamente.

### Ciclo de vida del artefacto

Los artefactos capturados son evidencia forense y se tratan de forma separada de la sesión efímera que los produjo:

- **Las capturas persisten entre reinicios del worker.** Cuando el worker de Celery se reinicia, el barrido de recuperación de arranque solo purga los overlays de disco de VM obsoletos (bajo `playground_disk_dir`) de sesiones que ya no están activas. El directorio de capturas (`playground_capture_dir`) nunca lo toca la recuperación, así que los volcados de memoria, imágenes de disco, manifiestos y sidecars SHA-256 completados permanecen intactos.
- **Las capturas persisten tras la destrucción de la sesión.** Finalizar la sesión de playground destruye sus VMs y redes, pero los artefactos ya producidos por `Capture Memory` / `Capture Disk` quedan en disco y sus filas `Acquisition` quedan en la base de datos. Siguen disponibles a través d**Storage** y el explorador de almacenamiento hasta que los elimines explícitamente.
- **Cadena de custodia.** Cada captura se escribe con un manifiesto y un archivo SHA-256 junto a ella; estos se escriben una vez en el momento de la captura y nunca los modifican los caminos de recuperación, limpieza o fin de sesión.

---

## Control de energía de la VM

El panel de detalles de la VM tiene botones de acción. Espera a que termine una transición de estado antes de repetir una operación:

- **Reboot** — reinicio ordenado vía ACPI. La VM sigue arrancada y conectada a sus redes.
- **Shutdown** — apagado ordenado. La VM se detiene pero el registro de la VM permanece. Haz clic en **Start** para volver a levantarla.
- **Start** — arranca una VM que estaba apagada (solo disponible cuando la VM no está en ejecución).
- **Destroy** — apagado ordenado → destrucción forzada. La VM se elimina, su disco se borra y el registro de la VM se marca como destruido.

---

## Detener la sesión

Cuando termines:

1. Pulsa **Finalizar sesión** (**End Session** en inglés) en el pie de la sesión y confirma la solicitud.
2. **Limpieza solicitada** (`destroy_requested`) confirma la aceptación; **Limpiando…** (`destroying`) indica que sigue en curso. Espera **Sesión finalizada. Se conservan las evidencias capturadas.** (`cancelled` tras Finalizar sesión, o `completed`).
3. Si aparece **Limpieza incompleta** (`destroy_failed` o `failed`) o no está disponible el estado, consulta Logs con el ID de sesión. Los recursos del host pueden seguir reservados; no des por terminada la limpieza. Las evidencias completadas se conservan en Storage.

Todos los artefactos capturados siguen disponibles en **Storage** hasta que los elimines manualmente desde el Almacenamiento.

---

## Restricciones y reglas de validación

- **Sesión activa única** — no puedes iniciar un nuevo playground si ya hay uno en ejecución. Primero debes destruir la sesión actual.
- **Unicidad de etiquetas** — las etiquetas de VM deben ser únicas dentro de una sesión.
- **Solapamiento de CIDR** — los CIDRs de red dentro de una sesión no deben solaparse.
- **Red en uso** — no puedes eliminar una red mientras haya VMs aún conectadas a ella. Destruye primero las VMs, o pasa un flag de cascada (solo API).
- **Estado de captura** — solo puedes disparar capturas en VMs target y atacante en estado `running`. Las capturas están desactivadas por defecto para los Network Nodes.
- **Sin ejecución de jobs solapada** — si hay un Job forense activo, no puedes iniciar un playground, y viceversa.

---

## Modelo de seguridad

V1 es localhost/local-confiable por defecto. Si vinculas la API/UI fuera de loopback, habilita la autenticación de API con un único admin y aun así usa TLS, VPN, firewall o reverse proxy para redes compartidas.

Las VMs guest son intencionadamente hostiles o vulnerables. El endurecimiento de guest hostil debería seleccionarse durante la creación del lab cuando esté disponible; hasta que esos controles estén completos, prefiere redes aisladas para labs de exploit y trata las credenciales del guest, las URLs de servicio y los artefactos capturados como sensibles.

---

## Limitaciones conocidas (diferidas)

- **Acceso a consola** — el proxy de consola VNC/SPICE está planificado para una versión futura. Hasta entonces, usa la IP del guest para hacer SSH directamente desde tu máquina (ver [Acceder a las VMs vía SSH](#acceder-a-las-vms-via-ssh)).
- **Snapshots y restauración** — el playground no soporta checkpoints de guardar-estado / restaurar.
- **Controles de recursos** — la API acepta `vcpus`, `memory_mb` y `disk_gb` por VM. El asistente de Target permite ajustar vCPU, memoria y disco; los creadores de Attacker y Network Node aplican los valores predeterminados de su rol.
- **Imágenes subidas por el usuario** — no puedes subir archivos qcow2 crudos fuera del catálogo para uso en playground.
- **Targets Windows** — roadmap; V1 se centra en labs vulnerables de aplicaciones web Linux.
- **Telemetría en vivo y experiencias de informes más completas**, más allá de los resúmenes de Volatility y los informes de plugins ya publicados — roadmap; V1 se centra en capturas, artefactos y salida de Volatility 3.

---

## Flujos de trabajo de ejemplo

### Volcado de memoria de una sola VM

1. Inicia una sesión.
2. Despliega una VM desde la imagen que elijas.
3. Espera a que alcance el estado `running`.
4. Pulsa **Capturar RAM**.
5. Descarga el volcado desde **Storage**.
6. Destruye la sesión.

### Topología de pivote multi-segmento

1. Inicia una sesión.
2. Crea tres redes: `dmz` (10.0.0.0/24, enrutada), `internal` (10.1.0.0/24, aislada), `sensitive` (10.2.0.0/24, aislada).
3. Despliega una VM `attacker` solo en `dmz`.
4. Despliega una VM `pivot` en `dmz` e `internal`.
5. Despliega una VM `target` en `internal` y `sensitive`.
6. El atacante puede alcanzar el pivote vía `dmz`; el pivote ve ambos segmentos; el target está detrás del pivote.
7. Dispara capturas en cualquier VM para obtener snapshots forenses en cualquier punto.
8. Destruye la sesión cuando termines.

### Explotar y capturar

1. Inicia una sesión.
2. Crea una red enrutada y despliega una VM vulnerable.
3. Desde tu máquina host, haz SSH a la IP del guest y ejecuta un exploit.
4. Una vez el estado del exploit está capturado en memoria, pulsa **Capturar RAM** desde la UI.
5. Analiza el volcado con Volatility 3 o tus herramientas preferidas; Volatility 3 es el camino central de análisis de memoria de V1.
6. Destruye la sesión.

---

## Resolución de problemas

### La VM no arranca o agota el tiempo

- Comprueba que la imagen es válida y tiene un nombre de usuario por defecto configurado (visible en la página de Imágenes).
- Asegúrate de que la conectividad SSH no esté bloqueada por reglas de firewall.
- Revisa los logs del runner por errores de sondeo de IP del guest.

### La creación de red falla con error de solapamiento de CIDR

- Revisa los CIDRs de las redes existentes. AstinusLab no permite rangos solapados dentro de una sesión.
- Elige un CIDR disjunto y reintenta.

### La captura se cuelga o falla

- Asegúrate de que la VM esté en estado `running` (revisa la tarjeta de la VM).
- Las capturas de memoria pueden tardar varios minutos en imágenes grandes. Revisa los logs del runner para ver el progreso.
- Las capturas de disco pausan la VM brevemente (~1s para discos pequeños, más para grandes). Da tiempo para que la pausa y la conversión se completen.

### No se puede eliminar una red

- Verifica que no haya VMs aún conectadas a la red. Destruye o reconecta primero todas las VMs dependientes.
- Revisa el mensaje de error en la UI para ver la lista de VMs que bloquean la eliminación.

### Reinicios y limpieza

Reiniciar solo la API o el worker de análisis no ejecuta la limpieza de inicio de Playground. Reiniciar el runner de ciclo de vida invoca la recuperación y termina las sesiones Playground ordinarias. Las sesiones congeladas del creador y las sesiones de paquetes activos están protegidas por su propio ciclo de verificación/instancia. Reiniciar todo el host también reinicia esos servicios e interrumpe los guests; proteger el registro no garantiza ejecución ininterrumpida de la VM.

**End Session** encola la limpieza. La página sigue esa sesión tras recargar y solo confirma el cierre con `cancelled` (el resultado de End Session) o `completed`. `destroy_failed` o `failed` requiere atención; los recursos pueden seguir reservados. Consulta **Logs** con el ID de sesión conservado. Un registro ausente o un estado no disponible no confirma la limpieza. Las capturas completadas y sus sidecars permanecen en **Storage**.


---

## Siguientes pasos

- **Analiza con Volatility 3** — una vez tengas un volcado de memoria, usa [Volatility](VOLATILITY3.md) para analizar la memoria del guest con tus plugins preferidos.
- **Aprende el flujo de Jobs** — para adquisiciones repetibles basadas en escenarios, consulta [Primeros pasos](../usage/getting-started.md).
- **Configura redes personalizadas** — el playground usa redes libvirt estándar. Los usuarios avanzados pueden inspeccionar el XML de red con `virsh net-dumpxml` o modificar las definiciones del catálogo para topologías complejas.

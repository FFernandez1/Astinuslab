# Paquetes de laboratorio portátiles

**Experimental y sin aceptación para 1.0.0.** Los paquetes están desactivados
por defecto y fuera de los flujos soportados de la primera versión. Sigue
pendiente la aceptación del ciclo completo, en dos hosts, sin red y de rendimiento
(P1–P3). Esta página documenta el código experimental para pruebas controladas,
sin esas garantías. La etiqueta **Bloqueado** no certifica aceptación de release.

El selector de intentos de verificación permite volver a consultar intentos en ejecución, fallidos y cancelados recientemente, con un motivo seguro y un ID que se puede copiar. El historial de verificaciones completadas muestra el título, la versión y la fecha límite real de exportación en UTC. Las certificaciones caducadas o no disponibles siguen siendo legibles, con la exportación desactivada; tras la caducidad, reconstruye el laboratorio y vuelve a verificarlo. Los rechazos de exportación aparecen junto a la fila afectada y conservan tus opciones de inclusión de archivos y redistribución.

Los paquetes están diseñados para compartir una experiencia de laboratorio versionada
entre instalaciones locales de AstinusLab. Incluyen topología, recursos,
runbook, licencias, resúmenes SHA-256 y, cuando se permite, capas de arranque
inmutables. No son un servicio en la nube.

Solo para pruebas experimentales controladas, actívala con
`[packages].enabled = true` en `astinuslab.toml` y abre **Paquetes** en la
barra lateral.

## Flujo del creador

1. Activa los paquetes y reinicia la API/aplicación para registrar las rutas y la entrada lateral. Construye y valida el laboratorio en Playground; mantén abierta esa sesión.
2. Abre **Paquetes → Crear un paquete**. Introduce ID, versión, título, creador y licencia y pulsa **Cargar desde el Playground actual**. Se genera el JSON canónico `LabDefinition` y se rellena el ID de la sesión sin congelar ni cambiar el laboratorio. Las estimaciones de recursos parten de los tamaños declarados de las VMs: revísalas. Cambiar los metadatos requiere cargar de nuevo; las opciones avanzadas se editan en el JSON.
3. Añade el runbook, objetivos, pistas, checkpoints y referencias de archivos. Objetivos/pistas son matrices JSON de cadenas; los checkpoints son observaciones/comprobaciones tipadas del guest, nunca hooks del host. Los archivos observados son JSON finito bajo `/var/lib/astinuslab/observations`, leídos por QEMU Guest Agent con un límite de 64 KiB. El autor de la imagen debe impedir enlaces simbólicos hacia otras rutas: QGA aplica una raíz léxica.
4. Pulsa **Guardar borrador y verificar ejecución limpia** con la sesión aún abierta. La verificación la congela automáticamente, se ejecuta de forma asíncrona y limpia la sesión al terminar o fallar. No pulses **End Session** antes. Sigue el progreso y cualquier fallo o cancelación.
5. Los intentos correctos permanecen en las verificaciones completadas tras limpiar la sesión o recargar el navegador. Exporta allí la verificación elegida: su ID vincula definición, runbook y capas atestadas. La incrustación está desactivada por defecto. Si la atestación exige capas incrustadas, actívala y confirma explícitamente los derechos de redistribución; de lo contrario se rechaza con `embedding_required` o `redistribution_unconfirmed`. Nunca se cambia silenciosamente el contenido verificado.
6. Para compartir solo topología e instrucciones, exporta **Sin bloqueo** desde una sesión Playground activa. Se marca como no reproducible y no demuestra bytes iniciales idénticos.

Es obligatorio añadir un runbook no vacío antes de verificar. Exporta en las 24 horas posteriores a la verificación correcta: después caduca la atestación y la exportación devuelve `attestation_expired`. Conserva el archivo descargado. Para obtener otra atestación, reconstruye el laboratorio y verifícalo de nuevo; el registro completado no recupera la sesión ya limpiada.

Una receta JSON de Playground describe la topología visual para importarla en Playground. El JSON canónico `LabDefinition` es la entrada del creador con identidad, recursos y experiencia. El archivo `.astinuslab` es el paquete que recibe **Importar / inspeccionar**. No son formatos intercambiables: cargar desde Playground realiza la conversión. La inspección y el renderizado son pasivos; no ejecutan el runbook ni contenido del guest.

## Flujo del receptor

**Usar un paquete** es la vista inicial. **Abrir paquete** recupera las instrucciones conservadas sin volver a importar. Las acciones corresponden al paquete seleccionado; cada panel de ejecución mantiene el nombre de su propio paquete. **Abrir laboratorio en Playground** comprueba que la sesión de la instancia sigue activa. Si la limpieza está incompleta, conserva el ID y consulta **Logs**: los recursos pueden seguir reservados; no se ofrece una reparación automática.

En **Importar / inspeccionar**, selecciona el archivo `.astinuslab`. La
inspección es inerte: valida límites, rutas, firmas e inventario SHA-256 sin
crear VM ni ejecutar contenido. Pulsa **Importar paquete inspeccionado** solo
después de revisar identidad, confianza, licencia, recursos y activos.

Después pulsa **Prepare** para resolver bytes incrustados, caché local y, por
último, fuentes fijadas permitidas explícitamente. La red de fuentes requiere
opt-in y política local. Ejecuta **Preflight**, revisa almacenamiento retenido,
capacidad, topología, reachability y advertencias; marca cada confirmación y el
egress antes de **Launch**. El servidor vuelve a calcular el preflight.

El lanzamiento devuelve una operación persistente. Espera a `ready`; **Reset**
recrea capas escribibles desde la capa inmutable y **Teardown** libera la
instancia sin eliminar contenido aún referenciado. La eliminación y GC esperan
hasta que desaparezcan referencias y leases.

## Host y límites

El perfil previsto para la aceptación es Ubuntu 24.04 LTS en x86_64 con KVM.
Ningún host está aceptado para el flujo portátil completo. Las versiones
posteriores de Ubuntu y otras distribuciones Linux no están verificadas; el uso
experimental no implica compatibilidad ni verificación. Un laboratorio pequeño
suele sugerir 2–4 hilos CPU, 4–8 GB RAM y 40 GB libres. Cada paquete define
mínimos y recomendaciones. El flujo es local: no ofrece publicación cloud ni
coordinación multi-host. La exportación sin bloqueo solo intercambia topología y
runbook. P1–P3 deben pasar en un mismo candidato antes de incorporar este flujo al alcance soportado.

## API

La misma operación está disponible bajo `/api/v1/lab-packages`: `inspect`,
`imports/{inspection_id}/commit`, listado, `prepare`, `preflight`, `launch`,
`operations/{instance_id}`, `drafts` y `verify-clean-run`. Las mutaciones
requieren CSRF y, si se configura, autenticación administrativa. Conserva las
claves de idempotencia al reintentar Prepare o Launch.

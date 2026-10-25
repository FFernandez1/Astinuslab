# ADR: Generación de ISF basada en VM

## Estado

Aceptado.

## Contexto

El análisis de Linux con Volatility 3 requiere un ISF que coincida con el kernel exacto que produjo el volcado de memoria. Las etiquetas de las imágenes cloud y los metadatos del catálogo son útiles, pero no bastan: las actualizaciones de paquetes, las reconstrucciones de imágenes y los kernels personalizados pueden cambiar el kernel en ejecución.

AstinusLab ya aprovisiona VMs recién creadas para la adquisición. Ese ciclo de vida le da al runner un sistema en vivo del que puede detectar el kernel real, instalar paquetes de depuración cuando hace falta y construir un ISF coincidente antes de que la VM se destruya.

## Decisión

Las VMs recién creadas siguen siendo la arquitectura central para la generación de ISF de Linux.

- AstinusLab detecta el kernel en ejecución desde el guest.
- AstinusLab puede obtener paquetes de depuración de la distro en el host cuando los repositorios los proporcionan.
- AstinusLab puede instalar paquetes de depuración dentro del guest cuando se necesita el camino de la VM en vivo.
- Los ISFs generados son artefactos de caché global reutilizables en `vol3/symbols/linux/`.
- La reutilización de la caché está condicionada tanto por la **release del kernel** como por el **build ID**. Un ISF en caché para el mismo `uname -r` pero con un GNU build ID diferente (backport del proveedor, reconstrucción de seguridad) se trata como un miss de caché y se reconstruye. Cuando el build ID del ISF en caché es desconocido (artefacto preexistente) se aplica un soft-hit: el ISF en caché se acepta y se registra el build ID para futuras comparaciones.
- Cada job registra qué ISF produjo o reutilizó, incluyendo el build ID.
- La salida de plugins de Volatility sigue siendo local al job en `storage/dumps/<job_id>/vol3/`.
- Los jobs explícitos solo-ISF fallan cuando no se produce ningún ISF válido.

## Consecuencias

La caché evita reconstruir símbolos para jobs repetidos con el mismo kernel, mientras que la salida de análisis local al job preserva la evidencia por ejecución. El sistema solo soporta las principales distribuciones Linux x86_64; los targets no-x86_64 quedan fuera de alcance.

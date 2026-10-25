# Matriz de soporte de distros

AstinusLab soporta la generación de ISF de Volatility 3 para las principales distribuciones Linux x86_64. Otras arquitecturas de CPU no están soportadas.

## Identificar imágenes

La distro, versión, usuario por defecto y `os_variant` de cada imagen de disco en
`images_dir` se detectan automáticamente con `virt-inspector` (de
`libguestfs-tools`) cuando la imagen se sube o se descarga. Los resultados se
persisten como un sidecar `<image>.meta.json` y los consume el autodescubrimiento
de catálogo, así que añadir soporte para una nueva release de distro ya no requiere
editar código. Si `virt-inspector` no está disponible, AstinusLab recurre a
heurísticas de nombre de archivo (`noble`, `bookworm`, `ubuntu-24.04`, etc.); la
página de administración de imágenes permite editar los campos detectados o disparar
una reinspección. Consulta
[Configuración avanzada → `[image_inspection]`](ADVANCED_CONFIG.md) para ajustar
el timeout de inspección o desactivarla.

La matriz de abajo documenta qué distros tienen actualmente un pipeline de ISF /
símbolos de depuración funcional, independientemente de cómo se identifiquen.

| Distribución | Releases soportadas | DWARF existente | debuginfod | paquete de debug | Notas |
|--------------|--------------------|-----|------------|-----------|-------|
| Ubuntu | 20.04, 22.04, 24.04 | Fallback en guest | Opcional | apt host/live | Requiere `ddebs` coincidentes o paquetes de depuración instalables. |
| Ubuntu | 25.04 | Fallback en guest | Opcional | apt live | El soporte de codename en host fetch no está mapeado actualmente; usa guest en vivo o soporte de repo configurado. |
| Debian | 10, 11, 12 | Fallback en guest | Opcional | apt host/live | Requiere paquetes de símbolos de depuración coincidentes. |
| Debian | 13 | Fallback en guest | Opcional | apt live | El soporte de codename en host fetch no está mapeado actualmente; usa guest en vivo o soporte de repo configurado. |
| Fedora | 38-43 | Fallback en guest | Opcional | dnf host/live | Requiere paquetes `kernel-debuginfo` coincidentes. |
| CentOS Stream | 8, 9 | Fallback en guest | Opcional | dnf host/live | Requiere repositorios debuginfo coincidentes. |
| Rocky Linux | 8, 9 | Fallback en guest | Opcional | dnf host/live | Requiere repositorios debuginfo coincidentes. |
| AlmaLinux | 8, 9 | Fallback en guest | Opcional | dnf host/live | Requiere repositorios debuginfo coincidentes. |
| RHEL | 8, 9 | Fallback en guest | Opcional | dnf live | Requiere repositorios de Red Hat o paquetes debuginfo equivalentes. |
| Oracle Linux | 8, 9 | Fallback en guest | Opcional | dnf host/live | Tratado como familia compatible con RHEL cuando hay paquetes debuginfo disponibles. |
| openSUSE Leap | 15.x | Fallback en guest | Opcional | zypper host/live | Requiere paquetes de depuración coincidentes de los repositorios habilitados. |
| SUSE Linux Enterprise Server | 15.x | Fallback en guest | Opcional | zypper live | Requiere acceso a paquetes de depuración SLES coincidentes. |
| Arch Linux | Rolling | Fallback en guest | Opcional | pacman live | La disponibilidad de paquetes de kernel/depuración depende del conjunto de paquetes actual y los mirrors. |
| Alpine Linux | 3.x | Necesario | Opcional | apk limitado | Se necesita un kernel de depuración coincidente con DWARF; BTF por sí solo no es compatible. |
| Kali Linux | Rolling | Fallback en guest | Opcional | apt familia Debian | Tratado como derivado de Debian cuando los metadatos de paquetes proporcionan símbolos de depuración coincidentes. |
| Parrot OS | Rolling | Fallback en guest | Opcional | apt familia Debian | Tratado como derivado de Debian cuando los metadatos de paquetes proporcionan símbolos de depuración coincidentes. |

## Alcance

- Arquitectura soportada: solo `x86_64`.
- Artefacto soportado: ISF JSON de Volatility 3 para Linux en `vol3/symbols/linux/`.
- Salida de análisis soportada: salida de plugins de Volatility local al job en `storage/dumps/<job_id>/vol3/`.
- No soportado: ARM, AArch64, ppc64le, s390x, guests no-Linux y kernels sin símbolos de depuración accesibles.

## Requisitos prácticos

- El guest debe arrancar lo suficiente para que AstinusLab detecte el kernel en ejecución.
- El paquete de depuración del kernel exacto debe estar disponible desde los repositorios de la distro o ser instalable dentro del guest.
- El acceso al repositorio debe funcionar desde el flujo de host fetch o desde el flujo de guest en vivo.
- El ISF generado debe ser válido para el kernel detectado antes de que un job solo-ISF pueda tener éxito.

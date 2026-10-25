# Matriu de suport de distros

AstinusLab suporta la generació d'ISF de Volatility 3 per a les principals distribucions Linux x86_64. Altres arquitectures de CPU no estan suportades.

## Identificar imatges

La distro, versió, usuari per defecte i `os_variant` de cada imatge de disc a
`images_dir` es detecten automàticament amb `virt-inspector` (de
`libguestfs-tools`) quan la imatge es puja o es descarrega. Els resultats es
persisteixen com un sidecar `<image>.meta.json` i els consumeix l'autodescobriment
de catàleg, així que afegir suport per a una nova release de distro ja no requereix
editar codi. Si `virt-inspector` no està disponible, AstinusLab recorre a
heurístiques de nom de fitxer (`noble`, `bookworm`, `ubuntu-24.04`, etc.); la
pàgina d'administració d'imatges permet editar els camps detectats o disparar una
reinspecció. Vegeu
[Configuració avançada → `[image_inspection]`](ADVANCED_CONFIG.md) per ajustar
el timeout d'inspecció o desactivar-la.

La matriu de sota documenta quines distros tenen actualment un pipeline d'ISF /
símbols de depuració funcional, independentment de com s'identifiquin.

| Distribució | Releases suportades | DWARF existent | debuginfod | paquet de debug | Notes |
|--------------|--------------------|-----|------------|-----------|-------|
| Ubuntu | 20.04, 22.04, 24.04 | Fallback en guest | Opcional | apt host/live | Requereix `ddebs` coincidents o paquets de depuració instal·lables. |
| Ubuntu | 25.04 | Fallback en guest | Opcional | apt live | El suport de codename en host fetch no està mapejat actualment; fes servir guest en viu o suport de repo configurat. |
| Debian | 10, 11, 12 | Fallback en guest | Opcional | apt host/live | Requereix paquets de símbols de depuració coincidents. |
| Debian | 13 | Fallback en guest | Opcional | apt live | El suport de codename en host fetch no està mapejat actualment; fes servir guest en viu o suport de repo configurat. |
| Fedora | 38-43 | Fallback en guest | Opcional | dnf host/live | Requereix paquets `kernel-debuginfo` coincidents. |
| CentOS Stream | 8, 9 | Fallback en guest | Opcional | dnf host/live | Requereix repositoris debuginfo coincidents. |
| Rocky Linux | 8, 9 | Fallback en guest | Opcional | dnf host/live | Requereix repositoris debuginfo coincidents. |
| AlmaLinux | 8, 9 | Fallback en guest | Opcional | dnf host/live | Requereix repositoris debuginfo coincidents. |
| RHEL | 8, 9 | Fallback en guest | Opcional | dnf live | Requereix repositoris de Red Hat o paquets debuginfo equivalents. |
| Oracle Linux | 8, 9 | Fallback en guest | Opcional | dnf host/live | Tractat com a família compatible amb RHEL quan hi ha paquets debuginfo disponibles. |
| openSUSE Leap | 15.x | Fallback en guest | Opcional | zypper host/live | Requereix paquets de depuració coincidents dels repositoris habilitats. |
| SUSE Linux Enterprise Server | 15.x | Fallback en guest | Opcional | zypper live | Requereix accés a paquets de depuració SLES coincidents. |
| Arch Linux | Rolling | Fallback en guest | Opcional | pacman live | La disponibilitat de paquets de kernel/depuració depèn del conjunt de paquets actual i els mirrors. |
| Alpine Linux | 3.x | Necessari | Opcional | apk limitat | Cal un kernel de depuració coincident amb DWARF; BTF sol no és compatible. |
| Kali Linux | Rolling | Fallback en guest | Opcional | apt família Debian | Tractat com a derivat de Debian quan les metadades de paquets proporcionen símbols de depuració coincidents. |
| Parrot OS | Rolling | Fallback en guest | Opcional | apt família Debian | Tractat com a derivat de Debian quan les metadades de paquets proporcionen símbols de depuració coincidents. |

## Abast

- Arquitectura suportada: només `x86_64`.
- Artefacte suportat: ISF JSON de Volatility 3 per a Linux a `vol3/symbols/linux/`.
- Sortida d'anàlisi suportada: sortida de plugins de Volatility local al job a `storage/dumps/<job_id>/vol3/`.
- No suportat: ARM, AArch64, ppc64le, s390x, guests no-Linux i kernels sense símbols de depuració accessibles.

## Requisits pràctics

- El guest ha d'arrencar prou perquè AstinusLab detecti el kernel en execució.
- El paquet de depuració del kernel exacte ha d'estar disponible des dels repositoris de la distro o ser instal·lable dins del guest.
- L'accés al repositori ha de funcionar des del flux de host fetch o des del flux de guest en viu.
- L'ISF generat ha de ser vàlid per al kernel detectat abans que un job només-ISF pugui tenir èxit.

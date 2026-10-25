# Matrice de prise en charge des distros

AstinusLab prend en charge la génération d'ISF Volatility 3 pour les principales distributions Linux x86_64. Les autres architectures de CPU ne sont pas prises en charge.

## Identifier les images

La distro, la version, l'utilisateur par défaut et `os_variant` de chaque image de
disque dans `images_dir` sont détectés automatiquement par `virt-inspector` (de
`libguestfs-tools`) lorsque l'image est téléversée ou téléchargée. Les résultats
sont persistés comme un sidecar `<image>.meta.json` et consommés par
l'auto-découverte du catalogue, donc ajouter la prise en charge d'une nouvelle
release de distro ne nécessite plus d'éditer le code source. Si `virt-inspector`
est indisponible, AstinusLab se rabat sur des heuristiques de nom de fichier
(`noble`, `bookworm`, `ubuntu-24.04`, etc.) ; la page d'administration des images
permet d'éditer les champs détectés ou de déclencher une réinspection. Voir
[Configuration avancée → `[image_inspection]`](ADVANCED_CONFIG.md) pour ajuster
le timeout d'inspection ou le désactiver.

La matrice ci-dessous documente quelles distros disposent actuellement d'un
pipeline d'ISF / symboles de débogage fonctionnel, indépendamment de la façon dont
elles sont identifiées.

| Distribution | Releases prises en charge | DWARF existant | debuginfod | paquet de debug | Notes |
|--------------|--------------------|-----|------------|-----------|-------|
| Ubuntu | 20.04, 22.04, 24.04 | Fallback guest | Optionnel | apt host/live | Nécessite des `ddebs` correspondants ou des paquets de débogage installables. |
| Ubuntu | 25.04 | Fallback guest | Optionnel | apt live | La prise en charge du codename en host fetch n'est pas mappée actuellement ; utilisez le guest en direct ou un repo configuré. |
| Debian | 10, 11, 12 | Fallback guest | Optionnel | apt host/live | Nécessite des paquets de symboles de débogage correspondants. |
| Debian | 13 | Fallback guest | Optionnel | apt live | La prise en charge du codename en host fetch n'est pas mappée actuellement ; utilisez le guest en direct ou un repo configuré. |
| Fedora | 38-43 | Fallback guest | Optionnel | dnf host/live | Nécessite des paquets `kernel-debuginfo` correspondants. |
| CentOS Stream | 8, 9 | Fallback guest | Optionnel | dnf host/live | Nécessite des dépôts debuginfo correspondants. |
| Rocky Linux | 8, 9 | Fallback guest | Optionnel | dnf host/live | Nécessite des dépôts debuginfo correspondants. |
| AlmaLinux | 8, 9 | Fallback guest | Optionnel | dnf host/live | Nécessite des dépôts debuginfo correspondants. |
| RHEL | 8, 9 | Fallback guest | Optionnel | dnf live | Nécessite les dépôts Red Hat ou des paquets debuginfo équivalents. |
| Oracle Linux | 8, 9 | Fallback guest | Optionnel | dnf host/live | Traité comme une famille compatible RHEL lorsque des paquets debuginfo sont disponibles. |
| openSUSE Leap | 15.x | Fallback guest | Optionnel | zypper host/live | Nécessite des paquets de débogage correspondants des dépôts activés. |
| SUSE Linux Enterprise Server | 15.x | Fallback guest | Optionnel | zypper live | Nécessite l'accès aux paquets de débogage SLES correspondants. |
| Arch Linux | Rolling | Fallback guest | Optionnel | pacman live | La disponibilité des paquets kernel/débogage dépend du jeu de paquets actuel et des mirrors. |
| Alpine Linux | 3.x | Requis | Optionnel | apk limité | Un noyau de débogage correspondant avec DWARF est requis ; le BTF seul n'est pas pris en charge. |
| Kali Linux | Rolling | Fallback guest | Optionnel | apt famille Debian | Traité comme un dérivé Debian lorsque les métadonnées de paquets fournissent des symboles de débogage correspondants. |
| Parrot OS | Rolling | Fallback guest | Optionnel | apt famille Debian | Traité comme un dérivé Debian lorsque les métadonnées de paquets fournissent des symboles de débogage correspondants. |

## Périmètre

- Architecture prise en charge : `x86_64` uniquement.
- Artefact pris en charge : ISF JSON Volatility 3 pour Linux sous `vol3/symbols/linux/`.
- Sortie d'analyse prise en charge : sortie des plugins Volatility locale au job sous `storage/dumps/<job_id>/vol3/`.
- Non pris en charge : ARM, AArch64, ppc64le, s390x, guests non-Linux et noyaux sans symboles de débogage accessibles.

## Exigences pratiques

- Le guest doit démarrer suffisamment pour qu'AstinusLab détecte le noyau en cours d'exécution.
- Le paquet de débogage du noyau exact doit être disponible depuis les dépôts de la distro ou installable dans le guest.
- L'accès au dépôt doit fonctionner depuis le flux host fetch ou depuis le flux guest en direct.
- L'ISF généré doit être valide pour le noyau détecté avant qu'un job ISF uniquement puisse réussir.

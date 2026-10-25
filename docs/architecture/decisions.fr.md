# Décisions

Cette page consigne les décisions architecturales actuelles qui affectent la configuration, les opérations et la maintenance.

## Utiliser l'acquisition côté hôte

Les artefacts mémoire et disque sont capturés depuis l'hôte via libvirt et les outils QEMU. Les agents côté guest sont évités pour l'intégrité d'acquisition et la compatibilité des distributions.

## Garder un seul mode de runner actif

Les runners de développement et de release ne doivent pas s'exécuter en même temps. Tous deux peuvent consommer des tâches Celery et opérer sur les ressources libvirt ; un mode mixte peut donc envoyer des jobs au mauvais runner ou provoquer des collisions de noms de VM, de réseaux et de stockage.

## Utiliser SQLite pour l'état local

Le projet utilise SQLite pour la base de données applicative, le broker Celery et le backend de résultats Celery. La concurrence de runner prise en charge est `1`, ce qui correspond au design local mono-hôte et évite les courses sur l'état partagé.

## Utiliser les helpers privilégiés installés

Le runner ne reçoit pas un sudo sans mot de passe large. Il peut appeler des scripts helper installés précis sous `/usr/local/libexec/astinuslab/` via `/etc/sudoers.d/astinuslab-runner`.

## Traiter les catalogues comme des données source maintenues par l'utilisateur

Les targets, scenarios, workloads et attackers sont déclarés dans des catalogues YAML. La découverte automatique d'images peut ajouter des targets, mais les mainteneurs doivent vérifier les utilisateurs, la famille de distro, le profil cloud-init, la variante OS et le comportement réseau.

## Rendre Playground éphémère, garder les captures durables

Les sessions, VMs, réseaux et overlays disque Playground sont temporaires. Les captures, manifestes, checksums et lignes d'acquisition persistent après la destruction de session.

## Génération d'ISF basée sur VM

Les VMs fraîches sont la source de vérité pour l'identité du kernel Linux et la génération d'ISF Volatility 3. Voir [Génération d'ISF basée sur VM](adr-vm-based-isf-generation.md).

## La capacité VM est un contrôle d'admission, pas une concurrence de workers (2026-07)

La limite configurable de VMs exécutées simultanément (`orchestration.max_concurrent_vms`) est appliquée au niveau produit en comptant les lignes `job_vms` dans des états consommateurs de capacité à l'intérieur de la transaction d'admission (`BEGIN IMMEDIATE`). Les workers Celery restent à `--concurrency=1` : le broker SQLite, les verrous de capture en processus, la sérialisation des tâches beat et la réconciliation mono-hôte supposent tous un runner sériel, et cet invariant est fixé par `tests/python/test_deploy_config.py`.

La valeur par défaut est sans limite, ce qui préserve le comportement existant. Les états VM transitoires comme `destroying` et `teardown_failed` comptent dans la limite jusqu'à ce que la réconciliation les résolve.

## Utiliser les frontières de package, d'environnement d'exécution et de runtime pour la structure du dépôt (2026-08)

La cible progressive utilise `src/astinuslab` pour l'application installable et les ressources web et de migration appartenant au package, une hiérarchie canonique `tests`, `ops` pour le code hôte et de déploiement, et `tools` pour les utilitaires de contribution et de release. Elle conserve `guest`, `config`, `images`, `vol3` et `storage` comme frontières visibles de premier niveau, car les déplacer sous des conteneurs génériques ajouterait un risque de chemins sans clarifier la responsabilité.

Les chemins du dépôt sont des interfaces de migration : chaque déplacement exige une matrice producteurs/consommateurs, une vérification et un rollback. Les imports internes et les chemins source peuvent être cassés dans une étape atomique, tandis que les routes HTTP, noms de tâches Celery, IDs de révision Alembic, noms d'unités systemd, clés de configuration, formats d'artefacts et de catalogues, et URLs statiques publiques restent des contrats stables.

Voir la [politique de placement du dépôt](repository-placement-policy.md), la [structure cible](repository-target-layout.md) et la [matrice de migration des chemins](path-migration-matrix.md). Ces cinq documents internes de structure sont canoniques en anglais et hors de la navigation MkDocs publiée.

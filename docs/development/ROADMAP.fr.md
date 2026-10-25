# Périmètre V1 et feuille de route

AstinusLab V1 est un playground d'attaque et d'investigation forensique pour la recherche en sécurité en local. Il se concentre sur la construction de labos d'applications web vulnérables, l'attachement de VMs attaquantes, la capture de preuves et l'analyse de mémoire avec Volatility 3.

## Périmètre de V1

- Fonctionnement localhost/local de confiance avec authentification optionnelle d'un administrateur par bearer token et session navigateur. Les installations release l'activent par défaut. Le multiutilisateur et le RBAC restent hors du périmètre V1.
- UI guidée du Playground comme flux de labo d'entrée principal.
- Import/export de recettes JSON pour des labos Playground répétables ; la création plus large de recettes YAML reste sur la feuille de route.
- Labos d'applications web vulnérables d'abord : WordPress, DVWA, Juice Shop, services de type GitLab et Workloads personnalisés.
- Support de VM attaquante pour la génération de trafic de type pentest.
- Capture d'artefacts mémoire et disque côté hôte.
- Sidecars de preuve : les manifestes et les sommes de contrôle SHA-256 devraient être standard sur les artefacts de Job et de Playground.
- Volatility 3 est la voie centrale d'analyse de V1 pour la mémoire Linux, à l'aide d'ISF Linux. Les fondations PDB/catalogue Windows sont expérimentales et restent hors des flux pris en charge en 1.0.0.
- **Capacité** : plusieurs Jobs peuvent conserver des targets actifs et leurs attaquants optionnels dans les limites cumulées de RAM, disque et nombre de VM. Le worker de cycle de vie unique sérialise les opérations ; il ne limite pas le système à un seul Job vivant. Un Playground ordinaire est exclusif avec les Jobs et autorise une seule session active. Les instances Packages expérimentales ont leur propre cycle de vie et restent hors du périmètre pris en charge dans 1.0.0. Voir [Configuration](../usage/configuration.md).
- Distribution depuis le checkout du code source plus `ops/deploy/install.sh`.

## Posture de sécurité

V1 suppose un opérateur local de confiance et un accès par localhost. Liez à `127.0.0.1` pour une utilisation normale. N'exposez pas l'API/UI à un LAN ou à Internet sans authentification externe et contrôles de reverse proxy.

Les workloads guest sont intentionnellement vulnérables. Le durcissement guest hostile devrait pouvoir être sélectionné lors de la création du labo à mesure que les contrôles mûrissent ; utilisez des réseaux isolés lorsque vous testez des chaînes d'exploit.

## Feuille de route

- Parité du provisionnement automatisé des cibles Windows.
- Télémétrie en direct continue au-delà des captures discrètes.
- Rapports générés plus riches, au-delà des résumés Volatility et des rapports de plugins déjà publiés.
- Presets de durcissement guest hostile plus riches.
- Support des recettes YAML et une UX de création de recettes plus complète au-delà de l'import/export JSON actuel.

## Hors périmètre pour V1

- Packages de laboratoire portables : désactivés, expérimentaux et non acceptés jusqu’à la réussite de P1–P3 sur un même candidat. Aucune garantie du cycle complet, sur deux hôtes, hors ligne ou de performances.
- Authentification multi-utilisateurs/RBAC à l'intérieur d'AstinusLab.
- Déploiement multi-tenant hébergé dans le cloud.
- Remplacement de SIEM ou plateforme de surveillance permanente.

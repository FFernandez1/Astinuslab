# AstinusLab

AstinusLab est un playground d'attaque et d'investigation forensique pour la recherche en sécurité en local. Créez des labos d'applications web vulnérables, attachez des VMs attaquantes, lancez des attaques, capturez des preuves RAM/disque et analysez la mémoire avec Volatility 3.

Utilisez d'abord Playground pour des labos interactifs guidés. Utilisez les Jobs et les recettes Playground JSON actuelles pour des acquisitions répétables pilotées par scénario, avec des métadonnées reproductibles, des manifestes et des sommes de contrôle SHA-256.

---

## Démarrer

*   [Playground](user/PLAYGROUND.md) - Créez des labos vulnérables, attachez des VMs attaquantes et capturez des preuves à la demande.
*   [Prise en main](usage/getting-started.md) - Installez les prérequis, démarrez les services et lancez votre première acquisition.
*   [Exécution en local](usage/running-locally.md) - Runbook complet de développement et de publication.

## Guides

*   [Configuration](usage/configuration.md) - Paramètres de l'app, environnement du lanceur et catalogues.
*   [Exemples](usage/examples.md) - Flux de travail courants UI et API.
*   [Dépannage](usage/troubleshooting.md) - Résolvez les problèmes courants d'installation, de réseau et de permissions.
*   [Images pré-cuites](user/PREBAKED_IMAGES.md) - Optimisez les performances en intégrant les workloads dans les images de base.
*   [Volatility 3](user/VOLATILITY3.md) - Construisez des ISF réutilisables et lancez une analyse mémoire locale au job.

## Référence

*   [Vue d'ensemble de l'API](api/overview.md) - URLs de base, schémas et modèle de sécurité.
*   [Endpoints de l'API](api/endpoints.md) - Endpoints REST vérifiés par rapport au code source.
*   [Erreurs de l'API](api/errors.md) - Statuts HTTP courants et comportement des réponses.
*   [Périmètre V1 et feuille de route](development/ROADMAP.md) - Limites actuelles du produit et travaux reportés.
*   [Configuration avancée](reference/ADVANCED_CONFIG.md) - Référence de configuration détaillée.
*   [Prise en charge des distros](reference/DISTRO_SUPPORT.md) - Familles Linux x86_64 prises en charge pour les symboles Volatility 3.
*   [Intégrité forensique](reference/FORENSIC_INTEGRITY.md) - Hachage, noms de fichiers canoniques et détails de la gestion des preuves.

## Architecture

*   [Vue d'ensemble de l'architecture](architecture/overview.md) - Responsabilités et limites du système.
*   [Composants](architecture/components.md) - Modules backend, runner, catalogue, stockage et UI.
*   [Flux de données](architecture/data-flow.md) - Flux des jobs, du playground et des artefacts.
*   [Décisions](architecture/decisions.md) - Décisions de conception pour les mainteneurs.

## Développement

*   [Configuration de développement](development/setup.md) - Environnement local et flux de travail du dépôt.
*   [Tests](development/testing.md) - Commandes de test, lint, type-check et CI.
*   [Contribuer](development/contributing.md) - Flux et standards de contribution.
*   [Développement de workloads](development/WORKLOADS.md) - Packagez des scénarios de labo répétables et des stacks de services.

## Communauté

*   [Gouvernance](community/GOVERNANCE.md) — Comment les décisions du projet sont prises.

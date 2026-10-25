# AstinusLab

AstinusLab és un playground d'atac i anàlisi forense per a la recerca de seguretat en local. Crea labs d'aplicacions web vulnerables, connecta VMs atacants, executa atacs, captura evidència de RAM/disc i analitza la memòria amb Volatility 3.

Fes servir primer Playground per a labs interactius guiats. Fes servir Jobs i les receptes actuals de Playground en JSON per a adquisicions repetibles basades en escenaris, amb metadades reproduïbles, manifestos i checksums SHA-256.

---

## Començar

*   [Playground](user/PLAYGROUND.md) - Crea labs vulnerables, connecta VMs atacants i captura evidència sota demanda.
*   [Primers passos](usage/getting-started.md) - Instal·la els prerequisits, inicia els serveis i executa la teva primera adquisició.
*   [Execució en local](usage/running-locally.md) - Runbook complet de desenvolupament i publicació.

## Guies

*   [Configuració](usage/configuration.md) - Ajustos de l'app, entorn del llançador i catàlegs.
*   [Exemples](usage/examples.md) - Fluxos de treball habituals d'UI i API.
*   [Resolució de problemes](usage/troubleshooting.md) - Resol problemes habituals de configuració, xarxa i permisos.
*   [Imatges pre-cuites](user/PREBAKED_IMAGES.md) - Optimitza el rendiment integrant workloads a les imatges base.
*   [Volatility 3](user/VOLATILITY3.md) - Construeix ISFs reutilitzables i executa anàlisi de memòria local al job.

## Referència

*   [Visió general de l'API](api/overview.md) - URLs base, esquemes i model de seguretat.
*   [Endpoints de l'API](api/endpoints.md) - Endpoints REST verificats contra el codi font.
*   [Errors de l'API](api/errors.md) - Estats HTTP habituals i comportament de les respostes.
*   [Abast V1 i full de ruta](development/ROADMAP.md) - Límits actuals del producte i feina diferida.
*   [Configuració avançada](reference/ADVANCED_CONFIG.md) - Referència detallada de configuració.
*   [Suport de distros](reference/DISTRO_SUPPORT.md) - Famílies Linux x86_64 suportades per als símbols de Volatility 3.
*   [Integritat forense](reference/FORENSIC_INTEGRITY.md) - Hashing, noms de fitxer canònics i detalls del maneig d'evidència.

## Arquitectura

*   [Visió general de l'arquitectura](architecture/overview.md) - Responsabilitats i límits del sistema.
*   [Components](architecture/components.md) - Mòduls de backend, runner, catàleg, emmagatzematge i UI.
*   [Flux de dades](architecture/data-flow.md) - Fluxos de jobs, playground i artefactes.
*   [Decisions](architecture/decisions.md) - Decisions de disseny per a mantenidors.

## Desenvolupament

*   [Configuració de desenvolupament](development/setup.md) - Entorn local i flux de treball del repositori.
*   [Proves](development/testing.md) - Ordres de test, lint, type-check i CI.
*   [Contribuir](development/contributing.md) - Flux i estàndards de contribució.
*   [Desenvolupament de workloads](development/WORKLOADS.md) - Empaqueta escenaris de lab repetibles i stacks de serveis.

## Comunitat

*   [Governança](community/GOVERNANCE.md) — Com es prenen les decisions del projecte.

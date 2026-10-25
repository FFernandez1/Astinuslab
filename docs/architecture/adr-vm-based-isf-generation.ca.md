# ADR: Generació d'ISF basada en VM

## Estat

Acceptat.

## Context

L'anàlisi de Linux amb Volatility 3 requereix un ISF que coincideixi amb el kernel exacte que va produir el bolcat de memòria. Les etiquetes de les imatges cloud i les metadades del catàleg són útils, però no n'hi ha prou: les actualitzacions de paquets, les reconstruccions d'imatges i els kernels personalitzats poden canviar el kernel en execució.

AstinusLab ja aprovisiona VMs acabades de crear per a l'adquisició. Aquest cicle de vida dona al runner un sistema en viu del qual pot detectar el kernel real, instal·lar paquets de depuració quan cal i construir un ISF coincident abans que la VM es destrueixi.

## Decisió

Les VMs acabades de crear continuen sent l'arquitectura central per a la generació d'ISF de Linux.

- AstinusLab detecta el kernel en execució des del guest.
- AstinusLab pot obtenir paquets de depuració de la distro a l'host quan els repositoris els proporcionen.
- AstinusLab pot instal·lar paquets de depuració dins del guest quan es necessita el camí de la VM en viu.
- Els ISFs generats són artefactes de memòria cau global reutilitzables a `vol3/symbols/linux/`.
- La reutilització de la memòria cau està condicionada tant per la **release del kernel** com pel **build ID**. Un ISF en memòria cau per al mateix `uname -r` però amb un GNU build ID diferent (backport del proveïdor, reconstrucció de seguretat) es tracta com un miss de memòria cau i es reconstrueix. Quan el build ID de l'ISF en memòria cau és desconegut (artefacte preexistent) s'aplica un soft-hit: l'ISF en memòria cau s'accepta i es registra el build ID per a futures comparacions.
- Cada job registra quin ISF ha produït o reutilitzat, incloent-hi el build ID.
- La sortida de plugins de Volatility continua sent local al job a `storage/dumps/<job_id>/vol3/`.
- Els jobs explícits només-ISF fallen quan no es produeix cap ISF vàlid.

## Conseqüències

La memòria cau evita reconstruir símbols per a jobs repetits amb el mateix kernel, mentre que la sortida d'anàlisi local al job preserva l'evidència per execució. El sistema només suporta les principals distribucions Linux x86_64; els targets no-x86_64 queden fora d'abast.

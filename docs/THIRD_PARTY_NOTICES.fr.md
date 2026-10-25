# Mentions des tiers

> Traduction non officielle. La version anglaise de cette page est la référence faisant autorité.

Cet inventaire sépare le code source appartenant à AstinusLab du matériel tiers qu'AstinusLab distribue réellement, et des logiciels externes et matériels d'exécution utilisés par des flux forensiques optionnels. L'inclusion dans cet inventaire de mentions ne signifie pas qu'un composant tiers listé est inclus dans l'archive source, l'installeur ou le paquet Python d'AstinusLab : lisez la colonne `Statut de distribution`, qui fait autorité ligne par ligne. `Distribué` n'implique pas la propriété d'AstinusLab.

## Inventaire de distribution

| Composant | Statut de distribution | Licence ou termes | Mention |
|---|---|---|---|
| Code source appartenant à AstinusLab | Distribué | Volatility Software License (VSL) 1.0 | Copyright 2026 Francisco Fernandez. Le `LICENSE` racine s'applique uniquement au matériel appartenant à AstinusLab. Il ne relicencie pas le matériel tiers, et AstinusLab ne fait pas partie de Volatility. |
| htmx 2.0.4, intégré dans `src/astinuslab/web/static/htmx.min.js` | Distribué | Zero-Clause BSD (0BSD) | Un des composants tiers distribués, avec Inter et JetBrains Mono. Il voyage à la fois dans l'archive source et dans le package installé, car les données de package setuptools incluent explicitement les ressources web appartenant au package. Le fichier est identique octet pour octet à l'artefact upstream [`v2.0.4` `dist/htmx.min.js`](https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js). 0BSD n'impose aucune condition de conservation de mention ni d'attribution ; le texte upstream exact est conservé dans `LICENSES/htmx-0BSD.txt` pour la complétude de l'inventaire. AstinusLab ne relicencie pas htmx. |
| Inter 4.1 (`src/astinuslab/web/static/fonts/InterVariable.woff2`) | Distribué | SIL Open Font License 1.1 | Avis et licence conservés dans `LICENSES/inter-OFL-1.1.txt`. |
| JetBrains Mono 2.304 (`src/astinuslab/web/static/fonts/JetBrainsMono-*.woff2`, 400/600/700/800) | Distribué | SIL Open Font License 1.1 | Avis et licence conservés dans `LICENSES/jetbrains-mono-OFL-1.1.txt`. |
| Volatility 3 Framework | Non distribué ; fourni par l'opérateur | Volatility Software License (VSL) 1.0 | Copyright 2019 Volatility Foundation. Consultez la [page officielle VSL 1.0](https://www.volatilityfoundation.org/license/vsl-v1.0) et le [code source épinglé de Volatility 3](https://github.com/volatilityfoundation/volatility3/tree/v2.28.0). AstinusLab ne relicencie pas Volatility 3. |
| `dwarf2json` | Non distribué ; fourni par l'opérateur | VSL 1.0 | Copyright 2019 Volatility Foundation. Consultez la [page officielle VSL 1.0](https://www.volatilityfoundation.org/license/vsl-v1.0) et le [code source épinglé audité](https://github.com/volatilityfoundation/dwarf2json/tree/9f14607e0d339d463ea725fbd5c08aa7b7d40f75). L'opérateur vérifie et enregistre l'artefact choisi. |
| `pflag` dans le build officiel audité de `dwarf2json` 0.9.0 | Non distribué par AstinusLab | BSD-3-Clause | Provenance informative uniquement. La mention upstream exacte est conservée dans `LICENSES/pflag-BSD-3-Clause.txt`. |
| Runtime Go dans le build officiel audité de `dwarf2json` 0.9.0 | Non distribué par AstinusLab | BSD-3-Clause | Provenance informative uniquement. La mention upstream exacte est conservée dans `LICENSES/go-BSD-3-Clause.txt`. |
| Paquets de symboles Windows et contenu de catalogue | Matériel externe d'exécution, pas un payload source | Termes du fournisseur upstream | Le matériel reste régi par les termes du fournisseur applicables. Les opérateurs doivent examiner ces termes avant usage ; AstinusLab enregistre les identifiants de source, URLs, hashs et autres provenances disponibles du matériel de symboles acquis. Cet inventaire n'attribue pas de responsabilité juridique. |

Les URLs de source de licence, révisions, dates de récupération et hashs de payload exacts des copies de texte conservées sont enregistrés dans `LICENSES/README.md`.

## Provenance de l'asset frontend intégré et distribué

`src/astinuslab/web/static/htmx.min.js`: Un des composants tiers distribués, avec Inter et JetBrains Mono. Il est suivi dans le dépôt, il voyage donc avec l'archive source, et les données de package setuptools déclarées l'incluent dans les wheels et les versions installées :

- projet upstream : <https://github.com/bigskysoftware/htmx> ;
- tag upstream : `v2.0.4` ;
- chaîne de version embarquée dans le fichier distribué : `version:"2.0.4"` ;
- artefact upstream :
  <https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js> ;
- taille du fichier distribué : 50917 octets ;
- SHA-256 du fichier distribué :
  `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`.

L'artefact upstream a été téléchargé indépendamment le 2026-08-14 et son SHA-256 correspondait au fichier distribué octet pour octet. htmx `v2.0.4` est sous licence Zero-Clause BSD, qui accorde l'usage, la copie, la modification et la distribution sans aucune condition de mention, d'attribution ou de copyleft. Conserver `LICENSES/htmx-0BSD.txt` complète donc l'inventaire ; cela ne s'acquitte d'aucune obligation.

## Provenance de l'artefact `dwarf2json` audité

Les faits suivants documentent l'artefact Linux officiel upstream audité pour cette revue de limite de release. Le binaire lui-même n'est pas distribué :

- version upstream : `0.9.0` ;
- commit upstream : `9f14607e0d339d463ea725fbd5c08aa7b7d40f75` ;
- date du commit : `2024-10-08T23:53:08Z` ;
- code source upstream : <https://github.com/volatilityfoundation/dwarf2json> ;
- asset officiel de release Linux v0.9.0 :
  <https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64> ;
- métadonnées de build : `go1.18.10`, `linux/amd64`, `vcs.modified=false` ;
- dépendance directe : `github.com/spf13/pflag v1.0.5` ;
- SHA-256 du binaire officiel audité :
  `e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b`.

L'asset officiel de release a été téléchargé indépendamment le 2026-08-04 et son SHA-256 correspondait à la valeur auditée du binaire ci-dessus.

Cette provenance est de la documentation, pas un sidecar embarqué. Ce n'est pas une liste d'autorisation automatique. Un opérateur ne peut configurer un artefact `dwarf2json` différent qu'après avoir examiné sa licence et sa provenance et enregistré son SHA-256 exact dans la configuration AstinusLab et les preuves de release.

# Volatility 3

Si l’état d’une analyse acceptée devient indisponible, son résultat reste inconnu. Rétablissez la connexion ou reconnectez-vous, puis choisissez **Vérifier l’état** pour consulter cette même exécution ; cela ne soumet pas une nouvelle analyse. Recharger le même onglet rétablit le suivi de l’exécution acceptée et de sa source exacte. Le lancement reste désactivé tant que le résultat est inconnu. Choisir une autre source ou un autre plugin change le contexte ; le texte des règles YARA n’est pas enregistré pour le rechargement.

AstinusLab utilise Volatility 3 pour l'analyse mémoire Linux. En V1, Volatility 3 est central dans le flux Capture → Analyse → Preuve pour les labos Linux. La frontière importante est :

- Les ISF sont des artefacts de cache global réutilisables sous `vol3/symbols/linux/`.
- La sortie des plugins est locale au job sous `storage/dumps/<job_id>/vol3/`.

## Analyser une capture Playground précise

1. Avant de fermer la VM, relevez le noyau Linux au moment de la capture (`uname -r`), l’architecture (`uname -m`) et la méthode de vérification, liés à la VM, à la session et à l’acquisition. Conservez l’identité de compilation et les symboles. Un autre démarrage de la même image ne prouve pas le noyau capturé.
2. Terminez une capture **mémoire** dans Playground. Dans **Volatility**, sélectionnez cette source exacte par son identité d’artefact/acquisition ; un nom semblable ne suffit pas. Un disque n’est pas une capture mémoire.
3. Saisissez le noyau capturé, l’architecture `x86_64` et une note de provenance expliquant la vérification. Confirmez avoir vérifié ce contexte par rapport à cette capture, puis enregistrez-le. Ne confirmez pas une version déduite du seul catalogue.
4. Un Job avec `execution_mode=artifact_analysis` est créé, lié à l’artefact original, à sa somme de contrôle, à la session et à l’acquisition. Il enregistre l’analyse dérivée sans démarrer ni recapturer une VM. Un contexte de noyau contradictoire est refusé.
5. Joignez ou téléversez un ISF correspondant. Ses métadonnées de noyau Linux et d’architecture doivent correspondre au contexte ; renommer un fichier incompatible ne suffit pas. Préparez les symboles avant la capture si possible ; un job ISF seul d’un autre démarrage n’est utile que si l’identité exacte du noyau/de compilation correspond.
6. Exécutez un plugin Linux tel que `linux.pslist.PsList` et attendez le résultat final. Téléchargez le rapport, son manifeste et sa somme de contrôle depuis le Job d’analyse ou les endpoints Artifact. Conservez séparément la mémoire source : [export des preuves](../reference/FORENSIC_INTEGRITY.md).

L’API utilise `POST /api/v1/volatility/analysis-contexts` avec `artifact_id`, `kernel_release`, `architecture: "x86_64"`, `verified: true` et une note `provenance` non vide. Utilisez la préparation authentification/CSRF des [Exemples](../usage/examples.md). Les disques, octets absents, propriétés incohérentes et contextes non vérifiés sont refusés.


## Outils externes : installation et récupération

Volatility 3 Framework 2.28.0 et `dwarf2json` sont des outils externes installés
par l'opérateur. Lisez le VSL amont et l'inventaire de `THIRD_PARTY_NOTICES.md`;
AstinusLab ne télécharge jamais les outils et n'accepte pas leurs licences au
nom de l'opérateur.

```bash
cd /opt/astinuslab/current
./astinuslab.sh guide-volatility
```

Cette commande est en lecture seule : elle affiche l'état et les commandes recommandées, sans télécharger, installer, configurer ni redémarrer les outils.

### Installer et vérifier

Utilisez un chemin versionné hors du checkout AstinusLab. Exemple exécuté par
l'opérateur, et non par l'installateur :

```bash
sudo install -d -m 0755 /opt/volatility3-2.28.0
sudo chown "$(id -un):$(id -gn)" /opt/volatility3-2.28.0
python3.12 -m venv /opt/volatility3-2.28.0
/opt/volatility3-2.28.0/bin/python -m pip install 'volatility3==2.28.0'
/opt/volatility3-2.28.0/bin/vol -q frameworkinfo.FrameworkInfo
sudo install -d -m 0755 /opt/dwarf2json/0.9.0
sudo install -m 0755 /path/to/downloaded/dwarf2json-linux-amd64 /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64
echo 'e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b  /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64' | sha256sum --check -
sha256sum /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64
/opt/dwarf2json/0.9.0/dwarf2json-linux-amd64 --help
```

L'exemple affiche `Volatility 3 Framework 2.28.0`. La version minimale acceptée
et la version testée sont **2.28.0**. Les versions ultérieures sont acceptées avec
`untested=true` (`READY (UNTESTED)` dans le guide) ; vérifiez vos cas avant de les utiliser.
Notez
l'origine, le tag ou commit, l'architecture et le SHA-256 local de `dwarf2json`.
Artefact audité : `https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64` ;
commit `9f14607e0d339d463ea725fbd5c08aa7b7d40f75`.

### Configurer, récupérer et mettre à niveau

Modifiez la section `[vol3]` existante dans `astinuslab.toml` : remplacez ou
décommentez les clés du guide et conservez les autres. Créez la section seulement
si elle manque ; n'ajoutez jamais une deuxième table `[vol3]`. Validez la syntaxe
avant de redémarrer, avec le chemin réel de vos données. Le guide affiche encore
les instructions si le TOML est mal formé.

```bash
/opt/astinuslab/current/.venv/bin/python -c 'import pathlib,tomllib; tomllib.loads(pathlib.Path("/opt/astinuslab/astinuslab.toml").read_text()); print("TOML valid")'
```

Configurez les chemins explicites et `dwarf2json_sha256`, redémarrez les services
et consultez `/api/v1/volatility/readiness` ou la page Volatility. Les codes
`*_not_configured`, `*_not_found`, `*_not_executable`,
`volatility_version_unsupported`, `volatility_probe_failed`,
`dwarf2json_hash_not_configured` et `dwarf2json_hash_mismatch` indiquent l'action
corrective. Un `guest transfer mismatch` impose de vérifier le même hash sur l'hôte
et le guest; Upload ISF reste l'alternative manuelle.
La préparation de l'analyse et celle de la construction ISF sont indépendantes : l'une peut être `READY` tandis que l'autre reste indisponible.

Pour une mise à niveau, cessez de mettre de nouvelles analyses en file, installez
un nouveau chemin versionné, vérifiez licence/provenance/hash, lancez la sonde et
les tests de régression, mettez à jour la configuration atomiquement, redémarrez
et vérifiez readiness. Gardez l'ancien chemin pour le rollback et ne remplacez
jamais un binaire en place. Pour revenir en arrière, restaurez l'ancien chemin et
la paire de hash, redémarrez, puis vérifiez readiness.

## Volatility 101 pour AstinusLab

Si vous migrez depuis des installations Volatility standard, les jobs stricts ISF-only peuvent sembler différents. Dans AstinusLab, l'analyse Linux utilise un Intermediate Symbol Format (ISF) adapté au kernel exact de la VM, pas des profils devinés. Flux typique : générer/cacher l'ISF, exécuter le lab, capturer la mémoire, puis analyser avec Volatility 3 en utilisant cet ISF.

## Symboles Windows (PDB)

Windows ne construit pas un ISF par kernel depuis DWARF comme Linux. Volatility 3 résout les symboles depuis les PDB : par défaut via Microsoft Symbol Server, ou via un cache offline sous `vol3/symbols/windows/` pour les labs isolés. Les étapes Linux de build ISF/live-guest ne s'appliquent pas aux targets Windows. Cette prise en charge de Windows est expérimentale : préparer des targets Windows n'est pas un flux automatisé en 1.0.0, elle ne dispose d'aucune preuve d'acceptation de release et reste hors du périmètre pris en charge.

## Génération d'ISF

AstinusLab construit un ISF pour le noyau exact en cours d'exécution de la VM fraîchement démarrée. C'est une architecture intentionnelle : la VM est la source de vérité pour la version du noyau, les métadonnées de distro et la disponibilité des paquets.

Le runner de production peut construire un ISF en :

- Réutilisant un ISF valide existant du cache local lorsque la version du noyau et le build ID connu correspondent.
- Récupérant les paquets de débogage de la distro depuis les dépôts de paquets côté hôte. Pour certaines distros, le script de host fetch peut utiliser debuginfod en interne lorsque le build ID du guest est connu.
- En se connectant au guest en direct et en utilisant un `vmlinux` correspondant avec DWARF, debuginfod ou une installation de paquets de débogage explicitement autorisée lorsque le host fetch ne peut pas produire un ISF valide. `dwarf2json` n'accepte pas une entrée contenant uniquement du BTF et ce fallback ne l'utilise jamais.

Chaque job enregistre s'il a produit un nouvel ISF ou réutilisé un ISF en cache.

## Jobs stricts ISF uniquement

Un job explicite ISF uniquement doit produire ou réutiliser un ISF valide. Si aucun ISF valide n'est disponible, le job échoue. Cela empêche l'analyse ultérieure d'utiliser des symboles manquants, périmés ou non correspondants.

Les jobs ISF uniquement utilisent la chaîne de fournisseurs côté hôte : `local_cache -> prebuilt_isf -> debuginfod -> host_fetch`. Si cette chaîne échoue, AstinusLab essaie le fallback du guest en direct, qui peut utiliser un `vmlinux` correspondant avec DWARF, debuginfod ou une installation de paquets de débogage explicitement autorisée. Une entrée contenant uniquement du BTF n'est pas envoyée à `dwarf2json`. Les journaux de host fetch sont conservés dans `storage/dumps/<job_id>/vol3/symbol-build-host.log` ; les journaux du fallback en direct sont conservés dans `storage/dumps/<job_id>/vol3/symbol-build-live.log` ; les tentatives des fournisseurs et les raisons de skip/échec sont conservées dans `storage/dumps/<job_id>/vol3/symbol-diagnostics.json`.

L'identité du cache tient compte du build ID. Un ISF en cache avec un build ID correspondant est réutilisé ; un ISF en cache avec un build ID différent est traité comme un miss et reconstruit. Les ISF hérités en cache sans build ID enregistré restent des soft hits afin que les anciens caches continuent de fonctionner, et AstinusLab enregistre le build ID connu du job sur la nouvelle ligne d'artefact.

Les versions d'AstinusLab antérieures à la Phase C écrivaient `vol3/symbols/linux/<kernel_release>.json`. Ces fichiers restent lisibles comme entrées de cache soft-hit héritées lorsqu'aucun build ID en conflit n'est enregistré. Les nouveaux symboles qualifiés par build ID utilisent `vol3/symbols/linux/<kernel_release>_<buildid-prefix>.json`.

## Override manuel (upload ISF / dump)

Quand le cache build-ID-aware et la chaîne live-guest ne peuvent pas produire des symboles que vous possédez déjà, AstinusLab permet **Upload ISF** depuis le workbench `/volatility` et **Upload memory dump** depuis le détail du job. Ces deux overrides contournent des garde-fous et enregistrent une provenance **unverified** ; utilisez-les seulement si vous savez que les symboles et le dump correspondent au kernel du job. Endpoints : `POST /api/v1/volatility/targets/{target_id}/isf/upload`, `POST /api/v1/jobs/{job_id}/isf/upload` et `POST /api/v1/jobs/{job_id}/dump/upload`.

### Exécuter ISF uniquement depuis l'UI

1. Ouvrez **Volatility** dans la barre latérale (`/volatility`).
2. Sélectionnez une cible Linux.
3. Sélectionnez un chemin de build `immediate`.
4. Sélectionnez **Queue ISF build**.
5. Surveillez la carte Volatility 3 ISF du détail du job lié ou le tableau d'historique des builds Volatility.

Les Jobs exécutent toujours le travail en interne ; la page Volatility est le flux dédié à la préparation des symboles, tandis que **Jobs** reste le registre d'exécution.

### Exécuter ISF uniquement depuis l'API

Créez un job avec `build_vol3_isf: true`. AstinusLab le traite comme un job ISF uniquement pour les modes `immediate`.

```json
{
  "target_id": "ubuntu-22-04-default",
  "scenario_id": "immediate-capture",
  "execution_mode": "immediate",
  "build_vol3_isf": true
}
```

## Sortie de l'analyse

Lorsque l'analyse des plugins s'exécute, les résultats sont écrits sous `storage/dumps/<job_id>/vol3/`. Ces fichiers appartiennent à ce job et peuvent être supprimés avec les artefacts du job. Ils ne constituent pas le cache de symboles global.

`analysis-summary.json` enregistre `dump_path`, `dump_sha256`, `isf_path`, `isf_sha256`, `symbols_path` et `vol3_version`. Pendant chaque exécution, AstinusLab installe l'ISF sélectionné dans un répertoire de symboles local au job et pointe Volatility 3 vers ce répertoire afin que l'ISF enregistré soit le seul ISF disponible pour l'exécution des plugins.

Une analyse réussie publie aussi `analysis-summary.json` comme artefact de rapport de première classe :

- `analysis-summary.json.manifest.json` utilise `artifact-manifest/v1` avec `producer: vol3_analysis` et `kind: report`.
- `analysis-summary.json.sha256` contient le sidecar SHA-256 du fichier de résumé.
- La ligne d'Artefact enregistre le fichier de rapport, le chemin du manifeste, le chemin du sidecar SHA-256, la taille et la somme de contrôle.
- La ligne de Rapport relie l'artefact mémoire d'entrée à l'artefact de rapport de sortie.

Les plugins courants incluent `pslist`, `pstree`, `lsmod`, `mountinfo`, `sockstat`, `ip.Addr` et `ip.Link`.

### Preuve des exécutions de plugin du workbench

Les exécutions à la demande d'un seul plugin depuis le workbench **Volatility** sont aussi des preuves de première classe. Chaque exécution terminée écrit `storage/dumps/<job_id>/vol3/runs/<run_id>.json`, publie manifeste et sidecar SHA-256, et crée des lignes Artifact/Report liant l'artefact mémoire d'entrée au rapport de sortie.

### Exécuter l'analyse post-capture

1. Terminez un job avec un dump mémoire.
2. Confirmez que le job a un ISF valide enregistré dans la carte Volatility 3 ISF.
3. Sélectionnez **Run Volatility 3** sur la page de détail du job.
4. Examinez `analysis-summary.json` et la sortie par plugin sous `storage/dumps/<job_id>/vol3/`.

L'analyse nécessite aussi que `settings.vol3_volatility_bin` et `settings.vol3_default_plugins` soient configurés.

## Dépannage

### Symboles de débogage manquants

Symptômes :

- Le job ISF uniquement échoue.
- Les journaux mentionnent `vmlinux`, `debuginfo`, `dbgsym` ou des paquets de dépôt de débogage manquants.

Correctifs :

- Confirmez que la VM exécute une distribution Linux x86_64 prise en charge depuis `docs/reference/DISTRO_SUPPORT.md`. Les architectures ARM64 (`aarch64`) ne sont explicitement pas prises en charge pour la génération d'ISF ni l'analyse mémoire.
- Confirmez que la version exacte du noyau dispose de paquets de débogage correspondants.
- Activez le dépôt de symboles de débogage de la distro.
- Inspectez `storage/dumps/<job_id>/vol3/symbol-build-host.log` pour l'échec exact du host fetch.
- Utilisez le fallback du guest en direct lorsque la récupération de paquets côté hôte ne peut pas résoudre le paquet.

### ISF invalide

Symptômes :

- Le fichier JSON est écrit mais Volatility le rejette.
- Les exécutions de plugins échouent avant de produire une sortie utile.

Correctifs :

- Supprimez le fichier défectueux de `vol3/symbols/linux/` et relancez la génération.
- Vérifiez que `dwarf2json` peut analyser le `vmlinux` sélectionné.
- Confirmez que le nom du fichier ISF et les métadonnées du job correspondent au noyau détecté.

### Échecs du dépôt de paquets

Symptômes :

- Les commandes de paquets échouent pendant le host fetch ou la construction sur le guest en direct.
- Les journaux montrent des erreurs DNS, de mirror, GPG, d'abonnement ou de métadonnées de release.

Correctifs :

- Vérifiez le réseau et le DNS depuis l'hôte et le guest.
- Vérifiez la disponibilité du mirror du dépôt.
- Accordez assez de temps pour les gros paquets de débogage ; les paquets de symboles Ubuntu/RHEL peuvent dépasser 1 Go.
- Ajustez `[vol3].fetch_timeout_seconds` lorsque les mirrors sont lents.
- Pour RHEL, confirmez l'entitlement du dépôt ou une source debuginfo équivalente.
- Réessayez avec une release d'image cloud prise en charge si la release de la distro est en fin de vie.

### Échecs de plugins

Symptômes :

- La génération d'ISF réussit mais la sortie des plugins est manquante ou incomplète.
- Les fichiers sous `storage/dumps/<job_id>/vol3/` contiennent des erreurs.

Correctifs :

- Confirmez que le dump mémoire existe pour le job.
- Confirmez que l'ISF correspondant existe sous `vol3/symbols/linux/`.
- Inspectez le journal d'événements du job et la sortie Volatility par job.
- Relancez l'analyse après régénération de l'ISF si les symboles étaient périmés, invalides ou construits pour un build de noyau différent (même `uname -r`, build ID différent — par ex. après une recompilation de sécurité du fournisseur).

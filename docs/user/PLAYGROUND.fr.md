# Mode Playground

Utilisez Playground pour modifier le laboratoire de manière interactive et capturer des preuves à la demande ; utilisez [Jobs](../usage/examples.md) pour les exécutions reproductibles de Scenario. Si aucune image n’est disponible pour un rôle, suivez **Gérer les images** ou le **Guide des images**. Si le chargement du catalogue échoue, **Réessayer le chargement des images** conserve le formulaire sans créer de laboratoire. **Formulaire complet — préparation de l’hôte non vérifiée** ne confirme que le formulaire ; la présence d’une image ne prouve pas que les fichiers Workload ou les prérequis de l’hôte sont prêts.

Voir aussi : [Référence de l'API REST Playground](../api/playground.md).

Le Playground est le flux d'entrée principal d'AstinusLab pour les labos d'attaque et d'investigation forensique. Il vous permet de lancer des cibles vulnérables, des VMs attaquantes et des Network Nodes, de les connecter via des réseaux multi-segments, d'exécuter des expérimentations de sécurité, de capturer des preuves à la demande et de tout démanteler une fois terminé — le tout dans une seule session.

Les exemples d'API ci-dessous utilisent l'assistant réutilisable `api_curl` en Bash documenté dans [Exemples](../usage/examples.md), qui gère l'authentification Bearer en mode release et l'amorçage du jeton CSRF sur les requêtes de mutation en mode développement.


Pour un premier essai, utilisez **+ Target → No workload** et capturez une VM nue. La session est créée automatiquement. Pour une charge vulnérable sur le réseau isolé par défaut, préparez d’abord un cache image/charge correspondant dans Jobs ou utilisez une image préparée hors ligne. L’installation en ligne nécessite un réseau avec accès sortant choisi explicitement ; l’assistant n’accorde pas Internet et ne vérifie pas le cache. Voir [Préparer les charges](PREBAKED_IMAGES.md) avant le lancement. Review vérifie le formulaire, pas la disponibilité de l’hôte.

## Qu'est-ce que le Playground ?

Voyez le Playground comme un labo éphémère où vous pouvez :

- **Lancer des cibles, des attaquants et des Network Nodes** sans créer d'abord un Job. Choisissez des images du catalogue, étiquetez-les et démarrez-les.
- **Construire des réseaux** — isolés ou routés, avec des CIDR personnalisés. Utilisez plusieurs réseaux pour modéliser des topologies de pivot.
- **Capturer des preuves** — dumps mémoire et disque sur n'importe quelle VM en cours d'exécution, à tout moment, avec des sidecars de manifeste et SHA-256.
- **Travailler de manière interactive** — inspectez les VMs, exécutez des attaques ou des vérifications défensives, déclenchez des captures au fur et à mesure.
- Le nettoyage est asynchrone ; fermer la session libère VMs et réseaux et conserve les preuves terminées dans Storage. Attendez l’état final et résolvez tout échec.

### Contraintes clés

- **Capacité** : plusieurs Jobs peuvent conserver des targets actifs et leurs attaquants optionnels dans les limites cumulées de RAM, disque et nombre de VM. Le worker de cycle de vie unique sérialise les opérations ; il ne limite pas le système à un seul Job vivant. Un Playground ordinaire est exclusif avec les Jobs et autorise une seule session active. Les instances Packages expérimentales ont leur propre cycle de vie et restent hors du périmètre pris en charge dans 1.0.0. Voir [Configuration](../usage/configuration.md).
- L’effet du redémarrage dépend du service et du propriétaire de la session ; voir la section redémarrages et nettoyage.
- **Import/export de recettes et paquets** — la création guidée de labo dans l'UI est la porte d'entrée de V1. Les recettes Playground actuelles sont en JSON (`schema_version: 1`) et peuvent être exportées depuis la session active ou importées dans une session. Les paquets de scénario enveloppent cette recette YAML, des métadonnées et des scripts optionnels dans un zip pour faciliter le partage. Les workflows YAML écrits à la main restent la voie de répétabilité à long terme.

---

## Démarrer une session Playground

1. Ouvrez **Playground** dans la barre latérale.
2. Utilisez **+ Target**, **+ Attacker**, **+ Network Node** ou importez une recette.
3. La première action crée ou réutilise automatiquement la session ; l’UI n’a pas de bouton Start Session.

Le canevas affiche la topologie. Sélectionner une VM ouvre ses détails : état, IPs, NICs, SSH, services et contrôles de capture. Le pied affiche la session et **End Session** lorsqu’elle existe. Les artefacts restent dans **Storage**. L’API permet de créer explicitement une session vide avec `POST /api/v1/playground/sessions`.

---

## Créer des réseaux

Les réseaux définissent la topologie de connectivité de vos VMs. Chaque réseau a un nom, une plage CIDR et un flag d'isolation.

### Réseaux isolés vs. routés

- **Isolé** (`isolated=true`) — ce réseau libvirt ne transfère pas de trafic vers les réseaux externes. Les invités peuvent communiquer entre eux et avec l'hôte via son adresse de pont, selon les règles des pare-feu. Cela n'isole pas les services de l'hôte : limitez séparément ses ports d'écoute.
- **Routé** (`isolated=false`) — les VMs de ce réseau peuvent atteindre l'hôte et d'autres réseaux routés via NAT/routage. Utilisez-le pour la connectivité sortante ou pour relier des segments.

### Créer un réseau

1. Ouvrez **+ Target**, **+ Attacker** ou **+ Network Node** dans l'en-tête. À l'étape des réseaux de l'assistant, cliquez sur **+ Nouveau** (**+ New** en anglais) pour ouvrir **Ajouter un réseau à la VM**.
2. Saisissez un nom (par ex. `internal`, `dmz`, `guest`) ou acceptez celui proposé.
3. Saisissez un **CIDR** ou acceptez la prochaine plage libre `10.42.x.0/24` proposée. Les CIDR d'une session ne doivent pas se chevaucher ; une demande avec chevauchement est remplacée par une plage libre et un avis s'affiche.
4. Gardez l'option d'isolation cochée pour désactiver le transfert externe ; décochez-la pour une connectivité routée (NAT).
5. Confirmez le réseau et terminez l'assistant pour créer la VM. Le réseau passe de `pending` à `active` pendant la création du laboratoire ; l'ajouter au formulaire ne démarre pas encore de ressources.

---

## Lancer des VMs

Une VM est démarrée depuis une image du catalogue et attachée à un ou plusieurs réseaux via des NIC (interfaces réseau). Cibles, attaquants et Network Nodes utilisent tous ce flux de VM.

### Network Nodes

Les Network Nodes sont des VMs légères à plusieurs NIC pour une image fournissant déjà des fonctions de routeur, pare-feu, proxy, VPN ou IDS. AstinusLab connecte les NIC et applique les ressources par défaut (1 vCPU, 512 Mo de RAM, 4 Go de disque) ; l'opérateur configure le transfert, les règles et les services réseau. Connectez deux NIC ou plus pour placer le nœud entre des segments.

Les captures sont désactivées par défaut pour les Network Nodes ; capturez les VMs cibles lorsque vous avez besoin de preuves forensiques.

### Lancer une VM

1. Dans l'en-tête, cliquez sur **+ Target**, **+ Attacker** ou **+ Network Node**.
2. Saisissez un libellé unique dans la session et choisissez une image de système d'exploitation du catalogue. Seules les images adaptées à Playground sont affichées.
3. Pour un Target, choisissez éventuellement un Workload compatible. Vérifiez ses exigences de préparation avant de continuer.
4. Sélectionnez les réseaux : chacun ajoute une NIC. Pour un pivot ou un Network Node entre segments, sélectionnez-en au moins deux. Sans sélection, un réseau isolé `default` (`10.42.1.0/24`) est créé et utilisé. Les Targets permettent de régler vCPU, mémoire et disque ; les autres rôles appliquent leurs valeurs par défaut.
5. Vérifiez le formulaire et confirmez la création. La première action de laboratoire crée ou réutilise automatiquement la session ; ouvrir ou remplir le formulaire ne vérifie pas la préparation de l'hôte.

AstinusLab démarre la VM et actualise ses IP dès qu'elles sont disponibles. Sélectionnez la machine pour ouvrir son panneau de détails, qui affiche à l'état `running` :

- **Label** et **Image**
- **State** (`pending`, `booting`, `running`, `destroying`, `failed`)
- **IP par NIC** — l'IP du guest attribuée à chaque interface réseau
- **Boutons d'action** — redémarrer, arrêter, démarrer, détruire la VM, **Capturer la RAM** et **Capturer le disque**

---

## Accéder aux VMs via SSH

En attendant qu'un accès console VNC/SPICE soit disponible dans une prochaine version, vous devez interagir avec les VMs via SSH. AstinusLab injecte automatiquement une clé SSH dans les VMs qu'il lance.

Pour accéder en SSH à une VM sur un réseau **routé ou isolé** depuis votre machine hôte, utilisez la clé gérée de l'espace de travail et l'IP de l'invité (guest IP) indiquée sur la carte de la VM :

Utilisez la commande SSH affichée pour cette VM, avec son adresse et son utilisateur. `<storage_dir>/ssh_keys/.active` contient le nom sélectionné ; la clé privée est `<storage_dir>/ssh_keys/<nom-actif>`, sans suffixe. Le nom initial du lanceur est `astinuslab-default`, mais **Security** permet de le changer. Résolvez `storage_dir` depuis la configuration : dans une installation, il dépend généralement de la racine des données, pas de `current`. Une VM existante peut garder la clé injectée à sa création après un changement de sélection ; conservez cette clé. Avec des identités de service séparées, utilisez un accès autorisé sans publier ni copier les clés privées dans un emplacement partagé.
*(Consultez la page du catalogue Images si vous n'êtes pas sûr du `<username>` par défaut pour une image spécifique.)*

**Réseaux isolés :** l'hôte possède une adresse sur le pont libvirt. Utilisez l'IP
et la clé indiquées pour un accès SSH direct si SSH et les pare-feu le permettent.
Une VM pivot est facultative pour accéder depuis un autre segment. Les invités
peuvent aussi atteindre les services de l'hôte ; désactiver le transfert ne remplace
pas un pare-feu. Consultez la [sémantique libvirt](https://libvirt.org/formatnetwork.html#isolated-network-config).

---

## Visualiser la topologie

Le Playground affiche toujours la **carte du laboratoire**, avec les VMs, les réseaux et leurs connexions. Déplacez les nœuds pour l'organiser ; la disposition est enregistrée automatiquement. Sélectionnez une machine pour ouvrir son panneau de détails. Utilisez la carte pour vérifier les connexions entre segments avant de capturer.

---

## Importer et exporter des recettes et paquets

Playground prend en charge deux formats de partage :

- **Recette JSON** — `PlaygroundRecipe` brute depuis la session active.
- **Zip de paquet de scénario** — archive partageable avec `metadata.yaml`, `scenario.yaml` et entrées optionnelles de scripts relatives et sûres.

### Exporter une recette

1. Démarrez ou ouvrez une session Playground.
2. Construisez des réseaux, lancez des VMs cible, attaquant et Network Node, et organisez la topologie.
3. Cliquez sur **Export Recipe**.
4. AstinusLab télécharge `<session-id>.recipe.json`.

Les recettes exportées incluent :

- `schema_version: 1`
- `name`
- `networks` avec noms, CIDR et flags d'isolation
- `vms` avec labels, rôles, IDs d'image, IDs de workload optionnels, `vcpus`, `memory_mb`, `disk_gb` optionnels et noms de réseau
- les positions de nœuds de `topology`

### Importer une recette

1. Cliquez sur **Import Recipe**.
2. Collez le JSON de la recette.
3. Soumettez l'import.

AstinusLab valide l'unicité des noms de réseau et des labels de VM, rejette les références à des réseaux inconnus, crée les réseaux manquants, lance les VMs et restaure les positions de topologie. L'import dans une session existante échoue avec `409 Conflict` si les noms de la recette entrent en collision avec des réseaux ou VMs existants.

### Exporter un paquet de scénario

Utilisez l'API REST pour télécharger le Playground actif sous la forme d'un paquet de partage en un clic :

```bash
api_curl -OJ "$BASE_URL/api/v1/scenario-packages/playground/current"
```

Contenu du paquet :

- `metadata.yaml` — type de paquet, version de schéma et métadonnées d'affichage.
- `scenario.yaml` — données de recette de Playground.
- Chemins de script optionnels — entrées relatives sûres incluses par les assistants de paquet du backend lorsqu'elles sont référencées par de futurs flux catalogue/workload.

### Importer un paquet de scénario avec substitution de ressources

Téléversez le zip vers une autre instance AstinusLab :

```bash
api_curl -X POST "$BASE_URL/api/v1/scenario-packages/import" \
  -F "file=@dvwa-lab.zip"
```

Pour redimensionner les machines sans modifier l'archive, passez `resource_overrides` en JSON indexé par libellé de VM :

```bash
api_curl -X POST "$BASE_URL/api/v1/scenario-packages/import" \
  -F "file=@dvwa-lab.zip" \
  -F 'resource_overrides={"target-1":{"vcpus":4,"memory_mb":8192,"disk_gb":80}}'
```

Les champs autorisés sont `vcpus`, `memory_mb` et `disk_gb`. L'import valide les surcharges via le même schéma de recette Playground avant de créer les réseaux ou lancer les VMs.

---

## Capturer des artefacts forensiques

Vous pouvez déclencher des captures mémoire et disque sur les VMs cible et attaquant en cours d'exécution. Les captures sont désactivées par défaut pour les Network Nodes ; capturez les VMs cibles lorsque vous avez besoin de preuves forensiques.

### Capture mémoire

1. Sélectionnez la VM sur la carte pour ouvrir son panneau de détails.
2. Cliquez sur **Capturer la RAM** (**Capture RAM** en anglais).
3. AstinusLab acquiert la RAM du guest via `virDomainCoreDump` (format ELF, sans modification du guest).
4. La progression apparaît dans la console de capture. Une fois terminée, la preuve apparaît dans le panneau de détails et dans **Storage**, avec des liens vers l'artefact, le manifeste et le SHA-256.

### Capture disque

1. Sélectionnez la VM sur la carte pour ouvrir son panneau de détails.
2. Cliquez sur **Capturer le disque** (**Capture Disk** en anglais).
3. AstinusLab met le domaine en pause brièvement, convertit le disque en artefact qcow2 non compressé et reprend la VM.
4. L'artefact apparaît dans **Storage**.

### Accéder aux artefacts capturés

Toutes les captures de la session apparaissent dans **Storage** avec des liens de téléchargement et des métadonnées (type, taille, horodatage). Cliquez sur le lien pour télécharger directement.

### Cycle de vie de l'artefact

Les artefacts capturés sont des preuves forensiques et sont traités séparément de la session éphémère qui les a produits :

- **Les captures persistent à travers les redémarrages du worker.** Lorsque le worker Celery redémarre, le balayage de récupération au démarrage ne purge que les overlays de disque de VM périmés (sous `playground_disk_dir`) pour les sessions qui ne sont plus actives. Le répertoire de captures (`playground_capture_dir`) n'est jamais touché par la récupération, donc les dumps mémoire, images disque, manifestes et sidecars SHA-256 terminés restent intacts.
- **Les captures persistent à travers la destruction de la session.** Terminer la session playground détruit ses VMs et réseaux, mais les artefacts déjà produits par `Capture Memory` / `Capture Disk` restent sur le disque et leurs lignes `Acquisition` restent dans la base de données. Ils restent disponibles via **Storage** et l'explorateur de stockage jusqu'à ce que vous les supprimiez explicitement.
- **Chaîne de traçabilité.** Chaque capture est écrite avec un manifeste et un fichier SHA-256 à côté ; ceux-ci sont écrits une fois au moment de la capture et ne sont jamais modifiés par les chemins de récupération, de nettoyage ou de fin de session.

---

## Contrôle d'alimentation de la VM

Le panneau de détails de la VM comporte les boutons d'action. Attendez la fin d'une transition d'état avant de répéter une opération :

- **Reboot** — redémarrage propre via ACPI. La VM reste démarrée et connectée à ses réseaux.
- **Shutdown** — arrêt propre. La VM s'arrête mais l'enregistrement de la VM demeure. Cliquez sur **Start** pour la relancer.
- **Start** — démarre une VM qui était arrêtée (disponible uniquement lorsque la VM n'est pas en cours d'exécution).
- **Destroy** — arrêt propre → destruction forcée. La VM est supprimée, son disque est effacé et l'enregistrement de la VM est marqué comme détruit.

---

## Arrêter la session

Une fois terminé :

1. Cliquez sur **Terminer la session** (**End Session** en anglais) dans le pied de page de la session et confirmez la demande.
2. **Nettoyage demandé** (`destroy_requested`) confirme l'acceptation ; **Nettoyage en cours…** (`destroying`) indique que l'opération continue. Attendez **Session terminée. Les preuves capturées sont conservées.** (`cancelled` après Terminer la session, ou `completed`).
3. Si **Nettoyage incomplet** (`destroy_failed` ou `failed`) s'affiche ou si le statut est indisponible, consultez Logs avec l'identifiant de session. Des ressources de l'hôte peuvent rester réservées ; ne considérez pas le nettoyage comme terminé. Les preuves terminées restent dans Storage.

Tous les artefacts capturés restent disponibles dans **Storage** jusqu'à ce que vous les supprimiez manuellement depuis le Stockage.

---

## Contraintes et règles de validation

- **Session active unique** — vous ne pouvez pas démarrer un nouveau playground si un est déjà en cours. Vous devez d'abord détruire la session actuelle.
- **Unicité des labels** — les labels de VM doivent être uniques au sein d'une session.
- **Chevauchement de CIDR** — les CIDR de réseau au sein d'une session ne doivent pas se chevaucher.
- **Réseau utilisé** — vous ne pouvez pas supprimer un réseau tant que des VMs y sont encore attachées. Détruisez d'abord les VMs, ou passez un flag de cascade (API uniquement).
- **État de capture** — vous ne pouvez déclencher des captures que sur les VMs cible et attaquant en état `running`. Les captures sont désactivées par défaut pour les Network Nodes.
- **Pas d'exécution de jobs chevauchante** — si un Job forensique est actif, vous ne pouvez pas démarrer un playground, et vice versa.

---

## Modèle de sécurité

V1 est localhost/local-de-confiance par défaut. Si vous liez l'API/UI hors loopback, activez l'authentification d'API à administrateur unique et utilisez quand même TLS, VPN, firewall ou reverse proxy pour les réseaux partagés.

Les VMs guest sont intentionnellement hostiles ou vulnérables. Le durcissement guest hostile devrait être sélectionné lors de la création du labo lorsqu'il est disponible ; tant que ces contrôles ne sont pas complets, préférez des réseaux isolés pour les labos d'exploit et traitez les identifiants du guest, les URLs de service et les artefacts capturés comme sensibles.

---

## Limitations connues (reportées)

- **Accès console** — le proxy de console VNC/SPICE est prévu pour une version future. D'ici là, utilisez l'IP du guest pour vous connecter en SSH directement depuis votre machine (voir [Accéder aux VMs via SSH](#acceder-aux-vms-via-ssh)).
- **Snapshots et restauration** — le playground ne prend pas en charge les checkpoints de sauvegarde-d'état / restauration.
- **Contrôles de ressources** — l'API accepte `vcpus`, `memory_mb` et `disk_gb` par VM. L'assistant Target permet de régler vCPU, mémoire et disque ; les créateurs Attacker et Network Node appliquent les valeurs par défaut de leur rôle.
- **Images téléversées par l'utilisateur** — vous ne pouvez pas téléverser de fichiers qcow2 bruts hors catalogue pour une utilisation playground.
- **Cibles Windows** — roadmap ; V1 se concentre sur les labos vulnérables d'applications web Linux.
- **Télémétrie en direct et expériences de rapports plus riches**, au-delà des résumés Volatility et des rapports de plugins déjà publiés — roadmap ; V1 se centre sur les captures, les artefacts et la sortie de Volatility 3.

---

## Exemples de workflows

### Dump mémoire d'une seule VM

1. Démarrez une session.
2. Lancez une VM depuis l'image de votre choix.
3. Attendez qu'elle atteigne l'état `running`.
4. Cliquez sur **Capturer la RAM**.
5. Téléchargez le dump depuis **Storage**.
6. Détruisez la session.

### Topologie de pivot multi-segments

1. Démarrez une session.
2. Créez trois réseaux : `dmz` (10.0.0.0/24, routé), `internal` (10.1.0.0/24, isolé), `sensitive` (10.2.0.0/24, isolé).
3. Lancez une VM `attacker` sur `dmz` uniquement.
4. Lancez une VM `pivot` sur `dmz` et `internal`.
5. Lancez une VM `target` sur `internal` et `sensitive`.
6. L'attaquant peut atteindre le pivot via `dmz` ; le pivot voit les deux segments ; la cible est derrière le pivot.
7. Déclenchez des captures sur n'importe quelle VM pour obtenir des snapshots forensiques à tout moment.
8. Détruisez la session une fois terminé.

### Exploiter et capturer

1. Démarrez une session.
2. Créez un réseau routé et lancez une VM vulnérable.
3. Depuis votre machine hôte, connectez-vous en SSH à l'IP du guest et exécutez un exploit.
4. Une fois l'état de l'exploit capturé en mémoire, cliquez sur **Capturer la RAM** depuis l'UI.
5. Analysez le dump avec Volatility 3 ou les outils de votre choix ; Volatility 3 est la voie centrale d'analyse mémoire de V1.
6. Détruisez la session.

---

## Dépannage

### La VM ne démarre pas ou expire

- Vérifiez que l'image est valide et a un nom d'utilisateur par défaut configuré (visible sur la page Images).
- Assurez-vous que la connectivité SSH n'est pas bloquée par des règles de pare-feu.
- Consultez les journaux du runner pour les erreurs de sondage d'IP du guest.

### La création de réseau échoue avec une erreur de chevauchement de CIDR

- Examinez les CIDR des réseaux existants. AstinusLab n'autorise pas les plages qui se chevauchent au sein d'une session.
- Choisissez un CIDR disjoint et réessayez.

### La capture se bloque ou échoue

- Assurez-vous que la VM est en état `running` (vérifiez la carte de la VM).
- Les captures mémoire peuvent prendre plusieurs minutes sur de grandes images. Consultez les journaux du runner pour la progression.
- Les captures disque mettent la VM en pause brièvement (~1 s pour les petits disques, plus pour les grands). Laissez le temps à la pause et à l'opération de conversion de se terminer.

### Impossible de supprimer un réseau

- Vérifiez qu'aucune VM n'est encore attachée au réseau. Détruisez ou reconnectez d'abord toutes les VMs dépendantes.
- Consultez le message d'erreur dans l'UI pour la liste des VMs bloquant la suppression.

### Redémarrages et nettoyage

Redémarrer seulement l’API ou le worker d’analyse ne lance pas le nettoyage de démarrage de Playground. Redémarrer le runner de cycle de vie déclenche la récupération et termine les sessions Playground ordinaires. Les sessions figées du créateur et les sessions de paquets actifs sont protégées par leur propre cycle de vérification/instance. Un redémarrage de l’hôte relance aussi ces services et interrompt les guests ; protéger un enregistrement ne garantit pas une VM ininterrompue.

**End Session** met le nettoyage en file. La page suit cette session après rechargement et confirme sa fin uniquement pour `cancelled` (le résultat d’End Session) ou `completed`. `destroy_failed` ou `failed` exige une intervention ; les ressources peuvent rester réservées. Consultez **Logs** avec l’identifiant conservé. Un enregistrement absent ou un état indisponible ne confirme pas le nettoyage. Les captures terminées et leurs sidecars restent dans **Storage**.


---

## Étapes suivantes

- **Analyser avec Volatility 3** — une fois que vous avez un dump mémoire, utilisez [Volatility](VOLATILITY3.md) pour analyser la mémoire du guest avec vos plugins préférés.
- **Apprendre le flux de Jobs** — pour des acquisitions répétables pilotées par scénario, voir [Prise en main](../usage/getting-started.md).
- **Configurer des réseaux personnalisés** — le playground utilise des réseaux libvirt standard. Les utilisateurs avancés peuvent inspecter le XML réseau avec `virsh net-dumpxml` ou modifier les définitions du catalogue pour des topologies complexes.

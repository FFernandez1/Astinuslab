# Prise en main

AstinusLab provisionne des VMs Linux sur KVM/libvirt, exécute la configuration du scénario ou du workload, capture des artefacts de mémoire et de disque depuis l'hôte, enregistre les sommes de contrôle et les métadonnées, puis démantèle les ressources du labo.


L’hôte testé est Ubuntu 24.04 LTS x86_64 avec KVM. Les autres recettes sont expérimentales. L’espace de téléchargement est distinct de la capacité opérationnelle : le smoke strict avertit sous 150 Go libres ou 16 Go de RAM ; chaque laboratoire nécessite son propre budget.

## Choisir un mode d'exécution

Utilisez un seul mode à la fois :

| Mode | À utiliser quand | Point d'entrée |
| --- | --- | --- |
| Développement | Vous éditez du code, testez en local ou itérez sur la doc | `./astinuslab.sh` |
| Release | Vous exécutez AstinusLab comme un service local durable | `ops/deploy/install.sh` plus les unités systemd |

N'exécutez pas le runner de développement et le runner systemd en même temps. Ils partagent Celery, libvirt et les ressources de nommage des VMs.

Les installations release activent l'authentification admin par défaut. Une installation propre affiche le token lors de sa création puis une dernière fois dans le bloc final ; enregistrez-le immédiatement. Les mises à jour conservent le hash existant. Pour une installation sans authentification, explicitement réservée au développement, passez `--allow-unauthenticated-local` à `ops/deploy/install.sh`. Voir [Authentification](../api/authentication.md) pour les règles de hash et d'options.

## Première installation release

```bash
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER"
cd /opt/astinuslab/current
sudo -u "$USER" ./ops/images/download_images.sh
./astinuslab.sh smoke --require-running
./astinuslab.sh guide-volatility  # facultatif ; affiche seulement un guide en lecture seule
```

Le preflight est en lecture seule et renvoie un code non nul si une exigence manque. Enregistrez le token de `CREDENTIALS — SAVE NOW`, gardez au moins 10 Go libres pour les quelque 3 Go d'images et n'ouvrez l'UI qu'après le smoke strict.

## Premier démarrage en local

```bash
./astinuslab.sh setup
sudo ./astinuslab.sh setup-host --with-mac
./ops/images/download_images.sh
./astinuslab.sh start
./astinuslab.sh smoke --require-running
```

`./astinuslab.sh setup` initialise chaque catalogue actif dans `config/catalog/` à partir de son
fichier `*.example.yaml` correspondant lorsque le fichier actif manque ; aucune copie
manuelle n'est nécessaire (voir [Configuration](configuration.md)).

Ouvrez `http://localhost:8000`.

Si vous voulez aussi le serveur de documentation :

```bash
./astinuslab.sh start --docs
```

Le serveur de docs utilise `http://localhost:9000` par défaut.

## Ajouter une image cloud

Placez les images cloud Linux dans `images/definitions/`. Les entrées de catalogue existantes référencent des noms de fichiers tels que :

| Target ID | Nom de fichier image attendu |
| --- | --- |
| `ubuntu-22-04-default` | `ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img` |
| `ubuntu-24.04` | `noble-server-cloudimg-amd64.img` |
| `debian-12` | `debian-12-genericcloud-amd64.qcow2` |
| `fedora-40` | `Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2` |

Pour scanner les images nouvellement ajoutées :

```bash
./astinuslab.sh scan-images --dry-run
./astinuslab.sh scan-images
./astinuslab.sh restart
```

## Première capture dans Playground

1. Ouvrez `http://localhost:8000` et connectez-vous si nécessaire. L'accueil ouvre **Playground**. **Aide** redirige vers le serveur de documentation séparé, normalement `http://localhost:9000`, dans la langue choisie. En développement, utilisez `./astinuslab.sh start --docs`. Avec une API déjà active, servez les guides depuis un checkout équipé des dépendances de documentation : `.venv/bin/python -m mkdocs serve -f docs/mkdocs.yml --dev-addr localhost:9000`. Si Aide ne répond pas, démarrez ce serveur ou lisez les fichiers source. Le lien utilise le `localhost` du navigateur ; un navigateur distant nécessite son propre serveur ou un transfert de port configuré.
2. Choisissez **+ Target**, sélectionnez une image disponible et gardez **No workload** pour la première capture.
3. Vérifiez les ressources et le réseau isolé par défaut, puis choisissez **Create target**. La session est créée automatiquement ; aucun bouton distinct ne la démarre.
4. Attendez que la VM fonctionne, sélectionnez-la et capturez la mémoire ou le disque depuis ses détails. Attendez la fin avant de télécharger l’artefact, le manifeste et la somme de contrôle.
5. Les preuves restent dans **Storage**, même après **End Session**. La fermeture déclenche un nettoyage asynchrone : attendez le résultat final et examinez tout échec.

Pour ajouter une charge vulnérable, consultez d’abord [Préparer les charges](../user/PREBAKED_IMAGES.md) : sur un réseau isolé, les installateurs nécessitent un cache ou une image préparée correspondante ; s’ils nécessitent Internet, choisissez explicitement un réseau avec accès sortant. L’assistant vérifie le formulaire et affiche ce prérequis, sans vérifier le cache ni la disponibilité de l’hôte.

Ensuite, [analysez la capture exacte](../user/VOLATILITY3.md) et [exportez ses preuves](../reference/FORENSIC_INTEGRITY.md). Relevez l’identité du noyau avant de fermer la VM. Les [packages portables](../user/PORTABLE_LABS.md) restent désactivés, expérimentaux et exclus des parcours pris en charge dans 1.0.0.

## Acquisition reproductible avec Jobs

Ouvrez **Jobs → New Job** dans la barre latérale. Sélectionnez une cible et `immediate-capture` pour une capture directe ou `lab-session` pour un laboratoire manuel. Lancez le job et attendez les artefacts. Jobs prépare également le cache des charges. Fermez la session Playground ordinaire et confirmez son nettoyage avant de passer à Jobs.

## Étapes suivantes

- [Exécution en local](running-locally.md) couvre la configuration complète et le runbook.
- [Configuration](configuration.md) explique `astinuslab.toml`, l'environnement du lanceur et les catalogues.
- [Exemples](examples.md) donne des appels d'API courants.
- [Dépannage](troubleshooting.md) liste les erreurs et correctifs courants.

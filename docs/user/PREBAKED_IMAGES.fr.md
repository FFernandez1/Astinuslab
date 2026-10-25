# Images pré-cuites et performance

Pour Windows, consultez [Images Windows](WINDOWS_IMAGES.md), le guide de référence en anglais. Les recettes Linux de cette page ne préparent pas Windows. Conservez les copies de référence et de récupération.

L'installation de workloads complexes (comme une pile LAMP complète) peut prendre plusieurs minutes par job. Pour obtenir des lancements de labo quasi instantanés, AstinusLab vous permet de « cuire » un workload directement dans une nouvelle image de base.

## 0b. Charges Playground sur un réseau isolé

Le réseau Playground par défaut ne transfère pas le trafic guest vers Internet. Les installateurs sans cache utilisant des dépôts ou Git nécessitent une préparation avant le lancement isolé. L’assistant affiche ce prérequis sans vérifier le cache ni activer l’accès sortant.

1. Fermez une session Playground ordinaire et confirmez son nettoyage avant de passer à Jobs. Activez la clé SSH dans **Security** et préparez l’image de base sélectionnée.
2. Sur un hôte avec un accès sortant explicite pour Jobs (généralement le réseau NAT `default` de libvirt), exécutez un `lab-session` avec l’image/charge souhaitée. Attendez un provisionnement réussi et `waiting_for_capture` ; confirmez une entrée correspondante dans `<storage_dir>/workload-cache/` avant d’annuler/terminer le Job et d’attendre son nettoyage.
3. Revenez dans Playground et choisissez la même image/charge sur le réseau isolé. La clé de cache lie ID de cible, charge, identité de l’image et empreinte du script. Une entrée correspondante évite l’installation. Sans elle, l’installateur s’exécute en direct : préparez d’abord ou choisissez explicitement un réseau avec accès sortant.

Définissez `api_curl` depuis les [Exemples API](../usage/examples.md) avant d’envoyer le Job de préparation :

```bash
api_curl -sS -X POST "$BASE_URL/api/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"target_id":"ubuntu-24.04","workload_id":"dvwa",
       "scenario_id":"lab-session","execution_mode":"manual_trigger"}'
```


Avant le helper, configurez `ASTINUSLAB_DATA_DIR` avec la racine de données et `ASTINUSLAB_TOKEN` avec le bearer token administrateur. `BASE_URL` permet de préciser l’URL de l’API locale. Sans token, le helper utilise cookie et en-tête CSRF pour le développement sans authentification. Consultez le contrat de connexion, les délais et la récupération dans le guide anglais (sélectionnez English dans le menu de langue) ; un échec retourne un code non nul et conserve le Job pour inspection.

## 1. Cuire un workload

Utilisez le script `ops/host/bake_workload.sh` pour automatiser ce processus. Il crée un job temporaire, installe le workload, arrête la VM et aplatit le résultat en une nouvelle image QCOW2 autonome.

### Utilisation
```bash
./ops/host/bake_workload.sh <target_id> <workload_id> [output_image_name]
```

**Exemple** :
```bash
./ops/host/bake_workload.sh ubuntu-22-04-default wordpress-6.4 ubuntu-22.04-wordpress.qcow2
```

### Étapes suivantes
Une fois le script terminé :
1. L’image est enregistrée sous `images/definitions/` dans la racine de données configurée. Le helper ne l’inspecte pas : ouvrez **Images**, demandez l’inspection puis vérifiez distribution, version et utilisateur. L’aplatissement ne prouve ni le démarrage ni la disponibilité du Workload.
2.  (Optionnel) Ajoutez une entrée de cible curée à `config/catalog/targets.yaml`
    si vous voulez surcharger les valeurs auto-découvertes ou fixer des
    paramètres spécifiques au scénario :
    ```yaml
    - id: ubuntu-22.04-wordpress-prebaked
      enabled: true
      distro: { slug: ubuntu, version: "22.04", family: debian }
      image:
        source_type: qcow2
        image_ref: ubuntu-22.04-wordpress.qcow2
        default_username: ubuntu
    ```
3. Actualisez le catalogue et validez la nouvelle image dans un labo jetable. Le logiciel préparé est déjà dans l’image ; choisir un autre Workload peut lancer son installateur.

---

## 2. Pré-cuire les images d'attaquant

Les images d'attaquant (Kali, Parrot) nécessitent souvent des outils et des configurations SSH spécifiques. Utilisez `ops/host/prebake_attacker_image.sh` pour les préparer.

### Utilisation
```bash
./ops/host/prebake_attacker_image.sh <source_image> <output_image> [attacker_user] [ssh_public_key]
```

**Exemple** :
```bash
./ops/host/prebake_attacker_image.sh \
  images/definitions/kali-linux-2024.qcow2 \
  images/definitions/kali-prebaked.qcow2 \
  kali \
  storage/ssh_keys/astinuslab-default.pub
```

### Pourquoi l'utiliser ?
*   **Configuration SSH automatique** : Injecte la clé publique du runner AstinusLab pour qu'il puisse contrôler la VM attaquante.
*   **Guest Agent** : Installe `qemu-guest-agent` pour des arrêts propres.
*   **Vitesse** : Pas besoin que cloud-init exécute de lourdes tâches de configuration à chaque démarrage.

---

## 3. Préparer des images target stock

Les VMs **target** Playground sont provisionnées avec un seed cloud-init par VM. Certaines images cloud stock conservent un état cloud-init qui empêche le seed d'être réappliqué, elles ne deviennent donc jamais accessibles en SSH. Contrairement au prebaking attacker, la préparation target ne bake pas d'utilisateur ni de clé : l'étape essentielle est `cloud-init clean`, qui supprime l'état stale pour que cloud-init se relance au prochain boot.

### Utilisation
```bash
# dry run first (no root, just prints what would be prepared):
ops/host/prepare_target_image.sh ubuntu-20-04-legacy --resolve-only

# prepare it (in place; network-free):
ops/host/prepare_target_image.sh ubuntu-20-04-legacy

# online host or image missing ssh? also apt-install ssh + guest agent:
ops/host/prepare_target_image.sh debian-12 --with-packages
```

Vous pouvez passer un id de target de catalogue ou un nom de fichier sous `images/definitions/`. L'image est réécrite atomiquement et son `.meta.json` est marqué `"prepared": true` en cas de succès.

### Prérequis
`libguestfs-tools` (fournit `virt-customize`). `--with-packages` nécessite aussi apt/réseau dans l'appliance de build. Si `virt-customize` échoue avec une erreur supermin, installez `libguestfs-tools`, autorisez la lecture de `/boot/vmlinuz-*` et confirmez que `libguestfs-test-tool` sort 0.

## 4. Conseils de performance

*   **Copie reflink** : AstinusLab utilise `cp --reflink=auto` lors de la création des overlays. Sur des systèmes de fichiers comme **Btrfs** ou **XFS**, cela rend la préparation des images quasi instantanée et économise beaucoup d'espace disque.
*   **Stockage SSD** : Placez toujours vos répertoires `images/definitions` et `storage/` sur un SSD pour de meilleures performances des VMs.

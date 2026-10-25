# Dépannage

## Erreurs de permissions et de libvirt

Vérifiez l'appartenance aux groupes :

```bash
id -nG | tr ' ' '\n' | grep -E 'libvirt|kvm|astinuslab'
```

Si `libvirt` ou `kvm` manque :

```bash
sudo usermod -aG libvirt,kvm "$USER"
```

Déconnectez-vous et reconnectez-vous avant de réessayer.

Vérifiez libvirt :

```bash
virsh -c qemu:///system list
```

## Le préflight sudo sans mot de passe échoue

Le host runner attend des scripts auxiliaires installés sous `/usr/local/libexec/astinuslab/`.

Installez-les ou rafraîchissez-les :

```bash
sudo ./astinuslab.sh setup-host
```

Vérifiez :

```bash
sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop
```

Ne configurez pas sudoers pour exécuter les scripts auxiliaires directement depuis le checkout du code source.

## Le mode développement refuse de démarrer

`./astinuslab.sh start` refuse de démarrer lorsque les services systemd AstinusLab sont actifs.

```bash
./astinuslab.sh status
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo ./astinuslab.sh setup-host
./astinuslab.sh start
```

## Mises à niveau de bases legacy pré-Alembic

Si une ancienne base ne peut pas migrer, sauvegardez `storage/astinuslab.db`, inspectez les logs de démarrage et appliquez le chemin de migration documenté avant de relancer les services.

## Les Jobs restent en queue pendant une longue capture

Le runner de capture/cycle de vie est sériel par conception. Une longue capture peut retarder réconciliation, expiration et autres jobs jusqu'à sa fin. Les exécutions interactives du workbench utilisent la queue `analysis` séparée.

## Capacité VM atteinte (HTTP 409, code `vm_capacity`)

`orchestration.max_concurrent_vms` limite le nombre de VMs actives simultanément. Arrêtez ou détruisez des VMs Playground, attendez que la réconciliation libère les lignes en teardown, ou augmentez la limite si l'hôte a assez de RAM.

## Les Jobs restent en `waiting_for_guest_access`

Causes probables :

- `default_username` dans `config/catalog/targets.yaml` ne correspond pas à l'image cloud.
- Le guest n'a pas traité les données cloud-init.
- Le réseau a échoué ou DHCP n'a pas attribué d'adresse.
- La clé SSH est manquante ou non active dans l'UI.

Vérifiez :

```bash
./astinuslab.sh logs
```

Pour les images avec un réseau spécifique au fournisseur, essayez une surcharge dans le catalogue de cibles telle que `inject_network_config: false`.

## Les nouvelles images ne sont pas reconnues

Exécutez :

```bash
./astinuslab.sh scan-images --dry-run
```

Si l'image apparaît toujours comme inconnue :

- Installez `libguestfs-tools` pour disposer de `virt-inspector`.
- Utilisez un nom de fichier qui identifie la distro et la version.
- Ajoutez une entrée explicite à `config/catalog/targets.yaml`.
- Vérifiez le sidecar `<image>.meta.json` généré dans `images/definitions/`.

## Erreurs AppArmor ou d'accès au stockage

Le mode release configure AppArmor et les ACL de traversée pour `/opt/astinuslab/storage`.

Le mode développement stocke les artefacts sous `<repo>/storage`. Si les jobs avec de vraies VMs échouent à cause de l'accès au stockage, exécutez :

```bash
sudo ./astinuslab.sh setup-host --with-apparmor
```

Puis réessayez `./astinuslab.sh smoke` et le job.

## La génération d'ISF Volatility 3 échoue

Consultez d'abord `/api/v1/volatility/readiness` ou la page Volatility. Les
codes `*_not_configured`, `*_not_found`, `*_not_executable`,
`volatility_version_unsupported`, `volatility_probe_failed`,
`dwarf2json_hash_not_configured` et `dwarf2json_hash_mismatch` indiquent le
correctif attendu. Un `guest transfer mismatch` impose de vérifier le même hash
sur l'hôte et le guest; Upload ISF reste l'alternative manuelle. Ne copiez pas
les outils dans le checkout AstinusLab.
La chaîne de production ISF uniquement est :

```text
local_cache -> prebuilt_isf -> debuginfod -> host_fetch
```

Si cette chaîne échoue, AstinusLab essaie le fallback du guest en direct, qui peut utiliser un `vmlinux` correspondant avec DWARF, debuginfod ou une installation de paquets de débogage explicitement autorisée. `dwarf2json` n'accepte pas une entrée contenant uniquement du BTF et ce fallback ne l'utilise jamais.

Causes courantes :

- `rpm2cpio`, `cpio`, `libguestfs-tools`, `qemu-utils` ou `libvirt-clients` manquants.
- Aucun symbole de débogage correspondant au noyau exact du guest.
- Les URLs du mirror prebuilt manquent ou ne sont pas en `https://`.
- La cible n'est pas une distribution Linux x86_64 prise en charge.

Voir [Volatility 3](../user/VOLATILITY3.md) et [Prise en charge des distros](../reference/DISTRO_SUPPORT.md).

### Sortie de plugin manquante

Vérifiez qu'un dump mémoire existe, qu'un ISF compatible est disponible et que les fichiers sous `storage/dumps/<job_id>/vol3/` ne contiennent pas d'erreurs Volatility 3. Regénérez l'ISF s'il a été construit pour un build ID différent.

## Problèmes d'espace disque

Les dumps mémoire peuvent être aussi volumineux que la RAM de la VM. Les artefacts disque peuvent faire plusieurs Go.

Vérifiez :

- `storage/dumps/`
- `storage/playground/captures/`
- `/var/lib/libvirt/images/astinuslab/`
- `vol3/symbols/`
- `site/`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`

Voir [Fichiers d'exécution générés](../development/GENERATED_RUNTIME_FILES.md) avant de supprimer des chemins générés.

## Journaux

Le mode développement écrit les journaux sous `storage/logs/` :

```bash
./astinuslab.sh logs
```

Le mode release utilise les journaux systemd :

```bash
journalctl -u astinuslab-api@$USER -f
journalctl -u astinuslab-runner@$USER -f
journalctl -u astinuslab-analysis@$USER -f
```

Une installation conserve aussi sa propre copie alternée dans `storage/logs/`, que lit la page **Logs**. Utilisez `[orchestration] log_to_file = false` pour désactiver cette copie et n'utiliser que le journal.

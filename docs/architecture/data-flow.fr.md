# Flux de données

## Flux d'un Job forensique

```text
1. Operator -> FastAPI: POST /api/v1/jobs
2. FastAPI -> SQLite: persist queued Job
3. FastAPI -> Celery broker: enqueue task
4. Lifecycle runner -> SQLite: preparing_image
5. Lifecycle runner -> libvirt: overlay, seed, domain
6. Lifecycle runner -> guest: wait for IP/SSH; run scenario/workload
7. Lifecycle runner -> libvirt: capture memory and/or disk
8. Lifecycle runner -> storage: artifact + manifest + SHA-256
9. Lifecycle runner -> SQLite: completed / failed / cancelled
10. Operator -> FastAPI: download published evidence
```

Tous les changements d'état d'un job doivent passer par la logique de transition du repository. Les états terminaux sont `completed`, `failed` et `cancelled`.

## Flux de labo manuel

Les jobs de labo manuel utilisent des scénarios `manual_trigger` :

1. Le runner démarre la VM cible.
2. Le workload sélectionné est installé et son état de santé vérifié.
3. Des métadonnées optionnelles de runtime de la VM attaquante sont enregistrées.
4. Le job attend en `waiting_for_capture`.
5. L'opérateur appelle `/api/v1/jobs/{job_id}/capture` ou utilise l'UI.
6. Le runner capture la mémoire, le disque ou les deux.

## Flux du Playground

Le Playground utilise le même host runner mais une surface d'API et un modèle d'état différents :

1. Créer ou réutiliser une session de playground active.
2. Créer des réseaux de session adossés à libvirt.
3. Lancer des VMs depuis les images du catalogue.
4. Attacher ou détacher des NIC selon les besoins.
5. Déclencher des captures mémoire ou disque au niveau VM.
6. Détruire la session.

Les overlays de VM du playground sont éphémères. Les artefacts de capture du playground persistent jusqu'à leur suppression explicite.

## Flux d'artefacts

La capture mémoire utilise la fonctionnalité de core dump de libvirt depuis l'hôte. La capture disque convertit l'état du disque de la VM en un artefact téléchargeable. Pour les artefacts terminés, AstinusLab stocke les métadonnées de chemin, nom de fichier, taille et SHA-256, et sert les fichiers via les endpoints d'artefacts avec des en-têtes de somme de contrôle lorsqu'ils sont disponibles.

## Flux Volatility 3

Pour l'analyse mémoire Linux, AstinusLab identifie le noyau du guest et essaie les fournisseurs d'ISF dans l'ordre :

```text
local_cache -> prebuilt_isf -> debuginfod -> host_fetch
```

Si la chaîne côté hôte échoue, AstinusLab essaie le fallback du guest en direct, qui peut utiliser un `vmlinux` correspondant avec DWARF, debuginfod ou une installation de paquets de débogage explicitement autorisée. `dwarf2json` n'accepte pas une entrée contenant uniquement du BTF et ce fallback ne l'utilise jamais. Les ISF réutilisables sont stockés dans `vol3/symbols/linux/`. La sortie Volatility par job est stockée sous `storage/dumps/<job_id>/vol3/`.

# ADR : Génération d'ISF basée sur VM

## Statut

Accepté.

## Contexte

L'analyse Linux avec Volatility 3 nécessite un ISF qui correspond au noyau exact ayant produit le dump mémoire. Les étiquettes des images cloud et les métadonnées du catalogue sont utiles, mais elles ne suffisent pas : les mises à jour de paquets, les reconstructions d'images et les noyaux personnalisés peuvent changer le noyau en cours d'exécution.

AstinusLab provisionne déjà des VMs fraîches pour l'acquisition. Ce cycle de vie donne au runner un système en direct depuis lequel il peut détecter le vrai noyau, installer des paquets de débogage si nécessaire et construire un ISF correspondant avant que la VM ne soit détruite.

## Décision

Les VMs fraîches restent l'architecture centrale pour la génération d'ISF Linux.

- AstinusLab détecte le noyau en cours d'exécution depuis le guest.
- AstinusLab peut récupérer les paquets de débogage de la distro sur l'hôte lorsque les dépôts les fournissent.
- AstinusLab peut installer des paquets de débogage dans le guest lorsque la voie de la VM en direct est nécessaire.
- Les ISF générés sont des artefacts de cache global réutilisables sous `vol3/symbols/linux/`.
- La réutilisation du cache est conditionnée à la fois par la **version du noyau** et le **build ID**. Un ISF en cache pour le même `uname -r` mais avec un GNU build ID différent (backport du fournisseur, reconstruction de sécurité) est traité comme un miss de cache et reconstruit. Lorsque le build ID de l'ISF en cache est inconnu (artefact préexistant), un soft-hit est appliqué : l'ISF en cache est accepté et le build ID est enregistré pour de futures comparaisons.
- Chaque job enregistre quel ISF il a produit ou réutilisé, y compris le build ID.
- La sortie des plugins Volatility reste locale au job sous `storage/dumps/<job_id>/vol3/`.
- Les jobs explicites ISF uniquement échouent lorsqu'aucun ISF valide n'est produit.

## Conséquences

Le cache évite de reconstruire les symboles pour les jobs répétés avec le même noyau, tandis que la sortie d'analyse locale au job préserve la preuve par exécution. Le système ne prend en charge que les principales distributions Linux x86_64 ; les cibles non-x86_64 sont hors périmètre.

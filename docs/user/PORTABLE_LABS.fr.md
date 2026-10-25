# Packages de laboratoire portables

**Expérimental et non accepté pour 1.0.0.** Les packages sont désactivés par
défaut et exclus des parcours pris en charge dans la première version.
L’acceptation du cycle complet, sur deux hôtes, hors ligne et des performances
(P1–P3) reste ouverte. Cette page décrit le code expérimental pour des essais
contrôlés, sans ces garanties. Le libellé **Verrouillé** ne certifie pas
l’acceptation de la version.

Le sélecteur de tentatives de vérification permet de consulter les tentatives en cours, échouées et récemment annulées, avec un motif sûr et un identifiant copiable. L’historique des vérifications terminées affiche le titre, la version et l’échéance réelle d’exportation en UTC. Les attestations expirées ou indisponibles restent lisibles, avec l’exportation désactivée ; après expiration, reconstruisez le laboratoire et vérifiez-le à nouveau. Les refus d’exportation apparaissent à côté de la ligne concernée et conservent vos choix d’intégration des fichiers et de redistribution.

Les packages sont conçus pour partager une expérience de laboratoire versionnée
entre installations locales d’AstinusLab. Ils contiennent topologie, ressources,
runbook, licences, empreintes SHA-256 et, lorsque cela est autorisé, des couches
de démarrage immuables. Ce n’est pas un service cloud.

Pour des essais expérimentaux contrôlés uniquement, activez `[packages].enabled = true` dans
`astinuslab.toml`, puis ouvrez **Packages** dans la barre latérale.

## Parcours du créateur

1. Activez les paquets et redémarrez l’API/application pour enregistrer les routes et l’entrée latérale. Construisez et validez le laboratoire dans Playground ; gardez cette session ouverte.
2. Ouvrez **Paquets → Créer un package**. Saisissez ID, version, titre, créateur et licence, puis **Charger depuis le Playground actuel**. Le JSON canonique `LabDefinition` et l’ID de session sont générés sans figer ni modifier le laboratoire. Les ressources sont estimées à partir des tailles déclarées des VMs : vérifiez-les. Rechargez pour appliquer des changements de métadonnées ; les options avancées se modifient dans le JSON.
3. Ajoutez runbook, objectifs, indices, points de contrôle et références de fichiers. Objectifs/indices sont des tableaux JSON de chaînes ; les contrôles sont des observations/assertions typées du guest, jamais des hooks de l’hôte. Les fichiers observés sont du JSON fini sous `/var/lib/astinuslab/observations`, lu via QEMU Guest Agent avec une limite de 64 Kio. L’auteur de l’image doit empêcher les liens symboliques vers d’autres chemins : QGA applique une racine lexicale.
4. Choisissez **Enregistrer le brouillon et vérifier l’exécution propre** avec la session encore ouverte. La vérification la fige automatiquement, s’exécute de manière asynchrone et la nettoie après réussite ou échec. Ne choisissez pas **End Session** auparavant. Suivez la progression, les échecs et les annulations.
5. Les tentatives réussies restent dans les vérifications terminées après nettoyage ou rechargement du navigateur. Exportez-y la vérification choisie : son ID lie définition, runbook et couches attestées. L’intégration des couches est désactivée par défaut. Si l’attestation l’exige, activez-la et confirmez explicitement les droits de redistribution ; sinon l’export est refusé avec `embedding_required` ou `redistribution_unconfirmed`. Le contenu vérifié n’est jamais modifié silencieusement.
6. Pour partager seulement topologie et instructions, exportez **Déverrouillé** depuis une session Playground active. Ce mode est non reproductible et ne prouve pas des octets initiaux identiques.

Un runbook non vide est obligatoire avant la vérification. Exportez dans les 24 heures suivant la vérification réussie : l’attestation expire ensuite et l’export renvoie `attestation_expired`. Conservez l’archive téléchargée. Pour obtenir une nouvelle attestation, reconstruisez le laboratoire et vérifiez-le à nouveau ; le résultat terminé ne restaure pas la session nettoyée.

Une recette JSON Playground décrit la topologie visuelle à réimporter dans Playground. Le JSON canonique `LabDefinition` est l’entrée du créateur avec identité, ressources et expérience. L’archive `.astinuslab` est le paquet reçu par **Importer / inspecter**. Ces formats ne sont pas interchangeables : le chargement depuis Playground effectue la conversion. Inspection et rendu sont passifs ; ils n’exécutent ni runbook ni contenu du guest.

## Flux du destinataire

**Utiliser un package** est la vue initiale. **Ouvrir le package** retrouve les instructions conservées sans réimporter. Les actions concernent le package sélectionné ; chaque panneau d’exécution conserve le nom de son propre package. **Ouvrir le laboratoire dans Playground** vérifie que sa session est toujours active. Si le nettoyage est incomplet, conservez l’identifiant et consultez **Logs** : les ressources peuvent rester réservées ; aucune réparation automatique n’est proposée.

Dans **Importer / inspecter**, sélectionnez une archive `.astinuslab`.
L’inspection est inerte: elle valide limites, chemins, signatures et inventaire
SHA-256 sans créer de VM ni exécuter de contenu. Importez seulement après avoir
examiné identité, confiance, licence, ressources et actifs.

Lancez ensuite **Prepare** pour résoudre les octets intégrés, le cache local,
puis les sources épinglées explicitement autorisées. Le réseau des sources
demande un opt-in et reste soumis à la politique locale. Lancez **Preflight**,
examinez stockage conservé, capacité, topologie, reachability et avertissements;
confirmez chaque avertissement et l’egress avant **Launch**. Le serveur recalcule
le preflight au lancement.

Le lancement renvoie une opération persistante. Attendez `ready`; **Reset**
recrée les couches inscriptibles depuis la couche immuable et **Teardown** libère
l’instance sans supprimer le contenu encore référencé. Suppression et GC
attendent la disparition des références et leases.

## Hôte et limites

Le profil prévu pour l’acceptation est Ubuntu 24.04 LTS sur x86_64 avec KVM.
Aucun hôte n’est accepté pour le parcours portable complet. Les versions
ultérieures d’Ubuntu et les autres distributions Linux ne sont pas vérifiées ;
leur utilisation expérimentale n’implique ni prise en charge ni vérification. Un
petit laboratoire suggère souvent 2–4 threads CPU, 4–8 Go de RAM et 40 Go libres.
Le flux est local: aucune publication cloud ni coordination multi-hôte. Un
export non verrouillé ne partage que topologie et runbook. Une preuve sur hôte
réel est nécessaire pour déclarer la reproductibilité.

## API

Le même cycle est disponible sous `/api/v1/lab-packages`: `inspect`,
`imports/{inspection_id}/commit`, liste, `prepare`, `preflight`, `launch`,
`operations/{instance_id}`, `drafts` et `verify-clean-run`. Les mutations
nécessitent CSRF et, si configurée, l’authentification administrateur. Conservez
les clés d’idempotence pour les nouvelles tentatives de Prepare ou Launch.

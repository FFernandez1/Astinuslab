# Erreurs

AstinusLab utilise des erreurs HTTP standard de FastAPI et spécifiques à l'application.

## Codes de statut courants

| Statut | Signification |
| --- | --- |
| `400 Bad Request` | Forme de requête invalide ou entrée de playground invalide, comme des références à des images/réseaux inconnus. |
| `401 Unauthorized` | Token bearer administrateur absent ou invalide lorsque l'authentification API est activée. Le corps est `{"detail": "admin bearer token required"}` avec un en-tête `WWW-Authenticate: Bearer`. Ce code n'est pas renvoyé lorsque l'authentification est désactivée (valeur par défaut). |
| `404 Not Found` | Job, artefact, session de playground, VM, réseau, acquisition ou métadonnées de labo inexistants. |
| `409 Conflict` | Transition d'état invalide, session de playground active dupliquée, conflit avec un job actif, noms dupliqués, réseau utilisé, ou capture demandée alors qu'une VM n'est pas en cours d'exécution. |
| `422 Unprocessable Entity` | Échec de validation Pydantic, comme des valeurs d'enum invalides ou des champs malformés. |
| `500 Internal Server Error` | Échec inattendu côté serveur. |
| `503 Service Unavailable` | Le travail du playground n'a pas pu être mis en file, la collecte de métriques est indisponible, ou la requête nécessite un outil Volatility fourni par l'opérateur qui n'est pas prêt. Le cas de dépendance porte `code: "dependency_unavailable"` et `context.dependency_code`. |

## Corps d'erreur

La plupart des erreurs renvoient du JSON avec `detail` :

```json
{
  "detail": "no active playground session"
}
```

Les transitions d'état de job invalides incluent des métadonnées d'état :

```json
{
  "detail": "invalid transition",
  "code": "invalid_job_transition",
  "context": { "from_status": "completed", "to_status": "queued" },
  "from_status": "completed",
  "to_status": "queued"
}
```

## Erreurs d'artefacts

Les endpoints hérités d'artefacts de job renvoient `404` lorsque l'artefact mémoire ou disque demandé n'est pas disponible pour ce job.

Les endpoints génériques de fichier d'artefact renvoient `404` lorsque le fichier de l'artefact, le chemin du manifeste ou le chemin du sidecar SHA-256 est manquant ou absent sur le disque. Ils renvoient `403` lorsqu'un chemin enregistré se résout en dehors de la frontière de stockage d'artefacts autorisée.

Lorsqu'elles sont disponibles, les réponses d'artefact réussies incluent :

| En-tête | Signification |
| --- | --- |
| `X-Astinuslab-SHA256` | Somme de contrôle SHA-256 enregistrée pour l'artefact. |
| `X-Astinuslab-Size` | Taille de l'artefact en octets. |

## Erreurs de file du Playground

Les endpoints du playground qui mettent en file du travail en arrière-plan renvoient `503` avec :

```json
{
  "detail": "operation could not be queued"
}
```

L'API tente de marquer comme échoués le réseau, la VM ou l'acquisition affectés lorsqu'une compensation de mise en file est disponible.

## Erreurs de dépendance

Volatility 3 et `dwarf2json` sont des outils hôtes fournis par l'opérateur qu'AstinusLab ne télécharge ni n'installe jamais. Une requête qui a besoin de l'un d'eux alors qu'il est absent, non vérifié ou dans une version non prise en charge échoue avec `503` et un code stable lisible par machine :

```json
{
  "detail": "Configure [vol3].volatility_bin to enable Volatility analysis.",
  "code": "dependency_unavailable",
  "context": { "dependency_code": "volatility_not_configured" }
}
```

`context.dependency_code` est l'un des codes de readiness :

| Code | Signification |
| --- | --- |
| `feature_disabled` | L'intégration Volatility est désactivée dans la configuration. |
| `volatility_not_configured` | Aucun exécutable Volatility n'est configuré. |
| `volatility_not_found` | L'exécutable Volatility configuré n'existe pas ou n'est pas dans le `PATH`. |
| `volatility_not_executable` | Le chemin Volatility configuré n'est pas un fichier exécutable régulier. |
| `volatility_probe_failed` | La sonde de version n'a pas pu s'exécuter, a échoué ou a renvoyé une bannière non reconnue. |
| `volatility_version_unsupported` | La sonde a rapporté une version inférieure au minimum `2.28.0`, ou une chaîne de version qui n'a pas pu être interprétée. |
| `dwarf2json_not_configured` | Aucun chemin `dwarf2json` n'est configuré. |
| `dwarf2json_hash_not_configured` | Un chemin `dwarf2json` est configuré sans son pin SHA-256. |
| `dwarf2json_not_found` | Le fichier `dwarf2json` configuré n'existe pas. |
| `dwarf2json_not_executable` | Le chemin `dwarf2json` configuré n'est pas un fichier exécutable régulier, ou n'a pas pu être lu. |
| `dwarf2json_hash_mismatch` | Le fichier `dwarf2json` configuré ne correspond pas à son pin SHA-256 configuré. |

`volatility_version_unsupported` est un minimum, pas une correspondance exacte. `2.28.0` est le minimum documenté : les versions sont ordonnées selon [PEP 440](https://peps.python.org/pep-0440/), de sorte qu'une préversion du minimum comme `2.28.0rc1` se situe *en dessous* de lui et est également refusée, et une chaîne de version qui ne peut pas être interprétée comme une version est refusée car elle ne peut pas être comparée au minimum. Une version égale ou supérieure au minimum qui ne fait pas partie des versions testées avec cette version du produit n'est **pas** refusée : elle rapporte `ready` avec `untested: true`, et l'analyse s'exécute. Consultez [Endpoints](endpoints.md) pour ce champ.

`detail` est un message sûr et actionnable par l'opérateur. Il ne porte jamais le chemin de l'exécutable configuré, le hash attendu ou réel, des valeurs d'environnement ni la sortie brute du sous-processus ; cela reste dans les logs de service protégés. Interrogez `GET /api/v1/volatility/readiness` pour lire les mêmes codes avant de mettre du travail en file.

Cela a remplacé la réponse `409` / `conflict` antérieure pour les outils indisponibles. `409` / `conflict` signifie toujours un véritable conflit d'état, par exemple une exécution déjà en file.

Les envois de formulaire du navigateur dans l'UI web redirigent vers la page avec un message d'erreur au lieu de renvoyer ce corps.

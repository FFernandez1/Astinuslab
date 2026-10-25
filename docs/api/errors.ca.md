# Errors

AstinusLab fa servir errors HTTP estàndard de FastAPI i específics de l'aplicació.

## Codis d'estat habituals

| Estat | Significat |
| --- | --- |
| `400 Bad Request` | Forma de petició no vàlida o entrada de playground no vàlida, com ara referències a imatges/xarxes desconegudes. |
| `401 Unauthorized` | Falta el token bearer d'administrador o no és vàlid quan l'autenticació de l'API està activada. El cos és `{"detail": "admin bearer token required"}` amb la capçalera `WWW-Authenticate: Bearer`. No es retorna quan l'autenticació està desactivada (valor per defecte). |
| `404 Not Found` | Job, artefacte, sessió de playground, VM, xarxa, adquisició o metadades de lab inexistents. |
| `409 Conflict` | Transició d'estat no vàlida, sessió de playground activa duplicada, conflicte amb un job actiu, noms duplicats, xarxa en ús, o captura sol·licitada mentre una VM no està en execució. |
| `422 Unprocessable Entity` | Fallada de validació de Pydantic, com ara valors d'enum no vàlids o camps malformats. |
| `500 Internal Server Error` | Fallada inesperada del costat del servidor. |
| `503 Service Unavailable` | No s'ha pogut encuar la feina del playground, la recol·lecció de mètriques no està disponible, o la petició necessita una eina Volatility subministrada per l'operador que no està a punt. El cas de dependència porta `code: "dependency_unavailable"` i `context.dependency_code`. |

## Cossos d'error

La majoria dels errors retornen JSON amb `detail`:

```json
{
  "detail": "no active playground session"
}
```

Les transicions d'estat de job no vàlides inclouen metadades d'estat:

```json
{
  "detail": "invalid transition",
  "code": "invalid_job_transition",
  "context": { "from_status": "completed", "to_status": "queued" },
  "from_status": "completed",
  "to_status": "queued"
}
```

## Errors d'artefactes

Els endpoints heretats d'artefactes de job retornen `404` quan l'artefacte de memòria o disc sol·licitat no està disponible per a aquell job.

Els endpoints genèrics de fitxer d'artefacte retornen `404` quan el fitxer de l'artefacte, la ruta del manifest o la ruta del sidecar SHA-256 falta o no és present al disc. Retornen `403` quan una ruta registrada es resol fora del límit permès d'emmagatzematge d'artefactes.

Quan estan disponibles, les respostes d'artefacte correctes inclouen:

| Capçalera | Significat |
| --- | --- |
| `X-Astinuslab-SHA256` | Checksum SHA-256 registrat per a l'artefacte. |
| `X-Astinuslab-Size` | Mida de l'artefacte en bytes. |

## Errors de cua del Playground

Els endpoints del playground que encuen feina en segon pla retornen `503` amb:

```json
{
  "detail": "operation could not be queued"
}
```

L'API intenta marcar com a fallida la xarxa, VM o adquisició afectada quan hi ha compensació d'encuament disponible.

## Errors de dependència

Volatility 3 i `dwarf2json` són eines de host subministrades per l'operador que AstinusLab mai no descarrega ni instal·la. Una petició que necessita una d'elles mentre falta, no està verificada o té una versió no suportada falla amb `503` i un codi estable llegible per màquina:

```json
{
  "detail": "Configure [vol3].volatility_bin to enable Volatility analysis.",
  "code": "dependency_unavailable",
  "context": { "dependency_code": "volatility_not_configured" }
}
```

`context.dependency_code` és un dels codis de readiness:

| Codi | Significat |
| --- | --- |
| `feature_disabled` | La integració amb Volatility està desactivada a la configuració. |
| `volatility_not_configured` | No hi ha cap executable de Volatility configurat. |
| `volatility_not_found` | L'executable de Volatility configurat no existeix o no és al `PATH`. |
| `volatility_not_executable` | La ruta de Volatility configurada no és un fitxer executable regular. |
| `volatility_probe_failed` | El sondeig de versió no s'ha pogut executar, ha fallat o ha retornat un banner no reconegut. |
| `volatility_version_unsupported` | El sondeig ha reportat una versió per sota del mínim `2.28.0`, o una cadena de versió que no s'ha pogut interpretar. |
| `dwarf2json_not_configured` | No hi ha cap ruta de `dwarf2json` configurada. |
| `dwarf2json_hash_not_configured` | Hi ha una ruta de `dwarf2json` configurada sense el seu pin SHA-256. |
| `dwarf2json_not_found` | El fitxer `dwarf2json` configurat no existeix. |
| `dwarf2json_not_executable` | La ruta de `dwarf2json` configurada no és un fitxer executable regular, o no s'ha pogut llegir. |
| `dwarf2json_hash_mismatch` | El fitxer `dwarf2json` configurat no coincideix amb el seu pin SHA-256 configurat. |

`volatility_version_unsupported` és un mínim, no una coincidència exacta. `2.28.0` és el mínim documentat: les versions s'ordenen segons [PEP 440](https://peps.python.org/pep-0440/), de manera que una preversió del mínim com ara `2.28.0rc1` queda *per sota* d'ell i també es rebutja, i una cadena de versió que no es pot interpretar com a versió es rebutja perquè no es pot comparar amb el mínim. Una versió igual o superior al mínim que no sigui entre les versions provades amb aquesta versió del producte **no** es rebutja: informa `ready` amb `untested: true`, i l'anàlisi s'executa. Consulteu [Endpoints](endpoints.md) per a aquest camp.

`detail` és un missatge segur i accionable per l'operador. Mai no porta la ruta de l'executable configurat, el hash esperat o real, valors d'entorn ni sortida crua del subprocés; això queda als logs de servei protegits. Consulta `GET /api/v1/volatility/readiness` per llegir els mateixos codis abans d'encuar feina.

Això va substituir la resposta anterior `409` / `conflict` per a eines no disponibles. `409` / `conflict` continua significant un conflicte d'estat real, com ara una execució ja encuada.

Els enviaments de formulari del navegador a la UI web redirigeixen a la pàgina amb un missatge d'error en lloc de retornar aquest cos.

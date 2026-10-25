# Errores

AstinusLab usa errores HTTP estándar de FastAPI y específicos de la aplicación.

## Códigos de estado comunes

| Estado | Significado |
| --- | --- |
| `400 Bad Request` | Forma de petición inválida o entrada de playground inválida, como referencias a imágenes/redes desconocidas. |
| `401 Unauthorized` | Falta el token bearer de administrador o no es válido cuando la autenticación de API está activada. El cuerpo es `{"detail": "admin bearer token required"}` y lleva la cabecera `WWW-Authenticate: Bearer`. No se devuelve cuando la autenticación está desactivada (valor predeterminado). |
| `404 Not Found` | Job, artefacto, sesión de playground, VM, red, adquisición o metadatos de lab inexistentes. |
| `409 Conflict` | Transición de estado inválida, sesión de playground activa duplicada, conflicto con un job activo, nombres duplicados, red en uso, o captura solicitada mientras una VM no está en ejecución. |
| `422 Unprocessable Entity` | Fallo de validación de Pydantic, como valores de enum inválidos o campos malformados. |
| `500 Internal Server Error` | Fallo inesperado del lado del servidor. |
| `503 Service Unavailable` | No se pudo encolar el trabajo del playground, la recolección de métricas no está disponible, o la petición necesita una herramienta Volatility suministrada por el operador que no está lista. El caso de dependencia lleva `code: "dependency_unavailable"` y `context.dependency_code`. |

## Cuerpos de error

La mayoría de los errores devuelven JSON con `detail`:

```json
{
  "detail": "no active playground session"
}
```

Las transiciones de estado de job inválidas incluyen metadatos de estado:

```json
{
  "detail": "invalid transition",
  "code": "invalid_job_transition",
  "context": { "from_status": "completed", "to_status": "queued" },
  "from_status": "completed",
  "to_status": "queued"
}
```

## Errores de artefactos

Los endpoints heredados de artefactos de job devuelven `404` cuando el artefacto de memoria o disco solicitado no está disponible para ese job.

Los endpoints genéricos de archivo de artefacto devuelven `404` cuando el archivo del artefacto, la ruta del manifiesto o la ruta del sidecar SHA-256 falta o no está presente en disco. Devuelven `403` cuando una ruta registrada se resuelve fuera del límite permitido de almacenamiento de artefactos.

Cuando están disponibles, las respuestas de artefacto exitosas incluyen:

| Cabecera | Significado |
| --- | --- |
| `X-Astinuslab-SHA256` | Checksum SHA-256 registrado para el artefacto. |
| `X-Astinuslab-Size` | Tamaño del artefacto en bytes. |

## Errores de cola del Playground

Los endpoints del playground que encolan trabajo en segundo plano devuelven `503` con:

```json
{
  "detail": "operation could not be queued"
}
```

La API intenta marcar como fallida la red, VM o adquisición afectada cuando hay compensación de encolado disponible.

## Errores de dependencia

Volatility 3 y `dwarf2json` son herramientas de host suministradas por el operador que AstinusLab nunca descarga ni instala. Una petición que necesita una de ellas mientras falta, no está verificada o tiene una versión no soportada falla con `503` y un código estable legible por máquina:

```json
{
  "detail": "Configure [vol3].volatility_bin to enable Volatility analysis.",
  "code": "dependency_unavailable",
  "context": { "dependency_code": "volatility_not_configured" }
}
```

`context.dependency_code` es uno de los códigos de readiness:

| Código | Significado |
| --- | --- |
| `feature_disabled` | La integración con Volatility está desactivada en la configuración. |
| `volatility_not_configured` | No hay ejecutable de Volatility configurado. |
| `volatility_not_found` | El ejecutable de Volatility configurado no existe o no está en el `PATH`. |
| `volatility_not_executable` | La ruta de Volatility configurada no es un archivo ejecutable regular. |
| `volatility_probe_failed` | El sondeo de versión no pudo ejecutarse, falló o devolvió un banner no reconocido. |
| `volatility_version_unsupported` | El sondeo reportó una versión por debajo del mínimo `2.28.0`, o una cadena de versión que no se pudo interpretar. |
| `dwarf2json_not_configured` | No hay ruta de `dwarf2json` configurada. |
| `dwarf2json_hash_not_configured` | Hay una ruta de `dwarf2json` configurada sin su pin SHA-256. |
| `dwarf2json_not_found` | El archivo `dwarf2json` configurado no existe. |
| `dwarf2json_not_executable` | La ruta de `dwarf2json` configurada no es un archivo ejecutable regular, o no se pudo leer. |
| `dwarf2json_hash_mismatch` | El archivo `dwarf2json` configurado no coincide con su pin SHA-256 configurado. |

`volatility_version_unsupported` es un mínimo, no una coincidencia exacta. `2.28.0` es el mínimo documentado: las versiones se ordenan según [PEP 440](https://peps.python.org/pep-0440/), de modo que una preversión del mínimo como `2.28.0rc1` queda *por debajo* de él y también se rechaza, y una cadena de versión que no se puede interpretar como versión se rechaza porque no se puede comparar con el mínimo. Una versión igual o superior al mínimo que no esté entre las versiones probadas con esta versión del producto **no** se rechaza: informa `ready` con `untested: true`, y el análisis se ejecuta. Consulta [Endpoints](endpoints.md) para ese campo.

`detail` es un mensaje seguro y accionable por el operador. Nunca lleva la ruta del ejecutable configurado, el hash esperado o real, valores de entorno ni salida cruda del subproceso; eso queda en los logs de servicio protegidos. Consulta `GET /api/v1/volatility/readiness` para leer los mismos códigos antes de encolar trabajo.

Esto sustituyó la respuesta anterior `409` / `conflict` para herramientas no disponibles. `409` / `conflict` sigue significando un conflicto de estado real, como una ejecución ya encolada.

Los envíos de formulario del navegador en la UI web redirigen de vuelta a la página con un mensaje de error en lugar de devolver este cuerpo.

# Abast V1 i full de ruta

AstinusLab V1 és un playground d'atac i forense per a la recerca de seguretat en local. Se centra a construir labs d'aplicacions web vulnerables, connectar VMs atacants, capturar evidència i analitzar memòria amb Volatility 3.

## Abast de V1

- Operació localhost/local de confiança amb autenticació opcional d'un administrador mitjançant bearer token i sessió de navegador. Les instal·lacions release l'activen per defecte. Multiusuari i RBAC continuen fora de l'abast de V1.
- UI guiada del Playground com a flux de lab d'entrada principal.
- Importació/exportació de receptes JSON per a labs de Playground repetibles; la creació més àmplia de receptes YAML continua sent full de ruta.
- Labs d'aplicacions web vulnerables primer: WordPress, DVWA, Juice Shop, serveis estil GitLab i Workloads personalitzats.
- Suport de VM atacant per a generació de trànsit estil pentest.
- Captura d'artefactes de memòria i disc des del costat de l'host.
- Sidecars d'evidència: els manifestos i checksums SHA-256 haurien de ser estàndard en els artefactes de Job i Playground.
- Volatility 3 és el camí central d'anàlisi de V1 per a memòria de Linux, fent servir ISFs de Linux. Les bases de PDB/catàleg de Windows són experimentals i queden fora dels fluxos suportats d'1.0.0.
- **Capacitat**: diversos Jobs poden conservar targets vius i atacants opcionals dins dels límits agregats de RAM, disc i nombre de VM. L'únic worker de cicle de vida serialitza operacions; no limita el sistema a un únic Job viu. Un Playground ordinari és exclusiu amb Jobs i només admet una sessió activa. Les instàncies experimentals de Packages tenen el seu propi cicle de vida i queden fora de l’abast compatible de 1.0.0. Consulta [Configuració](../usage/configuration.md).
- Distribució des del checkout del codi font més `ops/deploy/install.sh`.

## Postura de seguretat

V1 assumeix un operador local de confiança i accés per localhost. Vincula a `127.0.0.1` per a ús normal. No exposis l'API/UI a LAN o Internet sense autenticació externa i controls de reverse proxy.

Els workloads guest són intencionadament vulnerables. L'enduriment de guest hostil hauria de poder seleccionar-se durant la creació del lab a mesura que els controls madurin; fes servir xarxes aïllades quan provis cadenes d'exploit.

## Full de ruta

- Paritat d'aprovisionament de targets Windows automatitzat.
- Telemetria en viu contínua més enllà de les captures discretes.
- Informes generats més complets, més enllà dels resums de Volatility i els informes de plugins ja publicats.
- Presets més rics d'enduriment de guest hostil.
- Suport de receptes YAML i una UX de creació de receptes més completa més enllà de la importació/exportació JSON actual.

## Fora d'abast per a V1

- Paquets de laboratori portàtils: desactivats, experimentals i sense acceptació fins que P1–P3 passin en un mateix candidat. Sense garanties del cicle complet, entre dos amfitrions, sense xarxa ni de rendiment.
- Autenticació multiusuari/RBAC dins d'AstinusLab.
- Desplegament multi-tenant allotjat al cloud.
- Reemplaçament de SIEM o plataforma de monitoratge sempre activa.

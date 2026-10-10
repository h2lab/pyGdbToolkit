<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# pyTestFarm

`pyTestFarm` est le client d'automatisation multi-cible de `pyGdbServer`.
Il remplace `pyGdbClient` pour les traitements automatises, sans tableau de
bord interactif. L'ancien executable reste disponible pour compatibilite.

## Principes

- Une cible associe un identifiant unique dans la ferme a une instance
  `pyGdbServer`, avec un role optionnel dans l'API Python ou le scenario.
- Chaque instance possede sa connexion WebSocket JSON-RPC independante.
  Les identifiants ne modifient pas la configuration des serveurs.
- Les connexions et les operations sur plusieurs cibles sont concurrentes.
  Les resultats sont indexes par identifiant, jamais par ordre d'arrivee.
- Une commande peut viser toutes les cibles ou une selection explicite.
  Plusieurs commandes CLI s'executent dans l'ordre : la commande suivante
  attend les reponses de toutes les cibles selectionnees.
- Si une connexion echoue, toutes les connexions ouvertes sont fermees avant
  d'executer une commande. Une erreur RPC ou un delai depasse identifie la cible
  concernee ; les resultats des autres cibles restent accessibles dans
  `FarmOperationError.results`.
- La fermeture de la ferme deconnecte les clients ; elle ne termine pas les
  serveurs, ne reinitialise pas les cibles et n'annule pas une commande deja
  transmise a GDB. Il n'y a pas de rollback des effets sur le materiel.

Un identifiant de ferme designe une session serveur, pas un coeur physique.
La selection d'un coeur au sein d'une session reste une operation du serveur
(`target.select_core` ou `dap core`). Les autres clients d'un meme serveur
partagent son etat de debug.

## Installation et CLI

Depuis la racine du depot, avec Python 3.12 ou plus :

```console
python -m pip install -e .
pyTestFarm --help
```

Demarrer les instances `pyGdbServer` separement, puis verifier leur etat :

```console
pyTestFarm \
  --target cpu_primary=192.168.1.42:1234 \
  --target cpu_secondary=192.168.1.42:1235
```

Executer les memes commandes sur les deux sessions :

```console
pyTestFarm \
  --target cpu_primary=192.168.1.42:1234 \
  --target cpu_secondary=192.168.1.42:1235 \
  --command "lscpu" \
  --command "gdb info registers"
```

Ajouter `--select cpu_primary` pour ne commander que cette cible ; l'option
est repetable. Toutes les instances declarees sont quand meme connectees.
`--timeout 60` configure le delai par cible en secondes. Pour les commandes,
ce delai est transmis a GDB, avec cinq secondes supplementaires pour recevoir
la reponse RPC. Le serveur limite son delai de commande a 300 secondes.

Les adresses acceptent `HOST:PORT`, `ws://HOST:PORT` et `wss://HOST:PORT`
(IPv6 : `[::1]:1234`). Les identifiants utilisent lettres ASCII, chiffres,
`_`, `-` et `.`, avec une lettre, un chiffre ou `_` en premier.

Sans `--command`, la CLI demande `server.status`. Les resultats JSON sont
ecrits sur stdout avec cette structure :

```json
{
  "cpu_primary": {
    "server": "ws://192.168.1.42:1234",
    "role": null,
    "results": [
      {"method": "server.status", "result": {"ready": true}}
    ]
  }
}
```

Les erreurs d'operation sont emises en JSON sur stderr avec `operation`,
`errors` par identifiant et `results` partiels de l'operation concernee.
La CLI s'arrete a la premiere operation en erreur : aucune commande suivante
n'est envoyee. Codes de sortie : `0` succes, `1` erreur d'operation, `2`
arguments invalides, `130` interruption utilisateur.
`python -m pyTestFarm` propose la meme interface.

## API Python

```python
import asyncio

from pyTestFarm import Target, TestFarm


async def analyse():
    targets = [
        Target("cpu_primary", "192.168.1.42:1234", role="controller"),
        Target("cpu_secondary", "192.168.1.42:1235", role="protocol_peer"),
    ]
    async with TestFarm(targets) as farm:
        status = await farm.request("server.status")
        registers = await farm.execute(
            "gdb info registers", targets=["cpu_primary"]
        )
        print(status, registers)


asyncio.run(analyse())
```

`request()` accepte les methodes et parametres JSON-RPC existants ;
`execute()` utilise `command.execute` (commandes toolkit, `gdb ...`,
`monitor ...`). Les notifications restent dans une file par cible :
apres `await farm.request("logs.subscribe")`, consommer
`await farm.notifications("cpu_primary").get()` dans sa propre tache.
Le mode commandes CLI ne surveille pas les notifications. Le transport existant conserve
au maximum 2000 notifications par cible ; ce n'est pas un stockage durable.

## Scenarios YAML et JSON

Le support initial couvre les actions specialisees et les commandes libres de
[l'exemple multi-cible](examples/multi_soc_protocol_resilience.yaml).
Adapter les adresses des serveurs et l'adresse d'execution avant de le lancer :

```console
pyTestFarm --scenario src/pyTestFarm/examples/multi_soc_protocol_resilience.yaml
```

`--scenario` ne se combine pas avec `--target`, `--select` ou `--command`.
Les cibles et leurs roles viennent du fichier. `--timeout` reste disponible
pour les connexions, requetes RPC et le delai par defaut de `run_until`.
Le fichier entier est valide **avant toute connexion** : champs inconnus,
identifiants dupliques, references invalides, actions sans commande inconnues, valeurs
negatives et cles YAML dupliquees sont refuses. Le chargement YAML est sur ;
les tags de construction d'objets Python ne sont pas autorises.

| Action | Parametres | Appels JSON-RPC et comportement |
| --- | --- | --- |
| `connect_all` | Aucun | Doit etre la premiere et unique etape de connexion. Ouvre les connexions en parallele puis demande `server.status`. |
| `reset_sequence` | `targets`, `delay_ms` (defaut : 0) | `command.execute` avec `monitor reset halt` sur toutes les cibles selectionnees **en parallele**, puis attend `delay_ms` apres les reponses de toutes les cibles. |
| `run_until` | `target`, `address`, `timeout_ms` (optionnel) | `command.execute` avec `gdb until *adresse &`, puis lit `logs.get` jusqu'a un evenement MI `*stopped` a l'adresse demandee. |
| `peer failure` ou `peer_failure` | `target`, `command: reset` | `command.execute` avec `monitor reset` sur ce pair uniquement. Les autres commandes d'injection ne sont pas encore supportees. |
| `wait_and_observe` | `duration_ms`, `monitor`, `poll_interval_ms` (defaut : 100) | Echantillonne `target.status` et collecte les nouveaux evenements via `logs.get`, separes par cible. |
| Libelle libre, par exemple `show cpustate` | `targets`, `command` | Transmet la commande telle quelle via `command.execute` en parallele aux cibles selectionnees. |

Pour les commandes libres, la CLI affiche les resultats sur stdout apres chaque
etape, avec le prefixe `[identifiant]` sur chaque ligne, puis le rapport JSON
final. Par exemple :

```text
[stm32n657] show cpustate: lscpu
[stm32n657] ... resultat de lscpu ...
[stm32u5a5] show cpustate: lscpu
[stm32u5a5] ... resultat de lscpu ...
```

Les sorties des cibles ayant reussi sont aussi affichees si une autre cible
echoue. Le mode commandes CLI existant (`--command`) conserve sa sortie JSON.
Les commandes du scenario ne sont ni corrigees ni interpretees localement :
une commande inconnue fait echouer l'etape avec l'erreur du serveur.

Les durees sont des entiers en millisecondes ; `timeout_ms` et
`poll_interval_ms` doivent etre strictement positifs. Une duree d'observation
de zero produit un echantillon immediat. `address` accepte un entier ou une
chaine hexadecimale, entre 0 et `0xffffffffffffffff`.

`run_until` ne confond pas l'acquittement avec l'arrivee a l'adresse. Il
verifie un nouvel evenement d'arret (`location-reached` ou `breakpoint-hit`)
et l'adresse de la frame GDB. Un autre point d'arret, un signal, une sortie
du programme ou un timeout fait echouer l'etape. Les evenements historiques
sont exclus avec un curseur de sequence etabli avant la commande.
L'execution en arriere-plan doit etre supportee par GDB et sa cible.
`until` peut egalement s'arreter si la fonction courante se termine :
si l'adresse demandee n'est pas atteinte, cela reste un echec.

L'observation retourne `samples` (temps ecoule en ms et etats par cible) et
`events` (listes de logs par cible). Les appels reseau peuvent allonger la
duree reelle au-dela de `duration_ms` ; l'echantillonnage n'est pas temps reel.
Une interruption des sequences de logs est signalee comme une erreur :
le serveur ne garde que 10000 evenements recents accessibles par `logs.get`.
Les etats d'execution indisponibles ou les erreurs RPC interrompent egalement
le scenario.

Le rapport JSON contient `scenario`, `targets` (serveur et role) et `steps`
(index a partir de 1, action canonique et resultat). Sur erreur, stderr
contient aussi `step`, `action` et `report` avec les etapes deja terminees,
en plus des erreurs et resultats partiels de l'operation en echec.
Les codes de sortie sont les memes que pour le mode commandes.

API Python :

```python
import asyncio
from pathlib import Path

from pyTestFarm import load_scenario, run_scenario

scenario = load_scenario(Path("scenario.yaml"))
report = asyncio.run(run_scenario(scenario, timeout=30))
```

## Limites et tests

Cette version n'ajoute pas d'assertions metier, boucles, conditions ou
reconnexion automatique. Elle ne supervise pas les processus serveurs.
Les resets sont propres au backend OCD configure ; notamment, le reset
d'un coeur peut affecter d'autres coeurs du meme SoC. Verifier ce comportement
sur le materiel avant l'execution du scenario.

Le scenario ne restaure pas l'etat du materiel : meme sur erreur ou timeout,
une cible reprise peut continuer a executer, et les effets des commandes
deja envoyees persistent. Fermer les connexions n'est pas une annulation
de l'execution. Apres `run_until`, la cible reste arretee a l'adresse
demandee ; aucune reprise implicite n'est faite.

Les tests de base utilisent des serveurs WebSocket locaux sans GDB ni sonde :

```console
python -m pytest tests/test_testfarm.py tests/test_scenarios.py
```

Pour une utilisation distante, proteger l'API par un reseau de confiance ou
des controles TLS et d'authentification. La ferme n'ajoute ni authentification
ni supervision des processus serveurs.

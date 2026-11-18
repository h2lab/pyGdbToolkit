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
  `pyGdbServer`, avec un role optionnel dans l'API Python.
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
La CLI ne surveille pas les notifications. Le transport existant conserve
au maximum 2000 notifications par cible ; ce n'est pas un stockage durable.

## Scenarios futurs et limites actuelles

Cette version fournit uniquement les fondations de connexion et de routage.
Elle ne lit **aucun fichier de scenario**, JSON ou YAML, et n'interprete pas
encore `connect_all`, `reset_sequence`, `run_until`, `peer failure` ou
`wait_and_observe` comme etapes de scenario.

Une future couche de scenario pourra mapper `targets[].id`, `server` et
`role` vers `Target`, puis ordonnancer les etapes via cette API. Les commandes
sont concurrentes, mais ne garantissent pas une synchronisation temps reel.
Une reponse a `gdb continue` ne signifie pas que la cible a atteint un point
d'arret : une future action `run_until` devra observer les evenements.

Les tests de base utilisent des serveurs WebSocket locaux sans GDB ni sonde :

```console
python -m pytest tests/test_testfarm.py
```

Pour une utilisation distante, proteger l'API par un reseau de confiance ou
des controles TLS et d'authentification. La ferme n'ajoute ni authentification
ni supervision des processus serveurs.

# Piloter Air-Construct avec Claude (MCP)

Claude peut utiliser Air-Construct à votre place, **dans la fenêtre que vous avez
ouverte** : il regarde la vue 3D, construit le modèle, règle l'air et le maillage,
lance le calcul et lit les résultats. Vous voyez chaque action se faire.

## Ce que vous voyez pendant que Claude travaille

- La pastille **« Claude »** en haut à droite apparaît quand Claude se connecte. Pendant
  une action elle passe en bleu et dit ce qu'il fait (« Claude · Ajoute la pièce
  « dcdc » »). Un clic dessus montre ses dernières actions et permet de le **mettre en
  pause** : il peut encore regarder, plus rien modifier, jusqu'à « Reprendre ».
- L'onglet concerné s'ouvre et l'objet touché est sélectionné et cadré dans la vue 3D.
- La barre d'état affiche chaque action, et chaque modification est une étape
  d'annulation nommée « Claude · … » : **Ctrl+Z** défait ce que Claude a fait.
- Claude ne ferme pas un projet modifié sans votre accord et ne modifie rien pendant
  un calcul (le modèle est verrouillé, comme pour vous).
- **Fichier > Autoriser le pilotage par Claude** coupe ou rétablit l'accès (activé
  par défaut). Seuls les programmes de votre session peuvent se connecter : le serveur
  n'écoute que sur 127.0.0.1 et demande un jeton écrit dans votre profil
  (`~/.airconstruct/bridge.json`).

## Installation

Le serveur MCP est livré avec l'application :

- application installée (Windows) : `Air-Construct-MCP.exe`, à côté de
  `Air-Construct.exe` (par défaut
  `%LOCALAPPDATA%\Programs\Air-Construct\Air-Construct-MCP.exe`) ;
- depuis les sources : la commande `airconstruct-mcp` (`pip install -e ".[ui]"`).

Si Air-Construct n'est pas ouvert, la première action de Claude le démarre.

### Claude Desktop

Paramètres > Développeur > Modifier la configuration, puis dans
`claude_desktop_config.json` :

```json
{
  "mcpServers": {
    "air-construct": {
      "command": "C:\\Users\\<vous>\\AppData\\Local\\Programs\\Air-Construct\\Air-Construct-MCP.exe"
    }
  }
}
```

(depuis les sources : `"command": "airconstruct-mcp"`). Redémarrez Claude Desktop :
les outils « air-construct » apparaissent dans le menu des connecteurs.

### Claude Code

```bash
claude mcp add air-construct -- "C:\Users\<vous>\AppData\Local\Programs\Air-Construct\Air-Construct-MCP.exe"
# ou, depuis les sources :
claude mcp add air-construct -- airconstruct-mcp
```

## Les outils

| Pour | Outils |
|---|---|
| Regarder | `get_state` (onglet, sélection, alertes, calcul), `screenshot` (fenêtre ou vue 3D, onglet et caméra au choix), `get_model` (le cas complet), `set_view` (onglet, caméra, sélection, champ T/U) |
| Fichiers | `new_project` (modèles : vide, carte, rack 1U, alimentation), `open_project`, `save_project`, `import_cad` (STEP/STL) |
| Modéliser | `add_part`, `update_part`, `add_material`, `delete`, `undo`, `redo` |
| Configurer | `add_opening`, `update_opening` (débit, vitesse, ventilateur, grille), `update_case` (enceinte, ambiance, parois, précision du maillage, solveur), `add_refinement`, `add_probe` |
| Simuler | `quick_estimate` (estimation analytique en une seconde), `run_simulation` (ou reprise), `simulation_status` (attend la fin si demandé), `stop_simulation` |
| Résultats | `get_results` (températures par pièce et limites, air, pertes de charge, bilan d'énergie, ventilateurs, sondes), `screenshot(tab="results")` |

Les valeurs sont en unités SI (mètres, °C, W, m³/s), comme dans le fichier du cas,
quelles que soient les unités d'affichage choisies dans la barre d'état.

Exemples de demandes : « Ajoute un régulateur de 5 W à côté du CPU et dis-moi s'il
reste sous 100 °C », « Essaie un ventilateur de 20 m³/h en entrée à la place du débit
imposé, compare avec l'estimation rapide, puis lance le calcul », « Montre-moi le champ
de vitesse ».

## Fonctionnement (développeurs)

```
Claude ──stdio/MCP──> airconstruct-mcp ──TCP 127.0.0.1 + jeton──> pont dans la fenêtre
                    (assistant/mcp_server.py)                   (ui/assistant/bridge.py)
```

- `airconstruct/assistant/protocol.py` : format des messages (une ligne JSON par
  requête), fichier de liaison (port, jeton), client.
- `airconstruct/assistant/mcp_server.py` : serveur MCP minimal sans dépendance
  (initialize, tools/list, tools/call, ping), définitions des outils, lancement de
  l'application, attente de fin de calcul.
- `airconstruct/ui/assistant/` : `bridge.py` (QTcpServer dans le fil de l'interface,
  pause, historique), `commands.py` (une méthode par commande, passant par
  `project.set_data` et les fonctions des onglets), `indicator.py` (la pastille).
- Ajouter un outil : une méthode et une entrée dans `Commands.table`, puis sa
  définition dans `TOOLS` (même nom). `tests/test_assistant.py` vérifie que chaque
  outil a sa commande.

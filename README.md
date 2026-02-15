# LedFx -> Corsair iCUE UDP Bridge

Petit bridge Windows qui recoit des paquets UDP de LedFx et envoie les couleurs vers les ventilateurs Corsair via le SDK iCUE.

## Pre-requis
- Windows + iCUE installe
- SDK iCUE active dans les options iCUE (Settings > Software and Games > Enable SDK)
- Python 3.10 installe

## Installation
1. Installer les dependances
```
py -3 -m pip install -U -r requirements.txt
```

2. Creer la config
```
copy config.example.json config.json
```

3. Verifier la detection
```
py -3 ledfx_icue_bridge.py --list-devices
```

## Lancer
```
py -3 ledfx_icue_bridge.py
```

Au demarrage, le script demande le mode:
- **unique**: chaque groupe/appareil sur son port (config `groups`)
- **groupe**: tous les appareils ensemble sur le port `34983`
- **fusion**: tous les peripheriques en un seul flux, ordre fixe (clavier -> tapis -> souris -> ventilos -> CPU cooler -> RAM -> ventilos AIO)

Tu peux forcer:
```
py -3 ledfx_icue_bridge.py --mode unique
py -3 ledfx_icue_bridge.py --mode group --group-port 34983
py -3 ledfx_icue_bridge.py --mode fusion --fusion-port 34984
```

Pendant l execution, appuie sur `M` pour revenir au choix de mode.

## Test iCUE (sans LedFx)
```
py -3 ledfx_icue_bridge.py --test
```
Ou une couleur:
```
py -3 ledfx_icue_bridge.py --test-color 0,255,0
```

## Debug ventilos (1 LED blanche)
Pour identifier l ordre des ventilos:
```
py -3 ledfx_icue_bridge.py --mode unique --fan-sweep --fan-index 1
```
Tu peux changer `--fan-index` (1..N) et la vitesse avec `--fan-speed 0.08`.

Pour allumer seulement certains ventilos:
```
py -3 ledfx_icue_bridge.py --mode unique --fan-on 1,2 --fan-color 255,255,255
```

## Groupes (un port par appareil)
Tu peux mapper chaque appareil Corsair sur un port UDP different (LedFx -> sorties independantes).
Dans `config.json`, ajoute `groups`:
```
"groups": [
  {"name":"clavier","udp_port":6477,"device_types_include":["CDT_Keyboard"],"model_contains":["K95 RGB PLATINUM"]},
  {"name":"ram","udp_port":7934,"device_types_include":["CDT_MemoryModule"],"model_contains":["VENGEANCE RGB PRO"]},
  {"name":"aio","udp_port":8758,"device_types_include":["CDT_Cooler"],"model_contains":["H115i PLATINUM"]},
  {"name":"souris_tapis","udp_port":9999,"device_types_include":["CDT_Mouse","CDT_Mousemat"],"model_contains":["M55 RGB PRO","MM800 RGB POLARIS"],"link_mouse_to_mousemat_center":true},
  {"name":"ventilos","udp_port":7999,"device_types_include":["CDT_LedController","CDT_Fan"],"model_contains":["LIGHTING NODE PRO"]}
]
```
Astuce: `model_contains` est un filtre partiel (case-insensitive).
Si besoin, utilise `device_ids` (copie depuis `--list-devices`).
Si tu utilises `groups`, laisse `device_types_include` vide au niveau global.

## Mode Fusion (ordre fixe)
Le mode fusion met tous les peripheriques sur un seul port UDP (defaut `34984`),
dans cet ordre:
1. Clavier
2. Tapis de souris
3. Souris
4. Ventilos boitier
5. CPU cooler (pompe)
6. RAM
7. Ventilos du AIO

Le mode fusion utilise les filtres de `groups` si presents (ex: `clavier`, `souris_tapis`,
`ventilos`, `aio`, `ram`). Sinon, il se base sur les types d appareils.
Pour la RAM en mode fusion:
- `fusion_ram_mode: "sticks"` (defaut) = barrettes l une apres l autre, gauche -> droite
- `fusion_ram_mode: "rows"` = intercale par rangs (colonne par colonne)
- `fusion_ram_mirror: true` = inverse un barrettes sur deux (effet miroir, utile en mode `rows`)
- `fusion_ram_led_axis: "x"` = force l ordre interne des LEDs par axe horizontal (gauche -> droite)

Pour inserer le CPU cooler au milieu des ventilos boitier:
- `fusion_cpu_after_fan: 2` = insere le CPU cooler apres le ventilo 2 (avant le 3)

## Clavier en serpentin (matrice)
Pour envoyer le clavier en ordre serpentin (ligne 1 gauche->droite, ligne 2 droite->gauche):
```
"keyboard_serpentine": true,
"keyboard_serpentine_row_tolerance": null,
"keyboard_serpentine_first_dir": "left",
"keyboard_serpentine_row_order": "top",
"keyboard_serpentine_rows": 6,
"keyboard_serpentine_flip_x": false,
"keyboard_serpentine_flip_y": false,
"keyboard_serpentine_swap_xy": false,
"keyboard_serpentine_mode": "serpentine"
```
`row_tolerance` peut etre ajuste si les lignes se melangent (ex: `5.0`).
`row_order` controle si la premiere ligne est en haut (`top`) ou en bas (`bottom`).
Si les LEDs sont eparpillees, fixe `keyboard_serpentine_rows` (ex: `6`), sinon laisse `null` pour auto.
Si le depart est a droite au lieu de gauche, mets `keyboard_serpentine_flip_x: true`.
Si le sens est haut->bas au lieu de gauche->droite, mets `keyboard_serpentine_swap_xy: true`.
Si le rendu est en diagonale, remets `keyboard_serpentine_swap_xy: false` et ajuste `keyboard_serpentine_rows`.
Si tu veux gauche->droite a chaque ligne (pas en serpentin), mets `keyboard_serpentine_mode: "linear"`.
Le script utilise les coordonnees du SDK (`cx`, `cy`) pour ordonner les touches.

## RAM en matrice
Par defaut, les RAM sont ordonnees par module de gauche a droite (`device_sort: "x"`)
et chaque module est mappe en mode `linear` (haut/bas):
```
"ram_serpentine": true,
"ram_serpentine_row_order": "bottom",
"ram_serpentine_mode": "linear"
```
Pour synchroniser les barrettes, ajoute dans le groupe RAM:
```
"update_mode": "buffer_safe"
```
Si besoin:
- `ram_serpentine_swap_xy: true` pour inverser l axe
- `ram_serpentine_flip_y: true` pour inverser haut/bas
- `ram_order_axis: "x"` pour forcer un ordre gauche -> droite (mode 1 et 2)
- `ram_match_group_order: true` pour forcer le meme ordre RAM en mode 1/3 que le mode 2
- `ram_group_layout: "rows"` pour intercaler les LEDs des barrettes gauche -> droite

## Ventilos en cercle
Pour des ventilateurs 12 LEDs externes + 4 internes (16 par ventilo):
```
"fan_ring": true,
"fan_outer_leds": 12,
"fan_inner_leds": 4,
"fan_count": 3,
"fan_start": "top",
"fan_direction": "clockwise",
"fan_group_sort": "x",
"fan_layout": "sequential",
"fan_ring_order": "index"
```
Options utiles:
- `fan_inner_first: true` si tu veux d abord le centre
- `fan_flip_x` / `fan_flip_y` si le sens est inverse
- `fan_swap_xy` si le cercle tourne dans le mauvais axe
- `fan_layout: "cluster"` si tu veux regrouper les LEDs par position (sinon `sequential`)
- `fan_group_order: [1,3,2]` pour forcer l ordre des ventilos (1-based)
- `fan_ring_order: "index"` garde l ordre d origine (utile si les LEDs partent dans tous les sens)
- `fan_lock_to_first: true` applique le meme ordre que le ventilo 1 sur les autres

## AIO (2 ventilos + pompe)
Pour un AIO type H115i (2 ventilos + pompe), le script peut clusteriser en 3 groupes
et ordonner chaque groupe en cercle:
```
"aio_cluster": true,
"aio_cluster_count": 3,
"aio_cluster_sort": "x",
"aio_angle_start": "top",
"aio_angle_direction": "clockwise",
"aio_pump_angle_start": "left",
"aio_pump_split": true,
"aio_pump_first": true
```
Si tu veux que la pompe parte de la gauche et allume haut/bas en meme temps vers la droite,
garde `aio_pump_split: true`.
Balance des blancs pour la pompe uniquement:
```
"aio_pump_white_balance": [255, 240, 220]
```
Si l ordre n est pas bon, force le regroupement avec:
```
"aio_cluster_order": [1,3,2]
```
et inverse avec `aio_angle_direction: "counter"` ou `aio_flip_x` / `aio_flip_y`.

## Tapis de souris (gauche -> droite)
```
"mousemat_serpentine": true,
"mousemat_serpentine_first_dir": "left",
"mousemat_serpentine_rows": 1,
"mousemat_serpentine_mode": "linear",
"mousemat_order_mode": "angle",
"mousemat_angle_start": "left",
"mousemat_angle_direction": "clockwise",
"mousemat_reverse": false
```
Si la fin du tapis est inverse, garde `mousemat_order_mode: "angle"` et mets `mousemat_reverse: true`
ou change `mousemat_angle_direction` en `"counter"`.
Pour que la souris reproduise la LED du milieu du tapis, ajoute dans le groupe:
```
"link_mouse_to_mousemat_center": true
```

Lister les groupes detectes:
```
py -3 ledfx_icue_bridge.py --list-groups
```

## Config LedFx
Choisir une sortie UDP dans LedFx et pointer vers l IP de ce PC.

Options conseillees:
- **UDP WLED / DRGB**: utiliser un data prefix `02` (ou `0201`)
- **DDP** (optionnel): port 4048 (ou le port de `config.json`)

Le bridge accepte les protocoles suivants:
- `ddp`
- `wled` ou `drgb` (WLED realtime UDP / DRGB)
- `raw` (RGB brut)
- `auto`

Pour LedFx en UDP DRGB, mets `protocol` sur `drgb`.

## Notes
- Auto-detection: le bridge recupere tous les LEDs exposes par iCUE. Si d autres peripheriques sont affectes, filtre par type via `device_type_mask` (ex: `CDT_LedController|CDT_Cooler`).
- `brightness` et `gamma` dans la config permettent d ajuster l intensite.
- Nouveau: tu peux filtrer les types exacts avec `device_types_include` et `device_types_exclude`.
  Exemple pour ne garder que les ventilateurs: `["CDT_LedController","CDT_Cooler","CDT_Fan"]`.
- Watchdog iCUE: si iCUE se deconnecte apres un long moment, le bridge tente de se reconnecter automatiquement.
- Logs: un fichier `ledfx_icue_bridge.log` est cree pour comprendre les blocages (configurable via `log_file` / `log_level`).
- Keepalive iCUE: re-demande le controle et re-applique le dernier frame pour eviter les gels (configurable via `icue_keepalive*`).
- Anti-faux positifs: `icue_watchdog_fail_threshold`, `icue_reconnect_cooldown`, `icue_watchdog_idle_only`.
  - `icue_keepalive_request_always: true` force un request_control periodique (desactive par defaut).
  - `keepalive_reapply: true` dans un groupe pour garder la derniere couleur quand LedFx n envoie rien.
  - `icue_skip_reconnect_when_idle: true` evite les reconnexions quand aucun UDP n est recu.

## Depannage
Si vous voyez `CE_NotConnected`:
- Verifie que iCUE est lance.
- Active le SDK dans iCUE (Settings > Software and Games > Enable SDK).
- Lance le script avec les memes droits que iCUE (admin ou non).

## Exemple config.json
```
{
  "udp_host": "0.0.0.0",
  "udp_port": 21324,
  "protocol": "drgb",
  "max_fps": 60,
  "brightness": 1.0,
  "gamma": 1.0,
  "device_type_mask": "CDT_All",
  "device_types_include": ["CDT_LedController","CDT_Cooler","CDT_Fan"],
  "device_types_exclude": [],
  "clear_on_start": true
}
```

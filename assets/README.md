# assets/ — kawkab card templates

El bot yelawwej 3ala ez-zouz fichiers hedhouma bech ya3mel bta9et el mouwatana:

| Fichier | Chnowa houwa |
|---|---|
| `kawkab_card_male.png` | El card el **bleu** (bla taswira w bla isem) |
| `kawkab_card_female.png` | El card el **rose** (bla taswira w bla isem) |
| `kawkab_font.ttf` | *(optionnel)* font bech yektbou biha l-isem |

## Kifech tzid-hom

1. Sajjel ez-zouz tsawer b hedhouma el asemi bel dhabt, houni fi `assets/`.
2. `git add assets/ && git commit && git push` — lazem ykounou fi repo bech
   Render yelqahom wa9t el deploy.
3. Fi Discord: `?kawkabcard male` walla `?kawkabcard female` bech tchouf preview.

> ⚠️ Links kima `https://imgur.com/a/xxxxx` houma **page**, mch taswira.
> Ken t7eb URL 3ivadh el fichier, lazem ykoun direct link
> (`https://i.imgur.com/xxxxx.png`) w t7ottou fi `.env`:
> `KAWKAB_CARD_MALE_URL=...` / `KAWKAB_CARD_FEMALE_URL=...`

## Bech tbaddel blaset el taswira wala l-isem

El blayes mawjoudin fi `config.py` → `KAWKAB_CARD_LAYOUT`, ma3doudin b nisba
(0.0 – 1.0) mel 3ordh w toul el template:

```python
"male": {
    "avatar_box": (left, top, right, bottom),   # el cadre mte3 et-taswira
    "name_box":   (left, top, right, bottom),   # el plaque mte3 l-isem
    "name_color": (R, G, B),
},
```

Baddel el ar9am, ba3d a3mel `?kawkabcard male` — el cache yetnahha wa7dou
w tchouf el natija el jdida deghya.

# ============================================================
# CONFIG — hedhi el fichier eli 3andek te3addel fih el channels
# w el kalmet eli te7eb el bot yefsa5hom.
# ============================================================

import re

# IDs mte3 el channels eli el bot ye5dem fihom (raqabat).
WATCHED_CHANNELS = {
    1518043545712463902,  # chat support
    1517949169132769452,  # general
    1518560304903094272,  # memes
    1511674200543199334,  # clips and highlights
    1524882989312512262,  # giveaway feedback
    1524463663447146727,  # event chat
    1518395048582971474,  # team up ping
    1517597975856025854,  # chat of verify
}

# Regex eli te3raf ay link (http/https/discord invite/www.)
LINK_REGEX = re.compile(
    r"(https?://\S+)|(www\.\S+)|(discord\.gg/\S+)", re.IGNORECASE
)

# Liste kalmet mamnou3a — zid/na9ess kif te7eb.
# Ken kelma feha capital wala small mafamech far9 (comparaison lower()).
# Liste standard (Anglais + 3arbi/Franco) — zid/7i kif te7eb.
BANNED_WORDS = [
    # --- Anglais ---
    "fuck", "fucking", "fucker", "motherfucker", "shit", "bullshit",
    "bitch", "asshole", "bastard", "cunt", "dick", "pussy", "whore",
    "slut", "nigger", "nigga", "faggot", "retard", "rape",

    # --- 3arbi/Franco (Tounsi) ---
    "nik", "nik omk", "nik oukhtk", "9a7ba",
    "zebi", "zeb", "kess", "3ahra", "sharmouta", "sharmuta", "mnayek",
    "manyouk", "kahba",

    # --- 3arbi (script 3arbi) ---
    "قحبة", "شرموطة", "منيوك", "زبي", "عرص",
    "كس اختك", "نيك امك",
]

# Rôles eli ma yet7a5ouch el messages mte3hom (ex: admins/mods).
# 7ott hna IDs mte3 el rôles eli te7eb tel3afihom (exempt).
EXEMPT_ROLE_IDS = {
    # ROLE_ID_HERE,
}

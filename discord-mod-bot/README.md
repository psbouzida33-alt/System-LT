# Discord Mod Bot (Python)

Bot ye7ares 3la channels me7ddin, yefsa5 ay **link** wala **kelma mamnou3a**, w yeb3athlek **report fi DM** (chkoun sifet, fi ay channel, w chnowa el message).

## 1) Installation

```bash
cd discord-mod-bot
pip install -r requirements.txt
```

## 2) Config el .env

Copy `.env.example` l `.env` w 3amru:

```bash
cp .env.example .env
```

- **DISCORD_TOKEN**: Token mte3 el bot (men [Discord Developer Portal](https://discord.com/developers/applications) → Application-ek → Bot → Reset Token).
- **OWNER_ID**: ID mte3ek (Discord Settings → Advanced → Developer Mode ON, mba3d right-click 3la profile-ek → Copy User ID).

⚠️ **Ma tzidech `.env` fi ay repo public** — fih el token mte3ek, ken 7ad 3rafou y9dar yst3mel el bot mte3ek kifek.

## 3) Activi el Intents fi Developer Portal

Fi [Developer Portal](https://discord.com/developers/applications) → Application-ek → **Bot**, activi:
- `MESSAGE CONTENT INTENT`
- `SERVER MEMBERS INTENT`

## 4) Invite el bot lel server

Men Developer Portal → **OAuth2 → URL Generator**:
- Scopes: `bot`
- Permissions: `Manage Messages`, `Read Message History`, `View Channels`, `Send Messages`

Copy el link eli yet7awel w 7ellou fi browser bech tzid el bot fi server-ek.

## 5) Config el channels/kalmet (`config.py`)

- `WATCHED_CHANNELS`: déja 3amrethom bel IDs eli 3tetni (chat support, general, memes, clips, giveaway feedback, event chat, team up ping, chat of verify).
- `BANNED_WORDS`: liste vide default — zid fiha el kalmet eli te7eb el bot yefsa5hom (ex: `["kelma1", "kelma2"]`).
- `EXEMPT_ROLE_IDS`: ken 3andek rôle (mods/admins) ma te7ebch el bot ye7assbou, 7ott ID mte3ou hna.

## 6) Yesha8el

```bash
python bot.py
```

## Note

- El bot lezmou **role fo9 el members el 3adiyin** fi hierarchy mte3 el server bech y9dar yefsa5 messages.
- Bech el bot y9dar yeb3athlek DM, lezem tkoun **member fi nafss el server** eli fih el bot (DM privacy settings).

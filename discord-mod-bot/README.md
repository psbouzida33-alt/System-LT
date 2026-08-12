# Discord Mod Bot (Python)

Bot ye7ares 3la channels me7ddin, ye7ass **biss** b members eli 3andhom wa7ed
mel rôles staff (Head Admin/Moderator/Staff Team/Trial Staff), yefsa5 ay
**link** wala **kelma mamnou3a** eli yekteb, w yeb3athlek **report fi DM**
(chkoun sifet, fi ay channel, w chnowa el message) — el DM yemchi l **kol
member 3andou wa7ed mel rôles staff**, mch l chakhs wa7ed.

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

⚠️ **Ma tzidech `.env` fi ay repo public** — fih el token mte3ek, ken 7ad 3rafou y9dar yst3mel el bot mte3ek kifek.

(Machich lezmek `OWNER_ID` fi `.env` — el DM recipients hardcoded fi `config.py` bel role IDs.)

## 3) Activi el Intents fi Developer Portal

Fi [Developer Portal](https://discord.com/developers/applications) → Application-ek → **Bot**, activi:
- `MESSAGE CONTENT INTENT`
- `SERVER MEMBERS INTENT`

## 4) Invite el bot lel server

Men Developer Portal → **OAuth2 → URL Generator**:
- Scopes: `bot`
- Permissions: `Manage Messages`, `Read Message History`, `View Channels`, `Send Messages`

Copy el link eli yet7awel w 7ellou fi browser bech tzid el bot fi server-ek.

## 5) Config (`config.py`)

- `WATCHED_CHANNELS`: el 8 channels eli el bot ye7ares 3lihom (chat support, general, memes, clips, giveaway feedback, event chat, team up ping, chat of verify).
- `BANNED_WORDS`: liste kalmet (Anglais + 3arbi/Franco) — zid/na9ess kif te7eb.
- `MONITORED_ROLE_IDS`: el bot ye7ass **biss** b members 3andhom wa7ed mel had el rôles (Head Admin/Moderator/Staff Team/Trial Staff) — members el 3adiyin mahich mora9bin.
- `REPORT_ROLE_IDS`: kol member 3andou wa7ed mel had el rôles yewsellou el DM report (el author mahouch yewsellou report 3la rou7ou).

## 6) Yesha8el

```bash
python bot.py
```

## Deploy 3al Render (24/7, minghir PC 7ay)

1. Push el code l GitHub (déja mawjoud fi repo `System-LT`).
2. Fi [Render Dashboard](https://dashboard.render.com) → **New → Blueprint** → 5tar el repo `System-LT` → Render ye9ra `render.yaml` mel root w yel9a service `discord-mod-bot` automatique.
3. Fi service `discord-mod-bot` → **Environment** → zid:
   - `DISCORD_TOKEN` = token mte3 el bot **el jdid** (application mnfassla fi Developer Portal — **mch nafss token System LT**)
4. Deploy. El bot ykoun online 24/7 (free plan Render yospindown b3ad 15 min minghir traffic HTTP — ken t7eb yeb9a always-on, zid uptime pinger kif [UptimeRobot](https://uptimerobot.com) 3al URL mte3 el service, wala upgrade el plan).

# SYKE

A Discord bot that reads the channels your admins pick and writes a brutally honest
(and scientifically questionable) personality report on any member: hard stats, a
trait breakdown, a hall of fame of their worst messages, an AI-written roast,
achievements, and how they compare to the rest of the server.

`!profile` replies with a three-page card. Buttons switch between pages, and the
card's colour matches the member's strongest trait:

| Page | What's on it |
| --- | --- |
| 📊 **Overview** | Archetype (e.g. *The Class Clown*), the roast summary, six stat tiles (messages, active since, peak hours, average length, top emojis, catchphrase) and the personality bars |
| 🏆 **Highlights** | Funniest, most unhinged, freakiest and most toxic messages, quoted |
| 🥇 **Ranks** | Achievements and "Top X%" against everyone else judged in the server |

Only the person who ran the command can flip pages; anyone else who clicks gets a
private copy of that page. Buttons stop working after 10 minutes.

Run `python -m syke.preview` to see a report from a fake server in your terminal.

## Commands

Every command works with the server prefix (default `!`) **and** as a slash command
(`!profile` or `/profile`). Mentioning the bot (`@SYKE profile`) always works, so a
forgotten prefix is never a problem. Admins can also switch a server to mentions only
with `!prefix mention`.

| Command | Who | What it does |
| --- | --- | --- |
| `!profile [@member]` | everyone | Full personality report (yourself if no member). Aliases: `!judge`, `!p`, `!me` |
| `!top [trait]` | everyone | Leaderboard for `funny`, `toxic`, `cringe`, `freaky`, `serious`, `chaotic` or `active`. Aliases: `!leaderboard`, `!lb` |
| `!optout` / `!optin` | everyone | Exclude yourself from being read or judged |
| `!help` | everyone | Command list using this server's prefix |
| `!track #channel` / `!untrack #channel` | Manage Server | Choose which channels SYKE reads |
| `!channels` | Manage Server | Watched channels, prefix, timezone and demo mode. Alias: `!settings` |
| `!prefix <new\|mention\|reset>` | Manage Server | Custom prefix (1-5 characters), `mention` so SYKE only answers to `@SYKE profile`, or `reset` for `!`. `!prefix` alone shows the current setting |
| `!timezone <zone>` | Manage Server | Timezone for "most active" hours, e.g. `Europe/London`. Alias: `!tz` |
| `!rescan` | Manage Server | Drop the cache and re-read channels now |
| `!demo on\|off` | Manage Server | Add 5 fake members to rankings so you can test alone |
| `!sample [name]` | Manage Server | Full report for a fake member (Zyro, Mira, bubbles, Dex, Vex) |

## Testing on your own

1. `!demo on` adds five fake members (Zyro, Mira, bubbles, Dex, Vex) to this server's
   comparisons and leaderboards. Reports and leaderboards say so in the footer.
2. `!sample zyro` shows a full report instantly, with no messages needed.
   It also exercises your OpenAI key if one is set.
3. To judge yourself, send at least `SYKE_MIN_MESSAGES` messages (default 15) in a watched channel,
   then run `!rescan` and `!profile`. You'll be ranked against the fake members.
   For quicker tests, set `SYKE_MIN_MESSAGES=5` in Railway's Variables.
4. Turn it off with `!demo off` before real members start using SYKE.

## How it works

1. **Collect**: when someone asks for a report, SYKE reads the last `SYKE_SCAN_LIMIT`
   messages from each watched channel (bots and opted-out users are skipped). The scan
   is cached in memory for `SYKE_CACHE_MINUTES`. Message content is never written to disk;
   the SQLite file only holds watched channels, opt-outs, prefixes, timezones and demo mode.
2. **Hard stats** (`syke/stats.py`): message count, first seen, busiest 3-hour window,
   average words, top emojis (including custom ones), and catchphrases (repeated 2-3 word phrases).
3. **Traits** (`syke/traits.py`): every message gets scored by word lists in
   `syke/lexicon.py` plus signals like caps lock, `!!!`, keyboard smashes, late-night posting,
   rapid-fire bursts and 😂/💀 reactions from *other* people. Scores are 0-100.
   Because this is local and deterministic, SYKE scores everyone and computes the
   "Top X%" server comparison and leaderboards without any AI calls.
4. **Roast** (`syke/ai.py`): the stats, scores and ~250 recent messages go to an LLM
   (OpenAI or Anthropic), which writes the profile summary, can pick better highlight
   messages, and awards a bonus achievement. The prompt forbids attacking identity
   (race, gender, sexuality, disability, and so on). **No API key? No problem**: SYKE falls back to a
   built-in offline roaster, so the bot works fully without one.
5. **Render** (`syke/render.py`): everything becomes a 34-column monospace card in an
   embed, sized to read well on mobile.

Members need `SYKE_MIN_MESSAGES` (default 15) messages in watched channels before they can be judged.

## Setup

### 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> and create an app named **SYKE**.
2. **Bot** tab: copy the token. Under *Privileged Gateway Intents*, enable
   **Message Content Intent** (SYKE can't read messages without it).
3. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`; permissions
   **View Channels**, **Read Message History**, **Send Messages**, **Embed Links**.
   Open the generated URL to invite SYKE to your server.

### 2. Run it

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then paste DISCORD_TOKEN (and optionally an AI key)
python -m syke
```

Set `SYKE_DEV_GUILD_ID` to your server's ID while developing so slash commands appear
instantly (global commands can take a while to propagate).

### 3. In Discord

```
!track #general
!timezone Europe/London
!profile @someone
```

## Deploying to Railway

SYKE is a background worker (no web port), and `railway.json` already sets the start
command and restart policy.

1. Push this repo to GitHub, then in Railway: **New Project → Deploy from GitHub repo**.
2. **Add a volume** to the service (right-click the service → *Attach volume*) mounted at `/data`.
   Without it, the SQLite file is wiped on every redeploy and admins lose their channel setup.
3. In the service's **Variables** tab, set:
   - `DISCORD_TOKEN` = your bot token
   - `SYKE_DB_PATH` = `/data/syke.db`
   - `OPENAI_API_KEY` = your key (optional; omit to use the offline roaster)
   - `SYKE_DEFAULT_TIMEZONE`, e.g. `Europe/London` (optional)
4. Deploy. The **Deploy Logs** should show `Synced 2 global commands` and `SYKE online as ...`.

No external database (Supabase, Postgres, etc.) is needed. SYKE only stores watched
channels, opt-outs and timezones.

## Configuration

All settings live in `.env` (see `.env.example`):

| Variable | Default | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | required | Bot token |
| `SYKE_DEV_GUILD_ID` | empty | Sync commands to one server instantly |
| `SYKE_AI_PROVIDER` | `auto` | `auto`, `openai`, `anthropic` or `none` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | empty / `gpt-4o-mini` | |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | empty / `claude-3-5-haiku-latest` | |
| `SYKE_SCAN_LIMIT` | `3000` | Messages read per channel |
| `SYKE_MIN_MESSAGES` | `15` | Messages needed to be judged |
| `SYKE_CACHE_MINUTES` | `15` | How long a scan is reused |
| `SYKE_DEFAULT_TIMEZONE` | `UTC` | Used until an admin sets one |
| `SYKE_DEFAULT_PREFIX` | `!` | Used until an admin runs `!prefix` |
| `SYKE_OWNER_IDS` | `1342786189576634398` | Comma-separated user IDs that can use every admin command in every server, even without Manage Server. Owners without the permission use the prefix or @mention form, since Discord hides admin slash commands from them |

## Development

```bash
python -m pytest              # analysis and rendering tests
python -m syke.preview        # sample report, offline
python -m syke.preview --user 3 --ai   # different persona, using your AI key
```

Tune the personality engine by editing the word lists in `syke/lexicon.py` and the
`SENSITIVITY` values in `syke/traits.py`.

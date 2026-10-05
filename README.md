# SYKE

A Discord bot that reads the channels your admins pick and writes a brutally honest
(and scientifically questionable) personality report on any member: hard stats, a
trait breakdown, a hall of fame of their worst messages, an AI-written roast,
achievements, and how they compare to the rest of the server.

`!profile` replies with a compact four-page card. Buttons switch between pages, and the
card's colour matches the member's strongest trait:

| Page | What's on it |
| --- | --- |
| 📊 **Overview** | The compact card posted in chat: archetype (e.g. *The Class Clown*), the first paragraph of the roast, one line of key numbers, top emojis and catchphrase, and the top 3 traits |
| 📋 **Full report** | The whole roast, six stat tiles (messages, active since, peak hours, average length, top emojis, catchphrase) and all six personality bars |
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
| `!iq [@member]` | everyone | A very unscientific IQ estimate (55-160) with a bell curve, what raised or lowered it (vocabulary, big words, message length, punctuation, serious takes, brainrot, caps-lock chaos), and their smartest and least smart messages. Also shown on `!profile`. Aliases: `!brain`, `!smarts` |
| `!top [trait]` | everyone | Leaderboard for `funny`, `toxic`, `cringe`, `freaky`, `serious`, `chaotic`, `active` or `iq`. Aliases: `!leaderboard`, `!lb` |
| `!scanme` | everyone | Reads back through watched channels (up to `SYKE_DEEP_SCAN_LIMIT` messages each) and adds your older messages to your profile. Aliases: `!scan`, `!addme`. Once per 10 minutes |
| `!mimic [@member]` | everyone | A made-up message in someone's style, from a Markov chain of their messages. Aliases: `!impersonate`, `!copy` |
| `!optout` / `!optin` | everyone | Exclude yourself from being read or judged (opting out also deletes your stored messages) |
| `!help` | everyone | Command list using this server's prefix |
| `!track #channel` / `!untrack #channel` | Manage Server | Choose which channels SYKE reads |
| `!channels` | Manage Server | Watched channels, prefix, timezone and demo mode. Alias: `!settings` |
| `!prefix <new\|mention\|reset>` | Manage Server | Custom prefix (1-5 characters), `mention` so SYKE only answers to `@SYKE profile`, or `reset` for `!`. `!prefix` alone shows the current setting |
| `!timezone <zone>` | Manage Server | Timezone used to spot late-night posting, e.g. `Europe/London`. Peak hours on cards don't need it: they are Discord timestamps, so every reader sees them in their own timezone. Alias: `!tz` |
| `!rescan` | Manage Server | Drop the cache and re-read channels now |
| `!collect [#channel] [limit]` | Manage Server | Read up to 20,000 messages per channel (default 5,000) into the Markov corpus. Without a channel it reads every watched channel |
| `!yap on\|off [#channel]` | Manage Server | When on, SYKE now and then answers chat in that channel with a random message a member sent in the past (`SYKE_YAP_CHANCE` per message, at most once per `SYKE_YAP_COOLDOWN` seconds). Reply to a yap and SYKE replies with another stored message that fits what you said, picked by the AI (or by shared keywords without an AI key). `!yap` alone lists where it's on |
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

1. **Collect**: SYKE reads the last `SYKE_SCAN_LIMIT` messages from each watched channel,
   in parallel, when it starts and whenever the scan is older than `SYKE_CACHE_MINUTES`.
   Refreshes only fetch messages newer than the last scan, and scores are reused until the
   scan changes, so most reports skip straight to the roast. Bots and opted-out users are skipped.
   Text from watched channels (and anything `!collect` or `!scanme` reads) is kept in SQLite.
   It feeds `!mimic` and `!yap`, and profiles include stored messages older than the live
   scan, so `!scanme` permanently adds someone's history. `!optout` deletes a member's stored messages.
2. **Hard stats** (`syke/stats.py`): message count, first seen, busiest 3-hour window,
   average words, top emojis (custom server emojis show up as themselves), and catchphrases
   (repeated 2-3 word phrases).
3. **Traits** (`syke/traits.py`): every message gets scored by word and phrase lists in
   `syke/lexicon.py`, custom emoji names (`:KEKW:` reads as laughing, `:pepe_horny:` as freaky),
   signals like caps lock, `!!!`, keyboard smashes, late-night posting, rapid-fire bursts and
   😂/💀 reactions from *other* people, plus any labels the AI gave that message. Scores are 0-100.
   Because this is local and deterministic, SYKE scores everyone and computes the
   "Top X%" server comparison and leaderboards without any AI calls.
4. **Roast** (`syke/ai.py`): the stats, scores and ~250 recent messages go to an LLM
   (OpenAI or Anthropic). Each run picks a random format (nature documentary, police report,
   patch notes, and so on) and is told to avoid its previous report on that member, so
   summaries read differently every time. The model quotes the member's real messages, can use
   the server's custom emojis, picks highlights, awards a bonus achievement, and labels which
   messages are funny, toxic, cringe, freaky or serious. Those labels are saved and feed back into
   everyone's scores and rankings, and the model's overall ratings are blended into that
   member's card (40%). The prompt forbids attacking identity
   (race, gender, sexuality, disability, and so on). **No API key? No problem**: SYKE falls back to a
   built-in offline roaster (`syke/roast.py`) that mixes hundreds of lines with the member's
   real quotes, catchphrases, emojis and hours, so the bot works fully without one.
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

No external database (Supabase, Postgres, etc.) is needed. SYKE stores watched channels,
opt-outs, per-server settings and the Markov corpus in that SQLite file.

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
| `SYKE_DEEP_SCAN_LIMIT` | `20000` | Messages per channel `!scanme` reads back through |
| `SYKE_YAP_CHANCE` | `0.08` | Chance that a message in a yap channel gets an answer |
| `SYKE_YAP_COOLDOWN` | `120` | Minimum seconds between yaps in one channel |
| `SYKE_OWNER_IDS` | `1342786189576634398` | Comma-separated user IDs that can use every admin command in every server, even without Manage Server. Owners without the permission use the prefix or @mention form, since Discord hides admin slash commands from them |

## Development

```bash
python -m pytest              # analysis and rendering tests
python -m syke.preview        # sample report, offline
python -m syke.preview --user 3 --ai   # different persona, using your AI key
```

Tune the personality engine by editing the word lists in `syke/lexicon.py` and the
`SENSITIVITY` values in `syke/traits.py`.

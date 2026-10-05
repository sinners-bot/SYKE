# SYKE

A Discord bot that reads the channels your admins pick and writes a brutally honest
(and scientifically questionable) personality report on any member: hard stats, a
trait breakdown, a hall of fame of their worst messages, an AI-written roast,
achievements, and how they compare to the rest of the server.

```
📊 ACTIVITY
Messages analyzed: 2,847
Active since: May 2026
Most active: 22:00–01:00
Average message length: 5 words
Top emojis: 💀×588 😭×449 😏×319
Catchphrases:
  "bro really" ×461

──────────────────────────────────

🎭 PERSONALITY BREAKDOWN
😂 Funny   ██████████░░  80%
☠️ Toxic   ████░░░░░░░░  31%
💀 Cringe  ░░░░░░░░░░░░   0%
😏 Freaky  ████░░░░░░░░  33%
🧠 Serious █░░░░░░░░░░░   6%
🔥 Chaotic ██████░░░░░░  50%

──────────────────────────────────

🏆 YOUR STATS

😂 Funniest message:
"bro really thought he could solo
the raid 💀💀"

💀 Most unhinged message:
"ASDFGHJKL I CANT"
...
🧬 PROFILE SUMMARY
🥇 ACHIEVEMENTS
📈 Compared with the server
```

Run `python -m syke.preview` to see a full report generated from a fake server.

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `/syke profile [member]` | everyone | Full personality report (yourself if no member given) |
| `/syke leaderboard <trait>` | everyone | Server ranking for Funny, Toxic, Cringe, Freaky, Serious, Chaotic or Most active |
| `/syke optout` / `/syke optin` | everyone | Exclude yourself from being read or judged |
| `/syke help` | everyone | Quick guide |
| `/syke-admin track <channel>` | Manage Server | Let SYKE read a channel |
| `/syke-admin untrack <channel>` | Manage Server | Stop reading a channel |
| `/syke-admin channels` | Manage Server | List watched channels |
| `/syke-admin timezone <name>` | Manage Server | Timezone for "most active" hours (autocompletes) |
| `/syke-admin rescan` | Manage Server | Drop the cache and re-read channels now |

## How it works

1. **Collect**: when someone asks for a report, SYKE reads the last `SYKE_SCAN_LIMIT`
   messages from each watched channel (bots and opted-out users are skipped). The scan
   is cached in memory for `SYKE_CACHE_MINUTES`. Message content is never written to disk;
   the SQLite file only holds watched channels, opt-outs and timezones.
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
/syke-admin track channel:#general
/syke-admin timezone name:Europe/London
/syke profile member:@someone
```

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

## Development

```bash
python -m pytest              # analysis and rendering tests
python -m syke.preview        # sample report, offline
python -m syke.preview --user 3 --ai   # different persona, using your AI key
```

Tune the personality engine by editing the word lists in `syke/lexicon.py` and the
`SENSITIVITY` values in `syke/traits.py`.

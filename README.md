# SYKE

A Discord bot that reads the channels your admins pick and writes a brutally honest
(and scientifically questionable) personality report on any member: hard stats, a
trait breakdown, a hall of fame of their worst messages, an AI-written roast,
achievements, and how they compare to the rest of the server.

`!profile` replies with a compact four-page card. Buttons switch between pages, and the
card's colour matches the member's strongest trait:

| Page | What's on it |
| --- | --- |
| 📊 **Overview** | The compact card posted in chat: archetype (e.g. *The Class Clown*), the first paragraph of the roast, one line of key numbers (including IQ and aura), top emojis and catchphrase, and the top 3 traits |
| 📋 **Full report** | The whole roast, six stat tiles (messages, active since, peak hours, average length, top emojis, catchphrase) and all six personality bars |
| 🏆 **Highlights** | Funniest, most unhinged, freakiest and most toxic messages, quoted |
| 🥇 **Ranks** | Achievements and "Top X%" against everyone else judged in the server |

Only the person who ran the command can flip pages; anyone else who clicks gets a
private copy of that page. Buttons stop working after 10 minutes.

Run `python -m syke.preview` to see a report from a fake server in your terminal.

## Commands

Every command works with the server prefix (default `!`) **and** as a slash command
(`!profile` or `/profile`). Mentioning the bot as a prefix (`@SYKE profile`) always
works, so a forgotten prefix is never a problem. `@SYKE` plus anything that isn't a
command gets a yap in the server's voice, not a ChatGPT-style answer. Admins can also
switch a server to mentions only with `!prefix mention`.

| Command | Who | What it does |
| --- | --- | --- |
| `!profile [@member]` | everyone | Full personality report (yourself if no member). Aliases: `!judge`, `!p`, `!me` |
| `!ship [@member] [@member]` | everyone | How well two people match. One member ships you with them; two members ships that pair. A percent from their traits, a ship name, a line from each, and a funny write-up. With an AI key the write-up is generated from both chats. Alias: `!compatibility` |
| `$tldr` | everyone | Summarises the previous 300 messages or more in this channel: what it was about, where it drifted, who talked, the lines that landed, the vibe. Also works as `!tldr` with the server prefix. Aliases: `recap`, `summary` |
| `!iq [@member]` | everyone | A very unscientific IQ estimate (55-160) with a bell curve, a verdict written from that member's actual factors and quotes (not a stock line), what raised or lowered it, and their smartest and least smart messages. Also shown on `!profile`. Aliases: `!brain`, `!smarts` |
| `!aura [@member]` | everyone | A very unscientific aura reading (0-200) with a personal verdict, what farmed or spent it, and their biggest aura farm and aura loss. Uses stored messages when it can, so it stays snappy. Also shown on `!profile`. Aliases: `!auracheck`, `!rizz` |
| `!dexify` | everyone | Reply to a message and SYKE posts it back as normal text with `......` after a random few words, like Dex. No embed. Aliases: `!dex`, `!dots` |
| `!steal` | everyone | Reply to a message (or right-click it → Apps → Steal emoji/sticker) and SYKE adds that message's custom emojis and stickers to this server. A name only applies when there is exactly one. Aliases: `!yoink`, `!grab` |
| `!emoji [name]` | everyone | Add a custom emoji from a PNG, JPG or GIF you attach, paste as a link, or reply to. Replying to custom emojis steals those instead. `!emoji list`, `!emoji remove <name>`, `!emoji rename <old> <new>`. The person who added one can remove or rename it; Manage Expressions or Manage Server can change any. Aliases: `!emote`, `!addemoji` |
| `!sticker [name]` | everyone | Same as `!emoji`, for stickers. JPG is converted to PNG and pictures are fitted to 320×320. `!sticker list`, `!sticker remove`, `!sticker rename`. Aliases: `!stickers`, `!addsticker` |
| `!top [trait]` | everyone | Leaderboard for `funny`, `toxic`, `cringe`, `freaky`, `serious`, `chaotic`, `active`, `iq` or `aura`. Aliases: `!leaderboard`, `!lb` |
| `!ask <question>` | everyone | Ask SYKE anything, ChatGPT-style. Also works in DMs; reply to an answer to follow up. Aliases: `!ai`, `!gpt`, `!chat`, `!q`. Needs an AI key. `@SYKE <anything>` in a server yaps instead |
| `!showcase` | everyone | A tour of every feature with live example cards. Aliases: `!features`, `!tour` |
| `!about` | everyone | How many servers use SYKE, members watched, uptime. Aliases: `!stats`, `!info` |
| `!invite` | everyone | Button to add SYKE to your own server |
| `!inviter [@member]` | everyone | Who invited that member. SYKE only knows people who joined while it was in the server (its role needs **Manage Server**). Alias: `!invitedby` |
| `!verify` | everyone | Get the verified role. The slash command is `/verify me`. An admin sets that role up first |
| `!verify @member [@role]` | Manage Server | Verify that member: remove the unverified role and add the verified role, or `@role` if you name one. The slash form is `/verify me` with those options |
| `!verify setup` | Manage Server | Show the verification gate and how to finish it. Also `role`, `take`, `channel`, `anywhere`, `panel`, `message`, `off` |
| `!servers` | SYKE owners | Every server SYKE is in, biggest first, with watched channels and join dates |
| `!scanme` | everyone | Reads back through watched channels (up to `SYKE_DEEP_SCAN_LIMIT` messages each) and adds your older messages to your profile. Aliases: `!scan`, `!addme`. Once per 10 minutes |
| `!mimic [@member]` | everyone | A made-up message in someone's style, from a Markov chain of their messages. Aliases: `!impersonate`, `!copy` |
| `!optout` / `!optin` | everyone | Exclude yourself from being read or judged (opting out also deletes your stored messages) |
| `!help` | everyone | Command list using this server's prefix |
| `!track #channel` / `!untrack #channel` | Manage Server | Choose which channels SYKE reads |
| `!channels` | Manage Server | Watched channels, prefix, timezone, demo mode and verification. Alias: `!settings` |
| `!prefix <new\|mention\|reset>` | Manage Server | Custom prefix (1-5 characters), `mention` so SYKE only answers to `@SYKE profile`, or `reset` for `!`. `!prefix` alone shows the current setting |
| `!timezone <zone>` | Manage Server | Timezone used to spot late-night posting, e.g. `Europe/London`. Peak hours on cards don't need it: they are Discord timestamps, so every reader sees them in their own timezone. Alias: `!tz` |
| `!rescan` | Manage Server | Drop the cache and re-read channels now |
| `!collect [#channel] [limit]` | Manage Server | Read up to 20,000 messages per channel (default 5,000) into the Markov corpus. Without a channel it reads every watched channel |
| `!yap on\|off [#channel]` | Manage Server | When on, SYKE now and then answers chat in that channel (`SYKE_YAP_CHANCE` per message, at most once per `SYKE_YAP_COOLDOWN` seconds). **@mention SYKE** (with anything that isn't a command) and it always yaps back in the server's voice, even if yap isn't on in that channel. Reply to a yap and SYKE answers back in the chat's tone (toxic, funny or freaky, read from your reply and the recent chat). Every stored message feeds the server's vocabulary; the ones that match the topic (and Markov remixes) are ammunition. The AI writes a new original line from those words plus matching slang from SYKE's lexicon, instead of pasting or stitching old quotes. It skips lines it (or anyone) just said in that channel. Without an AI key it uses the best matching stored message for the topic and tone. `!yap` alone lists where it's on |
| `!demo on\|off` | Manage Server | Add 5 fake members to rankings so you can test alone |
| `!sample [name]` | Manage Server | Full report for a fake member (Zyro, Mira, bubbles, Dex, Vex) |

### Games, awards and welcomes

| Command | Who | What it does |
| --- | --- | --- |
| `!guess` | everyone | **Who said it?** SYKE posts a real stored message with the name hidden and four names as buttons. One guess each, first correct guess wins a point, 30 seconds. `!guess top` is the leaderboard. Aliases: `!whosaid`, `!wsi` |
| `!awards` | Manage Server | **Weekly awards** from the last 7 days in watched channels: Biggest Yapper, Funniest, Most Laughs, Most Unhinged, Most Toxic, Freakiest, Cringe Lord, Most Serious, Night Owl, plus Message of the Week, each with a quote. `!awards channel #channel` turns them on, `!awards when sunday 18:00` sets the time (server timezone), `!awards role @Role` gives every winner a role until next week, `!awards preview` shows just you, `!awards now` posts immediately, `!awards off` |
| `!welcome` | Manage Server | **Welcome message** posted when someone gets verified (by `!verify`, the Verify button, or an admin). Your own embed design with a gentle AI roast of their name and avatar (a built-in roaster without an AI key). `!welcome channel #channel`, `!welcome title <text>`, `!welcome message <text>`, `!welcome colour #ff8800`, `!welcome image <url>`, `!welcome footer <text>`, `!welcome roles #channel`, `!welcome colors #channel` (adds "Grab your roles in #roles and pick a name colour in #colors." after the default text), `!welcome ping on\|off`, `!welcome thumbnail on\|off`, `!welcome test`, `!welcome off`. Placeholders: `{roast}` `{mention}` `{name}` `{username}` `{server}` `{count}` `{roles}` `{colors}` `{next_steps}`; `\n` makes a new line in slash commands |
| `!dashboard` | Manage Server | **Every setting in one message**: pick a section (General, Channels, Verification, Welcome, Weekly awards, Moderation) and change things with menus, buttons and forms. Only the person who opened it can use it. Aliases: `!panel`, `!admin`, `!setup` |

### Moderation

Each command needs the matching Discord permission (or Administrator, or `SYKE_OWNER_IDS`).
Moderators can only act on members whose top role is below theirs, and SYKE's role must be above
the member for timeouts, kicks and bans. Actions are numbered as cases, DMed to the member, and posted to
the mod log if one is set.

| Command | Who | What it does |
| --- | --- | --- |
| `!restrict @member [what] [duration] [reason]` | Manage Roles | Stop a member posting `images` (all files), `links` (links, GIFs and embeds), `reactions`, `stickers` or `emojis` (external ones) anywhere in the server. Combine them (`images links`), or use `media` (images, links, stickers) or `all`. Nothing named means images and links. Durations like `30m`, `2h`, `7d`; nothing means until lifted. Example: `!restrict @Dex images links 1d posting gore`. Aliases: `!mediaban`, `!nomedia` |
| `!unrestrict @member [what]` | Manage Roles | Lift some restrictions, or all of them |
| `!restrictions [list\|sync]` | Manage Roles | Who is restricted and until when. `sync` re-applies the restriction roles to every channel |
| `!timeout @member <duration> [reason]` | Moderate Members | Discord timeout, up to 28 days. `!untimeout @member` ends it. Aliases: `!mute`, `!unmute` |
| `!warn @member <reason>` | Moderate Members | A warning in their record. `!warnings @member` lists them, `!delwarn <case>` removes one, `!clearwarns @member` removes all |
| `!history @member` | Moderate Members | Every case on that member: warnings, restrictions, timeouts, kicks, bans. Aliases: `!modlogs`, `!cases` |
| `!purge <amount> [@member]` | Manage Messages | Delete up to 500 recent messages here, or only that member's. Pins are kept. Aliases: `!clear`, `!prune` |
| `!slowmode <10s\|2m\|off> [#channel]` | Manage Channels | Set or remove slowmode (max 6h) |
| `!lock [#channel] [reason]` / `!unlock [#channel]` | Manage Channels | Stop @everyone talking in a channel; unlocking restores what was there before |
| `!kick @member [reason]` | Kick Members | Kick a member |
| `!ban @user [reason]` / `!unban <user ID>` | Ban Members | Ban a member, or anyone by ID even if they already left |
| `!tempban @user <duration> [reason]` | Ban Members | Ban for a set time (e.g. `1d`, `12h`, `1w`), then SYKE auto-unbans. Survives restarts. Alias: `!tban` |
| `!softban @user [reason]` | Ban Members | Ban and instantly unban to wipe the member's last day of messages (a kick that cleans up a spammer) |
| `!modlog <#channel\|off>` | Manage Server | Post every moderation action, and restrictions ending, to a channel |

### Automod

Runs automatically on every message. Administrators, anyone with Manage Messages, SYKE owners, and any
role you exempt are never filtered. Every action becomes a numbered case and is posted to the mod log.
See the whole setup at a glance with `!automod`.

| Command | Who | What it does |
| --- | --- | --- |
| `!filter on\|off` · `!filter add <word>` · `!filter remove <word>` | Manage Server | **Word/slur blacklist** that auto-deletes matches. Catches whole words, leetspeak (`sh1t`), and spaced-out or embedded long words. `!filter action delete\|warn\|timeout [10m]` sets what happens, `!filter clear` empties it |
| `!automod toxic <#channel> [threshold]` | Manage Server | **Flag** borderline-toxic messages (using SYKE's toxicity scoring) to a review channel **instead of deleting**, so mods decide. Threshold 1.0 (strict) – 3.0 (loose) |
| `!links invites on\|off` | Manage Server | Auto-remove Discord server invites from non-mods |
| `!links urls on\|off` · `!links allow <domain>` | Manage Server | Auto-remove links not on the allowlist. `!links allow youtube.com` whitelists a domain |
| `!antispam on\|off` | Manage Server | Catch message **floods**, **duplicate** messages, **mass mentions** and **wall-of-caps**, then timeout the sender. Tune with `!antispam rate <count> <seconds>`, `!antispam mentions <n>`, `!antispam duration <10m>` |
| `!automod ignore @role` | Manage Server | Exempt a role from all automod (toggle) |

SYKE needs **Manage Messages** to delete, and **Timeout Members** for anti-spam timeouts. Toxicity
review only needs a channel it can post in.

**How `!restrict` holds server-wide.** SYKE makes one role per restriction (`SYKE · no images`,
`SYKE · no links`, …) and denies that permission to it in every channel and category, including channels
made later. Discord then greys out uploads for that member. Anything that still gets through is deleted:
a role elsewhere that explicitly *allows* the permission (Discord lets an allow win), administrators,
forwarded messages, server stickers (no Discord permission covers those) and reactions. Restrictions are
saved, end on time even across restarts, and come back if the member leaves and rejoins.
SYKE needs **Manage Roles** and **Manage Messages**. The `SYKE · no …` roles are created at the bottom
of the role list, below SYKE, so they work straight away. Run `!restrictions sync` if you move channels around or SYKE was offline
when channels were created.

## Testing on your own

1. `!demo on` adds five fake members (Zyro, Mira, bubbles, Dex, Vex) to this server's
   comparisons and leaderboards. Reports and leaderboards say so in the footer.
2. `!sample zyro` shows a full report instantly, with no messages needed.
   It also exercises your OpenAI key if one is set.
3. To judge yourself, send at least `SYKE_MIN_MESSAGES` messages (default 15) in a watched channel,
   then run `!rescan` and `!profile`. You'll be ranked against the fake members.
   For quicker tests, set `SYKE_MIN_MESSAGES=5` in Railway's Variables.
4. Turn it off with `!demo off` before real members start using SYKE.

## Verification

A gate for new members. SYKE gives the verified role, and can remove another one. Which channels that role can see stays in Discord's permission settings.

1. Make a role such as **Verified** and drag it **under** SYKE's role. Give SYKE **Manage Roles**.
2. In channel permissions, hide the server from @everyone and let **Verified** see it. Leave one channel, `#gate`, that newcomers can see.
3. `!verify role @Verified`
4. `!verify channel #gate`
5. `!verify panel` posts a button in `#gate`.

Members run `!verify` or press **Verify**. An admin can verify someone else with `!verify @member`, from any channel. `!verify @member @Role` grants that role instead of the saved one. `!verify take @Unverified` removes a role when they pass, including when an admin verifies them. `!verify message hello` changes the panel text (`!verify message reset` puts the default back). `!verify anywhere` drops the channel limit. `!verify off` turns the gate off. `!verify setup` shows where things stand.

The button keeps working after a restart.

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
   signals like caps lock, `!!!`, keyboard smashes, shouted words, emoji spam, late-night posting,
   rapid-fire bursts and 😂/💀 reactions from *other* people, plus any labels the AI gave that message.
   Freaky, toxic and unhinged (chaotic) also catch thirst, aimed insults ("you're so bad") and
   crashouts ("i'm losing it") that never use the obvious keyword. A streak of those messages
   lifts the score even when the rest of someone's chat is ordinary, so volume doesn't wash them out.
   Scores are 0-100. Because this is local and deterministic, SYKE scores everyone and computes the
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
   **Message Content Intent** (SYKE can't read messages without it) and **Server Members Intent**
   (SYKE can't see joins, so `!inviter` stays empty, without it).
3. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`; permissions
   **View Channels**, **Read Message History**, **Send Messages**, **Embed Links**,
   **Use External Emojis**, **Add Reactions**, **Manage Expressions** (so `!steal`, `!emoji` and `!sticker` can add them),
   **Manage Server** (so `!inviter` can read which invite was used),
   **Manage Roles** (so `!verify` and `!restrict` can give roles), and for moderation **Attach Files**,
   **Use External Stickers**, **Manage Messages**, **Manage Channels**, **Moderate Members**,
   **Kick Members** and **Ban Members**. A bot can only deny permissions it has, which is why
   `!restrict` needs Attach Files and Use External Stickers itself.
   Open the generated URL to invite SYKE to your server.
4. **Add App button**: keep **Public Bot** on (Bot tab) so other people can add SYKE. On startup SYKE sets
   its own guild-install settings (scopes `bot` + `applications.commands`, the permissions above), so **Add App** on SYKE's profile adds it to a server.
   If you set a custom install link in *Installation*, SYKE leaves it alone; `SYKE_MANAGE_INSTALL=0` turns this off.

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

## Owner web dashboard

An optional private website for SYKE's owners: every server SYKE is in, their channels and live
chats, and controls to act as the bot. It runs inside the same process as the bot (no extra service),
and is **off** unless you set the four variables below. Login is Discord OAuth, and only the user IDs
in `SYKE_OWNER_IDS` can get in; everyone else is turned away after signing in.

What you can do from it:
- **Browse** every server, its channels, and recent messages in each channel.
- **Send messages as SYKE** in any channel it can talk in.
- **Moderate**: timeout, restrict, kick, ban (and the reverses) by member ID. Each action is logged
  as a normal case and shows up in `!history`.
- **Change settings**: prefix, timezone, and which channels are watched.
- **Maintenance**: rescan a server, clear its stored messages, or make SYKE leave.

### Turn it on

1. In the [Developer Portal](https://discord.com/developers/applications) → your app:
   - **General Information**: copy the **Application ID** → `DISCORD_CLIENT_ID`.
   - **OAuth2**: copy the **Client Secret** → `DISCORD_CLIENT_SECRET`.
   - **OAuth2 → Redirects**: add `https://YOUR-URL/callback` (whatever `SYKE_WEB_BASE_URL` is, plus `/callback`).
2. Set the variables (locally in `.env`, or on Railway under **Variables**):
   - `SYKE_WEB=1`
   - `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`
   - `SYKE_WEB_SECRET` = a long random string you invent (it signs login cookies; keep it secret)
   - `SYKE_WEB_BASE_URL` = the public URL of the service
3. On Railway, give the service a public domain: **Settings → Networking → Generate Domain**. Use that
   URL (with `https://`) for `SYKE_WEB_BASE_URL` and the OAuth redirect. Railway sets `PORT` itself.

Then open the URL and log in with Discord. Security: sessions are signed cookies (HMAC), every action
needs a CSRF token tied to your account, and anything shown from Discord is HTML-escaped.

## Deploying to Railway

SYKE runs as a single service, and `railway.json` already sets the start command and restart policy.
It needs no web port unless you turn on the owner web dashboard (see above), in which case it also
listens on Railway's `PORT`.

1. Push this repo to GitHub, then in Railway: **New Project → Deploy from GitHub repo**.
2. **Add a volume** to the service (right-click the service → *Attach volume*) mounted at `/data`.
   Without it, the SQLite file is wiped on every redeploy and admins lose their channel setup.
3. In the service's **Variables** tab, set:
   - `DISCORD_TOKEN` = your bot token
   - `SYKE_DB_PATH` = `/data/syke.db`
   - `OPENAI_API_KEY` = your key (optional; omit to use the offline roaster)
   - `SYKE_DEFAULT_TIMEZONE`, e.g. `Europe/London` (optional)
   - For the owner web dashboard (optional): `SYKE_WEB=1`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`,
     `SYKE_WEB_SECRET`, `SYKE_WEB_BASE_URL` (see **Owner web dashboard** above)
4. Deploy. The **Deploy Logs** should show `Synced 2 global commands` and `SYKE online as ...`.

No external database (Supabase, Postgres, etc.) is needed. SYKE stores watched channels,
opt-outs, per-server settings, who invited each member, moderation cases and restrictions, and the
Markov corpus in that SQLite file.

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
| `SYKE_MANAGE_INSTALL` | `1` | Set the app's guild-install settings on startup so **Add App** works (`0` to manage them yourself) |
| `SYKE_YAP_CHANCE` | `0.08` | Chance that a message in a yap channel gets an answer |
| `SYKE_YAP_COOLDOWN` | `120` | Minimum seconds between yaps in one channel |
| `SYKE_OWNER_IDS` | `1342786189576634398` | Comma-separated user IDs that can use every admin command in every server, even without Manage Server. Owners without the permission use the prefix or @mention form, since Discord hides admin slash commands from them. These are also the only accounts that can log in to the owner web dashboard |
| `SYKE_WEB` | empty | `1` turns on the owner web dashboard (needs the three values below). See **Owner web dashboard** |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | empty | OAuth app credentials for the dashboard login |
| `SYKE_WEB_SECRET` | empty | A long random secret that signs dashboard login cookies |
| `SYKE_WEB_BASE_URL` | empty | The dashboard's public URL, e.g. `https://syke.up.railway.app` |

## Development

```bash
python -m pytest              # analysis and rendering tests
python -m syke.preview        # sample report, offline
python -m syke.preview --user 3 --ai   # different persona, using your AI key
```

Tune the personality engine by editing the word lists in `syke/lexicon.py` and the
`SENSITIVITY` values in `syke/traits.py`.

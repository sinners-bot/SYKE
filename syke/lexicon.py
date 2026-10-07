"""Word lists behind the trait heuristics. Tweak these to tune SYKE's judgement.

Words are matched against whole lowercase words; phrases are matched as substrings of the
lowercased message; emoji-name hints are matched as substrings of custom server emoji names
(so `:KEKW:`, `:pepe_laugh:` and `:omegalul:` all count as laughing).
"""

LAUGH_WORDS = {
    "lol", "lool", "loool", "lolol", "lololol", "lmao", "lmaoo", "lmaooo", "lmaoooo", "lmfao",
    "lmfaoo", "lmfaooo", "haha", "hahah", "hahaha", "hahahaha", "hahahahaha", "ahaha", "ahahaha",
    "hehe", "hehehe", "heh", "rofl", "roflmao", "kek", "kekw", "icant", "icantt", "xd", "xdd",
    "xddd", "deadass", "crying", "dying", "dead", "deceased", "bruh", "bruhh", "bruhhh", "ded",
    "screaming", "weak", "wheeze", "wheezing", "hilarious", "funny", "joke", "jk", "jkjk", "lul",
    "lulw", "omegalul", "pmsl", "pmo", "jajaja", "kkkk", "kkkkk", "wkwk", "wkwkwk", "mdr", "ptdr",
    "sksksk", "skskskks", "ayo", "istg", "lolz", "lulz",
}
LAUGH_PHRASES = (
    "i can't", "i cant", "im dead", "i'm dead", "i'm crying", "im crying", "crying rn",
    "i'm weak", "im weak", "no way", "nah bro", "nahh bro", "bro what", "this is so funny",
    "too funny", "so funny", "i'm screaming", "im screaming", "help me", "i'm wheezing",
    "can't breathe", "cant breathe", "on god", "who let", "bro really", "not the",
    "the way i", "why is this so", "this got me", "sent me", "this sent me", "💀💀",
)
LAUGH_EMOJIS = {"😂", "🤣", "💀", "😭", "😹", "☠️", "😆", "😅", "😝", "🙈", "🫠", "🗿", "🤭"}

TOXIC_WORDS = {
    "stfu", "gtfo", "idiot", "idiots", "stupid", "dumb", "dumbass", "dumbfuck", "trash", "clown",
    "loser", "losers", "moron", "morons", "pathetic", "garbage", "ratio", "ratioed", "cope",
    "coping", "seethe", "seething", "mald", "malding", "bozo", "noob", "shut", "fuck", "fucking",
    "fucker", "fuckin", "fk", "fkn", "fking", "shit", "shitty", "bullshit", "bitch", "bitches",
    "ass", "asshole", "hate", "hated", "annoying", "worst", "useless", "kill", "kys", "wtf",
    "skill", "issue", "retard", "retarded", "cunt", "twat", "prick", "dick", "dickhead",
    "wanker", "bastard", "scum", "dogshit", "braindead", "brainless", "dipshit", "jackass",
    "imbecile", "incompetent", "clueless", "ugly", "fatass", "nerd", "virgin", "incel", "simp",
    "irrelevant", "fraud", "washed", "mid", "ez",
    "cry", "crybaby", "bum", "rat", "snake", "toxic", "trash-talk",
    "lame", "suck", "sucks", "sucked", "dogwater", "hardstuck", "uninstall", 
    "hell", "damn", "dammit", "goddamn", "screw", "pissed", "piss", "crap",
}
TOXIC_PHRASES = (
    "shut up", "shut the", "skill issue", "get good", "git gud", "go cry", "cry about it",
    "touch grass", "no one asked", "nobody asked", "who asked", "didn't ask", "didnt ask",
    "you're trash", "ur trash", "you suck", "u suck", "get a life", "go outside",
    "kill yourself", "end yourself", "fuck off", "fuck you", "fuck u", "screw you", "piss off",
    "ratio +", "+ ratio", "l + ratio", "cope harder", "stay mad", "seethe more", "cry more",
    "you're so dumb", "ur so dumb", "you're stupid", "ur stupid", "are you stupid", "are u dumb",
    "dog water", "uninstall the game", "delete the game", "log off", "get rekt", "get wrecked",
    "go back to", "you're annoying", "ur annoying", "nobody likes", "no one likes", "shut it",
    "zip it", "who tf", "what tf", "tf is wrong", "the hell is wrong", "hold this l",
)
TOXIC_EMOJIS = {"🖕", "🤡", "🙄", "😒", "🤬", "😡", "😤", "💩", "🤮", "🫵", "😠", "🗑️", "🚮"}

CRINGE_WORDS = {
    "uwu", "owo", "rawr", "nya", "nyaa", "skibidi", "rizz", "rizzler", "gyatt", "gyat", "sigma",
    "ohio", "fanum", "mewing", "mew", "bestie", "besties", "slay", "slayed", "slaying", "periodt",
    "yeet", "yeeted", "pog", "poggers", "pogchamp", "sus", "amogus", "mood", "vibes", "vibing",
    "smol", "heckin", "doggo", "pupper", "nom", "noms", "uwuu", "hewwo", "senpai",
    "kawaii", "desu", "baka", "pwease", "pwetty", "boop", "snuggle", "snuggles",
    "cuddles", "hugz", "teehee", "luv", "wuv", "frfr", "bussin", "sheesh", "goated", "based",
    "aura", "delulu", "alpha", "beta", "glizzy", "griddy", "edging", "looksmaxxing",
    "mogging", "mogged", "huzz", "chuzz", "jit", "opp", "opps", "mothered",
    "iconic", "queen", "kween", "yass", "yasss", "yaas", "slayy", "gurl", "hunty", "sksk",
    "lit", "fam", "squad", "goals", "bae", "cutie", "cutiepie", "xoxo",
    "nyan", "meow", "mrow", "purr", "purrr", "ehehe", "uwah", "kyaa",
}
CRINGE_PATTERNS = (
    ":3", ">w<", "x3", "^^", "^_^", ">.<", "uwu", "owo", "^w^", "(◕‿◕)", "( ˘ ³˘)", "♡", "<3",
    "*hugs*", "*blushes*", "*notices", "*nuzzles", "*pats", "*boops", "*giggles", "rawr x3",
    "hewwo", "no cap", "on skibidi", "fanum tax", "ohio rizz", "sigma male", "w rizz",
    "it's giving", "its giving", "ate and left no crumbs", "main character", "living rent free",
    "rent free", "understood the assignment", "slay queen", "pick me", "not me", "chile",
)

FREAKY_WORDS = {
    "freaky", "freak", "freaks", "daddy", "mommy", "mami", "papi", "kinky", "kink", "kinks",
    "thicc", "thick", "zaddy", "spicy", "sexy", "smash", "feet", "babygirl", "steamy",
    "naughty", "unholy", "horny", "horni", "bonk", "goon", "gooning", "gooner", "lewd", "nsfw",
    "dumptruck", "booty", "bussy", "milf", "dilf", "baddie", "baddies",
    "seduce", "seductive", "moan", "moaning", "ahegao", "hentai", "onlyfans", "nudes",
    "tease", "teasing", "submissive", "dominant", "degrade", "choke",
    "spank", "spanked", "lick", "licking", "thighs", "thigh", "curvy", "juicy",
    "bedroom", "pegged", "rail", "railed", "breed", "bred", "risky", "frisky", "flirty",
    "flirt", "flirting", "simp", "simping", "pookie", "shawty", "shorty", "wifey", "hubby",
}
FREAKY_PHRASES = (
    "down bad", "step on me", "rail me", "ngl kinda", "would smash", "i'd smash", "id smash",
    "sit on", "on my face", "choke me", "call me daddy", "call me mommy", "good boy",
    "good girl", "be my", "come here", "come over", "in my bed", "what are you wearing",
    "send pics", "send feet", "show feet", "feet pics", "i'm down bad", "so down bad",
    "kinda hot", "lowkey hot", "kinda freaky", "lowkey freaky", "hit it", "hitting it",
    "bend over", "on your knees", "netflix and chill", "smash or pass", "body count",
    "situationship", "rizz me", "rizzed", "make out", "kiss me", "touch me", "spit in",
    "i'm horny", "im horny", "so horny", "goon cave", "freak mode", "freak of the week",
)
FREAKY_EMOJIS = {
    "😏", "👅", "🥵", "😩", "😈", "🍑", "🍆", "💦", "🫦", "😳", "🥴", "🤤", "😘", "💋",
    "🌶️", "🍒", "🍌", "😮‍💨", "🛏️", "⛓️", "👠",
}

SERIOUS_WORDS = {
    "actually", "however", "because", "therefore", "although", "honestly", "basically",
    "think", "believe", "consider", "agree", "disagree", "argument", "reason", "evidence",
    "research", "understand", "technically", "realistically", "important", "explain",
    "context", "source", "opinion", "probably", "furthermore", "moreover", "meanwhile",
    "nevertheless", "regardless", "consequently", "essentially", "fundamentally", "objectively",
    "subjectively", "arguably", "specifically", "generally", "typically", "theory", "data",
    "analysis", "perspective", "assume", "assumption", "conclusion", "logic", "logical",
    "factually", "statistically", "historically", "philosophy", "economy", "politics",
    "society", "ethical", "morally", "situation", "responsibility", "relationship",
    "therapy", "mental", "health", "career", "university", "study", "studying", "exam",
    "assignment", "deadline", "project", "budget", "finance", "investment", "advice",
    "recommend", "suggest", "clarify", "elaborate", "nuance", "nuanced", "complex",
    "complicated", "significant", "perhaps", "likely", "unlikely", "accurate", "inaccurate",
    "correct", "incorrect", "valid", "discussion", "debate",
}
SERIOUS_PHRASES = (
    "in my opinion", "to be fair", "on the other hand", "that being said", "with that said",
    "the problem is", "the issue is", "the reason is", "it depends", "i think that",
    "i believe that", "i disagree", "i agree", "for example", "for instance", "in general",
    "at the end of the day", "the point is", "to be honest", "in fact", "as a result",
    "do you have a source", "according to", "studies show", "research shows", "makes sense",
    "doesn't make sense", "let me explain", "here's the thing", "the thing is", "imo", "imho",
    "if you think about it", "not necessarily", "long story short", "keep in mind",
)

# Substrings of custom emoji names (lowercase) that hint at a trait.
EMOJI_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "funny": ("kek", "lul", "lol", "lmao", "laugh", "haha", "omega", "cry", "joy", "dead",
              "skull", "clown", "xd", "wheeze", "rofl", "funny", "giggle", "lmfao", "icant"),
    "toxic": ("mad", "angry", "rage", "pout", "trash", "bin", "ban", "hammer", "gun", "knife",
              "middle", "finger", "fuck", "stfu", "reee", "triggered", "disgust", "smh", "bonk",
              "ratio", "cope", "seethe", "mald", "kys", "die", "punch", "slap", "spit"),
    "cringe": ("uwu", "owo", "blush", "shy", "cute", "kawaii", "nya", "cat", "pat", "hug",
               "heart", "love", "sparkle", "pog", "cringe", "sus", "amogus", "sigma", "rizz",
               "chad", "mew", "skibidi", "smol", "boop", "pleading", "pleadge"),
    "freaky": ("horny", "lewd", "bonk", "drool", "lick", "thirst", "smirk", "flirt", "wink",
               "freak", "kiss", "peach", "eggplant", "sweat", "hot", "spicy", "naughty", "moan",
               "simp", "daddy", "gyat", "thicc", "bed", "nsfw", "kinky", "ahegao", "eyes"),
    "serious": ("think", "hmm", "nerd", "glasses", "book", "read", "study", "brain", "big_brain",
                "bigbrain", "galaxy", "monocle", "science", "smart", "notes", "teacher", "wise"),
}

# Everyday chat slang, not tied to a trait. Yap offers the ones a server actually uses to the AI.
CHAT_SLANG = {
    "ngl", "fr", "tbh", "lowkey", "highkey", "deadass", "istg", "ong", "icl", "fym", "smh",
    "idk", "idc", "imo", "rn", "bc", "cuz", "ur", "u", "ya", "yall", "y'all", "bro", "bruh",
    "dude", "nah", "naw", "yea", "ye", "yep", "nope", "aight", "ight", "bet", "fax", "facts",
    "cap", "valid", "mid", "cooked", "crashout", "glazing", "glaze", "yapping", "yap", "ts",
    "gng", "twin", "lil", "kinda", "sorta", "tho", "prolly", "def", "fs", "wym", "wdym", "hbu",
    "gg", "ez", "w", "l", "ratio", "real", "bffr", "unc", "chat", "ain't", "finna", "tryna",
}

STOPWORDS = {
    "the", "and", "for", "you", "that", "this", "with", "are", "was", "but", "not",
    "have", "has", "had", "its", "it's", "just", "like", "what", "when", "where",
    "who", "why", "how", "can", "could", "would", "should", "will", "your", "you're",
    "they", "them", "their", "there", "then", "than", "from", "about", "into", "out",
    "all", "any", "get", "got", "did", "does", "doing", "dont", "don't", "i'm", "im",
    "yeah", "yes", "also", "too", "very", "really", "some", "one", "our", "his",
    "her", "she", "him", "him", "were", "been", "being", "who", "which", "more",
    "now", "here", "lol", "lmao", "know", "think", "thats", "that's", "going", "gonna",
    "want", "see", "say", "said", "make", "even", "because", "much", "only", "way",
    "well", "who", "why", "off", "still", "over", "let", "can't", "cant", "didn't",
    "didnt", "isn't", "isnt", "these", "those", "my", "me", "mine",
    "of", "to", "in", "on", "at", "is", "it", "be", "so", "do", "if", "or", "an", "as",
    "we", "us", "am", "by", "we're", "they're", "i've", "i'd", "i'll",
}

"""Songs for the radio and the song quiz, by genre: well-known tracks as (artist, title).

Only names: the audio is found on YouTube (or SoundCloud) when a song plays, like a Spotify link.
"""

from __future__ import annotations

GENRES: dict[str, list[tuple[str, str]]] = {
    "drill": [
        ("Pop Smoke", "Dior"), ("Pop Smoke", "Welcome to the Party"), ("Central Cee", "Doja"),
        ("Central Cee", "Sprinter"), ("Central Cee", "Band4Band"), ("Digga D", "Woi"), ("Headie One", "18HUNNA"),
        ("Fivio Foreign", "Big Drip"), ("Kay Flock", "Being Honest"), ("Sheff G", "No Suburban"),
        ("Unknown T", "Homerton B"), ("Russ Millions", "Body"), ("Chief Keef", "Love Sosa"),
        ("King Von", "Crazy Story"), ("Lil Durk", "Viral Moment"), ("Ice Spice", "Munch (Feelin' U)"),
        ("Luciano", "Drilla"), ("Pop Smoke", "Mood Swings"), ("Central Cee", "Obsessed With You"),
        ("Headie One", "Only You Freestyle"),
    ],
    "deutschrap": [
        ("Luciano", "Beautiful Girl"), ("Luciano", "Mies"), ("Capital Bra", "Neymar"), ("Capital Bra", "Berlin lebt"),
        ("Apache 207", "Roller"), ("Apache 207", "Komet"), ("Bonez MC & RAF Camora", "500 PS"),
        ("Bonez MC & RAF Camora", "Palmen aus Plastik"), ("RAF Camora", "Primo"), ("Gzuz", "Was hast du gedacht"),
        ("Shirin David", "Bitches brauchen Rap"), ("Ski Aggu", "Party Sahne"),
        ("Pashanim", "Airwaves"), ("Samra", "Cataleya"), ("Sido", "Mein Block"),
        ("Cro", "Easy"), ("Kontra K", "Erfolg ist kein Glück"), ("Haftbefehl", "Chabos wissen wer der Babo ist"),
        ("Bausa", "Was du Liebe nennst"), ("Kool Savas", "LMS"), ("Luciano", "Schmetterling"),
    ],
    "rap": [
        ("Kendrick Lamar", "Not Like Us"), ("Kendrick Lamar", "HUMBLE."), ("Drake", "God's Plan"),
        ("Drake", "Hotline Bling"), ("Travis Scott", "SICKO MODE"), ("Travis Scott", "FE!N"),
        ("Eminem", "Lose Yourself"), ("Eminem", "Without Me"), ("50 Cent", "In Da Club"),
        ("Kanye West", "Stronger"), ("J. Cole", "No Role Modelz"), ("Future", "Mask Off"),
        ("Post Malone", "Rockstar"), ("Lil Nas X", "Old Town Road"), ("Juice WRLD", "Lucid Dreams"),
        ("XXXTENTACION", "SAD!"), ("Playboi Carti", "Magnolia"), ("Metro Boomin", "Superhero"),
        ("Jack Harlow", "First Class"), ("Doja Cat", "Paint The Town Red"), ("Cardi B", "Bodak Yellow"),
        ("Snoop Dogg", "Drop It Like It's Hot"), ("Dr. Dre", "Still D.R.E."), ("Tyler, The Creator", "EARFQUAKE"),
    ],
    "pop": [
        ("The Weeknd", "Blinding Lights"), ("Dua Lipa", "Levitating"), ("Harry Styles", "As It Was"),
        ("Ed Sheeran", "Shape of You"), ("Taylor Swift", "Anti-Hero"), ("Billie Eilish", "bad guy"),
        ("Olivia Rodrigo", "drivers license"), ("Miley Cyrus", "Flowers"), ("Sabrina Carpenter", "Espresso"),
        ("Chappell Roan", "Good Luck, Babe!"), ("Bruno Mars", "Uptown Funk"), ("Justin Bieber", "Peaches"),
        ("Ariana Grande", "7 rings"), ("Lady Gaga", "Bad Romance"), ("Katy Perry", "Firework"),
        ("Adele", "Rolling in the Deep"), ("Rihanna", "Umbrella"), ("Beyoncé", "Crazy in Love"),
        ("Benson Boone", "Beautiful Things"), ("Billie Eilish", "BIRDS OF A FEATHER"), ("Tate McRae", "greedy"),
        ("Lady Gaga & Bruno Mars", "Die With A Smile"), ("Rosé & Bruno Mars", "APT."), ("Teddy Swims", "Lose Control"),
    ],
    "rnb": [
        ("SZA", "Kill Bill"), ("SZA", "Snooze"), ("Frank Ocean", "Thinkin Bout You"), ("Usher", "Yeah!"),
        ("Chris Brown", "Under the Influence"), ("Daniel Caesar", "Best Part"), ("Giveon", "Heartbreak Anniversary"),
        ("Brent Faiyaz", "Dead Man Walking"), ("Summer Walker", "Girls Need Love"), ("The Weeknd", "Earned It"),
        ("Alicia Keys", "No One"), ("Ne-Yo", "So Sick"), ("Miguel", "Sure Thing"), ("Jhené Aiko", "Sativa"),
        ("Bryson Tiller", "Don't"), ("Destiny's Child", "Say My Name"), ("Beyoncé", "Halo"),
        ("Tems", "Free Mind"), ("Victoria Monét", "On My Mama"), ("Muni Long", "Hrs and Hrs"),
    ],
    "throwback": [
        ("Toto", "Africa"), ("a-ha", "Take On Me"), ("Queen", "Bohemian Rhapsody"), ("Michael Jackson", "Billie Jean"),
        ("Whitney Houston", "I Wanna Dance with Somebody"), ("Backstreet Boys", "I Want It That Way"),
        ("Britney Spears", "...Baby One More Time"), ("Spice Girls", "Wannabe"), ("Nirvana", "Smells Like Teen Spirit"),
        ("Oasis", "Wonderwall"), ("Eminem", "The Real Slim Shady"), ("Outkast", "Hey Ya!"),
        ("The Killers", "Mr. Brightside"), ("Linkin Park", "In the End"), ("Gorillaz", "Feel Good Inc."),
        ("Daft Punk", "One More Time"), ("Haddaway", "What Is Love"), ("Eiffel 65", "Blue (Da Ba Dee)"),
        ("Las Ketchup", "The Ketchup Song"), ("Nelly", "Hot in Herre"), ("Coolio", "Gangsta's Paradise"),
        ("Avicii", "Wake Me Up"), ("Rick Astley", "Never Gonna Give You Up"), ("Shakira", "Hips Don't Lie"),
    ],
    "rock": [
        ("Arctic Monkeys", "Do I Wanna Know?"), ("Arctic Monkeys", "505"), ("Linkin Park", "Numb"),
        ("AC/DC", "Back In Black"), ("Guns N' Roses", "Sweet Child O' Mine"), ("Red Hot Chili Peppers", "Californication"),
        ("Foo Fighters", "Everlong"), ("Green Day", "Boulevard of Broken Dreams"), ("Imagine Dragons", "Believer"),
        ("The White Stripes", "Seven Nation Army"), ("Metallica", "Enter Sandman"), ("Queen", "Don't Stop Me Now"),
        ("Bon Jovi", "Livin' on a Prayer"), ("System Of A Down", "Chop Suey!"), ("Paramore", "Misery Business"),
        ("My Chemical Romance", "Welcome to the Black Parade"), ("Måneskin", "Beggin'"), ("Nirvana", "Come As You Are"),
        ("Fall Out Boy", "Centuries"), ("The Neighbourhood", "Sweater Weather"),
    ],
    "afro": [
        ("Rema", "Calm Down"), ("Burna Boy", "Last Last"), ("Wizkid", "Essence"), ("CKay", "Love Nwantiti"),
        ("Asake", "Lonely At The Top"), ("Tyla", "Water"), ("Davido", "FALL"), ("Ayra Starr", "Rush"),
        ("Burna Boy", "On The Low"), ("Fireboy DML", "Peru"), ("Omah Lay", "Soso"), ("Tems", "Me & U"),
        ("Rema", "Dumebi"), ("Wizkid", "Come Closer"), ("Kizz Daniel", "Buga"), ("Uncle Waffles", "Tanzania"),
    ],
    "edm": [
        ("Avicii", "Levels"), ("Swedish House Mafia", "Don't You Worry Child"), ("David Guetta", "Titanium"),
        ("Calvin Harris", "Summer"), ("Martin Garrix", "Animals"), ("Kygo", "Firestone"), ("Alan Walker", "Faded"),
        ("Marshmello", "Alone"), ("The Chainsmokers", "Closer"), ("Fred again..", "Delilah (pull me out of this)"),
        ("Fisher", "Losing It"), ("Calvin Harris", "Feel So Close"), ("Zedd", "Clarity"), ("Tiësto", "The Business"),
        ("Skrillex", "Bangarang"), ("Daft Punk", "Get Lucky"),
    ],
    "latin": [
        ("Bad Bunny", "Tití Me Preguntó"), ("Bad Bunny", "Me Porto Bonito"), ("Bad Bunny", "Monaco"),
        ("Bad Bunny & Jhay Cortez", "Dákiti"), ("Daddy Yankee", "Gasolina"), ("Daddy Yankee", "Con Calma"),
        ("Luis Fonsi", "Despacito"), ("J Balvin", "Mi Gente"), ("Karol G", "Provenza"), ("Karol G & Shakira", "TQG"),
        ("Rauw Alejandro", "Todo de Ti"), ("Feid", "Normal"), ("Eslabon Armado & Peso Pluma", "Ella Baila Sola"),
        ("Shakira", "Hips Don't Lie"), ("DJ Snake", "Taki Taki"),
    ],
    "uk": [
        ("Stormzy", "Shut Up"), ("Stormzy", "Vossi Bop"), ("Skepta", "Shutdown"), ("Dave", "Location"),
        ("Dave", "Funky Friday"), ("AJ Tracey", "Ladbroke Grove"), ("Aitch", "Taste (Make It Shake)"),
        ("Wiley", "Wearing My Rolex"), ("Dizzee Rascal", "Bonkers"), ("Dizzee Rascal", "Fix Up, Look Sharp"),
        ("J Hus", "Did You See"), ("Little Simz", "Venom"),
    ],
    "indie": [
        ("Arctic Monkeys", "Do I Wanna Know?"), ("Arctic Monkeys", "505"), ("The Neighbourhood", "Sweater Weather"),
        ("Tame Impala", "The Less I Know the Better"), ("Glass Animals", "Heat Waves"), ("The Strokes", "Reptilia"),
        ("Cage the Elephant", "Ain't No Rest for the Wicked"), ("Foster the People", "Pumped Up Kicks"),
        ("MGMT", "Electric Feel"), ("Hozier", "Take Me to Church"), ("Lana Del Rey", "Summertime Sadness"),
        ("Mitski", "My Love Mine All Mine"), ("Phoebe Bridgers", "Motion Sickness"),
        ("Cigarettes After Sex", "Apocalypse"), ("Mac DeMarco", "Chamber of Reflection"),
    ],
    "metal": [
        ("Metallica", "Enter Sandman"), ("Metallica", "Master of Puppets"), ("Slipknot", "Duality"),
        ("Slipknot", "Psychosocial"), ("System Of A Down", "Chop Suey!"), ("System Of A Down", "Toxicity"),
        ("Iron Maiden", "The Trooper"), ("Black Sabbath", "Paranoid"), ("Rammstein", "Du Hast"),
        ("Rammstein", "Sonne"), ("Pantera", "Walk"), ("Avenged Sevenfold", "Hail to the King"),
        ("Bring Me the Horizon", "Throne"), ("Disturbed", "Down with the Sickness"),
    ],
    "country": [
        ("Morgan Wallen", "Last Night"), ("Morgan Wallen", "Whiskey Glasses"), ("Luke Combs", "Fast Car"),
        ("Zach Bryan", "Something in the Orange"), ("Zach Bryan", "I Remember Everything"),
        ("Shaboozey", "A Bar Song (Tipsy)"), ("Lil Nas X", "Old Town Road"), ("Chris Stapleton", "Tennessee Whiskey"),
        ("Dolly Parton", "Jolene"), ("Johnny Cash", "Ring of Fire"), ("Kenny Rogers", "The Gambler"),
        ("Beyoncé", "Texas Hold 'Em"),
    ],
    "reggae": [
        ("Bob Marley", "Three Little Birds"), ("Bob Marley", "Could You Be Loved"), ("Bob Marley", "Is This Love"),
        ("Sean Paul", "Temperature"), ("Sean Paul", "Get Busy"), ("Sean Paul", "Gimme the Light"),
        ("Shaggy", "It Wasn't Me"), ("Shaggy", "Boombastic"), ("Vybz Kartel", "Fever"),
        ("Damian Marley", "Welcome to Jamrock"), ("Skillibeng", "Whap Whap"),
    ],
    "disco": [
        ("Bee Gees", "Stayin' Alive"), ("Earth, Wind & Fire", "September"), ("Donna Summer", "I Feel Love"),
        ("Gloria Gaynor", "I Will Survive"), ("Chic", "Le Freak"), ("ABBA", "Dancing Queen"),
        ("Michael Jackson", "Don't Stop 'Til You Get Enough"), ("Kool & The Gang", "Celebration"),
        ("Sister Sledge", "We Are Family"), ("Rick James", "Super Freak"), ("Daft Punk", "Get Lucky"),
        ("Mark Ronson", "Uptown Funk"), ("Dua Lipa", "Levitating"),
    ],
    "jazz": [
        ("Frank Sinatra", "Fly Me to the Moon"), ("Dave Brubeck", "Take Five"), ("Miles Davis", "So What"),
        ("Louis Armstrong", "What a Wonderful World"), ("Nina Simone", "Feeling Good"),
        ("John Coltrane", "My Favorite Things"), ("Chet Baker", "My Funny Valentine"),
        ("Duke Ellington", "Take the A Train"), ("Etta James", "At Last"), ("Norah Jones", "Don't Know Why"),
        ("Herbie Hancock", "Cantaloupe Island"),
    ],
    "anime": [
        ("Linked Horizon", "Guren no Yumiya"), ("LiSA", "Gurenge"), ("YOASOBI", "Idol"), ("King Gnu", "SPECIALZ"),
        ("Kenshi Yonezu", "KICK BACK"), ("TK from Ling tosite sigure", "Unravel"), ("Ikimonogakari", "Blue Bird"),
        ("FLOW", "GO!!!"), ("FLOW", "Sign"), ("Eve", "Kaikai Kitan"), ("Porno Graffitti", "The Day"),
    ],
    "classical": [
        ("Beethoven", "Für Elise"), ("Beethoven", "Symphony No. 5"), ("Mozart", "Eine kleine Nachtmusik"),
        ("Vivaldi", "The Four Seasons: Spring"), ("Bach", "Toccata and Fugue in D minor"), ("Debussy", "Clair de Lune"),
        ("Tchaikovsky", "Swan Lake"), ("Pachelbel", "Canon in D"), ("Grieg", "In the Hall of the Mountain King"),
        ("Chopin", "Nocturne Op. 9 No. 2"), ("Satie", "Gymnopédie No. 1"), ("Carl Orff", "O Fortuna"),
        ("Johann Strauss II", "The Blue Danube"), ("Rossini", "William Tell Overture"),
    ],
}

# Radio-only moods: endless search seeds rather than a fixed list.
RADIO_SEARCHES: dict[str, list[str]] = {
    "lofi": ["lofi hip hop beat", "chill lofi study beat", "lofi jazz hop", "rainy lofi beat", "late night lofi"],
    "phonk": ["drift phonk", "brazilian phonk", "phonk house", "aggressive phonk"],
    "chill": ["chill acoustic song", "chill indie pop song", "chill rnb song"],
    "gym": ["gym workout rap song", "hardstyle gym song", "workout motivation song", "gym phonk"],
    "sad": ["sad rap song", "sad pop song", "heartbreak song", "sad acoustic song"],
    "party": ["party anthem song", "club banger song", "party hit song", "dance party song"],
    "techno": ["peak time techno track", "hard techno track", "melodic techno track", "berlin techno track"],
    "house": ["deep house track", "tech house track", "afro house track", "classic house track"],
    "dnb": ["drum and bass track", "liquid drum and bass", "jump up drum and bass", "neurofunk track"],
    "amapiano": ["amapiano song", "amapiano hit", "private school amapiano", "amapiano dance"],
    "jerseyclub": ["jersey club song", "jersey club remix", "jersey club hit"],
    "balkan": ["balkan rap song", "balkan pop hit", "balkan trap song", "narodna muzika hit"],
    "turkish": ["türkçe rap şarkı", "turkish pop song", "türkçe pop hit"],
    "arabic": ["arabic pop song", "arabic rap song", "khaleeji song", "rai song"],
    "bollywood": ["bollywood hit song", "punjabi song", "bollywood dance song", "hindi romantic song"],
    "synthwave": ["synthwave track", "retrowave song", "outrun synthwave", "darksynth track"],
    "nightcore": ["nightcore song", "sped up song", "nightcore remix"],
    "gaming": ["video game soundtrack", "gaming music mix song", "boss battle music", "minecraft soundtrack"],
    "sleep": ["sleep ambient music", "calm piano music", "ambient soundscape", "rain sleep music"],
    "christmas": ["christmas song", "classic christmas song", "christmas pop song", "christmas jazz song"],
    "french": ["rap français son", "french pop song", "chanson française", "rap fr hit"],
    "italian": ["canzone italiana", "rap italiano hit", "italian pop song", "musica italiana"],
    "brazilian": ["funk brasileiro", "brazilian funk song", "sertanejo hit", "mpb song"],
    "persian": ["persian pop song", "آهنگ ایرانی", "iranian song", "persian rap song"],
    # Rap
    "trap": ["trap rap song", "atlanta trap hit", "rage rap song", "trap banger"],
    "oldschool": ["90s hip hop classic", "boom bap rap song", "golden era hip hop", "east coast rap classic"],
    "emorap": ["emo rap song", "sad emo rap", "soundcloud rap song", "emo trap song"],
    # Pop
    "hyperpop": ["hyperpop song", "digicore song", "glitchcore song", "hyperpop hit"],
    "jpop": ["j-pop song", "japanese pop hit", "city pop song", "j-pop hit"],
    "cpop": ["c-pop song", "mandopop hit", "chinese pop song", "华语流行歌曲"],
    "tiktok": ["tiktok viral song", "viral tiktok hit", "trending tiktok song", "tiktok dance song"],
    # Rock
    "punk": ["punk rock song", "pop punk song", "skate punk song", "classic punk song"],
    "emo": ["emo song", "pop punk emo anthem", "2000s emo song", "midwest emo song"],
    "grunge": ["grunge song", "90s grunge classic", "post grunge song", "alternative rock 90s"],
    # Dance
    "hardstyle": ["hardstyle track", "rawstyle track", "euphoric hardstyle", "hardstyle anthem"],
    "dubstep": ["dubstep track", "riddim dubstep", "melodic dubstep", "brostep track"],
    "trance": ["trance anthem", "uplifting trance track", "psytrance track", "progressive trance"],
    "ukgarage": ["uk garage track", "2 step garage song", "speed garage track", "uk garage classic"],
    # Soul & more
    "soul": ["soul classic song", "motown hit", "neo soul song", "classic soul song"],
    "funk": ["funk classic song", "70s funk song", "p-funk song", "funk groove song"],
    "blues": ["blues song", "electric blues classic", "delta blues song", "blues rock song"],
    # Around the world
    "spanish": ["rap español canción", "spanish pop song", "música urbana española", "pop español hit"],
    "polish": ["polski rap", "polish pop song", "polska piosenka hit", "polski hip hop"],
    "greek": ["greek song", "ελληνικά τραγούδια", "greek pop hit", "greek rap song"],
    "dutch": ["nederlandse rap", "dutch pop song", "nederlandstalige hit", "dutch rap song"],
    # Decades
    "70s": ["70s hit song", "1970s classic song", "70s pop hit", "70s rock classic"],
    "80s": ["80s hit song", "1980s classic song", "80s pop hit", "80s synth pop song"],
    "90s": ["90s hit song", "1990s classic song", "90s pop hit", "90s dance hit"],
    "2000s": ["2000s hit song", "2000s pop hit", "2000s rap hit", "2000s rnb hit"],
    "2010s": ["2010s hit song", "2010s pop hit", "2010s rap hit", "2010s dance hit"],
    # Moods
    "love": ["love song", "romantic song", "love rnb song", "romantic pop ballad"],
    "summer": ["summer hit song", "summer vibes song", "beach party song", "summer pop song"],
    "roadtrip": ["road trip song", "driving song", "sing along road trip song", "night drive song"],
    "halloween": ["halloween song", "spooky song", "halloween party song", "horror theme music"],
}

# What belongs on each station, for the AI that picks songs (and keeps the wrong genre out).
STATION_BRIEFS: dict[str, str] = {
    "rap": "mainstream US hip-hop hits and classics", "drill": "UK and US drill (Chicago, NY, UK drill)",
    "uk": "UK rap and grime", "deutschrap": "German-language rap (Deutschrap)", "phonk": "phonk and drift phonk",
    "pop": "global pop hits", "rnb": "R&B, old and new",
    "latin": "reggaeton, Latin trap and Latin pop", "afro": "Afrobeats and Afropop", "anime": "anime openings, "
    "endings and J-pop/J-rock used in anime", "rock": "rock classics and modern rock", "metal": "heavy metal, "
    "nu metal, metalcore", "indie": "indie and alternative", "country": "country, old and new",
    "reggae": "reggae and dancehall", "edm": "EDM: big-room, electro house, festival dance",
    "house": "house music (deep, tech, afro, classic house)", "techno": "techno", "dnb": "drum and bass",
    "amapiano": "amapiano from South Africa", "jerseyclub": "Jersey club", "disco": "disco and funk",
    "synthwave": "synthwave and retrowave", "nightcore": "nightcore and sped-up versions of popular songs",
    "balkan": "Balkan pop and rap (ex-Yugoslav, Albanian, Bulgarian)", "turkish": "Turkish pop and rap",
    "arabic": "Arabic pop and rap", "bollywood": "Bollywood and Punjabi songs", "french": "French rap and pop (rap français, variété)", "italian": "Italian pop and "
    "rap", "brazilian": "Brazilian funk, sertanejo, MPB and Brazilian pop", "persian": "Persian (Iranian) pop and "
    "rap", "throwback": "2000s and 2010s hits everyone knows", "jazz": "jazz standards and modern jazz",
    "classical": "classical music: well-known pieces", "chill": "chill, laid-back songs (indie, R&B, acoustic)",
    "lofi": "lo-fi hip-hop beats to study to", "sad": "sad songs: heartbreak rap, sad pop, emotional R&B",
    "gym": "hype workout songs: hard rap, phonk, rock anthems", "party": "party and club bangers",
    "gaming": "video game soundtracks and gaming anthems", "sleep": "calm ambient and piano music for sleeping",
    "christmas": "Christmas songs, classic and modern",
    "trap": "trap: Atlanta trap, rage, modern trap bangers", "oldschool": "90s and early 2000s hip-hop: boom bap, "
    "golden era, East and West Coast classics", "emorap": "emo rap and SoundCloud rap (Lil Peep, Juice WRLD style)",
    "hyperpop": "hyperpop and digicore", "jpop": "Japanese pop (J-pop) and city pop, not only anime songs",
    "cpop": "Chinese-language pop (Mandopop, Cantopop, C-pop)", "tiktok": "songs that went viral on TikTok",
    "punk": "punk rock and pop punk", "emo": "emo and pop-punk anthems (2000s emo, midwest emo)",
    "grunge": "grunge and 90s alternative rock", "hardstyle": "hardstyle and rawstyle",
    "dubstep": "dubstep (brostep, riddim, melodic dubstep)", "trance": "trance (uplifting, progressive, psytrance)",
    "ukgarage": "UK garage and 2-step", "soul": "soul, Motown and neo-soul", "funk": "funk classics and grooves",
    "blues": "blues and blues rock", "spanish": "music from Spain: Spanish rap, pop and urbano (not Latin American)",
    "polish": "Polish rap and pop", "greek": "Greek pop, laïko and Greek rap", "dutch": "Dutch-language rap and pop",
    "70s": "hits from the 1970s", "80s": "hits from the 1980s", "90s": "hits from the 1990s",
    "2000s": "hits from the 2000s", "2010s": "hits from the 2010s", "love": "love songs: romantic pop, R&B and ballads",
    "summer": "summer hits and feel-good beach songs", "roadtrip": "road trip and driving sing-alongs",
    "halloween": "Halloween and spooky songs",
}
GENRE_ALIASES = {"uk drill": "drill", "ukdrill": "drill", "german": "deutschrap", "german rap": "deutschrap",
                 "deutsch": "deutschrap", "hiphop": "rap", "hip-hop": "rap", "hip hop": "rap", "r&b": "rnb",
                 "randb": "rnb", "old": "throwback", "oldies": "throwback", "classics": "throwback", "90s": "90s",
                 "00s": "2000s", "afrobeats": "afro", "dance": "edm", "electronic": "edm", "lo-fi": "lofi",
                 "study": "lofi", "reggaeton": "latin", "espanol": "spanish", "grime": "uk", "uk rap": "uk", "ukrap": "uk", "british": "uk", "alternative": "indie", "alt": "indie",
                 "heavy metal": "metal", "rockmetal": "metal", "dancehall": "reggae", "70s funk": "funk", "seventies": "70s",
                 "eighties": "80s", "bluesrock": "blues", "japanese": "jpop", "j pop": "jpop", "j-pop": "jpop",
                 "orchestra": "classical", "piano": "classical", "workout": "gym", "training": "gym",
                 "sad songs": "sad", "heartbreak": "sad", "club": "party", "drum and bass": "dnb",
                 "drum & bass": "dnb", "d&b": "dnb", "jersey": "jerseyclub", "jersey club": "jerseyclub",
                 "ex-yu": "balkan", "yugo": "balkan", "turkce": "turkish", "türkçe": "turkish", "arab": "arabic",
                 "hindi": "bollywood", "punjabi": "bollywood", "desi": "bollywood", "retrowave": "synthwave",
                 "sped up": "nightcore", "spedup": "nightcore", "games": "gaming", "game": "gaming",
                 "ost": "gaming", "ambient": "sleep", "xmas": "christmas", "weihnachten": "christmas",
                 "france": "french", "francais": "french", "français": "french",
                 "rap fr": "french", "fr": "french", "italy": "italian", "italiano": "italian", "brazil": "brazilian",
                 "brasil": "brazilian", "funk brasileiro": "brazilian", "baile funk": "brazilian",
                 "iranian": "persian", "farsi": "persian", "iran": "persian",
                 "atl": "trap", "rage": "trap", "boom bap": "oldschool", "old school": "oldschool",
                 "90s rap": "oldschool", "90s hip hop": "oldschool", "emo rap": "emorap", "soundcloud": "emorap",
                 "digicore": "hyperpop", "city pop": "jpop", "chinese": "cpop", "c-pop": "cpop", "mandopop": "cpop",
                 "viral": "tiktok", "trending": "tiktok", "pop punk": "punk", "pop-punk": "punk", "midwest emo": "emo",
                 "rawstyle": "hardstyle", "riddim": "dubstep", "brostep": "dubstep", "psytrance": "trance",
                 "garage": "ukgarage", "uk garage": "ukgarage", "2step": "ukgarage", "2-step": "ukgarage",
                 "motown": "soul", "neo soul": "soul", "español": "spanish", "spain": "spanish",
                 "poland": "polish", "polski": "polish", "greece": "greek", "ελληνικά": "greek",
                 "netherlands": "dutch", "nederlands": "dutch", "holland": "dutch", "1970s": "70s",
                 "1980s": "80s", "1990s": "90s", "nineties": "90s", "2000": "2000s", "noughties": "2000s",
                 "2010": "2010s", "10s": "2010s", "romantic": "love", "love songs": "love", "beach": "summer",
                 "road trip": "roadtrip", "driving": "roadtrip", "drive": "roadtrip", "spooky": "halloween"}


def resolve_genre(text: str) -> str | None:
    key = (text or "").strip().lower()
    key = GENRE_ALIASES.get(key, key)
    return key if key in GENRES or key in RADIO_SEARCHES else None


def all_genres() -> list[str]:
    return list(GENRES) + list(RADIO_SEARCHES)


# How the station list is shown: (heading, stations).
STATION_GROUPS: list[tuple[str, list[str]]] = [
    ("🎤 Rap", ["rap", "trap", "drill", "uk", "deutschrap", "phonk", "oldschool", "emorap"]),
    ("🎧 Pop & R&B", ["pop", "rnb", "jpop", "cpop", "latin", "afro", "anime", "hyperpop", "tiktok"]),
    ("🎸 Rock & more", ["rock", "metal", "punk", "emo", "grunge", "indie", "country", "reggae"]),
    ("🪩 Dance", ["edm", "house", "techno", "dnb", "hardstyle", "dubstep", "trance", "ukgarage", "amapiano",
                 "jerseyclub", "disco", "synthwave", "nightcore"]),
    ("🎷 Soul & classics", ["soul", "funk", "blues", "jazz", "classical", "throwback"]),
    ("📅 Decades", ["70s", "80s", "90s", "2000s", "2010s"]),
    ("🌍 Around the world", ["balkan", "turkish", "arabic", "persian", "bollywood", "french", "italian",
                            "spanish", "brazilian", "polish", "greek", "dutch"]),
    ("✨ Moods", ["chill", "lofi", "sad", "love", "gym", "party", "summer", "roadtrip", "gaming", "sleep",
                 "halloween", "christmas"]),
]

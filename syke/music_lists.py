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
}

# Radio-only moods: endless search seeds rather than a fixed list.
RADIO_SEARCHES: dict[str, list[str]] = {
    "lofi": ["lofi hip hop beat", "chill lofi study beat", "lofi jazz hop", "rainy lofi beat", "late night lofi"],
    "phonk": ["drift phonk", "brazilian phonk", "phonk house", "aggressive phonk"],
    "chill": ["chill acoustic song", "chill indie pop song", "chill rnb song"],
}
GENRE_ALIASES = {"uk drill": "drill", "ukdrill": "drill", "german": "deutschrap", "german rap": "deutschrap",
                 "deutsch": "deutschrap", "hiphop": "rap", "hip-hop": "rap", "hip hop": "rap", "r&b": "rnb",
                 "randb": "rnb", "old": "throwback", "oldies": "throwback", "classics": "throwback", "90s": "throwback",
                 "00s": "throwback", "afrobeats": "afro", "dance": "edm", "electronic": "edm", "lo-fi": "lofi",
                 "study": "lofi"}


def resolve_genre(text: str) -> str | None:
    key = (text or "").strip().lower()
    key = GENRE_ALIASES.get(key, key)
    return key if key in GENRES or key in RADIO_SEARCHES else None


def all_genres() -> list[str]:
    return list(GENRES) + list(RADIO_SEARCHES)

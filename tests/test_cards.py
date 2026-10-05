import asyncio
from datetime import timezone

from syke.cards import ProfileView, archetype, bar, build_pages, colour_for
from syke.demo import build_demo_server
from syke.profile import analyze_server, build_profile


def demo_profile(user_id: int):
    server = analyze_server(build_demo_server(), timezone.utc, min_messages=15)
    profile = build_profile(server, user_id)
    profile.summary = "First paragraph.\n\nSecond *paragraph*."
    return profile


def test_pages_fit_discord_limits():
    for uid in range(1, 6):
        pages = build_pages(demo_profile(uid), "https://example.com/a.png", demo=True)
        assert list(pages) == ["overview", "report", "highlights", "ranks"]
        for embed in pages.values():
            assert len(embed) <= 6000
            assert len(embed.fields) <= 25
            assert all(len(f.value) <= 1024 and len(f.name) <= 256 for f in embed.fields)
            assert "demo mode" in embed.footer.text


def test_overview_is_compact_and_report_has_everything():
    profile = demo_profile(1)
    pages = build_pages(profile)
    page = pages["overview"]
    assert page.title == "Zyro"
    assert page.description.startswith(f"**{archetype(profile)[0]}**")
    assert "> First paragraph.\n" in page.description and "Second" not in page.description
    assert "💬 " in page.description and "msgs" in page.description
    assert page.description.count("%") == 3 and not page.fields
    assert len(page) < len(pages["report"]) * 0.7
    assert page.colour == colour_for(profile)

    report = pages["report"]
    assert "> First paragraph.\n>\n> Second *paragraph*." in report.description
    names = [f.name for f in report.fields]
    assert names[:6] == ["💬 Messages", "📅 Active since", "🕐 Peak hours", "✍️ Avg length", "😀 Top emojis", "🗣️ Catchphrase"]
    assert names[-1] == "🎭 Personality" and not report.fields[-1].inline


def test_short_summary_cuts_at_a_sentence():
    from syke.cards import short_summary

    assert short_summary("One. Two.\n\nThree.") == "One. Two."
    long = "This is a sentence. " * 30
    cut = short_summary(long, limit=100)
    assert len(cut) <= 100 and cut.endswith(".")
    assert short_summary("x" * 300, limit=50).endswith("…")


def test_highlights_quote_and_escape():
    profile = demo_profile(1)
    profile.highlights["funniest"] = profile.messages[0].__class__(1, "Zyro", "**bold** ``` @everyone", profile.messages[0].created_at)
    profile.highlights["toxic"] = None
    page = build_pages(profile)["highlights"]
    fields = {f.name: f.value for f in page.fields}
    assert fields["😂 Funniest message"].startswith("> \\*\\*bold\\*\\*")
    assert fields["☠️ Most toxic moment"] == "*Somehow, a saint.*"


def test_bar():
    assert bar(0) == "░" * 10 and bar(100) == "█" * 10 and bar(55).count("█") == 6


def test_view_switches_pages_for_owner_only():
    async def go():
        pages = build_pages(demo_profile(2))
        cards = {55: (7, pages)}
        view = ProfileView(cards.get)
        assert view.timeout is None and view.is_persistent()
        buttons = {b.label: b for b in view.children}
        assert buttons["Overview"].disabled and not buttons["Ranks"].disabled

        edits = []

        class Resp:
            async def edit_message(self, **kw):
                edits.append(kw)

            async def send_message(self, *args, **kw):
                edits.append(("ephemeral", args, kw))

        class Inter:
            def __init__(self, uid, message_id=55):
                self.user = type("U", (), {"id": uid})()
                self.response = Resp()
                self.message = type("M", (), {"id": message_id})()

        await buttons["Ranks"].callback(Inter(7))
        assert edits[-1]["embed"] is pages["ranks"]
        flipped = {b.label: b for b in edits[-1]["view"].children}
        assert flipped["Ranks"].disabled and not flipped["Overview"].disabled

        await buttons["Highlights"].callback(Inter(8))
        assert edits[-1][0] == "ephemeral" and edits[-1][2]["embed"] is pages["highlights"]

        restarted = ProfileView(cards.get)
        await restarted.children[2].callback(Inter(7))
        assert edits[-1]["embed"] is pages["highlights"]

        await buttons["Ranks"].callback(Inter(7, message_id=999))
        assert edits[-1][0] == "ephemeral" and "expired" in edits[-1][1][0]

    asyncio.run(go())


def test_cards_survive_a_restart(tmp_path):
    from syke.storage import Storage

    pages = build_pages(demo_profile(2))
    Storage(str(tmp_path / "c.db")).save_card(1, 7, {k: e.to_dict() for k, e in pages.items()})
    owner, stored = Storage(str(tmp_path / "c.db")).card(1)
    assert owner == 7 and stored["ranks"]["title"] == pages["ranks"].title
    assert Storage(str(tmp_path / "c.db")).card(2) is None

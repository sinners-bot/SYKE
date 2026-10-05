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
        assert list(pages) == ["overview", "highlights", "ranks"]
        for embed in pages.values():
            assert len(embed) <= 6000
            assert len(embed.fields) <= 25
            assert all(len(f.value) <= 1024 and len(f.name) <= 256 for f in embed.fields)
            assert "demo mode" in embed.footer.text


def test_overview_layout():
    profile = demo_profile(1)
    page = build_pages(profile)["overview"]
    assert page.title == "Zyro"
    assert page.description.startswith(f"**{archetype(profile)[0]}**")
    assert "> First paragraph.\n>\n> Second *paragraph*." in page.description
    names = [f.name for f in page.fields]
    assert names[:6] == ["💬 Messages", "📅 Active since", "🕐 Peak hours", "✍️ Avg length", "😀 Top emojis", "🗣️ Catchphrase"]
    assert names[-1] == "🎭 Personality" and not page.fields[-1].inline
    assert page.colour == colour_for(profile)


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
        view = ProfileView(pages, owner_id=7)
        buttons = {b.label: b for b in view.children}
        assert buttons["Overview"].disabled and not buttons["Ranks"].disabled

        edits = []

        class Resp:
            async def edit_message(self, **kw):
                edits.append(kw)

            async def send_message(self, **kw):
                edits.append(("ephemeral", kw))

        class Inter:
            def __init__(self, uid, page):
                self.user = type("U", (), {"id": uid})()
                self.response = Resp()
                self.data = {"custom_id": f"syke:{page}"}

        await buttons["Ranks"].callback(Inter(7, "ranks"))
        assert edits[-1]["embed"] is pages["ranks"] and buttons["Ranks"].disabled
        assert not await view.interaction_check(Inter(8, "highlights"))
        assert edits[-1][0] == "ephemeral" and edits[-1][1]["embed"] is pages["highlights"]

    asyncio.run(go())

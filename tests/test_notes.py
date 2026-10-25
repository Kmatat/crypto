"""The desk's notebook: Markdown rendered from the journal, diary events, weekly snapshots, the zip, the review."""
import sys
import threading
import zipfile
from types import SimpleNamespace

from trader.agents.research import ResearchAgent, review_text
from trader.config import Settings
from trader.journal import Journal
from trader.lab import Lab
from trader.notes import Notes, card_periods, champion_at, default_dir, lineage_text, replicate_command
from trader.notify import MAX_LEN, Telegram
from trader.orchestrator import Orchestrator
from trader.strategy import StrategyCard
from tests.test_lab import fake_fetch


def desk(tmp_path, cycles=120):
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], notes_dir=str(tmp_path / "notes"), mentor_checks=())
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    for _ in range(cycles):
        bot.run_cycle()
    return bot


def test_default_dir_sits_next_to_the_journal():
    assert default_dir(Settings(), "/x/data/journal.db") == "/x/data/notes"
    assert default_dir(Settings(), "/x/data/journal_offline.db") == "/x/data/notes_offline"
    assert default_dir(Settings(notes_dir="/n"), ":memory:") == "/n" and default_dir(Settings(), ":memory:") is None


def test_notebook_is_rebuilt_from_the_journal_after_trades(tmp_path):
    bot = desk(tmp_path)
    trades = bot.journal.db.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
    assert trades > 0
    d = tmp_path / "notes"
    assert {"README.md", "STRATEGY.md", "TRADES.md", "DIARY.md", "PERFORMANCE.md", "strategies"} <= {p.name for p in d.iterdir()}
    trades_md = (d / "TRADES.md").read_text()
    assert f"{trades} closed trades" in trades_md
    assert "Decision at entry: BUY" in trades_md and "Agents at entry: technical" in trades_md
    strategy = (d / "STRATEGY.md").read_text()
    champ = bot.card
    assert f"[{champ.id}]" in strategy and "python -m trader backtest" in strategy and "Allowed range" in strategy
    assert (d / "strategies" / f"{champ.id}.md").exists()
    diary = (d / "DIARY.md").read_text()
    assert "trade: BUY" in diary and "trade: SELL" in diary and "strategy: First champion" in diary
    perf = (d / "PERFORMANCE.md").read_text()
    assert "Week by week" in perf and "Profit factor to date" in perf
    readme = (d / "README.md").read_text()
    assert "Fixed guardrails" in readme and "How to use this with Claude" in readme


def test_diary_and_lineage_follow_the_laboratory(tmp_path):
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], lab_alt_symbols=("BNBUSDT", "XRPUSDT"),
                 notes_dir=str(tmp_path / "n"))
    j = Journal(":memory:", 10_000)
    lab = Lab(s, j, client=None, fetch=fake_fetch)
    champ = lab.champion()
    cards = ResearchAgent().neighbours(champ, 2, set())
    lab.run_week(cards)
    kinds = [e["kind"] for e in j.events()]
    assert kinds[0] == "strategy" and kinds.count("challenger") == 2 and kinds.count("lab") >= 3
    text = lineage_text(j)
    assert "Already tried" in text and all(f"[{c.id}]" in text for c in cards) and "exam mean" in text

    seen = {}

    class LLM:  # the research agent hands the lineage to Claude so it does not repeat itself
        def research(self, *a, **k):
            return None

        def ask(self, system, prompt, schema, **k):
            seen["prompt"] = prompt
            return {"proposals": [], "ideas_needing_code": []}
    ResearchAgent(LLM()).propose(champ, {"days": 30}, 2, {c.id for c in cards}, lineage=text)
    assert "already tried" in seen["prompt"] and f"[{cards[0].id}]" in seen["prompt"]

    notes = Notes(j, tmp_path / "n", s)
    written = notes.refresh()
    assert len([p for p in written if p.parent.name == "strategies"]) == 3   # champion + 2 challengers
    strategy = (tmp_path / "n" / "STRATEGY.md").read_text()
    assert ("failed the exam" in strategy or "passed the exam" in strategy) and "## Lineage" in strategy
    assert lab.reject(cards[0].id) and j.events()[-1]["kind"] == "owner"


def test_trade_history_knows_which_strategy_was_live():
    hist = [{"ts": "2026-01-01T00:00:00+00:00", "event": "promote", "id": "a", "name": "A"},
            {"ts": "2026-02-01T00:00:00+00:00", "event": "promote", "id": "b", "name": "B"},
            {"ts": "2026-02-01T00:00:00+00:00", "event": "rollback", "id": "b", "name": "B"},
            {"ts": "2026-03-01T00:00:00+00:00", "event": "promote", "id": "a", "name": "A"}]
    assert champion_at(hist, "2026-01-15T00:00:00+00:00")["id"] == "a"
    assert champion_at(hist, "2026-02-15T00:00:00+00:00")["id"] == "b"
    assert champion_at(hist, "2025-12-01T00:00:00+00:00") is None
    assert card_periods(hist, "a") == [("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),
                                       ("2026-03-01T00:00:00+00:00", None)]
    cmd = replicate_command(StrategyCard("x", interval="4h", risk_per_trade_pct=0.005),
                            Settings(symbols=["BTCUSDT", "SOLUSDT"]))
    assert "--interval 4h" in cmd and "--risk 0.5" in cmd and "--symbols BTC,SOL" in cmd and "--sell -0.35" in cmd


def test_weekly_snapshot_is_written_once_and_the_bundle_zips_everything(tmp_path):
    bot = desk(tmp_path, cycles=30)
    p = bot.notes.weekly_snapshot("2026-W01", [("Laboratory", "lab text"), ("Empty", "")])
    assert p.read_text().count("## ") == 1 and "lab text" in p.read_text()
    bot.notes.weekly_snapshot("2026-W01", [("Laboratory", "changed")])
    assert "changed" not in p.read_text()                                   # never rewritten
    z = bot.notes.bundle()
    names = zipfile.ZipFile(z).namelist()
    assert "notes/README.md" in names and "notes/weekly/2026-W01.md" in names
    assert any(n.startswith("notes/strategies/") for n in names)
    z.unlink()
    assert Notes(bot.journal, "/proc/not-writable/notes", bot.base).refresh() is None   # never raises into the loop
    off = Notes(bot.journal, None, bot.base)
    assert not off.enabled and off.refresh() is None and off.bundle() is None


def test_digest_and_review_text(tmp_path):
    bot = desk(tmp_path, cycles=40)
    digest = bot.notes.digest(bot.last_price, scorecard="scorecard here")
    assert "# Strategy" in digest and "# Performance" in digest and "scorecard here" in digest and "# Recent diary" in digest
    assert len(bot.notes.digest(max_chars=500)) <= 520
    seen = {}

    class LLM:
        def ask(self, system, prompt, schema, **k):
            seen.update(system=system, prompt=prompt)
            return {"summary": "Too early to tell.", "working": ["stops"], "failing": [],
                    "corrections_within_bounds": ["try 4h"], "ideas_needing_code": [], "risks": ["small sample"],
                    "evidence_quality": "12 trades only"}
    data = ResearchAgent(LLM()).review(digest)
    assert "cannot change anything" in seen["system"] and "# Strategy" in seen["prompt"]
    text = review_text(data)
    assert "Too early to tell." in text and "• try 4h" in text and "Nothing changes from this review" in text
    assert ResearchAgent(None).review(digest) is None


def test_journal_events_and_cross_thread_use():
    j = Journal(":memory:", 10_000)
    j.event("start", "hello", {"a": 1})
    j.event("trade", "BUY X")
    ev = j.events()
    assert [e["kind"] for e in ev] == ["start", "trade"] and ev[0]["data"] == {"a": 1} and ev[1]["data"] is None
    assert len(j.events(since=ev[0]["ts"])) == 2
    out = []
    t = threading.Thread(target=lambda: out.append(len(j.events())))    # /lab and /review use the journal from threads
    t.start()
    t.join()
    assert out == [2]


def test_send_long_splits_on_lines(monkeypatch):
    tg = Telegram("t", "1")
    sent = []
    monkeypatch.setattr(tg, "send", lambda text: sent.append(text) or True)
    assert tg.send_long("\n".join(["x" * 1000] * 10) + "\n" + "y" * 9000)
    assert all(len(c) <= MAX_LEN for c in sent) and len(sent) > 3
    assert "".join(sent).count("x") == 10_000 and "".join(sent).count("y") == 9000


def test_send_notebook_uploads_the_zip_and_cleans_up(tmp_path):
    from trader.__main__ import send_notebook
    bot = desk(tmp_path, cycles=10)
    uploads = []
    tg = SimpleNamespace(send_document=lambda path, caption: uploads.append((path.exists(), caption)) or True)
    assert send_notebook(bot, tg, "cap")
    assert uploads == [(True, "cap")] and not list(tmp_path.glob("notes-*.zip"))


def test_notes_command_rebuilds_from_a_journal_file(tmp_path, monkeypatch, capsys):
    from trader import __main__ as m
    s = Settings(offline=True, db_path=str(tmp_path / "journal.db"), notes_dir=str(tmp_path / "nb"))
    Journal(s.db_path, s.starting_balance).db.close()
    monkeypatch.setattr(m.Settings, "load", classmethod(lambda cls, offline=False: s))
    monkeypatch.setattr(sys, "argv", ["trader", "notes", "--zip"])
    m.main()
    out = capsys.readouterr().out
    assert "Notebook rebuilt" in out and "Zip:" in out and (tmp_path / "nb" / "README.md").exists()

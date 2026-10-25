"""Per-coin thresholds, the threshold study and the head trader."""
import datetime as dt
import json
import random

import pytest

from trader import lab as lab_module
from trader.agents import decision as dmod
from trader.agents.decision import DecisionAgent
from trader.agents.head import CORRELATED, NEWS_MIN_JUDGED, TILT, HeadTraderAgent, correlation
from trader.backtest import Backtest
from trader.calibration import CANDIDATES, _shrink, calibrated_card, choose, study, study_text
from trader.config import Settings
from trader.journal import Journal
from trader.lab import Lab, restore_decision_rules
from trader.orchestrator import Orchestrator
from trader.strategy import StrategyCard, summary_line, thresholds_line
from tests.test_backtest import synthetic
from tests.test_lab import TEST_WINDOWS, fake_fetch


def reports(score):  # combined score = 0.5 * score + 0.3 * score = 0.8 * score
    return {"technical": {"score": score}, "prediction": {"score": score}, "news": {"score": 0}, "forex": {"score": 0}}


def test_each_coin_can_have_its_own_threshold():
    d = DecisionAgent(coin_thresholds={"XRPUSDT": 0.45})
    assert d.run("BTCUSDT", reports(0.5), holding=False)["action"] == "BUY"                 # 0.40 >= 0.35
    r = d.run("XRPUSDT", reports(0.5), holding=False)
    assert r["action"] == "HOLD" and r["threshold"] == 0.45 and r["rules_action"] == "HOLD"   # 0.40 < 0.45
    assert d.run("XRPUSDT", reports(0.5), holding=False, threshold=0.3)["action"] == "BUY"   # head trader's value wins
    assert d.run("XRPUSDT", reports(-0.5), holding=True)["action"] == "HOLD"                # -0.40 > -0.45: keep
    assert d.run("BTCUSDT", reports(-0.5), holding=True)["action"] == "SELL"
    saved = (dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD, dict(dmod.WEIGHTS), dict(dmod.COIN_THRESHOLDS))
    try:
        StrategyCard("c", coin_thresholds={"BTCUSDT": 0.5}).apply_decision_rules()
        assert DecisionAgent().threshold_for("BTCUSDT") == 0.5 and DecisionAgent().threshold_for("ETHUSDT") == 0.35
        assert not DecisionAgent.worth_asking(0.4, False, 0.5) and DecisionAgent.worth_asking(0.4, False, 0.35)
    finally:
        restore_decision_rules(saved)
    assert dmod.COIN_THRESHOLDS == saved[3]


def test_card_with_coin_thresholds_is_one_bounded_change():
    base = StrategyCard("base")
    card = StrategyCard("mine", coin_thresholds={"BTCUSDT": 0.4, "SOLUSDT": 0.3})
    assert card.id != base.id and card.diff(base) == ["coin_thresholds"]
    assert card.describe_diff(base) == "coin thresholds: BTC 0.35 → 0.4, SOL 0.35 → 0.3"
    assert StrategyCard.from_dict(card.to_dict()) == card and card.violations() == []
    v = StrategyCard("bad", coin_thresholds={"BTCUSDT": 0.9, "GOLD": 0.3}).violations()
    assert any("outside" in x for x in v) and any("not a USDT pair" in x for x in v)
    assert "own thresholds for 2 coin(s)" in summary_line(card)
    assert thresholds_line(card) == "BTC 0.4, SOL 0.3 (others 0.35)" and thresholds_line(base) == "all coins 0.35"
    assert StrategyCard("x").weights == dmod.WEIGHTS   # a bare card equals the live rules


def test_backtest_honours_per_coin_thresholds():
    hist = {"BTCUSDT": synthetic("BTCUSDT", 2600, 1, drift=0.0003)}
    s = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=())
    loose = Backtest(s, hist, decision=DecisionAgent(coin_thresholds={"BTCUSDT": 0.15}), days=60).run()
    strict = Backtest(s, hist, decision=DecisionAgent(coin_thresholds={"BTCUSDT": 0.6}), days=60).run()
    assert loose["trades"] > strict["trades"]


def rows_for(best=0.5, best_ret=5.0, base_ret=1.0, n=40, halves=((20, 2.5, 1.0), (20, 2.5, 0.0)), pf=1.5, dd=1.0):
    """Rows for every candidate: `best` makes best_ret, the common 0.35 makes base_ret, the rest less.
    halves: (trades, best half return, common half return) for each half."""
    out = []
    for t in CANDIDATES:
        if abs(t - best) < 1e-9:
            ret, hv = best_ret, [{"trades": h[0], "return_pct": h[1]} for h in halves]
        elif abs(t - 0.35) < 1e-9:
            ret, hv = base_ret, [{"trades": h[0], "return_pct": h[2]} for h in halves]
        else:
            ret, hv = base_ret - 1, [{"trades": h[0], "return_pct": -1.0} for h in halves]
        out.append({"threshold": t, "return_pct": ret, "trades": n, "profit_factor": pf, "max_drawdown_pct": dd,
                    "halves": hv})
    return out


def test_choose_requires_profit_a_clear_edge_and_both_halves_then_shrinks():
    c = choose(rows_for(), 0.35, 10.0)
    assert c["best"] == 0.5 and c["threshold"] == 0.45 and c["edge"] == 4.0   # 3 steps x 40/60 = 2 steps
    assert "won both halves" in c["why"] and "moved only to 0.45" in c["why"]
    thin = choose(rows_for(halves=((30, 2.5, 1.0), (5, 2.5, 0.0))), 0.35, 10.0)
    assert thin["threshold"] == 0.35 and "each half" in thin["why"]            # one market phase only
    small = choose(rows_for(best_ret=1.5), 0.35, 10.0)
    assert small["threshold"] == 0.35 and "not evidence" in small["why"]       # 0.5 points is noise
    one_half = choose(rows_for(halves=((20, 2.5, 1.0), (20, -0.5, 0.0))), 0.35, 10.0)
    assert one_half["threshold"] == 0.35 and "half 2" in one_half["why"]
    loser = choose(rows_for(best_ret=-0.5, base_ret=-3.0, pf=0.8), 0.35, 10.0)
    assert loser["threshold"] == 0.35 and "made money" in loser["why"]
    assert choose(rows_for(dd=50.0), 0.35, 10.0)["threshold"] == 0.35           # too deep a drop
    few = choose(rows_for(n=15, best=0.4, halves=((15, 2.5, 1.0), (15, 2.5, 0.0))), 0.35, 10.0)
    assert few["threshold"] == 0.35 and "too few to move a full step" in few["why"]   # one step needs > 20 trades


def test_study_and_calibrated_card_on_synthetic_history(monkeypatch):
    monkeypatch.setattr("trader.calibration.CANDIDATES", [0.25, 0.35, 0.45])
    monkeypatch.setattr("trader.calibration.STUDY_DAYS", 40)
    monkeypatch.setattr("trader.calibration.HOLDOUT_DAYS", 10)
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=())
    hist = {sym: fake_fetch(None, sym, 60) for sym in ("BTCUSDT", "ETHUSDT")}
    card = StrategyCard.from_settings(s)
    before = (dmod.BUY_THRESHOLD, dict(dmod.COIN_THRESHOLDS), dict(dmod.WEIGHTS))
    result = study(card, s, hist, hist["BTCUSDT"], ["BTCUSDT", "ETHUSDT"])
    assert (dmod.BUY_THRESHOLD, dict(dmod.COIN_THRESHOLDS), dict(dmod.WEIGHTS)) == before   # live rules untouched
    assert set(result["coins"]) == {"BTCUSDT", "ETHUSDT"} and all(len(c["rows"]) == 3 for c in result["coins"].values())
    assert all(0.15 <= c["threshold"] <= 0.6 for c in result["coins"].values())
    assert "Threshold study" in study_text(result) and "• BTC" in study_text(result)
    new = calibrated_card(card, result)
    if new is None:
        assert all(abs(c["threshold"] - card.buy_threshold) < 1e-9 for c in result["coins"].values())
    else:
        assert new.diff(card) == ["coin_thresholds"] and new.source == "study" and new.violations() == []
    result["coins"]["BTCUSDT"]["threshold"] = 0.5
    forced = calibrated_card(card, result)
    assert forced.coin_thresholds["BTCUSDT"] == 0.5 and "per-coin thresholds" in forced.name


class FakeLLM:
    def __init__(self, budget, spent, cost):
        self._budget, self.spent_today, self._cost = budget, spent, cost

    def effective_daily_budget(self):
        return self._budget

    def avg_call_cost(self):
        return self._cost


def cands():
    return [{"symbol": "BTCUSDT", "holding": True, "score": -0.5, "threshold": 0.35, "eligible": True},   # position at risk
            {"symbol": "ETHUSDT", "holding": False, "score": 0.6, "threshold": 0.35, "eligible": True},    # strong buy
            {"symbol": "SOLUSDT", "holding": False, "score": 0.4, "threshold": 0.35, "eligible": True},    # weaker buy
            {"symbol": "XRPUSDT", "holding": False, "score": 0.9, "threshold": 0.35, "eligible": False},   # checklist failed
            {"symbol": "ADAUSDT", "holding": False, "score": 0.2, "threshold": 0.35, "eligible": True}]    # no signal


def test_head_plans_claude_calls_by_priority_and_paces_the_budget():
    s = Settings(cycle_minutes=15)
    j = Journal(":memory:", 10_000)
    assert HeadTraderAgent(s, j, None).plan_ai(cands()) == set()
    assert HeadTraderAgent(s, j, FakeLLM(0, 0, 0.03)).plan_ai(cands()) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}  # no cap
    noon = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)                  # 48 checks left today
    head = HeadTraderAgent(s, j, FakeLLM(5.0, 0.0, 0.05))                            # 100 calls for 48 checks: 2 each
    assert head.plan_ai(cands(), noon) == {"BTCUSDT", "ETHUSDT"} and head.last_plan["skipped"] == ["SOLUSDT"]
    head = HeadTraderAgent(s, j, FakeLLM(1.0, 0.5, 0.05))                            # 10 calls left for 48 checks
    assert head.plan_ai(cands(), noon) == {"BTCUSDT"}                                # the position at risk goes first
    assert head.plan_ai(cands(), noon + dt.timedelta(minutes=15)) == set()           # spaced out: not due yet
    assert head.plan_ai(cands(), noon + dt.timedelta(minutes=80)) == {"BTCUSDT"}     # due again (about 72 min apart)
    head = HeadTraderAgent(s, j, FakeLLM(1.0, 0.99, 0.05))
    assert head.plan_ai(cands(), noon) == set() and "spent" in head.last_plan["why"]


def test_head_tilts_thresholds_for_correlated_holdings_and_proven_news():
    j = Journal(":memory:", 10_000)
    head = HeadTraderAgent(Settings(), j)
    rng = random.Random(5)
    base = [100.0]
    for _ in range(199):
        base.append(base[-1] * (1 + rng.gauss(0, 0.01)))
    twin = [x * 2 * (1 + rng.gauss(0, 0.0005)) for x in base]        # moves with base
    other = [100.0]
    for _ in range(199):
        other.append(other[-1] * (1 + rng.gauss(0, 0.01)))             # moves on its own
    closes = {"BTCUSDT": base, "ETHUSDT": twin, "SOLUSDT": other}
    assert correlation(base, twin) > CORRELATED and abs(correlation(base, other)) < CORRELATED
    thr, why = head.threshold("ETHUSDT", 0.35, closes, {"BTCUSDT": {}, "SOLUSDT": {}})
    assert thr == pytest.approx(0.35 + TILT) and "holding BTC" in why
    assert head.threshold("SOLUSDT", 0.35, closes, {"BTCUSDT": {}}) == (0.35, "coin threshold")
    assert head.threshold("SOLUSDT", 0.35, closes, {}, news_score=0.9) == (0.35, "coin threshold")   # news unproven
    j.set("news_scorecard", {"judged": NEWS_MIN_JUDGED, "hit_rate_pct": 60.0})
    thr, why = head.threshold("SOLUSDT", 0.35, closes, {}, news_score=0.9)
    assert thr == pytest.approx(0.30) and "60.0% hit rate" in why
    assert head.threshold("SOLUSDT", 0.6, closes, {}, news_score=-0.5)[0] == 0.6     # never beyond the bounds


def test_playbook_carries_the_desk_knowledge():
    j = Journal(":memory:", 10_000)
    s = Settings(offline=True)
    head = HeadTraderAgent(s, j)
    assert head.playbook() == ""
    Lab(s, j, client=None, fetch=fake_fetch).champion()
    j.set("lessons", {"notes": ["many stop-losses hit within two hours of entry"]})
    j.set("threshold_study", {"base": 0.35, "proposed_card": None,
                              "coins": {"XRPUSDT": {"threshold": 0.45, "why": "best 0.5 (+2% on 30 trades)"}}})
    j.set("last_review", {"review": {"summary": "Too early to tell.", "failing": ["fees"]}})
    head._playbook = None
    text = head.playbook(private=True)      # for the owner and Claude: with Claude's review of the private notebook
    assert "Strategy in force" in text and "XRP 0.45" in text and "two hours" in text and "Too early" in text
    assert len(text) <= 1500 and head.playbook(private=True) == text   # cached
    shared = head.playbook()                # what a free AI may read: no review of the notebook
    assert "Strategy in force" in shared and "two hours" in shared and "Too early" not in shared


def test_desk_records_the_head_threshold_and_can_run_without_the_head():
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=())
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    assert bot.head is not None
    bot.run_cycle()
    rep = json.loads(bot.journal.db.execute("SELECT reports FROM decisions").fetchone()["reports"])
    assert rep["head"] == {"threshold": 0.35, "why": "coin threshold"}
    off = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"], head_trader=False), Journal(":memory:", 10_000))
    assert off.head is None
    assert all("error" not in r for r in off.run_cycle())


def test_head_study_feeds_the_laboratory(monkeypatch):
    monkeypatch.setattr(lab_module, "WINDOWS", TEST_WINDOWS)
    monkeypatch.setattr("trader.calibration.CANDIDATES", [0.25, 0.35, 0.45])
    monkeypatch.setattr("trader.calibration.STUDY_DAYS", 30)
    monkeypatch.setattr("trader.calibration.HOLDOUT_DAYS", 10)
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], lab_alt_symbols=("BNBUSDT", "XRPUSDT"))
    j = Journal(":memory:", 10_000)
    lab = Lab(s, j, client=None, fetch=fake_fetch)
    head = HeadTraderAgent(s, j)
    result, card = head.study(lab, ["BTCUSDT", "ETHUSDT"])
    assert set(j.get("threshold_study")["coins"]) == {"BTCUSDT", "ETHUSDT"} and j.events()[-1]["kind"] == "head"
    moved = [c for c in result["coins"].values() if c["threshold"] != 0.35]
    assert ("Threshold study" in head.playbook()) == bool(moved) and result["proposed_card"] == (card.id if card else None)
    if card is not None:
        assert lab.add_challenger(card)["status"] == "proposed"
        lab.run_week([])
        assert any(c["card"]["id"] == card.id and c["status"] in ("lab_passed", "lab_failed") for c in lab.challengers())


def test_study_cards_must_win_the_held_out_windows():
    s = Settings(offline=True, symbols=["BTCUSDT"], lab_alt_symbols=("BNBUSDT",))
    lab = Lab(s, Journal(":memory:", 10_000), client=None, fetch=fake_fetch)
    windows = [(90, 0), (180, 0), (365, 0), (180, 90), (180, 180)]
    champ = {"tests": [{"coins": c, "days": d, "ending_days_ago": o, "return_pct": 1.0 + i / 10, "trades": 20,
                        "profit_factor": 1.2, "max_drawdown_pct": 2.0, "basket_hold_pct": 0}
                       for c in ("BTC", "BNB") for i, (d, o) in enumerate(windows)],
             "mean_return": 1.0, "worst_return": 1.0, "total_trades": 200, "mean_profit_factor": 1.2, "max_drawdown_pct": 2.0}
    better = {**champ, "tests": [{**t, "return_pct": t["return_pct"] + 1} for t in champ["tests"]]}
    better["tests"][0] = {**better["tests"][0], "return_pct": 0.5}   # loses only the BTC 90-day holdout: 9 of 10 wins
    assert lab.compare(better, champ)["passed"]                      # fine for an ordinary challenger
    v = lab.compare(better, champ, strict_holdout=True)
    assert not v["passed"] and "held-out window" in v["reasons"][0] and "BTC" in v["reasons"][0]
    allwin = {**better, "tests": [{**t, "return_pct": t["return_pct"] + 1} for t in champ["tests"]]}
    assert lab.compare(allwin, champ, strict_holdout=True)["passed"]
    # a per-coin card that leaves the second coin set untouched: those five ties are not losses
    core_only = {**champ, "tests": [{**t, "return_pct": t["return_pct"] + (1 if t["coins"] == "BTC" else 0)}
                                    for t in champ["tests"]]}
    v = lab.compare(core_only, champ, strict_holdout=True)
    assert v["passed"] and v["ties"] == 5 and v["wins"] == 5


def test_shrink_moves_whole_steps_towards_the_common_threshold():
    assert [_shrink(0.35, 0.40, n) for n in (12, 20, 21)] == [0.35, 0.35, 0.40]     # one step needs > 20 trades
    assert [_shrink(0.35, 0.45, n) for n in (60, 61)] == [0.40, 0.45]                # two steps need > 60
    assert _shrink(0.35, 0.25, 30) == 0.30 and _shrink(0.35, 0.35, 99) == 0.35


def test_held_coins_keep_the_cards_sell_bar():
    j = Journal(":memory:", 10_000)
    j.set("news_scorecard", {"judged": NEWS_MIN_JUDGED, "hit_rate_pct": 60.0})
    head = HeadTraderAgent(Settings(), j)
    closes = {"BTCUSDT": [100 + i for i in range(200)], "ETHUSDT": [50 + i / 2 for i in range(200)]}
    held = {"BTCUSDT": {}, "ETHUSDT": {}}
    assert head.threshold("ETHUSDT", 0.35, closes, held) == (0.35, "coin threshold (held: exits are not tilted)")
    assert head.threshold("ETHUSDT", 0.35, closes, {"ETHUSDT": {}}, news_score=-0.8)[0] == 0.35
    assert head.threshold("ETHUSDT", 0.35, closes, {"BTCUSDT": {}})[0] == pytest.approx(0.40)   # not held: tilted


def test_one_study_card_per_held_out_period_and_no_duplicate_claims(monkeypatch):
    monkeypatch.setattr(lab_module, "WINDOWS", TEST_WINDOWS)
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], lab_alt_symbols=("BNBUSDT", "XRPUSDT"))
    j = Journal(":memory:", 10_000)
    lab = Lab(s, j, client=None, fetch=fake_fetch)
    head = HeadTraderAgent(s, j)
    forced = {"base": 0.35, "studied_at": "2026-01-01T00:00:00+00:00", "study_days": 30, "holdout_days": 10,
              "coins": {"BTCUSDT": {"threshold": 0.45, "why": "w", "edge": 2.0, "rows": []}}}
    monkeypatch.setattr("trader.calibration.study", lambda *a, **k: json.loads(json.dumps(forced)))
    _, card = head.study(lab, ["BTCUSDT"])
    assert card is not None and "proposed" in j.events()[-1]["text"]
    lab.add_challenger(card)
    _, again = head.study(lab, ["BTCUSDT"])
    assert again is None and "already proposed" in j.events()[-1]["text"]          # same card: not claimed again
    forced["coins"]["BTCUSDT"]["threshold"] = 0.40
    _, other = head.study(lab, ["BTCUSDT"])
    assert other is None and f"one study card per" in j.events()[-1]["text"]       # within 90 days of the last


def test_cards_from_before_1_4_keep_their_ids_and_stored_ids_are_repaired():
    from trader.lab import migrate_ids
    import hashlib
    old_style = StrategyCard("x")
    params = old_style.params()
    assert "coin_thresholds" not in params                                          # empty table: old id kept
    assert old_style.id == hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:8]
    j = Journal(":memory:", 10_000)
    stored = {**old_style.to_dict(), "id": "deadbeef"}                              # id written by 1.4.0-1.4.2
    j.set("strategy_champion", {"card": stored, "since": "x", "by": "owner settings", "previous": None})
    j.set("strategy_challengers", [{"card": {**StrategyCard("y", trail_atr=3.0).to_dict(), "id": "cafecafe"},
                                    "status": "ready", "proposed_at": "x", "notes": []}])
    j.set("strategy_history", [{"ts": "x", "event": "promote", "id": "deadbeef", "name": "x"}])
    assert migrate_ids(j) == 2 and migrate_ids(j) == 0
    assert j.get("strategy_champion")["card"]["id"] == old_style.id and j.get("strategy_history")[0]["id"] == old_style.id
    lab = Lab(Settings(offline=True), j, client=None)
    ready_id = StrategyCard("y", trail_atr=3.0).id
    assert lab.promote(ready_id, by="owner") is not None                            # /approve works again


def test_per_coin_changes_count_one_each_and_unexamined_coins_are_refused():
    champ = StrategyCard("c")
    many = StrategyCard("m", coin_thresholds={"BTCUSDT": 0.4, "ETHUSDT": 0.4, "SOLUSDT": 0.3})
    assert many.change_count(champ) == 3 and StrategyCard("o", trail_atr=3.0).change_count(champ) == 1
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"], lab_alt_symbols=("BNBUSDT",))
    lab = Lab(s, Journal(":memory:", 10_000), client=None)
    assert "3 changes" in lab.add_challenger(many)["notes"][0]
    doge = lab.add_challenger(StrategyCard("d", coin_thresholds={"DOGEUSDT": 0.4}))
    assert doge["status"] == "rejected" and "never trades: DOGE" in doge["notes"][0]


def test_the_exam_never_touches_the_live_rules(monkeypatch):
    monkeypatch.setattr(lab_module, "WINDOWS", TEST_WINDOWS)
    s = Settings(offline=True, symbols=["BTCUSDT"], lab_alt_symbols=("BNBUSDT",))
    lab = Lab(s, Journal(":memory:", 10_000), client=None, fetch=fake_fetch)
    before = (dmod.BUY_THRESHOLD, dict(dmod.WEIGHTS), dict(dmod.COIN_THRESHOLDS))
    seen = []
    real_run = Backtest.run

    def spy(self):
        seen.append((dmod.BUY_THRESHOLD, dict(dmod.COIN_THRESHOLDS)))   # what a live decision would see meanwhile
        return real_run(self)
    monkeypatch.setattr(Backtest, "run", spy)
    ev = lab.evaluate(StrategyCard("strict", buy_threshold=0.55, coin_thresholds={"BTCUSDT": 0.6}))
    assert seen and all(x == (before[0], before[2]) for x in seen) and len(ev["tests"]) == 10
    strict = Lab(s, Journal(":memory:", 10_000), client=None, fetch=fake_fetch).evaluate(StrategyCard("strict", buy_threshold=0.55))
    loose = Lab(s, Journal(":memory:", 10_000), client=None, fetch=fake_fetch).evaluate(StrategyCard("loose", buy_threshold=0.2))
    assert strict["total_trades"] < loose["total_trades"]                     # the card's own rules were used


def test_history_downloads_retry_transient_errors_and_bad_coin_buy_is_a_usage_error(capsys):
    import argparse
    import requests
    from trader.backtest import _klines_page, parse_coin_buy
    from trader.binance_client import BinanceError
    calls = []

    class Flaky:
        def klines(self, *a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise requests.ConnectionError("reset")
            return [{"time": 1}]
    assert _klines_page(Flaky(), "BTCUSDT", "1h", 0, sleep=lambda s: None) == [{"time": 1}] and len(calls) == 3

    class Refused:
        def klines(self, *a, **k):
            raise BinanceError("HTTP 400: Invalid symbol.")
    with pytest.raises(BinanceError):
        _klines_page(Refused(), "NOPEUSDT", "1h", 0, sleep=lambda s: None)   # not transient: no retry
    ap = argparse.ArgumentParser(prog="trader backtest")
    assert parse_coin_buy(ap, "btc=0.4, ETHUSDT=0.3,") == {"BTCUSDT": 0.4, "ETHUSDT": 0.3}
    with pytest.raises(SystemExit):
        parse_coin_buy(ap, "BTC:0.4")
    assert "--coin-buy expects COIN=VALUE" in capsys.readouterr().err

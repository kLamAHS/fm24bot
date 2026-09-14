"""Tests for the promise ledger (spec 11.3)."""
from __future__ import annotations

import unittest

from ..interactions.promises import (
    KIND_ALLOW_TRANSFER, KIND_NEW_CONTRACT, KIND_PLAYING_TIME, KIND_RECRUIT, KIND_STARTING_ROLE, MATCH_MINUTES, PLAYING_TIME_MINUTES_PER_FIXTURE,
    SEVERITY_BLOCKING, SEVERITY_INFO, SEVERITY_WARNING, STARTING_ROLE_MINUTES_PER_FIXTURE, STATUS_BROKEN, STATUS_EXPIRED, STATUS_KEPT, PromiseError,
    PromiseLedger, PromiseTerms, parse_terms, promise_from_observed,
)
from ..state.identity import CareerRegistry, SaveManifest
from ..state.records import Promise
from ..state.store import Store
from . import fixtures as fx


def ledger() -> tuple[Store, PromiseLedger]:
    store = Store.memory()
    _, branch, _ = CareerRegistry(store).register_career("t", SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME))
    return store, PromiseLedger(store, branch.branch_id)


class ParseTermsTests(unittest.TestCase):
    def test_keyword_table_is_labelled_heuristic(self):
        terms = parse_terms("I promise you will be a regular starter at ST this season")
        self.assertEqual((terms.kind, terms.position, terms.basis), (KIND_STARTING_ROLE, "ST", "heuristic"))
        self.assertEqual(parse_terms("You will get at least 30 minutes a game").minutes_per_fixture, 30)
        self.assertEqual(parse_terms("We will sign a new goalkeeper in January").kind, KIND_RECRUIT)
        self.assertEqual(parse_terms("We will offer you a new contract").kind, KIND_NEW_CONTRACT)
        self.assertEqual(parse_terms("We will listen to offers for you").kind, KIND_ALLOW_TRANSFER)
        self.assertEqual(parse_terms("Thanks for coming in").kind, "other")


class RecordTests(unittest.TestCase):
    def test_record_from_observed_choice(self):
        store, book = ledger()
        promise, terms = promise_from_observed(1001, "player", "You will be a regular starter", source="obs-dialogue-1", source_choice="choice:promise_start", deadline="2024-05-01")
        book.record(promise, terms)
        [loaded] = book.open()
        self.assertEqual((loaded.party_id, loaded.confidence, loaded.source_choice, loaded.status), (1001, "observed", "choice:promise_start", "open"))
        self.assertEqual(book.terms(promise.promise_id).kind, KIND_STARTING_ROLE)
        self.assertEqual(store.journal_entries(kind="promise_terms", ref_id=promise.promise_id)[0]["body"]["terms"]["kind"], KIND_STARTING_ROLE)

    def test_explicit_terms_are_preferred_and_persist_across_ledgers(self):
        store, book = ledger()
        promise, _ = promise_from_observed(1002, "player", "You'll get your chance", source="ui:inbox#77", terms=PromiseTerms(KIND_PLAYING_TIME, "DC", 45, 6))
        book.record(promise, PromiseTerms(KIND_PLAYING_TIME, "DC", 45, 6))
        self.assertEqual(promise.playing_time_minutes_per_fixture, 45)
        again = PromiseLedger(store, book.branch_id)
        self.assertEqual(again.terms(promise.promise_id), PromiseTerms(KIND_PLAYING_TIME, "DC", 45, 6))
        self.assertEqual(len(again.open()), 1)

    def test_promise_inferred_from_morale_is_refused(self):
        _, book = ledger()
        inferred = Promise("p-x", 1001, "player", "probably wants to start", None, "", None, None, "inferred", "morale:1001")
        with self.assertRaises(PromiseError):
            book.record(inferred)
        unsourced = Promise("p-y", 1001, "player", "You will start", None, "", None, None, "observed", "")
        with self.assertRaises(PromiseError):
            book.record(unsourced)
        with self.assertRaises(PromiseError):
            promise_from_observed(1001, "player", "You will start", source="")
        self.assertEqual(book.open(), [])


class CheckBeforeTests(unittest.TestCase):
    def setUp(self):
        self.store, self.book = ledger()
        self.starter, _ = promise_from_observed(1001, "player", "You will be a regular starter at ST", source="obs-d1", source_choice="c1")
        self.book.record(self.starter)
        self.leaver, _ = promise_from_observed(1005, "player", "We will listen to offers for you", source="obs-d2", source_choice="c2")
        self.book.record(self.leaver)

    def test_selling_a_promised_starter_is_blocking(self):
        [conflict] = self.book.check_before("sell_player", {"player_id": 1001})
        self.assertEqual((conflict.severity, conflict.promise_id), (SEVERITY_BLOCKING, self.starter.promise_id))
        self.assertTrue(conflict.blocking)
        self.assertEqual(self.book.check_before("sell_player", {"player_id": 1009}), [])

    def test_selling_a_player_promised_a_move_is_informational(self):
        [conflict] = self.book.check_before("accept_sale", {"player_id": 1005})
        self.assertEqual(conflict.severity, SEVERITY_INFO)

    def test_recruiting_into_a_promised_position_warns(self):
        [conflict] = self.book.check_before("commit.transfer_offer", {"position": "ST"})
        self.assertEqual((conflict.severity, conflict.party_id), (SEVERITY_WARNING, 1001))
        self.assertEqual(self.book.check_before("commit.transfer_offer", {"position": "GK"}), [])
        anywhere, _ = promise_from_observed(1003, "player", "You will get more games", source="obs-d3", source_choice="c3")
        self.book.record(anywhere)
        self.assertEqual(len(self.book.check_before("recruit", {"position": "GK"})), 1)

    def test_lineup_without_promised_starter_warns(self):
        [conflict] = self.book.check_before("advise.lineup", {"starters": [1002, 1003]})
        self.assertEqual((conflict.severity, conflict.party_id), (SEVERITY_WARNING, 1001))
        self.assertEqual(self.book.check_before("advise.lineup", {"starters": [1001, 1002]}), [])

    def test_conversation_lists_open_promises_as_info(self):
        [conflict] = self.book.check_before("conversations.player", {"player_id": 1001})
        self.assertEqual(conflict.severity, SEVERITY_INFO)


class MinutesTests(unittest.TestCase):
    def test_reservations_use_observed_minutes_or_labelled_defaults(self):
        _, book = ledger()
        starter, _ = promise_from_observed(1001, "player", "You will be a regular starter", source="obs-d1", source_choice="c1")
        book.record(starter)
        timed, _ = promise_from_observed(1002, "player", "You will play at least 30 minutes a game", source="obs-d2", source_choice="c2")
        book.record(timed)
        other, _ = promise_from_observed(1003, "player", "We will offer you a new contract", source="obs-d3", source_choice="c3")
        book.record(other)
        self.assertEqual(book.minutes_commitments(3), {1001: STARTING_ROLE_MINUTES_PER_FIXTURE * 3, 1002: 90})
        bases = {r.player_id: r.basis for r in book.minutes_reservations(3)}
        self.assertEqual(bases, {1001: "heuristic", 1002: "observed"})

    def test_deadline_and_fixture_span_limit_reservations(self):
        _, book = ledger()
        promise, _ = promise_from_observed(1001, "player", "You will get regular football", source="obs-d1", source_choice="c1", deadline="2024-03-05")
        book.record(promise)
        dates = ["2024-02-20", "2024-02-24", "2024-03-02", "2024-03-09", "2024-03-16"]
        self.assertEqual(book.minutes_commitments(5, dates), {1001: PLAYING_TIME_MINUTES_PER_FIXTURE * 3})
        capped, _ = promise_from_observed(1002, "player", "You will start", source="obs-d2", source_choice="c2", terms=PromiseTerms(KIND_STARTING_ROLE, None, 90, 2))
        book.record(capped, PromiseTerms(KIND_STARTING_ROLE, None, 90, 2))
        self.assertEqual(book.minutes_commitments(5)[1002], 180)

    def test_combined_reservations_never_exceed_the_match_total(self):
        _, book = ledger()
        for index in range(2):
            p, _ = promise_from_observed(1001, "player", "You will start", source=f"obs-{index}", source_choice=f"c{index}", terms=PromiseTerms(KIND_STARTING_ROLE, None, 80, None))
            book.record(p, PromiseTerms(KIND_STARTING_ROLE, None, 80, None))
        self.assertEqual(book.minutes_commitments(2), {1001: MATCH_MINUTES * 2})


class LifecycleTests(unittest.TestCase):
    def test_progress_and_closure(self):
        store, book = ledger()
        promise, _ = promise_from_observed(1001, "player", "You will start", source="obs-d1", source_choice="c1")
        book.record(promise)
        book.update_progress(promise.promise_id, "obs-match-1", "started 1 of 1")
        self.assertEqual(book.get(promise.promise_id).progress, "started 1 of 1")
        kept = book.mark_kept(promise.promise_id, "obs-inbox-9")
        self.assertEqual(kept.status, STATUS_KEPT)
        self.assertEqual(book.open(), [])
        with self.assertRaises(PromiseError):
            book.mark_broken(promise.promise_id, "obs-inbox-10")
        kinds = [e["kind"] for e in store.journal_entries(ref_id=promise.promise_id)]
        self.assertEqual(kinds, ["promise", "promise_terms", "promise", "promise_progress", "promise", "promise_status"])

    def test_broken_keeps_consequences_only_when_observed(self):
        _, book = ledger()
        a, _ = promise_from_observed(1001, "player", "You will start", source="obs-d1", source_choice="c1")
        b, _ = promise_from_observed(1002, "player", "You will start", source="obs-d2", source_choice="c2")
        book.record(a)
        book.record(b)
        self.assertIsNone(book.mark_broken(a.promise_id, "obs-inbox-11").consequences)
        self.assertEqual(book.mark_broken(b.promise_id, "obs-inbox-12", "Player is unhappy and wants to leave").consequences, "Player is unhappy and wants to leave")
        self.assertEqual({p.status for p in book.for_party(1001, include_closed=True)}, {STATUS_BROKEN})

    def test_expire_requires_evidence(self):
        _, book = ledger()
        promise, _ = promise_from_observed(1001, "player", "You will start", source="obs-d1", source_choice="c1", deadline="2024-03-01")
        book.record(promise)
        with self.assertRaises(PromiseError):
            book.expire(promise.promise_id, "")
        self.assertEqual(book.expire(promise.promise_id, "obs-date-passed").status, STATUS_EXPIRED)
        with self.assertRaises(PromiseError):
            book.expire("nope", "obs")


if __name__ == "__main__":
    unittest.main()

"""Tests for inbox items, classification and the inbox text boundary (spec 1.3, 3.2, CAL 01)."""
from __future__ import annotations

import unittest

from ..interactions.inbox import (
    CAPABILITY_INBOX_TEXT, CAPABILITY_PENDING_ACTIONS, CONFIDENCE_CONFIRMED, CONFIDENCE_EXPLICIT_LIST, CONFIDENCE_HEURISTIC, CONFIDENCE_NONE,
    INBOX_PATTERNS_VERSION, KIND_DECISION_REQUIRED, KIND_INFORMATIONAL, KIND_UNKNOWN, TIME_STATUS_UNKNOWN, DeclaredInboxTextProvider, DialogueOption,
    InboxItem, InboxText, NoInboxTextProvider, classify, continue_blocked_by_inbox, inbox_item, inbox_items, unresolved_mandatory,
)
from ..state.status import ValueStatus
from . import fixtures as fx

OFFER_TEXT = InboxText(501, "Transfer offer for Sam Wing", "Derby have offered £450k.", (DialogueOption("accept", "Accept the offer"), DialogueOption("reject", "Reject the offer"), DialogueOption("negotiate", "Negotiate")), None, True, None)


def item(**overrides) -> InboxItem:
    if "message_id" in overrides:
        overrides["id"] = overrides.pop("message_id")
    base = {"id": 900, "date": "2024-02-17", "time": "09:00", "unread": True, "event_type": "news_item_generic", "sender_id": None, "sender_name": None, "text_status": "not_decoded", "time_status": "current"}
    base.update(overrides)
    return inbox_item(base, "obs-test")


class InboxItemTests(unittest.TestCase):
    def test_items_from_bridge_payload_keep_missing_time_explicit(self):
        items = inbox_items(fx.inbox_payload(), "obs-1")
        self.assertEqual([i.message_id for i in items], [501, 502, 503])
        board = items[2]
        self.assertIsNone(board.time)
        self.assertEqual(board.time_status, "not_initialized")
        self.assertFalse(board.time_known)
        self.assertTrue(items[0].time_known)
        self.assertEqual(items[0].text_status, "not_decoded")
        self.assertEqual(items[0].source, "obs-1")

    def test_obs02_omitted_time_status_is_unknown_not_current(self):
        """OBS 02 / spec 5.2: a record with a time but no time_status was never asserted current by the bridge, so time_known is False."""
        without = inbox_item({"id": 7, "date": "2024-02-17", "time": "09:00", "unread": True, "event_type": "news_item_generic"})
        self.assertEqual(without.time, "09:00")
        self.assertEqual(without.time_status, TIME_STATUS_UNKNOWN)
        self.assertFalse(without.time_known)
        no_time = inbox_item({"id": 8, "date": "2024-02-17", "unread": True, "event_type": "news_item_generic"})
        self.assertIsNone(no_time.time)
        self.assertEqual(no_time.time_status, TIME_STATUS_UNKNOWN)
        self.assertFalse(no_time.time_known)
        asserted = inbox_item({"id": 9, "date": "2024-02-17", "time": "09:00", "time_status": "current", "unread": True, "event_type": "news_item_generic"})
        self.assertTrue(asserted.time_known)

    def test_items_from_snapshot(self):
        snap = fx.snapshot_for(["/inbox"])
        items = inbox_items(snap)
        self.assertEqual(len(items), 3)
        self.assertIn(snap.snapshot_id, items[0].source)


class ClassifyTests(unittest.TestCase):
    def test_transfer_offer_requires_decision_but_mandatory_not_established(self):
        cls = classify(item(event_type="news_item_transfer_offer"))
        self.assertEqual(cls.kind, KIND_DECISION_REQUIRED)
        self.assertIsNone(cls.mandatory)
        self.assertEqual(cls.confidence, CONFIDENCE_HEURISTIC)
        self.assertIn("offer", cls.matched)
        self.assertEqual(cls.patterns_version, INBOX_PATTERNS_VERSION)

    def test_board_meeting_is_mandatory_heuristic(self):
        cls = classify(item(event_type="news_item_board_meeting_request"))
        self.assertEqual((cls.kind, cls.mandatory), (KIND_DECISION_REQUIRED, True))

    def test_training_is_informational_by_explicit_list(self):
        cls = classify(item(event_type="news_item_training"))
        self.assertEqual((cls.kind, cls.mandatory, cls.confidence), (KIND_INFORMATIONAL, False, CONFIDENCE_EXPLICIT_LIST))
        self.assertFalse(cls.needs_answer)

    def test_unmatched_type_is_unknown_not_informational(self):
        cls = classify(item(event_type="news_item_something_new"))
        self.assertEqual((cls.kind, cls.mandatory, cls.confidence), (KIND_UNKNOWN, None, CONFIDENCE_NONE))
        self.assertTrue(cls.needs_answer)

    def test_confirmed_text_with_options_overrides_heuristic(self):
        provider = DeclaredInboxTextProvider()
        provider.declare(OFFER_TEXT, source="ui:inbox", observed_at="2026-01-01T00:00:00+00:00", game_time="2024-02-17 10:00")
        cls = classify(item(message_id=501, event_type="news_item_training"), provider.get_text(501))
        self.assertEqual((cls.kind, cls.confidence), (KIND_DECISION_REQUIRED, CONFIDENCE_CONFIRMED))
        self.assertIsNone(cls.mandatory)

    def test_confirmed_text_with_deadline_marks_mandatory(self):
        text = InboxText(7, "Registration", "Submit your squad list.", (DialogueOption("submit", "Submit"),), "2024-03-01", True, None)
        provider = DeclaredInboxTextProvider()
        provider.declare(text, source="ui:inbox", observed_at="t", game_time=None)
        self.assertTrue(classify(item(message_id=7), provider.get_text(7)).mandatory)

    def test_confirmed_no_decision_is_informational(self):
        text = InboxText(8, "Match report", "We won.", (), None, False, False)
        provider = DeclaredInboxTextProvider()
        provider.declare(text, source="ui:inbox", observed_at="t", game_time=None)
        cls = classify(item(message_id=8, event_type="news_item_transfer_offer"), provider.get_text(8))
        self.assertEqual((cls.kind, cls.confidence), (KIND_INFORMATIONAL, CONFIDENCE_CONFIRMED))


class TextProviderTests(unittest.TestCase):
    def test_no_provider_is_unsupported(self):
        observed = NoInboxTextProvider().get_text(501)
        self.assertFalse(observed.available)
        self.assertEqual(observed.status, ValueStatus.UNSUPPORTED)
        with self.assertRaises(Exception):
            observed.require()

    def test_declared_provider_states(self):
        provider = DeclaredInboxTextProvider("adapter")
        self.assertEqual(provider.get_text(501).status, ValueStatus.MISSING)
        provider.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="2026-01-01T00:00:00+00:00", game_time="2024-02-17 10:00", verified=False)
        self.assertEqual(provider.get_text(501).status, ValueStatus.UNSUPPORTED)
        provider.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="2026-01-01T00:00:00+00:00", game_time="2024-02-17 10:00")
        self.assertEqual(provider.get_text(501, game_time="2024-02-18 10:00").status, ValueStatus.STALE)
        observed = provider.get_text(501, game_time="2024-02-17 10:00")
        self.assertTrue(observed.available)
        self.assertEqual(observed.source, "ui:inbox#501")
        self.assertEqual(observed.game_time, "2024-02-17 10:00")
        self.assertEqual(observed.require().fixed_option_ids, ["accept", "reject", "negotiate"])

    def test_declaration_requires_source(self):
        with self.assertRaises(ValueError):
            DeclaredInboxTextProvider().declare(OFFER_TEXT, source="", observed_at="t", game_time=None)


class UnresolvedMandatoryTests(unittest.TestCase):
    def test_without_text_provider_blockers_name_inbox_text(self):
        blockers = unresolved_mandatory(inbox_items(fx.inbox_payload()))
        self.assertEqual([b.item.message_id for b in blockers], [501, 503])
        for blocker in blockers:
            self.assertIn(CAPABILITY_INBOX_TEXT, blocker.report.missing)
            self.assertFalse(blocker.resolvable_now)
            self.assertEqual(blocker.text_status, "unsupported")
        self.assertIn("mandatory decision", blockers[1].description)

    def test_unread_unknown_blocks_but_read_unknown_does_not(self):
        self.assertEqual(len(unresolved_mandatory([item(event_type="news_item_mystery", unread=True)])), 1)
        self.assertEqual(unresolved_mandatory([item(event_type="news_item_mystery", unread=False)]), [])

    def test_read_decision_message_needs_pending_actions(self):
        [blocker] = unresolved_mandatory([item(event_type="news_item_transfer_offer", unread=False)])
        self.assertIn(CAPABILITY_PENDING_ACTIONS, blocker.report.missing)
        [blocker] = unresolved_mandatory([item(event_type="news_item_transfer_offer", unread=False)], pending_actions_supported=True)
        self.assertNotIn(CAPABILITY_PENDING_ACTIONS, blocker.report.missing)

    def test_declared_text_makes_blocker_resolvable(self):
        provider = DeclaredInboxTextProvider()
        provider.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="t", game_time="2024-02-17 10:00")
        blockers = unresolved_mandatory(inbox_items(fx.inbox_payload()), provider, game_time="2024-02-17 10:00")
        offer = next(b for b in blockers if b.item.message_id == 501)
        self.assertFalse(offer.report.blocked)
        self.assertTrue(offer.resolvable_now)
        self.assertEqual(offer.legal_option_ids, ["accept", "reject", "negotiate"])
        board = next(b for b in blockers if b.item.message_id == 503)
        self.assertIn(CAPABILITY_INBOX_TEXT, board.report.missing)

    def test_free_text_only_decision_is_not_resolvable(self):
        text = InboxText(501, "Offer", "Reply in your own words", (DialogueOption("compose", "Write a reply", kind="free_text"),), None, True, None)
        provider = DeclaredInboxTextProvider()
        provider.declare(text, source="ui:inbox#501", observed_at="t", game_time=None)
        [blocker] = unresolved_mandatory([item(message_id=501, event_type="news_item_transfer_offer")], provider)
        self.assertIn(CAPABILITY_INBOX_TEXT, blocker.report.missing)
        self.assertEqual(blocker.legal_option_ids, [])

    def test_continue_report_folds_blockers(self):
        report = continue_blocked_by_inbox(unresolved_mandatory(inbox_items(fx.inbox_payload())))
        self.assertTrue(report.blocked)
        self.assertEqual(report.blocked_action, "progress.continue")
        self.assertIn(CAPABILITY_INBOX_TEXT, report.missing)
        provider = DeclaredInboxTextProvider()
        provider.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="t", game_time=None)
        report = continue_blocked_by_inbox(unresolved_mandatory([item(message_id=501, event_type="news_item_transfer_offer")], provider))
        self.assertEqual(report.missing, ["inbox_decision"])


if __name__ == "__main__":
    unittest.main()

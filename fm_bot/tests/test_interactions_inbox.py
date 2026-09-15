"""Tests for inbox items, classification and the inbox text boundary (spec 1.3, 3.2, CAL 01)."""
from __future__ import annotations

import unittest

from ..interactions.inbox import (
    CAPABILITY_INBOX_TEXT, CAPABILITY_PENDING_ACTIONS, CONFIDENCE_CONFIRMED, CONFIDENCE_EXPLICIT_LIST, CONFIDENCE_HEURISTIC, CONFIDENCE_NONE,
    CONFIDENCE_UNVERIFIED_TEXT, FRESHNESS_CURRENT, FRESHNESS_UNVERIFIED, INBOX_PATTERNS_VERSION, KIND_DECISION_REQUIRED, KIND_INFORMATIONAL,
    KIND_UNKNOWN, TIME_STATUS_UNKNOWN, DeclaredInboxTextProvider, DialogueOption, InboxItem, InboxText, NoInboxTextProvider, classification_text_of,
    classify, continue_blocked_by_inbox, inbox_item, inbox_items, unresolved_mandatory,
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
        cls = classify(item(message_id=501, event_type="news_item_training"), provider.get_text(501, game_time="2024-02-17 10:00"))
        self.assertEqual((cls.kind, cls.confidence), (KIND_DECISION_REQUIRED, CONFIDENCE_CONFIRMED))
        self.assertIsNone(cls.mandatory)

    def test_confirmed_text_with_deadline_marks_mandatory(self):
        text = InboxText(7, "Registration", "Submit your squad list.", (DialogueOption("submit", "Submit"),), "2024-03-01", True, None)
        provider = DeclaredInboxTextProvider()
        provider.declare(text, source="ui:inbox", observed_at="t", game_time="2024-02-17 10:00")
        self.assertTrue(classify(item(message_id=7), provider.get_text(7, game_time="2024-02-17 10:00")).mandatory)

    def test_confirmed_no_decision_is_informational(self):
        text = InboxText(8, "Match report", "We won.", (), None, False, False)
        provider = DeclaredInboxTextProvider()
        provider.declare(text, source="ui:inbox", observed_at="t", game_time="2024-02-17 10:00")
        cls = classify(item(message_id=8, event_type="news_item_transfer_offer"), provider.get_text(8, game_time="2024-02-17 10:00"))
        self.assertEqual((cls.kind, cls.confidence), (KIND_INFORMATIONAL, CONFIDENCE_CONFIRMED))

    def test_obs02_an_unverified_reading_classifies_but_is_labelled_not_confirmed(self):
        """OBS 02 / spec 5.2: a reading that in-game time could not show to be current still says what KIND of message this is
        (a match report stays a match report), but it is labelled ``unverified_text`` instead of ``confirmed`` so no caller can
        mistake it for evidence about what the game is offering now."""
        report = InboxText(8, "Match report", "We won.", (), None, False, False)
        provider = DeclaredInboxTextProvider()
        provider.declare(report, source="ui:inbox", observed_at="t", game_time=None)
        message = item(message_id=8, event_type="news_item_transfer_offer")
        text = provider.get_text(8, game_time="2024-09-30 18:00")
        self.assertFalse(text.available, "a timeless reading is not current text")
        classification = classification_text_of(provider, 8, game_time="2024-09-30 18:00")
        self.assertEqual(classification.freshness, FRESHNESS_UNVERIFIED)
        cls = classify(message, text, classification)
        self.assertEqual((cls.kind, cls.mandatory, cls.confidence), (KIND_INFORMATIONAL, False, CONFIDENCE_UNVERIFIED_TEXT))
        self.assertIn(FRESHNESS_UNVERIFIED, cls.reason)
        # Without the classification reading the event type still rules, and it never says informational.
        self.assertEqual(classify(message, text).kind, KIND_DECISION_REQUIRED)


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

    def test_obs02_a_reading_that_cannot_be_timed_is_never_served_as_current_text(self):
        """OBS 02 / spec 5.2: in-game time is the only clock, so a declaration whose game time was never recorded is ``stale``
        however long the career runs, and a caller with no in-game clock of its own gets ``missing`` verification rather than
        whatever was last declared. Both are still offered for classification, labelled ``unverified``."""
        timeless = DeclaredInboxTextProvider()
        timeless.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="t", game_time=None)
        for asked in (None, "2024-02-17 10:00", "2024-09-30 18:00"):
            observed = timeless.get_text(501, game_time=asked)
            self.assertFalse(observed.available, f"a timeless declaration is not current at {asked}")
            self.assertEqual(observed.status, ValueStatus.STALE)
            self.assertEqual(timeless.classification_text(501, game_time=asked).freshness, FRESHNESS_UNVERIFIED)
        timed = DeclaredInboxTextProvider()
        timed.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="t", game_time="2024-02-17 10:00")
        self.assertEqual(timed.get_text(501).status, ValueStatus.MISSING, "no caller clock: nothing to check the reading against")
        self.assertEqual(timed.classification_text(501).freshness, FRESHNESS_UNVERIFIED)
        self.assertEqual(timed.classification_text(501, game_time="2024-02-17 10:00").freshness, FRESHNESS_CURRENT)
        self.assertIsNone(timed.classification_text(502), "nothing is offered for a message never declared")
        unverified = DeclaredInboxTextProvider()
        unverified.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="t", game_time="2024-02-17 10:00", verified=False)
        self.assertIsNone(unverified.classification_text(501, game_time="2024-02-17 10:00"), "an unverified declaration is not even classification evidence")
        self.assertIsNone(NoInboxTextProvider().classification_text(501))

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
        provider.declare(text, source="ui:inbox#501", observed_at="t", game_time="2024-02-17 10:00")
        [blocker] = unresolved_mandatory([item(message_id=501, event_type="news_item_transfer_offer")], provider, game_time="2024-02-17 10:00")
        self.assertEqual(blocker.text_status, ValueStatus.AVAILABLE.value, "the text itself is current; only the free-text box makes it unanswerable")
        self.assertIn(CAPABILITY_INBOX_TEXT, blocker.report.missing)
        self.assertEqual(blocker.legal_option_ids, [])

    def test_cal01_a_read_message_reported_resolved_is_no_longer_a_blocker(self):
        """CAL 01: a read decision message named by a current, capability-backed pending-actions observation is answered, so it
        stops being a blocker; without the capability the same id proves nothing and it keeps blocking, and an unread message is
        never resolved this way because unread is the game's own evidence that nobody has answered it."""
        read_offer = item(message_id=501, event_type="news_item_transfer_offer", unread=False)
        resolved = ["inbox:501"]
        self.assertEqual(unresolved_mandatory([read_offer], resolved_action_ids=resolved, pending_actions_supported=True), [])
        [still_blocking] = unresolved_mandatory([read_offer], resolved_action_ids=resolved, pending_actions_supported=False)
        self.assertIn(CAPABILITY_PENDING_ACTIONS, still_blocking.report.missing)
        unread_offer = item(message_id=501, event_type="news_item_transfer_offer", unread=True)
        [unread_blocker] = unresolved_mandatory([unread_offer], resolved_action_ids=resolved, pending_actions_supported=True)
        self.assertIn(CAPABILITY_INBOX_TEXT, unread_blocker.report.missing)

    def test_obs02_a_reading_that_is_not_current_answers_nothing_but_still_classifies(self):
        """OBS 02 / spec 5.2, CAL 01: in-game time is the only clock, so a reading of an offer that cannot be shown current -
        read at an earlier in-game moment, or with no game time recorded at all - publishes no legal option ids and keeps naming
        ``inbox_text``; a months-old accept/reject list is never handed to a decision. The same unverified reading may still
        establish that a message is informational, which is a claim about the message and not about this moment."""
        offer = item(message_id=501, event_type="news_item_transfer_offer")
        now = "2024-09-30 18:00"
        earlier = DeclaredInboxTextProvider()
        earlier.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="t", game_time="2024-02-17 10:00")
        timeless = DeclaredInboxTextProvider()
        timeless.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="t", game_time=None)
        for label, provider in (("read earlier", earlier), ("read at an unknown moment", timeless)):
            [blocker] = unresolved_mandatory([offer], provider, game_time=now)
            self.assertFalse(blocker.resolvable_now, label)
            self.assertEqual(blocker.legal_option_ids, [], label)
            self.assertIn(CAPABILITY_INBOX_TEXT, blocker.report.missing, label)
            self.assertEqual(blocker.text_status, ValueStatus.STALE.value, label)
            self.assertEqual(blocker.classification.confidence, CONFIDENCE_HEURISTIC, label)
            self.assertEqual(continue_blocked_by_inbox([blocker]).missing, [CAPABILITY_INBOX_TEXT], label)
            # The reading is still there, labelled unverified, for the question classification really asks.
            self.assertEqual(classification_text_of(provider, 501, game_time=now).freshness, FRESHNESS_UNVERIFIED, label)
        # The same declaration read at the caller's own in-game moment does answer the message.
        current = DeclaredInboxTextProvider()
        current.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="t", game_time=now)
        [answerable] = unresolved_mandatory([offer], current, game_time=now)
        self.assertTrue(answerable.resolvable_now)
        self.assertEqual(answerable.legal_option_ids, ["accept", "reject", "negotiate"])
        self.assertEqual(continue_blocked_by_inbox([answerable]).missing, ["inbox_decision"])
        # Both halves of one decision point judge by current text alone (spec 12.4): an unverified reading that showed no
        # decision is required does NOT quietly drop the message here, it stays a blocker naming ``inbox_text``.
        informational = DeclaredInboxTextProvider()
        informational.declare(InboxText(501, "Match report", "We won.", (), None, False, False), source="ui:inbox#501", observed_at="t", game_time=None)
        [kept] = unresolved_mandatory([offer], informational, game_time=now)
        self.assertIn(CAPABILITY_INBOX_TEXT, kept.report.missing)
        self.assertEqual(classify(offer, informational.get_text(501, game_time=now), classification_text_of(informational, 501, game_time=now)).kind, KIND_INFORMATIONAL,
                         "classification still reads it, explicitly as an unverified reading")

    def test_continue_report_folds_blockers(self):
        report = continue_blocked_by_inbox(unresolved_mandatory(inbox_items(fx.inbox_payload())))
        self.assertTrue(report.blocked)
        self.assertEqual(report.blocked_action, "progress.continue")
        self.assertIn(CAPABILITY_INBOX_TEXT, report.missing)
        provider = DeclaredInboxTextProvider()
        provider.declare(OFFER_TEXT, source="ui:inbox#501", observed_at="t", game_time="2024-02-17 10:00")
        report = continue_blocked_by_inbox(unresolved_mandatory([item(message_id=501, event_type="news_item_transfer_offer")], provider, game_time="2024-02-17 10:00"))
        self.assertEqual(report.missing, ["inbox_decision"])


if __name__ == "__main__":
    unittest.main()

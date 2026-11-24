"""Synthetic sample data for the demo and for trying the server by hand.

Everything here is invented. The people, phone numbers and addresses are
placeholders (Alex Sample, 555-01xx, example.com) and describe no real business.
"""

from __future__ import annotations

from datetime import timedelta

from .service import BrainService

RULES: tuple[tuple[int, str, str], ...] = (
    (10, "*", "Never send a message to a customer until a human has approved the draft."),
    (20, "*", "Quote prices only from the current price sheet. If an item is missing, ask."),
    (30, "*", "Write dates in full (Tuesday 6 January), never as bare numbers."),
    (40, "ops_agent", "Before booking a visit, confirm the address back to the customer."),
    (40, "billing_agent", "Do not issue a refund above 100.00 without a named approver."),
)

CORRECTIONS: tuple[tuple[int, str, str], ...] = (
    # (hours ago, agent, text)
    (30, "*", "Stop opening replies with an apology when nothing went wrong."),
    (6, "ops_agent", "The Tuesday slot for Alex Sample was double booked. Check the calendar "
                     "before offering a time, not after."),
    (24 * 40, "*", "Old correction outside the window: use the long form of the company name."),
)

DUE_ITEMS: tuple[tuple[int, str, str], ...] = (
    # (hours from now, agent, title)
    (-3, "ops_agent", "Call back Jordan Example on 555-0142 about the gutter estimate"),
    (5, "*", "Send the weekly open jobs summary"),
    (24 * 6, "ops_agent", "Renew the equipment rental (not due yet, should not appear)"),
)

MEMORIES: tuple[tuple[str, str], ...] = (
    ("customers", "Alex Sample prefers text messages over calls and is only free after 4 pm."),
    ("customers", "Jordan Example (jordan@example.com) asked for estimates as a PDF attachment."),
    ("pricing", "Gutter cleaning is priced per metre; the minimum charge covers 20 metres."),
    ("process", "Invoice reminders go out 7 and 14 days after the due date, never earlier."),
    ("process", "Photos of finished work are filed under the job number before the invoice."),
)


def seed(service: BrainService) -> dict[str, int]:
    """Load the sample data relative to the service clock. Returns row counts."""
    now = service.now()
    for priority, scope, text in RULES:
        service.add_rule(text, scope=scope, priority=priority)
    for hours_ago, agent, text in CORRECTIONS:
        service.store.add_correction(text, agent, "human", now - timedelta(hours=hours_ago))
    for hours_ahead, agent, title in DUE_ITEMS:
        service.add_due_item(title, now + timedelta(hours=hours_ahead), agent_key=agent)
    for topic, text in MEMORIES:
        service.remember(text, topic=topic)
    return {
        "rules": len(RULES),
        "corrections": len(CORRECTIONS),
        "due_items": len(DUE_ITEMS),
        "memories": len(MEMORIES),
    }

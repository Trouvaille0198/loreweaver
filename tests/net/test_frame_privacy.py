"""The wire frame carries a privacy flag.

A line the server unicasts to ONE connection — a sensitive command reply, a
failed-turn refusal, a private echo — must be distinguishable from broadcast
table content on the receiving side, so a client can mark it as visible to
this seat alone and never present it as something the whole room saw.
"""

from __future__ import annotations

from gateway.hub import Event
from infra.i18n import get_i18n
from net.session import error_frame, render_frame


def test_private_narrative_frame_is_marked():
    frame = render_frame(Event.narrative(speaker="kp", text="only for you", private=True))
    assert frame is not None
    assert frame["private"] is True


def test_public_narrative_frame_has_no_private_flag():
    frame = render_frame(Event.narrative(speaker="kp", text="for everyone"))
    assert frame is not None
    assert "private" not in frame


def test_private_player_action_echo_is_marked():
    event = Event.player_action(name="Nyx", text=".model key")
    event.private = True
    frame = render_frame(event)
    assert frame is not None
    assert frame["private"] is True


def test_public_player_action_echo_has_no_private_flag():
    frame = render_frame(Event.player_action(name="Nyx", text="I open the door."))
    assert frame is not None
    assert "private" not in frame


def test_error_frame_is_always_marked_private():
    frame = error_frame("forbidden", get_i18n("en"))
    assert frame["type"] == "error"
    assert frame["private"] is True

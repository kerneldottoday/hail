"""The shimmering logo: same letters on every frame, a band of light that moves."""

from hail import tui_logo


def test_every_frame_spells_the_logo_and_tagline():
    text = tui_logo.frame(7).plain
    assert all(line.rstrip() in text for line in tui_logo.LOGO) and tui_logo.TAGLINE in text


def test_the_band_moves_and_the_sweep_repeats():
    styles = [[str(span.style) for span in tui_logo.frame(s).spans] for s in (0, 10, tui_logo.PERIOD + 10)]
    assert styles[0] != styles[1] and styles[1] == styles[2]


def test_the_band_lights_some_letters_and_rests_off_screen():
    lit = {str(s.style) for s in tui_logo.frame(10).spans}
    assert any(g in style for g in tui_logo.GLOW for style in lit)
    resting = {str(s.style) for s in tui_logo.frame(tui_logo.PERIOD - 1).spans if "dim" not in str(s.style)}
    assert resting == {f"bold {tui_logo.BASE}"}

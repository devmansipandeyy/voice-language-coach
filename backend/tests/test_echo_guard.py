"""Echo-overlap barge-in guard (PLAN.md R1) — the load-bearing piece of
full-duplex. These cover the accept/reject/boundary cases the eng review asked
for: a real interruption is accepted, the agent hearing itself is rejected, and
the threshold behaves exactly at the boundary."""
from backend.session import echo_is_real_barge_in


def test_too_few_words_is_not_barge_in():
    # A single stray word shouldn't cut the coach off.
    assert echo_is_real_barge_in("hola", "", min_words=2, echo_overlap=0.5) is False


def test_real_interruption_low_overlap_accepted():
    # Learner says something unrelated to the coach's current sentence.
    assert echo_is_real_barge_in("wait stop please", "como estas hoy amigo", 2, 0.5) is True


def test_echo_high_overlap_rejected():
    # Partial mirrors the coach's in-flight speech → it's the agent's own voice.
    assert echo_is_real_barge_in("como estas hoy", "como estas hoy amigo", 2, 0.5) is False


def test_no_agent_speech_treated_as_real():
    # Nothing playing → any qualifying partial is a real learner utterance.
    assert echo_is_real_barge_in("hello there friend", "", 2, 0.5) is True


def test_boundary_overlap_equals_threshold_is_echo():
    # "como wait" vs "como estas": overlap = 1/2 = 0.5. Reject when overlap >=
    # threshold, so exactly 0.5 with threshold 0.5 is NOT a barge-in.
    assert echo_is_real_barge_in("como wait", "como estas", 2, 0.5) is False


def test_lower_threshold_is_more_eager():
    # Same input, threshold 0.6 → 0.5 < 0.6 → now counts as a real barge-in.
    assert echo_is_real_barge_in("como wait", "como estas", 2, 0.6) is True

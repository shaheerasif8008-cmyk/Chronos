"""Chat routing: which messages take the fast tool-free path vs the agent loop.

The gate is biased toward the loop — a misrouted tool-needing message would
silently lose tool access, so only obviously conversational messages skip it.
"""
import pytest

from routers.chat import _is_trivial_chat


@pytest.mark.parametrize(
    "message",
    [
        "hi",
        "hello",
        "thanks",
        "thank you",
        "ok",
        "cool",
        "got it",
        "",
        "   ",
        "nice work",          # 2 words, no question, no tool hint
    ],
)
def test_trivial_messages_take_fast_path(message):
    assert _is_trivial_chat(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "what is our refund policy?",          # question
        "find the latest funding news",        # tool hints: find, latest, news
        "search for competitors",              # tool hint: search
        "draft an email to the CFO",           # tool hint: draft, email
        "summarize this quarter's pipeline",   # tool hint: summarize
        "who are the top 5 vendors we use",    # >3 words, substantive
        "look up the Q3 numbers",              # tool hint: look
    ],
)
def test_substantive_messages_route_through_loop(message):
    assert _is_trivial_chat(message) is False


def test_question_mark_forces_loop_even_when_short():
    assert _is_trivial_chat("why?") is False


def test_format_task_answer_renders_plain_chat_reply():
    """A conversational loop result must render as just its text, not a task summary."""
    from runtime.agent_loop import format_task_answer

    assert format_task_answer({"answer": "The capital of France is Paris."}) == "The capital of France is Paris."

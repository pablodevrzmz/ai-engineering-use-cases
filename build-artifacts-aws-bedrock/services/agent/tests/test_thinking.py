"""ThinkingFilter: Nova's <thinking> blocks never reach the user, even when tags are split across deltas."""
from app.agent import ThinkingFilter


def run(deltas: list[str]) -> str:
    f = ThinkingFilter()
    out = "".join(f.feed(d) for d in deltas)
    return out + f.flush()


def test_plain_text_passes_through():
    assert run(["Hello, ", "world"]) == "Hello, world"


def test_whole_block_in_one_delta():
    assert run(["<thinking>plan</thinking> The file is ready."]) == "The file is ready."


def test_tags_split_across_deltas():
    assert run(["<thi", "nking>pl", "an</thi", "nking>\n\nDone."]) == "Done."


def test_multiple_blocks_and_text_between():
    assert run(["A <thinking>x</thinking> B <thinking>y</thinking> C"]) == "A B C"


def test_unterminated_block_is_dropped_at_flush():
    assert run(["Visible. <thinking>never closed"]) == "Visible. "


def test_lone_angle_bracket_is_released():
    assert run(["1 < 2 and 3 > 2"]) == "1 < 2 and 3 > 2"


def test_partial_prefix_that_is_not_a_tag_is_released():
    assert run(["see <think", "ers>"]) == "see <thinkers>"


def test_tools_run_sequentially():
    """write-then-publish must not race: the agent is built with the sequential tool executor."""
    from strands.tools.executors import SequentialToolExecutor

    from app.agent import build_agent
    from app.settings import Settings

    agent = build_agent(Settings(), tools=[], hooks=[], publishing=False)
    assert isinstance(agent.tool_executor, SequentialToolExecutor)

def test_format_task_answer_renders_plain_chat_reply():
    from runtime.agent_loop import format_task_answer
    assert format_task_answer({"answer": "The capital of France is Paris."}) == "The capital of France is Paris."

from core.task_envelope import build_task_envelope, envelope_to_agent_prompt, extract_task_entities


def test_task_envelope_preserves_github_repo_url_from_raw_message():
    raw = (
        "https://github.com/shaheerasif8008-cmyk/Chronos.git "
        "this is my repo, run it and tell me about its strengths and weaknesses"
    )

    envelope = build_task_envelope(
        task_id="task-1",
        raw_user_message=raw,
        ui_title="Run the provided GitHub repository and analyze its strengths and weaknesses",
        router_decision={
            "mode": "agent",
            "ui_title": "Run the provided GitHub repository and analyze its strengths and weaknesses",
        },
    )
    prompt = envelope_to_agent_prompt(envelope)

    assert envelope.raw_user_message == raw
    assert raw in prompt
    assert "https://github.com/shaheerasif8008-cmyk/Chronos.git" in envelope.extracted_entities.repo_urls
    assert "https://github.com/shaheerasif8008-cmyk/Chronos.git" in prompt
    assert "Use the Original user request as the source of truth" in prompt


def test_task_envelope_preserves_email_date_and_money_literals():
    raw = "Email john@example.com tomorrow at 3pm about the $4,500 invoice."

    entities = extract_task_entities(raw)
    prompt = envelope_to_agent_prompt(build_task_envelope(
        task_id="task-2",
        raw_user_message=raw,
        ui_title="Email about invoice",
        router_decision={"mode": "agent", "ui_title": "Email about invoice"},
    ))

    assert "john@example.com" in entities.emails
    assert "tomorrow" in entities.dates
    assert "3pm" in entities.dates
    assert "$4,500" in entities.money_amounts
    assert raw in prompt


def test_task_envelope_preserves_quoted_filename_and_slide_instruction():
    raw = 'Use the file "Q4_board_deck_final.pdf" and summarize slide 7 only.'

    entities = extract_task_entities(raw)
    prompt = envelope_to_agent_prompt(build_task_envelope(
        task_id="task-3",
        raw_user_message=raw,
        ui_title="Summarize board deck slide",
        router_decision={"mode": "agent", "ui_title": "Summarize board deck slide"},
    ))

    assert "Q4_board_deck_final.pdf" in entities.file_names
    assert "slide 7 only" in prompt
    assert raw in prompt


def test_lossy_router_summary_cannot_replace_raw_execution_prompt():
    raw = (
        "https://github.com/shaheerasif8008-cmyk/Chronos.git "
        "this is my repo, run it and tell me about its strengths and weaknesses"
    )
    lossy_summary = "Run the provided GitHub repository and analyze its strengths and weaknesses"

    envelope = build_task_envelope(
        task_id="task-4",
        raw_user_message=raw,
        ui_title=lossy_summary,
        router_decision={"mode": "agent", "ui_title": lossy_summary},
    )
    prompt = envelope_to_agent_prompt(envelope)

    assert envelope.ui.title == lossy_summary
    assert envelope.raw_user_message == raw
    assert raw in prompt
    assert "Original user request:" in prompt
    assert "Router metadata:" in prompt

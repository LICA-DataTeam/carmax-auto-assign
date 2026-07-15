from src.integrations.liveagent.messages import extract_private_message_page_name


def test_extract_private_message_page_name_finds_match_in_nested_payload() -> None:
    payload = {
        "messages": [
            {
                "messages": [
                    {
                        "type": "system",
                        "message": "Private message to: <a href='https://facebook.com/123'>Carmax Authorized Agent - Mark john Castro</a>",
                    },
                    {"type": "message", "message": "Location"},
                ]
            }
        ]
    }
    assert (
        extract_private_message_page_name(payload)
        == "Carmax Authorized Agent - Mark john Castro"
    )


def test_extract_private_message_page_name_returns_none_when_absent() -> None:
    payload = {"messages": [{"messages": [{"type": "message", "message": "hi there"}]}]}
    assert extract_private_message_page_name(payload) is None


def test_extract_private_message_page_name_handles_list_payload() -> None:
    payload = [
        {"message": "hello"},
        {"message": "Private message to: <a href='#'>CarMax Auto Dealership</a>"},
    ]
    assert extract_private_message_page_name(payload) == "CarMax Auto Dealership"


def test_extract_private_message_page_name_handles_non_dict_non_list_payload() -> None:
    assert extract_private_message_page_name(None) is None
    assert extract_private_message_page_name("just a string") is None
    assert extract_private_message_page_name(42) is None

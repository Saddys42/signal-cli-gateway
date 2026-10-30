from app.events import normalize_receive


def test_normalize_direct_message():
    event = normalize_receive({
        "method": "receive",
        "params": {"account": "+39000", "envelope": {
            "sourceNumber": "+39111", "sourceName": "Mario",
            "timestamp": 42, "dataMessage": {"message": "ciao"}}},
    })
    assert event["text"] == "ciao"
    assert event["source"] == "+39111"
    assert event["group_id"] is None
    assert event["account"] == "+39000"


def test_normalize_group_and_sync_wrapped():
    event = normalize_receive({
        "method": "receive",
        "params": {"result": {"envelope": {
            "sourceNumber": "+39111", "timestamp": 1,
            "syncMessage": {"sentMessage": {
                "message": "from phone", "groupInfo": {"groupId": "abc="}}}}}},
    })
    assert event["is_sync"] is True
    assert event["group_id"] == "abc="
    assert event["text"] == "from phone"


def test_non_message_is_ignored():
    assert normalize_receive({"method": "somethingElse", "params": {}}) is None

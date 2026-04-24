"""Tests for WhatsApp Open Lines conversation export."""
from __future__ import annotations

from bitrix_ingest.application.whatsapp import WhatsAppExportService
from bitrix_ingest.application.whatsapp.export_service import (
    WhatsAppExportRequest,
    _OutputDirectories,
)


def _deal(deal_id: str, *, contact_id: str = "64050") -> dict[str, str]:
    return {
        "ID": deal_id,
        "TITLE": ". - WhatsApp",
        "CONTACT_ID": contact_id,
        "SOURCE_ID": "WZ-1",
        "ASSIGNED_BY_ID": "5",
        "STAGE_ID": "NEW",
        "STAGE_SEMANTIC_ID": "P",
        "CATEGORY_ID": "0",
        "DATE_CREATE": "2026-04-19T16:32:29+03:00",
        "DATE_MODIFY": "2026-04-19T18:44:07+03:00",
        "LAST_COMMUNICATION_TIME": "2026-04-19T18:44:07+03:00",
    }


class _Gateway:
    def __init__(self, responses: dict[str, list[dict[str, object]]]) -> None:
        self._responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[dict[str, object]] = []
        self.page_delay = 0.0

    def call(self, method: str, body: dict | None = None, label: str | None = None) -> dict:
        self.calls.append({"method": method, "body": body, "label": label})
        queue = self._responses.get(method)
        assert queue, f"unexpected method call: {method}"
        return queue.pop(0)


class _Sink:
    def __init__(self) -> None:
        self.documents: dict[str, object] = {}

    def write(self, path, data) -> None:
        self.documents[str(path)] = data


def test_build_conversation_reads_deal_openline_history(tmp_path):
    gateway = _Gateway(
        {
            "imopenlines.crm.chat.get": [
                {
                    "result": [
                        {
                            "CHAT_ID": "70000",
                            "CONNECTOR_ID": "telegram",
                            "CONNECTOR_TITLE": "Telegram",
                        },
                        {
                            "CHAT_ID": "64990",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        },
                    ]
                }
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 64990,
                        "name": ". - WhatsApp",
                        "dialog_id": "chat64990",
                        "entity_data_1": "Y|DEAL|51044|N|N|26250|1776605548|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 26250,
                        "message": {
                            "1071670": {
                                "id": "1071670",
                                "senderid": "0",
                                "date": "2026-04-19T16:32:29+03:00",
                                "text": "[b]Created deal[/b]",
                                "params": {},
                            },
                            "1071674": {
                                "id": "1071674",
                                "senderid": "21408",
                                "date": "2026-04-19T16:32:30+03:00",
                                "text": "Hello",
                                "params": {"fileId": ["55"]},
                            },
                            "1071676": {
                                "id": "1071676",
                                "senderid": "5",
                                "date": "2026-04-19T16:32:58+03:00",
                                "text": "=== Outgoing message, author: Phone ===\nHi there!",
                                "params": {},
                            },
                        },
                        "users": {
                            "21408": {
                                "id": "21408",
                                "name": ".",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            },
                            "5": {
                                "id": "5",
                                "name": "Manager",
                                "connector": False,
                            },
                        },
                        "files": {
                            "55": {
                                "id": "55",
                                "name": "price.pdf",
                                "urlDownload": "https://cdn.example.com/price.pdf",
                            }
                        },
                    }
                }
            ],
        }
    )
    sink = _Sink()
    service = WhatsAppExportService(gateway=gateway, sink=sink)
    dirs = _OutputDirectories.prepare(tmp_path)

    conversation = service._build_conversation_with_fallback(
        _deal("51044"),
        "51044",
        dirs.paths_for("51044"),
        include_system_messages=True,
    )

    assert conversation.timeline_source == "deal"
    assert conversation.chat_id == "64990"
    assert conversation.session_id == "26250"
    assert conversation.dialog_id == "chat64990"
    assert conversation.connector_title == "WAZZUP: WhatsApp"
    assert conversation.chat_name == ". - WhatsApp"
    assert conversation.stage_semantic_id == "P"
    assert [message.sender_role for message in conversation.messages] == ["system", "client", "manager"]
    assert conversation.messages[0].text == "Created deal"
    assert conversation.messages[1].attachments[0].url == "https://cdn.example.com/price.pdf"
    assert conversation.messages[1].sender_label is None
    assert conversation.messages[2].sender_label == "Manager"
    assert gateway.calls[2]["body"] == {"SESSION_ID": 26250}

    raw_payload = sink.documents[str(dirs.paths_for("51044").deal_openline_raw)]
    assert raw_payload["binding"]["chat_id"] == "64990"
    assert raw_payload["history"]["sessionId"] == 26250


def test_build_conversation_falls_back_to_contact_openline_history(tmp_path):
    gateway = _Gateway(
        {
            "imopenlines.crm.chat.get": [
                {"result": []},
                {
                    "result": [
                        {
                            "CHAT_ID": "64991",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        }
                    ]
                },
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 64991,
                        "name": "Contact chat",
                        "dialog_id": "chat64991",
                        "entity_data_1": "Y|CONTACT|64050|N|N|26251|1776605548|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 26251,
                        "message": {
                            "1071700": {
                                "id": "1071700",
                                "senderid": "21408",
                                "date": "2026-04-19T16:40:00+03:00",
                                "text": "Hello from contact",
                                "params": {},
                            }
                        },
                        "users": {
                            "21408": {
                                "id": "21408",
                                "name": "Client",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            }
                        },
                        "files": [],
                    }
                }
            ],
        }
    )
    sink = _Sink()
    service = WhatsAppExportService(gateway=gateway, sink=sink)
    dirs = _OutputDirectories.prepare(tmp_path)

    conversation = service._build_conversation_with_fallback(
        _deal("51044", contact_id="64050"),
        "51044",
        dirs.paths_for("51044"),
        include_system_messages=True,
    )

    assert conversation.timeline_source == "contact"
    assert conversation.timeline_entity_type == "contact"
    assert conversation.timeline_entity_id == "64050"
    assert conversation.chat_id == "64991"
    assert conversation.stats.total_messages == 1
    assert conversation.messages[0].text == "Hello from contact"

    deal_raw = sink.documents[str(dirs.paths_for("51044").deal_openline_raw)]
    contact_raw = sink.documents[str(dirs.paths_for("51044").contact_openline_raw)]
    assert deal_raw["binding"] is None
    assert contact_raw["binding"]["timeline_source"] == "contact"


def test_outgoing_connector_message_is_classified_as_manager(tmp_path):
    gateway = _Gateway(
        {
            "imopenlines.crm.chat.get": [
                {
                    "result": [
                        {
                            "CHAT_ID": "64992",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        }
                    ]
                }
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 64992,
                        "name": ". - WhatsApp",
                        "dialog_id": "chat64992",
                        "entity_data_1": "Y|DEAL|51054|N|N|26252|1776605548|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 26252,
                        "message": {
                            "1071706": {
                                "id": "1071706",
                                "senderid": "21410",
                                "date": "2026-04-19T19:37:37+03:00",
                                "text": "=== Outgoing message, author: Phone ===\nHello",
                                "params": {},
                            }
                        },
                        "users": {
                            "21410": {
                                "id": "21410",
                                "name": "Client",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            }
                        },
                        "files": [],
                    }
                }
            ],
        }
    )
    service = WhatsAppExportService(gateway=gateway, sink=_Sink())
    dirs = _OutputDirectories.prepare(tmp_path)

    conversation = service._build_conversation_with_fallback(
        _deal("51054"),
        "51054",
        dirs.paths_for("51054"),
        include_system_messages=True,
    )

    assert conversation.messages[0].sender_role == "manager"
    assert conversation.messages[0].text == "Hello"
    assert conversation.messages[0].sender_label == "Phone"


def test_client_reply_message_drops_quoted_prefix(tmp_path):
    gateway = _Gateway(
        {
            "imopenlines.crm.chat.get": [
                {
                    "result": [
                        {
                            "CHAT_ID": "64993",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        }
                    ]
                }
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 64993,
                        "name": ". - WhatsApp",
                        "dialog_id": "chat64993",
                        "entity_data_1": "Y|DEAL|51055|N|N|26253|1776605548|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 26253,
                        "message": {
                            "1071708": {
                                "id": "1071708",
                                "senderid": "21410",
                                "date": "2026-04-19T19:37:45+03:00",
                                "text": ">>Which city?\n\n\nShymkent",
                                "params": {},
                            }
                        },
                        "users": {
                            "21410": {
                                "id": "21410",
                                "name": "Client",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            }
                        },
                        "files": [],
                    }
                }
            ],
        }
    )
    service = WhatsAppExportService(gateway=gateway, sink=_Sink())
    dirs = _OutputDirectories.prepare(tmp_path)

    conversation = service._build_conversation_with_fallback(
        _deal("51055"),
        "51055",
        dirs.paths_for("51055"),
        include_system_messages=True,
    )

    assert conversation.messages[0].sender_role == "client"
    assert conversation.messages[0].text == "Shymkent"
    assert conversation.messages[0].sender_label == "Client"


def test_quoted_outgoing_connector_message_is_classified_as_manager(tmp_path):
    gateway = _Gateway(
        {
            "imopenlines.crm.chat.get": [
                {
                    "result": [
                        {
                            "CHAT_ID": "64994",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        }
                    ]
                }
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 64994,
                        "name": ". - WhatsApp",
                        "dialog_id": "chat64994",
                        "entity_data_1": "Y|DEAL|51056|N|N|26254|1776605548|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 26254,
                        "message": {
                            "1071712": {
                                "id": "1071712",
                                "senderid": "21410",
                                "date": "2026-04-19T19:38:15+03:00",
                                "text": ">>Shymkent\n\n\n=== Outgoing message, author: Phone ===\nBazaryk 261/1",
                                "params": {},
                            }
                        },
                        "users": {
                            "21410": {
                                "id": "21410",
                                "name": "Client",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            }
                        },
                        "files": [],
                    }
                }
            ],
        }
    )
    service = WhatsAppExportService(gateway=gateway, sink=_Sink())
    dirs = _OutputDirectories.prepare(tmp_path)

    conversation = service._build_conversation_with_fallback(
        _deal("51056"),
        "51056",
        dirs.paths_for("51056"),
        include_system_messages=True,
    )

    assert conversation.messages[0].sender_role == "manager"
    assert conversation.messages[0].text == "Bazaryk 261/1"
    assert conversation.messages[0].sender_label == "Phone"


def test_lowercase_file_urls_are_exported_as_attachments(tmp_path):
    gateway = _Gateway(
        {
            "imopenlines.crm.chat.get": [
                {
                    "result": [
                        {
                            "CHAT_ID": "64992",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        }
                    ]
                }
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 64992,
                        "name": ". - WhatsApp",
                        "dialog_id": "chat64992",
                        "entity_data_1": "Y|DEAL|51054|N|N|26252|1776605548|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 26252,
                        "message": {
                            "1071710": {
                                "id": "1071710",
                                "senderid": "21410",
                                "date": "2026-04-19T19:38:09+03:00",
                                "text": "",
                                "params": {"fileId": ["231600"]},
                            }
                        },
                        "users": {
                            "21410": {
                                "id": "21410",
                                "name": "Client",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            }
                        },
                        "files": {
                            "231600": {
                                "id": 231600,
                                "type": "audio",
                                "name": "voice.mp3",
                                "urldownload": "https://cdn.example.com/voice.mp3",
                            }
                        },
                    }
                }
            ],
        }
    )
    service = WhatsAppExportService(gateway=gateway, sink=_Sink())
    dirs = _OutputDirectories.prepare(tmp_path)

    conversation = service._build_conversation_with_fallback(
        _deal("51054"),
        "51054",
        dirs.paths_for("51054"),
        include_system_messages=True,
    )

    assert conversation.messages[0].text == ""
    assert [attachment.to_dict() for attachment in conversation.messages[0].attachments] == [
        {
            "type": "audio",
            "label": "voice.mp3",
            "url": "https://cdn.example.com/voice.mp3",
        }
    ]


def test_build_conversation_uses_chat_id_when_dialog_session_id_is_zero(tmp_path):
    gateway = _Gateway(
        {
            "imopenlines.crm.chat.get": [
                {
                    "result": [
                        {
                            "CHAT_ID": "41080",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        }
                    ]
                }
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 41080,
                        "name": ". - WhatsApp",
                        "dialog_id": "chat41080",
                        "entity_data_1": "Y|DEAL|28736|N|N|0|1776605548|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 0,
                        "message": {
                            "1": {
                                "id": "1",
                                "senderid": "21408",
                                "date": "2026-04-19T16:32:30+03:00",
                                "text": "Hello from client",
                                "params": {},
                            },
                        },
                        "users": {
                            "21408": {
                                "id": "21408",
                                "name": "Client",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            }
                        },
                        "files": [],
                    }
                }
            ],
        }
    )
    service = WhatsAppExportService(gateway=gateway, sink=_Sink())
    dirs = _OutputDirectories.prepare(tmp_path)

    conversation = service._build_conversation_with_fallback(
        _deal("28736"),
        "28736",
        dirs.paths_for("28736"),
        include_system_messages=True,
    )

    assert conversation.chat_id == "41080"
    assert conversation.session_id == ""
    assert conversation.stats.total_messages == 1
    assert gateway.calls[2]["body"] == {"CHAT_ID": 41080}


def test_execute_can_exclude_system_messages_from_output(tmp_path):
    gateway = _Gateway(
        {
            "profile": [{"result": {"ID": 1, "NAME": "", "LAST_NAME": ""}}],
            "crm.deal.list": [{"result": [_deal("51044")]}],
            "imopenlines.crm.chat.get": [
                {
                    "result": [
                        {
                            "CHAT_ID": "64990",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        }
                    ]
                }
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 64990,
                        "name": ". - WhatsApp",
                        "dialog_id": "chat64990",
                        "entity_data_1": "Y|DEAL|51044|N|N|26250|1776605548|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 26250,
                        "message": {
                            "1": {
                                "id": "1",
                                "senderid": "0",
                                "date": "2026-04-19T16:32:29+03:00",
                                "text": "[b]Created deal[/b]",
                                "params": {},
                            },
                            "2": {
                                "id": "2",
                                "senderid": "21408",
                                "date": "2026-04-19T16:32:30+03:00",
                                "text": "Hello from client",
                                "params": {},
                            },
                        },
                        "users": {
                            "21408": {
                                "id": "21408",
                                "name": "Client",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            }
                        },
                        "files": [],
                    }
                }
            ],
        }
    )
    sink = _Sink()
    service = WhatsAppExportService(gateway=gateway, sink=sink)

    service.execute(
        WhatsAppExportRequest(
            output_dir=tmp_path,
            limit=1,
            include_system_messages=False,
        )
    )

    conversation = sink.documents[str(tmp_path / "conversations" / "deal_51044.json")]
    assert [message["sender_role"] for message in conversation["messages"]] == ["client"]
    assert conversation["stats"]["system_messages"] == 0
    report = sink.documents[str(tmp_path / "report.json")]
    assert report["totals"]["system_messages"] == 0


def test_execute_keeps_full_chat_history_for_selected_deal(tmp_path):
    deal = _deal("43428", contact_id="57096") | {
        "TITLE": "77055997906 - WhatsApp",
        "DATE_CREATE": "2025-12-10T13:18:57+03:00",
        "DATE_MODIFY": "2026-04-14T11:48:28+03:00",
        "LAST_COMMUNICATION_TIME": "2026-04-14T13:47:36",
    }
    gateway = _Gateway(
        {
            "profile": [{"result": {"ID": 1, "NAME": "", "LAST_NAME": ""}}],
            "crm.deal.list": [{"result": [deal]}],
            "imopenlines.crm.chat.get": [
                {
                    "result": [
                        {
                            "CHAT_ID": "55564",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        }
                    ]
                }
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 55564,
                        "name": "77055997906 - WhatsApp",
                        "dialog_id": "chat55564",
                        "entity_data_1": "Y|DEAL|43428|N|N|22870|1765361937|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 22870,
                        "message": {
                            "948804": {
                                "id": "948804",
                                "senderid": "0",
                                "date": "2025-12-10T13:18:56+03:00",
                                "text": "Начат новый диалог №22870",
                                "params": {},
                            },
                            "951668": {
                                "id": "951668",
                                "senderid": "18606",
                                "date": "2025-12-13T14:08:10+03:00",
                                "text": "Терезелер жөнінде ақпарат керек",
                                "params": {},
                            },
                            "956468": {
                                "id": "956468",
                                "senderid": "18606",
                                "date": "2025-12-20T15:25:10+03:00",
                                "text": "=== Исходящее сообщение, автор: Битрикс24 (Жанна) ===\nСалеметсіз бе",
                                "params": {},
                            },
                        },
                        "users": {
                            "18606": {
                                "id": "18606",
                                "name": "77055997906",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            }
                        },
                        "files": [],
                    }
                }
            ],
        }
    )
    sink = _Sink()
    service = WhatsAppExportService(gateway=gateway, sink=sink)

    service.execute(
        WhatsAppExportRequest(
            output_dir=tmp_path,
            limit=1,
            date_from="2026-03-01",
            include_system_messages=True,
        )
    )

    conversation = sink.documents[str(tmp_path / "conversations" / "deal_43428.json")]
    assert [message["created_at"] for message in conversation["messages"]] == [
        "2025-12-10T13:18:56+03:00",
        "2025-12-13T14:08:10+03:00",
        "2025-12-20T15:25:10+03:00",
    ]
    assert [message["sender_role"] for message in conversation["messages"]] == [
        "system",
        "client",
        "manager",
    ]
    assert conversation["stats"]["total_messages"] == 3

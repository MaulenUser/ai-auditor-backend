"""Tests for WhatsApp comment normalization and deal/contact conversation assembly."""
from __future__ import annotations

from bitrix_ingest.application.whatsapp import (
    CommentParser,
    ConversationAssembler,
    UrlAttachmentExtractor,
    WhitespaceNormalizer,
)

_NORMALIZER = WhitespaceNormalizer()
_ATTACHMENT_EXTRACTOR = UrlAttachmentExtractor(_NORMALIZER)
_PARSER = CommentParser(
    attachment_extractor=_ATTACHMENT_EXTRACTOR,
    whitespace=_NORMALIZER,
)
_ASSEMBLER = ConversationAssembler(parser=_PARSER)


# ---------------------------------------------------------------------------
# WhitespaceNormalizer
# ---------------------------------------------------------------------------

class TestNormalizeWhitespace:
    def test_collapses_multiple_spaces(self):
        assert _NORMALIZER.normalize("hello   world") == "hello world"

    def test_converts_nbsp(self):
        assert _NORMALIZER.normalize("a\u00a0b") == "a b"

    def test_normalises_crlf(self):
        result = _NORMALIZER.normalize("line1\r\nline2")
        assert result == "line1\nline2"

    def test_strips_leading_trailing(self):
        assert _NORMALIZER.normalize("  hello  ") == "hello"

    def test_per_line_normalisation(self):
        result = _NORMALIZER.normalize("  a  b  \n  c  d  ")
        assert result == "a b\nc d"


# ---------------------------------------------------------------------------
# UrlAttachmentExtractor
# ---------------------------------------------------------------------------

class TestGetUrlAttachments:
    def test_extracts_single_attachment(self):
        text = "[url=https://example.com/file.pdf]Download PDF[/url]"
        attachments = _ATTACHMENT_EXTRACTOR.extract(text)
        assert len(attachments) == 1
        assert attachments[0].url == "https://example.com/file.pdf"
        assert attachments[0].label == "Download PDF"
        assert attachments[0].type == "file"

    def test_extracts_multiple_attachments(self):
        text = "[url=https://a.com/a.png]Image A[/url] [url=https://b.com/b.pdf]Doc B[/url]"
        attachments = _ATTACHMENT_EXTRACTOR.extract(text)
        assert len(attachments) == 2
        assert attachments[0].url == "https://a.com/a.png"
        assert attachments[1].url == "https://b.com/b.pdf"

    def test_no_attachments_returns_empty(self):
        assert _ATTACHMENT_EXTRACTOR.extract("plain text with no links") == []

    def test_label_is_whitespace_normalised(self):
        text = "[url=https://x.com/f]  some   label  [/url]"
        attachments = _ATTACHMENT_EXTRACTOR.extract(text)
        assert attachments[0].label == "some label"


# ---------------------------------------------------------------------------
# CommentParser
# ---------------------------------------------------------------------------

DEAL = {"ID": "42", "TITLE": "Test Client"}


class TestNormalizeComment:
    _IMG = "[img]https://cdn.wazzup24.com/whatsapp.png[/img]"

    def _comment(self, text: str, comment_id: str = "1") -> dict:
        return {"ID": comment_id, "CREATED": "2024-01-01T10:00:00", "AUTHOR_ID": "5", "COMMENT": text}

    def _wa_comment(self, body: str, comment_id: str = "1") -> dict:
        return self._comment(f"{self._IMG}\n{body}", comment_id)

    def test_non_whatsapp_comment_returns_none(self):
        comment = self._comment("Regular CRM note")
        assert _PARSER.parse(comment, DEAL) is None

    def test_wazzup24_in_url_triggers_whatsapp(self):
        comment = self._comment("[url=https://cdn.wazzup24.com/file.jpg]img[/url]")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None

    def test_whatsapp_png_in_img_triggers_whatsapp(self):
        comment = self._comment(self._IMG)
        result = _PARSER.parse(comment, DEAL)
        assert result is not None

    def test_system_wz_sets_role_system(self):
        comment = self._wa_comment("=== SYSTEM WZ === connection event")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert result.sender_role == "system"
        assert result.is_system_message is True

    def test_sender_matching_deal_title_is_client(self):
        comment = self._wa_comment("Test Client:\nHello from client")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert result.sender_role == "client"
        assert result.sender_label == "Test Client"
        assert result.text == "Hello from client"

    def test_sender_not_matching_deal_title_is_manager(self):
        comment = self._wa_comment("Manager Name:\nHello from manager")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert result.sender_role == "manager"
        assert result.sender_label == "Manager Name"

    def test_no_sender_line_is_unknown(self):
        comment = self._comment("wazzup24 just some text without a sender line")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert result.sender_role == "unknown"
        assert result.sender_label is None

    def test_img_tags_stripped_from_text(self):
        comment = self._comment("wazzup24 [img]https://cdn.example.com/icon.png[/img] text")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert "[img]" not in result.text

    def test_url_tags_replaced_with_label(self):
        comment = self._comment("wazzup24 [url=https://cdn.example.com/file.pdf]Attachment[/url]")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert "Attachment" in result.text
        assert "[url=" not in result.text

    def test_url_attachments_extracted(self):
        comment = self._comment("wazzup24 [url=https://cdn.example.com/file.pdf]File[/url]")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert len(result.attachments) == 1
        assert result.attachments[0].url == "https://cdn.example.com/file.pdf"

    def test_html_entities_decoded(self):
        comment = self._comment("wazzup24 &amp; hello &lt;world&gt;")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert "&amp;" not in result.text

    def test_raw_comment_preserved(self):
        raw = f"{self._IMG}\nTest Client:\nhello"
        comment = self._comment(raw)
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert result.raw_comment == raw

    def test_deal_id_set_on_message(self):
        comment = self._comment("wazzup24 message")
        result = _PARSER.parse(comment, DEAL)
        assert result is not None
        assert result.deal_id == "42"


# ---------------------------------------------------------------------------
# ConversationAssembler
# ---------------------------------------------------------------------------

class TestBuildConversation:
    _IMG = "[img]https://cdn.wazzup24.com/whatsapp.png[/img]"

    def _make_comment(self, comment_id: str, text: str) -> dict:
        return {"ID": comment_id, "CREATED": f"2024-01-01T{comment_id.zfill(2)}:00:00", "AUTHOR_ID": "5", "COMMENT": text}

    def _wa(self, comment_id: str, sender: str, body: str) -> dict:
        return self._make_comment(comment_id, f"{self._IMG}\n{sender}:\n{body}")

    def test_empty_timeline_gives_zero_stats(self):
        conv = _ASSEMBLER.assemble(DEAL, [], "deal", "deal", "42")
        assert conv.stats.total_messages == 0
        assert conv.stats.first_message_at is None
        assert conv.stats.last_message_at is None

    def test_message_counts_correct(self):
        comments = [
            self._wa("1", "Test Client", "client msg"),
            self._wa("2", "Manager", "manager msg"),
            self._wa("3", "Manager", "another manager"),
        ]
        conv = _ASSEMBLER.assemble(DEAL, comments, "deal", "deal", "42")
        assert conv.stats.total_messages == 3
        assert conv.stats.client_messages == 1
        assert conv.stats.manager_messages == 2

    def test_first_and_last_message_at(self):
        comments = [
            self._make_comment("1", "wazzup24 msg one"),
            self._make_comment("9", "wazzup24 msg nine"),
        ]
        conv = _ASSEMBLER.assemble(DEAL, comments, "deal", "deal", "42")
        assert conv.stats.first_message_at == "2024-01-01T01:00:00"
        assert conv.stats.last_message_at == "2024-01-01T09:00:00"

    def test_non_whatsapp_comments_excluded(self):
        comments = [
            self._make_comment("1", "wazzup24 this is whatsapp"),
            self._make_comment("2", "Regular CRM activity note"),
        ]
        conv = _ASSEMBLER.assemble(DEAL, comments, "deal", "deal", "42")
        assert conv.stats.total_messages == 1

    def test_timeline_source_fields_propagated(self):
        conv = _ASSEMBLER.assemble(DEAL, [], "contact", "contact", "99")
        assert conv.timeline_source == "contact"
        assert conv.timeline_entity_type == "contact"
        assert conv.timeline_entity_id == "99"

    def test_messages_with_files_counted(self):
        comments = [
            self._make_comment("1", "wazzup24 [url=https://x.com/f.pdf]File[/url]"),
            self._make_comment("2", "wazzup24 plain text"),
        ]
        conv = _ASSEMBLER.assemble(DEAL, comments, "deal", "deal", "42")
        assert conv.stats.messages_with_files == 1

    def test_to_dict_includes_required_keys(self):
        conv = _ASSEMBLER.assemble(DEAL, [], "deal", "deal", "42")
        d = conv.to_dict()
        for key in (
            "channel", "integration", "deal_id", "contact_id", "deal_title",
            "timeline_source", "timeline_entity_type", "timeline_entity_id",
            "stats", "messages",
        ):
            assert key in d, f"missing key: {key}"


# ---------------------------------------------------------------------------
# Contact timeline fallback condition
# ---------------------------------------------------------------------------

class TestContactTimelineFallback:
    def test_empty_deal_conversation_detected(self):
        conv = _ASSEMBLER.assemble({"ID": "1", "TITLE": "X"}, [], "deal", "deal", "1")
        assert conv.stats.total_messages == 0

    def test_contact_conversation_replaces_when_has_messages(self):
        deal = {"ID": "1", "TITLE": "X", "CONTACT_ID": "99"}
        deal_conv = _ASSEMBLER.assemble(deal, [], "deal", "deal", "1")
        assert deal_conv.stats.total_messages == 0

        img = "[img]https://cdn.wazzup24.com/whatsapp.png[/img]"
        contact_comments = [
            {
                "ID": "10",
                "CREATED": "2024-01-01T10:00:00",
                "AUTHOR_ID": "5",
                "COMMENT": f"{img}\nX:\nclient hello",
            },
        ]
        contact_conv = _ASSEMBLER.assemble(deal, contact_comments, "contact", "contact", "99")
        assert contact_conv.stats.total_messages == 1

        chosen = contact_conv if contact_conv.stats.total_messages > 0 else deal_conv
        assert chosen.timeline_source == "contact"
        assert chosen.timeline_entity_id == "99"

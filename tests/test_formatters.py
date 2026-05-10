"""Pure-function unit tests for formatters: video-id parsing, slug, [MM:SS],
research-report frontmatter rendering. No network."""

from __future__ import annotations

import pytest

from mcp_youtube.formatters import (
    format_timestamp,
    parse_video_id,
    render_research_markdown,
    render_transcript_text,
    slugify,
)


class TestParseVideoId:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=42", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ],
    )
    def test_extracts_id(self, value: str, expected: str) -> None:
        assert parse_video_id(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not-a-url",
            "https://example.com/watch?v=dQw4w9WgXcQ",  # wrong host
            "https://www.youtube.com/watch?v=tooshort",
            "https://www.youtube.com/playlist?list=PL12345",
        ],
    )
    def test_rejects_invalid(self, value: str) -> None:
        assert parse_video_id(value) is None


class TestFormatTimestamp:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "[0:00]"),
            (5, "[0:05]"),
            (65, "[1:05]"),
            (599, "[9:59]"),
            (600, "[10:00]"),
            (3599, "[59:59]"),
            (3600, "[1:00:00]"),
            (3725, "[1:02:05]"),
        ],
    )
    def test_formats(self, seconds: float, expected: str) -> None:
        assert format_timestamp(seconds) == expected

    def test_negative_clamps_to_zero(self) -> None:
        assert format_timestamp(-5) == "[0:00]"


class TestRenderTranscriptText:
    def test_with_timestamps(self) -> None:
        snippets = [
            {"text": "hello world", "start": 0.0, "duration": 1.0},
            {"text": "next line", "start": 65.5, "duration": 1.0},
        ]
        rendered = render_transcript_text(snippets, with_timestamps=True)
        assert rendered == "[0:00] hello world\n[1:05] next line"

    def test_without_timestamps(self) -> None:
        snippets = [
            {"text": "alpha", "start": 0.0, "duration": 1.0},
            {"text": "beta", "start": 5.0, "duration": 1.0},
        ]
        rendered = render_transcript_text(snippets, with_timestamps=False)
        assert rendered == "alpha\nbeta"

    def test_skips_blank_text(self) -> None:
        snippets = [
            {"text": "alpha", "start": 0.0, "duration": 1.0},
            {"text": "   ", "start": 5.0, "duration": 1.0},
            {"text": "gamma", "start": 10.0, "duration": 1.0},
        ]
        rendered = render_transcript_text(snippets, with_timestamps=False)
        assert rendered == "alpha\ngamma"

    def test_empty(self) -> None:
        assert render_transcript_text([], with_timestamps=True) == ""


class TestSlugify:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Hello World", "hello-world"),
            ("Boris Cherny: The Pragmatic Engineer", "boris-cherny-the-pragmatic-engineer"),
            ("Café résumé naïve", "cafe-resume-naive"),
            ("!!!---!!!", "untitled"),
            ("", "untitled"),
            ("  spaces  ", "spaces"),
            ("Already-A-Slug", "already-a-slug"),
        ],
    )
    def test_slug(self, value: str, expected: str) -> None:
        assert slugify(value) == expected

    def test_truncates(self) -> None:
        slug = slugify("a" * 200, max_length=10)
        assert slug == "aaaaaaaaaa"
        assert len(slug) <= 10


class TestRenderResearchMarkdown:
    def test_basic_shape(self) -> None:
        result = render_research_markdown(
            transcript="[0:00] hello world",
            video_id="dQw4w9WgXcQ",
            title="Never Gonna Give You Up",
            channel="Rick Astley",
            language="English",
            is_generated=False,
            today="2026-05-09",
        )
        assert result["slug"] == "never-gonna-give-you-up"
        assert result["suggested_path"] == "research-reports/never-gonna-give-you-up.md"
        md = result["frontmatter_markdown"]
        assert md.startswith("---\n")
        assert 'title: "Never Gonna Give You Up"' in md
        assert "date: 2026-05-09" in md
        assert "Rick Astley" in md
        assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in md
        assert "type: primary-source" in md
        assert "status: permanent — do not overwrite" in md
        assert "[0:00] hello world" in md

    def test_default_title(self) -> None:
        result = render_research_markdown(
            transcript="x",
            video_id="dQw4w9WgXcQ",
            today="2026-05-09",
        )
        assert "youtube-transcript" in result["slug"]
        assert "YouTube transcript: dQw4w9WgXcQ" in result["frontmatter_markdown"]

    def test_auto_generated_label(self) -> None:
        result = render_research_markdown(
            transcript="x",
            video_id="dQw4w9WgXcQ",
            title="Test",
            is_generated=True,
            today="2026-05-09",
        )
        assert "auto-generated" in result["frontmatter_markdown"]

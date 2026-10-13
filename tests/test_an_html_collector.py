"""Tests for AN HTML debate collector (src/collectors/an_html_debates.py)."""

from unittest.mock import MagicMock, patch

import httpx

from src.collectors.an_html_debates import SESSIONS, ANHtmlCollector

# ─── Index page HTML fixtures ───

SAMPLE_INDEX_LEG12_FORMAT_A = """\
<html>
<head><title>Session 2001-2002</title></head>
<body>
<table><td>
<p><a href="20020144.asp#TopOfPage">Premiere seance du mardi 25 juin 2002</a></p>
<p><a href="20020145.asp#TopOfPage">Deuxieme seance du mardi 25 juin 2002</a></p>
<p><a href="20020146.asp#TopOfPage">Seance du mercredi 26 juin 2002</a></p>
</td></table>
</body>
</html>
"""

SAMPLE_INDEX_LEG12_FORMAT_B = """\
<html>
<head><title>Session 2003-2004</title></head>
<body>
<hr>
<p><a href="20040001.asp#TopOfPage"><b><font face="Arial" size="2">\
Premiere seance du mardi 7 octobre 2003</font></b></a></p>
<ul><li>Discussion du projet de loi de finances</li></ul>
<hr>
<p><a href="20040002.asp#TopOfPage"><b><font face="Arial" size="2">\
Deuxieme seance du mardi 7 octobre 2003</font></b></a></p>
<hr>
</body>
</html>
"""

SAMPLE_INDEX_LEG13 = """\
<html>
<head><title>Session 2011-2012</title></head>
<body>
<table id="listecri">
  <tr>
    <td class="date"><h1 class="date">mardi 6 mars 2012</h1></td>
  </tr>
  <tr>
    <td class="seance">
      <h1 class="seance"><a href="20120147.asp">Seance unique</a></h1>
      <h2 class="titre1"><a href="20120147.asp#P108_4658">Questions au Gouvernement</a></h2>
    </td>
  </tr>
  <tr>
    <td class="date"><h1 class="date">lundi 5 mars 2012</h1></td>
  </tr>
  <tr>
    <td class="seance">
      <h1 class="seance"><a href="20120145.asp">Premiere seance</a></h1>
      <h1 class="seance"><a href="20120146.asp">Deuxieme seance</a></h1>
    </td>
  </tr>
</table>
</body>
</html>
"""

SAMPLE_DEBATE_CONTENT = b"""\
<html>
<head><title>Premiere seance du mardi 6 mars 2012</title></head>
<body><!-- RSPEAK_START -->
<p>compte rendu integral de la seance</p>
<!-- RSPEAK_STOP --></body>
</html>
"""


# ─── Test classes ───


class TestANHtmlCollector:
    def test_list_debates_leg13(self, tmp_path):
        """Test list_debates with leg13 <table id="listecri"> format."""
        mock_response = MagicMock()
        mock_response.content = SAMPLE_INDEX_LEG13.encode("iso-8859-1")
        mock_response.status_code = 200

        with ANHtmlCollector(tmp_path) as collector:
            with patch.object(collector, "_get", return_value=mock_response):
                debates = collector.list_debates(13, "2011-2012")

        assert len(debates) == 3
        filenames = {d["filename"] for d in debates}
        assert "20120147.asp" in filenames
        assert "20120145.asp" in filenames
        assert "20120146.asp" in filenames

    def test_list_debates_leg12_format_a(self, tmp_path):
        """Test list_debates with leg12 Format A (plain <p><a href>)."""
        mock_response = MagicMock()
        mock_response.content = SAMPLE_INDEX_LEG12_FORMAT_A.encode("iso-8859-1")
        mock_response.status_code = 200

        with ANHtmlCollector(tmp_path) as collector:
            with patch.object(collector, "_get", return_value=mock_response):
                debates = collector.list_debates(12, "2001-2002")

        assert len(debates) == 3
        filenames = {d["filename"] for d in debates}
        assert "20020144.asp" in filenames
        assert "20020145.asp" in filenames

    def test_list_debates_leg12_format_b(self, tmp_path):
        """Test list_debates with leg12 Format B (<hr> separated)."""
        mock_response = MagicMock()
        mock_response.content = SAMPLE_INDEX_LEG12_FORMAT_B.encode("iso-8859-1")
        mock_response.status_code = 200

        with ANHtmlCollector(tmp_path) as collector:
            with patch.object(collector, "_get", return_value=mock_response):
                debates = collector.list_debates(12, "2003-2004")

        assert len(debates) == 2
        filenames = {d["filename"] for d in debates}
        assert "20040001.asp" in filenames
        assert "20040002.asp" in filenames

    def test_list_debates_404_returns_empty(self, tmp_path):
        """Test list_debates returns empty list on 404."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        error = httpx.HTTPStatusError(
            "Not Found", request=MagicMock(), response=mock_response
        )

        with ANHtmlCollector(tmp_path) as collector:
            with patch.object(collector, "_get", side_effect=error):
                debates = collector.list_debates(12, "nonexistent")

        assert debates == []

    def test_list_debates_empty_index(self, tmp_path):
        """Test list_debates returns empty list on empty index page."""
        mock_response = MagicMock()
        mock_response.content = b"<html><body><p>No debates here</p></body></html>"
        mock_response.status_code = 200

        with ANHtmlCollector(tmp_path) as collector:
            with patch.object(collector, "_get", return_value=mock_response):
                debates = collector.list_debates(12, "2001-2002")

        assert debates == []

    def test_download_debate_idempotent(self, tmp_path):
        """Test download_debate skips existing files."""
        output_path = tmp_path / "test.html"
        output_path.write_bytes(b"existing content")

        with ANHtmlCollector(tmp_path) as collector:
            result = collector.download_debate("http://example.com/test.asp", output_path)

        assert result == output_path
        assert output_path.read_bytes() == b"existing content"

    def test_download_debate_creates_dirs(self, tmp_path):
        """Test download_debate creates parent directories."""
        output_path = tmp_path / "deep" / "nested" / "dir" / "test.html"
        mock_response = MagicMock()
        mock_response.content = SAMPLE_DEBATE_CONTENT
        mock_response.status_code = 200

        with ANHtmlCollector(tmp_path) as collector:
            with patch.object(collector, "_get", return_value=mock_response):
                result = collector.download_debate(
                    "http://example.com/test.asp", output_path
                )

        assert result is not None
        assert output_path.exists()

    def test_download_debate_404_returns_none(self, tmp_path):
        """Test download_debate returns None on 404."""
        output_path = tmp_path / "test.html"
        mock_response = MagicMock()
        mock_response.status_code = 404
        error = httpx.HTTPStatusError(
            "Not Found", request=MagicMock(), response=mock_response
        )

        with ANHtmlCollector(tmp_path) as collector:
            with patch.object(collector, "_get", side_effect=error):
                result = collector.download_debate(
                    "http://example.com/test.asp", output_path
                )

        assert result is None
        assert not output_path.exists()

    def test_download_debate_content_validation(self, tmp_path):
        """Test download_debate rejects pages without debate content markers."""
        output_path = tmp_path / "test.html"
        mock_response = MagicMock()
        mock_response.content = b"<html><body>Error: page not found</body></html>"
        mock_response.status_code = 200

        with ANHtmlCollector(tmp_path) as collector:
            with patch.object(collector, "_get", return_value=mock_response):
                result = collector.download_debate(
                    "http://example.com/test.asp", output_path
                )

        assert result is None

    def test_sessions_registry_completeness(self):
        """Verify session registry has expected entries."""
        assert 12 in SESSIONS
        assert 13 in SESSIONS
        assert "2001-2002" in SESSIONS[12]
        assert "2011-2012" in SESSIONS[13]
        assert len(SESSIONS[12]) == 11
        assert len(SESSIONS[13]) == 17

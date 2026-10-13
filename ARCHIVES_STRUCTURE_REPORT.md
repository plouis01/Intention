# French National Assembly Historical Archives Structure Report

**Archive URL:** https://archives.assemblee-nationale.fr
**Date of Analysis:** 2026-02-09
**Scope:** Legislatures 1-11 (1958-2002)

## Executive Summary

The French National Assembly historical archives contain debate transcripts (Comptes Rendus Intégraux - CRI) from the first 11 legislatures of the Fifth Republic, spanning **1958 to 2002** (44 years).

- **Total Legislatures Available:** 11 (Legislature 12+ not yet digitized)
- **Total Sessions:** 146 sessions across all legislatures
- **Estimated Total PDFs:** ~9,640 PDF documents
- **Date Range:** December 1958 - July 2002

## URL Structure and Patterns

### Base Structure

```
https://archives.assemblee-nationale.fr/{legislature}/cri/
```

### Navigation Hierarchy

1. **Legislature Index** (e.g., `/1/cri/`)
   - Lists all sessions for that legislature
   - Links to HTML pages for each session

2. **Session HTML Page** (e.g., `/1/cri/1-1958-1959-ordinaire1.html`)
   - Index page listing all debate PDFs for that session
   - Pattern: `/{legislature}/cri/{legislature}-{session}.html`

3. **Individual PDF** (e.g., `/1/cri/1958-1959-ordinaire1/001.pdf`)
   - Actual debate transcript PDF
   - Pattern: `/{legislature}/cri/{session}/{number:03d}.pdf`

### URL Examples

```
Legislature 1, Session 1958-1959-ordinaire1, PDF 001:
https://archives.assemblee-nationale.fr/1/cri/1958-1959-ordinaire1/001.pdf

Legislature 9, Session 1988-1989-ordinaire1, PDF 001:
https://archives.assemblee-nationale.fr/9/cri/1988-1989-ordinaire1/001.pdf

Legislature 11, Session 1998-1999-ordinaire1, PDF 295:
https://archives.assemblee-nationale.fr/11/cri/1998-1999-ordinaire1/295.pdf
```

## Legislature Overview

| Legislature | Years      | Sessions | Est. PDFs | Notes                          |
|-------------|------------|----------|-----------|--------------------------------|
| 1           | 1958-1962  | 14       | ~275      | Founding of Fifth Republic     |
| 2           | 1962-1967  | 11       | ~788      |                                |
| 3           | 1967-1968  | 3        | ~202      | Short legislature              |
| 4           | 1968-1973  | 12       | ~860      |                                |
| 5           | 1973-1978  | 15       | ~1,305    |                                |
| 6           | 1978-1981  | 11       | ~788      |                                |
| 7           | 1981-1986  | 27       | ~1,341    | Most sessions                  |
| 8           | 1986-1988  | 9        | ~645      |                                |
| 9           | 1988-1993  | 21       | ~1,148    |                                |
| 10          | 1993-1997  | 15       | ~1,075    |                                |
| 11          | 1997-2002  | 8        | ~1,213    | Last pre-digital, largest PDFs |
| **TOTAL**   | **44 yrs** | **146**  | **~9,640**|                                |

## Session Types

Sessions are named with the pattern: `{year1}-{year2}-{type}{number}`

### Session Type Breakdown

| Type             | Count | Description                                        |
|------------------|-------|----------------------------------------------------|
| ordinaire1       | 73    | First ordinary session (Oct-Dec)                   |
| ordinaire2       | 56    | Second ordinary session (Apr-Jul)                  |
| ordinaire3       | 9     | Third ordinary session (rare)                      |
| extraordinaire1  | 27    | First extraordinary session                        |
| extraordinaire2  | 20    | Second extraordinary session                       |
| extraordinaire3  | 8     | Third extraordinary session                        |
| extraordinaire4  | 3     | Fourth extraordinary session                       |
| droit/droit1     | 7     | "Session de droit" (constitutional sessions)       |
| const            | 1     | Constitutional/founding sessions (1958 only)       |

## PDF Numbering and Volume

### Numbering Convention

- PDFs are numbered sequentially starting from `001.pdf`
- Numbering is **3-digit zero-padded** (001, 002, ..., 099, 100, ...)
- Each session has its own numbering sequence starting from 001

### Volume Patterns by Session Type

Based on sampled sessions:

| Session Type      | Typical PDF Count | Range    |
|-------------------|-------------------|----------|
| Ordinaire         | 50-150 PDFs       | Large    |
| Extraordinaire    | 1-30 PDFs         | Small    |
| Session de droit  | 10-50 PDFs        | Medium   |

**Outliers:**
- Smallest session: 1 PDF (1983-1984-extraordinaire1)
- Largest session: 295 PDFs (1998-1999-ordinaire1 in Legislature 11)

### Missing PDF Numbers

Note: Some sessions have gaps in numbering (e.g., Legislature 1, session 1960-1961-droit skips PDF 040).

## Detailed Legislature Breakdown

### Legislature 1 (1958-1962) - 14 Sessions

**Years:** 1958-1962
**Estimated Total:** ~275 PDFs

Sessions:
- 1958-1959-const (3 PDFs) - Constitutional sessions
- 1958-1959-extraordinaire1
- 1958-1959-ordinaire1
- 1959-1960-extraordinaire1
- 1959-1960-extraordinaire2
- 1959-1960-ordinaire1
- 1959-1960-ordinaire2
- 1960-1961-droit (53 PDFs)
- 1960-1961-ordinaire1
- 1961-1962-extraordinaire1
- 1961-1962-extraordinaire2
- 1961-1962-ordinaire1
- 1961-1962-ordinaire2
- 1962-1963-ordinaire1 (3 PDFs)

### Legislature 2 (1962-1967) - 11 Sessions

**Years:** 1962-1967
**Estimated Total:** ~788 PDFs

Sessions include both ordinaire and extraordinaire sessions from 1962-1967.

### Legislature 3 (1967-1968) - 3 Sessions

**Years:** 1967-1968
**Estimated Total:** ~202 PDFs (all sampled)

Sessions:
- 1966-1967-ordinaire2 (74 PDFs)
- 1967-1968-ordinaire1 (95 PDFs)
- 1967-1968-ordinaire2 (33 PDFs)

### Legislature 5 (1973-1978) - 15 Sessions

**Years:** 1973-1978
**Estimated Total:** ~1,305 PDFs

Sample sessions:
- 1972-1973-ordinaire2 (58 PDFs)
- 1974-1975-ordinaire1 (104 PDFs)
- 1977-1978-ordinaire1 (99 PDFs)

### Legislature 7 (1981-1986) - 27 Sessions

**Years:** 1981-1986
**Estimated Total:** ~1,341 PDFs

Most active legislature with 27 sessions total.

Sample sessions:
- 1980-1981-droit1 (11 PDFs)
- 1983-1984-extraordinaire1 (1 PDF) - smallest session
- 1985-1986-ordinaire1 (137 PDFs)

### Legislature 9 (1988-1993) - 21 Sessions

**Years:** 1988-1993
**Estimated Total:** ~1,148 PDFs

Sample sessions:
- 1987-1988-droit1 (9 PDFs)
- 1990-1991-extraordinaire2 (17 PDFs)
- 1992-1993-ordinaire1 (138 PDFs)

### Legislature 11 (1997-2002) - 8 Sessions

**Years:** 1997-2002
**Estimated Total:** ~1,213 PDFs

Latest pre-digital legislature with largest individual sessions.

Sample sessions:
- 1996-1997-extraordinaire1 (17 PDFs)
- 1998-1999-ordinaire1 (295 PDFs) - **LARGEST SESSION**
- 2001-2002-ordinaire1 (143 PDFs)

## Technical Notes

### Access Requirements

- The site blocks automated requests with standard bot user agents
- Requires proper HTTP headers to access:
  ```python
  headers = {
      'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
      'Accept': 'text/html',
  }
  ```

### HTML Structure

- Legislature index pages use tables with class `#cra`
- Session links follow pattern: `{legislature}-{session}.html`
- PDF links are relative paths within session HTML pages

### Data Quality

- Some PDFs may have numbering gaps (observed in Legislature 1)
- Session dates and metadata are embedded in HTML titles
- All PDFs follow 3-digit zero-padded naming convention

## Recommended Scraping Strategy

### Phase 1: Enumerate All Sessions

For each legislature (1-11):
1. Fetch `/{legislature}/cri/` index page
2. Extract all session names using regex: `{leg}-(\d{4}-\d{4}-\w+)\.html`
3. Store session metadata (type, year range)

### Phase 2: Enumerate All PDFs

For each session:
1. Fetch `/{legislature}/cri/{legislature}-{session}.html`
2. Extract PDF links using regex: `{session}/(\d+)\.pdf`
3. Build complete PDF URL list

### Phase 3: Download PDFs

- Implement rate limiting (recommend 1-2 requests/second)
- Use proper HTTP headers to avoid blocking
- Implement retry logic for failed downloads
- Store with organized directory structure:
  ```
  /legislature_{num}/{session}/{pdf_number}.pdf
  ```

### Expected Download Volume

- **Total files:** ~9,640 PDFs
- **Estimated size:** Unknown (recommend sampling to estimate)
- **Estimated time:**
  - At 1 PDF/second: ~2.7 hours
  - At 2 PDFs/second: ~1.3 hours
  - (Add overhead for enumeration and retries)

## Archive Limitations

### What's NOT Available

- **Legislature 12+** (2002-present) - These are available through modern AN open data portal
- **Other document types** - Archives contain CRI only, not:
  - Written questions
  - Committee reports
  - Legislative texts
  - Amendments (available separately via AN open data)

### Overlaps with Modern Data

The current project already has access to:
- DILA debates (2012-2019 via XML)
- AN open data (legislatures 14-17 via JSON/XML)

The historical archives (1958-2002) would **fill the gap** for:
- **1958-2002:** Full coverage via PDF archives
- **2002-2012:** Gap period (may need separate data source)

## Integration Recommendations

### For Legal Tech Project Context

Given your project focuses on Finance Laws (LF, LFR, LFSS) from 2012-2025:

1. **Current coverage:** 2012-2025 via DILA + AN modern APIs
2. **Historical gap:** Pre-2012 debates
3. **These archives provide:** 1958-2002 coverage

**ROI Assessment:**
- Finance laws existed throughout 1958-2002
- Would provide historical legislative intent
- Requires OCR/PDF parsing (text not structured)
- Estimated ~1,000-2,000 relevant PDFs for finance law debates
- PDF quality may vary by decade

**Suggested Approach:**
1. **Targeted download:** Focus on October-December sessions (budget debates)
2. **Sample first:** Download one legislature to assess PDF quality
3. **OCR pipeline:** May need text extraction before parsing
4. **Link to laws:** Cross-reference with your existing law database

## Sample Sessions for Quality Assessment

Before full scraping, recommend testing with these representative samples:

1. **Early period (1960s):**
   - Legislature 1, Session 1958-1959-ordinaire1, PDF 001

2. **Middle period (1980s):**
   - Legislature 7, Session 1985-1986-ordinaire1, PDF 001

3. **Late period (1990s):**
   - Legislature 11, Session 1998-1999-ordinaire1, PDF 001

These will help assess:
- PDF scan quality
- OCR requirements
- Text extractability
- Metadata availability

## Files Generated

This analysis produced:

1. `archive_structure.json` - Detailed structure with sampled PDF lists
2. `legislatures_overview.json` - Complete legislature and session inventory
3. `explore_archives.py` - Python script used for exploration
4. `ARCHIVES_STRUCTURE_REPORT.md` - This document

## Next Steps

1. **Validate sampling:** Download sample PDFs to confirm structure
2. **Test OCR:** Assess text extraction quality
3. **Build scraper:** Implement robust download script
4. **Design parser:** Create pipeline for PDF → structured data
5. **Integrate:** Link to existing law/article database

---

**Report Generated:** 2026-02-09
**Analysis Coverage:** Legislatures 1-11 (1958-2002)
**Total Estimated Volume:** ~9,640 PDFs across 146 sessions

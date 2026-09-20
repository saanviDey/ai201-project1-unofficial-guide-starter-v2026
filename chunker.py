"""
Stage 2 of the pipeline: splitting documents into chunks.

⚠️ THIS IS THE FILE YOU CHANGE IN MILESTONE 3.

`split_documents` below is deliberately plain. It cuts every document into
fixed-size pieces with a fixed overlap and pays no attention to where sentences
or paragraphs end. It works, and it is not good.

On a corpus of short posts it may not cut anything at all: `campus_life` comes
out as 88 documents and 88 chunks, because almost nothing in it reaches 800
characters. That is the baseline, not a bug — Milestone 3 is where you decide
whether one post should stay one chunk.

Your job in Milestone 3 is to replace the *body* of `split_documents` with a
strategy that fits the documents you actually read in Milestone 1. Keep the
name and the shape of what it returns — the rest of the pipeline calls it, and
your README has to name the function that produced your chunks.

If you get stuck for 30 minutes, `fallback_split` is the original. Switch back
to it, write down what you saw, and move on. That's a real observation about
your pipeline, not giving up.
"""

import re
from dataclasses import dataclass, field

import config
from ingest import Document


@dataclass
class Chunk:
    """One piece of one document."""

    text: str
    source: str        # which file it came from
    index: int         # which chunk within that file, starting at 0
    produced_by: str   # the function that made it — cite this in your README

    @property
    def label(self) -> str:
        return f"{self.source}#{self.index}"


def fallback_split(
    documents: list[Document],
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    """
    The starter's original chunker. Fixed-size character windows with overlap.

    Keep this function. Milestone 3's stop rule points back at it, and having
    something to compare your own strategy against is useful in unit 2.
    """
    chunk_size = chunk_size or config.CHUNK_SIZE
    overlap = overlap or config.CHUNK_OVERLAP

    if overlap >= chunk_size:
        raise ValueError("overlap has to be smaller than chunk_size")

    chunks: list[Chunk] = []
    for doc in documents:
        start = 0
        index = 0
        while start < len(doc.text):
            piece = doc.text[start : start + chunk_size].strip()
            if piece:
                chunks.append(
                    Chunk(
                        text=piece,
                        source=doc.source,
                        index=index,
                        produced_by="chunker.py::fallback_split",
                    )
                )
                index += 1
            start += chunk_size - overlap

    return chunks


# ─── Milestone 3: structure-aware chunking ───────────────────────────────────
#
# What was wrong with the fallback, in the words of the three complaints:
#
#   "some chunks are too long"   — 800 characters of a city guide runs three
#                                  unrelated sections together, so the answer
#                                  to "where do I eat" arrives buried in train
#                                  timetables.
#   "some chunks are too short"  — an 188-character campus_life post and a
#                                  single forum reply are each one chunk with
#                                  nothing around them.
#   "it's different per corpus"  — that is the real problem. A fixed window
#                                  cuts city_guides into thirds and leaves
#                                  campus_life untouched, so the same setting
#                                  means two different things.
#
# The fix is to stop measuring in characters first and measuring structure
# first. Every document in every one of these corpora is a title followed by
# labelled parts — `## sections` in the markdown guides, `--- reply N ---`
# blocks in the threads, short standalone heading lines in the long practice
# documents, plain paragraphs everywhere else. So:
#
#   1. Find the title and split the body on whichever of those markers the
#      document actually uses  (_segment).
#   2. Merge parts that are too thin to answer anything (_merge_small).
#   3. Split parts that are over the ceiling, at paragraph breaks first and
#      sentence breaks second, never mid-sentence  (_split_to_budget).
#   4. Put the title (and section heading) back on top of every chunk
#      (_header).
#
# Step 4 matters as much as the sizing. "Yeah. Cuts an 18 minute walk to about
# 6." contains no word a question about bikes would match, and Brightwater's
# "Getting around" section never says "Brightwater". The header is what gets
# embedded along with the body, so each chunk carries its own topic.
#
# The result is consistent across corpora because the *output* is specified —
# every chunk is a titled, whole-sentence unit between CHUNK_MIN_CHARS and
# CHUNK_MAX_CHARS — rather than the input being cut at a fixed stride.


@dataclass
class _Section:
    """One structural part of a document, before any sizing is applied."""

    text: str
    headings: list[str] = field(default_factory=list)


# `## Getting around`, any level.
_MD_HEADING = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")

# `--- reply 1 (14 votes) ---`, the advice_threads delimiter. The inner text
# has to be non-empty so a bare `---` rule doesn't match.
_REPLY_MARKER = re.compile(r"^\s*-{2,}\s*(\S.*?)\s*-{2,}\s*$")

# End of sentence: ., ! or ? plus any closing quote or bracket, then space.
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+")

_MAX_HEADER_CHARS = 120


def split_documents(documents: list[Document]) -> list[Chunk]:
    """
    Split documents into chunks along their own structure.

    One chunk is one titled, whole-sentence unit of a document: a markdown
    section, a run of forum replies, a short post kept intact. Every chunk is
    at most config.CHUNK_MAX_CHARS characters, and is at least
    config.CHUNK_MIN_CHARS unless the whole document is smaller than that.

    Chunks never span two documents, so the source on every chunk is the one
    file its text came from.
    """
    max_chars = config.CHUNK_MAX_CHARS
    min_chars = min(config.CHUNK_MIN_CHARS, max_chars)

    chunks: list[Chunk] = []
    for doc in documents:
        title, body = _split_title(doc.text)
        sections = _merge_small(_segment(body), title, max_chars, min_chars)

        index = 0
        for section in sections:
            header = _header(title, section.headings)
            # Reserve room for the header so the finished chunk, header and
            # all, still comes in under the ceiling.
            budget = max_chars - len(header) - 2 if header else max_chars

            for piece in _regroup_tail(
                _split_to_budget(section.text, budget), budget, min_chars
            ):
                text = f"{header}\n\n{piece}" if header else piece
                chunks.append(
                    Chunk(
                        text=text[:max_chars].strip(),
                        source=doc.source,
                        index=index,
                        produced_by="chunker.py::split_documents",
                    )
                )
                index += 1

    return chunks


def _split_title(text: str) -> tuple[str, str]:
    """
    Pull the document's title off the top, so it can be prefixed to each chunk.

    Handles a markdown `# Title`, and the plain first line that campus_life,
    practice and advice_threads all use ("THREAD: is a bike worth it?"). A long
    first line is a paragraph, not a title, and is left in the body.
    """
    lines = text.split("\n")
    first = lines[0].strip() if lines else ""
    rest = "\n".join(lines[1:]).strip()

    md = _MD_HEADING.match(first)
    if md:
        return md.group(2), rest

    # A real title is short and stands alone — blank line under it.
    second_is_blank = len(lines) > 1 and not lines[1].strip()
    if first and len(first) <= _MAX_HEADER_CHARS and second_is_blank:
        return first, rest

    return "", text.strip()


def _segment(body: str) -> list[_Section]:
    """
    Cut a document body on whatever structural marker it actually uses.

    Tried in order of how reliable the marker is. The first one that finds more
    than one part wins; a document with no markers comes back as a single
    section and gets sized by paragraph in `_split_to_budget`.
    """
    for detector in (_segment_markdown, _segment_replies, _segment_plain_headings):
        sections = detector(body)
        if len(sections) > 1:
            return sections

    return [_Section(text=body)] if body.strip() else []


def _segment_markdown(body: str) -> list[_Section]:
    """city_guides: split on `## Section` headings."""
    sections: list[_Section] = []
    heading: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            sections.append(_Section(text=text, headings=[heading] if heading else []))

    for line in body.split("\n"):
        match = _MD_HEADING.match(line)
        if match:
            flush()
            heading, buffer = match.group(2), []
        else:
            buffer.append(line)
    flush()

    return sections


def _segment_replies(body: str) -> list[_Section]:
    """
    advice_threads: split on `--- reply N (14 votes) ---`.

    The marker stays at the top of its section rather than going into the
    header: the vote count is worth showing the model, but embedding the words
    "reply 3 (22 votes)" alongside the question would only add noise.
    """
    sections: list[_Section] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            sections.append(_Section(text=text))

    for line in body.split("\n"):
        if _REPLY_MARKER.match(line):
            flush()
            buffer = [line.strip()]
        else:
            buffer.append(line)
    flush()

    return sections


def _segment_plain_headings(body: str) -> list[_Section]:
    """
    practice: split on short standalone lines like "The opening".

    Only attempted on documents long enough to need splitting at all, because
    on a short post this would happily promote an ordinary sentence fragment to
    a heading and shred it.
    """
    if len(body) <= config.CHUNK_MAX_CHARS:
        return []

    lines = body.split("\n")
    sections: list[_Section] = []
    heading: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            sections.append(_Section(text=text, headings=[heading] if heading else []))

    for i, line in enumerate(lines):
        stripped = line.strip()
        blank_above = i == 0 or not lines[i - 1].strip()
        blank_below = i + 1 < len(lines) and not lines[i + 1].strip()

        if stripped and blank_above and blank_below and _looks_like_heading(stripped):
            flush()
            heading, buffer = stripped.rstrip(":"), []
        else:
            buffer.append(line)
    flush()

    return sections


def _looks_like_heading(line: str) -> bool:
    """A short, unpunctuated, non-bulleted line on its own is a heading."""
    return (
        len(line) <= 60
        and len(line.split()) <= 9
        and not line.endswith((".", ",", ";", "!", "?"))
        and not line[0] in "-*•#>"
    )


def _merge_small(
    sections: list[_Section],
    title: str,
    max_chars: int,
    min_chars: int,
) -> list[_Section]:
    """
    Glue neighbouring sections together while either one is under the minimum.

    This is what stops a forum thread becoming four 180-character chunks: the
    replies merge into one or two chunks that each hold a whole exchange.
    Well-sized sections are left alone, so a city guide keeps one chunk per
    topic instead of being blended back together.
    """
    merged: list[_Section] = []

    for section in sections:
        if merged:
            previous = merged[-1]
            headings = _dedupe(previous.headings + section.headings)
            combined = f"{previous.text}\n\n{section.text}"
            too_thin = len(previous.text) < min_chars or len(section.text) < min_chars
            fits = len(_header(title, headings)) + 2 + len(combined) <= max_chars

            if too_thin and fits:
                merged[-1] = _Section(text=combined, headings=headings)
                continue

        merged.append(section)

    return merged


def _split_to_budget(text: str, budget: int) -> list[str]:
    """
    Fit text into pieces of at most `budget` characters.

    Paragraph breaks are the preferred cut. A paragraph that is itself over
    budget falls through to sentence breaks. Nothing is ever cut mid-sentence
    unless a single sentence is longer than the whole budget.
    """
    pieces: list[str] = []
    buffer = ""

    for paragraph in _paragraphs(text):
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) <= budget:
            buffer = candidate
            continue

        if buffer:
            pieces.append(buffer)
            buffer = ""

        if len(paragraph) <= budget:
            buffer = paragraph
        else:
            sentence_pieces = _split_sentences_to_budget(paragraph, budget)
            pieces.extend(sentence_pieces[:-1])
            buffer = sentence_pieces[-1]

    if buffer:
        pieces.append(buffer)

    return pieces or [text.strip()]


def _split_sentences_to_budget(paragraph: str, budget: int) -> list[str]:
    """
    Pack a single over-long paragraph into sentence groups.

    config.CHUNK_SENTENCE_OVERLAP sentences are carried from the end of one
    piece to the start of the next, so a thought that straddles the cut is
    still findable from either side. This is the only place overlap is used —
    sections and paragraphs have real boundaries and don't need it.
    """
    overlap = max(config.CHUNK_SENTENCE_OVERLAP, 0)
    pieces: list[str] = []
    buffer: list[str] = []

    for sentence in _sentences(paragraph):
        if len(sentence) > budget:
            # A single sentence longer than the budget. Nothing to do but cut
            # it on whitespace; this is the last resort, not the normal path.
            if buffer:
                pieces.append(" ".join(buffer))
                buffer = []
            pieces.extend(_hard_cut(sentence, budget))
            continue

        if buffer and len(" ".join(buffer + [sentence])) > budget:
            pieces.append(" ".join(buffer))
            buffer = buffer[len(buffer) - overlap :] if overlap else []
            # Don't let the carried-over sentences crowd out the new one.
            while buffer and len(" ".join(buffer + [sentence])) > budget:
                buffer.pop(0)

        buffer.append(sentence)

    if buffer:
        pieces.append(" ".join(buffer))

    return pieces or [paragraph.strip()]


def _regroup_tail(pieces: list[str], budget: int, min_chars: int) -> list[str]:
    """
    Fold a runt last piece back into the one before it, where there's room.

    Splitting on paragraphs leaves an offcut sometimes — 620 characters and
    then 40. Those 40 characters are not a retrievable answer on their own.
    """
    while len(pieces) > 1 and len(pieces[-1]) < min_chars:
        combined = f"{pieces[-2]}\n\n{pieces[-1]}"
        if len(combined) > budget:
            break
        pieces[-2:] = [combined]

    return pieces


def _header(title: str, headings: list[str]) -> str:
    """
    The line that goes on top of a chunk: "Brightwater — Getting around".

    Gives every chunk its own topic words, which is what lets a section that
    never repeats the document's name still match a question about it.
    """
    parts = _dedupe([p for p in [title, *headings] if p])
    return " — ".join(parts)[:_MAX_HEADER_CHARS].strip()


def _paragraphs(text: str) -> list[str]:
    """Blank-line-separated blocks, blanks dropped."""
    return [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]


def _sentences(text: str) -> list[str]:
    """Sentences, with line breaks inside a paragraph treated as breaks too."""
    sentences: list[str] = []
    for line in text.split("\n"):
        if line.strip():
            sentences.extend(s.strip() for s in _SENTENCE_END.split(line) if s.strip())
    return sentences


def _hard_cut(text: str, budget: int) -> list[str]:
    """Cut on whitespace at the budget. Only used on a runaway sentence."""
    pieces: list[str] = []
    while len(text) > budget:
        cut = text.rfind(" ", 0, budget)
        cut = cut if cut > budget // 2 else budget
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving dedupe, so a merged section doesn't repeat a heading."""
    seen: set[str] = set()
    return [v for v in values if not (v in seen or seen.add(v))]


def describe(chunks: list[Chunk]) -> str:
    """A one-line summary, printed after indexing."""
    if not chunks:
        return "0 chunks"
    lengths = [len(c.text) for c in chunks]
    return (
        f"{len(chunks)} chunks, "
        f"{sum(lengths) // len(lengths)} characters on average "
        f"(shortest {min(lengths)}, longest {max(lengths)}), "
        f"produced by {chunks[0].produced_by}"
    )


if __name__ == "__main__":
    from ingest import load_documents

    chunks = split_documents(load_documents())
    print(describe(chunks))

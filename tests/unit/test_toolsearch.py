"""Tool retrieval: stemming, scoring, and the find_tool / call_tool contract.

These are the invariants the retrieval layer must hold regardless of how the
ranking is tuned. Ranking *quality* is measured separately and numerically by
tests/toolsearch-eval; this file guards the properties that a tuning change
must never break.
"""
from __future__ import annotations

import pytest


class TestStem:
    """The stemmer only has to be consistent, not linguistically correct: the
    same function runs over the query and the index, so both sides agree."""

    @pytest.mark.parametrize(
        "word,stem",
        [("messages", "message"), ("replies", "reply"), ("days", "day"),
         ("muting", "mut"), ("blocked", "block"),
         ("boxes", "box"), ("matches", "match")],
    )
    def test_folds_variants(self, toolsearch, word, stem):
        assert toolsearch._stem(word) == stem

    @pytest.mark.parametrize(
        "singular,plural",
        [("message", "messages"), ("note", "notes"), ("file", "files"),
         ("name", "names"), ("image", "images"), ("device", "devices"),
         ("poll", "polls"), ("contact", "contacts"), ("reply", "replies"),
         ("box", "boxes"), ("match", "matches")],
    )
    def test_singular_and_plural_land_on_the_same_stem(self, toolsearch, singular, plural):
        """The only property that matters: not correctness, agreement.

        A blanket 'es' strip broke this for every noun ending in -e, which is
        most of this domain (message, file, name, image, note). See the _stem
        docstring.
        """
        assert toolsearch._stem(singular) == toolsearch._stem(plural)

    @pytest.mark.parametrize("word", ["is", "as", "des", "yes"])
    def test_short_words_are_not_mangled_away(self, toolsearch, word):
        """The length guard exists so a suffix strip can't empty a word."""
        assert toolsearch._stem(word) == word

    def test_plural_noun_converges_with_its_singular(self, toolsearch):
        """The stem this retrieval actually relies on: a plural in the query
        reaches the singular in a tool name. (The `-ing` verb form is a known
        gap - `scheduling` stems to `schedul`, not `schedule` - which the alias
        map in toolsearch.py covers instead; see its `schedule_message` entry.)"""
        assert toolsearch._stem("polls") == toolsearch._stem("poll")
        assert toolsearch._stem("schedules") == toolsearch._stem("schedule")


class TestTokens:
    def test_lowercases_and_splits_on_punctuation(self, toolsearch):
        assert toolsearch._tokens("Send-Message, PLEASE!") == ["send", "message"]

    def test_drops_stopwords(self, toolsearch):
        assert "the" not in toolsearch._tokens("the message in the chat")

    def test_terse_query_falls_back_rather_than_emptying(self, toolsearch):
        """'show me' is all stopwords; returning [] would match nothing."""
        assert toolsearch._tokens("show me") != []

    def test_handles_empty_input(self, toolsearch):
        assert toolsearch._tokens("") == []


class TestSignature:
    """_sig() renders the one-line parameter hint find_tool shows, so the model
    can call a tool without a second round-trip for the full schema."""

    def test_marks_optional_params_with_a_question_mark(self, toolsearch):
        sig = toolsearch._sig({
            "type": "object",
            "properties": {"chat_jid": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["chat_jid"],
        })
        assert sig == "chat_jid:string, limit?:integer"

    def test_collapses_nullable_unions(self, toolsearch):
        """FastMCP renders Optional[str] as anyOf/[string,null]; show the branch."""
        sig = toolsearch._sig({
            "type": "object",
            "properties": {"q": {"type": ["string", "null"]}},
            "required": [],
        })
        assert sig == "q?:string"

    def test_unknown_type_degrades_to_any(self, toolsearch):
        assert toolsearch._sig(
            {"type": "object", "properties": {"x": {}}, "required": ["x"]}
        ) == "x:any"

    @pytest.mark.parametrize("params", [None, "not-a-dict", {}, {"type": "object"}])
    def test_malformed_schema_returns_empty_not_an_error(self, toolsearch, params):
        assert toolsearch._sig(params) == ""


class TestVectorMath:
    def test_unit_normalises(self, toolsearch):
        v = toolsearch._unit([3.0, 4.0])
        assert v == pytest.approx([0.6, 0.8])

    def test_unit_leaves_the_zero_vector_alone(self, toolsearch):
        """A zero vector has no direction; dividing would raise."""
        assert toolsearch._unit([0.0, 0.0]) == [0.0, 0.0]

    def test_cosine_of_identical_unit_vectors_is_one(self, toolsearch):
        v = toolsearch._unit([1.0, 2.0, 3.0])
        assert toolsearch._cos(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self, toolsearch):
        assert toolsearch._cos([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    @pytest.mark.parametrize("a,b", [(None, [1.0]), ([1.0], None), ([], []), (None, None)])
    def test_missing_embedding_is_zero_not_a_crash(self, toolsearch, a, b):
        """Semantics are optional; a tool with no vector must not break search."""
        assert toolsearch._cos(a, b) == 0.0


class TestCapture:
    def test_indexes_every_tool(self, library):
        assert len(library._LIBRARY) == 8
        assert len(library._INDEX) == 8

    def test_idf_makes_common_terms_cheap(self, library):
        """'message' spans several tools; 'poll' is distinctive. Rarer scores higher."""
        assert library._IDF[library._stem("poll")] > library._IDF[library._stem("message")]


class TestRanking:
    @pytest.mark.parametrize(
        "query,expected",
        [
            ("send a text to someone", "send_message"),
            ("what does this voice note say", "transcribe_voice"),
            ("make a poll for the group", "create_poll"),
            ("find someone's phone number", "search_contacts"),
            ("stop hearing from this person", "block_user"),
        ],
    )
    def test_plain_language_reaches_the_right_tool(self, library, query, expected):
        assert expected in [h["name"] for h in library._search(query, 5)]

    def test_exact_name_ranks_first(self, library):
        assert library._search("create_poll", 5)[0]["name"] == "create_poll"

    def test_results_are_ordered_by_score(self, library):
        ranked = library._ranked("send a message", 8)
        assert [s for s, *_ in ranked] == sorted((s for s, *_ in ranked), reverse=True)

    def test_respects_the_limit(self, library):
        assert len(library._search("message", 3)) == 3

    def test_results_carry_what_the_model_needs_to_call(self, library):
        hit = next(h for h in library._search("send a text", 5) if h["name"] == "send_message")
        assert hit["description"]
        assert "chat_jid:string" in hit["params"]
        assert "reply_to_message_id?:string" in hit["params"]

    def test_nonsense_query_returns_without_raising(self, library):
        """A miss is allowed; an exception is not."""
        assert isinstance(library._search("zzzz qqqq", 5), list)


class TestBrowse:
    def test_catalog_lists_every_non_meta_tool(self, library):
        names = {row["name"] for row in library._catalog(10_000)}
        assert names == set(library._LIBRARY)          # all 8, none dropped
        assert names.isdisjoint(library._META_NAMES)   # and no meta-tools

    def test_catalog_is_sorted_by_name(self, library):
        names = [row["name"] for row in library._catalog(10_000)]
        assert names == sorted(names)

    def test_catalog_respects_the_limit(self, library):
        assert len(library._catalog(3)) == 3

    @pytest.mark.parametrize("token", ["", "*", "all", "everything", "catalog"])
    def test_browse_tokens_are_recognised(self, library, token):
        """These are the queries find_tool routes to the full catalog browse."""
        assert token in library._BROWSE_TOKENS


class TestSuggest:
    def test_suggests_near_misses_for_a_typo(self, library):
        assert "create_poll" in library._suggest("create_polls")

    def test_unknown_name_still_returns_a_list(self, library):
        assert isinstance(library._suggest("totally_unknown"), list)


class TestMetaToolsAreNotRecursive:
    def test_meta_tools_cannot_dispatch_themselves(self, toolsearch):
        """call_tool(find_tool) would let the model loop inside the dispatcher."""
        assert {"find_tool", "call_tool"} == toolsearch._META_NAMES

    def test_meta_names_are_excluded_from_the_library(self, library):
        for name in library._META_NAMES:
            assert name not in library._LIBRARY

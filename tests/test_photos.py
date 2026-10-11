"""Tests for the photo providers.

Every payload here is the shape measured against the live API, so a provider
can be changed without a network call and a change in response shape shows up
as a failing test rather than as "no picture" in a child's chat.
"""
import pytest

from kitegate.images import safe_image_url
from kitegate.photos import (BLOCKED_SUBJECT, DEFAULT_PROVIDERS, PROVIDERS, Nasa, Wikimedia,
                             get_providers, image_hosts_for, next_search_title, shorten,
                             strip_tracking)

WIKI = "https://simple.wikipedia.org"
# As measured: the scaled thumbnail host, with the tracking parameters the API
# actually appends.
TIGER_THUMB = ("https://thumb.wikimedia.org/wikipedia/commons/thumb/3/3b/"
               "Royal_Bengal_Tiger_at_Kanha_National_Park.jpg/960px-Royal_Bengal_Tiger.jpg"
               "?utm_source=simple.wikipedia.org&utm_campaign=api&utm_content=thumbnail")
# And the unscaled host, which the same API returns for other subjects.
JUPITER_THUMB = ("https://upload.wikimedia.org/wikipedia/commons/2/2b/"
                 "Jupiter_and_its_shrunken_Great_Red_Spot.jpg?utm_source=simple.wikipedia.org")


def wiki_page(**over):
    """A MediaWiki query response. Pass thumbnail=None to drop the lead image,
    which is what a disambiguation page actually looks like."""
    page = {"pageid": 1, "title": "Tiger",
            "thumbnail": {"source": TIGER_THUMB, "width": 960, "height": 640},
            "extract": "The tiger (Panthera tigris) is the largest living member of the "
                       "cat family.\nIt lives in Asia."}
    page.update(over)
    page = {k: v for k, v in page.items() if v is not None}
    return {"batchcomplete": "", "query": {"pages": {"1": page}}}


# -- the registry ---------------------------------------------------------------
def test_wikimedia_is_the_default():
    assert DEFAULT_PROVIDERS == ("wikimedia",)
    assert [p.name for p in get_providers(None)] == ["wikimedia"]
    assert [p.name for p in get_providers("")] == ["wikimedia"]


def test_providers_are_used_in_the_order_given():
    assert [p.name for p in get_providers("nasa,wikimedia")] == ["nasa", "wikimedia"]


def test_an_unknown_provider_is_refused_not_skipped():
    # A typo must not read as "pictures are off": that is indistinguishable
    # from an outage.
    with pytest.raises(ValueError, match="unknown photo provider"):
        get_providers("wikipedia")


def test_nasa_is_declared_topical():
    # Measured: "tiger" returns 69 NASA hits, all of them Enceladus. A caller
    # and the docs need to be able to say so.
    assert PROVIDERS["nasa"].general is False and PROVIDERS["nasa"].topic
    assert PROVIDERS["wikimedia"].general is True


# -- the host allowlist, which is the easiest thing to get silently wrong ------
def test_wikimedia_declares_both_of_its_image_hosts():
    assert set(image_hosts_for("wikimedia")) == {"thumb.wikimedia.org", "upload.wikimedia.org"}


@pytest.mark.parametrize("url", [TIGER_THUMB, JUPITER_THUMB])
def test_real_provider_urls_pass_the_gateways_own_allowlist(url):
    # The cross-check that matters: the provider declares its hosts and the
    # gateway enforces them. If the two ever disagree, working pictures vanish
    # with no error anywhere.
    assert safe_image_url(strip_tracking(url), image_hosts_for("wikimedia")) is None


def test_hosts_for_several_providers_are_merged_without_duplicates():
    hosts = image_hosts_for("wikimedia,nasa")
    assert hosts == ["thumb.wikimedia.org", "upload.wikimedia.org", "images-assets.nasa.gov"]


# -- Wikimedia ------------------------------------------------------------------
def test_one_request_resolves_the_title_and_the_image():
    request = Wikimedia().lookup(WIKI, "tiger", 640)
    assert request.url == f"{WIKI}/w/api.php"
    # redirects=1 plus MediaWiki's own capitalisation is what makes "tiger"
    # reach "Tiger" without a second call.
    assert request.params["redirects"] == 1
    assert request.params["pithumbsize"] == 640
    assert request.params["titles"] == "tiger"


def test_a_lead_image_becomes_a_photo():
    title, photo = Wikimedia().read_lookup(wiki_page(), WIKI)
    assert title == "Tiger"
    assert photo.title == "Tiger" and photo.provider == "wikimedia"
    assert photo.source == "simple.wikipedia.org"
    assert photo.caption.startswith("The tiger (Panthera tigris)")
    assert "\n" not in photo.caption          # first line only
    assert "utm_" not in photo.image_url      # tracking stripped


def test_a_disambiguation_page_gives_a_title_but_no_photo():
    # Measured on "Mercury": a title, an extract listing the meanings, and no
    # thumbnail at all. That is the signal to search, not an error.
    title, photo = Wikimedia().read_lookup(
        wiki_page(title="Mercury", thumbnail=None,
                  extract="Mercury could mean:\n\nMercury (planet)"), WIKI)
    assert title == "Mercury" and photo is None


def test_a_missing_page_gives_nothing():
    data = {"query": {"pages": {"-1": {"title": "Wibblefish", "missing": ""}}}}
    assert Wikimedia().read_lookup(data, WIKI) == (None, None)


@pytest.mark.parametrize("data", [{}, {"query": {}}, {"query": {"pages": None}}, None])
def test_a_malformed_response_is_not_an_exception(data):
    assert Wikimedia().read_lookup(data or {}, WIKI) == (None, None)


def test_search_results_are_read_in_order():
    data = {"query": {"search": [{"title": "Mercury"}, {"title": "Mercury (element)"},
                                 {"title": "Mercury (planet)"}]}}
    assert Wikimedia().read_search(data) == ["Mercury", "Mercury (element)", "Mercury (planet)"]


# -- the disambiguation fallback -----------------------------------------------
def test_the_already_tried_title_is_skipped():
    # The real "Mercury" case. The first search result is the disambiguation
    # page that just failed; retrying it would give up and show no picture.
    titles = ["Mercury", "Mercury (element)", "Mercury (planet)"]
    assert next_search_title(titles, ["Mercury", "Mercury"]) == "Mercury (element)"


def test_an_unsuitable_search_result_is_skipped():
    # Search can land somewhere unsuitable for an innocent query, so a
    # resolved title is not trusted just because the input passed.
    assert next_search_title(["Nude descending a staircase", "Staircase"], ["stairs"]) == "Staircase"


def test_no_usable_search_result_is_none():
    assert next_search_title(["Tiger"], ["tiger"]) is None
    assert next_search_title([], []) is None


# -- NASA -----------------------------------------------------------------------
def nasa_items(*items):
    return {"collection": {"items": list(items)}}


def nasa_item(title="Jupiter Plume", preview="https://images-assets.nasa.gov/image/PIA01518/"
                                             "PIA01518~thumb.jpg", description="Jupiter Plume"):
    links = [{"href": preview, "rel": "preview", "render": "image"}] if preview else []
    return {"data": [{"title": title, "description": description, "center": "JPL"}],
            "links": links}


def test_nasa_reads_the_preview_link():
    title, photo = Nasa().read_lookup(nasa_items(nasa_item()), "https://images-api.nasa.gov")
    assert title == "Jupiter Plume"
    assert photo.image_url.endswith("PIA01518~thumb.jpg")
    assert photo.provider == "nasa" and photo.source == "images-api.nasa.gov"


def test_nasa_skips_an_item_with_no_preview():
    # Measured: ~small, ~medium and ~large return 403, so a size cannot be
    # constructed. An item without a preview link is simply unusable.
    data = nasa_items(nasa_item(preview=None), nasa_item(title="Jupiter Ahoy!"))
    title, photo = Nasa().read_lookup(data, "https://images-api.nasa.gov")
    assert title == "Jupiter Ahoy!" and photo is not None


def test_nasa_skips_an_unsuitable_title():
    data = nasa_items(nasa_item(title="Autopsy of a star"), nasa_item(title="Jupiter"))
    title, _ = Nasa().read_lookup(data, "https://images-api.nasa.gov")
    assert title == "Jupiter"


def test_nasa_with_no_results():
    assert Nasa().read_lookup(nasa_items(), "https://images-api.nasa.gov") == (None, None)


# -- subject screening ----------------------------------------------------------
@pytest.mark.parametrize("subject", [
    "naked mole rat", "nude painting", "human anatomy", "autopsy", "a gory battle",
    "gore", "childbirth", "beheading of Anne Boleyn", "cocaine", "bikini", "syringe",
])
def test_imagery_risks_are_refused(subject):
    assert BLOCKED_SUBJECT.search(subject)


@pytest.mark.parametrize("subject", [
    # The false-positive fixtures that matter: a \b-anchored list must not
    # refuse innocent natural history.
    "Drugstore beetle", "needlefish", "Humphrey", "Scunthorpe", "peacock",
    "shuttlecock", "breaststroke", "robin redbreast", "Sexsmith",
    "Great Pyramid of Giza", "tiger", "flag of Japan", "Jupiter",
    # The words that made widening "gore" to "gor(e|y)" worth checking.
    "Gregory the Great", "allegory", "category", "gorilla",
])
def test_innocent_subjects_are_not_refused(subject):
    assert not BLOCKED_SUBJECT.search(subject)


# -- captions -------------------------------------------------------------------
def test_a_long_caption_is_trimmed_on_a_word_boundary():
    caption = shorten("word " * 100)
    assert len(caption) <= 240 and caption.endswith("...")
    assert not caption.endswith(" ...")


def test_a_caption_is_the_first_line_only():
    assert shorten("First line.\nSecond line.") == "First line."


def test_tracking_is_stripped_but_real_parameters_are_kept():
    assert strip_tracking("https://h/x.jpg?utm_source=a&width=100") == "https://h/x.jpg?width=100"
    assert strip_tracking("https://h/x.jpg") == "https://h/x.jpg"
    assert strip_tracking("https://h/x.jpg?utm_source=a") == "https://h/x.jpg"


# -- a redirect must not land on an unsuitable article --------------------------
def test_a_lookup_that_resolves_to_a_blocked_title_gives_no_photo():
    # MediaWiki follows redirects, so an innocent subject can come back as
    # another article. Its title is checked, not only the words asked for.
    data = {"query": {"pages": {"1": {
        "title": "Nudity", "thumbnail": {"source": "https://upload.wikimedia.org/x.jpg"}}}}}
    assert Wikimedia().read_lookup(data, "https://en.wikipedia.org") == (None, None)


def test_a_blocked_title_is_found_through_look_alike_characters():
    from kitegate.photos import is_blocked
    assert is_blocked("Nu\u200bdity") and is_blocked("n\u0430ked") and not is_blocked("Tiger")


@pytest.mark.parametrize("subject", ["tiger|Mercury", "tiger\x1fMercury", "tiger\n", "a<b>c", "{x}"])
def test_a_subject_that_could_name_several_pages_has_forbidden_characters(subject):
    from kitegate.photos import has_forbidden_characters
    assert has_forbidden_characters(subject)


@pytest.mark.parametrize("subject", ["tiger", "Great Pyramid of Giza", "Mercury (planet)", "caf\u00e9"])
def test_an_ordinary_subject_has_no_forbidden_characters(subject):
    from kitegate.photos import has_forbidden_characters
    assert not has_forbidden_characters(subject)

"""paper_meta_out 投影：published_online 形态校验与兜底。"""

from __future__ import annotations

from loomvec.api.services.assets import paper_meta_out


def test_paper_meta_out_projects_published_online():
    out = paper_meta_out(
        {
            "paper": {
                "published_online": " 2016-06-27 ",
                "source": "crossref",
            }
        }
    )
    assert out == {
        "year": None,
        "journal": None,
        "title": None,
        "first_author": None,
        "authors": [],
        "doi": None,
        "published_online": "2016-06-27",
        "source": "crossref",
    }


def test_paper_meta_out_rejects_invalid_published_online():
    assert paper_meta_out({"paper": {"published_online": "2016-02-31"}}) is None
    assert paper_meta_out({"paper": {"published_online": "tomorrow"}}) is None
    assert paper_meta_out({"paper": {"published_online": 2016}}) is None


def test_paper_meta_out_falls_back_when_only_invalid_online():
    out = paper_meta_out(
        {
            "paper": {
                "year": 2016,
                "published_online": "not-a-date",
                "source": "crossref",
            }
        }
    )
    assert out is not None
    assert out["year"] == 2016
    assert out["published_online"] is None


def test_paper_meta_out_hides_explicit_non_paper():
    assert paper_meta_out({"paper": {"is_paper": False, "published_online": "2016-06-27"}}) is None

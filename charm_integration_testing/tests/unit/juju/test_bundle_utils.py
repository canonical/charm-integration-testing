# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju.bundle_utils import (
    parse_offer_names_from_bundle,
    parse_offers_from_bundle,
    strip_offers_from_bundle,
    strip_saas_from_bundle,
)

# ---------------------------------------------------------------------------
# Fixtures: sample bundle YAML
# ---------------------------------------------------------------------------

SIMPLE_BUNDLE = """\
applications:
  postgresql:
    charm: postgresql
    channel: 14/stable
relations:
- - postgresql:database
  - postgresql:database
"""

CMR_TARGET_BUNDLE = """\
applications:
  glauth-utils:
    charm: glauth-utils
    channel: latest/edge
saas:
  glauth-k8s:
    url: admin/neighbor-model:glauth-k8s
relations:
- - glauth-utils:glauth-auxiliary
  - glauth-k8s:glauth-auxiliary
- - glauth-utils:logging
  - glauth-utils:logging
"""

CMR_BUNDLE_WITH_OFFERS = """\
applications:
  glauth-k8s:
    charm: glauth-k8s
    channel: latest/edge
---
applications:
  glauth-k8s:
    offers:
      glauth-k8s:
        endpoints:
        - glauth-auxiliary
"""


# ---------------------------------------------------------------------------
# Tests: strip_saas_from_bundle
# ---------------------------------------------------------------------------


def test_strip_saas_removes_saas_section() -> None:
    result = strip_saas_from_bundle(CMR_TARGET_BUNDLE)
    assert "saas:" not in result
    assert "glauth-k8s" not in result


def test_strip_saas_removes_cross_model_relations() -> None:
    result = strip_saas_from_bundle(CMR_TARGET_BUNDLE)
    assert "glauth-auxiliary" not in result


def test_strip_saas_keeps_local_relations() -> None:
    result = strip_saas_from_bundle(CMR_TARGET_BUNDLE)
    assert "logging" in result


def test_strip_saas_no_op_for_simple_bundle() -> None:
    result = strip_saas_from_bundle(SIMPLE_BUNDLE)
    # Relations should be preserved
    assert "database" in result
    assert "saas" not in result


def test_strip_saas_preserves_overlay_documents() -> None:
    result = strip_saas_from_bundle(CMR_BUNDLE_WITH_OFFERS)
    assert "---" in result
    assert "offers:" in result
    assert "glauth-auxiliary" in result


# ---------------------------------------------------------------------------
# Tests: parse_offer_names_from_bundle
# ---------------------------------------------------------------------------


def test_parse_offer_names_finds_offers() -> None:
    names = parse_offer_names_from_bundle(CMR_BUNDLE_WITH_OFFERS)
    assert names == {"glauth-k8s"}


def test_parse_offer_names_empty_for_simple_bundle() -> None:
    names = parse_offer_names_from_bundle(SIMPLE_BUNDLE)
    assert names == set()


def test_parse_offer_names_empty_for_cmr_target_bundle() -> None:
    # The target bundle has saas (it consumes an offer) but doesn't define offers itself.
    names = parse_offer_names_from_bundle(CMR_TARGET_BUNDLE)
    assert names == set()


def test_parse_offer_names_multiple_offers() -> None:
    bundle = """\
applications:
  app-a:
    charm: app-a
---
applications:
  app-a:
    offers:
      offer-one:
        endpoints:
        - endpoint-a
      offer-two:
        endpoints:
        - endpoint-b
"""
    names = parse_offer_names_from_bundle(bundle)
    assert names == {"offer-one", "offer-two"}


# ---------------------------------------------------------------------------
# Tests: parse_offers_from_bundle
# ---------------------------------------------------------------------------


def test_parse_offers_from_bundle_returns_app_and_endpoints() -> None:
    offers = parse_offers_from_bundle(CMR_BUNDLE_WITH_OFFERS)
    assert "glauth-k8s" in offers
    assert offers["glauth-k8s"].app == "glauth-k8s"
    assert offers["glauth-k8s"].endpoints == ["glauth-auxiliary"]


def test_parse_offers_from_bundle_empty_for_simple_bundle() -> None:
    assert parse_offers_from_bundle(SIMPLE_BUNDLE) == {}


def test_parse_offers_from_bundle_empty_for_target_bundle() -> None:
    assert parse_offers_from_bundle(CMR_TARGET_BUNDLE) == {}


# ---------------------------------------------------------------------------
# Tests: strip_offers_from_bundle
# ---------------------------------------------------------------------------


def test_strip_offers_removes_offers_from_overlay() -> None:
    result = strip_offers_from_bundle(CMR_BUNDLE_WITH_OFFERS)
    assert "offers:" not in result


def test_strip_offers_keeps_base_bundle_unchanged() -> None:
    result = strip_offers_from_bundle(CMR_BUNDLE_WITH_OFFERS)
    assert "glauth-k8s:" in result
    assert "charm: glauth-k8s" in result


def test_strip_offers_drops_empty_overlay() -> None:
    # If the only content of the overlay was offers, the overlay is dropped entirely.
    result = strip_offers_from_bundle(CMR_BUNDLE_WITH_OFFERS)
    assert "---" not in result


def test_strip_offers_no_op_for_simple_bundle() -> None:
    result = strip_offers_from_bundle(SIMPLE_BUNDLE)
    assert "postgresql" in result


def test_strip_offers_preserves_non_offer_overlay_content() -> None:
    bundle = """\
applications:
  app-a:
    charm: app-a
---
applications:
  app-a:
    offers:
      my-offer:
        endpoints:
        - ep-a
    annotations:
      gui-x: "10"
"""
    result = strip_offers_from_bundle(bundle)
    assert "my-offer" not in result
    assert "annotations" in result
    assert "---" in result  # overlay retained because annotations remain


# ---------------------------------------------------------------------------
# Tests: input validation and defensive handling
# ---------------------------------------------------------------------------


def test_strip_saas_raises_on_empty_bundle() -> None:
    with pytest.raises(ValueError, match="base bundle"):
        strip_saas_from_bundle("")


def test_strip_saas_raises_on_non_mapping_bundle() -> None:
    with pytest.raises(ValueError, match="base bundle"):
        strip_saas_from_bundle("- item1\n- item2\n")


def test_strip_offers_raises_on_empty_bundle() -> None:
    with pytest.raises(ValueError, match="base bundle"):
        strip_offers_from_bundle("")


def test_strip_offers_raises_on_non_mapping_bundle() -> None:
    with pytest.raises(ValueError, match="base bundle"):
        strip_offers_from_bundle("- item1\n- item2\n")


def test_parse_offers_skips_non_dict_overlay() -> None:
    bundle = """\
applications:
  myapp:
    charm: myapp
---
- not_a_mapping
"""
    assert parse_offers_from_bundle(bundle) == {}


def test_parse_offers_skips_null_app_entry() -> None:
    bundle = """\
applications:
  myapp:
    charm: myapp
---
applications:
  myapp: null
"""
    assert parse_offers_from_bundle(bundle) == {}


def test_strip_saas_handles_null_saas_value() -> None:
    # A bundle with "saas: null" should not raise AttributeError
    bundle = "applications:\n  myapp:\n    charm: myapp\nsaas: null\n"
    result = strip_saas_from_bundle(bundle)
    assert "saas" not in result


def test_parse_offers_skips_null_applications_in_overlay() -> None:
    bundle = "applications:\n  myapp:\n    charm: myapp\n---\napplications: null\n"
    assert parse_offers_from_bundle(bundle) == {}


def test_parse_offers_skips_null_offers_value() -> None:
    bundle = "applications:\n  myapp:\n    charm: myapp\n" "---\napplications:\n  myapp:\n    offers: null\n"
    assert parse_offers_from_bundle(bundle) == {}


def test_parse_offers_skips_null_offer_data() -> None:
    bundle = (
        "applications:\n  myapp:\n    charm: myapp\n"
        "---\napplications:\n  myapp:\n    offers:\n      my-offer: null\n"
    )
    assert parse_offers_from_bundle(bundle) == {}


def test_strip_offers_handles_null_applications_in_overlay() -> None:
    bundle = "applications:\n  myapp:\n    charm: myapp\n" "---\napplications: null\n"
    # Should not raise; null applications overlay is treated as empty and dropped
    result = strip_offers_from_bundle(bundle)
    assert "myapp" in result


def test_parse_offers_skips_offer_with_missing_endpoints() -> None:
    bundle = (
        "applications:\n  myapp:\n    charm: myapp\n" "---\napplications:\n  myapp:\n    offers:\n      my-offer: {}\n"
    )
    assert parse_offers_from_bundle(bundle) == {}


def test_parse_offers_skips_offer_with_string_endpoints() -> None:
    # A scalar string for endpoints must not be iterated char-by-char
    bundle = (
        "applications:\n  myapp:\n    charm: myapp\n"
        "---\napplications:\n  myapp:\n    offers:\n      my-offer:\n        endpoints: ep-a\n"
    )
    assert parse_offers_from_bundle(bundle) == {}


def test_parse_offers_skips_offer_with_null_endpoints() -> None:
    bundle = (
        "applications:\n  myapp:\n    charm: myapp\n"
        "---\napplications:\n  myapp:\n    offers:\n      my-offer:\n        endpoints: null\n"
    )
    assert parse_offers_from_bundle(bundle) == {}

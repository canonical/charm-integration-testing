# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from juju import JujuModelHandle


class TestJujuModelHandle:
    class TestOwner:
        def test_uri_omits_owner_when_unset(self) -> None:
            # GIVEN a handle without an owner (the common case: models are otherwise identified
            # by their bare name throughout this framework)
            handle = JujuModelHandle(controller="my-controller", model="my-model")

            # THEN the URI is unqualified
            assert handle.uri == "my-controller:my-model"

        def test_uri_includes_owner_when_set(self) -> None:
            # GIVEN a handle qualified with an owner, e.g. to address a model whose owner
            # differs from the currently authenticated user
            handle = JujuModelHandle(controller="my-controller", model="my-model", owner="admin")

            # THEN the URI includes the owner
            assert handle.uri == "my-controller:admin/my-model"

        def test_owner_is_not_part_of_identity(self) -> None:
            # GIVEN two handles for the same controller/model that differ only by owner
            bare = JujuModelHandle(controller="my-controller", model="my-model")
            qualified = JujuModelHandle(controller="my-controller", model="my-model", owner="admin")

            # THEN they compare equal, hash the same, and share the same resource_id -- owner is
            # an addressing detail, not part of the model's identity for comparison/tracking
            # purposes elsewhere in this framework (e.g. dict/set lookups by bare model handle)
            assert bare == qualified
            assert hash(bare) == hash(qualified)
            assert bare.resource_id == qualified.resource_id
